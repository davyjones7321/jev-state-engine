# Phase S Review Fixes Report

## Fix 1 (Phase 3b): No Execution Before Approval

### 1. What Changed (Files and Lines)

- `src/jev/proposals.py`:
  - Lines 206–215: Modified `inspect_executable` to resolve executable paths using `shutil.which` only and return `(exe_path, None)`. Removed `subprocess.run([exe_path, "--version"])`.
  - Updated docstrings for `inspect_executable` and `build_proposal_from_repo` to truthfully state that no command or subprocess is ever executed during proposal creation.
- `tests/test_proposals.py`:
  - Line 22: Imported `patch` from `unittest.mock`.
  - Line 37: Imported `inspect_executable` from `jev.proposals`.
  - Lines 470–498: Added unit test `test_proposal_creation_never_starts_subprocess` verifying that `build_proposal_from_repo` never executes the proposed tool with `--version` and `inspect_executable` never invokes any subprocess.

### 2. How It Works

During command proposal extraction in `build_proposal_from_repo`, the candidate executable extracted from CI workflows, Makefiles, manifests, or README instructions is inspected solely via `shutil.which(exe_name)` to determine whether the binary exists on `PATH`. It leaves `tool_version` empty (`None`), avoiding arbitrary command or version flag execution prior to explicit owner review and approval.

### 3. Rule Enforcement: Enforced by Test vs Only by Prompt

| Rule / Requirement | Enforced By Test | Enforced Only By Prompt | Test Name(s) |
|---|:---:|:---:|---|
| Proposal creation never executes tool with `--version` before approval | Yes | No | `test_proposal_creation_never_starts_subprocess` |
| `inspect_executable` resolves path via `shutil.which` without subprocess | Yes | No | `test_proposal_creation_never_starts_subprocess` |
| Proposals never execute command without approval | Yes | No | `test_proposals_never_execute_without_approval` |

### 4. Unrequested Changes

- None.

### 5. Open Problems

- None. Full offline test suite passed (999 passed, 24 skipped, 0 failed).
