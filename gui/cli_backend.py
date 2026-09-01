"""KaliClaude — Claude Code CLI "partner" account switching.

A separate auth plane from the Desktop app: the CLI keeps a plaintext
``%USERPROFILE%\\.claude\\.credentials.json`` (Windows never uses Credential
Manager for it). This backend parks the outgoing profile's credentials and
installs the incoming profile's — as a partner to the Desktop switch — without
ever touching ``gui/desktop_backend.py`` or the Phase 1 transaction.

Core rule: **capture = verified move, install = copy.** A move never has a
moment with zero intact copies; the only ``unlink`` in this module is the
verified-move source. ``~\\.claude.json`` is read-only (harvest ``oauthAccount``
best-effort); we never write it. ``.credentials.json`` is opaque bytes, never
parsed. Every account check is warn-only — a CLI failure never rolls back the
Desktop switch.
"""
from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

# Pure helpers only — importing them is not a modification, so the "zero edits
# to desktop_backend.py" boundary holds. Fallback mirrors app.py:28-41 (the
# PyInstaller entry script runs from gui/ with no package parent).
try:
    from .desktop_backend import _atomic_json, _load_json, _sha256
except ImportError:  # pragma: no cover - exercised only in the frozen build
    from desktop_backend import _atomic_json, _load_json, _sha256  # type: ignore

CREDS_NAME = ".credentials.json"
ACCOUNT_NAME = "account.json"
CREATE_NEW_CONSOLE = 0x00000010

# Env vars the CLI honours ahead of the credentials file. If any is set, a file
# swap may not change the billed account — detect and warn only.
_ENV_TOKENS = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")


class CliBackendError(RuntimeError):
    pass


@dataclass(frozen=True)
class CliAccount:
    account_uuid: str = ""
    email: str = ""
    captured_at: str = ""


@dataclass(frozen=True)
class CliPairInfo:
    profile: str
    paired: bool
    email: str = ""
    account_uuid: str = ""


@dataclass(frozen=True)
class CliSwitchResult:
    ok: bool
    target: str
    needs_login: bool = False
    installed: bool = False
    captured_outgoing: str = ""
    warnings: tuple[str, ...] = ()
    message: str = ""


@dataclass(frozen=True)
class CliPairResult:
    ok: bool
    profile: str
    cancelled: bool = False
    timed_out: bool = False
    account: Optional[CliAccount] = None
    warnings: tuple[str, ...] = ()
    message: str = ""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stamp() -> str:
    # Microseconds keep parked-store names unique within one second.
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def _atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def _copy_file(source: Path, destination: Path) -> None:
    """Atomic same-dir write, then sha256-verify the destination bytes."""
    data = source.read_bytes()
    _atomic_bytes(destination, data)
    if _sha256(destination.read_bytes()) != _sha256(data):
        raise CliBackendError(f"Copy verification failed for {destination}")


def _move_file(source: Path, destination: Path) -> None:
    """Verified copy first (destination now intact), then drop the source."""
    _copy_file(source, destination)
    source.unlink()


def _read_registry_env() -> dict[str, str]:
    """HKCU + HKLM Environment values for the override tokens; failures swallowed."""
    import winreg  # local import: this module must import on non-Windows for tests

    found: dict[str, str] = {}
    for hive, subkey in (
        (winreg.HKEY_CURRENT_USER, r"Environment"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
    ):
        try:
            with winreg.OpenKey(hive, subkey) as key:
                for name in _ENV_TOKENS:
                    try:
                        value = winreg.QueryValueEx(key, name)[0]
                        if value:
                            found[name] = str(value)
                    except OSError:
                        pass
        except OSError:
            pass
    return found


def _default_spawner(argv: list[str]) -> Any:
    import subprocess

    return subprocess.Popen(argv, creationflags=CREATE_NEW_CONSOLE)


class CliBackend:
    """Claude Code CLI account engine with injectable system boundaries."""

    def __init__(
        self,
        *,
        home: Optional[Path] = None,
        cache_dir: Optional[Path] = None,
        spawner: Optional[Callable[[list[str]], Any]] = None,
        env_reader: Optional[Callable[[], dict[str, str]]] = None,
        which: Optional[Callable[[str], Optional[str]]] = None,
        pair_timeout: float = 600.0,
        pair_interval: float = 2.0,
    ) -> None:
        home = Path(home) if home else Path.home()
        self.home = home
        self.cache_dir = Path(cache_dir) if cache_dir else home / ".kalikot-claude-switcher"
        self.cli_data_dir = self.cache_dir / "cli-data"
        self.live_creds = home / ".claude" / CREDS_NAME
        self.claude_json = home / ".claude.json"
        self._spawner = spawner or _default_spawner
        self._env_reader = env_reader or _read_registry_env
        self._which = which or shutil.which
        self._pair_timeout = pair_timeout
        self._pair_interval = pair_interval

    # ----- stores -----------------------------------------------------------

    def _store_dir(self, name: str) -> Path:
        return self.cli_data_dir / name

    def _unclaimed_name(self, suffix: str = "") -> str:
        return f"_unclaimed-{_stamp()}" + (f"-{suffix}" if suffix else "")

    def _harvest_account(self) -> CliAccount:
        """Read-only harvest of oauthAccount from ~\\.claude.json (best-effort)."""
        oauth = _load_json(self.claude_json).get("oauthAccount")
        if not isinstance(oauth, dict):
            return CliAccount()
        return CliAccount(
            account_uuid=str(oauth.get("accountUuid") or ""),
            email=str(oauth.get("emailAddress") or ""),
            captured_at=_utcnow_iso(),
        )

    def _read_account(self, store: Path) -> CliAccount:
        data = _load_json(store / ACCOUNT_NAME)
        return CliAccount(
            account_uuid=str(data.get("accountUuid") or ""),
            email=str(data.get("emailAddress") or ""),
            captured_at=str(data.get("captured_at") or ""),
        )

    def _write_account(self, store: Path, account: CliAccount) -> None:
        _atomic_json(store / ACCOUNT_NAME, {
            "accountUuid": account.account_uuid,
            "emailAddress": account.email,
            "captured_at": account.captured_at or _utcnow_iso(),
        })

    # ----- queries ----------------------------------------------------------

    def pair_info(self, profile: str) -> CliPairInfo:
        store = self._store_dir(profile)
        paired = (store / CREDS_NAME).is_file()
        account = self._read_account(store) if paired else CliAccount()
        return CliPairInfo(profile=profile, paired=paired,
                           email=account.email, account_uuid=account.account_uuid)

    def env_overrides(self) -> list[str]:
        try:
            registry = self._env_reader() or {}
        except Exception:
            registry = {}
        active = [name for name in _ENV_TOKENS
                  if os.environ.get(name) or registry.get(name)]
        if not active:
            return []
        return ["Environment override(s) set (" + ", ".join(active) + ") — the CLI "
                "reads these before the credentials file, so a profile switch may "
                "not change the billed account."]

    def resolve_cli(self) -> Path:
        try:
            found = self._which("claude")
        except Exception:
            found = None
        if found:
            return Path(found)
        fallback = self.home / ".local" / "bin" / "claude.exe"
        if fallback.is_file():
            return fallback
        raise CliBackendError(
            "Could not find the Claude Code CLI ('claude' not on PATH and no "
            f"executable at {fallback}). Install it, then try again.")

    # ----- capture / install ------------------------------------------------

    def capture_live(self, profile: str) -> CliAccount:
        """Copy the live credentials into a profile store; the live file stays."""
        if not self.live_creds.is_file():
            raise CliBackendError("No live CLI credentials to capture.")
        store = self._store_dir(profile)
        _copy_file(self.live_creds, store / CREDS_NAME)
        account = self._harvest_account()
        self._write_account(store, account)
        return account

    def switch_to(self, target: str, outgoing: str) -> CliSwitchResult:
        """Park the live (outgoing) credentials, then install the target's.

        Capture is a verified move; install is a copy. An unpaired target leaves
        the CLI deliberately signed out with the old credentials recoverable in a
        store. Never rolls back — the Desktop switch always stands.
        """
        warnings = self.env_overrides()
        captured_outgoing = ""

        if self.live_creds.is_file():
            live = self._harvest_account()
            destination = outgoing
            if not outgoing:
                destination = self._unclaimed_name()
                warnings.append(
                    "The outgoing profile was unknown, so the current CLI "
                    f"credentials were parked in cli-data\\{destination}.")
            else:
                stored = self._read_account(self._store_dir(outgoing))
                if (live.account_uuid and stored.account_uuid
                        and live.account_uuid != stored.account_uuid):
                    destination = self._unclaimed_name(outgoing)
                    warnings.append(
                        f"The live CLI account did not match stored '{outgoing}', "
                        f"so it was parked in cli-data\\{destination} rather than "
                        "overwriting that store.")
            store = self._store_dir(destination)
            _move_file(self.live_creds, store / CREDS_NAME)
            self._write_account(store, live)
            captured_outgoing = destination

        target_creds = self._store_dir(target) / CREDS_NAME
        if target_creds.is_file():
            _copy_file(target_creds, self.live_creds)
            return CliSwitchResult(
                ok=True, target=target, installed=True,
                captured_outgoing=captured_outgoing, warnings=tuple(warnings),
                message=f"Installed the stored CLI login for '{target}'.")

        return CliSwitchResult(
            ok=True, target=target, needs_login=True,
            captured_outgoing=captured_outgoing, warnings=tuple(warnings),
            message=(f"No stored CLI login for '{target}'; the CLI is signed out "
                     "until you pair it. The previous login is recoverable."))

    # ----- pairing ----------------------------------------------------------

    def _verify(self, account: CliAccount, expected_account_sha256: str) -> list[str]:
        if not expected_account_sha256 or not account.account_uuid:
            return []
        if _sha256(account.account_uuid) != expected_account_sha256:
            return ["The paired CLI account may not match this profile's Desktop "
                    "account (account IDs differ). The login was kept — verify it "
                    "bills the intended account."]
        return []

    def pair(self, profile: str, expected_account_sha256: str = "") -> CliPairResult:
        """Adopt a live login if present; otherwise spawn a visible login terminal.

        Never automates login. Polls for the credentials file until it appears,
        the terminal closes (cancel), or the timeout elapses. Account match is
        soft-verified, warn-only.
        """
        warnings = self.env_overrides()

        if self.live_creds.is_file():
            account = self.capture_live(profile)
            return CliPairResult(
                ok=True, profile=profile, account=account,
                warnings=tuple(warnings + self._verify(account, expected_account_sha256)),
                message="Adopted the current CLI login for this profile.")

        executable = self.resolve_cli()
        process = self._spawner(["cmd.exe", "/k", str(executable)])
        deadline = time.monotonic() + self._pair_timeout
        while time.monotonic() < deadline:
            if self.live_creds.is_file():
                account = self.capture_live(profile)
                return CliPairResult(
                    ok=True, profile=profile, account=account,
                    warnings=tuple(warnings + self._verify(account, expected_account_sha256)),
                    message="Paired this profile with the new CLI login.")
            if process.poll() is not None:
                return CliPairResult(
                    ok=False, profile=profile, cancelled=True,
                    message="The login terminal closed before a login completed.")
            time.sleep(self._pair_interval)

        return CliPairResult(
            ok=False, profile=profile, timed_out=True,
            message="Timed out waiting for a CLI login.")

    # ----- store lifecycle (recoverable dir moves, same volume) -------------

    def rename_store(self, old: str, new: str) -> Optional[Path]:
        source = self._store_dir(old)
        if not source.is_dir():
            return None
        destination = self._store_dir(new)
        if destination.exists():
            self.retire_store(new)  # park the collision, recoverable
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.rename(destination)
        return destination

    def retire_store(self, name: str) -> Optional[Path]:
        source = self._store_dir(name)
        if not source.is_dir():
            return None
        destination = self._store_dir(self._unclaimed_name(name))
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.rename(destination)
        return destination


_default_cli_backend: Optional[CliBackend] = None


def _default() -> CliBackend:
    global _default_cli_backend
    if _default_cli_backend is None:
        _default_cli_backend = CliBackend()
    return _default_cli_backend


def pair_info(profile: str) -> CliPairInfo:
    return _default().pair_info(profile)


def env_overrides() -> list[str]:
    return _default().env_overrides()


def resolve_cli() -> Path:
    return _default().resolve_cli()


def capture_live(profile: str) -> CliAccount:
    return _default().capture_live(profile)


def switch_to(target: str, outgoing: str) -> CliSwitchResult:
    return _default().switch_to(target, outgoing)


def pair(profile: str, expected_account_sha256: str = "") -> CliPairResult:
    return _default().pair(profile, expected_account_sha256)


def rename_store(old: str, new: str) -> Optional[Path]:
    return _default().rename_store(old, new)


def retire_store(name: str) -> Optional[Path]:
    return _default().retire_store(name)
