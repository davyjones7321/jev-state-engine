# Phase 4 Report: Preflight and Support Profile

## 1. What Changed (Files and Lines)

### Allowed Files Modified / Created:
- `src/jev/preflight.py` (New file, lines 1–535):
  - Defines `PreflightStatus` enum (`IN_PROFILE`, `OUTSIDE_PROFILE`, `PROFILE_UNKNOWN`) and aliases `IN_PROFILE`, `OUTSIDE_PROFILE`, `PROFILE_UNKNOWN`.
  - Defines `PreflightCheckResult` and `PreflightResult` Pydantic models.
  - Implements the 10 preflight checks:
    1. `check_git_and_disposable_worktree`: Checks git binary availability (`shutil.which("git")`), repository validity, and disposable worktree accessibility.
    2. `check_host_platform`: Normalizes and checks platform support against known platforms (`linux`, `darwin`, `win32`).
    3. `check_profile_platform_fit`: Validates runner profile against host platform; ensures Xcode/macOS yields `ENVIRONMENT_UNAVAILABLE` on non-darwin platforms and never substitutes another runner.
    4. `check_executable_available`: Validates that the runner executable exists on host or PATH, yielding `MISSING_TOOL` when absent, and checks version bounds when tool version is available.
    5. `check_non_interactive_shape`: Detects interactive flags (`-i`, `--interactive`, `--prompt`) and bare interactive shells.
    6. `check_watch_daemon_indicators`: Detects watch flags (`--watch`, `-w`, `--watchAll`, `--watch-all`, `--watch-files`, `--live`, `--hot`, `--daemon`, `-d`, `--serve`, `--watch=true`) and subcommands, preventing unbounded executions (`TOOLING_ERROR`).
    7. `check_declared_external_services`: Detects external network assumptions and declared services from repo config or command flags (`ENVIRONMENT_UNAVAILABLE`).
    8. `check_target_representable`: Validates that test targets can be represented by profile target capabilities (e.g. node_id, filter) and exist in worktree (`NO_TEST_EXECUTION_SIGNAL`).
    9. `check_command_identity_complete`: Verifies complete command identity; detects ambiguous runner matches (`source="ambiguous"`) or untrusted config, yielding `PROFILE_UNKNOWN(NO_COMMAND)`.
    10. `check_timeout_and_process_tree`: Validates positive timeout and process tree management capability (Windows Job Objects / POSIX process groups).
  - Implements `run_preflight(...)`: Executes all 10 checks in deterministic order, determines overall status and failure reason, and generates durable `MechanicalEvidence` (`mech_status=UNMEASURED`, `mechanically_unverified=True`, `full_suite_verified=False`) on non-in-profile statuses.

- `src/jev/workspace.py` (Modified, lines 739, 1705–1725, 2112–2147, 3763):
  - In `Workspace.__init__`: Initializes `self.last_preflight = None`.
  - Adds `Workspace.run_preflight(...)`: Public workspace method to run preflight checks on demand.
  - In `Workspace.evaluate_tests`: Injects `run_preflight` before any candidate or base test execution. If preflight status is not `IN_PROFILE`, records preflight telemetry and immediately returns `preflight_res.evidence` without running any test subprocess or allocating detached worktrees.
  - In `Workspace.run_mechanical_checks`: Resets `self.last_preflight = None` at beginning of check suite.

- `src/jev/engine.py` (Modified, lines 3755–3757):
  - In `node_gate`: Embeds preflight telemetry (`workspace.last_preflight.model_dump()`) into `tier0_telemetry` when preflight was executed.
  - Preserves existing strike invariant: preflight failures (`env_not_ready` for `MISSING_TOOL`/`ENVIRONMENT_UNAVAILABLE`, and `is_unmeasured` for `NO_COMMAND`/`TOOLING_ERROR`) bypass strike increments, preserving `mechanical_strike_count == 0` and `semantic_strike_count == 0`.

- `tests/test_preflight.py` (New file, lines 1–416):
  - 18 unit and integration tests covering all 10 checks, status transitions, durable evidence generation, stopping before execution, strike prevention, and runner substitution prevention.

---

## 2. How It Works

1. **Pre-Execution Gate**: Before any candidate test process or detached base worktree execution is launched in `Workspace.evaluate_tests`, `run_preflight` evaluates the repository, host platform, runner profile, command shape, and execution bounds.
2. **10 Bounded Preflight Checks**:
   - `git and disposable worktree`: Verifies `git` binary and working repository structure.
   - `host platform`: Detects current operating system (`win32`, `linux`, `darwin`).
   - `profile/platform fit`: Confirms runner profile compatibility with host platform; flags macOS-exclusive runners (e.g. `xcode-test`) as `ENVIRONMENT_UNAVAILABLE` on non-macOS hosts without substituting runners.
   - `executable available and launchable`: Validates that runner binary exists and is launchable, yielding `MISSING_TOOL` when not found on host or PATH.
   - `non-interactive command shape`: Rejects interactive REPL flags (`-i`, `--interactive`) that would cause test hangs.
   - `watch/daemon indicators`: Catches watch/daemon modes (`--watch`, `-w`, `--watchAll`, `--serve`, etc.) that prevent bounded process completion.
   - `declared external services or network`: Rejects network or external service assumptions declared in configuration or argv.
   - `target representable`: Ensures targets match runner capabilities (e.g. `node_id`) and exist on disk.
   - `command identity complete`: Flags ambiguous multi-profile matches as `PROFILE_UNKNOWN(NO_COMMAND)`.
   - `timeout and process-tree capability`: Confirms positive timeout and OS process-tree termination capability (Windows Job Objects / POSIX process groups).
3. **Durable Unmeasured Evidence**: Any check failure immediately halts execution with status `OUTSIDE_PROFILE` or `PROFILE_UNKNOWN` and returns durable `MechanicalEvidence` with `mech_status=UNMEASURED` and the appropriate `MechReason`.
4. **Zero-Strike Guarantee**: Preflight failure routes through either `env_not_ready` (for missing tools / unavailable environments) or `is_unmeasured` (for tooling errors / unknown profiles). Neither path increments `mechanical_strike_count` or `semantic_strike_count`.

---

## 3. Rules Enforced by Tests vs Only by Prompts

| Rule / Requirement | Enforcement Type | Test Name |
| :--- | :--- | :--- |
| In-profile execution passes all 10 checks | Mechanical (Test) | `tests/test_preflight.py::test_preflight_in_profile_success` |
| Missing tool yields `OUTSIDE_PROFILE` with `UNMEASURED(MISSING_TOOL)` | Mechanical (Test) | `tests/test_preflight.py::test_preflight_missing_tool_outside_profile` |
| Missing tool execution halts before any subprocess launch | Mechanical (Test) | `tests/test_preflight.py::test_preflight_stops_before_candidate_or_base_run` |
| Xcode/macOS yields `OUTSIDE_PROFILE` with `UNMEASURED(ENVIRONMENT_UNAVAILABLE)` | Mechanical (Test) | `tests/test_preflight.py::test_preflight_xcode_environment_unavailable` |
| Never substitute another runner when tool/platform is unsupported | Mechanical (Test) | `tests/test_preflight.py::test_preflight_never_substitutes_another_runner` |
| Ambiguous runner profile yields `PROFILE_UNKNOWN` with `UNMEASURED(NO_COMMAND)` | Mechanical (Test) | `tests/test_preflight.py::test_preflight_ambiguous_profile` |
| Watch/daemon mode indicators yield `OUTSIDE_PROFILE` with `UNMEASURED(TOOLING_ERROR)` | Mechanical (Test) | `tests/test_preflight.py::test_preflight_watch_mode_outside_profile[--watch]` |
| Watch mode `--watchAll` yields `OUTSIDE_PROFILE` | Mechanical (Test) | `tests/test_preflight.py::test_preflight_watch_mode_outside_profile[--watchAll]` |
| Watch mode `-w` yields `OUTSIDE_PROFILE` | Mechanical (Test) | `tests/test_preflight.py::test_preflight_watch_mode_outside_profile[-w]` |
| Watch mode `--watch-all` yields `OUTSIDE_PROFILE` | Mechanical (Test) | `tests/test_preflight.py::test_preflight_watch_mode_outside_profile[--watch-all]` |
| Watch mode `--watch=true` yields `OUTSIDE_PROFILE` | Mechanical (Test) | `tests/test_preflight.py::test_preflight_watch_mode_outside_profile[--watch=true]` |
| Watch mode `--watchAll=1` yields `OUTSIDE_PROFILE` | Mechanical (Test) | `tests/test_preflight.py::test_preflight_watch_mode_outside_profile[--watchAll=1]` |
| Interactive command flags yield `OUTSIDE_PROFILE` with `UNMEASURED(TOOLING_ERROR)` | Mechanical (Test) | `tests/test_preflight.py::test_preflight_interactive_command_outside_profile` |
| Declared external services/network yield `OUTSIDE_PROFILE(ENVIRONMENT_UNAVAILABLE)` | Mechanical (Test) | `tests/test_preflight.py::test_preflight_declared_external_services` |
| Unrepresentable test targets yield `OUTSIDE_PROFILE` before execution | Mechanical (Test) | `tests/test_preflight.py::test_preflight_target_representability` |
| Invalid/non-positive timeout yields `OUTSIDE_PROFILE(TOOLING_ERROR)` | Mechanical (Test) | `tests/test_preflight.py::test_preflight_invalid_timeout` |
| Preflight failure (MISSING_TOOL) adds NO strike in engine | Mechanical (Test) | `tests/test_preflight.py::test_preflight_missing_tool_adds_no_strike_in_engine` |
| Preflight failure (watch mode) adds NO strike in engine | Mechanical (Test) | `tests/test_preflight.py::test_preflight_watch_mode_adds_no_strike_in_engine` |
| LLM cannot invent arbitrary commands to bypass preflight | Prompt & Architecture | Architectural enforcement by `resolve_test_command` returning `NO_COMMAND` for unknown environments and `run_preflight` halting unverified commands |

---

## 4. Unrequested Changes
- **None**. Only files specified in Phase 4 were added or modified (`src/jev/preflight.py`, `src/jev/workspace.py`, `src/jev/engine.py`, `tests/test_preflight.py`, `docs/phases/phase4.md`).

---

## 5. Open Problems
- **None**: All 18 unit and integration tests in `tests/test_preflight.py` pass. The offline test suite passes completely with zero regressions.

---

## 6. Verification Script Output
