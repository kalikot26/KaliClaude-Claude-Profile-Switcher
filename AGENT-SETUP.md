# Agent setup guide — KaliClaude CLI pool

Give this file to your coding agent. It describes how to set up and drive a pool
of Claude Code CLI accounts so that a top-tier main account **orchestrates** work
while the pool accounts **execute** it.

Everything below is grounded in the code in this repository. Where something is
unverified, it says so.

## The model

```
main account  (your best tier, e.g. Fable)   ← orchestrates, holds context, decides
    │
    │  claude-pool.cmd -p "..."
    ▼
pool account 1 → 2 → 3 …                     ← execute the work, get billed for it
    (automatic failover when one hits a usage limit)
```

Each pool account is a private `CLAUDE_CONFIG_DIR` home, so they stay signed in
side by side and never conflict. The launcher walks them in order and fails over
when one is limited.

**What this genuinely saves, and what it does not.** The delegated *work* is
authenticated and billed against the pool account — that part is real. Main still
pays for two things: the orchestration reasoning itself, and every token of the
child's reply, which enters main's context and is re-billed on each later turn.
That second cost is the one you control, and the fix is in the prompt: tell the
child to write its deliverable to a file and reply with one short line.

## One-time setup

1. Open KaliClaude and click **CLI Pool…**.
2. **Add Account**, once per account. A terminal opens with an isolated login.
   Sign in there. If no prompt appears, type `/login`. Closing it cancels; the
   timeout is 10 minutes. **A human must do this step** — never automate a login.
   - Names allow letters, digits, `-` and `_`, and may not start with `_`.
   - **Use a different Anthropic account for every slot.** Two slots on one
     account are not redundancy: failover from a limited slot lands on the same
     exhausted quota. KaliClaude now flags such a row with `⚠ same account as
     pool slot '<name>'`, and flags a slot holding your live default login with
     `⚠ same account as the live default CLI login`. Retire any flagged slot.
   - Do **not** put your main orchestrator account in the pool. Failover would
     route work straight back to the account you are trying to spare.
3. **Move Up / Move Down** to set failover order. Position 1 is tried first.
4. **Install Launcher**. This writes `claude-pool.ps1` and `claude-pool.cmd` into
   `%USERPROFILE%\.local\bin`.
5. Add `%USERPROFILE%\.local\bin` to your `PATH`. **KaliClaude does not do this
   for you** — it never modifies `PATH`.

Verify the setup without spending anything:

```powershell
Get-Command claude.exe
Get-Content "$env:USERPROFILE\.kalikot-claude-switcher\cli-data\pool\pool.json"
```

The first must resolve, and the second must list your intended accounts in order.

## Delegating work — the canonical call

```
claude-pool.cmd -p "<self-contained task>. Write your output to <absolute path>. Reply with one short line only." 2> pool.err
```

Every part of that matters:

- **`claude-pool.cmd`, spelled out.** The `.cmd` shim runs the launcher in a
  throwaway child process and bypasses PowerShell execution policy. Invoking the
  `.ps1` directly runs it in your own shell — survivable now that the launcher
  restores `CLAUDE_CONFIG_DIR` on exit, but the `.cmd` is still the safe default
  and is the form that resolves from Git Bash.
- **`-p`.** The launcher runs the CLI with stdin closed and buffers all output
  until the child exits. Interactive and streaming use are structurally
  impossible. Do not pipe input into it either.
- **"write to a file, reply one line."** This is the single biggest lever on
  main's cost, and it also shrinks the text the limit detector scans.
- **`2> pool.err`, and read it.** Routing is reported only on stderr:
  - `claude-pool: served by '<name>'` — success, and which account paid.
  - `claude-pool: '<name>' limit hit before completing (exit N), failing over`
  - `claude-pool: all accounts exhausted`
  - `claude-pool: no pool accounts configured`
  - `claude-pool: could not find the Claude Code CLI …`

**Never branch on the exit code.** The launcher returns the child's own exit
code, so a successful run and an exhausted pool can both exit 0. Read stderr.

Quote each multi-word prompt in one pair of double quotes, and do not end a
prompt with a backslash — a trailing `\` escapes the closing quote. Write
`-p "audit src/gui/"` rather than `-p "audit src\gui\"`.

### Things not to delegate through the pool

- **Non-idempotent work** — git commits, appends, sends, outbound POSTs. On
  failover the launcher re-runs the *identical* prompt on the next account, so
  such an action can happen once per account. Delegate these to a single pinned
  account instead, or have the child work on a throwaway branch or worktree.
- **`--model` you are not sure every pool account is entitled to.** The flag is
  forwarded to every account in the chain, and an entitlement error is not a
  limit error, so it is returned to you as if it were the answer.

## Addressing one specific pool account

The launcher has no account selector and always starts at position 1. To pin one
account, bypass it and set `CLAUDE_CONFIG_DIR` in a **child** process so the
variable never touches your own shell:

```powershell
cmd /c "set CLAUDE_CONFIG_DIR=%USERPROFILE%\.kalikot-claude-switcher\cli-data\pool\<name>&& claude -p ""<task>"""
```

Take `<name>` from the `order` array in `pool.json` — that is the only list the
launcher reads. The CLI Pool dialog also shows on-disk strays that are not in the
order and will never be run.

You lose failover when you do this. That is the trade.

## Rules that prevent real damage

- **Never use the Desktop "Switch to this Profile" button to change pool
  routing.** It does not affect the pool at all, and it moves the live
  `~\.claude\.credentials.json` that your main session is running on — either
  re-pointing it at another account or signing it out. Change routing with
  **CLI Pool → Move Up/Down**, or by pinning an account as shown above.
- **Unset `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_API_KEY`, and
  `CLAUDE_CODE_OAUTH_TOKEN`** in the shell you launch from. The CLI reads these
  before the credentials file, and the launcher does not clear them, so they
  silently defeat the whole scheme.
- **The pool is stateless.** It restarts at position 1 on every call, so once
  account 1 is limited every later call still pays a round-trip to it before
  failing over. Move a dead account down manually.

## Desktop profiles — the isolated root ("VM")

Desktop profiles and CLI pool accounts are **completely independent planes**.
Switching a Desktop profile does not change the pool, and adding a pool account
does not create a Desktop profile.

Each Desktop profile owns a full Electron user-data root and is launched with
`--user-data-dir=<root>`. Sessions are never copied into a shared root, and no
window is ever stopped on a switch — that is what keeps a session alive.

### Creating a profile — the supported path

1. **Prepare New Login** — creates an empty root and opens Claude on it.
2. **Sign in in that window. The human must do this**, never the agent.
3. **Save Current Login**, and name it. Letters, digits, `-`, `_`.
4. **Switch to this Profile** once. It moves from *Needs validation* to *Ready*
   when the launch is verified against that root's own log.

Do not click Switch repeatedly to make a badge turn green: a second click on an
already-running root reports ready without inspecting anything. Judge by the
window, not the badge.

### Cloning an existing signed-in profile — unsupported

Prefer a fresh sign-in. It costs about two minutes, writes into a brand-new
empty directory, and cannot corrupt anything. Cloning is explicitly disabled in
the code (`seed_profile` raises *"Saved-login seeding is disabled"*), so any
clone routes around a deliberate refusal and nothing validates the result.

**Copying a folder into `desktop-data\` does nothing.** `meta.json` is the only
catalog; the app never scans that directory. A profile's folder also keeps its
`pending-<32 hex>` name permanently — `meta.json` maps the profile name to that
path — so a folder named after your profile is an orphan the app will never list.
Do not hand-edit `meta.json` either: the account hash is re-checked on every
launch, and one malformed character empties the catalog and can re-trigger the
legacy migration.

If a clone is still wanted, the only writable landing zone is the `pending-<hex>`
directory that **Prepare New Login** just created, and the sequence is:

1. **[HUMAN]** Fully quit Claude Desktop for the source root — tray icon → Quit.
   Never `taskkill /F`: a forced kill leaves un-checkpointed SQLite `-wal` files
   and stale LevelDB locks, which is exactly the crash this is trying to avoid.
2. Verify quiescence **by executable path, not image name**:

   ```powershell
   Get-CimInstance Win32_Process -Filter "Name='claude.exe'" |
     Select-Object ProcessId, ExecutablePath
   ```

   **The Claude Code CLI is also named `claude.exe`.** Rows under
   `\claude-code\` or `\.local\bin\` are the CLI — killing one can terminate the
   very agent session running these steps. Require zero *Desktop* rows. A blank
   `ExecutablePath` means you cannot see it; treat that as still running.
3. **[HUMAN]** Click **Prepare New Login**, note the `pending-<hex>` path, then
   **Stop Claude** to close the window it opened. Re-check step 2.
4. Empty that pending directory's **contents** but keep the directory. It is not
   empty: step 3 booted Claude into it, so it holds a freshly generated
   `Local State` whose encryption key does not match the source's cookies.
   Deleting the directory itself breaks Launch.
5. Mirror — never merge — the source root in, excluding caches and crash dumps:

   ```
   robocopy "<SOURCE>" "<PENDING>" /MIR /XJ /R:1 /W:1 /NP ^
     /XD Cache "Code Cache" GPUCache DawnCache DawnGraphiteCache DawnWebGPUCache ^
         ShaderCache GrShaderCache Crashpad "Service Worker" Logs blob_storage ^
     /XF lockfile LOCK debug.log
   ```

   `/XJ` stops a junction from sending the copy out of the tree. `/R:1 /W:1`
   matters: robocopy's defaults retry a locked file about a million times.
   Quote every exclusion containing a space — unquoted, `Code Cache` becomes two
   useless exclusions. **Robocopy exit codes are a bitmask: success is `< 8`, and
   a normal successful copy exits `1`.** Never test `-ne 0`, and never chain it
   with `&&` or run it under `set -e`.

   What must survive: `config.json`, `Local State`, `claude_desktop_config.json`,
   `Network\Cookies` plus whichever of `Cookies-journal` / `Cookies-wal` /
   `Cookies-shm` exist, and the `Session Storage`, `Local Storage` and `IndexedDB`
   directories. Copying only that allowlist is strictly safer than mirroring
   minus exclusions.
6. Check `config.json` parses and has a non-empty `lastKnownAccountUuid`, and
   that `Local State` parses and has `os_crypt.encrypted_key`. Both come from the
   **same** source root — pairing cookies with a foreign `Local State` produces a
   profile that launches and is silently signed out.
7. **[HUMAN]** **Save Current Login**, then **Switch to this Profile** once, and
   confirm in the window that the expected account is signed in.

Rollback: if anything fails before step 7, use the app's discard path rather than
deleting the folder by hand — a stale `pending_login` entry breaks Launch for
every profile. After step 7, select another profile and use **Remove**, which
moves the root to `backups\removed-*` rather than deleting it.

Only the same Windows user on the same machine can use a cloned root at all: the
cookie encryption key is bound to the Windows account via DPAPI.

### Honest limits

These were not confirmed by running Claude Desktop:

- Whether a cloned root stays authenticated, or whether device-bound material
  causes a later sign-out. This is the most likely cause of "it worked, then
  signed me out".
- Whether the exclusion list above is exactly right — it comes from Chromium
  layout, not from this repository, which uses an allowlist instead.
- Whether shipped Claude Desktop emits the phrases the readiness check looks for,
  so a correctly signed-in profile may sit at *Needs validation*.
- The Claude CLI's exact usage-limit wording, so failover detection is a
  heuristic: it triggers on a short, non-completing run whose tail matches the
  limit pattern. A long successful answer that merely discusses rate limits or
  HTTP 429 no longer triggers it.
