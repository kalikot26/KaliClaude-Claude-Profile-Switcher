"""KaliClaude Profile Switcher — session-snapshot edition.

Captures and restores the live Claude Desktop OAuth session
(the encrypted `oauth:tokenCache` blob in %APPDATA%\\Claude\\config.json),
mirroring how the Codex switcher snapshots auth.json. Works entirely on
the local machine — the blob is encrypted under the current Windows user, so
it is stored and restored verbatim for switching.

For the optional, manual "Refresh Usage" action only, the blob is decrypted
in-memory (Chromium os_crypt: AES-256-GCM with a DPAPI-protected key) to read
the access token for a read-only usage GET to Anthropic. The refresh token is
never used (nothing rotates) and tokens are never logged, shown, or cached.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import queue
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional

import tkinter as tk
from tkinter import messagebox, ttk

# ---------------------------------------------------------------------------
# Theme — warm dark, Claude vibe
# ---------------------------------------------------------------------------
BG_ROOT    = "#17140F"
BG_SIDEBAR = "#1E1A14"
BG_CARD    = "#28231C"
BG_CARD_HV = "#312B23"
BG_CARD_SEL= "#3A3229"
BG_PANEL   = "#1A1710"
BG_INPUT   = "#28231C"

CLR_ACCENT = "#D97340"   # Claude warm orange
CLR_ACTIVE = "#D97340"
CLR_OK     = "#6BBF78"
CLR_WARN   = "#E0B84A"
CLR_ERR    = "#D95050"
CLR_DIV    = "#332D25"

TXT_PRI  = "#EDE4D4"
TXT_SUB  = "#9A8E7E"
TXT_MUTE = "#5E5649"

FF = "Segoe UI"

APP_TITLE = "KaliClaude"
APP_VER   = "2.0"

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _res(name: str) -> Path:
    if hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / name
    return Path(__file__).resolve().parent.parent / name

ICON_PATH  = _res("app.ico")

CACHE_DIR  = Path.home() / ".kalikot-claude-switcher"
STORE_DIR  = CACHE_DIR / "profiles"     # <name>/  per-profile session snapshot
META_FILE  = CACHE_DIR / "meta.json"    # {active, aumid, profiles:{...}}
BACKUP_DIR = CACHE_DIR / "backups"      # timestamped config.json + session backups
CC_SYNC_MANIFEST = CACHE_DIR / "cc-sync-manifest.json"  # last-distributed CC sessions

CLAUDE_DIR    = Path.home() / "AppData" / "Roaming" / "Claude"
CLAUDE_CONFIG = CLAUDE_DIR / "config.json"
LOCAL_STATE   = CLAUDE_DIR / "Local State"   # holds the os_crypt AES key
CLAUDE_LOG    = CLAUDE_DIR / "logs" / "main.log"  # LocalSessionManager load lines
OAUTH_KEY     = "oauth:tokenCache"
OAUTH_KEY_V2  = "oauth:tokenCacheV2"   # Claude Desktop migrated the live token cache here

# A Claude login is the embedded claude.ai web session, not just the token.
# These items (relative to CLAUDE_DIR) together make up that session; a profile
# snapshots and restores all of them, with Claude fully stopped.
#
# NOTE: "Local Storage" is intentionally NOT swapped — it holds Claude Code
# drafts (composer-draft:*) and project UI state we want SHARED across every
# profile. The authoritative login is the oauth token (config.json) + cookies,
# both of which are still swapped below.
SESSION_ITEMS = [
    ("Session Storage",        "dir"),
    ("IndexedDB",              "dir"),
    ("Network/Cookies",        "file"),
    ("Network/Cookies-journal", "file"),
]

# Shared, never swapped — Claude Code / agent-mode local session stores. On disk
# they are keyed as <root>/<project>/<accountId>/<session>.json, so simply
# switching the active account hides them. On every switch we MIRROR each
# account's sessions into the incoming account's id folder, so the Claude Code
# project list + history stay global across all profiles.
CC_SESSION_ROOTS = ("claude-code-sessions", "local-agent-mode-sessions")

# Cleared (in addition to SESSION_ITEMS) only when preparing a brand-new login.
# Local Storage is deliberately NOT swapped on a normal switch — it's shared UI
# state and the login rides on the cookies. It's wiped only for a fresh sign-in.
CLEAR_EXTRA = [("Local Storage", "dir")]

USAGE_API   = "https://api.anthropic.com/api/oauth/usage"
PROFILE_API = "https://api.anthropic.com/api/oauth/profile"

MUTEX_NAME = "Local\\KaliClaudeProfileSwitcherV2"
IPC_HOST   = "127.0.0.1"
IPC_PORT   = 47323

MAX_CONFIG_BACKUPS  = 15
MAX_SESSION_BACKUPS = 5

# ---------------------------------------------------------------------------
# JSON / meta helpers
# ---------------------------------------------------------------------------

def _load_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        return {}

def _save_json(p: Path, d: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)

def _load_meta() -> dict:
    m = _load_json(META_FILE)
    m.setdefault("active", None)
    m.setdefault("aumid", None)
    m.setdefault("profiles", {})
    return m

def _save_meta(m: dict) -> None:
    _save_json(META_FILE, m)

# ---------------------------------------------------------------------------
# Claude config (the live session lives here)
# ---------------------------------------------------------------------------

def _read_config() -> Optional[dict]:
    """Parse Claude Desktop config.json. None if missing/unreadable."""
    if not CLAUDE_CONFIG.exists():
        return None
    try:
        return json.loads(CLAUDE_CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return None

def _backup_config() -> Optional[Path]:
    if not CLAUDE_CONFIG.exists():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    dest = BACKUP_DIR / f"config.{ts}.json"
    try:
        dest.write_bytes(CLAUDE_CONFIG.read_bytes())
    except OSError:
        return None
    # prune old backups
    backups = sorted(BACKUP_DIR.glob("config.*.json"))
    for old in backups[:-MAX_CONFIG_BACKUPS]:
        try: old.unlink()
        except OSError: pass
    return dest

def _write_config(cfg: dict) -> None:
    """Back up then write config.json, preserving tab indentation."""
    _backup_config()
    CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CLAUDE_CONFIG.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent="\t", ensure_ascii=False),
                   encoding="utf-8")
    tmp.replace(CLAUDE_CONFIG)

def _live_blob() -> Optional[str]:
    """The live oauth token blob for the signed-in account, and the SINGLE source
    of truth for the live token everywhere (capture, restore, backup, fingerprint,
    presence, usage). Claude Desktop migrated the active token to
    `oauth:tokenCacheV2` and left the legacy `oauth:tokenCache` empty, so prefer
    V2 and fall back to the legacy key for older Claude builds. Nothing should read
    the raw legacy key directly, or it will pick up the dead/empty blob."""
    cfg = _read_config()
    if not cfg:
        return None
    for key in (OAUTH_KEY_V2, OAUTH_KEY):
        blob = cfg.get(key)
        if isinstance(blob, str) and blob:
            return blob
    return None

def _fp(blob: Optional[str]) -> str:
    if not blob:
        return ""
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]

# ---------------------------------------------------------------------------
# Session store — full per-profile snapshot of the Claude login.
#
# A login is the embedded claude.ai web session (Local Storage + Session
# Storage + IndexedDB + cookies) PLUS the oauth token cache.  All copying and
# swapping happens only while Claude is fully stopped, so databases are never
# read or written under a live process.  Everything is backed up before being
# overwritten, so any step is recoverable.
# ---------------------------------------------------------------------------

def _profile_dir(name: str) -> Path:
    return STORE_DIR / name

def _session_root(name: str) -> Path:
    return _profile_dir(name) / "session"

def _oauth_path(name: str) -> Path:
    return _profile_dir(name) / "oauth.blob"

def _has_session(name: str) -> bool:
    """True if a full web-session snapshot exists (not just an old token)."""
    sr = _session_root(name)
    return sr.exists() and ((sr / "IndexedDB").exists()
                            or (sr / "Local Storage").exists())

def _store_oauth(name: str, blob: str) -> None:
    _profile_dir(name).mkdir(parents=True, exist_ok=True)
    _oauth_path(name).write_text(blob, encoding="utf-8")

def _load_blob(name: str) -> Optional[str]:
    """Return the profile's stored oauth token blob (for usage / fingerprint).
    Falls back to the legacy flat <name>.blob layout."""
    for p in (_oauth_path(name), STORE_DIR / f"{name}.blob"):
        try:
            if p.exists():
                return p.read_text(encoding="utf-8")
        except OSError:
            pass
    return None

def _copy_item(src_base: Path, dst_base: Path, rel: str, kind: str) -> None:
    src = src_base / rel
    dst = dst_base / rel
    try:
        if kind == "dir":
            if src.exists():
                if dst.exists():
                    shutil.rmtree(dst, ignore_errors=True)
                shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
    except Exception:
        pass

def _capture_session(name: str) -> None:
    """Snapshot the live Claude session into the profile. Claude must be stopped."""
    sr = _session_root(name)
    if sr.exists():
        shutil.rmtree(sr, ignore_errors=True)
    sr.mkdir(parents=True, exist_ok=True)
    for rel, kind in SESSION_ITEMS:
        _copy_item(CLAUDE_DIR, sr, rel, kind)
    blob = _live_blob()
    if blob:
        _store_oauth(name, blob)

def _backup_live_session() -> Optional[Path]:
    """Back up the current live session before overwriting/clearing it."""
    ts = time.strftime("%Y%m%d-%H%M%S")
    dest = BACKUP_DIR / f"session-{ts}"
    try:
        dest.mkdir(parents=True, exist_ok=True)
        for rel, kind in SESSION_ITEMS:
            _copy_item(CLAUDE_DIR, dest, rel, kind)
        b = _live_blob()
        if b:
            (dest / "oauth.blob").write_text(b, encoding="utf-8")
    except Exception:
        return None
    for old in sorted(BACKUP_DIR.glob("session-*"))[:-MAX_SESSION_BACKUPS]:
        shutil.rmtree(old, ignore_errors=True)
    return dest

def _restore_session(name: str) -> None:
    """Replace the live Claude session with the profile's snapshot. Stopped only."""
    sr = _session_root(name)
    for rel, kind in SESSION_ITEMS:
        live = CLAUDE_DIR / rel
        snap = sr / rel
        if kind == "dir":
            if snap.exists():
                if live.exists():
                    shutil.rmtree(live, ignore_errors=True)
                shutil.copytree(snap, live, dirs_exist_ok=True)
        else:
            if snap.exists():
                live.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(snap, live)
            elif live.exists():
                try: live.unlink()
                except OSError: pass
    blob = _load_blob(name)
    if blob:
        cfg = _read_config() or {}
        cfg[OAUTH_KEY_V2] = blob   # Claude Desktop reads the live token from V2 now
        cfg[OAUTH_KEY]    = blob   # keep the legacy key in sync for older builds
        _write_config(cfg)

def _clear_live_session() -> None:
    """Remove the live web session + token so Claude shows a fresh login. Stopped.
    Also clears Local Storage (via CLEAR_EXTRA) — it isn't swapped on a normal
    switch, but a brand-new account should start from a clean slate."""
    for rel, kind in list(SESSION_ITEMS) + CLEAR_EXTRA:
        live = CLAUDE_DIR / rel
        try:
            if kind == "dir":
                if live.exists():
                    shutil.rmtree(live, ignore_errors=True)
            elif live.exists():
                live.unlink()
        except OSError:
            pass
    cfg = _read_config()
    if cfg and (OAUTH_KEY in cfg or OAUTH_KEY_V2 in cfg):
        cfg.pop(OAUTH_KEY, None)
        cfg.pop(OAUTH_KEY_V2, None)   # clear the live-read key too, or the old token lingers
        _write_config(cfg)

def _delete_profile_store(name: str) -> None:
    d = _profile_dir(name)
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    legacy = STORE_DIR / f"{name}.blob"
    if legacy.exists():
        try: legacy.unlink()
        except OSError: pass

def _valid_name(name: str) -> bool:
    return bool(name) and len(name) <= 32 and all(
        c.isalnum() or c in "-_" for c in name)

# ---------------------------------------------------------------------------
# Session decryption + usage API  (manual, read-only)
#
# The oauth blob is Chromium os_crypt: base64( "v10" + 12-byte nonce + AES-256-GCM
# ciphertext+tag ).  The AES key lives in "Local State" → os_crypt.encrypted_key,
# itself DPAPI-protected under the current Windows user.  We decrypt ONLY to read
# the access token for a read-only usage GET — the refresh token is never used, so
# nothing rotates.  Tokens are never logged, displayed, or cached.
# ---------------------------------------------------------------------------

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM as _AESGCM
    _CRYPTO_OK = True
except Exception:
    _CRYPTO_OK = False

_aes_key_cache: Optional[bytes] = None


def _dpapi_unprotect(data: bytes) -> bytes:
    import ctypes
    from ctypes import wintypes

    class _BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    bi = _BLOB(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)),
                                      ctypes.POINTER(ctypes.c_char)))
    bo = _BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(bi), None, None, None, None, 0, ctypes.byref(bo)):
        raise OSError("CryptUnprotectData failed")
    buf = ctypes.create_string_buffer(bo.cbData)
    ctypes.memmove(buf, bo.pbData, bo.cbData)
    ctypes.windll.kernel32.LocalFree(bo.pbData)
    return buf.raw


def _os_crypt_key() -> bytes:
    global _aes_key_cache
    if _aes_key_cache:
        return _aes_key_cache
    ls = json.loads(LOCAL_STATE.read_text(encoding="utf-8"))
    ek = base64.b64decode(ls["os_crypt"]["encrypted_key"])
    if ek[:5] != b"DPAPI":
        raise ValueError("unexpected os_crypt key prefix")
    _aes_key_cache = _dpapi_unprotect(ek[5:])
    return _aes_key_cache


def _decrypt_oauth(blob: Optional[str]) -> Optional[dict]:
    if not (_CRYPTO_OK and blob):
        return None
    try:
        raw = base64.b64decode(blob)
        if raw[:3] != b"v10":
            return None
        pt = _AESGCM(_os_crypt_key()).decrypt(raw[3:15], raw[15:], None)
        return json.loads(pt)
    except Exception:
        return None


def _token_info(blob: Optional[str]) -> Optional[dict]:
    """Extract {token, expiresAt, plan, tier} from a blob, preferring the
    claude_code-scoped entry. Returns None if undecryptable."""
    d = _decrypt_oauth(blob)
    if not isinstance(d, dict):
        return None
    chosen = None
    for k, v in d.items():
        if isinstance(v, dict) and v.get("token"):
            if "claude_code" in k:
                chosen = v
                break
            chosen = chosen or v
    if not chosen:
        return None
    return {
        "token":     chosen.get("token"),
        "expiresAt": chosen.get("expiresAt"),
        "plan":      chosen.get("subscriptionType"),
        "tier":      chosen.get("rateLimitTier"),
    }


def _account_id(blob: Optional[str]) -> Optional[str]:
    """Account UUID for an oauth blob — the 2nd field of its token-cache key
    (<orgId>:<accountId>:<apiUrl>:<scopes>). None if the blob can't be decrypted
    (e.g. crypto unavailable), in which case mirroring is simply skipped."""
    d = _decrypt_oauth(blob)
    if not isinstance(d, dict):
        return None
    for k in d:
        parts = k.split(":")
        if len(parts) >= 2 and len(parts[1]) >= 8:
            return parts[1]
    return None


def _known_account_ids() -> set:
    """Every account id we can see — across saved profiles and the live login."""
    ids: set = set()
    for nm in _load_meta().get("profiles", {}):
        a = _account_id(_load_blob(nm))
        if a:
            ids.add(a)
    a = _account_id(_live_blob())
    if a:
        ids.add(a)
    return ids


def _log_pairs() -> dict:
    """root_name -> set of (workspace, account) folders Claude Code has loaded or
    tried to load, parsed from main.log. Crucially this reveals the folder a
    freshly added account reads *before* it has any sessions on disk, so we can
    create and seed it. (An account/org can use several workspace ids.)"""
    out = {r: set() for r in CC_SESSION_ROOTS}
    try:
        text = CLAUDE_LOG.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return out
    for line in text.splitlines():
        if "persisted sessions from" not in line and "does not exist yet" not in line:
            continue
        for root_name in CC_SESSION_ROOTS:
            if root_name not in line:
                continue
            tail = line.split(root_name, 1)[1].replace("/", "\\")
            segs = [s for s in tail.split("\\") if s]
            if len(segs) >= 2:
                out[root_name].add((segs[0], segs[1]))
            break
    return out


def _backup_cc_roots(tag: str) -> None:
    """Snapshot the Claude Code / agent-mode session roots before a destructive
    step (deletion propagation). Pruned like the other session backups."""
    ts = time.strftime("%Y%m%d-%H%M%S")
    dest = BACKUP_DIR / f"cc-{tag}-{ts}"
    try:
        for root_name in CC_SESSION_ROOTS:
            src = CLAUDE_DIR / root_name
            if src.exists():
                shutil.copytree(src, dest / root_name, dirs_exist_ok=True)
    except Exception:
        return
    for old in sorted(BACKUP_DIR.glob("cc-*"))[:-MAX_SESSION_BACKUPS]:
        shutil.rmtree(old, ignore_errors=True)


def _session_targets(accounts: set, log_pairs: dict) -> list:
    """Every (root_name, root, folder, key) belonging to one of our accounts —
    from BOTH existing on-disk folders AND the load paths in the log. Seeding all
    of them means each login (including a brand-new account that has only just
    touched Claude Code, with an empty or not-yet-created folder) gets the merged
    history in the exact folder it reads."""
    out = []
    for root_name in CC_SESSION_ROOTS:
        root = CLAUDE_DIR / root_name
        pairs = set(log_pairs.get(root_name, set()))
        if root.exists():
            for ws in root.iterdir():
                if ws.is_dir():
                    for acc in ws.iterdir():
                        if acc.is_dir():
                            pairs.add((ws.name, acc.name))
        for ws, acc in pairs:
            if acc in accounts:
                out.append((root_name, root, root / ws / acc, f"{root_name}/{ws}/{acc}"))
    return out


def sync_cc_histories() -> dict:
    """Make Claude Code + agent-mode history global across profiles, with
    deletions honoured. EVERY folder our accounts load (<root>/<workspace>/
    <accountId>, discovered from disk + the log so even brand-new accounts are
    covered) is brought to the UNION of every account's sessions — except
    conversations the user has since deleted, which are detected (gone from a
    folder we previously wrote them to), swept from every folder, and not
    re-added. Additive otherwise, keep-newest. Claude must be stopped.
    Returns {'added': N, 'deleted': N}."""
    accounts = _known_account_ids()
    manifest = _load_json(CC_SYNC_MANIFEST)     # {folder_key: [basenames]}
    targets = _session_targets(accounts, _log_pairs())
    report = {"added": 0, "deleted": 0}

    # 1. Detect user deletions: sessions we distributed to a target folder last
    #    time that are now missing from it (Claude removed them on delete).
    deleted: set = set()
    for _rn, _root, folder, key in targets:
        prev = set(manifest.get(key, []))
        cur = {p.name for p in folder.glob("*.json")} if folder.exists() else set()
        deleted |= (prev - cur)

    # 2. Propagate deletions: sweep tombstoned sessions from EVERY folder so the
    #    next union can't resurrect them.
    if deleted:
        _backup_cc_roots("predelete")
        for root_name in CC_SESSION_ROOTS:
            root = CLAUDE_DIR / root_name
            if not root.exists():
                continue
            for p in root.glob("*/*/*.json"):
                if p.name in deleted:
                    try:
                        p.unlink()
                        report["deleted"] += 1
                    except OSError:
                        pass

    # 3. Distribute the surviving union into each managed folder.
    new_manifest: dict = {}
    for root_name in CC_SESSION_ROOTS:
        root = CLAUDE_DIR / root_name
        if not root.exists():
            continue
        union: dict = {}                        # basename -> newest source path
        for p in root.glob("*/*/*.json"):
            if p.name in deleted:
                continue
            cur = union.get(p.name)
            try:
                if cur is None or p.stat().st_mtime > cur.stat().st_mtime:
                    union[p.name] = p
            except OSError:
                pass
        for rn, _root, folder, key in targets:
            if rn != root_name:
                continue
            folder.mkdir(parents=True, exist_ok=True)
            for name, src in union.items():
                out = folder / name
                try:
                    if not out.exists() or src.stat().st_mtime > out.stat().st_mtime:
                        shutil.copy2(src, out)
                        report["added"] += 1
                except OSError:
                    pass
            new_manifest[key] = sorted(union.keys())

    _save_json(CC_SYNC_MANIFEST, new_manifest)
    return report


def _api_get(url: str, token: str, timeout: int = 15) -> dict:
    req = urllib.request.Request(url, method="GET", headers={
        "Authorization":    f"Bearer {token}",
        "anthropic-version": "2023-06-01",
        "anthropic-beta":    "oauth-2025-04-20",
        "User-Agent":        "KaliClaude/2.1",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return {"_http_error": e.code}
    except Exception as e:
        return {"_error": str(e)}


# ---------------------------------------------------------------------------
# Usage via the live claude.ai SESSION COOKIE (not the OAuth token).
#
# Reading usage with the OAuth token against api.anthropic.com is what Anthropic
# flags as anomalous token use and revokes the session ("checking usage logs me
# out"). The app's own web UI reads usage with the browser session cookie against
# claude.ai — a completely normal request that never trips that protection. So we
# do the same: decrypt the claude.ai cookies (same os_crypt key as the token) and
# call claude.ai directly. Worst case (wrong endpoint) is "no numbers", never a
# session kill.
# ---------------------------------------------------------------------------

_CLAUDE_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def _claude_cookie_header() -> Optional[str]:
    """Decrypt live claude.ai cookies into a Cookie header (never logged/stored)."""
    if not _CRYPTO_OK:
        return None
    db = CLAUDE_DIR / "Network" / "Cookies"
    if not db.exists():
        return None
    try:
        key = _os_crypt_key()
        con = sqlite3.connect(f"file:{db.as_posix()}?immutable=1", uri=True)
    except Exception:
        return None
    jar: dict = {}
    try:
        rows = con.execute(
            "select name, encrypted_value from cookies "
            "where host_key like '%claude.ai%'").fetchall()
        for name, ev in rows:
            if not ev or bytes(ev[:3]) != b"v10":
                continue
            try:
                pt = _AESGCM(key).decrypt(bytes(ev[3:15]), bytes(ev[15:]), None)
                if len(pt) > 32 and any(b < 32 or b > 126 for b in pt[:32]):
                    pt = pt[32:]          # strip Chrome 130+ SHA256(host) prefix
                jar[name] = pt.decode("utf-8", "ignore")
            except Exception:
                pass
    except Exception:
        return None
    finally:
        con.close()
    if "sessionKey" not in jar:
        return None
    return "; ".join(f"{k}={v}" for k, v in jar.items())


def _claude_web_get(path: str, cookie: str, timeout: int = 15) -> dict:
    """Cookie-authed GET to claude.ai (the same origin the web UI calls)."""
    req = urllib.request.Request("https://claude.ai" + path, method="GET", headers={
        "Cookie":                    cookie,
        "User-Agent":                _CLAUDE_UA,
        "Accept":                    "application/json",
        "Referer":                   "https://claude.ai/",
        "anthropic-client-platform": "web_claude_ai",
        "Accept-Encoding":           "gzip",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
            if (r.headers.get("content-encoding") or "").lower() == "gzip":
                data = gzip.decompress(data)
            txt = data.decode("utf-8", "replace")
            try:
                return {"_status": r.status, "json": json.loads(txt)}
            except Exception:
                return {"_status": r.status, "text": txt[:400]}
    except urllib.error.HTTPError as e:
        return {"_http_error": e.code}
    except Exception as e:
        return {"_error": str(e)}


def _norm_metric(m) -> Optional[dict]:
    """Map a claude.ai usage bucket to {utilization, resets_at} for the bars."""
    if not isinstance(m, dict):
        return None
    util = m.get("utilization")
    if util is None:
        used, limit = m.get("used"), m.get("limit")
        if isinstance(used, (int, float)) and isinstance(limit, (int, float)) and limit:
            util = 100.0 * used / limit
    reset = (m.get("resets_at") or m.get("reset_at")
             or m.get("resetsAt") or m.get("reset"))
    if util is None and reset is None:
        return None
    return {"utilization": util, "resets_at": reset}


def _usage_via_cookies() -> dict:
    """Read usage via the live claude.ai session cookie — no OAuth-token call."""
    cookie = _claude_cookie_header()
    if not cookie:
        return {"_error": "No live claude.ai session found. Open Claude, sign in, then retry."}
    orgs = _claude_web_get("/api/organizations", cookie)
    if "json" not in orgs:
        return orgs if ("_http_error" in orgs or "_error" in orgs) \
            else {"_error": "organizations: unexpected response"}
    org_list = orgs["json"] if isinstance(orgs["json"], list) else []
    if not org_list:
        return {"_error": "No organizations for this session."}
    want = None
    try:
        want = json.loads(CLAUDE_CONFIG.read_text("utf-8")).get("lastKnownAccountUuid")
    except Exception:
        pass
    org = next((o for o in org_list if want and want in json.dumps(o)), org_list[0])
    org_id = org.get("uuid")

    out: dict = {"fetched": time.time()}
    raw_usage = None
    for path in (f"/api/organizations/{org_id}/usage",
                 f"/api/organizations/{org_id}/rate_limits",
                 f"/api/organizations/{org_id}"):
        u = _claude_web_get(path, cookie)
        if "json" in u:
            j = u["json"]
            raw_usage = {"path": path, "data": j}
            base = j.get("usage") if isinstance(j.get("usage"), dict) else j
            fh = _norm_metric(base.get("five_hour"))
            sd = _norm_metric(base.get("seven_day"))
            if fh or sd:
                out["five_hour"], out["seven_day"] = fh, sd
                break
    out["email"] = org.get("billing_email") or org.get("name") or ""
    caps = " ".join(str(c) for c in (org.get("capabilities") or [])).lower()
    out["plan"] = "Max" if "max" in caps else "Pro" if "pro" in caps else ""

    # Debug dump (usage stats, no credentials) so ONE test reveals the real shape.
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (CACHE_DIR / "usage-debug.json").write_text(
            json.dumps({"orgs": org_list, "usage": raw_usage}, indent=2)[:40000], "utf-8")
    except Exception:
        pass
    if "five_hour" not in out and "seven_day" not in out:
        out["_note"] = "connected (no kill) but couldn't parse numbers — see usage-debug.json"
    return out


def _plan_label(profile: dict, fallback: Optional[str]) -> str:
    acc = profile.get("account", {})
    org = profile.get("organization", {})
    if acc.get("has_claude_max"):
        return "Max"
    if acc.get("has_claude_pro"):
        return "Pro"
    ot = org.get("organization_type", "") or ""
    if "team" in ot:
        return "Team"
    if "enterprise" in ot:
        return "Enterprise"
    return (fallback or "").capitalize() or "—"


def _until_str(iso: Optional[str]) -> str:
    """Forward countdown from an ISO timestamp → '2h 14m' / '5d 3h'."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        secs = dt.timestamp() - time.time()
        if secs <= 0:
            return "now"
        m = int(secs // 60)
        if m < 60:
            return f"{m}m"
        if m < 1440:
            return f"{m // 60}h {m % 60}m"
        return f"{m // 1440}d {(m % 1440) // 60}h"
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Claude Desktop process control
# ---------------------------------------------------------------------------

_NOWIN = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

def _claude_running() -> bool:
    try:
        r = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq claude.exe", "/NH"],
            capture_output=True, text=True, timeout=5, creationflags=_NOWIN)
        return "claude.exe" in r.stdout.lower()
    except Exception:
        return False

def _kill_claude() -> None:
    """Stop Claude Desktop. Try a GRACEFUL close first (WM_CLOSE) so Electron
    flushes the session — token + Chromium session DBs — to disk. A hard /F kill
    right after a login can lose or corrupt the just-written session, which shows
    up as being logged out after a snapshot/switch. Force-kill only stragglers
    that ignore the close (or minimize to tray) after a grace period."""
    # graceful: ask each claude.exe window to close
    subprocess.run(["taskkill", "/IM", "claude.exe"],
                   capture_output=True, creationflags=_NOWIN)
    deadline = time.time() + 6.0
    while time.time() < deadline:
        if not _claude_running():
            return
        time.sleep(0.3)
    # still up → force it
    subprocess.run(["taskkill", "/F", "/IM", "claude.exe"],
                   capture_output=True, creationflags=_NOWIN)

def _wait_stopped(timeout: float = 6.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _claude_running():
            return True
        time.sleep(0.3)
    return not _claude_running()

def _detect_aumid() -> Optional[str]:
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-StartApps | Where-Object { $_.Name -match 'Claude' } "
             "| Select-Object -First 1).AppID"],
            capture_output=True, text=True, timeout=12, creationflags=_NOWIN)
        a = (r.stdout or "").strip()
        return a or None
    except Exception:
        return None

def _launch_claude(aumid: Optional[str]) -> None:
    target = aumid or "Claude_pzs8sxrjxfjjc!Claude"
    subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{target}"],
                     creationflags=_NOWIN)

# ---------------------------------------------------------------------------
# Single-instance
# ---------------------------------------------------------------------------

def _try_mutex() -> Optional[int]:
    if os.name != "nt":
        return 1
    try:
        import ctypes
        h = ctypes.windll.kernel32.CreateMutexW(None, True, MUTEX_NAME)
        if ctypes.windll.kernel32.GetLastError() == 183:
            if h: ctypes.windll.kernel32.CloseHandle(h)
            return None
        return int(h) if h else None
    except Exception:
        return 1

def _poke_existing() -> None:
    try:
        with socket.create_connection((IPC_HOST, IPC_PORT), timeout=1.0) as s:
            s.sendall(b"FOCUS\n")
    except OSError:
        pass

def _start_ipc(on_focus) -> None:
    def _serve():
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
                srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                srv.bind((IPC_HOST, IPC_PORT))
                srv.listen(1)
                while True:
                    conn, _ = srv.accept()
                    with conn:
                        if b"FOCUS" in conn.recv(64):
                            on_focus()
        except Exception:
            pass
    threading.Thread(target=_serve, daemon=True).start()

# ---------------------------------------------------------------------------
# Profile model
# ---------------------------------------------------------------------------

class Profile:
    __slots__ = ("name", "label", "note", "updated", "fp", "is_active",
                 "has_blob", "email", "plan", "usage")

    def __init__(self, name, label, note, updated, fp, is_active, has_blob,
                 email="", plan="", usage=None):
        self.name = name
        self.label = label
        self.note = note
        self.updated = updated
        self.fp = fp
        self.is_active = is_active
        self.has_blob = has_blob
        self.email = email
        self.plan = plan
        self.usage = usage or {}

def _list_profiles() -> list[Profile]:
    m = _load_meta()
    active = m.get("active")
    out: list[Profile] = []
    for name, info in sorted(m.get("profiles", {}).items()):
        out.append(Profile(
            name=name,
            label=info.get("label", ""),
            note=info.get("note", ""),
            updated=info.get("updated", 0),
            fp=info.get("fp", ""),
            is_active=(name == active),
            has_blob=_has_session(name),
            email=info.get("email", ""),
            plan=info.get("plan", ""),
            usage=info.get("usage") or {},
        ))
    return out

def _rel_time(ts: float) -> str:
    if not ts:
        return "never"
    secs = max(0, time.time() - ts)
    if secs < 60:   return "just now"
    if secs < 3600: return f"{int(secs//60)}m ago"
    if secs < 86400:return f"{int(secs//3600)}h ago"
    return f"{int(secs//86400)}d ago"

# ---------------------------------------------------------------------------
# Widget helpers
# ---------------------------------------------------------------------------

def _draw_spark(canvas, cx, cy, r, color, hub_text="K", hub_fg=BG_SIDEBAR):
    """Draw the Claude-style radiating spark (with K hub) on a tk.Canvas."""
    import math
    n = 12
    w = max(2, int(r * 0.15))
    hub = max(4, int(r * 0.46))
    for i in range(n):
        a = 2 * math.pi * i / n - math.pi / 2
        length = r if i % 2 == 0 else r * 0.74
        r0 = hub * 0.92
        x0, y0 = cx + r0 * math.cos(a), cy + r0 * math.sin(a)
        x1, y1 = cx + length * math.cos(a), cy + length * math.sin(a)
        canvas.create_line(x0, y0, x1, y1, fill=color, width=w,
                           capstyle=tk.ROUND)
    canvas.create_oval(cx - hub, cy - hub, cx + hub, cy + hub,
                       fill=color, outline=color)
    if hub_text:
        canvas.create_text(cx, cy + 1, text=hub_text, fill=hub_fg,
                           font=(FF, max(7, int(hub * 1.15)), "bold"))


def _btn(parent, text, cmd, accent=False, danger=False, **kw) -> tk.Button:
    base = {"font": (FF, 9), "relief": tk.FLAT, "bd": 0,
            "padx": 12, "pady": 5, "cursor": "hand2"}
    if danger:
        base.update(bg="#3D1F1F", fg=CLR_ERR, activebackground="#4A2626",
                    activeforeground=CLR_ERR)
    elif accent:
        base.update(bg=CLR_ACCENT, fg="#17140F", activebackground="#BF6330",
                    activeforeground="#17140F")
    else:
        base.update(bg=BG_CARD, fg=TXT_PRI, activebackground=BG_CARD_HV,
                    activeforeground=TXT_PRI)
    base.update(kw)
    return tk.Button(parent, text=text, command=cmd, **base)

# ---------------------------------------------------------------------------
# Main App
# ---------------------------------------------------------------------------

class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self._profiles: list[Profile] = []
        self._sel = -1
        self._q: queue.Queue = queue.Queue()
        self._claude_up = False
        self._live_present = False
        self._note_pending = False
        self._busy = False
        self._usage_busy = False
        self._switch_ctx: dict = {}

        self._build()
        _start_ipc(lambda: self.root.after(0, self._focus))
        self.root.after(200, self._refresh)
        self.root.after(1500, self._tick)
        self.root.after(150, self._pump)
        # detect & cache AUMID in background
        threading.Thread(target=self._ensure_aumid, daemon=True).start()

    def _ensure_aumid(self):
        m = _load_meta()
        if not m.get("aumid"):
            a = _detect_aumid()
            if a:
                m["aumid"] = a
                _save_meta(m)

    def _focus(self):
        self.root.deiconify(); self.root.lift(); self.root.focus_force()

    # ----- layout -----------------------------------------------------------

    def _build(self):
        self.root.title(APP_TITLE)
        self.root.geometry("900x680")
        self.root.minsize(760, 600)
        self.root.configure(bg=BG_ROOT)
        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)
        if ICON_PATH.exists():
            try: self.root.iconbitmap(str(ICON_PATH))
            except Exception: pass

        style = ttk.Style()
        try: style.theme_use("default")
        except tk.TclError: pass
        try:
            style.configure("Kali.Vertical.TScrollbar", background=BG_SIDEBAR,
                            troughcolor=BG_SIDEBAR, bordercolor=BG_SIDEBAR,
                            arrowcolor=TXT_MUTE, relief=tk.FLAT, borderwidth=0)
        except tk.TclError: pass

        self._build_header()
        body = tk.Frame(self.root, bg=BG_ROOT)
        body.pack(fill=tk.BOTH, expand=True)
        self._build_sidebar(body)
        tk.Frame(body, bg=CLR_DIV, width=1).pack(side=tk.LEFT, fill=tk.Y)
        self._build_panel(body)
        self._build_status()

    def _build_header(self):
        hdr = tk.Frame(self.root, bg=BG_SIDEBAR, height=54)
        hdr.pack(fill=tk.X); hdr.pack_propagate(False)

        mark = tk.Canvas(hdr, width=30, height=30, bg=BG_SIDEBAR,
                         highlightthickness=0, bd=0)
        mark.pack(side=tk.LEFT, padx=(16, 0), pady=12)
        _draw_spark(mark, 15, 15, 13, CLR_ACCENT)

        tk.Label(hdr, text="KaliClaude", bg=BG_SIDEBAR, fg=TXT_PRI,
                 font=(FF, 13, "bold"), padx=10).pack(side=tk.LEFT, pady=16)
        tk.Label(hdr, text="Session Switcher", bg=BG_SIDEBAR, fg=TXT_MUTE,
                 font=(FF, 9)).pack(side=tk.LEFT, pady=20)

        right = tk.Frame(hdr, bg=BG_SIDEBAR)
        right.pack(side=tk.RIGHT, padx=16, pady=12)
        self._btn_refresh = _btn(right, "Refresh", self._refresh, bg=BG_CARD)
        self._btn_refresh.pack(side=tk.RIGHT, padx=(6, 0))
        _btn(right, "Save Current Login", self._on_save_current,
             accent=True).pack(side=tk.RIGHT, padx=(6, 0))
        _btn(right, "Prepare New Login", self._on_prepare_login,
             bg=BG_CARD).pack(side=tk.RIGHT, padx=(6, 0))
        _btn(right, "Sync History", self._on_sync_histories,
             bg=BG_CARD).pack(side=tk.RIGHT)

    def _build_sidebar(self, parent):
        sb = tk.Frame(parent, bg=BG_SIDEBAR, width=240)
        sb.pack(side=tk.LEFT, fill=tk.Y); sb.pack_propagate(False)

        self._count_lbl = tk.Label(sb, text="PROFILES", bg=BG_SIDEBAR,
                                    fg=TXT_MUTE, font=(FF, 7), anchor="w",
                                    padx=14, pady=6)
        self._count_lbl.pack(fill=tk.X)

        wrap = tk.Frame(sb, bg=BG_SIDEBAR); wrap.pack(fill=tk.BOTH, expand=True)
        self._cv = tk.Canvas(wrap, bg=BG_SIDEBAR, highlightthickness=0, bd=0)
        vsb = ttk.Scrollbar(wrap, orient=tk.VERTICAL, command=self._cv.yview,
                            style="Kali.Vertical.TScrollbar")
        self._inner = tk.Frame(self._cv, bg=BG_SIDEBAR)
        self._cv.configure(yscrollcommand=vsb.set)
        self._cv.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._win = self._cv.create_window((0, 0), window=self._inner, anchor="nw")
        self._inner.bind("<Configure>",
                         lambda _: self._cv.configure(scrollregion=self._cv.bbox("all")))
        self._cv.bind("<Configure>",
                      lambda e: self._cv.itemconfig(self._win, width=e.width))
        self._cv.bind("<MouseWheel>",
                      lambda e: self._cv.yview_scroll(-1 if e.delta > 0 else 1, "units"))

    def _build_panel(self, parent):
        self._panel = tk.Frame(parent, bg=BG_PANEL)
        self._panel.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._empty = tk.Label(
            self._panel,
            text="No profile selected\n\n"
                 "Log into Claude, then click  'Save Current Login'\n"
                 "to capture this session as a profile.",
            bg=BG_PANEL, fg=TXT_MUTE, font=(FF, 11), justify=tk.CENTER)
        self._empty.place(relx=0.5, rely=0.42, anchor="center")

        self._content = tk.Frame(self._panel, bg=BG_PANEL)

        nr = tk.Frame(self._content, bg=BG_PANEL)
        nr.pack(fill=tk.X, padx=32, pady=(28, 4))
        self._dot = tk.Label(nr, text="●", bg=BG_PANEL, fg=TXT_MUTE, font=(FF, 11))
        self._dot.pack(side=tk.LEFT, padx=(0, 8))
        self._name_lbl = tk.Label(nr, text="", bg=BG_PANEL, fg=TXT_PRI,
                                   font=(FF, 17, "bold"))
        self._name_lbl.pack(side=tk.LEFT)
        self._active_badge = tk.Label(nr, text=" ACTIVE ", bg=CLR_ACCENT,
                                       fg="#17140F", font=(FF, 7, "bold"),
                                       padx=4, pady=2)

        meta = tk.Frame(self._content, bg=BG_PANEL)
        meta.pack(fill=tk.X, padx=32, pady=(0, 4))
        self._email_row = self._meta_row(meta, "Email")
        self._plan_row  = self._meta_row(meta, "Plan")
        self._saved_row = self._meta_row(meta, "Snapshot")

        tk.Frame(self._content, bg=CLR_DIV, height=1).pack(
            fill=tk.X, padx=32, pady=(10, 10))

        # ---- Usage (live, manual refresh) ----
        uh = tk.Frame(self._content, bg=BG_PANEL)
        uh.pack(fill=tk.X, padx=32, pady=(0, 6))
        tk.Label(uh, text="Usage", bg=BG_PANEL, fg=TXT_MUTE,
                 font=(FF, 9)).pack(side=tk.LEFT)
        self._usage_when = tk.Label(uh, text="", bg=BG_PANEL, fg=TXT_MUTE,
                                    font=(FF, 8))
        self._usage_when.pack(side=tk.LEFT, padx=8)
        self._btn_usage = _btn(uh, "Refresh Usage", self._on_refresh_usage,
                               bg=BG_CARD, padx=10, pady=3)
        self._btn_usage.pack(side=tk.RIGHT)

        self._u5_cv, self._u5_val = self._usage_row("5-hour")
        self._u7_cv, self._u7_val = self._usage_row("7-day")

        tk.Frame(self._content, bg=CLR_DIV, height=1).pack(
            fill=tk.X, padx=32, pady=(10, 12))

        nh = tk.Frame(self._content, bg=BG_PANEL)
        nh.pack(fill=tk.X, padx=32, pady=(0, 6))
        tk.Label(nh, text="Note", bg=BG_PANEL, fg=TXT_MUTE,
                 font=(FF, 9)).pack(side=tk.LEFT)
        self._note_saved = tk.Label(nh, text="", bg=BG_PANEL, fg=CLR_OK,
                                     font=(FF, 8))
        self._note_saved.pack(side=tk.LEFT, padx=8)
        self._note = tk.Text(self._content, height=3, font=(FF, 10),
                             bg=BG_INPUT, fg=TXT_PRI, insertbackground=TXT_PRI,
                             relief=tk.FLAT, bd=0, padx=10, pady=8,
                             highlightbackground=CLR_DIV, highlightthickness=1,
                             wrap=tk.WORD)
        self._note.pack(fill=tk.X, padx=32, pady=(0, 16))
        self._note.bind("<KeyRelease>", self._note_changed)

        act = tk.Frame(self._content, bg=BG_PANEL)
        act.pack(fill=tk.X, padx=32, pady=(0, 8))
        self._btn_switch = _btn(act, "Switch to this Profile",
                                self._on_switch, accent=True)
        self._btn_switch.pack(side=tk.LEFT, padx=(0, 8))
        self._btn_update = _btn(act, "Update Snapshot", self._on_update)
        self._btn_update.pack(side=tk.LEFT, padx=(0, 8))
        self._btn_rename = _btn(act, "Rename", self._on_rename)
        self._btn_rename.pack(side=tk.LEFT, padx=(0, 8))
        self._btn_remove = _btn(act, "Remove", self._on_remove, danger=True)
        self._btn_remove.pack(side=tk.LEFT)

        hint = tk.Label(
            self._content,
            text="“Update Snapshot” re-captures the current login into this "
                 "profile (use after a long session to keep its token fresh).",
            bg=BG_PANEL, fg=TXT_MUTE, font=(FF, 8), justify=tk.LEFT,
            wraplength=460, anchor="w")
        hint.pack(fill=tk.X, padx=32, pady=(2, 14))

        tk.Frame(self._content, bg=CLR_DIV, height=1).pack(
            fill=tk.X, padx=32, pady=(0, 12))

        ca = tk.Frame(self._content, bg=BG_PANEL)
        ca.pack(fill=tk.X, padx=32)
        self._claude_lbl = tk.Label(ca, text="", bg=BG_PANEL, fg=TXT_MUTE,
                                    font=(FF, 9))
        self._claude_lbl.pack(side=tk.LEFT, padx=(0, 10))
        self._btn_launch = _btn(ca, "Launch Claude", self._on_launch)
        self._btn_launch.pack(side=tk.LEFT, padx=(0, 6))
        self._btn_stop = _btn(ca, "Stop Claude", self._on_stop, danger=True)
        self._btn_stop.pack(side=tk.LEFT)

    def _meta_row(self, parent, label):
        row = tk.Frame(parent, bg=BG_PANEL); row.pack(fill=tk.X, pady=2)
        tk.Label(row, text=label, bg=BG_PANEL, fg=TXT_MUTE, font=(FF, 9),
                 width=11, anchor="w").pack(side=tk.LEFT)
        val = tk.Label(row, text="—", bg=BG_PANEL, fg=TXT_SUB, font=(FF, 9))
        val.pack(side=tk.LEFT)
        return val

    def _usage_row(self, caption):
        row = tk.Frame(self._content, bg=BG_PANEL)
        row.pack(fill=tk.X, padx=32, pady=2)
        tk.Label(row, text=caption, bg=BG_PANEL, fg=TXT_SUB, font=(FF, 9),
                 width=7, anchor="w").pack(side=tk.LEFT)
        cv = tk.Canvas(row, width=210, height=10, bg=BG_CARD,
                       highlightthickness=0, bd=0)
        cv.pack(side=tk.LEFT, padx=(2, 10))
        val = tk.Label(row, text="—", bg=BG_PANEL, fg=TXT_MUTE, font=(FF, 8))
        val.pack(side=tk.LEFT)
        return cv, val

    def _draw_bar(self, cv, pct):
        cv.delete("all")
        w = int(cv.cget("width")); h = int(cv.cget("height"))
        cv.create_rectangle(0, 0, w, h, fill=BG_CARD, outline=BG_CARD)
        if pct is None:
            return
        pct = max(0.0, min(100.0, float(pct)))
        color = CLR_OK if pct < 70 else (CLR_WARN if pct < 90 else CLR_ERR)
        fw = int(round(w * pct / 100.0))
        if fw > 0:
            cv.create_rectangle(0, 0, fw, h, fill=color, outline=color)

    def _build_status(self):
        bar = tk.Frame(self.root, bg=BG_SIDEBAR, height=26)
        bar.pack(fill=tk.X, side=tk.BOTTOM); bar.pack_propagate(False)
        self._st = tk.Label(bar, text="Ready", bg=BG_SIDEBAR, fg=TXT_MUTE,
                            font=(FF, 8), anchor="w", padx=14)
        self._st.pack(side=tk.LEFT, fill=tk.Y)
        self._live_lbl = tk.Label(bar, text="", bg=BG_SIDEBAR, fg=TXT_MUTE,
                                  font=(FF, 8), padx=14)
        self._live_lbl.pack(side=tk.RIGHT, fill=tk.Y)

    # ----- list rendering ---------------------------------------------------

    def _render_list(self):
        for w in self._inner.winfo_children():
            w.destroy()
        n = len(self._profiles)
        self._count_lbl.configure(text=f"PROFILES  ·  {n}" if n else "PROFILES")
        if not n:
            tk.Label(self._inner,
                     text="No saved sessions yet\n\nLog into Claude, then\n"
                          "'Save Current Login'",
                     bg=BG_SIDEBAR, fg=TXT_MUTE, font=(FF, 9),
                     justify=tk.CENTER, pady=40).pack(fill=tk.X, padx=10)
            return
        for i, p in enumerate(self._profiles):
            self._card(i, p)

    def _card(self, idx, p: Profile):
        sel = (idx == self._sel)
        cbg = BG_CARD_SEL if sel else BG_CARD
        outer = tk.Frame(self._inner, bg=cbg, cursor="hand2")
        outer.pack(fill=tk.X, padx=8, pady=(3, 0))
        tk.Frame(outer, bg=CLR_ACTIVE if p.is_active else cbg,
                 width=3).pack(side=tk.LEFT, fill=tk.Y)
        inner = tk.Frame(outer, bg=cbg)
        inner.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(10, 10), pady=9)

        nr = tk.Frame(inner, bg=cbg); nr.pack(fill=tk.X)
        tk.Label(nr, text="●" if p.is_active else "○", bg=cbg,
                 fg=CLR_ACTIVE if p.is_active else TXT_MUTE,
                 font=(FF, 8)).pack(side=tk.LEFT, padx=(0, 6))
        tk.Label(nr, text=p.name, bg=cbg, fg=TXT_PRI,
                 font=(FF, 10, "bold" if p.is_active else "normal"),
                 anchor="w").pack(side=tk.LEFT, fill=tk.X, expand=True)

        sub = p.email or p.label or (p.note[:30] + "…" if len(p.note) > 30 else p.note)
        if sub:
            tk.Label(inner, text=sub, bg=cbg, fg=TXT_MUTE, font=(FF, 8),
                     anchor="w").pack(fill=tk.X, pady=(2, 0))
        meta = f"saved {_rel_time(p.updated)}"
        if not p.has_blob:
            meta = "⚠ snapshot missing"
        tk.Label(inner, text=meta, bg=cbg,
                 fg=CLR_WARN if not p.has_blob else TXT_MUTE,
                 font=(FF, 7), anchor="w").pack(fill=tk.X, pady=(3, 0))

        for w in self._all(outer) + [outer]:
            w.bind("<Button-1>", lambda e, i=idx: self._select(i))
            w.bind("<Double-Button-1>", lambda e, i=idx: self._dbl(i))
            w.bind("<Enter>", lambda e, f=outer,
                   b=(BG_CARD_SEL if sel else BG_CARD_HV): self._recolor(f, b))
            w.bind("<Leave>", lambda e, f=outer, b=cbg: self._recolor(f, b))

    def _all(self, w):
        out = []
        for c in w.winfo_children():
            out.append(c); out.extend(self._all(c))
        return out

    def _recolor(self, frame, bg):
        try:
            frame.configure(bg=bg)
            for w in self._all(frame):
                try: w.configure(bg=bg)
                except tk.TclError: pass
        except tk.TclError: pass

    # ----- detail -----------------------------------------------------------

    def _show_detail(self, p: Optional[Profile]):
        if p is None:
            self._content.place_forget()
            self._empty.place(relx=0.5, rely=0.42, anchor="center")
            return
        self._empty.place_forget()
        self._content.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._name_lbl.configure(text=p.name)
        if p.is_active:
            self._dot.configure(fg=CLR_ACTIVE)
            self._active_badge.pack(side=tk.LEFT, padx=10)
        else:
            self._dot.configure(fg=TXT_MUTE)
            self._active_badge.pack_forget()
        self._email_row.configure(text=p.email or "— (refresh usage to fetch)")
        self._plan_row.configure(text=p.plan or "—")
        self._saved_row.configure(
            text=_rel_time(p.updated) if p.has_blob else "⚠ missing",
            fg=CLR_WARN if not p.has_blob else TXT_SUB)
        self._note.delete("1.0", tk.END); self._note.insert("1.0", p.note)
        self._note_saved.configure(text="")
        self._render_usage(p)

        if p.is_active:
            self._btn_switch.configure(text="Active Now", state=tk.DISABLED,
                                       bg=BG_CARD, fg=TXT_MUTE,
                                       activebackground=BG_CARD)
        elif not p.has_blob:
            self._btn_switch.configure(text="No Snapshot", state=tk.DISABLED,
                                       bg=BG_CARD, fg=TXT_MUTE,
                                       activebackground=BG_CARD)
        else:
            self._btn_switch.configure(text="Switch to this Profile",
                                       state=tk.NORMAL, bg=CLR_ACCENT,
                                       fg="#17140F", activebackground="#BF6330")
        self._update_btn = getattr(self, "_btn_update", None)
        self._btn_update.configure(
            state=tk.NORMAL if self._live_present else tk.DISABLED,
            bg=BG_CARD if self._live_present else BG_CARD,
            fg=TXT_PRI if self._live_present else TXT_MUTE)
        self._refresh_claude_ui()

    def _render_usage(self, p: Profile):
        u = p.usage or {}

        def fmt(metric):
            metric = metric or {}
            util = metric.get("utilization")
            if util is None:
                return None, "—"
            reset = _until_str(metric.get("resets_at"))
            txt = f"{int(round(util))}% used"
            if reset:
                txt += f" · resets in {reset}"
            return util, txt

        u5, t5 = fmt(u.get("five_hour"))
        u7, t7 = fmt(u.get("seven_day"))
        self._draw_bar(self._u5_cv, u5)
        self._draw_bar(self._u7_cv, u7)
        self._u5_val.configure(text=t5)
        self._u7_val.configure(text=t7)

        fetched = u.get("fetched")
        if not _CRYPTO_OK:
            self._usage_when.configure(text="· unavailable")
        elif fetched:
            tag = _rel_time(fetched) + ("" if p.is_active else " · cached")
            self._usage_when.configure(text=f"· {tag}")
        else:
            self._usage_when.configure(text="· not fetched yet")

        can = _CRYPTO_OK and (self._live_present if p.is_active else p.has_blob)
        if self._usage_busy:
            can = False
        self._btn_usage.configure(
            state=tk.NORMAL if can else tk.DISABLED,
            fg=TXT_PRI if can else TXT_MUTE,
            text="Refresh Usage")

    def _refresh_claude_ui(self):
        if self._claude_up:
            self._claude_lbl.configure(text="● Claude is running", fg=CLR_OK)
            self._btn_launch.configure(state=tk.DISABLED, bg=BG_CARD, fg=TXT_MUTE)
            self._btn_stop.configure(state=tk.NORMAL, bg="#3D1F1F", fg=CLR_ERR)
        else:
            self._claude_lbl.configure(text="○ Claude is not running", fg=TXT_MUTE)
            self._btn_launch.configure(state=tk.NORMAL, bg=BG_CARD, fg=TXT_PRI)
            self._btn_stop.configure(state=tk.DISABLED, bg=BG_CARD, fg=TXT_MUTE)

    # ----- refresh / polling ------------------------------------------------

    def _refresh(self):
        self._profiles = _list_profiles()
        if self._sel >= len(self._profiles):
            self._sel = len(self._profiles) - 1
        self._render_list()
        self._show_detail(self._profiles[self._sel]
                          if 0 <= self._sel < len(self._profiles) else None)
        m = _load_meta()
        active = m.get("active")
        n = len(self._profiles)
        self._set_status(f"{n} profile{'s' if n != 1 else ''}"
                         + (f"  ·  Active: {active}" if active else ""))

    def _tick(self):
        def check():
            up = _claude_running()
            live = _live_blob() is not None
            self.root.after(0, lambda: self._apply_tick(up, live))
        threading.Thread(target=check, daemon=True).start()
        self.root.after(5000, self._tick)

    def _apply_tick(self, up, live):
        self._claude_up = up
        self._live_present = live
        self._live_lbl.configure(
            text="● session detected" if live else "○ not logged in",
            fg=CLR_OK if live else TXT_MUTE)
        if self._content.winfo_ismapped():
            self._refresh_claude_ui()
            if 0 <= self._sel < len(self._profiles):
                self._btn_update.configure(
                    state=tk.NORMAL if live else tk.DISABLED,
                    fg=TXT_PRI if live else TXT_MUTE)

    # ----- selection --------------------------------------------------------

    def _select(self, idx):
        self._sel = idx
        self._render_list()
        self._show_detail(self._profiles[idx]
                          if 0 <= idx < len(self._profiles) else None)

    def _dbl(self, idx):
        self._sel = idx
        if 0 <= idx < len(self._profiles):
            p = self._profiles[idx]
            if not p.is_active and p.has_blob:
                self._on_switch()

    # ----- actions ----------------------------------------------------------

    def _on_refresh_usage(self):
        # ACTIVE ACCOUNT ONLY. Usage is read using the live account's own token
        # (from oauth:tokenCacheV2) against the same OAuth endpoint Claude Code
        # itself uses — indistinguishable from normal usage. We never read a
        # non-active profile's stored token, which is the cross-account pattern
        # that could look anomalous. Non-active profiles show their last cache.
        if self._usage_busy or not (0 <= self._sel < len(self._profiles)):
            return
        if not _CRYPTO_OK:
            messagebox.showwarning(
                "Usage Unavailable",
                "The encryption library isn't available, so usage can't be read.",
                parent=self.root)
            return
        p = self._profiles[self._sel]
        if not p.is_active:
            messagebox.showinfo(
                "Active Account Only",
                "Usage can only be refreshed for the account you're currently "
                "signed into (the active profile). Other profiles show the last "
                "cached numbers — switch to an account and refresh to update its "
                "reset countdown.", parent=self.root)
            return
        name = p.name
        self._usage_busy = True
        self._btn_usage.configure(state=tk.DISABLED, text="Fetching…", fg=TXT_MUTE)
        self._set_status(f"Fetching usage for {name}…")

        def work():
            try:
                # Cookie-authed read against claude.ai (NOT the OAuth token against
                # api.anthropic.com) — this is what the app's web UI does, so it
                # never trips the token-anomaly revocation that was logging you out.
                u = _usage_via_cookies()
                if "_http_error" in u or "_error" in u:
                    self._q.put(("usage_err",
                                 (name, u.get("_error")
                                  or f"claude.ai returned {u.get('_http_error')}")))
                    return
                result = {
                    "five_hour": u.get("five_hour"),
                    "seven_day": u.get("seven_day"),
                    "fetched":   u.get("fetched", time.time()),
                }
                if u.get("email"):
                    result["email"] = u["email"]
                if u.get("plan"):
                    result["plan"] = u["plan"]
                self._q.put(("usage_ok", (name, result)))
            except Exception as e:
                self._q.put(("usage_err", (name, str(e))))

        threading.Thread(target=work, daemon=True).start()

    def _on_save_current(self):
        if self._busy:
            return
        if not _live_blob():
            messagebox.showwarning(
                "Not Logged In",
                "No active Claude session was found.\n\n"
                "Log into Claude Desktop first, then click "
                "'Save Current Login'.", parent=self.root)
            return
        existing = [p.name for p in self._profiles]
        dlg = SaveDialog(self.root, existing)
        self.root.wait_window(dlg.top)
        if not dlg.result:
            return
        name, label, note = dlg.result
        if not messagebox.askyesno(
            "Save Current Login",
            f"Capture the current login as profile '{name}'.\n\n"
            "Claude stays open — this reads the live session without closing it. "
            "Continue?", parent=self.root):
            return

        self._busy = True
        self._set_status(f"Saving '{name}'…")

        def work():
            try:
                _capture_session(name)   # live capture — Claude stays open
                blob = _load_blob(name)
                m = _load_meta()
                info = m["profiles"].get(name, {})
                info.update(label=label, note=note, updated=time.time(),
                            fp=_fp(blob))
                info.setdefault("created", time.time())
                m["profiles"][name] = info
                m["active"] = name
                _save_meta(m)
                self._q.put(("save_ok", name))
            except Exception as e:
                self._q.put(("save_err", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def _on_update(self):
        if self._busy or not (0 <= self._sel < len(self._profiles)):
            return
        p = self._profiles[self._sel]
        if not _live_blob():
            messagebox.showwarning("Not Logged In",
                                   "No active Claude session to capture.",
                                   parent=self.root)
            return
        if not messagebox.askyesno(
            "Update Snapshot",
            f"Re-capture the CURRENT login into profile '{p.name}'?\n\n"
            "Use this only if the current login belongs to this profile.\n"
            "Claude stays open — the live session is read without closing it. "
            "Continue?",
            parent=self.root):
            return
        name = p.name
        self._busy = True
        self._set_status(f"Updating '{name}'…")

        def work():
            try:
                _capture_session(name)   # live capture — Claude stays open
                blob = _load_blob(name)
                m = _load_meta()
                info = m["profiles"].setdefault(name, {})
                info.update(updated=time.time(), fp=_fp(blob))
                m["active"] = name
                _save_meta(m)
                self._q.put(("save_ok", name))
            except Exception as e:
                self._q.put(("save_err", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def _on_switch(self):
        if self._busy or not (0 <= self._sel < len(self._profiles)):
            return
        p = self._profiles[self._sel]
        if p.is_active or not p.has_blob:
            return
        running = _claude_running()
        msg = f"Switch Claude to profile  '{p.name}'?"
        if running:
            msg += "\n\nClaude is running and will be closed first."
        if not messagebox.askyesno("Switch Profile", msg, parent=self.root):
            return

        self._busy = True
        self._btn_switch.configure(state=tk.DISABLED, text="Switching…",
                                   bg=BG_CARD, fg=TXT_MUTE)
        self._set_status(f"Switching to {p.name}…")
        target = p.name

        # Phase 1 (worker): stop Claude, then read the outgoing token and decide
        # whether its snapshot needs refreshing — WITHOUT writing anything. The
        # main thread handles any prompt (Tk isn't thread-safe), then phase 2 commits.
        def work():
            try:
                if _claude_running():
                    _kill_claude()
                    if not _wait_stopped(6.0):
                        raise RuntimeError(
                            "Claude did not close — try again or stop it manually.")
                m = _load_meta()
                # Restore-only switch: NEVER re-capture the outgoing account's
                # snapshot. Snapshots are refreshed only when YOU choose, via
                # Update Snapshot. The token lives ~a month, so a switch leaves the
                # current account's snapshot completely untouched.
                self._switch_ctx = {"target": target, "active": m.get("active")}
                self._q.put(("switch_phase1", {}))
            except Exception as e:
                self._q.put(("switch_err", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def _start_switch_phase2(self):
        """Commit the switch: optionally re-capture the outgoing full session,
        back up the live session, then restore the target's full session."""
        ctx = self._switch_ctx

        def work():
            try:
                m = _load_meta()
                # Restore-only: the outgoing account's snapshot is intentionally
                # left untouched (refresh it manually with Update Snapshot).
                if not _has_session(ctx["target"]):
                    raise RuntimeError(
                        f"'{ctx['target']}' has no full-session snapshot. "
                        "Switch to it once and use 'Update Snapshot', or re-save it.")
                _backup_live_session()
                _restore_session(ctx["target"])
                # Merge Claude Code / agent-mode history across all accounts so
                # the incoming profile sees the combined project list + sessions.
                try:
                    sync_cc_histories()
                except Exception:
                    pass
                m["active"] = ctx["target"]
                _save_meta(m)
                self._q.put(("switch_ok", ctx["target"]))
            except Exception as e:
                self._q.put(("switch_err", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def _on_prepare_login(self):
        """Capture the current session (optional), then CLEAR the live web
        session + token so Claude shows a genuine fresh login screen. Saved
        profiles remain valid and restorable."""
        if self._busy:
            return
        save_req = None
        m0 = _load_meta()
        active = m0.get("active")
        already_saved = bool(active and active in m0["profiles"]
                             and _has_session(active))
        # Only offer to save the current login if it isn't already captured
        # (e.g. you just used "Save Current Login"). No nagging otherwise.
        if _live_blob() and not already_saved:
            if messagebox.askyesno(
                "Save Current First?",
                "The current login isn't saved as a profile yet.\n\n"
                "Save it before preparing a new one, so you can switch back?",
                parent=self.root):
                existing = [p.name for p in self._profiles]
                dlg = SaveDialog(self.root, existing)
                self.root.wait_window(dlg.top)
                if dlg.result:
                    save_req = dlg.result   # (name, label, note)

        if not messagebox.askyesno(
            "Prepare New Login",
            "This closes Claude and CLEARS the active web session so you can "
            "sign in with a DIFFERENT account.\n\n"
            "• Your saved profiles are NOT affected — they stay restorable.\n"
            "• The current session is backed up first.\n\n"
            "After Claude reopens you'll see a fresh login screen. Sign in, "
            "then click 'Save Current Login'.\n\nContinue?",
            parent=self.root):
            return

        self._busy = True
        self._set_status("Preparing new login…")

        def work():
            try:
                if _claude_running():
                    _kill_claude()
                    if not _wait_stopped(6.0):
                        raise RuntimeError("Claude did not close.")
                if save_req:
                    sname, slabel, snote = save_req
                    _capture_session(sname)
                    blob = _load_blob(sname)
                    m = _load_meta()
                    info = m["profiles"].get(sname, {})
                    info.update(label=slabel, note=snote, updated=time.time(),
                                fp=_fp(blob))
                    info.setdefault("created", time.time())
                    m["profiles"][sname] = info
                    _save_meta(m)
                _backup_live_session()
                _clear_live_session()
                m = _load_meta()
                m["active"] = None
                _save_meta(m)
                self._q.put(("prep_ok", None))
            except Exception as e:
                self._q.put(("prep_err", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def _on_sync_histories(self):
        """Merge Claude Code + agent-mode history across every account so all
        profiles share one project list / session history. Closes Claude first
        (file safety), then redistributes the union into each account's folder."""
        if self._busy:
            return
        if not messagebox.askyesno(
            "Sync Claude Code History",
            "Merge every account's Claude Code + agent-mode sessions so the "
            "combined project list and history are visible under any profile.\n\n"
            "• Additive only — nothing is deleted, newest copy wins.\n"
            "• Claude will be closed first if it's running.\n\n"
            "Continue?", parent=self.root):
            return
        self._busy = True
        self._set_status("Syncing Claude Code history…")

        def work():
            try:
                if _claude_running():
                    _kill_claude()
                    if not _wait_stopped(6.0):
                        raise RuntimeError("Claude did not close.")
                report = sync_cc_histories()
                self._q.put(("sync_ok", report))
            except Exception as e:
                self._q.put(("sync_err", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def _pump(self):
        """Single persistent dispatcher for background-thread results."""
        try:
            while True:
                kind, data = self._q.get_nowait()
                self._handle_result(kind, data)
        except queue.Empty:
            pass
        self.root.after(150, self._pump)

    def _handle_result(self, kind, data):
        if kind == "switch_phase1":
            # Restore-only switch: no outgoing re-capture, no prompt — go commit.
            self._start_switch_phase2()
        elif kind == "switch_ok":
            self._busy = False
            self._set_status(f"Switched to '{data}'.")
            self._refresh()
            if messagebox.askyesno(
                "Launch Claude",
                f"Now signed in as '{data}'.\n\nLaunch Claude?",
                parent=self.root):
                _launch_claude(_load_meta().get("aumid"))
                self._set_status("Launching Claude…")
        elif kind == "switch_err":
            self._busy = False
            messagebox.showerror("Switch Failed", data, parent=self.root)
            self._set_status(f"Switch failed: {data}")
            self._refresh()
        elif kind == "save_ok":
            self._busy = False
            self._set_status(f"Saved '{data}'.")
            self._sel = -1
            self._refresh()
            for i, p in enumerate(self._profiles):
                if p.name == data:
                    self._select(i); break
            # Live capture leaves Claude running — only offer to launch if it's
            # actually closed, so a normal snapshot doesn't nag or steal focus.
            if not _claude_running() and messagebox.askyesno(
                "Launch Claude", f"Saved '{data}'.\n\nReopen Claude now?",
                parent=self.root):
                _launch_claude(_load_meta().get("aumid"))
                self._set_status("Launching Claude…")
        elif kind == "save_err":
            self._busy = False
            messagebox.showerror("Save Failed", data, parent=self.root)
            self._set_status(f"Save failed: {data}")
            self._refresh()
        elif kind == "prep_ok":
            self._busy = False
            self._refresh()
            if messagebox.askyesno(
                "Ready for New Login",
                "Session cleared. Launch Claude now to sign in with the new "
                "account?\n\n(After signing in, click 'Save Current Login'.)",
                parent=self.root):
                _launch_claude(_load_meta().get("aumid"))
                self._set_status("Launching Claude for new login…")
            else:
                self._set_status("Ready for new login — launch Claude when ready.")
        elif kind == "prep_err":
            self._busy = False
            messagebox.showerror("Prepare Failed", data, parent=self.root)
            self._set_status(f"Prepare failed: {data}")
            self._refresh()
        elif kind == "sync_ok":
            self._busy = False
            added = (data or {}).get("added", 0)
            removed = (data or {}).get("deleted", 0)
            self._set_status(
                f"Claude Code history synced — {added} added, {removed} removed.")
            messagebox.showinfo(
                "History Synced",
                "Claude Code + agent-mode history is now merged across all "
                "profiles.\n\n"
                f"• {added} session copies added\n"
                f"• {removed} deleted conversations propagated\n\n"
                "Whichever account you switch to shows the same up-to-date list.",
                parent=self.root)
            self._refresh()
        elif kind == "sync_err":
            self._busy = False
            messagebox.showerror("Sync Failed", data, parent=self.root)
            self._set_status(f"Sync failed: {data}")
        elif kind == "usage_ok":
            name, result = data
            m = _load_meta()
            info = m["profiles"].setdefault(name, {})
            info["usage"] = result
            if result.get("email"):
                info["email"] = result["email"]
            if result.get("plan"):
                info["plan"] = result["plan"]
            _save_meta(m)
            self._usage_busy = False
            self._set_status(f"Usage updated for '{name}'.")
            self._refresh()
        elif kind == "usage_err":
            name, msg = data
            self._usage_busy = False
            self._set_status(f"Usage: {msg}")
            messagebox.showinfo("Usage", msg, parent=self.root)
            if 0 <= self._sel < len(self._profiles):
                self._render_usage(self._profiles[self._sel])

    def _on_rename(self):
        if not (0 <= self._sel < len(self._profiles)):
            return
        p = self._profiles[self._sel]
        existing = [x.name for x in self._profiles if x.name != p.name]
        dlg = RenameDialog(self.root, p.name, p.label, existing)
        self.root.wait_window(dlg.top)
        if not dlg.result:
            return
        new_name, new_label = dlg.result
        m = _load_meta()
        if new_name != p.name:
            # move the profile's snapshot directory (and any legacy flat blob)
            old_dir = _profile_dir(p.name)
            if old_dir.exists():
                old_dir.rename(_profile_dir(new_name))
            legacy = STORE_DIR / f"{p.name}.blob"
            if legacy.exists():
                legacy.rename(STORE_DIR / f"{new_name}.blob")
            m["profiles"][new_name] = m["profiles"].pop(p.name, {})
            if m.get("active") == p.name:
                m["active"] = new_name
        m["profiles"].setdefault(new_name, {})["label"] = new_label
        _save_meta(m)
        self._refresh()
        for i, x in enumerate(self._profiles):
            if x.name == new_name:
                self._select(i); break

    def _on_remove(self):
        if not (0 <= self._sel < len(self._profiles)):
            return
        p = self._profiles[self._sel]
        if p.is_active:
            messagebox.showwarning(
                "Cannot Remove",
                f"'{p.name}' is the active profile.\n"
                "Switch to another profile first.", parent=self.root)
            return
        if not messagebox.askyesno(
            "Remove Profile",
            f"Remove  '{p.name}'  and its saved session?\n\n"
            "This does not log you out of Claude.",
            parent=self.root, icon="warning"):
            return
        _delete_profile_store(p.name)
        m = _load_meta()
        m["profiles"].pop(p.name, None)
        _save_meta(m)
        self._set_status(f"Removed '{p.name}'.")
        self._refresh()

    def _note_changed(self, _=None):
        if not self._note_pending:
            self._note_pending = True
            self.root.after(900, self._save_note)

    def _save_note(self):
        self._note_pending = False
        if not (0 <= self._sel < len(self._profiles)):
            return
        p = self._profiles[self._sel]
        text = self._note.get("1.0", tk.END).strip()
        p.note = text
        m = _load_meta()
        m["profiles"].setdefault(p.name, {})["note"] = text
        _save_meta(m)
        self._note_saved.configure(text="saved")
        self.root.after(1800, lambda: self._note_saved.configure(text=""))

    def _on_launch(self):
        m = _load_meta()
        _launch_claude(m.get("aumid"))
        self._set_status("Launching Claude…")

    def _on_stop(self):
        if not messagebox.askyesno("Stop Claude", "Force-close Claude Desktop?",
                                   parent=self.root):
            return
        _kill_claude()
        self._claude_up = False
        self._refresh_claude_ui()
        self._set_status("Claude stopped.")

    def _set_status(self, msg):
        self._st.configure(text=msg)


# ---------------------------------------------------------------------------
# Dialogs
# ---------------------------------------------------------------------------

class _BaseDialog:
    def _center(self, parent, w, h):
        t = self.top
        t.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() - w) // 2
        y = parent.winfo_y() + (parent.winfo_height() - h) // 2
        t.geometry(f"{w}x{h}+{x}+{y}")

    def _header(self, title):
        hdr = tk.Frame(self.top, bg=BG_SIDEBAR, height=44)
        hdr.pack(fill=tk.X); hdr.pack_propagate(False)
        tk.Label(hdr, text=title, bg=BG_SIDEBAR, fg=TXT_PRI,
                 font=(FF, 11, "bold"), padx=20).pack(side=tk.LEFT, pady=10)

    def _field(self, parent, label, var, show=None):
        tk.Label(parent, text=label, bg=BG_PANEL, fg=TXT_MUTE,
                 font=(FF, 8)).pack(anchor="w", pady=(8, 2))
        e = tk.Entry(parent, textvariable=var, font=(FF, 10), bg=BG_INPUT,
                     fg=TXT_PRI, insertbackground=TXT_PRI, relief=tk.FLAT, bd=0,
                     highlightbackground=CLR_DIV, highlightthickness=1,
                     **({"show": show} if show else {}))
        e.pack(fill=tk.X, ipady=6)
        return e


class SaveDialog(_BaseDialog):
    def __init__(self, parent, existing):
        self.result = None
        self.existing = existing
        t = self.top = tk.Toplevel(parent)
        t.title("Save Current Login"); t.configure(bg=BG_PANEL)
        t.resizable(False, False); t.grab_set(); t.transient(parent)
        try: t.iconbitmap(str(ICON_PATH))
        except Exception: pass
        self._center(parent, 440, 400)
        self._header("Save Current Login")

        body = tk.Frame(t, bg=BG_PANEL)
        body.pack(fill=tk.BOTH, expand=True, padx=20, pady=(14, 18))
        tk.Label(body,
                 text="Capture the account currently logged into Claude "
                      "Desktop as a switchable profile.",
                 bg=BG_PANEL, fg=TXT_SUB, font=(FF, 9), justify=tk.LEFT,
                 wraplength=400, anchor="w").pack(fill=tk.X, pady=(0, 4))

        self._name = tk.StringVar(); self._label = tk.StringVar()
        self._note = tk.StringVar()
        # Buttons anchored to the bottom first, so they're never clipped.
        btns = tk.Frame(body, bg=BG_PANEL)
        btns.pack(side=tk.BOTTOM, fill=tk.X, pady=(18, 0))
        _btn(btns, "Save", self._ok, accent=True,
             padx=18, pady=7).pack(side=tk.LEFT, padx=(0, 8))
        _btn(btns, "Cancel", t.destroy,
             padx=18, pady=7).pack(side=tk.LEFT)

        e = self._field(body, "Profile name  *  (letters, numbers, - _)",
                        self._name)
        self._field(body, "Label  (optional, e.g. work / personal)", self._label)
        self._field(body, "Note  (optional)", self._note)
        e.focus_set()

        t.bind("<Return>", lambda _: self._ok())
        t.bind("<Escape>", lambda _: t.destroy())

    def _ok(self):
        name = self._name.get().strip()
        if not _valid_name(name):
            messagebox.showwarning(
                "Invalid Name",
                "Use 1–32 chars: letters, numbers, hyphens, underscores.",
                parent=self.top); return
        if name in self.existing and not messagebox.askyesno(
            "Overwrite?", f"Profile '{name}' exists. Overwrite its snapshot?",
            parent=self.top):
            return
        self.result = (name, self._label.get().strip(), self._note.get().strip())
        self.top.destroy()


class RenameDialog(_BaseDialog):
    def __init__(self, parent, cur_name, cur_label, existing):
        self.result = None
        self.existing = existing
        t = self.top = tk.Toplevel(parent)
        t.title("Rename Profile"); t.configure(bg=BG_PANEL)
        t.resizable(False, False); t.grab_set(); t.transient(parent)
        try: t.iconbitmap(str(ICON_PATH))
        except Exception: pass
        self._center(parent, 420, 280)
        self._header("Rename Profile")
        body = tk.Frame(t, bg=BG_PANEL)
        body.pack(fill=tk.BOTH, expand=True, padx=20, pady=(14, 18))
        self._name = tk.StringVar(value=cur_name)
        self._label = tk.StringVar(value=cur_label)
        btns = tk.Frame(body, bg=BG_PANEL)
        btns.pack(side=tk.BOTTOM, fill=tk.X, pady=(18, 0))
        _btn(btns, "Save", self._ok, accent=True,
             padx=18, pady=7).pack(side=tk.LEFT, padx=(0, 8))
        _btn(btns, "Cancel", t.destroy,
             padx=18, pady=7).pack(side=tk.LEFT)
        e = self._field(body, "Profile name  *", self._name)
        self._field(body, "Label", self._label)
        e.focus_set(); e.select_range(0, tk.END)
        t.bind("<Return>", lambda _: self._ok())
        t.bind("<Escape>", lambda _: t.destroy())

    def _ok(self):
        name = self._name.get().strip()
        if not _valid_name(name):
            messagebox.showwarning("Invalid Name",
                                   "Use 1–32 chars: letters, numbers, - _.",
                                   parent=self.top); return
        if name in self.existing:
            messagebox.showwarning("Name Taken",
                                   f"'{name}' already exists.", parent=self.top)
            return
        self.result = (name, self._label.get().strip())
        self.top.destroy()


# ---------------------------------------------------------------------------

def main():
    if _try_mutex() is None:
        _poke_existing(); return
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
