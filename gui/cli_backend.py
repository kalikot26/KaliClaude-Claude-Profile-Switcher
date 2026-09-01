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
import re
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

# ----- CLI pool (v1: multiple simultaneously-logged-in CLI accounts) --------
# Each pool account is a private CLAUDE_CONFIG_DIR home; the CLI relocates its
# whole credential store there, so accounts run side-by-side with zero conflict.
POOL_DIRNAME = "pool"
POOL_ORDER_NAME = "pool.json"
CLAUDE_CONFIG_DIR = "CLAUDE_CONFIG_DIR"
RETIRED_PREFIX = "_retired-"
LAUNCHER_PS1_NAME = "claude-pool.ps1"
LAUNCHER_CMD_NAME = "claude-pool.cmd"
# First char must be alphanumeric, so a name can never start with '_' — the
# '_retired-*' namespace stays reserved for parked accounts.
_POOL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

# The launcher failover pattern, shared verbatim by the ps1 below. Kept as a
# named constant so a test can pin exactly what the CLI output is scanned for.
_LIMIT_REGEX = r"(?i)(usage|rate).{0,20}limit|limit (reached|hit)|out of (usage|quota)|\b429\b"

# Self-contained PowerShell launcher — no python dependency. @@POOLJSON@@ is
# replaced with the absolute pool.json path at install time.
_LAUNCHER_PS1 = r"""# claude-pool.ps1 — run claude across a pool of isolated CLI logins.
# For each account in pool.json order, point CLAUDE_CONFIG_DIR at its private
# home and run claude with all args passed through. If the output looks like a
# usage/rate limit, fail over to the next account; otherwise return as-is.
$ErrorActionPreference = 'Stop'
$PoolJson = '@@POOLJSON@@'
$LimitRe = '""" + _LIMIT_REGEX + r"""'

# Resolve claude.exe: same-dir first, else PATH.
$Claude = Join-Path $PSScriptRoot 'claude.exe'
if (-not (Test-Path -LiteralPath $Claude)) {
    $found = Get-Command 'claude.exe' -ErrorAction SilentlyContinue
    if ($found) { $Claude = $found.Source } else { $Claude = 'claude.exe' }
}

$accounts = @()
if (Test-Path -LiteralPath $PoolJson) {
    try {
        $data = Get-Content -Raw -LiteralPath $PoolJson | ConvertFrom-Json
        if ($data.order) { $accounts = @($data.order) }
    } catch { }
}
if ($accounts.Count -eq 0) {
    [Console]::Error.WriteLine("claude-pool: no pool accounts configured")
    exit 1
}

$poolRoot = Split-Path -Parent $PoolJson
$lastOutput = ''
$lastCode = 0
foreach ($name in $accounts) {
    $env:CLAUDE_CONFIG_DIR = Join-Path $poolRoot $name
    $lastOutput = (& $Claude @args 2>&1 | Out-String)
    $lastCode = $LASTEXITCODE
    if ($lastOutput -match $LimitRe) {
        [Console]::Error.WriteLine("claude-pool: '$name' limit hit, failing over")
        continue
    }
    [Console]::Error.WriteLine("claude-pool: served by '$name'")
    [Console]::Out.Write($lastOutput)
    exit $lastCode
}
[Console]::Error.WriteLine("claude-pool: all accounts exhausted")
[Console]::Out.Write($lastOutput)
exit $lastCode
"""

_LAUNCHER_CMD = '@powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0claude-pool.ps1" %*\r\n'

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


@dataclass(frozen=True)
class CliUnpairResult:
    ok: bool
    profile: str
    parked_store: str = ""
    parked_live: str = ""
    message: str = ""


@dataclass(frozen=True)
class PoolAccount:
    name: str
    logged_in: bool
    email: str = ""
    account_uuid: str = ""


@dataclass(frozen=True)
class PoolAddResult:
    ok: bool
    name: str
    cancelled: bool = False
    timed_out: bool = False
    email: str = ""
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


def _default_spawner(argv: list[str], env: dict[str, str]) -> Any:
    import subprocess

    return subprocess.Popen(argv, env=env, creationflags=CREATE_NEW_CONSOLE)


class CliBackend:
    """Claude Code CLI account engine with injectable system boundaries."""

    def __init__(
        self,
        *,
        home: Optional[Path] = None,
        cache_dir: Optional[Path] = None,
        spawner: Optional[Callable[[list[str], dict[str, str]], Any]] = None,
        env_reader: Optional[Callable[[], dict[str, str]]] = None,
        which: Optional[Callable[[str], Optional[str]]] = None,
        pair_timeout: float = 600.0,
        pair_interval: float = 2.0,
    ) -> None:
        home = Path(home) if home else Path.home()
        self.home = home
        self.cache_dir = Path(cache_dir) if cache_dir else home / ".kalikot-claude-switcher"
        self.cli_data_dir = self.cache_dir / "cli-data"
        self.pool_dir = self.cli_data_dir / POOL_DIRNAME
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

    def _harvest_account(self, config_dir: Optional[Path] = None) -> CliAccount:
        """Read-only harvest of oauthAccount (best-effort); never writes any file.

        A pool login runs under an isolated CLAUDE_CONFIG_DIR, where the current
        CLI writes the true oauthAccount into that dir's OWN ``.claude.json``
        while the shared ``~\\.claude.json`` stays stale. So for a pool slot
        (``config_dir`` given) prefer that dir's own ``.claude.json`` when it
        carries an identity, and fall back to the shared file. Default
        ``config_dir=None`` is shared-only — correct for the live-default
        ``~\\.claude`` plane (capture_live / switch_to / unpair).
        """
        def _oauth(path: Path) -> Optional[dict]:
            oauth = _load_json(path).get("oauthAccount")
            return oauth if isinstance(oauth, dict) else None

        oauth = None
        if config_dir is not None:
            local = _oauth(Path(config_dir) / ".claude.json")
            if local and (local.get("accountUuid") or local.get("emailAddress")):
                oauth = local
        if oauth is None:
            oauth = _oauth(self.claude_json)
        if oauth is None:
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
        # Live plane: harvest the shared ~\.claude.json (its login's writer here).
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
            # Live plane: shared ~\.claude.json is the source (no pool dir here).
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
        # Partner-model pairing runs against the live store, so the child just
        # inherits the current environment (no CLAUDE_CONFIG_DIR relocation).
        process = self._spawner(["cmd.exe", "/k", str(executable)], dict(os.environ))
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

    def unpair(self, profile: str, sign_out_live: bool = False) -> CliUnpairResult:
        """Detach a profile's paired CLI login so another account can pair.

        Parks the profile's store (recoverable dir move, None-safe when
        unpaired) and, when ``sign_out_live``, verified-MOVEs the live
        credentials into a fresh unclaimed store — the CLI is signed out but
        the login stays recoverable. Never deletes; the only unlink is the
        verified-move source.
        """
        parked_store = self.retire_store(profile)  # None when nothing is paired

        parked_live = ""
        if sign_out_live and self.live_creds.is_file():
            parked_live = self._unclaimed_name("live")
            store = self._store_dir(parked_live)
            _move_file(self.live_creds, store / CREDS_NAME)
            # Live plane: shared ~\.claude.json is the source (best-effort).
            self._write_account(store, self._harvest_account())

        if parked_store is None and not parked_live:
            return CliUnpairResult(
                ok=True, profile=profile,
                message=f"No paired CLI login for '{profile}'; nothing needed parking.")

        parked_store_name = parked_store.name if parked_store else ""
        parts = []
        if parked_store_name:
            parts.append(f"parked its stored login in cli-data\\{parked_store_name}")
        if parked_live:
            parts.append(
                f"signed out the live CLI (recoverable in cli-data\\{parked_live})")
        return CliUnpairResult(
            ok=True, profile=profile,
            parked_store=parked_store_name, parked_live=parked_live,
            message=f"Unpaired '{profile}' — " + "; ".join(parts) + ".")

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

    # ----- CLI pool ---------------------------------------------------------
    # Each account is its own CLAUDE_CONFIG_DIR home under cli-data\pool\<name>;
    # pool.json records the run order. Identity is harvested read-only from the
    # pool account's OWN config-dir .claude.json (the true writer of a login run
    # under an isolated CLAUDE_CONFIG_DIR), falling back to the shared
    # ~\.claude.json; neither file is ever written.

    def _pool_order(self) -> list[str]:
        order = _load_json(self.pool_dir / POOL_ORDER_NAME).get("order")
        if not isinstance(order, list):
            return []
        return [str(name) for name in order if isinstance(name, str)]

    def _write_pool_order(self, order: list[str]) -> None:
        _atomic_json(self.pool_dir / POOL_ORDER_NAME, {"order": order})

    def _validate_pool_name(self, name: str) -> None:
        if not isinstance(name, str) or not _POOL_NAME_RE.match(name):
            raise CliBackendError(
                f"Invalid pool account name '{name}'. Use 1–64 characters: "
                "letters, numbers, '-' or '_', and it may not start with '_'.")

    def _pool_account(self, name: str) -> PoolAccount:
        account_dir = self.pool_dir / name
        account = self._read_account(account_dir)
        return PoolAccount(
            name=name,
            logged_in=(account_dir / CREDS_NAME).is_file(),
            email=account.email,
            account_uuid=account.account_uuid,
        )

    def pool_list(self) -> list[PoolAccount]:
        """Accounts in pool.json order, with on-disk strays appended.

        Excludes the reserved ``_retired-*`` namespace from both.
        """
        seen: set[str] = set()
        names: list[str] = []
        for name in self._pool_order():
            if name in seen or name.startswith(RETIRED_PREFIX):
                continue
            seen.add(name)
            names.append(name)
        if self.pool_dir.is_dir():
            for entry in sorted(self.pool_dir.iterdir(), key=lambda p: p.name):
                if (entry.is_dir() and entry.name not in seen
                        and not entry.name.startswith(RETIRED_PREFIX)):
                    seen.add(entry.name)
                    names.append(entry.name)
        return [self._pool_account(name) for name in names]

    def pool_add(self, name: str) -> PoolAddResult:
        """Spawn a visible, isolated login terminal and adopt the new account.

        The human does the OAuth login — this never automates it. The child is
        given a private ``CLAUDE_CONFIG_DIR`` so the login writes only there.
        Polls for that store's credentials file until it appears (adopt), the
        terminal closes (cancel), or the timeout elapses.
        """
        self._validate_pool_name(name)
        if name in self._pool_order():
            raise CliBackendError(f"A pool account named '{name}' already exists.")

        account_dir = self.pool_dir / name
        account_dir.mkdir(parents=True, exist_ok=True)
        creds = account_dir / CREDS_NAME
        executable = self.resolve_cli()

        env = dict(os.environ)
        env[CLAUDE_CONFIG_DIR] = str(account_dir)
        process = self._spawner(["cmd.exe", "/k", str(executable)], env)

        deadline = time.monotonic() + self._pair_timeout
        while time.monotonic() < deadline:
            if creds.is_file():
                # Prefer the pool dir's own .claude.json (a CLAUDE_CONFIG_DIR
                # login's true writer); fall back to the shared ~\.claude.json.
                account = self._harvest_account(account_dir)
                self._write_account(account_dir, account)
                order = self._pool_order()
                if name not in order:
                    order.append(name)
                    self._write_pool_order(order)
                return PoolAddResult(
                    ok=True, name=name, email=account.email,
                    message=f"Added pool account '{name}'.")
            if process.poll() is not None:
                # ponytail: the empty pool\<name> dir is left behind (never
                # deleted). It never entered the order, so re-adding the same
                # name just reuses the dir and completes; Remove parks it.
                return PoolAddResult(
                    ok=False, name=name, cancelled=True,
                    message="The login terminal closed before a login completed.")
            time.sleep(self._pair_interval)

        return PoolAddResult(
            ok=False, name=name, timed_out=True,
            message="Timed out waiting for a CLI login.")

    def pool_retire(self, name: str) -> Optional[Path]:
        """Park ``pool\\<name>`` as ``pool\\_retired-<stamp>-<name>`` and drop it
        from the order. Recoverable dir move, never a delete; None-safe."""
        order = self._pool_order()
        if name in order:
            self._write_pool_order([other for other in order if other != name])
        source = self.pool_dir / name
        if not source.is_dir():
            return None
        destination = self.pool_dir / f"{RETIRED_PREFIX}{_stamp()}-{name}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.rename(destination)
        return destination

    def pool_move(self, name: str, delta: int) -> list[str]:
        """Reorder ``name`` by ``delta`` within pool.json, clamped at the ends."""
        order = self._pool_order()
        if name not in order:
            return order
        index = order.index(name)
        target = max(0, min(len(order) - 1, index + delta))
        if target != index:
            order.insert(target, order.pop(index))
            self._write_pool_order(order)
        return order

    def pool_install_launcher(self) -> Path:
        """Write the two self-contained launcher files into ~\\.local\\bin.

        Returns the ps1 path. Overwrites any existing launcher.
        """
        bin_dir = self.home / ".local" / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        ps1 = bin_dir / LAUNCHER_PS1_NAME
        pool_json = str(self.pool_dir / POOL_ORDER_NAME)
        ps1.write_text(_LAUNCHER_PS1.replace("@@POOLJSON@@", pool_json), encoding="utf-8")
        (bin_dir / LAUNCHER_CMD_NAME).write_text(_LAUNCHER_CMD, encoding="utf-8")
        return ps1


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


def unpair(profile: str, sign_out_live: bool = False) -> CliUnpairResult:
    return _default().unpair(profile, sign_out_live)


def rename_store(old: str, new: str) -> Optional[Path]:
    return _default().rename_store(old, new)


def retire_store(name: str) -> Optional[Path]:
    return _default().retire_store(name)


def pool_list() -> list[PoolAccount]:
    return _default().pool_list()


def pool_add(name: str) -> PoolAddResult:
    return _default().pool_add(name)


def pool_retire(name: str) -> Optional[Path]:
    return _default().pool_retire(name)


def pool_move(name: str, delta: int) -> list[str]:
    return _default().pool_move(name, delta)


def pool_install_launcher() -> Path:
    return _default().pool_install_launcher()
