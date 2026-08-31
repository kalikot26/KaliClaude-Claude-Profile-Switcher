"""Isolated, local-only Claude Desktop profile management.

Each saved Desktop profile owns a complete Electron user-data root.  Switching
only selects a root and launches a verified Claude Desktop executable with
``--user-data-dir`` / ``CLAUDE_USER_DATA_DIR``. The original
`%APPDATA%\\Claude` window is never stopped. Profile windows are extra
processes. Credentials and Chromium storage are never copied between roots.
"""
from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
import uuid
import re
from contextlib import closing
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path, PureWindowsPath
from typing import Any, Callable, Optional


MANIFEST_SCHEMA = 3
OAUTH_KEY_V1 = "oauth:tokenCache"
OAUTH_KEY_V2 = "oauth:tokenCacheV2"
MAX_OPERATIONAL_BACKUPS = 5
MAX_HISTORY_BACKUPS = 5
CC_SESSION_ROOTS = ("claude-code-sessions", "local-agent-mode-sessions")
JSONL_NAME = ".jsonl"
_USER_DATA_DIR_RE = re.compile(
    r"--user-data-dir(?:=|\s+)(?:\"([^\"]+)\"|'([^']+)'|(\S+))",
    re.IGNORECASE,
)
LEGACY_SESSION_ITEMS = (
    "Session Storage",
    "IndexedDB",
    "Local Storage",
    "Network/Cookies",
    "Network/Cookies-journal",
    "Network/Cookies-wal",
    "Network/Cookies-shm",
)


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
    already_isolation_verified: bool


@dataclass(frozen=True)
class _VerifiedDesktopProcess:
    pid: int
    parent_pid: int
    started: str
    executable: str = ""
    command_line: str = ""
    user_data_dir: Optional[Path] = None


@dataclass(frozen=True)
class _LegacyGeneration:
    path: Path
    oauth_v1: bytes
    oauth_v2: bytes


@dataclass(frozen=True)
class _LegacyRecovery:
    path: Path
    config: dict[str, Any]
    account_id_hash: str


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


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def _windows_path_key(path: Path | str) -> str:
    return str(path).replace("/", "\\").lower()


def _is_codex_virtualized_path(path: Path | str) -> bool:
    key = _windows_path_key(path)
    return "\\packages\\openai.codex_" in key and "\\anthropicclaude\\" in key


def _parse_user_data_dir(command_line: str) -> Optional[Path]:
    match = _USER_DATA_DIR_RE.search(command_line or "")
    if match is None:
        return None
    raw = next((group for group in match.groups() if group), None)
    if not raw:
        return None
    try:
        return Path(raw)
    except (OSError, ValueError):
        return None


def _same_user_data_root(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return _windows_path_key(left) == _windows_path_key(right)


class WindowsDesktopProcessAdapter:
    """Windows boundary for verified Claude Desktop processes and launches."""

    _NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

    def __init__(
        self,
        *,
        platform_name: str | None = None,
        launcher: Callable[..., Any] | None = None,
    ) -> None:
        self._managed_pids: dict[int, str] = {}
        self._platform_name = platform_name or os.name
        self._launcher = launcher or subprocess.Popen

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
        if self._platform_name != "nt":
            raise OSError("Claude Desktop switching is Windows-only")
        rows = json.loads(
            self._run(
                "@(Get-CimInstance Win32_Process -Filter \"Name='claude.exe'\" | "
                "Select-Object ProcessId,ParentProcessId,CreationDate,ExecutablePath,CommandLine) | ConvertTo-Json -Compress"
            )
            or "[]"
        )
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list):
            raise OSError("Claude Desktop process query returned invalid data")
        verified_paths = {
            _windows_path_key(executable.path)
            for executable in [*self._verified_squirrel(), *self._verified_msix()]
        }
        if not verified_paths and rows:
            raise OSError("No installed Claude Desktop executable could be verified")
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
            if _is_codex_virtualized_path(path):
                raise OSError("Codex-packaged Claude is not a verified Desktop executable")
            if normalized not in verified_paths:
                raise OSError("A Claude executable is not an installed verified Desktop executable")
            command_line = str(row.get("CommandLine") or "") if isinstance(row, dict) else ""
            processes.append(
                _VerifiedDesktopProcess(
                    pid=int(row["ProcessId"]),
                    parent_pid=int(row.get("ParentProcessId") or 0),
                    started=str(row.get("CreationDate") or ""),
                    executable=executable,
                    command_line=command_line,
                    user_data_dir=_parse_user_data_dir(command_line),
                )
            )
        return processes

    def _verified_desktop_pids(self) -> list[int]:
        return [process.pid for process in self._verified_desktop_processes()]

    def desktop_pids(self) -> list[int]:
        return self._verified_desktop_pids()

    def desktop_processes(self) -> list[_VerifiedDesktopProcess]:
        return self._verified_desktop_processes()

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
        command = [str(executable.path)]
        user_data_dir = env.get("CLAUDE_USER_DATA_DIR")
        if user_data_dir:
            command.extend(["--user-data-dir", user_data_dir])
        process = self._launcher(
            command, env=env, creationflags=self._NO_WINDOW
        )
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
        launch_timeout: float = 8.0,
        launch_poll_interval: float = 0.05,
    ) -> None:
        home = Path.home()
        appdata = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
        self.claude_dir = claude_dir or appdata / "Claude"
        self.cache_dir = cache_dir or home / ".kalikot-claude-switcher"
        self.desktop_data_dir = self.cache_dir / "desktop-data"
        self.profiles_dir = self.cache_dir / "profiles"  # read-only legacy store for Task 2
        self.meta_file = self.cache_dir / "meta.json"
        self.backups_dir = self.cache_dir / "backups"
        self.history_manifest = self.cache_dir / "cc-sync-manifest.json"
        self._process = process_adapter or WindowsDesktopProcessAdapter()
        self._history_sync = history_sync
        self._oauth_decoder = oauth_decoder
        self._cookie_decoder = cookie_decoder
        self._claude_version = claude_version
        self._fault_hook = fault_hook or (lambda _step: None)
        self._launch_timeout = launch_timeout
        self._launch_poll_interval = launch_poll_interval
        self._os_crypt_key_cache: Optional[bytes] = None
        self._decryption_root: Optional[Path] = None

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
        raw_path = entry.get("root_path")
        if isinstance(raw_path, str) and raw_path.strip():
            return Path(raw_path)
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
            pending = meta.get("pending_login")
            if isinstance(pending, dict) and isinstance(pending.get("name"), str):
                pending_root = self.desktop_data_dir / pending["name"]
                if pending_root.is_dir():
                    return pending_root
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

    def discard_pending_login(self, pending_name: str) -> None:
        """Remove a failed fresh-login root without touching saved profiles."""
        try:
            meta = self._load_meta()
            pending = meta.get("pending_login")
            name = pending.get("name") if isinstance(pending, dict) else None
            if not isinstance(name, str):
                return
            if name != pending_name:
                raise SnapshotValidationError("The pending login changed before cleanup")
            root = (self.desktop_data_dir / name).resolve()
            if root.parent != self.desktop_data_dir.resolve() or not name.startswith("pending-"):
                raise SnapshotValidationError("The pending login path is not managed safely")
            try:
                self._stop_root(root)
            except Exception:
                meta.pop("pending_login", None)
                self._save_meta(meta)
                raise
            meta.pop("pending_login", None)
            self._save_meta(meta)
            if root.exists():
                shutil.rmtree(root)
        finally:
            self._clear_decryption_cache()

    def update_profile_usage(
        self, name: str, usage: dict[str, Any], plan: str = ""
    ) -> None:
        """Persist non-secret usage fields; account email remains memory-only."""
        try:
            meta = self._load_meta()
            entry = meta["profiles"].get(name)
            if not isinstance(entry, dict):
                raise SnapshotValidationError(f"Profile '{name}' does not exist")
            safe_usage: dict[str, Any] = {}
            for bucket in ("five_hour", "seven_day"):
                value = usage.get(bucket)
                if isinstance(value, dict):
                    safe_usage[bucket] = {
                        key: value.get(key)
                        for key in ("utilization", "resets_at")
                        if value.get(key) is not None
                    }
            fetched = usage.get("fetched")
            if isinstance(fetched, (int, float)):
                safe_usage["fetched"] = float(fetched)
            entry["usage"] = safe_usage
            if isinstance(plan, str) and plan.strip():
                entry["plan"] = plan.strip()[:64]
            entry.pop("email", None)
            self._save_meta(meta)
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
            meta["profiles"][name] = {
                "label": label.strip() or name,
                "note": note.strip(),
                "updated": time.time(),
                "storage_mode": StorageMode.ISOLATED.value,
                "state": ProfileState.NEEDS_VALIDATION.value,
                "state_reason": "Awaiting verified managed launch",
                "account_id_sha256": account_hash,
                "oauth_organization_sha256": self._oauth_organization_hashes(config),
                "root_path": str(pending_root),
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
        actual_hash = self._read_account_hash(root)
        if actual_hash != entry.get("account_id_sha256"):
            raise SnapshotValidationError("Profile config.json does not match saved account metadata")
        return entry, root

    def _process_state(self) -> list[int]:
        try:
            classifier = getattr(self._process, "unknown_desktop_pids", None)
            if not callable(classifier):
                raise ProcessDetectionError("The process adapter cannot classify external Claude Desktop launches")
            unknown = classifier()
            allowed = self._allowed_live_pids()
            unknown = [pid for pid in unknown if pid not in allowed]
            if unknown:
                raise UnknownLiveAccountError("An unknown external Claude Desktop launch is running")
            return list(self._process.desktop_pids())
        except UnknownLiveAccountError:
            raise
        except Exception as error:
            raise ProcessDetectionError("Could not verify whether Claude Desktop is running") from error

    def _known_user_data_roots(self) -> list[Path]:
        roots = [self.claude_dir]
        meta = _load_json(self.meta_file)
        pending = meta.get("pending_login")
        if isinstance(pending, dict) and isinstance(pending.get("name"), str):
            roots.append(self.desktop_data_dir / pending["name"])
        for name, entry in (meta.get("profiles") or {}).items():
            if isinstance(name, str) and isinstance(entry, dict) and self._valid_name(name):
                roots.append(self._root_for_entry(name, entry))
        return roots

    def _allowed_live_pids(self) -> set[int]:
        allowed: set[int] = set()
        for root in self._known_user_data_roots():
            allowed.update(self._pids_for_root(root))
        return allowed

    def _desktop_processes(self) -> list[_VerifiedDesktopProcess]:
        listing = getattr(self._process, "desktop_processes", None)
        if callable(listing):
            records = listing()
            if isinstance(records, list):
                return [record for record in records if isinstance(record, _VerifiedDesktopProcess)]
        return []

    def _process_user_data_dir(self, process: _VerifiedDesktopProcess) -> Path:
        if process.user_data_dir is not None:
            return process.user_data_dir
        parsed = _parse_user_data_dir(process.command_line)
        if parsed is not None:
            return parsed
        return self.claude_dir

    def _pids_for_root(self, root: Path) -> list[int]:
        return [
            process.pid
            for process in self._desktop_processes()
            if _same_user_data_root(self._process_user_data_dir(process), root)
        ]

    def _root_is_running(self, root: Path) -> bool:
        return bool(self._pids_for_root(root))

    def _history_folder_is_live(self, folder: Path) -> bool:
        for root, _account in self._managed_history_roots():
            try:
                resolved_root = root.resolve()
                resolved_folder = folder.resolve()
            except OSError:
                continue
            if resolved_folder == resolved_root or resolved_root in resolved_folder.parents:
                if self._root_is_running(root):
                    return True
        return False

    def _stop_desktop(self) -> bool:
        return self._stop_root(self.active_user_data_dir(), allow_original=False)

    def _stop_root(self, root: Path, *, allow_original: bool = False) -> bool:
        self._process_state()
        if not allow_original and _same_user_data_root(root, self.claude_dir):
            raise DesktopBackendError("The original Claude window is not stopped by KaliClaude")
        pids = self._pids_for_root(root)
        if not pids:
            return False
        try:
            self._process.request_close(pids)
            deadline = time.monotonic() + 6.0
            while time.monotonic() < deadline and self._pids_for_root(root):
                time.sleep(0.25)
            remaining = self._pids_for_root(root)
            if remaining:
                self._process.force_stop(remaining)
                deadline = time.monotonic() + 4.0
                while time.monotonic() < deadline and self._pids_for_root(root):
                    time.sleep(0.25)
            if self._pids_for_root(root):
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
        try:
            return self._root_is_running(self.active_user_data_dir())
        except Exception:
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
        self._process_state()
        proof_key = f"{executable.path}|{executable.version}"
        cached_proof = proof_key in proof_keys
        if self._root_is_running(root):
            return _LaunchVerification(
                proof_key=proof_key,
                ready=True,
                already_isolation_verified=True,
            )
        before = self._log_bytes(root)
        environment = dict(os.environ)
        environment["CLAUDE_USER_DATA_DIR"] = str(root)
        self._process.launch(executable, environment)
        deadline = time.monotonic() + self._launch_timeout
        while True:
            pids = self._pids_for_root(root)
            after = self._log_bytes(root)
            new_log = after[len(before):] if after.startswith(before) else after
            text = new_log.decode("utf-8", errors="ignore").lower()
            # A cached executable/version proof still requires evidence that this
            # launch wrote to the selected root, so an ignored env variable fails
            # closed. New executable versions must additionally establish a new
            # startup record before their proof is cached.
            if pids and new_log and ("startup" in text or "started" in text):
                return _LaunchVerification(
                    proof_key=proof_key,
                    ready=("account active" in text or "logged in" in text or "login successful" in text),
                    already_isolation_verified=cached_proof,
                )
            if time.monotonic() >= deadline:
                if not pids:
                    raise DesktopBackendError("Claude Desktop launch could not be verified")
                raise DesktopBackendError("Claude Desktop did not write a startup record in the selected root")
            time.sleep(self._launch_poll_interval)

    def _apply_launch_verification(self, meta: dict[str, Any], name: str, verification: _LaunchVerification) -> None:
        if not verification.already_isolation_verified:
            meta["launch_proofs"][verification.proof_key] = {"verified_at": time.time()}
        entry = meta["profiles"][name]
        if verification.ready:
            entry["state"] = ProfileState.READY.value
            entry["state_reason"] = ""

    def launch_active(self) -> LaunchResult:
        try:
            meta = self._load_meta()
            pending = meta.get("pending_login")
            if isinstance(pending, dict) and isinstance(pending.get("name"), str):
                name = pending["name"]
                root = self.desktop_data_dir / name
                if not root.is_dir():
                    return LaunchResult(False, name, root, "The pending login root is missing")
                executable = self._resolve_executable()
                try:
                    verification = self._launch_root(root, executable, set(meta["launch_proofs"]))
                except Exception as error:
                    return LaunchResult(False, name, root, str(error) or type(error).__name__)
                if not verification.already_isolation_verified:
                    meta["launch_proofs"][verification.proof_key] = {"verified_at": time.time()}
                    self._save_meta(meta)
                return LaunchResult(True, name, root)
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
            self._process_state()
            history = self.sync_histories()
            executable = self._resolve_executable()
            try:
                verification = self._launch_root(target_root, executable, set(meta["launch_proofs"]))
            except (ProcessDetectionError, UnknownLiveAccountError):
                raise
            except Exception as error:
                return SwitchResult(False, target_name, history=history, message=str(error) or type(error).__name__)
            self._apply_launch_verification(meta, target_name, verification)
            meta["desktop_active"] = target_name
            self._save_meta(meta)
            return SwitchResult(True, target_name, history=history, message="Profile selected", should_relaunch=False)
        finally:
            self._clear_decryption_cache()

    @staticmethod
    def _safe_history_segment(raw: str) -> str:
        if re.search(r"%[0-9a-fA-F]{2}", raw):
            raise SnapshotValidationError("History log contains an unsafe encoded path segment")
        decoded = raw.strip()
        if (
            not decoded
            or decoded in {".", ".."}
            or "/" in decoded
            or "\\" in decoded
            or ":" in decoded
            or "\x00" in decoded
            or Path(decoded).is_absolute()
            or PureWindowsPath(decoded).is_absolute()
        ):
            raise SnapshotValidationError("History log contains an unsafe path segment")
        return decoded

    @staticmethod
    def _contained_path(root: Path, *segments: str) -> Path:
        resolved_root = root.resolve()
        candidate = root.joinpath(*segments).resolve()
        if candidate != resolved_root and resolved_root not in candidate.parents:
            raise SnapshotValidationError("History path escapes its managed root")
        return candidate

    def _managed_history_roots(self) -> list[tuple[Path, str]]:
        meta = self._load_meta()
        managed: list[tuple[Path, str]] = []
        seen: set[tuple[str, str]] = set()

        def add(root: Path, account: str) -> None:
            try:
                resolved = root.resolve()
                safe_account = self._safe_history_segment(account)
            except (OSError, SnapshotValidationError):
                return
            key = (_windows_path_key(resolved), safe_account)
            if key in seen:
                return
            seen.add(key)
            managed.append((resolved, safe_account))

        for name, entry in meta.get("profiles", {}).items():
            if not isinstance(name, str) or not isinstance(entry, dict) or not self._valid_name(name):
                continue
            root = self._root_for_entry(name, entry)
            if not root.is_dir() or root.is_symlink():
                continue
            config = _load_json(root / "config.json")
            account = config.get("lastKnownAccountUuid")
            if not isinstance(account, str):
                continue
            add(root, account)
        if self.claude_dir.is_dir() and not self.claude_dir.is_symlink():
            config = _load_json(self.claude_dir / "config.json")
            account = config.get("lastKnownAccountUuid")
            if isinstance(account, str) and account.strip():
                add(self.claude_dir, account)
            for session_root in CC_SESSION_ROOTS:
                history_root = self.claude_dir / session_root
                if not history_root.is_dir() or history_root.is_symlink():
                    continue
                for workspace_path in history_root.iterdir():
                    if not workspace_path.is_dir() or workspace_path.is_symlink():
                        continue
                    for account_path in workspace_path.iterdir():
                        if account_path.is_dir() and not account_path.is_symlink():
                            add(self.claude_dir, account_path.name)
        return managed

    def _history_workspaces(self, root: Path, account: str) -> dict[str, set[str]]:
        workspaces = {root_name: set() for root_name in CC_SESSION_ROOTS}
        log = root / "Logs" / "main.log"
        try:
            lines = log.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            lines = []
        for line in lines:
            lower = line.lower()
            if "persisted sessions from" not in lower and "does not exist yet" not in lower:
                continue
            for root_name in CC_SESSION_ROOTS:
                position = lower.find(root_name.lower())
                if position < 0:
                    continue
                tail = line[position + len(root_name):].lstrip("/\\")
                segments = [segment for segment in re.split(r"[\\/]", tail) if segment]
                if len(segments) >= 2:
                    workspace = self._safe_history_segment(segments[0])
                    logged_account = self._safe_history_segment(segments[1])
                    if logged_account == account:
                        workspaces[root_name].add(workspace)
                break
        for root_name in CC_SESSION_ROOTS:
            history_root = self._contained_path(root, root_name)
            if not history_root.is_dir() or history_root.is_symlink():
                continue
            for workspace_path in history_root.iterdir():
                if not workspace_path.is_dir() or workspace_path.is_symlink():
                    continue
                workspace = self._safe_history_segment(workspace_path.name)
                account_path = self._contained_path(history_root, workspace, account)
                if account_path.is_dir() and not account_path.is_symlink():
                    workspaces[root_name].add(workspace)
        return workspaces

    def _managed_history_targets(self) -> dict[str, list[tuple[Path, str]]]:
        roots = self._managed_history_roots()
        discovered = {root_name: set() for root_name in CC_SESSION_ROOTS}
        for root, account in roots:
            for root_name, workspaces in self._history_workspaces(root, account).items():
                discovered[root_name].update(workspaces)
        targets = {root_name: [] for root_name in CC_SESSION_ROOTS}
        for root_name, workspaces in discovered.items():
            for root, account in roots:
                for workspace in sorted(workspaces):
                    folder = self._contained_path(root, root_name, workspace, account)
                    key = "/".join(
                        (
                            _sha256(str(root.resolve())),
                            root_name,
                            workspace,
                            _sha256(account),
                        )
                    )
                    targets[root_name].append((folder, key))
        return targets

    def _history_json_files(self, folder: Path) -> dict[str, Path]:
        if not folder.is_dir() or folder.is_symlink():
            return {}
        files: dict[str, Path] = {}
        for path in folder.iterdir():
            if path.is_symlink() or not path.is_file() or path.suffix.lower() != ".json":
                continue
            if path.suffix.lower() == JSONL_NAME or path.name.lower().endswith(".jsonl"):
                continue
            name = self._safe_history_segment(path.name)
            files[name] = path
        return files

    def _backup_managed_histories(self, targets: dict[str, list[tuple[Path, str]]]) -> None:
        destination = self.backups_dir / f"cc-predelete-{uuid.uuid4().hex}"
        destination.mkdir(parents=True, exist_ok=False)
        try:
            copied = 0
            for root_name, folders in targets.items():
                for folder, key in folders:
                    if folder.is_dir() and not folder.is_symlink():
                        shutil.copytree(folder, destination / root_name / _sha256(key))
                        copied += 1
            if not copied:
                shutil.rmtree(destination)
                return
        except Exception:
            shutil.rmtree(destination, ignore_errors=True)
            raise
        old = sorted(
            (path for path in self.backups_dir.glob("cc-predelete-*") if path.is_dir()),
            key=lambda path: (path.stat().st_mtime_ns, path.name),
        )
        for obsolete in old[:-MAX_HISTORY_BACKUPS]:
            shutil.rmtree(obsolete)

    def _sync_histories_local(self) -> dict[str, int]:
        targets = self._managed_history_targets()
        manifest = _load_json(self.history_manifest)
        report = {"added": 0, "removed": 0}
        deleted: set[str] = set()
        for folders in targets.values():
            for folder, key in folders:
                if not folder.is_dir():
                    continue
                current = set(self._history_json_files(folder))
                previous = manifest.get(key, [])
                if isinstance(previous, list):
                    deleted.update(
                        name for name in previous if isinstance(name, str) and name not in current
                    )
        if deleted:
            self._backup_managed_histories(targets)
            for folders in targets.values():
                for folder, _key in folders:
                    if self._history_folder_is_live(folder):
                        continue
                    for name in deleted:
                        safe_name = self._safe_history_segment(name)
                        path = self._contained_path(folder, safe_name)
                        if path.is_file() and not path.is_symlink():
                            path.unlink()
                            report["removed"] += 1

        new_manifest: dict[str, list[str]] = {}
        for root_name, folders in targets.items():
            union: dict[str, Path] = {}
            for folder, _key in folders:
                for name, path in self._history_json_files(folder).items():
                    if name in deleted:
                        continue
                    current = union.get(name)
                    if current is None or path.stat().st_mtime_ns > current.stat().st_mtime_ns:
                        union[name] = path
            for folder, key in folders:
                folder.mkdir(parents=True, exist_ok=True)
                for name, source in union.items():
                    destination = self._contained_path(folder, name)
                    if not destination.exists() or source.stat().st_mtime_ns > destination.stat().st_mtime_ns:
                        shutil.copy2(source, destination)
                        report["added"] += 1
                new_manifest[key] = sorted(union)
        _atomic_json(self.history_manifest, new_manifest)
        return report

    def sync_histories(self) -> SyncReport:
        try:
            value = self._history_sync() if self._history_sync is not None else self._sync_histories_local()
            if isinstance(value, SyncReport):
                return value
            if isinstance(value, dict):
                return SyncReport(True, int(value.get("added", 0)), int(value.get("removed", value.get("deleted", 0))))
            return SyncReport(True)
        except Exception as error:
            return SyncReport(False, message=str(error) or type(error).__name__)
        finally:
            self._clear_decryption_cache()

    @staticmethod
    def _is_interrupted_path(path: Path) -> bool:
        return any(part.endswith(".tmp") for part in path.parts)

    @classmethod
    def _operation_files(cls, root: Path) -> list[Path]:
        if root.is_file():
            return [] if cls._is_interrupted_path(Path(root.name)) else [root]
        if not root.is_dir():
            return []
        return [
            path
            for path in sorted(root.rglob("*"))
            if path.is_file() and not cls._is_interrupted_path(path.relative_to(root))
        ]

    @staticmethod
    def _cached_file_hash(path: Path, hashes: dict[Path, str]) -> str:
        resolved = path.resolve()
        if resolved not in hashes:
            hashes[resolved] = _sha256_file(path)
        return hashes[resolved]

    def _copy_verified(self, source: Path, destination: Path, hashes: dict[Path, str]) -> None:
        if source.is_file():
            if self._is_interrupted_path(Path(source.name)):
                return
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            if self._cached_file_hash(source, hashes) != self._cached_file_hash(destination, hashes):
                raise DesktopBackendError("Migration copy verification failed")
            return
        if not source.is_dir():
            return
        destination.mkdir(parents=True, exist_ok=True)
        for path in self._operation_files(source):
            relative = path.relative_to(source)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            if self._cached_file_hash(path, hashes) != self._cached_file_hash(target, hashes):
                raise DesktopBackendError("Migration copy verification failed")

    def _prune_operational_backups(self) -> None:
        if not self.backups_dir.is_dir():
            return
        managed = [
            path
            for path in self.backups_dir.glob("operational-*")
            if path.is_dir() and (path / ".kalikot-operational-backup").is_file()
        ]
        managed.sort(key=lambda path: (path.stat().st_mtime_ns, path.name))
        for obsolete in managed[:-MAX_OPERATIONAL_BACKUPS]:
            shutil.rmtree(obsolete)

    def _pre_migration_backup(self, hashes: dict[Path, str]) -> Optional[BackupRef]:
        destination = self.backups_dir / "operational-migration-schema2"
        if destination.is_dir() and (destination / ".kalikot-operational-backup").is_file():
            return None
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.backups_dir / f".operational-migration-{uuid.uuid4().hex}.tmp"
        temporary.mkdir(parents=True, exist_ok=False)
        artifacts: dict[str, dict[str, Any]] = {}
        try:
            if self.profiles_dir.is_dir():
                shutil.copytree(self.profiles_dir, temporary / "profiles")
                for source in self._operation_files(self.profiles_dir):
                    relative = Path("profiles") / source.relative_to(self.profiles_dir)
                    copied = temporary / relative
                    source_hash = self._cached_file_hash(source, hashes)
                    if source_hash != self._cached_file_hash(copied, hashes):
                        raise DesktopBackendError("Pre-migration profile backup verification failed")
                    artifacts[relative.as_posix()] = {
                        "size": source.stat().st_size,
                        "sha256": source_hash,
                    }
            if self.meta_file.is_file():
                shutil.copy2(self.meta_file, temporary / "meta.json")
                source_hash = self._cached_file_hash(self.meta_file, hashes)
                if source_hash != self._cached_file_hash(temporary / "meta.json", hashes):
                    raise DesktopBackendError("Pre-migration metadata backup verification failed")
                artifacts["meta.json"] = {
                    "size": self.meta_file.stat().st_size,
                    "sha256": source_hash,
                }
            _atomic_json(temporary / "backup-manifest.json", {"schema": 1, "artifacts": artifacts})
            (temporary / ".kalikot-operational-backup").write_text("migration-schema2", encoding="ascii")
            if destination.exists():
                raise DesktopBackendError("An unverified pre-migration backup already exists")
            os.replace(temporary, destination)
        except Exception:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
            raise
        self._prune_operational_backups()
        return BackupRef(destination)

    def _legacy_profile_names(self, meta: dict[str, Any]) -> list[str]:
        names = {
            name
            for name in meta.get("profiles", {})
            if isinstance(name, str) and self._valid_name(name)
        }
        if self.profiles_dir.is_dir():
            names.update(
                path.name
                for path in self.profiles_dir.iterdir()
                if path.is_dir() and not path.name.startswith(".") and self._valid_name(path.name)
            )
        return sorted(names)

    def _legacy_generation_paths(self, name: str) -> list[Path]:
        current = self.profiles_dir / name
        retained_root = self.profiles_dir / ".previous"
        retained = []
        if retained_root.is_dir():
            retained = sorted(
                (
                    path
                    for path in retained_root.iterdir()
                    if path.is_dir()
                    and re.fullmatch(re.escape(name) + r"-\d{14,20}", path.name)
                ),
                key=lambda path: path.name,
                reverse=True,
            )
        return ([current] if current.is_dir() else []) + retained

    @staticmethod
    def _valid_leveldb(path: Path) -> bool:
        if not path.is_dir() or not (path / "CURRENT").is_file():
            return False
        try:
            current = (path / "CURRENT").read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            return False
        manifest = path / current
        if not current.startswith("MANIFEST-") or not manifest.is_file() or not manifest.stat().st_size:
            return False
        return any(
            item.is_file() and item.stat().st_size and item.suffix.lower() in {".ldb", ".log"}
            for item in path.iterdir()
        )

    def _validate_legacy_stores(self, session: Path) -> str:
        if not self._valid_leveldb(session / "Session Storage"):
            return "Session Storage is incomplete"
        if not self._valid_leveldb(session / "Local Storage" / "leveldb"):
            return "Local Storage is incomplete"
        indexed = session / "IndexedDB"
        if not indexed.is_dir() or not any(
            self._valid_leveldb(path)
            for path in indexed.glob("*.indexeddb.leveldb")
            if path.is_dir()
        ):
            return "IndexedDB is incomplete"
        return ""

    def _validate_legacy_cookies(self, session: Path) -> str:
        """Validate saved cookies offline on a disposable read/write SQLite copy."""
        cookies = session / "Network" / "Cookies"
        if not cookies.is_file():
            return "Cookies database is missing"
        try:
            with tempfile.TemporaryDirectory(prefix="kaliclaude-cookie-audit-") as directory:
                copied_network = Path(directory) / "Network"
                copied_network.mkdir(parents=True)
                for filename in ("Cookies", "Cookies-journal", "Cookies-wal", "Cookies-shm"):
                    source = session / "Network" / filename
                    if source.is_file() and not source.name.endswith(".tmp"):
                        shutil.copy2(source, copied_network / filename)
                copied = copied_network / "Cookies"
                with closing(sqlite3.connect(copied)) as database:
                    if database.execute("PRAGMA quick_check").fetchone() != ("ok",):
                        return "Cookies database is corrupt"
                    columns = {
                        row[1] for row in database.execute("PRAGMA table_info(cookies)").fetchall()
                    }
                    if not {"host_key", "name", "encrypted_value"}.issubset(columns):
                        return "Cookies database schema is unsupported"
                    row = database.execute(
                        "SELECT encrypted_value FROM cookies "
                        "WHERE (host_key='claude.ai' OR host_key LIKE '%.claude.ai') "
                        "AND name='sessionKey' ORDER BY LENGTH(encrypted_value) DESC LIMIT 1"
                    ).fetchone()
                    database.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                encrypted = bytes(row[0]) if row and row[0] is not None else b""
                if not encrypted or all(32 <= value <= 126 for value in encrypted):
                    return "Cookies database has no encrypted session token"
                decoder = self._cookie_decoder or self._decrypt_cookie
                if not decoder(encrypted):
                    return "Cookies database has no locally decryptable session token"
        except (OSError, sqlite3.DatabaseError, TypeError, ValueError):
            return "Cookies database is corrupt or unreadable"
        return ""

    def _audit_legacy_generation(self, path: Path) -> Optional[_LegacyGeneration]:
        v1_path = path / "oauth-v1.blob"
        v2_path = path / "oauth-v2.blob"
        if not v2_path.is_file() and (path / "oauth.blob").is_file():
            v2_path = path / "oauth.blob"
        if not v1_path.is_file() or not v2_path.is_file():
            return None
        try:
            v1 = v1_path.read_bytes()
            v2 = v2_path.read_bytes()
            decoder = self._oauth_decoder or self._decrypt_oauth
            decoded_v1 = decoder(v1.decode("utf-8"))
            decoded_v2 = decoder(v2.decode("utf-8"))
        except (OSError, UnicodeError, ValueError):
            return None
        if not isinstance(decoded_v1, dict) or not decoded_v1:
            return None
        if not isinstance(decoded_v2, dict) or not decoded_v2:
            return None
        session = path / "session"
        if self._validate_legacy_cookies(session) or self._validate_legacy_stores(session):
            return None
        return _LegacyGeneration(path, v1, v2)

    def _select_legacy_generation(self, name: str) -> Optional[_LegacyGeneration]:
        for path in self._legacy_generation_paths(name):
            audited = self._audit_legacy_generation(path)
            if audited is not None:
                return audited
        return None

    @staticmethod
    def _backup_copy_roots(backup: Path) -> list[Path]:
        excluded = {
            "backup-manifest.json",
            "config.json",
            "claude_desktop_config.json",
            "Session Storage",
            "IndexedDB",
            "Local Storage",
            "Network",
        }
        return [
            path
            for path in sorted(backup.iterdir(), key=lambda item: item.name)
            if path.name not in excluded and not path.name.endswith(".tmp")
        ]

    def _manifest_file_matches(
        self,
        backup: Path,
        relative: Path,
        artifacts: dict[str, Any],
        hashes: dict[Path, str],
    ) -> bool:
        path = backup / relative
        expected = artifacts.get(relative.as_posix())
        return (
            path.is_file()
            and isinstance(expected, dict)
            and expected.get("size") == path.stat().st_size
            and expected.get("sha256") == self._cached_file_hash(path, hashes)
        )

    def _verified_recovery_backup(
        self,
        generation: _LegacyGeneration,
        hashes: dict[Path, str],
    ) -> Optional[_LegacyRecovery]:
        if not self.backups_dir.is_dir():
            return None
        for backup in sorted(self.backups_dir.glob("session-*"), key=lambda path: path.name, reverse=True):
            if not backup.is_dir():
                continue
            manifest = _load_json(backup / "backup-manifest.json")
            artifacts = manifest.get("artifacts")
            if manifest.get("schema") != 1 or not isinstance(artifacts, dict):
                continue
            if not self._manifest_file_matches(backup, Path("config.json"), artifacts, hashes):
                continue
            try:
                config_bytes = (backup / "config.json").read_bytes()
                config = json.loads(config_bytes.decode("utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if not isinstance(config, dict):
                continue
            v1 = config.get(OAUTH_KEY_V1)
            v2 = config.get(OAUTH_KEY_V2)
            account_uuid = config.get("lastKnownAccountUuid")
            if (
                not isinstance(v1, str)
                or not isinstance(v2, str)
                or v1.encode("utf-8") != generation.oauth_v1
                or v2.encode("utf-8") != generation.oauth_v2
                or not isinstance(account_uuid, str)
                or not account_uuid.strip()
            ):
                continue
            covered = True
            for copy_root in self._backup_copy_roots(backup):
                for source in self._operation_files(copy_root):
                    relative = source.relative_to(backup)
                    if not self._manifest_file_matches(backup, relative, artifacts, hashes):
                        covered = False
                        break
                if not covered:
                    break
            if covered:
                return _LegacyRecovery(backup, config, _sha256(account_uuid))
        return None

    def _legacy_organization_hashes(self, generation: _LegacyGeneration) -> list[str]:
        decoder = self._oauth_decoder or self._decrypt_oauth
        organizations: set[str] = set()
        for raw in (generation.oauth_v1, generation.oauth_v2):
            try:
                decoded = decoder(raw.decode("utf-8"))
            except (UnicodeError, ValueError):
                continue
            if not isinstance(decoded, dict):
                continue
            for key in decoded:
                parts = key.split(":") if isinstance(key, str) else []
                if len(parts) > 1 and parts[0].lower() in {"org", "organization"} and parts[1]:
                    organizations.add(parts[1])
        return sorted(_sha256(value) for value in organizations)

    def _seed_clean_root(self, stage: Path, hashes: dict[Path, str]) -> None:
        shared = self.claude_dir / "claude_desktop_config.json"
        if shared.is_file():
            self._copy_verified(shared, stage / shared.name, hashes)

    def _seed_recovered_root(
        self,
        stage: Path,
        generation: _LegacyGeneration,
        recovery: _LegacyRecovery,
        hashes: dict[Path, str],
    ) -> None:
        self._copy_verified(recovery.path / "config.json", stage / "config.json", hashes)
        session = generation.path / "session"
        for relative in LEGACY_SESSION_ITEMS:
            source = session / relative
            if source.exists():
                self._copy_verified(source, stage / relative, hashes)
        for source in self._backup_copy_roots(recovery.path):
            self._copy_verified(source, stage / source.name, hashes)
        local_state = stage / "Local State"
        if not local_state.is_file() and (self.claude_dir / "Local State").is_file():
            self._copy_verified(self.claude_dir / "Local State", local_state, hashes)
        self._seed_clean_root(stage, hashes)
        if not local_state.is_file():
            raise SnapshotValidationError("No compatible Local State is available")
        config = _load_json(stage / "config.json")
        if config != recovery.config:
            raise SnapshotValidationError("Recovered full config did not verify")
        previous_root = self._decryption_root
        self._decryption_root = stage
        self._clear_decryption_cache()
        try:
            decoder = self._oauth_decoder or self._decrypt_oauth
            decoded = [decoder(config[key]) for key in (OAUTH_KEY_V1, OAUTH_KEY_V2)]
            if any(not isinstance(value, dict) or not value for value in decoded):
                raise SnapshotValidationError("Recovered OAuth caches did not decrypt locally")
            if self._validate_legacy_cookies(stage) or self._validate_legacy_stores(stage):
                raise SnapshotValidationError("Recovered Desktop storage did not verify locally")
        except SnapshotValidationError:
            raise
        except Exception as error:
            raise SnapshotValidationError("Recovered Desktop storage did not decrypt locally") from error
        finally:
            self._clear_decryption_cache()
            self._decryption_root = previous_root

    def _promote_isolated_stage(self, name: str, stage: Path) -> Path:
        destination = self.desktop_data_dir / name
        quarantine = self.desktop_data_dir / ".quarantine"
        displaced: Optional[Path] = None
        if destination.exists():
            quarantine.mkdir(parents=True, exist_ok=True)
            displaced = quarantine / f"{name}-{uuid.uuid4().hex}"
            os.replace(destination, displaced)
        try:
            os.replace(stage, destination)
        except Exception:
            if displaced is not None and displaced.exists() and not destination.exists():
                os.replace(displaced, destination)
            raise
        return destination

    def _new_isolated_stage(self, name: str) -> Path:
        self.desktop_data_dir.mkdir(parents=True, exist_ok=True)
        stage = self.desktop_data_dir / f".{name}-migration-{uuid.uuid4().hex}.tmp"
        stage.mkdir(parents=True, exist_ok=False)
        return stage

    @staticmethod
    def _migration_marker(root: Path) -> Path:
        return root / ".kalikot-migration.json"

    def _write_migration_marker(
        self,
        root: Path,
        name: str,
        recovered: bool,
        recovery: Optional[_LegacyRecovery],
    ) -> None:
        _atomic_json(
            self._migration_marker(root),
            {
                "schema": MANIFEST_SCHEMA,
                "kind": "legacy-promotion",
                "name": name,
                "recovered": recovered,
                "account_id_sha256": recovery.account_id_hash if recovered and recovery else "",
            },
        )

    def _reusable_promoted_root(
        self,
        name: str,
        recovered: bool,
        recovery: Optional[_LegacyRecovery],
    ) -> bool:
        root = self.desktop_data_dir / name
        marker = _load_json(self._migration_marker(root))
        if (
            not root.is_dir()
            or root.is_symlink()
            or marker.get("schema") != MANIFEST_SCHEMA
            or marker.get("kind") != "legacy-promotion"
            or marker.get("name") != name
            or marker.get("recovered") is not recovered
        ):
            return False
        if not recovered:
            return True
        if recovery is None or marker.get("account_id_sha256") != recovery.account_id_hash:
            return False
        try:
            return self._read_account_hash(root) == recovery.account_id_hash
        except SnapshotValidationError:
            return False

    def audit_and_migrate(self) -> MigrationReport:
        """Losslessly recover schema-2 profiles into isolated schema-3 roots."""
        previous_decryption_root = self._decryption_root
        self._decryption_root = self.claude_dir
        try:
            raw_meta = _load_json(self.meta_file)
            report = MigrationReport()
            if raw_meta.get("schema") == MANIFEST_SCHEMA:
                profiles = raw_meta.get("profiles")
                if isinstance(profiles, dict):
                    report.unchanged.extend(sorted(name for name in profiles if isinstance(name, str)))
                return report

            if not isinstance(raw_meta.get("profiles"), dict):
                raw_meta["profiles"] = {}
            names = self._legacy_profile_names(raw_meta)
            hashes: dict[Path, str] = {}
            report.backup = self._pre_migration_backup(hashes)
            generations = {name: self._select_legacy_generation(name) for name in names}

            live_config = _load_json(self.claude_dir / "config.json")
            live_v1 = live_config.get(OAUTH_KEY_V1)
            live_v2 = live_config.get(OAUTH_KEY_V2)
            live_uuid = live_config.get("lastKnownAccountUuid")
            live_hash = _sha256(live_uuid) if isinstance(live_uuid, str) and live_uuid.strip() else ""
            default_matches: list[str] = []
            for name, generation in generations.items():
                legacy_entry = raw_meta["profiles"].get(name)
                saved_hash = legacy_entry.get("account_id_sha256") if isinstance(legacy_entry, dict) else ""
                blob_match = (
                    generation is not None
                    and isinstance(live_v1, str)
                    and isinstance(live_v2, str)
                    and live_v1.encode("utf-8") == generation.oauth_v1
                    and live_v2.encode("utf-8") == generation.oauth_v2
                )
                if live_hash and (blob_match or saved_hash == live_hash):
                    default_matches.append(name)
            default_name = default_matches[0] if len(default_matches) == 1 else None

            new_profiles: dict[str, dict[str, Any]] = {}
            for name in names:
                legacy_entry = raw_meta["profiles"].get(name)
                entry = legacy_entry if isinstance(legacy_entry, dict) else {}
                migrated_entry: dict[str, Any] = {
                    "label": str(entry.get("label") or name),
                    "note": str(entry.get("note") or ""),
                    "updated": float(entry.get("updated") or time.time()),
                }
                generation = generations[name]
                if name == default_name:
                    migrated_entry.update(
                        storage_mode=StorageMode.DEFAULT.value,
                        state=ProfileState.NEEDS_VALIDATION.value,
                        state_reason="Awaiting verified managed launch",
                        account_id_sha256=live_hash,
                        oauth_organization_sha256=(
                            self._legacy_organization_hashes(generation) if generation else []
                        ),
                    )
                    report.migrated.append(name)
                    new_profiles[name] = migrated_entry
                    continue

                recovery = (
                    self._verified_recovery_backup(generation, hashes)
                    if generation is not None
                    else None
                )
                recovered = generation is not None and recovery is not None
                if not self._reusable_promoted_root(name, recovered, recovery):
                    stage = self._new_isolated_stage(name)
                    recovered = False
                    try:
                        if generation is not None and recovery is not None:
                            try:
                                self._seed_recovered_root(stage, generation, recovery, hashes)
                                recovered = True
                            except SnapshotValidationError:
                                shutil.rmtree(stage)
                                stage = self._new_isolated_stage(name)
                                self._seed_clean_root(stage, hashes)
                        else:
                            self._seed_clean_root(stage, hashes)
                        self._write_migration_marker(stage, name, recovered, recovery)
                        self._promote_isolated_stage(name, stage)
                        self._fault_hook(f"migration:promoted:{name}")
                    except Exception:
                        if stage.exists():
                            quarantine = self.desktop_data_dir / ".quarantine"
                            quarantine.mkdir(parents=True, exist_ok=True)
                            os.replace(stage, quarantine / f"{name}-failed-{uuid.uuid4().hex}")
                        raise

                if recovered and recovery is not None and generation is not None:
                    migrated_entry.update(
                        storage_mode=StorageMode.ISOLATED.value,
                        state=ProfileState.NEEDS_VALIDATION.value,
                        state_reason="Offline recovery awaiting verified managed launch",
                        account_id_sha256=recovery.account_id_hash,
                        oauth_organization_sha256=self._legacy_organization_hashes(generation),
                    )
                    report.migrated.append(name)
                else:
                    reason = "No exact verified full-config recovery match; sign in again"
                    migrated_entry.update(
                        storage_mode=StorageMode.ISOLATED.value,
                        state=ProfileState.NEEDS_RELOGIN.value,
                        state_reason=reason,
                    )
                    report.needs_relogin.append(name)
                    report.issues[name] = reason
                new_profiles[name] = migrated_entry

            migrated_meta = dict(raw_meta)
            migrated_meta["profiles"] = new_profiles
            migrated_meta["desktop_active"] = default_name
            migrated_meta["launch_proofs"] = (
                raw_meta.get("launch_proofs") if isinstance(raw_meta.get("launch_proofs"), dict) else {}
            )
            self._save_meta(migrated_meta)
            return report
        finally:
            self._clear_decryption_cache()
            self._decryption_root = previous_decryption_root

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
        root = self._decryption_root or self.active_user_data_dir()
        state = _load_json(root / "Local State")
        encrypted = base64.b64decode(state["os_crypt"]["encrypted_key"])
        if not encrypted.startswith(b"DPAPI"):
            raise ValueError("Unexpected os_crypt key prefix")
        self._os_crypt_key_cache = self._dpapi_unprotect(encrypted[5:])
        return self._os_crypt_key_cache

    def _decrypt_chromium(self, encrypted: bytes) -> bytes:
        if encrypted.startswith((b"v10", b"v11")):
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM

            return AESGCM(self._os_crypt_key()).decrypt(encrypted[3:15], encrypted[15:], None)
        return self._dpapi_unprotect(encrypted)

    def _decrypt_oauth(self, blob: str) -> Optional[dict]:
        try:
            value = json.loads(self._decrypt_chromium(base64.b64decode(blob)))
        except Exception:
            return None
        return value if isinstance(value, dict) and value else None

    def _decrypt_cookie(self, encrypted: bytes) -> Optional[str]:
        try:
            plaintext = self._decrypt_chromium(encrypted)
            if len(plaintext) > 32 and any(value < 32 or value > 126 for value in plaintext[:32]):
                plaintext = plaintext[32:]
            value = plaintext.decode("utf-8")
        except Exception:
            return None
        return value or None

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
