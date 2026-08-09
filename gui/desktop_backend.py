"""Isolated, local-only Claude Desktop profile management.

Each saved Desktop profile owns a complete Electron user-data root.  Switching
only selects a root and launches a verified Claude Desktop executable with
``CLAUDE_USER_DATA_DIR``; it never copies credentials or Chromium storage.
"""
from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import subprocess
import time
import uuid
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional


MANIFEST_SCHEMA = 3
OAUTH_KEY_V1 = "oauth:tokenCache"
OAUTH_KEY_V2 = "oauth:tokenCacheV2"


class StorageMode(str, Enum):
    DEFAULT = "default"
    ISOLATED = "isolated"


class ProfileState(str, Enum):
    ACTIVE = "active"
    READY = "ready"
    NEEDS_VALIDATION = "needs_validation"
    NEEDS_RELOGIN = "needs_relogin"
    CORRUPT = "corrupt"
    UNKNOWN_LIVE_LOGIN = "unknown_live_login"


@dataclass(frozen=True)
class BackupRef:
    """Legacy migration reference retained for the Task 2 recovery API."""

    path: Path


@dataclass(frozen=True)
class DesktopProfile:
    name: str
    label: str
    note: str
    updated: float
    state: ProfileState
    account_id_hash: str = ""
    issue: str = ""
    storage_mode: StorageMode = StorageMode.ISOLATED
    user_data_dir: Optional[Path] = None


@dataclass(frozen=True)
class PendingLogin:
    name: str
    user_data_dir: Path


@dataclass(frozen=True)
class LaunchResult:
    ok: bool
    target_name: str = ""
    user_data_dir: Optional[Path] = None
    message: str = ""


@dataclass
class MigrationReport:
    migrated: list[str] = field(default_factory=list)
    needs_relogin: list[str] = field(default_factory=list)
    corrupt: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    issues: dict[str, str] = field(default_factory=dict)
    backup: Optional[BackupRef] = None


@dataclass(frozen=True)
class SyncReport:
    ok: bool
    added: int = 0
    removed: int = 0
    message: str = ""


@dataclass(frozen=True)
class SwitchResult:
    ok: bool
    target_name: str
    backup: Optional[BackupRef] = None
    history: Optional[SyncReport] = None
    recovery_backup: Optional[Path] = None
    message: str = ""
    should_relaunch: bool = False


@dataclass(frozen=True)
class ExecutableSpec:
    path: Path
    version: str
    kind: str


@dataclass(frozen=True)
class _LaunchVerification:
    proof_key: str
    ready: bool


@dataclass(frozen=True)
class _VerifiedDesktopProcess:
    pid: int
    parent_pid: int
    started: str


class DesktopBackendError(RuntimeError):
    pass


class SnapshotValidationError(DesktopBackendError):
    pass


class ProcessDetectionError(DesktopBackendError):
    pass


class UnknownLiveAccountError(DesktopBackendError):
    pass


class RollbackError(DesktopBackendError):
    """Compatibility error; schema-3 does not restore copied live state."""

    def __init__(self, message: str, recovery_path: Path):
        super().__init__(message)
        self.recovery_path = recovery_path


def _sha256(value: str | bytes) -> str:
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def _version_key(version: str) -> tuple[int, ...]:
    """Sort dotted installer versions numerically, not lexicographically."""
    return tuple(int(part) for part in re.findall(r"\d+", version)) or (0,)


class WindowsDesktopProcessAdapter:
    """Windows boundary for verified Claude Desktop processes and launches."""

    _NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

    def __init__(self) -> None:
        self._managed_pids: dict[int, str] = {}

    def _run(self, command: str) -> str:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            timeout=12,
            creationflags=self._NO_WINDOW,
        )
        if completed.returncode:
            raise OSError("Claude Desktop platform query failed")
        return completed.stdout

    def _verified_squirrel(self) -> list[ExecutableSpec]:
        install = Path(os.environ.get("LOCALAPPDATA", "")) / "AnthropicClaude"
        candidates: list[ExecutableSpec] = []
        for directory in install.glob("app-*"):
            executable = directory / "Claude.exe"
            if executable.is_file() and self._valid_product(executable):
                candidates.append(ExecutableSpec(executable, directory.name[4:], "squirrel"))
        return sorted(candidates, key=lambda item: _version_key(item.version), reverse=True)

    def _verified_msix(self) -> list[ExecutableSpec]:
        command = (
            "@(Get-AppxPackage -Name Claude -ErrorAction Stop | "
            "Where-Object { $_.PackageFamilyName -eq 'Claude_pzs8sxrjxfjjc' "
            "-and $_.Publisher -like '*Anthropic, PBC*' } | "
            "ForEach-Object { @{path=(Join-Path $_.InstallLocation 'app\\Claude.exe'); "
            "version=$_.Version} }) | ConvertTo-Json -Compress"
        )
        try:
            rows = json.loads(self._run(command) or "[]")
        except (OSError, json.JSONDecodeError):
            return []
        if isinstance(rows, dict):
            rows = [rows]
        candidates: list[ExecutableSpec] = []
        for row in rows if isinstance(rows, list) else []:
            path = Path(str(row.get("path") or "")) if isinstance(row, dict) else Path()
            if path.is_file() and self._valid_product(path):
                candidates.append(ExecutableSpec(path, str(row.get("version") or "unknown"), "msix"))
        return sorted(candidates, key=lambda item: _version_key(item.version), reverse=True)

    def _valid_product(self, executable: Path) -> bool:
        escaped = str(executable).replace("'", "''")
        command = (
            "$v=(Get-Item -LiteralPath '" + escaped + "').VersionInfo; "
            "if ($v.ProductName -like '*Claude*' -and $v.FileDescription -like '*Claude*') { 'ok' }"
        )
        try:
            return self._run(command).strip() == "ok"
        except OSError:
            return False

    def resolve_executable(self) -> ExecutableSpec:
        candidates = self._verified_squirrel() or self._verified_msix()
        if not candidates:
            raise OSError("No verified Claude Desktop executable was found")
        return candidates[0]

    def _verified_desktop_processes(self) -> list[_VerifiedDesktopProcess]:
        if os.name != "nt":
            raise OSError("Claude Desktop switching is Windows-only")
        rows = json.loads(
            self._run(
                "@(Get-CimInstance Win32_Process -Filter \"Name='claude.exe'\" | "
                "Select-Object ProcessId,ParentProcessId,CreationDate,ExecutablePath) | ConvertTo-Json -Compress"
            )
            or "[]"
        )
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list):
            raise OSError("Claude Desktop process query returned invalid data")
        processes: list[_VerifiedDesktopProcess] = []
        for row in rows:
            executable = str(row.get("ExecutablePath") or "") if isinstance(row, dict) else ""
            path = Path(executable)
            if not executable:
                raise OSError("A claude.exe process could not be identified safely")
            if path.name.lower() != "claude.exe":
                continue
            normalized = executable.replace("/", "\\").lower()
            if "\\claude-code\\" in normalized or "\\@anthropic-ai\\claude-code\\" in normalized or normalized.endswith("\\.local\\bin\\claude.exe"):
                continue
            if not self._valid_product(path):
                raise OSError("An unrecognized Claude executable is running")
            processes.append(
                _VerifiedDesktopProcess(
                    pid=int(row["ProcessId"]),
                    parent_pid=int(row.get("ParentProcessId") or 0),
                    started=str(row.get("CreationDate") or ""),
                )
            )
        return processes

    def _verified_desktop_pids(self) -> list[int]:
        return [process.pid for process in self._verified_desktop_processes()]

    def desktop_pids(self) -> list[int]:
        return self._verified_desktop_pids()

    def unknown_desktop_pids(self) -> list[int]:
        processes = self._verified_desktop_processes()
        by_pid = {process.pid: process for process in processes}
        for pid, started in list(self._managed_pids.items()):
            current = by_pid.get(pid)
            if current is None or (started and current.started != started):
                self._managed_pids.pop(pid, None)
            elif not started:
                # Bind a just-spawned PID to its WMI creation timestamp before
                # allowing it to trust descendants on later polls.
                self._managed_pids[pid] = current.started
        managed = set(self._managed_pids)
        changed = True
        while changed:
            changed = False
            for process in processes:
                if process.parent_pid in managed and process.pid not in managed:
                    managed.add(process.pid)
                    changed = True
        return sorted(set(by_pid) - managed)

    def request_close(self, pids: list[int]) -> None:
        for pid in pids:
            self._run(f"(Get-Process -Id {int(pid)} -ErrorAction SilentlyContinue).CloseMainWindow() | Out-Null")

    def wait_stopped(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.desktop_pids():
                return True
            time.sleep(0.25)
        return not self.desktop_pids()

    def force_stop(self, pids: list[int]) -> None:
        for pid in pids:
            subprocess.run(["taskkill.exe", "/F", "/PID", str(int(pid))], capture_output=True, timeout=8, creationflags=self._NO_WINDOW)

    def launch(self, executable: ExecutableSpec, env: dict[str, str]) -> None:
        process = subprocess.Popen([str(executable.path)], env=env, creationflags=self._NO_WINDOW)
        if isinstance(process.pid, int):
            self._managed_pids[process.pid] = ""
            try:
                for candidate in self._verified_desktop_processes():
                    if candidate.pid == process.pid:
                        self._managed_pids[process.pid] = candidate.started
                        break
            except OSError:
                # The post-launch process proof will retry this query and fail
                # closed if the child tree cannot be verified.
                pass


class DesktopBackend:
    """Schema-3 Desktop profile engine with injectable system boundaries."""

    def __init__(
        self,
        *,
        claude_dir: Optional[Path] = None,
        cache_dir: Optional[Path] = None,
        oauth_decoder: Optional[Callable[[str], Optional[dict]]] = None,
        cookie_decoder: Optional[Callable[[bytes], Optional[str]]] = None,
        claude_version: Optional[Callable[[], str]] = None,
        process_adapter: Any = None,
        history_sync: Optional[Callable[[], Any]] = None,
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> None:
        home = Path.home()
        self.claude_dir = claude_dir or home / "AppData" / "Roaming" / "Claude"
        self.cache_dir = cache_dir or home / ".kalikot-claude-switcher"
        self.desktop_data_dir = self.cache_dir / "desktop-data"
        self.profiles_dir = self.cache_dir / "profiles"  # read-only legacy store for Task 2
        self.meta_file = self.cache_dir / "meta.json"
        self._process = process_adapter or WindowsDesktopProcessAdapter()
        self._history_sync = history_sync
        self._oauth_decoder = oauth_decoder
        self._cookie_decoder = cookie_decoder
        self._claude_version = claude_version
        self._fault_hook = fault_hook or (lambda _step: None)
        self._os_crypt_key_cache: Optional[bytes] = None

    def _load_meta(self) -> dict[str, Any]:
        meta = _load_json(self.meta_file)
        if not isinstance(meta.get("profiles"), dict):
            meta["profiles"] = {}
        if not isinstance(meta.get("launch_proofs"), dict):
            meta["launch_proofs"] = {}
        # Schema-2's active key is read once for migration compatibility.  New
        # metadata writes intentionally contain desktop_active only.
        if "desktop_active" not in meta:
            meta["desktop_active"] = meta.get("active")
        return meta

    def _save_meta(self, meta: dict[str, Any]) -> None:
        meta["schema"] = MANIFEST_SCHEMA
        meta.pop("active", None)
        meta.pop("aumid", None)
        _atomic_json(self.meta_file, meta)

    @staticmethod
    def _valid_name(name: str) -> bool:
        return bool(name) and len(name) <= 64 and all(character.isalnum() or character in "-_" for character in name)

    def _root_for_entry(self, name: str, entry: dict[str, Any]) -> Path:
        try:
            mode = StorageMode(entry.get("storage_mode", StorageMode.ISOLATED.value))
        except ValueError as error:
            raise SnapshotValidationError(f"Profile '{name}' has an unsupported storage mode") from error
        return self.claude_dir if mode is StorageMode.DEFAULT else self.desktop_data_dir / name

    def _entry(self, name: str) -> tuple[dict[str, Any], Path]:
        meta = self._load_meta()
        entry = meta["profiles"].get(name)
        if not isinstance(entry, dict):
            raise SnapshotValidationError(f"Profile '{name}' does not exist")
        return entry, self._root_for_entry(name, entry)

    def _read_account_hash(self, root: Path) -> str:
        config = _load_json(root / "config.json")
        account_uuid = config.get("lastKnownAccountUuid")
        if not isinstance(account_uuid, str) or not account_uuid.strip():
            raise SnapshotValidationError("Claude Desktop config.json has no lastKnownAccountUuid")
        return _sha256(account_uuid)

    @staticmethod
    def _oauth_organization_hashes(config: dict[str, Any]) -> list[str]:
        """Return cache-key organization metadata only; never an account identity."""
        organizations: set[str] = set()
        for key in (OAUTH_KEY_V1, OAUTH_KEY_V2):
            raw = config.get(key)
            if not isinstance(raw, str):
                continue
            try:
                decoded = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(decoded, dict):
                continue
            for cache_key in decoded:
                parts = cache_key.split(":") if isinstance(cache_key, str) else []
                if len(parts) > 1 and parts[0].lower() in {"org", "organization"} and parts[1]:
                    organizations.add(parts[1])
        return sorted(_sha256(value) for value in organizations)

    def _profile_from_entry(self, name: str, entry: dict[str, Any], active: str | None) -> DesktopProfile:
        root = self._root_for_entry(name, entry)
        try:
            mode = StorageMode(entry.get("storage_mode", StorageMode.ISOLATED.value))
            state = ProfileState(entry.get("state", ProfileState.NEEDS_RELOGIN.value))
        except ValueError:
            mode, state = StorageMode.ISOLATED, ProfileState.CORRUPT
        if name == active and state is ProfileState.READY:
            state = ProfileState.ACTIVE
        return DesktopProfile(
            name=name,
            label=str(entry.get("label") or name),
            note=str(entry.get("note") or ""),
            updated=float(entry.get("updated") or 0),
            state=state,
            account_id_hash=str(entry.get("account_id_sha256") or ""),
            issue=str(entry.get("state_reason") or ""),
            storage_mode=mode,
            user_data_dir=root,
        )

    def list_profiles(self) -> list[DesktopProfile]:
        try:
            meta = self._load_meta()
            return [self._profile_from_entry(name, entry, meta.get("desktop_active")) for name, entry in sorted(meta["profiles"].items()) if isinstance(entry, dict)]
        finally:
            self._clear_decryption_cache()

    def active_user_data_dir(self) -> Path:
        try:
            meta = self._load_meta()
            active = meta.get("desktop_active")
            entry = meta["profiles"].get(active) if isinstance(active, str) else None
            return self._root_for_entry(active, entry) if isinstance(entry, dict) else self.claude_dir
        finally:
            self._clear_decryption_cache()

    def begin_new_login(self) -> PendingLogin:
        try:
            meta = self._load_meta()
            name = f"pending-{uuid.uuid4().hex}"
            root = self.desktop_data_dir / name
            root.mkdir(parents=True, exist_ok=False)
            meta["pending_login"] = {"name": name, "created": time.time()}
            self._save_meta(meta)
            return PendingLogin(name, root)
        finally:
            self._clear_decryption_cache()

    def finalize_current(self, name: str, label: str, note: str) -> DesktopProfile:
        try:
            if not self._valid_name(name):
                raise SnapshotValidationError("Profile names may contain only letters, numbers, '-' and '_'")
            meta = self._load_meta()
            pending = meta.get("pending_login")
            if not isinstance(pending, dict) or not isinstance(pending.get("name"), str):
                raise SnapshotValidationError("No pending isolated login is available")
            pending_root = self.desktop_data_dir / pending["name"]
            destination = self.desktop_data_dir / name
            if not pending_root.is_dir() or destination.exists() or name in meta["profiles"]:
                raise SnapshotValidationError("The pending login cannot be finalized safely")
            account_hash = self._read_account_hash(pending_root)
            config = _load_json(pending_root / "config.json")
            os.replace(pending_root, destination)
            meta["profiles"][name] = {
                "label": label.strip() or name,
                "note": note.strip(),
                "updated": time.time(),
                "storage_mode": StorageMode.ISOLATED.value,
                "state": ProfileState.NEEDS_VALIDATION.value,
                "state_reason": "Awaiting verified managed launch",
                "account_id_sha256": account_hash,
                "oauth_organization_sha256": self._oauth_organization_hashes(config),
            }
            meta.pop("pending_login", None)
            meta["desktop_active"] = name
            self._save_meta(meta)
            return self._profile_from_entry(name, meta["profiles"][name], name)
        finally:
            self._clear_decryption_cache()

    def verify_profile(self, name: str) -> DesktopProfile:
        try:
            meta = self._load_meta()
            entry = meta["profiles"].get(name)
            if not isinstance(entry, dict):
                raise SnapshotValidationError(f"Profile '{name}' does not exist")
            root = self._root_for_entry(name, entry)
            try:
                actual_hash = self._read_account_hash(root)
                if actual_hash != entry.get("account_id_sha256"):
                    raise SnapshotValidationError("config.json account identity no longer matches this profile")
                config = _load_json(root / "config.json")
                entry["oauth_organization_sha256"] = self._oauth_organization_hashes(config)
                if entry.get("state") == ProfileState.CORRUPT.value:
                    entry["state"] = ProfileState.NEEDS_VALIDATION.value
                    entry["state_reason"] = "Awaiting verified managed launch"
            except SnapshotValidationError as error:
                entry["state"] = ProfileState.CORRUPT.value
                entry["state_reason"] = str(error)
            entry["updated"] = time.time()
            self._save_meta(meta)
            return self._profile_from_entry(name, entry, meta.get("desktop_active"))
        finally:
            self._clear_decryption_cache()

    def _validated_profile(self, name: str) -> tuple[dict[str, Any], Path]:
        if not self._valid_name(name):
            raise SnapshotValidationError("Invalid profile name")
        entry, root = self._entry(name)
        if entry.get("storage_mode") != StorageMode.ISOLATED.value:
            raise SnapshotValidationError("Only isolated profiles can be managed by schema-3 switching")
        actual_hash = self._read_account_hash(root)
        if actual_hash != entry.get("account_id_sha256"):
            raise SnapshotValidationError("Profile config.json does not match saved account metadata")
        return entry, root

    def _process_state(self) -> list[int]:
        try:
            unknown = getattr(self._process, "unknown_desktop_pids", lambda: [])()
            if unknown:
                raise UnknownLiveAccountError("An unknown external Claude Desktop launch is running")
            return list(self._process.desktop_pids())
        except UnknownLiveAccountError:
            raise
        except Exception as error:
            raise ProcessDetectionError("Could not verify whether Claude Desktop is running") from error

    def _stop_desktop(self) -> bool:
        pids = self._process_state()
        if not pids:
            return False
        try:
            self._process.request_close(pids)
            if not self._process.wait_stopped(6.0):
                remaining = self._process_state()
                if remaining:
                    self._process.force_stop(remaining)
                if not self._process.wait_stopped(4.0):
                    raise DesktopBackendError("Claude Desktop did not stop")
            if self._process_state():
                raise DesktopBackendError("Claude Desktop shutdown could not be proven")
        except DesktopBackendError:
            raise
        except Exception as error:
            raise ProcessDetectionError("Claude Desktop shutdown could not be proven") from error
        return True

    def desktop_pids(self) -> list[int]:
        try:
            return self._process_state()
        finally:
            self._clear_decryption_cache()

    def desktop_running(self) -> bool:
        return bool(self.desktop_pids())

    def stop_desktop(self) -> bool:
        try:
            return self._stop_desktop()
        finally:
            self._clear_decryption_cache()

    @staticmethod
    def _normalise_executable(value: Any) -> ExecutableSpec:
        if isinstance(value, ExecutableSpec):
            return value
        if isinstance(value, dict):
            path = value.get("path")
            version = value.get("version")
            kind = value.get("kind")
            if isinstance(path, (str, Path)) and isinstance(version, str) and isinstance(kind, str):
                return ExecutableSpec(Path(path), version, kind)
        raise OSError("Claude Desktop executable verification returned invalid metadata")

    def _resolve_executable(self) -> ExecutableSpec:
        resolver = getattr(self._process, "resolve_executable", None)
        if not callable(resolver):
            raise OSError("The platform adapter cannot resolve a verified Claude executable")
        executable = self._normalise_executable(resolver())
        if executable.kind not in {"squirrel", "msix"} or not executable.version or not str(executable.path):
            raise OSError("Claude Desktop executable verification failed")
        return executable

    @staticmethod
    def _log_bytes(root: Path) -> bytes:
        try:
            return (root / "Logs" / "main.log").read_bytes()
        except OSError:
            return b""

    def _launch_root(self, root: Path, executable: ExecutableSpec, proof_keys: set[str]) -> _LaunchVerification:
        if self._process_state():
            raise DesktopBackendError("Claude Desktop is already running")
        before = self._log_bytes(root)
        environment = dict(os.environ)
        environment["CLAUDE_USER_DATA_DIR"] = str(root)
        self._process.launch(executable, environment)
        if not self._process_state():
            raise DesktopBackendError("Claude Desktop launch could not be verified")
        after = self._log_bytes(root)
        new_log = after[len(before):] if after.startswith(before) else after
        text = new_log.decode("utf-8", errors="ignore").lower()
        proof_key = f"{executable.path}|{executable.version}"
        # A cached proof records that this executable/version previously honored
        # isolation.  Every launch still needs its own selected-root startup log:
        # otherwise a later ignored environment variable would be invisible.
        if not ("startup" in text or "started" in text):
            raise DesktopBackendError("Claude Desktop did not write a startup record in the selected root")
        return _LaunchVerification(
            proof_key=proof_key,
            ready=("account active" in text or "logged in" in text or "login successful" in text),
        )

    def _apply_launch_verification(self, meta: dict[str, Any], name: str, verification: _LaunchVerification) -> None:
        meta["launch_proofs"][verification.proof_key] = {"verified_at": time.time()}
        entry = meta["profiles"][name]
        if verification.ready:
            entry["state"] = ProfileState.READY.value
            entry["state_reason"] = ""

    def launch_active(self) -> LaunchResult:
        try:
            meta = self._load_meta()
            name = meta.get("desktop_active")
            if not isinstance(name, str):
                return LaunchResult(False, message="No Desktop profile is selected")
            _entry, root = self._validated_profile(name)
            executable = self._resolve_executable()
            try:
                verification = self._launch_root(root, executable, set(meta["launch_proofs"]))
            except DesktopBackendError as error:
                return LaunchResult(False, name, root, str(error))
            except Exception as error:
                return LaunchResult(False, name, root, str(error) or type(error).__name__)
            self._apply_launch_verification(meta, name, verification)
            self._save_meta(meta)
            return LaunchResult(True, name, root)
        finally:
            self._clear_decryption_cache()

    def switch(self, target_name: str) -> SwitchResult:
        try:
            meta = self._load_meta()
            _target_entry, target_root = self._validated_profile(target_name)
            previous_name = meta.get("desktop_active") if isinstance(meta.get("desktop_active"), str) else None
            was_running = bool(self._process_state())
            if was_running:
                self._stop_desktop()
            history = self.sync_histories()
            if was_running:
                executable = self._resolve_executable()
                try:
                    verification = self._launch_root(target_root, executable, set(meta["launch_proofs"]))
                except Exception as error:
                    if previous_name and previous_name in meta["profiles"]:
                        try:
                            previous_root = self._root_for_entry(previous_name, meta["profiles"][previous_name])
                            self._launch_root(previous_root, executable, set(meta["launch_proofs"]))
                        except Exception:
                            pass
                    return SwitchResult(False, target_name, history=history, message=str(error) or type(error).__name__)
                self._apply_launch_verification(meta, target_name, verification)
            # Commit selection last: failed stop/detection/launch paths return above
            # without altering metadata or copying any root content.
            meta["desktop_active"] = target_name
            self._save_meta(meta)
            return SwitchResult(True, target_name, history=history, message="Profile selected")
        finally:
            self._clear_decryption_cache()

    def sync_histories(self) -> SyncReport:
        try:
            value = self._history_sync() if self._history_sync is not None else {"added": 0, "removed": 0}
            if isinstance(value, SyncReport):
                return value
            if isinstance(value, dict):
                return SyncReport(True, int(value.get("added", 0)), int(value.get("removed", value.get("deleted", 0))))
            return SyncReport(True)
        except Exception as error:
            return SyncReport(False, message=str(error) or type(error).__name__)
        finally:
            self._clear_decryption_cache()

    def audit_and_migrate(self) -> MigrationReport:
        """Record schema-3 metadata only; lossless legacy recovery is Task 2."""
        try:
            meta = self._load_meta()
            report = MigrationReport()
            # This reader deliberately does not rewrite legacy profiles or roots.
            # It only establishes the schema-3 metadata envelope and preserves a
            # schema-2 active selection as desktop_active for a later migration.
            if not self.meta_file.exists() or meta.get("schema") != MANIFEST_SCHEMA:
                self._save_meta(meta)
            report.unchanged.extend(sorted(name for name in meta["profiles"] if isinstance(name, str)))
            return report
        finally:
            self._clear_decryption_cache()

    # Compatibility names retained until Task 3 changes the GUI handlers.  They
    # do not capture, clear, restore, copy, or rollback any Desktop data.
    def prepare_new_login(self) -> PendingLogin:
        return self.begin_new_login()

    def capture_current(self, name: str, label: str, note: str) -> DesktopProfile:
        return self.finalize_current(name, label, note)

    @staticmethod
    def _dpapi_unprotect(data: bytes) -> bytes:
        class Blob(ctypes.Structure):
            _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_char))]
        source_buffer = ctypes.create_string_buffer(data, len(data))
        source = Blob(len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_char)))
        destination = Blob()
        if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(destination)):
            raise OSError("CryptUnprotectData failed")
        try:
            return ctypes.string_at(destination.data, destination.size)
        finally:
            ctypes.windll.kernel32.LocalFree(destination.data)

    def _os_crypt_key(self) -> bytes:
        if self._os_crypt_key_cache:
            return self._os_crypt_key_cache
        state = _load_json(self.active_user_data_dir() / "Local State")
        encrypted = base64.b64decode(state["os_crypt"]["encrypted_key"])
        if not encrypted.startswith(b"DPAPI"):
            raise ValueError("Unexpected os_crypt key prefix")
        self._os_crypt_key_cache = self._dpapi_unprotect(encrypted[5:])
        return self._os_crypt_key_cache

    def _clear_decryption_cache(self) -> None:
        self._os_crypt_key_cache = None


_default_backend: Optional[DesktopBackend] = None


def _default() -> DesktopBackend:
    global _default_backend
    if _default_backend is None:
        _default_backend = DesktopBackend()
    return _default_backend


def audit_and_migrate() -> MigrationReport:
    return _default().audit_and_migrate()


def list_profiles() -> list[DesktopProfile]:
    return _default().list_profiles()


def begin_new_login() -> PendingLogin:
    return _default().begin_new_login()


def finalize_current(name: str, label: str, note: str) -> DesktopProfile:
    return _default().finalize_current(name, label, note)


def verify_profile(name: str) -> DesktopProfile:
    return _default().verify_profile(name)


def switch(target_name: str) -> SwitchResult:
    return _default().switch(target_name)


def launch_active() -> LaunchResult:
    return _default().launch_active()


def sync_histories() -> SyncReport:
    return _default().sync_histories()
