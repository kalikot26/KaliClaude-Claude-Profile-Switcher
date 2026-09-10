# CLAUDE.md — agent guide to KaliClaude

KaliClaude is a Windows Tkinter tool that manages **two independent Claude auth
planes** on one machine:

| Plane | What it switches | Storage |
| --- | --- | --- |
| **Claude Desktop** | Electron user-data roots, one per login | `%USERPROFILE%\.kalikot-claude-switcher\desktop-data\<profile>` |
| **Claude Code CLI** | `.credentials.json` files, plus the CLI *pool* | `%USERPROFILE%\.kalikot-claude-switcher\cli-data\` |

The two planes never touch each other. A CLI failure is always warn-only and
never rolls back a Desktop switch.

## Code map

| File | Role |
| --- | --- |
| [gui/app.py](gui/app.py) | Tkinter UI, worker thread + queue marshalling, all dialogs |
| [gui/desktop_backend.py](gui/desktop_backend.py) | Desktop profile engine: isolated roots, launch verification, migration, history sync |
| [gui/cli_backend.py](gui/cli_backend.py) | Claude Code CLI engine: pair/unpair/switch, and the CLI pool |
| [tests/](tests) | `unittest` suite, no third-party test deps |

Run from source and test:

```powershell
python gui\app.py
python -m unittest discover -s tests -v
python -m compileall -q gui tests
```

Build: `.\build.bat` produces `dist\KaliClaude.exe`. Close a running KaliClaude
first — the GUI locks that path on Windows.

## Using the app

1. Launch KaliClaude and let the offline profile audit finish. If the audit
   fails, profile and process actions stay disabled by design.
2. **Prepare New Login** creates a fresh isolated root and opens Claude Desktop
   against it as an additional window. The default `%APPDATA%\Claude` window is
   never stopped.
3. Sign in there, then **Save Current Login** and name the profile.
4. **Switch to this Profile** opens or focuses that profile's isolated window.
5. **Verify Login** re-checks a profile showing *Needs validation*. A profile
   showing *Needs re-login* has recovery material but no safely selectable
   login — prepare and save a fresh one.
6. **Pair CLI** / **Unpair CLI** attach a Claude Code CLI login to the selected
   Desktop profile, so switching the profile also swaps the CLI credentials.
7. **CLI Pool…** is independent of the selected profile and always enabled.

Full user-facing detail lives in [README.md](README.md).

## Using the CLI pool

The pool solves a different problem from Pair CLI: instead of *swapping* one CLI
login at a time, it keeps **several CLI accounts signed in simultaneously** and
runs them in order with automatic failover when one hits a usage limit.

Each pool account is a private `CLAUDE_CONFIG_DIR` home at
`%USERPROFILE%\.kalikot-claude-switcher\cli-data\pool\<name>`. The CLI relocates
its whole credential store into that directory, so accounts never conflict.
Run order lives in `pool\pool.json` as `{"order": ["first", "second", ...]}`.

### Setting it up (GUI)

Open **CLI Pool…** from the main window:

- **Add Account** — asks for a name, then opens a terminal with
  `CLAUDE_CONFIG_DIR` pointed at that account's private directory. **A human
  must complete the OAuth login in that terminal**; if no login prompt appears,
  type `/login`. Closing the terminal cancels. Names must match
  `^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$` — the leading `_` is reserved.
- **Move Up / Move Down** — reorder failover priority. Position 1 is tried first.
- **Remove** — parks the account as `pool\_retired-<stamp>-<name>`. Recoverable
  directory move, never a delete.
- **Install Launcher** — writes `claude-pool.ps1` and `claude-pool.cmd` into
  `%USERPROFILE%\.local\bin`, overwriting any existing pair.

### Running through the pool

With `%USERPROFILE%\.local\bin` on `PATH`:

```powershell
claude-pool -p "summarize this repo" --model claude-opus-4-8
```

Every argument is passed straight through to `claude`. The launcher resolves
`claude.exe` from its own directory first, then `PATH`. For each account in
`pool.json` order it sets `CLAUDE_CONFIG_DIR` to that account's home and runs the
CLI. If the output matches the usage/rate-limit pattern it logs
`claude-pool: '<name>' limit hit, failing over` to stderr and tries the next
account; otherwise it logs `claude-pool: served by '<name>'`, writes the output
to stdout, and exits with the CLI's exit code. If every account is exhausted it
logs `claude-pool: all accounts exhausted` and returns the last output and code.

The launcher is self-contained PowerShell with no Python dependency, so it keeps
working when KaliClaude itself is not running.

**Use it for non-interactive runs.** The launcher feeds `$null` on stdin and
buffers the child's combined output through `Out-String` so it can scan for a
limit, which means `-p` / batch invocations work and an interactive REPL session
does not. For interactive work, set `CLAUDE_CONFIG_DIR` to one pool directory
yourself and run `claude` directly.

### Programmatic access

`gui/cli_backend.py` exposes module-level functions over a shared default
backend: `pool_list()`, `pool_add(name)`, `pool_retire(name)`,
`pool_move(name, delta)`, `pool_install_launcher()`. `CliBackend` takes injected
`home`, `spawner`, `env_reader`, and `which` boundaries, which is how the tests
drive it without touching the real machine.

## Constraints to respect when changing this code

- **Never automate a login.** `pair()` and `pool_add()` spawn a visible terminal
  and poll for the credentials file. Keep it that way.
- **Capture is a verified move, install is a copy.** A move copies and
  sha256-verifies the destination *before* unlinking the source, so there is
  never a moment with zero intact copies. The verified-move source is the only
  `unlink` in `cli_backend.py`.
- **Removal parks, it never deletes.** Retired stores and pool accounts become
  `_unclaimed-*` / `_retired-*` directories.
- `~\.claude.json` is read-only — harvest `oauthAccount` best-effort, never
  write it. `.credentials.json` is opaque bytes and is never parsed.
- `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_API_KEY`, and `CLAUDE_CODE_OAUTH_TOKEN`
  outrank the credentials file. `env_overrides()` detects them and warns; it does
  not clear them.
- `cli_backend.py` must not modify `desktop_backend.py`. It imports only pure
  helpers (`_atomic_json`, `_load_json`, `_sha256`).
- `winreg` is imported locally inside `_read_registry_env` so the module still
  imports on non-Windows for tests.
- No new third-party runtime dependencies. Runtime is stdlib plus `cryptography`;
  `pyinstaller` is build-only.
