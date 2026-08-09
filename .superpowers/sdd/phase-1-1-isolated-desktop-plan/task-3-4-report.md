# Phase 1.1 Tasks 3–4 combined report

## Changes

- Rewired the existing Tk layout to schema-3 backend semantics: startup-audit
  action gating; pending isolated login creation and managed launch; pending
  finalization; persistent login verification; managed switch/launch/stop; and
  backend active-root status/usage lookup.
- Removed GUI AUMID/Explorer launch, global Claude-root credential lookup,
  snapshot capture/prepare calls, and worker-thread Tk calls. Tick results now
  travel through the UI queue.
- Added active-root-only usage lookup. No inactive profile cookie fallback or
  enumeration is present.
- Added closed-only cross-root history merge with decoded-segment rejection,
  containment checks, symlink exclusion, and deletion confined to explicit
  managed account directories. History failure remains separate/non-fatal to a
  successful switch.
- Added restart-safe reuse of a valid already-promoted legacy migration root.
- Rewrote README for isolated roots, migration/recovery, validation/re-login
  states, direct-launch requirements, and the narrowed privacy boundary.
- Aligned `build.bat` with the checked-in PyInstaller spec and clean builds.

Rename, remove, and note editing remain visible but disabled because schema-3
has no safe public mutation API for those operations; the old direct filesystem
mutation path was removed rather than risk isolated-root loss.

## Focused TDD evidence

RED (before implementation): 12 focused tests ran; 6 failures and 4 errors
showed the missing GUI queue/contracts, history safety/merge/deletion, and
migration crash hook. The pre-existing non-fatal history switch and headless
import checks were already green.

GREEN:

- Backend history + crash-restart focus: 5/5 passed.
- GUI focus: 7/7 passed after tick config-read failures were separated from
  process-detection failures.

## Full verification

- `python -m unittest discover -s tests -v`: **51 tests passed**, 0 failures.
- `python -m compileall -q gui tests`: exit 0, no output.
- `pyinstaller --clean --noconfirm KaliClaude.spec`: analysis/package succeeded,
  but final replacement of the pre-existing `dist\KaliClaude.exe` failed with
  WinError 5 because that file is locked. No unknown process was terminated.
- `pyinstaller --clean --noconfirm --distpath dist-phase11 --workpath build-phase11 KaliClaude.spec`:
  **exit 0**, clean build complete.
- Artifact inspected without launch:
  `dist-phase11\KaliClaude.exe`, 17,657,419 bytes,
  SHA-256 `4E867A718187E0C216B62EAE112E0982FA358E276BE62A1914A24FE3F8C4EDED`.
  Archive contents include the app entry, Tk runtime, icon, SQLite, and
  cryptography runtime. PyInstaller warnings are platform/optional imports; no
  required Windows runtime module is reported missing.
- `git diff --check`: clean (line-ending notices only).
- Legacy GUI path scan for capture/prepare/AUMID/Explorer/global-root symbols:
  no matches.

The executable was not launched and no real Claude profile, cookie, credential,
or process was read, modified, stopped, or started.

## CodeRabbit disposition

- Fixed: #1 startup gating, #2 relaunch-result handling, #9 traversal, #10 scoped
  deletion, #11 privacy wording, #12 headless GUI collection, #13 pending-root
  result propagation, #15 AST no-network boundary, and #20 queued Tk tick.
- Preserved/fixed by migration work and tests: #3 temp artifacts, #4/#17 real
  WAL/SHM, #6 bounded operational retention, #16 printable non-token rejection,
  and #19 decryption-cache clearing.
- Obsolete code removed: #5 full-tree steady-state hashing, #7 generation prune,
  #8 fixed shared login stage, and #18 live-path replacement cleanup.
- #14 old restore-step test duplication is obsolete with snapshot restore removal.

## Self-review and concerns

- Reviewed the GUI diff for all legacy launch/global-root/capture paths and the
  backend diff for containment, symlink, and managed-target boundaries.
- Switch metadata remains commit-last; failed process detection/launch does not
  select a new root. History errors do not roll back a successful stopped-app
  selection.
- Concern: the conventional `dist\KaliClaude.exe` was locked, so the verified
  artifact is in `dist-phase11`. Once the external lock is released, `build.bat`
  can populate `dist` with the same clean spec build.
- Live two-account UAT remains the merge gate. No push, PR comment, or merge was
  performed.
