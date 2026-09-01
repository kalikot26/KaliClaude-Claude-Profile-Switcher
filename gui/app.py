"""KaliClaude — isolated Claude Desktop profile switching.

The backend owns profile roots, managed launches, migration, and process safety.
The optional usage action reads only the selected active root; inactive profile
cookies are never enumerated or replayed.
"""
from __future__ import annotations

import base64
import gzip
import json
import os
import queue
import socket
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional

import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

try:
    from .desktop_backend import (
        DesktopBackend,
        ProfileState,
        RollbackError,
        SwitchResult,
    )
except ImportError:  # PyInstaller runs gui/app.py as the entry script.
    from desktop_backend import (  # type: ignore
        DesktopBackend,
        ProfileState,
        RollbackError,
        SwitchResult,
    )

try:
    from .cli_backend import CliBackend
except ImportError:  # PyInstaller runs gui/app.py as the entry script.
    from cli_backend import CliBackend  # type: ignore

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
META_FILE  = CACHE_DIR / "meta.json"

MUTEX_NAME = "Local\\KaliClaudeProfileSwitcherV2"
IPC_HOST   = "127.0.0.1"
IPC_PORT   = 47323

# ---------------------------------------------------------------------------
# JSON / meta helpers
# ---------------------------------------------------------------------------

def _load_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        return {}

def _load_meta() -> dict:
    m = _load_json(META_FILE)
    m.setdefault("profiles", {})
    return m

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


def _os_crypt_key(user_data_dir: Path) -> bytes:
    ls = json.loads((user_data_dir / "Local State").read_text(encoding="utf-8"))
    ek = base64.b64decode(ls["os_crypt"]["encrypted_key"])
    if ek[:5] != b"DPAPI":
        raise ValueError("unexpected os_crypt key prefix")
    return _dpapi_unprotect(ek[5:])


_DESKTOP_BACKEND: Optional[DesktopBackend] = None


def _desktop_backend() -> DesktopBackend:
    global _DESKTOP_BACKEND
    if _DESKTOP_BACKEND is None:
        _DESKTOP_BACKEND = DesktopBackend()
    return _DESKTOP_BACKEND


_CLI_BACKEND: Optional[CliBackend] = None


def _cli_backend() -> CliBackend:
    global _CLI_BACKEND
    if _CLI_BACKEND is None:
        _CLI_BACKEND = CliBackend()
    return _CLI_BACKEND


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


def _cookies_from_db(db: Path, user_data_dir: Path) -> Optional[str]:
    """Decrypt claude.ai cookies from ONE Cookies SQLite file into a Cookie header.
    Returns None if the file is missing / locked / has no sessionKey."""
    if not db or not db.exists():
        return None
    try:
        key = _os_crypt_key(user_data_dir)
        con = sqlite3.connect(f"file:{db.as_posix()}?immutable=1", uri=True)
    except Exception:
        return None    # exclusively locked (the live file while Claude runs) or unreadable
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


def _claude_cookie_header(user_data_dir: Path) -> Optional[str]:
    """claude.ai cookies as a Cookie header — the LIVE session ONLY.

    SAFETY (do not "improve" this): never fall back to a profile SNAPSHOT's cookies.
    An inactive root can hold a SUPERSEDED sessionKey (claude.ai rotates it), and sending a
    rotated-away session token is indistinguishable from a stolen-session replay — the
    server's response is to REVOKE the account's sessions. That killed live sessions.
    Only the current live cookie is safe to send, and it is only readable while Claude
    Desktop is closed (it holds an exclusive lock on the file)."""
    if not _CRYPTO_OK:
        return None
    return _cookies_from_db(user_data_dir / "Network" / "Cookies", user_data_dir)


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


def _usage_via_cookies(user_data_dir: Path) -> dict:
    """Read usage via the live claude.ai session cookie — no OAuth-token call."""
    cookie = _claude_cookie_header(user_data_dir)
    if not cookie:
        return {"_error":
                "Can't read the live session while Claude Desktop is running — it "
                "holds an exclusive lock on the cookie file.\n\n"
                "Click 'Stop Claude', then Refresh Usage, then Launch Claude again.\n\n"
                "(We deliberately never fall back to an inactive profile cookie: that "
                "key may be superseded, and replaying it makes the server revoke your "
                "session.)"}
    orgs = _claude_web_get("/api/organizations", cookie)
    if "json" not in orgs:
        return orgs if ("_http_error" in orgs or "_error" in orgs) \
            else {"_error": "organizations: unexpected response"}
    org_list = orgs["json"] if isinstance(orgs["json"], list) else []
    if not org_list:
        return {"_error": "No organizations for this session."}
    want = None
    try:
        want = json.loads((user_data_dir / "config.json").read_text("utf-8")).get("lastKnownAccountUuid")
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
                 "has_blob", "email", "plan", "usage", "state", "issue",
                 "cli_paired", "cli_email")

    def __init__(self, name, label, note, updated, fp, is_active, has_blob,
                 email="", plan="", usage=None, state=ProfileState.READY,
                 issue="", cli_paired=False, cli_email=""):
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
        self.state = state
        self.issue = issue
        self.cli_paired = cli_paired
        self.cli_email = cli_email


PROFILE_STATE_LABELS = {
    ProfileState.ACTIVE: "Active",
    ProfileState.READY: "Ready",
    ProfileState.NEEDS_VALIDATION: "Needs validation",
    ProfileState.NEEDS_RELOGIN: "Needs re-login",
    ProfileState.CORRUPT: "Corrupt",
    ProfileState.UNKNOWN_LIVE_LOGIN: "Unknown live login",
}

def _list_profiles() -> list[Profile]:
    m = _load_meta()
    out: list[Profile] = []
    for saved in _desktop_backend().list_profiles():
        name = saved.name
        info = m.get("profiles", {}).get(name, {})
        ready = saved.state in (
            ProfileState.ACTIVE,
            ProfileState.READY,
            ProfileState.NEEDS_VALIDATION,
        )
        profile = Profile(
            name=name,
            label=saved.label,
            note=saved.note,
            updated=saved.updated,
            fp=info.get("fp", saved.account_id_hash[:12]),
            is_active=(saved.state == ProfileState.ACTIVE),
            has_blob=ready,
            email=info.get("email", ""),
            plan=info.get("plan", ""),
            usage=info.get("usage") or {},
            state=saved.state,
            issue=saved.issue,
        )
        try:  # bare guard: the CLI layer can never break profile listing
            cli_info = _cli_backend().pair_info(name)
            profile.cli_paired = cli_info.paired
            profile.cli_email = cli_info.email
        except Exception:
            pass
        out.append(profile)
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
        self._claude_detection_error = False
        self._live_present = False
        self._note_pending = False
        self._busy = False
        self._usage_busy = False
        self._startup_error = ""
        self._migration_report = None
        self._email_cache: dict[str, str] = {}
        self._pool_dialog = None

        try:
            self._migration_report = _desktop_backend().audit_and_migrate()
        except Exception as error:
            self._startup_error = str(error) or type(error).__name__

        self._build()
        _start_ipc(lambda: self.root.after(0, self._focus))
        self.root.after(200, self._refresh)
        self.root.after(1500, self._tick)
        self.root.after(150, self._pump)
        self.root.after(350, self._show_startup_audit)

    def _show_startup_audit(self):
        if self._startup_error:
            messagebox.showerror(
                "Profile Audit Failed",
                "KaliClaude could not safely audit the saved profiles. Switching "
                f"is unavailable until this is fixed.\n\n{self._startup_error}",
                parent=self.root,
            )
            return
        report = self._migration_report
        blocked = list(report.needs_relogin) + list(report.corrupt) if report else []
        if blocked:
            backup = f"\n\nRecovery backup:\n{report.backup.path}" if report.backup else ""
            messagebox.showwarning(
                "Profiles Need Attention",
                "The profile audit preserved but blocked these profiles:\n\n"
                + "\n".join(f"• {name}" for name in blocked)
                + "\n\nPrepare and save a fresh isolated login for each blocked profile."
                + backup,
                parent=self.root,
            )

    def _action_ready(self, *, require_selection: bool = False) -> bool:
        if self._startup_error:
            messagebox.showerror(
                "Profile Audit Failed",
                "Profile actions remain disabled because startup audit failed.\n\n"
                + self._startup_error,
                parent=self.root,
            )
            return False
        if self._busy or self._usage_busy:
            return False
        return not require_selection or 0 <= self._sel < len(self._profiles)

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
        self._btn_save = _btn(right, "Save Current Login", self._on_save_current,
                              accent=True)
        self._btn_save.pack(side=tk.RIGHT, padx=(6, 0))
        self._btn_prepare = _btn(right, "Prepare New Login", self._on_prepare_login,
                                 bg=BG_CARD)
        self._btn_prepare.pack(side=tk.RIGHT, padx=(6, 0))
        self._btn_sync = _btn(right, "Sync History", self._on_sync_histories,
                              bg=BG_CARD)
        self._btn_sync.pack(side=tk.RIGHT)

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
                 "to finalize it as an isolated profile.",
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
        self._saved_row = self._meta_row(meta, "Login state")
        self._cli_row   = self._meta_row(meta, "CLI login")

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
        self._btn_update = _btn(act, "Verify Login", self._on_update)
        self._btn_update.pack(side=tk.LEFT, padx=(0, 8))
        self._btn_rename = _btn(act, "Rename", self._on_rename)
        self._btn_rename.pack(side=tk.LEFT, padx=(0, 8))
        self._btn_remove = _btn(act, "Remove", self._on_remove, danger=True)
        self._btn_remove.pack(side=tk.LEFT)
        self._btn_pair_cli = _btn(act, "Pair CLI", self._on_pair_cli)
        self._btn_pair_cli.pack(side=tk.LEFT, padx=(8, 0))
        self._btn_unpair_cli = _btn(act, "Unpair CLI", self._on_unpair_cli)
        self._btn_unpair_cli.pack(side=tk.LEFT, padx=(8, 0))
        # Always enabled: the pool is independent of the selected profile.
        self._btn_cli_pool = _btn(act, "CLI Pool…", self._on_cli_pool)
        self._btn_cli_pool.pack(side=tk.LEFT, padx=(8, 0))

        hint = tk.Label(
            self._content,
            text="“Verify Login” checks this profile's persistent isolated root; "
                 "no session snapshot is copied or replaced.",
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
                     text="No saved profiles yet\n\nPrepare a new login, sign in, then\n"
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
        if p.state == ProfileState.ACTIVE:
            meta, meta_color = "Active", CLR_ACTIVE
        elif p.state == ProfileState.READY:
            meta, meta_color = f"Ready · saved {_rel_time(p.updated)}", TXT_MUTE
        else:
            meta, meta_color = PROFILE_STATE_LABELS[p.state], CLR_WARN
        if p.cli_paired:
            meta += "  ·  CLI"
        tk.Label(inner, text=meta, bg=cbg,
                 fg=meta_color,
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
        snapshot_text = PROFILE_STATE_LABELS[p.state]
        if p.has_blob:
            snapshot_text += f" · {_rel_time(p.updated)}"
        elif p.issue:
            snapshot_text += " · validation failed"
        self._saved_row.configure(
            text=snapshot_text,
            fg=TXT_SUB if p.has_blob else CLR_WARN)
        if p.cli_paired:
            self._cli_row.configure(text=p.cli_email or "paired", fg=CLR_OK)
        else:
            self._cli_row.configure(text="not paired", fg=TXT_MUTE)
        self._note.configure(state=tk.NORMAL)
        self._note.delete("1.0", tk.END); self._note.insert("1.0", p.note)
        self._note.configure(state=tk.DISABLED)
        self._note_saved.configure(text="")
        self._render_usage(p)

        actions_enabled = not self._startup_error
        if p.is_active:
            self._btn_switch.configure(text="Active Now", state=tk.DISABLED,
                                       bg=BG_CARD, fg=TXT_MUTE,
                                       activebackground=BG_CARD)
        elif not p.has_blob:
            self._btn_switch.configure(text="Re-login / Re-save", state=tk.NORMAL,
                                       bg=CLR_ACCENT, fg="#17140F",
                                       activebackground="#BF6330")
        else:
            self._btn_switch.configure(text="Switch to this Profile",
                                       state=tk.NORMAL, bg=CLR_ACCENT,
                                       fg="#17140F", activebackground="#BF6330")
        if not actions_enabled:
            self._btn_switch.configure(state=tk.DISABLED, bg=BG_CARD, fg=TXT_MUTE)
        self._btn_update.configure(
            state=tk.NORMAL if actions_enabled else tk.DISABLED,
            bg=BG_CARD,
            fg=TXT_PRI if actions_enabled else TXT_MUTE)
        self._btn_rename.configure(
            state=tk.NORMAL if actions_enabled else tk.DISABLED,
            fg=TXT_PRI if actions_enabled else TXT_MUTE)
        self._btn_remove.configure(
            state=tk.NORMAL if actions_enabled else tk.DISABLED,
            fg=CLR_ERR if actions_enabled else TXT_MUTE)
        cli_enabled = actions_enabled and p.is_active
        self._btn_pair_cli.configure(
            state=tk.NORMAL if cli_enabled else tk.DISABLED,
            fg=TXT_PRI if cli_enabled else TXT_MUTE)
        unpair_enabled = actions_enabled and p.cli_paired
        self._btn_unpair_cli.configure(
            state=tk.NORMAL if unpair_enabled else tk.DISABLED,
            fg=TXT_PRI if unpair_enabled else TXT_MUTE)
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
        if self._startup_error:
            self._claude_lbl.configure(text="⚠ Profile audit failed", fg=CLR_ERR)
            self._btn_launch.configure(state=tk.DISABLED, bg=BG_CARD, fg=TXT_MUTE)
            self._btn_stop.configure(state=tk.DISABLED, bg=BG_CARD, fg=TXT_MUTE)
        elif self._claude_detection_error:
            self._claude_lbl.configure(text="⚠ Claude status unavailable", fg=CLR_WARN)
            self._btn_launch.configure(state=tk.DISABLED, bg=BG_CARD, fg=TXT_MUTE)
            self._btn_stop.configure(state=tk.DISABLED, bg=BG_CARD, fg=TXT_MUTE)
        elif self._claude_up:
            self._claude_lbl.configure(text="● Claude is running", fg=CLR_OK)
            self._btn_launch.configure(state=tk.DISABLED, bg=BG_CARD, fg=TXT_MUTE)
            self._btn_stop.configure(state=tk.NORMAL, bg="#3D1F1F", fg=CLR_ERR)
        else:
            self._claude_lbl.configure(text="○ Claude is not running", fg=TXT_MUTE)
            self._btn_launch.configure(state=tk.NORMAL, bg=BG_CARD, fg=TXT_PRI)
            self._btn_stop.configure(state=tk.DISABLED, bg=BG_CARD, fg=TXT_MUTE)

    # ----- refresh / polling ------------------------------------------------

    def _refresh(self):
        if self._startup_error:
            self._profiles = []
            for button in (self._btn_save, self._btn_prepare, self._btn_sync):
                button.configure(state=tk.DISABLED)
            self._render_list()
            self._show_detail(None)
            self._set_status("Profile audit failed — actions disabled")
            return
        for button in (self._btn_save, self._btn_prepare, self._btn_sync):
            button.configure(state=tk.NORMAL)
        self._profiles = _list_profiles()
        for profile in self._profiles:
            profile.email = self._email_cache.get(profile.name, profile.email)
        if self._sel >= len(self._profiles):
            self._sel = len(self._profiles) - 1
        self._render_list()
        self._show_detail(self._profiles[self._sel]
                          if 0 <= self._sel < len(self._profiles) else None)
        active = next((p.name for p in self._profiles if p.is_active), None)
        n = len(self._profiles)
        live_status = f"  ·  Active: {active}" if active else ""
        self._set_status(f"{n} profile{'s' if n != 1 else ''}" + live_status)

    def _tick(self):
        def check():
            try:
                up = _desktop_backend().desktop_running()
            except Exception:
                self._q.put(("tick", (False, False, True)))
                return
            try:
                root = _desktop_backend().active_user_data_dir()
                live = bool(_load_json(root / "config.json").get("lastKnownAccountUuid"))
            except Exception:
                live = False
            self._q.put(("tick", (up, live)))
        threading.Thread(target=check, daemon=True).start()

    def _apply_tick(self, up, live, detection_error=False):
        self._claude_up = up
        self._claude_detection_error = detection_error
        self._live_present = live
        self._live_lbl.configure(
            text="● session detected" if live else "○ not logged in",
            fg=CLR_OK if live else TXT_MUTE)
        if self._content.winfo_ismapped():
            self._refresh_claude_ui()
            if 0 <= self._sel < len(self._profiles):
                self._btn_update.configure(
                    state=tk.NORMAL if not self._startup_error else tk.DISABLED,
                    fg=TXT_PRI if not self._startup_error else TXT_MUTE)

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
        if not self._action_ready(require_selection=True):
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
                active_root = _desktop_backend().active_user_data_dir()
                u = _usage_via_cookies(active_root)
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
        if not self._action_ready():
            return
        existing = [p.name for p in self._profiles]
        dlg = SaveDialog(self.root, existing)
        self.root.wait_window(dlg.top)
        if not dlg.result:
            return
        name, label, note = dlg.result
        if not messagebox.askyesno(
            "Save Current Login",
            f"Finalize the pending isolated login as profile '{name}'?\n\n"
            "The pending window stays running. The original Claude window is not "
            "stopped or copied. No existing profile root is replaced.",
            parent=self.root):
            return

        self._busy = True
        self._set_status(f"Saving '{name}'…")

        def work():
            try:
                _desktop_backend().finalize_current(name, label, note)
                self._q.put(("save_ok", name))
            except Exception as e:
                self._q.put(("save_err", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def _on_update(self):
        if not self._action_ready(require_selection=True):
            return
        p = self._profiles[self._sel]
        if not messagebox.askyesno(
            "Verify Login",
            f"Verify the persistent isolated login for profile '{p.name}'?\n\n"
            "This checks the stored account identity without copying session data.",
            parent=self.root):
            return
        name = p.name
        self._busy = True
        self._set_status(f"Updating '{name}'…")

        def work():
            try:
                profile = _desktop_backend().verify_profile(name)
                if profile.state == ProfileState.CORRUPT:
                    self._q.put(("verify_err", profile.issue or "Profile verification failed"))
                    return
                self._q.put(("verify_ok", name))
            except Exception as e:
                self._q.put(("verify_err", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def _on_switch(self):
        if not self._action_ready(require_selection=True):
            return
        p = self._profiles[self._sel]
        if p.is_active:
            return
        if not p.has_blob:
            self._on_relogin_profile(p)
            return
        msg = f"Switch Claude to profile  '{p.name}'?"
        msg += (
            "\n\nThis opens or focuses a second Claude window using only the selected "
            "isolated profile root. The default Desktop root is not used."
            "\n\nClose Claude Code CLI sessions first for a clean account swap."
        )
        if not messagebox.askyesno("Switch Profile", msg, parent=self.root):
            return

        self._busy = True
        self._btn_switch.configure(state=tk.DISABLED, text="Switching…",
                                   bg=BG_CARD, fg=TXT_MUTE)
        self._set_status(f"Switching to {p.name}…")
        target = p.name
        outgoing = next((q.name for q in self._profiles if q.is_active), "")

        def work():
            try:
                result = _desktop_backend().switch(target)
                self._q.put(("switch_ok", result))
                # CLI partner swap: strictly after the desktop switch, fully
                # guarded — an exception here must never touch the desktop result.
                try:
                    self._q.put(("cli_switch", _cli_backend().switch_to(target, outgoing)))
                except Exception as cli_error:
                    self._q.put(("cli_switch", str(cli_error) or type(cli_error).__name__))
            except RollbackError as error:
                self._q.put(("rollback_err", (str(error), str(error.recovery_path))))
            except Exception as e:
                self._q.put(("switch_err", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def _on_relogin_profile(self, profile: Profile):
        reason = f"\n\nReason: {profile.issue}" if profile.issue else ""
        if messagebox.askyesno(
            "Re-login Required",
            f"'{profile.name}' cannot be selected because its saved login is "
            f"incomplete or invalid.{reason}\n\nPrepare a fresh Claude login now? After "
            "signing in, save the pending login as a new profile.",
            parent=self.root,
        ):
            self._on_prepare_login()

    def _on_prepare_login(self):
        """Create and managed-launch a fresh isolated Desktop root."""
        if not self._action_ready():
            return

        if not messagebox.askyesno(
            "Prepare New Login",
            "Create a fresh isolated Claude Desktop root and open it for a new "
            "login?\n\nThe original Claude window stays running. A second window "
            "opens on a new isolated root. After signing in there, click "
            "'Save Current Login'.",
            parent=self.root):
            return

        self._busy = True
        self._set_status("Preparing new login…")

        def work():
            pending = None
            try:
                backend = _desktop_backend()
                pending = backend.begin_new_login()
                try:
                    launch = backend.launch_active()
                    if not launch.ok:
                        raise RuntimeError(launch.message or "Pending login launch failed")
                    if launch.user_data_dir != pending.user_data_dir:
                        raise RuntimeError("Managed launch did not use the pending login root")
                except Exception as launch_error:
                    try:
                        backend.discard_pending_login(pending.name)
                    except Exception as cleanup_error:
                        raise RuntimeError(
                            f"{str(launch_error) or type(launch_error).__name__}; "
                            "pending-login cleanup also failed: "
                            f"{str(cleanup_error) or type(cleanup_error).__name__}"
                        ) from launch_error
                    raise
                self._q.put(("prep_ok", (pending, launch)))
            except Exception as e:
                self._q.put(("prep_err", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def _on_sync_histories(self):
        """Sync local history only between roots carrying the same account ID."""
        if not self._action_ready():
            return
        if not messagebox.askyesno(
            "Sync Claude Code History",
            "Sync Claude Code + agent-mode sessions only between isolated roots "
            "with the same account ID. Different accounts never share cards.\n\n"
            "• Newest copy wins within one account; user-deleted conversations stay deleted.\n"
            "• A local history backup is made before deletions propagate.\n"
            "• The default Desktop root is never included.\n"
            "• Conversation JSONL files are not rewritten.\n\n"
            "Continue?", parent=self.root):
            return
        self._busy = True
        self._set_status("Syncing Claude Code history…")

        def work():
            try:
                report = _desktop_backend().sync_histories()
                if not report.ok:
                    raise RuntimeError(report.message or "History sync failed")
                self._q.put(("sync_ok", {
                    "added": report.added,
                    "deleted": report.removed,
                }))
            except Exception as e:
                self._q.put(("sync_err", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def _on_pair_cli(self):
        if not self._action_ready(require_selection=True):
            return
        p = self._profiles[self._sel]
        if not p.is_active:
            messagebox.showinfo(
                "Pair CLI",
                "Pair the Claude Code CLI while this profile is the active one — "
                "pairing captures or creates the CLI login that belongs to it.",
                parent=self.root)
            return
        self._start_cli_pair(p.name)

    def _start_cli_pair(self, name):
        if not messagebox.askyesno(
            "Pair Claude Code CLI",
            f"Pair the Claude Code CLI login with profile '{name}'?\n\n"
            "A terminal window opens for you to log in. If no login prompt "
            "appears, type /login. Closing the terminal cancels pairing.",
            parent=self.root):
            return
        expected = _load_meta().get("profiles", {}).get(name, {}).get(
            "account_id_sha256", "")
        self._busy = True
        self._set_status(
            f"Pairing CLI for '{name}' — a terminal opened; close it to cancel.")

        def work():
            try:
                self._q.put(("cli_pair_ok", _cli_backend().pair(name, expected)))
            except Exception as error:
                self._q.put(("cli_pair_err", str(error) or type(error).__name__))

        threading.Thread(target=work, daemon=True).start()

    def _on_unpair_cli(self):
        if not self._action_ready(require_selection=True):
            return
        p = self._profiles[self._sel]
        if not p.cli_paired:
            return
        active = p.is_active
        detail = ("The saved CLI login is parked recoverably under "
                  "cli-data\\_unclaimed-* — you can re-pair anytime with 'Pair CLI'.")
        if active:
            detail += ("\n\nThis profile is active, so the live Claude Code CLI "
                       "signs out immediately.")
        if not messagebox.askyesno(
            "Unpair Claude Code CLI",
            f"Detach the Claude Code CLI login from profile '{p.name}'?\n\n" + detail,
            parent=self.root):
            return
        name = p.name
        self._busy = True
        self._set_status(f"Unpairing CLI for '{name}'…")

        def work():
            try:
                self._q.put(
                    ("cli_unpair_ok", _cli_backend().unpair(name, sign_out_live=active)))
            except Exception as error:
                self._q.put(("cli_unpair_err", str(error) or type(error).__name__))

        threading.Thread(target=work, daemon=True).start()

    def _on_cli_pool(self):
        # Guarded so a pool-layer fault can never break the main window.
        try:
            dialog = getattr(self, "_pool_dialog", None)
            if dialog is not None:
                try:
                    if dialog.top.winfo_exists():
                        dialog.top.lift()
                        return
                except Exception:
                    pass
            self._pool_dialog = CliPoolDialog(self)
        except Exception as error:
            messagebox.showwarning(
                "CLI Pool", str(error) or type(error).__name__, parent=self.root)

    def _start_pool_add(self, name):
        """Add a pool account on a worker thread (a login terminal opens)."""
        self._busy = True
        self._set_status(
            f"Adding pool account '{name}' — a terminal opened; close it to cancel.")

        def work():
            try:
                self._q.put(("pool_add_ok", _cli_backend().pool_add(name)))
            except Exception as error:
                self._q.put(("pool_add_err", str(error) or type(error).__name__))

        threading.Thread(target=work, daemon=True).start()

    def _reload_pool_dialog(self):
        dialog = getattr(self, "_pool_dialog", None)
        if dialog is None:
            return
        try:
            if dialog.top.winfo_exists():
                dialog._reload()
        except Exception:
            pass

    def _pump(self):
        """Single persistent dispatcher for background-thread results."""
        try:
            while True:
                kind, data = self._q.get_nowait()
                try:
                    self._handle_result(kind, data)
                except Exception as error:
                    try:
                        self._set_status(
                            f"Background update failed: {str(error) or type(error).__name__}"
                        )
                    except Exception:
                        pass
        except queue.Empty:
            pass
        finally:
            try:
                self.root.after(150, self._pump)
            except tk.TclError:
                pass

    def _handle_result(self, kind, data):
        if kind == "switch_ok":
            self._busy = False
            result: SwitchResult = data
            target = result.target_name
            if not result.ok:
                messagebox.showerror(
                    "Switch Failed",
                    result.message or "The profile switch could not be verified.",
                    parent=self.root,
                )
                self._set_status(f"Switch failed: {result.message}")
                self._refresh()
                return
            self._set_status(f"Switched to '{target}'.")
            self._refresh()
            if result.history and not result.history.ok:
                messagebox.showwarning(
                    "Login Switched; History Not Synced",
                    "The Claude login switch succeeded and remains active, but "
                    f"history sync failed:\n\n{result.history.message}",
                    parent=self.root,
                )
            if result.should_relaunch:
                self._start_launch()
        elif kind == "rollback_err":
            self._busy = False
            message, recovery = data
            messagebox.showerror(
                "Switch Recovery Required",
                f"{message}\n\nDo not launch Claude Desktop. The verified recovery "
                f"backup is here:\n\n{recovery}",
                parent=self.root,
            )
            self._set_status(f"Recovery required — backup: {recovery}")
            self._refresh()
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
            if messagebox.askyesno(
                "Launch Claude", f"Saved '{data}'.\n\nReopen Claude now?",
                parent=self.root):
                self._start_launch()
        elif kind == "save_err":
            self._busy = False
            messagebox.showerror("Save Failed", data, parent=self.root)
            self._set_status(f"Save failed: {data}")
            self._refresh()
        elif kind == "verify_ok":
            self._busy = False
            self._set_status(f"Verified '{data}'.")
            self._refresh()
        elif kind == "verify_err":
            self._busy = False
            messagebox.showerror("Verify Failed", data, parent=self.root)
            self._set_status(f"Verify failed: {data}")
            self._refresh()
        elif kind == "rename_ok":
            self._busy = False
            old_name, new_name = data
            self._set_status(f"Renamed '{old_name}' to '{new_name}'.")
            self._sel = -1
            self._refresh()
            for i, p in enumerate(self._profiles):
                if p.name == new_name:
                    self._select(i)
                    break
        elif kind == "rename_err":
            self._busy = False
            messagebox.showerror("Rename Failed", data, parent=self.root)
            self._set_status(f"Rename failed: {data}")
            self._refresh()
        elif kind == "remove_ok":
            self._busy = False
            name, retained = data
            self._set_status(f"Removed '{name}'. Recovery copy retained.")
            self._sel = -1
            self._refresh()
            if retained:
                messagebox.showinfo(
                    "Profile Removed",
                    f"'{name}' was removed from KaliClaude.\n\n"
                    f"Recovery copy retained at:\n{retained}",
                    parent=self.root)
        elif kind == "remove_err":
            self._busy = False
            messagebox.showerror("Remove Failed", data, parent=self.root)
            self._set_status(f"Remove failed: {data}")
            self._refresh()
        elif kind == "prep_ok":
            self._busy = False
            self._refresh()
            pending, _launch = data
            messagebox.showinfo(
                "Ready for New Login",
                "Claude opened with the pending isolated root:\n\n"
                f"{pending.user_data_dir}\n\nSign in, then click 'Save Current Login'.",
                parent=self.root,
            )
            self._set_status(f"Pending login launched from {pending.user_data_dir}")
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
                "Claude Code + agent-mode history is synced only within the "
                "same account ID. Different profiles/accounts stay separate.\n\n"
                f"• {added} session copies added\n"
                f"• {removed} deleted conversations propagated\n\n"
                "No default-root or cross-account cards were copied.",
                parent=self.root)
            self._refresh()
        elif kind == "sync_err":
            self._busy = False
            messagebox.showerror("Sync Failed", data, parent=self.root)
            self._set_status(f"Sync failed: {data}")
        elif kind == "cli_switch":
            self._handle_cli_switch(data)
        elif kind == "cli_pair_ok":
            self._busy = False
            result = data
            if result.ok:
                extra = ("\n\n" + "\n".join(result.warnings)) if result.warnings else ""
                self._set_status(f"CLI paired for '{result.profile}'.")
                messagebox.showinfo(
                    "CLI Paired",
                    f"The Claude Code CLI is paired with '{result.profile}'." + extra,
                    parent=self.root)
            else:
                self._set_status(f"CLI pairing not completed: {result.message}")
                messagebox.showwarning(
                    "CLI Pairing Incomplete",
                    result.message or "Pairing did not complete.", parent=self.root)
            self._refresh()
        elif kind == "cli_pair_err":
            self._busy = False
            messagebox.showwarning("CLI Pairing Failed", data, parent=self.root)
            self._set_status(f"CLI pairing failed: {data}")
        elif kind == "cli_unpair_ok":
            self._busy = False
            result = data
            self._set_status(result.message)
            messagebox.showinfo(
                "CLI Unpaired",
                result.message + "\n\nRe-pair this profile anytime with 'Pair CLI'.",
                parent=self.root)
            self._refresh()
        elif kind == "cli_unpair_err":
            self._busy = False
            messagebox.showwarning("CLI Unpair Failed", data, parent=self.root)
            self._set_status(f"CLI unpair failed: {data}")
            self._refresh()
        elif kind == "pool_add_ok":
            self._busy = False
            result = data
            if result.ok:
                self._set_status(f"Added pool account '{result.name}'.")
                messagebox.showinfo(
                    "Pool Account Added",
                    f"Added '{result.name}'"
                    + (f" ({result.email})" if result.email else "")
                    + " to the CLI pool.",
                    parent=self.root)
            else:
                self._set_status(f"Pool add not completed: {result.message}")
                messagebox.showwarning(
                    "Pool Add Incomplete",
                    result.message or "The account was not added.", parent=self.root)
            self._reload_pool_dialog()
        elif kind == "pool_add_err":
            self._busy = False
            messagebox.showwarning("Pool Add Failed", data, parent=self.root)
            self._set_status(f"Pool add failed: {data}")
            self._reload_pool_dialog()
        elif kind == "tick":
            self._apply_tick(*data)
            self.root.after(5000, self._tick)
        elif kind == "launch_ok":
            self._busy = False
            self._claude_up = True
            self._refresh_claude_ui()
            self._set_status("Claude launched with the selected profile.")
        elif kind == "launch_err":
            self._busy = False
            messagebox.showerror("Launch Failed", data, parent=self.root)
            self._set_status(f"Launch failed: {data}")
        elif kind == "stop_ok":
            self._busy = False
            self._claude_up = False
            self._refresh_claude_ui()
            self._set_status("Claude stopped.")
        elif kind == "stop_err":
            self._busy = False
            messagebox.showerror("Stop Failed", data, parent=self.root)
            self._set_status(f"Stop failed: {data}")
        elif kind == "usage_ok":
            name, result = data
            cache_error = ""
            safe_usage = {
                key: result.get(key)
                for key in ("five_hour", "seven_day", "fetched")
                if result.get(key) is not None
            }
            try:
                _desktop_backend().update_profile_usage(
                    name, safe_usage, str(result.get("plan") or "")
                )
            except Exception as error:
                cache_error = str(error) or type(error).__name__
            email = result.get("email")
            if isinstance(email, str) and email:
                self._email_cache[name] = email
            for profile in self._profiles:
                if profile.name == name:
                    profile.usage = result
                    profile.email = result.get("email") or profile.email
                    profile.plan = result.get("plan") or profile.plan
                    break
            self._usage_busy = False
            if cache_error:
                self._set_status(f"Usage updated; cache save failed: {cache_error}")
            else:
                self._set_status(f"Usage updated for '{name}'.")
            if 0 <= self._sel < len(self._profiles):
                self._render_usage(self._profiles[self._sel])
        elif kind == "usage_err":
            name, msg = data
            self._usage_busy = False
            self._set_status(f"Usage: {msg}")
            messagebox.showinfo("Usage", msg, parent=self.root)
            if 0 <= self._sel < len(self._profiles):
                self._render_usage(self._profiles[self._sel])

    def _handle_cli_switch(self, data):
        """CLI partner result — warnings only; the desktop switch already stands."""
        if isinstance(data, str):
            self._set_status(f"Desktop switched; CLI account not swapped: {data}")
            messagebox.showwarning(
                "CLI Account Not Swapped",
                "The Desktop profile switch succeeded and remains active, but the "
                f"Claude Code CLI account could not be swapped:\n\n{data}",
                parent=self.root)
            return
        result = data
        if result.warnings:
            messagebox.showwarning(
                "CLI Account Warning",
                "The Desktop switch succeeded. Note about the CLI account:\n\n"
                + "\n".join(result.warnings),
                parent=self.root)
        if result.needs_login:
            if messagebox.askyesno(
                "CLI Sign-in Needed",
                f"'{result.target}' has no stored Claude Code CLI login, so the CLI "
                "is now signed out. Pair it now?",
                parent=self.root):
                self._start_cli_pair(result.target)
            else:
                self._set_status(f"Switched; CLI signed out for '{result.target}'.")
        else:
            self._set_status(
                f"Switched; CLI account now '{result.target}'. Any running CLI "
                "session bills this account from its next request.")
        self._refresh()

    def _on_rename(self):
        if not self._action_ready(require_selection=True):
            return
        old_name = self._profiles[self._sel].name
        new_name = simpledialog.askstring(
            "Rename Profile", "New profile name:", initialvalue=old_name, parent=self.root)
        if new_name is None or not new_name.strip():
            return
        new_name = new_name.strip()
        self._busy = True
        self._set_status(f"Renaming '{old_name}'…")

        def work():
            try:
                _desktop_backend().rename_profile(old_name, new_name)
                try:  # keep the CLI store attached to the renamed profile
                    _cli_backend().rename_store(old_name, new_name)
                except Exception:
                    pass
                self._q.put(("rename_ok", (old_name, new_name)))
            except Exception as error:
                self._q.put(("rename_err", str(error) or type(error).__name__))

        threading.Thread(target=work, daemon=True).start()

    def _on_remove(self):
        if not self._action_ready(require_selection=True):
            return
        name = self._profiles[self._sel].name
        if not messagebox.askyesno(
            "Remove Profile",
            f"Remove '{name}' from KaliClaude?\n\n"
            "Its isolated root will be moved to a recovery folder, not deleted. "
            "The active/running profile must be stopped first.",
            parent=self.root):
            return
        self._busy = True
        self._set_status(f"Removing '{name}'…")

        def work():
            try:
                retained = _desktop_backend().remove_profile(name)
                try:  # park stale CLI creds so a recycled name can't inherit them
                    _cli_backend().retire_store(name)
                except Exception:
                    pass
                self._q.put(("remove_ok", (name, str(retained) if retained else "")))
            except Exception as error:
                self._q.put(("remove_err", str(error) or type(error).__name__))

        threading.Thread(target=work, daemon=True).start()

    def _note_changed(self, _=None):
        return

    def _save_note(self):
        return

    def _start_launch(self):
        self._busy = True
        self._set_status("Launching Claude…")

        def work():
            try:
                launch = _desktop_backend().launch_active()
                if not launch.ok:
                    raise RuntimeError(launch.message or "Managed launch failed")
                self._q.put(("launch_ok", launch))
            except Exception as error:
                self._q.put(("launch_err", str(error) or type(error).__name__))

        threading.Thread(target=work, daemon=True).start()

    def _on_launch(self):
        if not self._action_ready():
            return
        self._start_launch()

    def _on_stop(self):
        if not self._action_ready():
            return
        if not messagebox.askyesno(
            "Stop Claude",
            "Close the selected profile window? The original Claude window is not stopped.",
                                   parent=self.root):
            return
        self._busy = True
        self._set_status("Stopping Claude…")

        def work():
            try:
                _desktop_backend().stop_desktop()
                self._q.put(("stop_ok", None))
            except Exception as error:
                self._q.put(("stop_err", str(error) or type(error).__name__))

        threading.Thread(target=work, daemon=True).start()

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
                 text="Finalize the pending isolated Claude Desktop login "
                      "as a switchable profile.",
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
        if name in self.existing:
            messagebox.showwarning(
                "Name Taken", f"Profile '{name}' already exists.", parent=self.top
            )
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


class CliPoolDialog(_BaseDialog):
    """Manage the CLI pool: several simultaneously-logged-in CLI accounts.

    Every cli_backend call is guarded so a pool-layer fault can only affect
    this dialog, never the main window. Add runs on the app's worker+queue;
    the file-local quick ops (list/retire/move/install) run inline.
    """

    def __init__(self, app):
        self.app = app
        parent = app.root
        t = self.top = tk.Toplevel(parent)
        t.title("CLI Pool"); t.configure(bg=BG_PANEL)
        t.resizable(False, False); t.grab_set(); t.transient(parent)
        try: t.iconbitmap(str(ICON_PATH))
        except Exception: pass
        self._center(parent, 540, 480)
        self._header("CLI Pool")

        body = tk.Frame(t, bg=BG_PANEL)
        body.pack(fill=tk.BOTH, expand=True, padx=20, pady=(14, 18))
        tk.Label(
            body,
            text="Each account keeps its own isolated CLI login "
                 "(CLAUDE_CONFIG_DIR), so they stay signed in side by side. "
                 "Install the launcher to run them in order with automatic "
                 "failover when one hits a usage limit.",
            bg=BG_PANEL, fg=TXT_SUB, font=(FF, 9), justify=tk.LEFT,
            wraplength=490, anchor="w").pack(fill=tk.X, pady=(0, 10))

        self._list = tk.Listbox(
            body, height=10, font=(FF, 10), bg=BG_INPUT, fg=TXT_PRI,
            relief=tk.FLAT, bd=0, highlightbackground=CLR_DIV,
            highlightthickness=1, selectbackground=CLR_ACCENT,
            selectforeground="#17140F", activestyle="none")
        self._list.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        row1 = tk.Frame(body, bg=BG_PANEL); row1.pack(fill=tk.X)
        _btn(row1, "Add Account", self._on_add, accent=True).pack(
            side=tk.LEFT, padx=(0, 6))
        _btn(row1, "Remove", self._on_remove, danger=True).pack(
            side=tk.LEFT, padx=(0, 6))
        _btn(row1, "Move Up", lambda: self._move(-1)).pack(side=tk.LEFT, padx=(0, 6))
        _btn(row1, "Move Down", lambda: self._move(1)).pack(side=tk.LEFT, padx=(0, 6))

        row2 = tk.Frame(body, bg=BG_PANEL); row2.pack(fill=tk.X, pady=(8, 0))
        _btn(row2, "Install Launcher", self._on_install).pack(side=tk.LEFT, padx=(0, 6))
        _btn(row2, "Refresh", self._reload).pack(side=tk.LEFT, padx=(0, 6))
        _btn(row2, "Close", t.destroy).pack(side=tk.RIGHT)

        t.bind("<Escape>", lambda _: t.destroy())
        self._accounts = []
        self._reload()

    def _reload(self):
        try:
            self._accounts = _cli_backend().pool_list()
        except Exception as error:
            self._accounts = []
            self.app._set_status(
                f"CLI pool list failed: {str(error) or type(error).__name__}")
        self._list.delete(0, tk.END)
        for i, account in enumerate(self._accounts, start=1):
            label = account.email or "signed out"
            self._list.insert(tk.END, f"{i}. {account.name} — {label}")

    def _selected_name(self):
        selection = self._list.curselection()
        if not selection or selection[0] >= len(self._accounts):
            return None
        return self._accounts[selection[0]].name

    def _select(self, name):
        for i, account in enumerate(self._accounts):
            if account.name == name:
                self._list.selection_clear(0, tk.END)
                self._list.selection_set(i)
                return

    def _on_add(self):
        if self.app._busy:
            messagebox.showinfo(
                "CLI Pool", "Another operation is in progress; try again shortly.",
                parent=self.top)
            return
        name = simpledialog.askstring(
            "Add Pool Account",
            "Account name (letters, numbers, - or _; not starting with _):",
            parent=self.top)
        if name is None or not name.strip():
            return
        name = name.strip()
        if not messagebox.askyesno(
            "Add Pool Account",
            f"Add pool account '{name}'?\n\n"
            "A terminal opens with an isolated login. Log in with the account "
            "to add; closing the terminal cancels. If no login prompt appears, "
            "type /login.",
            parent=self.top):
            return
        self.app._start_pool_add(name)

    def _on_remove(self):
        name = self._selected_name()
        if not name:
            return
        if not messagebox.askyesno(
            "Remove Pool Account",
            f"Remove pool account '{name}'?\n\n"
            "Its login is parked recoverably under pool\\_retired-* — never "
            "deleted.",
            parent=self.top):
            return
        try:
            _cli_backend().pool_retire(name)
            self.app._set_status(f"Removed pool account '{name}' (recoverable).")
        except Exception as error:
            messagebox.showwarning(
                "Remove Failed", str(error) or type(error).__name__, parent=self.top)
        self._reload()

    def _move(self, delta):
        name = self._selected_name()
        if not name:
            return
        try:
            _cli_backend().pool_move(name, delta)
        except Exception as error:
            messagebox.showwarning(
                "Move Failed", str(error) or type(error).__name__, parent=self.top)
            return
        self._reload()
        self._select(name)

    def _on_install(self):
        try:
            path = _cli_backend().pool_install_launcher()
        except Exception as error:
            messagebox.showwarning(
                "Install Failed", str(error) or type(error).__name__, parent=self.top)
            return
        messagebox.showinfo(
            "Launcher Installed",
            f"Installed the pool launcher:\n{path}\n\n"
            "With that folder on PATH, run e.g.:\n"
            'claude-pool -p "..." --model claude-opus-4-8 ...',
            parent=self.top)


# ---------------------------------------------------------------------------

def main():
    if _try_mutex() is None:
        _poke_existing(); return
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
