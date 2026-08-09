"""Validated, local-only Claude Desktop profile snapshots.

This module owns the credential and session-store operations used by the GUI.
It never displays dialogs and never sends saved cookies or credentials over the
network.
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
import time
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional


OAUTH_KEY_V1 = "oauth:tokenCache"
OAUTH_KEY_V2 = "oauth:tokenCacheV2"
MANIFEST_SCHEMA = 2
SESSION_ITEMS = (
    ("Session Storage", "dir"),
    ("IndexedDB", "dir"),
    ("Local Storage", "dir"),
    ("Network/Cookies", "file"),
    ("Network/Cookies-journal", "optional_file"),
)
CC_SESSION_ROOTS = ("claude-code-sessions", "local-agent-mode-sessions")
MAX_HISTORY_BACKUPS = 5


class ProfileState(str, Enum):
    ACTIVE = "active"
    READY = "ready"
    NEEDS_RELOGIN = "needs_relogin"
    CORRUPT = "corrupt"
    UNKNOWN_LIVE_LOGIN = "unknown_live_login"


@dataclass(frozen=True)
class BackupRef:
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


class DesktopBackendError(RuntimeError):
    pass


class SnapshotValidationError(DesktopBackendError):
    pass


class ProcessDetectionError(DesktopBackendError):
    pass


class UnknownLiveAccountError(DesktopBackendError):
    pass


class RollbackError(DesktopBackendError):
    def __init__(self, message: str, recovery_path: Path):
        super().__init__(message)
        self.recovery_path = recovery_path


@dataclass(frozen=True)
class _SnapshotAudit:
    valid: bool
    issue: str = ""
    account_id_hash: str = ""
    oauth: dict[str, dict[str, Any]] = field(default_factory=dict)


class WindowsDesktopProcessAdapter:
    """Targets only the installed Claude Desktop executable, never Claude Code."""

    _NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

    def desktop_pids(self) -> list[int]:
        if os.name != "nt":
            raise OSError("Claude Desktop switching is Windows-only")
        command = (
            "@(Get-CimInstance Win32_Process -Filter \"Name='claude.exe'\" | "
            "Select-Object ProcessId,ExecutablePath) | ConvertTo-Json -Compress"
        )
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            timeout=12,
            creationflags=self._NO_WINDOW,
        )
        if result.returncode != 0:
            raise OSError("Unable to query Claude Desktop processes")
        try:
            rows = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as error:
            raise OSError("Claude Desktop process query returned invalid data") from error
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list):
            raise OSError("Claude Desktop process query returned invalid data")
        pids: list[int] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            executable = str(row.get("ExecutablePath") or "").replace("/", "\\").lower()
            if not executable:
                raise OSError("A claude.exe process could not be identified safely")
            if (
                executable.endswith("\\claude.exe")
                and "\\anthropicclaude\\app-" in executable
                and "\\claude-code\\" not in executable
            ):
                pids.append(int(row["ProcessId"]))
        return pids

    def request_close(self, pids: list[int]) -> None:
        for pid in pids:
            subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-Command",
                    f"(Get-Process -Id {int(pid)} -ErrorAction SilentlyContinue).CloseMainWindow() | Out-Null",
                ],
                capture_output=True,
                timeout=8,
                creationflags=self._NO_WINDOW,
            )

    def wait_stopped(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.desktop_pids():
                return True
            time.sleep(0.25)
        return not self.desktop_pids()

    def force_stop(self, pids: list[int]) -> None:
        for pid in pids:
            subprocess.run(
                ["taskkill.exe", "/F", "/PID", str(int(pid))],
                capture_output=True,
                timeout=8,
                creationflags=self._NO_WINDOW,
            )

    def launch(self) -> None:
        subprocess.Popen(
            ["explorer.exe", "shell:AppsFolder\\Claude_pzs8sxrjxfjjc!Claude"],
            creationflags=self._NO_WINDOW,
        )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    _atomic_write(
        path,
        json.dumps(value, indent=2, ensure_ascii=False).encode("utf-8"),
    )


class DesktopBackend:
    """Claude Desktop snapshot engine with injectable system boundaries."""

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
        self.profiles_dir = self.cache_dir / "profiles"
        self.meta_file = self.cache_dir / "meta.json"
        self.backups_dir = self.cache_dir / "backups"
        self.history_manifest = self.cache_dir / "cc-sync-manifest.json"
        self.claude_log = self.claude_dir / "logs" / "main.log"
        self.local_state = self.claude_dir / "Local State"
        self._oauth_decoder = oauth_decoder or self._decrypt_oauth
        self._cookie_decoder = cookie_decoder or self._decrypt_cookie
        self._claude_version = claude_version or self._detect_claude_version
        self._process = process_adapter or WindowsDesktopProcessAdapter()
        self._history_sync = history_sync
        self._fault_hook = fault_hook or (lambda _step: None)
        self._os_crypt_key_cache: Optional[bytes] = None

    def _load_meta(self) -> dict[str, Any]:
        meta = _load_json(self.meta_file)
        meta.setdefault("active", None)
        meta.setdefault("aumid", None)
        if not isinstance(meta.get("profiles"), dict):
            meta["profiles"] = {}
        return meta

    def _save_meta(self, meta: dict[str, Any]) -> None:
        _atomic_json(self.meta_file, meta)

    def _profile_names(self, meta: dict[str, Any]) -> list[str]:
        names = set(meta["profiles"])
        if self.profiles_dir.exists():
            names.update(
                p.name
                for p in self.profiles_dir.iterdir()
                if p.is_dir() and not p.name.startswith(".")
            )
            names.update(p.stem for p in self.profiles_dir.glob("*.blob"))
        return sorted(name for name in names if name)

    def _profile_dir(self, name: str) -> Path:
        return self.profiles_dir / name

    def _profile_oauth_paths(self, profile: Path) -> dict[str, Path]:
        legacy = profile / "oauth.blob"
        flat_legacy = self.profiles_dir / f"{profile.name}.blob"
        if not legacy.exists() and flat_legacy.is_file():
            legacy = flat_legacy
        return {
            "v1": profile / "oauth-v1.blob",
            "v2": profile / "oauth-v2.blob",
            "legacy": legacy,
        }

    @staticmethod
    def _oauth_identity(decoded: Any) -> Optional[str]:
        if not isinstance(decoded, dict):
            return None
        for cache_key, entry in decoded.items():
            if not isinstance(cache_key, str) or not isinstance(entry, dict):
                continue
            parts = cache_key.split(":")
            token = entry.get("token") or entry.get("accessToken")
            if len(parts) >= 2 and len(parts[1]) >= 8 and isinstance(token, str) and token:
                return parts[1]
        return None

    def _inspect_oauth(self, profile: Path) -> tuple[dict[str, dict[str, Any]], str]:
        paths = self._profile_oauth_paths(profile)
        if not paths["v2"].exists() and paths["legacy"].exists():
            paths["v2"] = paths["legacy"]
        metadata: dict[str, dict[str, Any]] = {}
        identities: list[str] = []
        for version in ("v1", "v2"):
            path = paths[version]
            info: dict[str, Any] = {"present": path.is_file(), "usable": False}
            if path.is_file():
                try:
                    raw = path.read_bytes()
                    blob = raw.decode("utf-8")
                    identity = self._oauth_identity(self._oauth_decoder(blob))
                    info.update(
                        size=len(raw),
                        sha256=_sha256_bytes(raw),
                        usable=bool(identity),
                    )
                    if identity:
                        identities.append(identity)
                except (OSError, UnicodeError):
                    pass
            metadata[version] = info
        if not identities:
            return metadata, ""
        if len(set(identities)) != 1:
            return metadata, "mismatched"
        return metadata, identities[0]

    @staticmethod
    def _valid_leveldb(path: Path) -> bool:
        if not path.is_dir() or not (path / "CURRENT").is_file():
            return False
        try:
            current = (path / "CURRENT").read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            return False
        manifest = path / current
        if (
            not current.startswith("MANIFEST-")
            or not manifest.is_file()
            or manifest.stat().st_size == 0
        ):
            return False
        return any(
            file.is_file()
            and file.stat().st_size > 0
            and file.suffix.lower() in {".ldb", ".log"}
            for file in path.iterdir()
        )

    def _validate_stores(self, session: Path) -> str:
        if not self._valid_leveldb(session / "Session Storage"):
            return "Session Storage is missing or incomplete"
        if not self._valid_leveldb(session / "Local Storage" / "leveldb"):
            return "Local Storage is missing or incomplete"
        indexed = session / "IndexedDB"
        if not indexed.is_dir() or not any(
            self._valid_leveldb(path)
            for path in indexed.glob("*.indexeddb.leveldb")
            if path.is_dir()
        ):
            return "IndexedDB is missing or incomplete"
        return ""

    def _validate_cookies(self, session: Path) -> str:
        cookies = session / "Network" / "Cookies"
        if not cookies.is_file():
            return "Cookies database is missing"
        try:
            uri = cookies.resolve().as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True)) as db:
                if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
                    return "Cookies database is corrupt"
                columns = {
                    row[1] for row in db.execute("PRAGMA table_info(cookies)").fetchall()
                }
                if not {"host_key", "name", "encrypted_value"}.issubset(columns):
                    return "Cookies database has an unsupported schema"
                row = db.execute(
                    "SELECT encrypted_value FROM cookies "
                    "WHERE (host_key = 'claude.ai' OR host_key LIKE '%.claude.ai') "
                    "AND name = 'sessionKey' ORDER BY LENGTH(encrypted_value) DESC LIMIT 1"
                ).fetchone()
            encrypted = bytes(row[0]) if row and row[0] is not None else b""
            if not encrypted or not self._cookie_decoder(encrypted):
                return "Cookies database has no locally decryptable sessionKey"
        except (OSError, sqlite3.DatabaseError, TypeError, ValueError):
            return "Cookies database is corrupt or unreadable"
        return ""

    @staticmethod
    def _artifact_map(profile: Path) -> dict[str, dict[str, Any]]:
        artifacts: dict[str, dict[str, Any]] = {}
        for path in sorted(profile.rglob("*")):
            if not path.is_file() or path.name == "manifest.json":
                continue
            relative = path.relative_to(profile).as_posix()
            artifacts[relative] = {
                "size": path.stat().st_size,
                "sha256": _sha256_file(path),
            }
        return artifacts

    def _validate_manifest(self, profile: Path, manifest: dict[str, Any]) -> str:
        if manifest.get("schema") != MANIFEST_SCHEMA:
            return "Snapshot manifest schema is unsupported"
        expected = manifest.get("artifacts")
        if not isinstance(expected, dict) or expected != self._artifact_map(profile):
            return "Snapshot manifest does not match the files on disk"
        if manifest.get("validation", {}).get("status") != "valid":
            return "Snapshot manifest is not marked valid"
        return ""

    def _audit_snapshot(self, profile: Path, *, require_manifest: bool) -> _SnapshotAudit:
        oauth, identity = self._inspect_oauth(profile)
        if identity == "mismatched":
            return _SnapshotAudit(
                False,
                "OAuth V1 and V2 caches contain different account identities",
                oauth=oauth,
            )
        if not identity:
            return _SnapshotAudit(
                False, "OAuth cache is empty, corrupt, or undecryptable", oauth=oauth
            )
        session = profile / "session"
        issue = self._validate_cookies(session) or self._validate_stores(session)
        if issue:
            return _SnapshotAudit(False, issue, oauth=oauth)
        identity_hash = _sha256_bytes(identity.encode("utf-8"))
        if require_manifest:
            manifest = _load_json(profile / "manifest.json")
            issue = self._validate_manifest(profile, manifest)
            if issue:
                return _SnapshotAudit(False, issue, identity_hash, oauth)
            if manifest.get("oauth") != oauth:
                return _SnapshotAudit(
                    False,
                    "Snapshot OAuth metadata does not match the cache files",
                    identity_hash,
                    oauth,
                )
            if manifest.get("account_id_sha256") != identity_hash:
                return _SnapshotAudit(
                    False, "Snapshot account identity does not match its manifest", identity_hash, oauth
                )
        return _SnapshotAudit(True, account_id_hash=identity_hash, oauth=oauth)

    def _manifest(
        self,
        profile: Path,
        audit: _SnapshotAudit,
        captured_at: Optional[str] = None,
    ) -> dict[str, Any]:
        return {
            "schema": MANIFEST_SCHEMA,
            "captured_at": captured_at or _utc_now(),
            "claude_desktop_version": self._claude_version() or "unknown",
            "account_id_sha256": audit.account_id_hash,
            "oauth": {"v1": audit.oauth["v1"], "v2": audit.oauth["v2"]},
            "artifacts": self._artifact_map(profile),
            "validation": {"status": "valid", "checked_at": _utc_now()},
        }

    def _migration_backup(self) -> BackupRef:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        destination = self.backups_dir / f"migration-{stamp}"
        destination.mkdir(parents=True, exist_ok=False)
        if self.profiles_dir.exists():
            shutil.copytree(self.profiles_dir, destination / "profiles")
        if self.meta_file.exists():
            shutil.copy2(self.meta_file, destination / "meta.json")
        if self.profiles_dir.exists() and self._path_digest(
            self.profiles_dir
        ) != self._path_digest(destination / "profiles"):
            raise DesktopBackendError("Migration profile backup verification failed")
        if self.meta_file.exists() and _sha256_file(self.meta_file) != _sha256_file(
            destination / "meta.json"
        ):
            raise DesktopBackendError("Migration metadata backup verification failed")
        return BackupRef(destination)

    def audit_and_migrate(self) -> MigrationReport:
        """Audit legacy profiles offline and idempotently add schema-2 manifests."""
        meta = self._load_meta()
        profiles = meta["profiles"]
        decisions: list[tuple[str, Path, _SnapshotAudit, str, str]] = []
        report = MigrationReport()

        for name in self._profile_names(meta):
            profile = self._profile_dir(name)
            entry = profiles.setdefault(name, {"label": name})
            manifest_exists = (profile / "manifest.json").is_file()
            audit = self._audit_snapshot(profile, require_manifest=manifest_exists)
            if manifest_exists and audit.valid:
                desired, reason = ProfileState.READY.value, ""
            elif manifest_exists:
                desired, reason = ProfileState.CORRUPT.value, audit.issue
            elif audit.valid:
                desired, reason = ProfileState.READY.value, ""
            else:
                desired, reason = ProfileState.NEEDS_RELOGIN.value, audit.issue

            needs_change = (
                (audit.valid and not manifest_exists)
                or entry.get("state") != desired
                or entry.get("state_reason", "") != reason
            )
            if needs_change:
                decisions.append((name, profile, audit, desired, reason))
            else:
                report.unchanged.append(name)

        changed = bool(decisions)
        if decisions:
            report.backup = self._migration_backup()
            for name, profile, audit, desired, reason in decisions:
                entry = profiles[name]
                if audit.valid and not (profile / "manifest.json").exists():
                    legacy = self._profile_oauth_paths(profile)["legacy"]
                    v2 = profile / "oauth-v2.blob"
                    if legacy.is_file() and not v2.exists():
                        _atomic_write(v2, legacy.read_bytes())
                        audit = self._audit_snapshot(profile, require_manifest=False)
                    updated = entry.get("updated")
                    captured_at = (
                        datetime.fromtimestamp(float(updated), timezone.utc).isoformat(
                            timespec="seconds"
                        )
                        if isinstance(updated, (int, float)) and updated > 0
                        else None
                    )
                    _atomic_json(
                        profile / "manifest.json",
                        self._manifest(profile, audit, captured_at),
                    )
                    report.migrated.append(name)
                elif desired == ProfileState.NEEDS_RELOGIN.value:
                    report.needs_relogin.append(name)
                    report.issues[name] = reason
                elif desired == ProfileState.CORRUPT.value:
                    report.corrupt.append(name)
                    report.issues[name] = reason
                entry["state"] = desired
                entry["state_reason"] = reason

        try:
            live_hash, _ = self._live_identity()
        except SnapshotValidationError:
            live_hash = ""
        matches = [
            name
            for name in self._profile_names(meta)
            if profiles.get(name, {}).get("state") == ProfileState.READY.value
            and _load_json(self._profile_dir(name) / "manifest.json").get(
                "account_id_sha256"
            )
            == live_hash
        ] if live_hash else []
        if len(matches) == 1 and meta.get("active") != matches[0]:
            if report.backup is None:
                report.backup = self._migration_backup()
            meta["active"] = matches[0]
            changed = True

        if changed:
            self._save_meta(meta)
        return report

    def list_profiles(self) -> list[DesktopProfile]:
        meta = self._load_meta()
        active = None
        try:
            live_hash, _ = self._live_identity()
        except SnapshotValidationError:
            live_hash = ""
        if live_hash:
            live_matches = [
                name
                for name in self._profile_names(meta)
                if meta["profiles"].get(name, {}).get("state")
                == ProfileState.READY.value
                and _load_json(self._profile_dir(name) / "manifest.json").get(
                    "account_id_sha256"
                )
                == live_hash
            ]
            active = live_matches[0] if len(live_matches) == 1 else None
        result: list[DesktopProfile] = []
        for name in self._profile_names(meta):
            entry = meta["profiles"].get(name, {})
            profile = self._profile_dir(name)
            manifest = _load_json(profile / "manifest.json")
            state_value = entry.get("state", ProfileState.NEEDS_RELOGIN.value)
            try:
                state = ProfileState(state_value)
            except ValueError:
                state = ProfileState.CORRUPT
            if name == active and state == ProfileState.READY:
                state = ProfileState.ACTIVE
            result.append(
                DesktopProfile(
                    name=name,
                    label=entry.get("label") or name,
                    note=entry.get("note") or "",
                    updated=float(entry.get("updated") or 0),
                    state=state,
                    account_id_hash=manifest.get("account_id_sha256", ""),
                    issue=entry.get("state_reason", ""),
                )
            )
        return result

    @staticmethod
    def _valid_name(name: str) -> bool:
        return bool(name) and len(name) <= 32 and all(
            character.isalnum() or character in "-_" for character in name
        )

    def _read_live_config(self) -> dict[str, Any]:
        path = self.claude_dir / "config.json"
        if not path.is_file():
            raise SnapshotValidationError("Claude Desktop config.json is missing")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise SnapshotValidationError(
                "Claude Desktop config.json is unreadable"
            ) from error
        if not isinstance(value, dict):
            raise SnapshotValidationError("Claude Desktop config.json is invalid")
        return value

    def _live_identity(self) -> tuple[str, dict[str, str]]:
        config = self._read_live_config()
        blobs: dict[str, str] = {}
        identities: list[str] = []
        for version, key in (("v1", OAUTH_KEY_V1), ("v2", OAUTH_KEY_V2)):
            blob = config.get(key)
            if not isinstance(blob, str) or not blob:
                continue
            blobs[version] = blob
            identity = self._oauth_identity(self._oauth_decoder(blob))
            if identity:
                identities.append(identity)
        if not identities or len(set(identities)) != 1:
            return "", blobs
        return _sha256_bytes(identities[0].encode("utf-8")), blobs

    def _profile_for_identity(self, identity_hash: str) -> str:
        matches = []
        for profile in self.list_profiles():
            if (
                profile.state in (ProfileState.ACTIVE, ProfileState.READY)
                and profile.account_id_hash == identity_hash
            ):
                matches.append(profile.name)
        if len(matches) != 1:
            return ""
        return matches[0]

    def _validated_profile(self, name: str) -> tuple[Path, _SnapshotAudit]:
        if not self._valid_name(name):
            raise SnapshotValidationError("Invalid profile name")
        profile = self._profile_dir(name)
        if not profile.is_dir():
            raise SnapshotValidationError(f"Profile '{name}' does not exist")
        audit = self._audit_snapshot(profile, require_manifest=True)
        if not audit.valid:
            raise SnapshotValidationError(
                f"Profile '{name}' is blocked: {audit.issue}"
            )
        return profile, audit

    def _stop_desktop(self) -> bool:
        try:
            pids = self._process.desktop_pids()
        except Exception as error:
            raise ProcessDetectionError(
                "Could not verify whether Claude Desktop is running"
            ) from error
        if not pids:
            return False
        try:
            self._process.request_close(pids)
            if not self._process.wait_stopped(6.0):
                remaining = self._process.desktop_pids()
                if remaining:
                    self._process.force_stop(remaining)
                if not self._process.wait_stopped(4.0):
                    raise DesktopBackendError("Claude Desktop did not stop")
            if self._process.desktop_pids():
                raise DesktopBackendError("Claude Desktop shutdown could not be proven")
        except DesktopBackendError:
            raise
        except Exception as error:
            raise ProcessDetectionError(
                "Claude Desktop shutdown could not be proven"
            ) from error
        return True

    def desktop_pids(self) -> list[int]:
        try:
            return self._process.desktop_pids()
        except Exception as error:
            raise ProcessDetectionError(
                "Could not verify whether Claude Desktop is running"
            ) from error

    def desktop_running(self) -> bool:
        return bool(self.desktop_pids())

    def stop_desktop(self) -> bool:
        return self._stop_desktop()

    @staticmethod
    def _remove_path(path: Path) -> None:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif path.exists() or path.is_symlink():
            path.unlink()

    @staticmethod
    def _copy_path(source: Path, destination: Path) -> None:
        if source.is_dir():
            shutil.copytree(source, destination)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

    def _stage_live_snapshot(self, name: str) -> tuple[Path, _SnapshotAudit]:
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        stage = self.profiles_dir / f".{name}.capture-{stamp}"
        session = stage / "session"
        try:
            stage.mkdir(parents=True, exist_ok=False)
            for relative, kind in SESSION_ITEMS:
                self._fault_hook(f"capture:{relative}")
                source = self.claude_dir / relative
                if source.exists():
                    self._copy_path(source, session / relative)
                elif kind != "optional_file":
                    raise SnapshotValidationError(
                        f"Live {relative} is missing; the current login was not saved"
                    )
            _, blobs = self._live_identity()
            for version, blob in blobs.items():
                _atomic_write(stage / f"oauth-{version}.blob", blob.encode("utf-8"))
            audit = self._audit_snapshot(stage, require_manifest=False)
            if not audit.valid:
                raise SnapshotValidationError(
                    f"The current Claude Desktop session is incomplete: {audit.issue}"
                )
            _atomic_json(stage / "manifest.json", self._manifest(stage, audit))
            final_audit = self._audit_snapshot(stage, require_manifest=True)
            if not final_audit.valid:
                raise SnapshotValidationError(final_audit.issue)
            return stage, final_audit
        except Exception:
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)
            raise

    def _promote_snapshot(self, name: str, stage: Path) -> Optional[Path]:
        current = self._profile_dir(name)
        previous_root = self.profiles_dir / ".previous"
        previous: Optional[Path] = None
        if current.exists():
            previous_root.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
            previous = previous_root / f"{name}-{stamp}"
            os.replace(current, previous)
        try:
            os.replace(stage, current)
        except Exception:
            if previous and previous.exists() and not current.exists():
                os.replace(previous, current)
            raise
        old_generations = sorted(previous_root.glob(f"{name}-*")) if previous_root.exists() else []
        for old in old_generations[:-2]:
            shutil.rmtree(old, ignore_errors=True)
        return previous

    def _restore_previous_generation(self, name: str, previous: Optional[Path]) -> None:
        current = self._profile_dir(name)
        if current.exists():
            shutil.rmtree(current)
        if previous and previous.exists():
            os.replace(previous, current)

    @staticmethod
    def _backup_artifacts(root: Path) -> dict[str, dict[str, Any]]:
        return {
            path.relative_to(root).as_posix(): {
                "size": path.stat().st_size,
                "sha256": _sha256_file(path),
            }
            for path in sorted(root.rglob("*"))
            if path.is_file() and path.name != "backup-manifest.json"
        }

    def _backup_live_state(self) -> BackupRef:
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        temporary = self.backups_dir / f".session-{stamp}.tmp"
        destination = self.backups_dir / f"session-{stamp}"
        presence: dict[str, bool] = {}
        try:
            temporary.mkdir(parents=True, exist_ok=False)
            config = self.claude_dir / "config.json"
            self._fault_hook("backup:config")
            if not config.is_file():
                raise SnapshotValidationError("Live config.json is missing")
            self._copy_path(config, temporary / "config.json")
            presence["config.json"] = True
            for relative, kind in SESSION_ITEMS:
                self._fault_hook(f"backup:{relative}")
                source = self.claude_dir / relative
                presence[relative] = source.exists()
                if source.exists():
                    self._copy_path(source, temporary / relative)
                elif kind != "optional_file":
                    raise SnapshotValidationError(
                        f"Cannot back up live {relative}; restoration was not started"
                    )
            manifest = {
                "schema": 1,
                "captured_at": _utc_now(),
                "presence": presence,
                "artifacts": self._backup_artifacts(temporary),
            }
            _atomic_json(temporary / "backup-manifest.json", manifest)
            if manifest["artifacts"] != self._backup_artifacts(temporary):
                raise DesktopBackendError("Live-state backup verification failed")
            os.replace(temporary, destination)
            final_manifest = _load_json(destination / "backup-manifest.json")
            if final_manifest.get("artifacts") != self._backup_artifacts(destination):
                raise DesktopBackendError("Live-state backup verification failed")
            return BackupRef(destination)
        except Exception:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
            raise

    @staticmethod
    def _retry_replace(source: Path, destination: Path) -> None:
        for attempt in range(5):
            try:
                os.replace(source, destination)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))

    def _replace_live_path(self, source: Optional[Path], destination: Path) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        displaced = destination.parent / f".{destination.name}.kaliclaude-{stamp}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        had_destination = destination.exists()
        if had_destination:
            self._retry_replace(destination, displaced)
        try:
            if source and source.exists():
                self._retry_replace(source, destination)
        except Exception:
            if had_destination and displaced.exists() and not destination.exists():
                self._retry_replace(displaced, destination)
            raise
        if displaced.exists():
            self._remove_path(displaced)

    @staticmethod
    def _path_digest(path: Path) -> dict[str, str]:
        if path.is_file():
            return {".": _sha256_file(path)}
        if path.is_dir():
            return {
                item.relative_to(path).as_posix(): _sha256_file(item)
                for item in sorted(path.rglob("*"))
                if item.is_file()
            }
        return {}

    def _stage_target_restore(self, profile: Path) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        stage = self.claude_dir.parent / f".KaliClaude-restore-{stamp}"
        stage.mkdir(parents=True, exist_ok=False)
        try:
            session = profile / "session"
            for relative, _kind in SESSION_ITEMS:
                source = session / relative
                if source.exists():
                    self._copy_path(source, stage / relative)
            config = self._read_live_config()
            for version, key in (("v1", OAUTH_KEY_V1), ("v2", OAUTH_KEY_V2)):
                oauth = profile / f"oauth-{version}.blob"
                if oauth.is_file():
                    config[key] = oauth.read_text(encoding="utf-8")
                else:
                    config.pop(key, None)
            _atomic_json(stage / "config.json", config)
            return stage
        except Exception:
            shutil.rmtree(stage, ignore_errors=True)
            raise

    def _restore_target(self, profile: Path) -> None:
        stage = self._stage_target_restore(profile)
        try:
            for relative, _kind in SESSION_ITEMS:
                self._fault_hook(f"restore:{relative}")
                source = stage / relative
                self._replace_live_path(source if source.exists() else None, self.claude_dir / relative)
            self._fault_hook("restore:config")
            self._replace_live_path(stage / "config.json", self.claude_dir / "config.json")
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    def _verify_target_live(self, profile: Path, identity_hash: str) -> None:
        live_hash, _ = self._live_identity()
        if live_hash != identity_hash:
            raise DesktopBackendError("Restored OAuth account identity did not verify")
        issue = self._validate_cookies(self.claude_dir) or self._validate_stores(
            self.claude_dir
        )
        if issue:
            raise DesktopBackendError(f"Restored Desktop session did not verify: {issue}")
        for relative, _kind in SESSION_ITEMS:
            if self._path_digest(profile / "session" / relative) != self._path_digest(
                self.claude_dir / relative
            ):
                raise DesktopBackendError(f"Restored {relative} did not match the snapshot")
        config = self._read_live_config()
        for version, key in (("v1", OAUTH_KEY_V1), ("v2", OAUTH_KEY_V2)):
            oauth = profile / f"oauth-{version}.blob"
            expected = oauth.read_text(encoding="utf-8") if oauth.is_file() else None
            if config.get(key) != expected:
                raise DesktopBackendError(f"Restored OAuth {version.upper()} cache did not match")

    def _rollback_live(self, backup: BackupRef) -> None:
        manifest = _load_json(backup.path / "backup-manifest.json")
        if manifest.get("artifacts") != self._backup_artifacts(backup.path):
            raise DesktopBackendError("Recovery backup no longer verifies")
        presence = manifest.get("presence", {})
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        stage = self.claude_dir.parent / f".KaliClaude-rollback-{stamp}"
        stage.mkdir(parents=True, exist_ok=False)
        try:
            for relative, _kind in SESSION_ITEMS:
                source = backup.path / relative
                if presence.get(relative) and source.exists():
                    self._copy_path(source, stage / relative)
            self._copy_path(backup.path / "config.json", stage / "config.json")
            for relative, _kind in SESSION_ITEMS:
                source = stage / relative
                self._replace_live_path(
                    source if source.exists() else None,
                    self.claude_dir / relative,
                )
            self._replace_live_path(
                stage / "config.json", self.claude_dir / "config.json"
            )
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        if _sha256_file(backup.path / "config.json") != _sha256_file(
            self.claude_dir / "config.json"
        ):
            raise DesktopBackendError("Rolled-back config.json did not verify")
        for relative, _kind in SESSION_ITEMS:
            expected = (
                self._path_digest(backup.path / relative)
                if presence.get(relative)
                else {}
            )
            if expected != self._path_digest(self.claude_dir / relative):
                raise DesktopBackendError(f"Rolled-back {relative} did not verify")

    def capture_current(self, name: str, label: str, note: str) -> DesktopProfile:
        if not self._valid_name(name):
            raise SnapshotValidationError(
                "Profile names may contain only letters, numbers, '-' and '_'"
            )
        self._stop_desktop()
        identity_hash, _ = self._live_identity()
        if not identity_hash:
            raise SnapshotValidationError(
                "No usable Claude Desktop login was found; sign in before saving"
            )
        known_name = self._profile_for_identity(identity_hash)
        if known_name and known_name != name:
            raise SnapshotValidationError(
                f"This Claude account is already saved as profile '{known_name}'"
            )
        existing = self._profile_dir(name)
        if (existing / "manifest.json").is_file():
            previous_audit = self._audit_snapshot(existing, require_manifest=True)
            if previous_audit.valid and previous_audit.account_id_hash != identity_hash:
                raise SnapshotValidationError(
                    f"The live account does not match saved profile '{name}'"
                )
        stage, audit = self._stage_live_snapshot(name)
        previous = self._promote_snapshot(name, stage)
        meta = self._load_meta()
        entry = meta["profiles"].setdefault(name, {})
        entry.update(
            label=label.strip() or name,
            note=note.strip(),
            updated=time.time(),
            fp=audit.account_id_hash[:12],
            state=ProfileState.READY.value,
            state_reason="",
        )
        meta["active"] = name
        try:
            self._fault_hook("metadata")
            self._save_meta(meta)
        except Exception:
            self._restore_previous_generation(name, previous)
            raise
        return next(profile for profile in self.list_profiles() if profile.name == name)

    def switch(self, target_name: str) -> SwitchResult:
        target, target_audit = self._validated_profile(target_name)
        was_running = self._stop_desktop()
        live_hash, _ = self._live_identity()
        outgoing_name = self._profile_for_identity(live_hash) if live_hash else ""
        if not outgoing_name:
            raise UnknownLiveAccountError(
                "The live Claude account is not a saved profile. Save it before switching."
            )
        meta = self._load_meta()
        if outgoing_name == target_name:
            if meta.get("active") != target_name:
                meta["active"] = target_name
                self._save_meta(meta)
            return SwitchResult(
                True,
                target_name,
                history=SyncReport(True),
                message="The live account already matches this profile",
                should_relaunch=was_running,
            )

        stage, _outgoing_audit = self._stage_live_snapshot(outgoing_name)
        self._promote_snapshot(outgoing_name, stage)
        backup = self._backup_live_state()
        try:
            target, target_audit = self._validated_profile(target_name)
            self._restore_target(target)
            self._verify_target_live(target, target_audit.account_id_hash)
            self._fault_hook("metadata")
            meta["active"] = target_name
            target_entry = meta["profiles"].setdefault(target_name, {})
            target_entry["state"] = ProfileState.READY.value
            target_entry["state_reason"] = ""
            self._save_meta(meta)
        except Exception as error:
            try:
                self._rollback_live(backup)
            except Exception as rollback_error:
                raise RollbackError(
                    "The switch failed and automatic rollback also failed",
                    backup.path,
                ) from rollback_error
            raise error

        history = self.sync_histories()
        return SwitchResult(
            True,
            target_name,
            backup=backup,
            history=history,
            message=(
                "Account switched; history sync failed"
                if not history.ok
                else "Account switched"
            ),
            should_relaunch=was_running,
        )

    def prepare_new_login(self) -> BackupRef:
        self._stop_desktop()
        live_hash, _ = self._live_identity()
        if live_hash:
            outgoing_name = self._profile_for_identity(live_hash)
            if not outgoing_name:
                raise UnknownLiveAccountError(
                    "The live Claude account is not saved. Save it before preparing a new login."
                )
            stage, _audit = self._stage_live_snapshot(outgoing_name)
            self._promote_snapshot(outgoing_name, stage)
        backup = self._backup_live_state()
        meta = self._load_meta()
        try:
            for relative, _kind in SESSION_ITEMS:
                self._fault_hook(f"clear:{relative}")
                self._replace_live_path(None, self.claude_dir / relative)
            config = self._read_live_config()
            config.pop(OAUTH_KEY_V1, None)
            config.pop(OAUTH_KEY_V2, None)
            stage = self.claude_dir.parent / ".KaliClaude-new-login-config.json"
            _atomic_json(stage, config)
            try:
                self._fault_hook("clear:config")
                self._replace_live_path(stage, self.claude_dir / "config.json")
            finally:
                if stage.exists():
                    stage.unlink()
            cleared = self._read_live_config()
            if OAUTH_KEY_V1 in cleared or OAUTH_KEY_V2 in cleared:
                raise DesktopBackendError("Claude OAuth caches were not cleared")
            if any((self.claude_dir / relative).exists() for relative, _ in SESSION_ITEMS):
                raise DesktopBackendError("Claude Desktop session stores were not cleared")
            self._fault_hook("metadata")
            meta["active"] = None
            self._save_meta(meta)
        except Exception as error:
            try:
                self._rollback_live(backup)
            except Exception as rollback_error:
                raise RollbackError(
                    "Preparing a new login failed and automatic rollback also failed",
                    backup.path,
                ) from rollback_error
            raise error
        return backup

    def _history_account_ids(self) -> set[str]:
        accounts: set[str] = set()
        meta = self._load_meta()
        for name in self._profile_names(meta):
            entry = meta["profiles"].get(name, {})
            if entry.get("state") != ProfileState.READY.value:
                continue
            paths = self._profile_oauth_paths(self._profile_dir(name))
            for version in ("v2", "v1"):
                path = paths[version]
                if not path.is_file():
                    continue
                try:
                    identity = self._oauth_identity(
                        self._oauth_decoder(path.read_text(encoding="utf-8"))
                    )
                except (OSError, UnicodeError):
                    identity = None
                if identity:
                    accounts.add(identity)
        try:
            config = self._read_live_config()
        except SnapshotValidationError:
            config = {}
        live_ids = {
            identity
            for key in (OAUTH_KEY_V2, OAUTH_KEY_V1)
            if isinstance(config.get(key), str)
            for identity in [
                self._oauth_identity(self._oauth_decoder(config[key]))
            ]
            if identity
        }
        if len(live_ids) == 1:
            accounts.update(live_ids)
        return accounts

    def _history_log_pairs(self) -> dict[str, set[tuple[str, str]]]:
        pairs: dict[str, set[tuple[str, str]]] = {
            root: set() for root in CC_SESSION_ROOTS
        }
        try:
            text = self.claude_log.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return pairs
        for line in text.splitlines():
            if "persisted sessions from" not in line and "does not exist yet" not in line:
                continue
            for root_name in CC_SESSION_ROOTS:
                if root_name not in line:
                    continue
                tail = line.split(root_name, 1)[1].replace("/", "\\")
                segments = [segment for segment in tail.split("\\") if segment]
                if len(segments) >= 2:
                    pairs[root_name].add((segments[0], segments[1]))
                break
        return pairs

    def _history_targets(
        self,
        accounts: set[str],
        log_pairs: dict[str, set[tuple[str, str]]],
    ) -> list[tuple[str, Path, Path, str]]:
        targets: list[tuple[str, Path, Path, str]] = []
        for root_name in CC_SESSION_ROOTS:
            root = self.claude_dir / root_name
            pairs = set(log_pairs.get(root_name, set()))
            if root.is_dir():
                for workspace in root.iterdir():
                    if workspace.is_dir():
                        pairs.update(
                            (workspace.name, account.name)
                            for account in workspace.iterdir()
                            if account.is_dir()
                        )
            for workspace, account in pairs:
                if account in accounts:
                    folder = root / workspace / account
                    targets.append(
                        (
                            root_name,
                            root,
                            folder,
                            f"{root_name}/{workspace}/{account}",
                        )
                    )
        return targets

    def _backup_histories(self) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        destination = self.backups_dir / f"cc-predelete-{stamp}"
        destination.mkdir(parents=True, exist_ok=False)
        try:
            for root_name in CC_SESSION_ROOTS:
                source = self.claude_dir / root_name
                if source.is_dir():
                    shutil.copytree(source, destination / root_name)
        except Exception:
            shutil.rmtree(destination, ignore_errors=True)
            raise
        for old in sorted(self.backups_dir.glob("cc-predelete-*"))[
            :-MAX_HISTORY_BACKUPS
        ]:
            shutil.rmtree(old, ignore_errors=True)

    def _sync_histories_local(self) -> dict[str, int]:
        accounts = self._history_account_ids()
        manifest = _load_json(self.history_manifest)
        targets = self._history_targets(accounts, self._history_log_pairs())
        report = {"added": 0, "deleted": 0}

        deleted: set[str] = set()
        for _root_name, _root, folder, key in targets:
            previous = set(manifest.get(key, []))
            current = (
                {path.name for path in folder.glob("*.json")}
                if folder.is_dir()
                else set()
            )
            deleted.update(previous - current)

        if deleted:
            self._backup_histories()
            for root_name in CC_SESSION_ROOTS:
                root = self.claude_dir / root_name
                for path in (root.glob("*/*/*.json") if root.is_dir() else ()):
                    if path.name in deleted:
                        path.unlink()
                        report["deleted"] += 1

        new_manifest: dict[str, list[str]] = {}
        for root_name in CC_SESSION_ROOTS:
            root = self.claude_dir / root_name
            if not root.is_dir():
                continue
            union: dict[str, Path] = {}
            for path in root.glob("*/*/*.json"):
                if path.name in deleted:
                    continue
                current = union.get(path.name)
                if current is None or path.stat().st_mtime > current.stat().st_mtime:
                    union[path.name] = path
            for target_root, _root, folder, key in targets:
                if target_root != root_name:
                    continue
                folder.mkdir(parents=True, exist_ok=True)
                for name, source in union.items():
                    destination = folder / name
                    if (
                        not destination.exists()
                        or source.stat().st_mtime > destination.stat().st_mtime
                    ):
                        shutil.copy2(source, destination)
                        report["added"] += 1
                new_manifest[key] = sorted(union)

        _atomic_json(self.history_manifest, new_manifest)
        return report

    def sync_histories(self) -> SyncReport:
        try:
            self._stop_desktop()
            value = (
                self._history_sync()
                if self._history_sync is not None
                else self._sync_histories_local()
            )
            if isinstance(value, SyncReport):
                return value
            return SyncReport(
                True,
                int(value.get("added", 0)),
                int(value.get("removed", value.get("deleted", 0))),
            )
        except Exception as error:
            return SyncReport(False, message=str(error) or type(error).__name__)

    @staticmethod
    def _dpapi_unprotect(data: bytes) -> bytes:
        class Blob(ctypes.Structure):
            _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_char))]

        source_buffer = ctypes.create_string_buffer(data, len(data))
        source = Blob(len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_char)))
        destination = Blob()
        if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(source), None, None, None, None, 0, ctypes.byref(destination)
        ):
            raise OSError("CryptUnprotectData failed")
        try:
            return ctypes.string_at(destination.data, destination.size)
        finally:
            ctypes.windll.kernel32.LocalFree(destination.data)

    def _os_crypt_key(self) -> bytes:
        if self._os_crypt_key_cache:
            return self._os_crypt_key_cache
        state = json.loads(self.local_state.read_text(encoding="utf-8"))
        encrypted = base64.b64decode(state["os_crypt"]["encrypted_key"])
        if not encrypted.startswith(b"DPAPI"):
            raise ValueError("Unexpected os_crypt key prefix")
        self._os_crypt_key_cache = self._dpapi_unprotect(encrypted[5:])
        return self._os_crypt_key_cache

    def _decrypt_chromium(self, encrypted: bytes) -> bytes:
        if encrypted.startswith((b"v10", b"v11")):
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM

            return AESGCM(self._os_crypt_key()).decrypt(
                encrypted[3:15], encrypted[15:], None
            )
        return self._dpapi_unprotect(encrypted)

    def _decrypt_oauth(self, blob: str) -> Optional[dict]:
        try:
            value = json.loads(self._decrypt_chromium(base64.b64decode(blob)))
        except Exception:
            return None
        return value if isinstance(value, dict) else None

    def _decrypt_cookie(self, encrypted: bytes) -> Optional[str]:
        try:
            plaintext = self._decrypt_chromium(encrypted)
            if len(plaintext) > 32 and any(
                value < 32 or value > 126 for value in plaintext[:32]
            ):
                plaintext = plaintext[32:]
            value = plaintext.decode("utf-8")
        except Exception:
            return None
        return value or None

    @staticmethod
    def _detect_claude_version() -> str:
        install = Path(os.environ.get("LOCALAPPDATA", "")) / "AnthropicClaude"
        versions = sorted(
            (path.name.removeprefix("app-") for path in install.glob("app-*") if path.is_dir()),
            reverse=True,
        )
        return versions[0] if versions else "unknown"


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


def capture_current(name: str, label: str, note: str) -> DesktopProfile:
    return _default().capture_current(name, label, note)


def switch(target_name: str) -> SwitchResult:
    return _default().switch(target_name)


def prepare_new_login() -> BackupRef:
    return _default().prepare_new_login()


def sync_histories() -> SyncReport:
    return _default().sync_histories()
