# 🔄 KaliClaude — Multi-Account Claude Desktop Switcher

**Switch between multiple Claude accounts with one click — a full *session
snapshot*, not just the token — and keep your Claude Code history & context
with you across every account.**

*kalikot* (Tagalog for *tinkering / fiddling with something*) is a Windows
desktop app for anyone juggling several Claude Desktop logins. It saves each
account as a profile, switches the live login safely (stopping Claude,
snapshotting the outgoing session, restoring the target's session, then
relaunching), shows live 5-hour & weekly usage with countdowns, and — the part
that makes multi-account actually usable — **merges your Claude Code session
history across accounts** so your projects and context follow you when you
switch. Everything stays on your machine, and a standalone `.exe` means there's
nothing to install.

## 🛠️ Built With

- Python 3
- [Tkinter](https://docs.python.org/3/library/tkinter.html) — the GUI (ttk widgets)
- [cryptography](https://pypi.org/project/cryptography/) — AES-256-GCM + Windows DPAPI, to read usage **read-only**
- Win32 API (`ctypes`) — DPAPI unprotect, single-instance mutex, process control
- PyInstaller — standalone `.exe` packaging

## ✨ Features

- 👥 **Profile list** with at-a-glance status — active marker (●), account email,
  plan (Max / Pro / Team), snapshot age, and a free-text note.
- 💾 **Save Current Login** — capture the account currently signed into Claude
  Desktop as a switchable profile (the *whole* web session, not just a token).
- 🔀 **Switch Profile** — the *safe* switch: stop Claude → snapshot the outgoing
  account → back up the live session → restore the target's session → relaunch.
- 🧠 **Claude Code history sync** — merges every account's Claude Code &
  agent-mode session history so the combined project list and context are visible
  under any profile. Deletion-aware, and runs **automatically on every switch**
  (plus a manual **Sync History** button).
- ♻️ **Update Snapshot** — re-capture the current login into a profile to keep its
  stored session fresh after a long session.
- 🆕 **Prepare New Login** — safely clears the live session (with backup) so you
  can sign into a different account, then **Save Current Login** captures it.
- 📊 **Live usage bars** — 5-hour and 7-day quota with reset countdowns
  ("resets in ~2h 15m"). Read **read-only**; the active profile shows live
  numbers, others show the last cached values.
- ▶️ **Launch / Stop Claude** with live running-state detection.
- 💾 **Portable** — the standalone `dist\KaliClaude.exe` needs no Python and
  enforces a single running instance.

## 🧠 How it works

**A Claude login is a whole web session, not just a token.** A profile snapshots
the embedded claude.ai session — Session Storage, IndexedDB, cookies — plus the
encrypted `oauth:tokenCache` blob from `config.json`. All copying and swapping
happens only while **Claude is fully stopped**, and the live session is backed up
before anything is overwritten, so every step is recoverable.

**Claude Code history is local — and account-scoped.** Claude Code stores its
sessions at `claude-code-sessions\<workspace>\<accountId>\`. Because the folder is
keyed by account, switching logins normally *hides* your projects and history.
KaliClaude reads the desktop log to learn the exact folder each account loads
from (including brand-new accounts), then distributes the **union** of every
account's sessions into each one — so your history is global. Deletions are
detected and propagated, so a removed conversation stays removed everywhere.

## 🔧 Setup

**Easiest — no install needed:** download the standalone **`KaliClaude.exe`**
from [Releases](https://github.com/kalikot26/KaliClaude-Claude-Profile-Switcher/releases/latest)
and run it. It bundles Python and every dependency.

> **Prerequisite:** the **Claude Desktop** app must be installed.

**To run from source** (Python 3 required):

```bash
cd gui
python app.py
```

**To build the `.exe` yourself:**

```bash
build.bat            # runs PyInstaller, outputs dist\KaliClaude.exe
```

## 🚀 Usage

1. Launch the app — your saved profiles appear with the active one marked ●.
2. **Save Current Login** captures whatever account is signed into Claude as a
   new profile.
3. Select a profile and **Switch to this Profile** — it stops Claude, switches
   safely, syncs Claude Code history, and offers to relaunch.
4. Use **Sync History** any time (no switch needed) to bring every account's
   Claude Code projects and context up to date.
5. Watch the **5-hour / 7-day** bars for remaining quota.

## 📝 Notes

- **Windows only** — it relies on the Claude Desktop app, Windows DPAPI, and
  Windows process management.
- **Privacy:** everything stays on your machine. The session blob is encrypted
  under your Windows user; it is decrypted **only in-memory** to read the access
  token for a read-only usage request — the refresh token is never used (nothing
  rotates), and tokens are never logged, displayed, or uploaded. The
  single-instance IPC socket binds to `127.0.0.1` exclusively.
- Profiles, snapshots, and timestamped backups live under
  `%USERPROFILE%\.kalikot-claude-switcher\`.

## 👨‍💻 Author

**John Venice Almazan** — [@kalikot26](https://github.com/kalikot26)
