# KaliClaude — Isolated Claude Desktop Profiles

KaliClaude is a Windows desktop tool for selecting between multiple Claude
Desktop logins. Each saved profile owns a complete Electron user-data root;
switching selects that root and launches the verified Claude Desktop executable
directly with `--user-data-dir` / `CLAUDE_USER_DATA_DIR`. The original
`%APPDATA%\Claude` window is never stopped; saved profiles open as extra
windows. Steady-state switching does not copy, restore, or replay profile
credentials.

## Features

- Profile states: **Active**, **Ready**, **Needs validation**, **Needs re-login**,
  and **Corrupt**.
- **Prepare New Login** creates a unique pending isolated root and opens Claude
  Desktop against that exact root as a second window. The original Claude
  window stays running. Existing profiles are not cleared or rewritten.
- **Save Current Login** names the pending isolated root after sign-in without
  closing Claude.
- **Verify Login** checks the persistent profile identity without making a snapshot.
- Managed launch/stop targets only a verified Claude Desktop installation and
  fails closed on unknown or unclassifiable `claude.exe` processes.
- Manual usage refresh reads only the selected active root's cookie database.
- History sync is limited to isolated roots carrying the same account UUID;
  different accounts and the default root never share cards. Conversation JSONL
  files are never rewritten. Unsafe path segments are rejected, and deletion
  does not touch a root whose Claude window is still running.

## Requirements and setup

- Windows 10 or 11
- Claude Desktop installed through a supported Squirrel or Microsoft Store package
- Python 3 when running from source

Install the source/build dependencies in a clean Python environment:

```powershell
python -m pip install cryptography pyinstaller
```

Run from source:

```powershell
python gui\app.py
```

Build the standalone executable:

```powershell
.\build.bat
```

The build produces `dist\KaliClaude.exe`. Do not run an unreviewed build against
important profile data.

## Usage

1. Start KaliClaude and allow its offline profile audit to finish. If the audit
   fails, profile/process actions stay disabled.
2. Choose **Prepare New Login**. Claude opens with a fresh isolated root.
3. Sign in in the new window, then choose **Save Current Login** and name it.
4. Select another healthy profile and choose **Switch to this Profile**. That
   opens or focuses its isolated window; the default root is never used.
5. Use **Verify Login** when a profile shows **Needs validation**.
6. A **Needs re-login** profile has preserved recovery material but no exact
   verified login that can be selected safely; prepare and save a fresh login.

## Migration and recovery

On first schema-3 launch, KaliClaude creates one bounded operational backup of
schema-2 metadata/profile storage. Exact, offline-verified recovery matches are
promoted to isolated roots and marked **Needs validation** until a managed launch
is verified. Incomplete or ambiguous recoveries become **Needs re-login** rather
than being guessed. Existing legacy session backups are preserved. Interrupted
promotions are restart-safe: a valid already-promoted root is reused instead of
quarantined or replaced.

Data lives under `%USERPROFILE%\.kalikot-claude-switcher\`:

- `desktop-data\<profile>` — isolated Claude Desktop roots
- `backups\` — bounded operational and pre-deletion recovery backups
- `profiles\` — retained legacy schema-2 material used only for migration
- `meta.json` — schema-3 profile selection and validation metadata

`meta.json` is the single source for the last selected profile (`desktop_active`)
and is written only after a managed isolated launch is verified. A separate
SQLite database is intentionally not used for this one-row state; it would
duplicate the catalog and introduce another recovery/concurrency boundary.

## Privacy and safety boundary

KaliClaude stores profile data locally and does not log credentials. Its usage
refresh sends only the currently selected active root's live Claude session cookie
to `claude.ai`; it never enumerates or falls back to inactive profile cookies.
Claude Desktop itself may use the selected root's session when KaliClaude launches
it. The local single-instance socket binds only to `127.0.0.1`.

## Tests

```powershell
python -m unittest discover -s tests -v
python -m compileall -q gui tests
```

The suite covers isolated roots and direct launch verification, fail-closed process
handling, lossless migration (including real SQLite WAL/SHM fixtures), AST-enforced
offline cookie recovery, active-root-only usage, Tk worker marshalling, and safe
cross-root history sync.

Packaged-launcher note: Squirrel discovery checks `%LOCALAPPDATA%`, the derived
`%USERPROFILE%\AppData\Local` path, and `Path.home()`; it is case-insensitive and
requires only an existing `app-*\claude.exe`. Squirrel is preferred before the
Store executable, so a locked Store install cannot mask the usable Squirrel
install. The launch boundary re-probes Squirrel before accepting any Store
specification, blocking stale Store-first callers as well. Rebuild only after
closing KaliClaude itself; the running GUI locks `dist\KaliClaude.exe` on
Windows.

Store-launch compatibility proof (2026-09-01): the fresh Microsoft Store build
accepted `--user-data-dir=<isolated-root>` but split `--user-data-dir <root>`
left its child processes on `%APPDATA%\Claude`. Launch now uses the equals form.
The live source launch created the pending isolated root, produced its own
`logs\main.log`, and all nine verified Claude processes reported that isolated
root; the login page opened without credentials. The packaged GUI was rebuilt
after this fix.

Claude Code account switching is outside Phase 1.1. Managed Claude Code and
agent-mode history synchronization remains part of this release.
