# KaliClaude — Isolated Claude Desktop Profiles

KaliClaude is a Windows desktop tool for selecting between multiple Claude
Desktop logins. Each saved profile owns a complete Electron user-data root;
switching selects that root and launches the verified Claude Desktop executable
directly with `CLAUDE_USER_DATA_DIR`. Steady-state switching does not copy,
restore, or replay profile credentials.

## Features

- Profile states: **Active**, **Ready**, **Needs validation**, **Needs re-login**,
  and **Corrupt**.
- **Prepare New Login** creates a unique pending isolated root and opens Claude
  Desktop against that exact root. Existing profiles are not cleared or rewritten.
- **Save Current Login** finalizes the pending root after sign-in.
- **Verify Login** checks the persistent profile identity without making a snapshot.
- Managed launch/stop targets only a verified Claude Desktop installation and
  fails closed on unknown or unclassifiable `claude.exe` processes.
- Manual usage refresh reads only the selected active root's cookie database.
- History sync merges managed Claude Code and agent-mode account directories
  across default and isolated roots while Claude Desktop is closed. Unsafe path
  segments are rejected, and deletion propagation stays inside managed account
  directories.

## Requirements and setup

- Windows 10 or 11
- Claude Desktop installed through a supported Squirrel or Microsoft Store package
- Python 3 when running from source

Run from source:

```powershell
python gui\app.py
```

Build the standalone executable:

```powershell
build.bat
```

The build produces `dist\KaliClaude.exe`. Do not run an unreviewed build against
important profile data.

## Usage

1. Start KaliClaude and allow its offline profile audit to finish. If the audit
   fails, profile/process actions stay disabled.
2. Choose **Prepare New Login**. Claude opens with a fresh isolated root.
3. Sign in to Claude Desktop, then choose **Save Current Login** and name it.
4. Select another healthy profile and choose **Switch to this Profile**.
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

Claude Code account switching is outside Phase 1.1; this release changes Claude
Desktop profile selection only.
