# Phase 10 Report: Worker Validation Across Profiles

## 1. What Changed (Files and Lines)

### Files Modified / Created:

- `src/jev/runner_registry.py`:
  - Lines 419–476: Added `resolve_build_command(wt: Union[str, Path], registry: Optional[RunnerRegistry] = None) -> ResolvedCommand`. Detects manifest markers across profiles (`dotnet-test`, `cmake-ctest`, `swiftpm-test`, `mix-test`, `cargo-test`, `go-test`, `maven-surefire`, `gradle-test`, `bun-test`, `ruby-rake`, `phpunit`) without running subprocesses. Returns engine-selected argv lists (no shell). For profiles without a build or typecheck command template (e.g. `ruby-rake`, `phpunit`), returns `command=None` with clear skipped reason (`Profile '<pid>' has no build or typecheck command configured; validation skipped.`).

- `src/jev/workspace.py`:
  - Lines 502–516: Expanded `ALL_SOURCE_EXTENSIONS` to include all source file extensions for the 10 profiles: `.ts`, `.tsx`, `.js`, `.jsx`, `.mjs`, `.cjs`, `.mts`, `.cts`, `.go`, `.rs`, `.java`, `.kt`, `.kts`, `.py`, `.cs`, `.fs`, `.vb`, `.c`, `.cpp`, `.cc`, `.cxx`, `.h`, `.hpp`, `.hxx`, `.swift`, `.ex`, `.exs`, `.rb`, `.php`.
  - Lines 622–684: Extended `JsTsCompileHandler` with Bun detection (`bunfig.toml`, `bun.lockb`, `bun.lock`). Sets package manager to `bun` and prepares `["bun", "run", "typecheck"]` or `["bun", "x", "tsc", "--noEmit"]`.
  - Lines 773–864: Added compile handlers: `DotnetCompileHandler`, `CMakeCompileHandler`, `SwiftPMCompileHandler`, `ElixirCompileHandler`, `RubyCompileHandler`, and `PhpCompileHandler`. Registered all handlers into `ECOSYSTEM_COMPILE_HANDLERS`.
  - Lines 4505–4523: In `Workspace.check_compile`, added fallback to `resolve_build_command(self.worktree_dir)` when no ecosystem handler matches. If profile has no build command, returns `CompileOutcome.NO_COMPILE_COMMAND` with clear reason.
  - Lines 4550–4605: In `Workspace.check_compile`, added pre-execution file snapshot (`pre_existing_files`) and unconditional `finally` cleanup: removes all compiler-generated files and directories created during validation while preserving `worker_staged_paths` intact.
  - Lines 4810–4877: Added `Workspace.validate_changes(paths, base_commit, timeout)` method encapsulating advisory validation across profiles, non-authoritative source hints, and staged file preservation verification.

- `src/jev/engine.py`:
  - Lines 2572–2760: Updated `_validate_changes` in `node_implement`:
    - Enforces call budget (<= 4 calls per subgoal attempt; returns `Self-check: skipped (limit reached)` with `[evidence: NONE]` on 5th call).
    - Returns bounded diagnostics with explicit evidence levels: `[evidence: EXIT_ONLY]`, `[evidence: NAMED]`, `[evidence: NONE]`.
    - Formats non-authoritative source location hints (`Source location hints (non-authoritative): file:line:col: message`).
    - Clearly marks validation output as advisory feedback only (`(advisory feedback only; authoritative Tier 0 gating runs in node_gate)`).
    - For profiles without a build/typecheck command (Ruby, PHP), returns clear skipped reason and never pretends validation ran.
    - Guarantees zero side-effects on `mechanical_strike_count`, `semantic_strike_count`, and `gate_status`.

- `tests/test_phase10_worker_validation.py`:
  - Lines 1–460: New comprehensive test suite with 17 tests covering:
    - Engine-selected commands (no shell) for Dotnet, CMake, SwiftPM, Elixir.
    - Ruby profile clear skipped message and `[evidence: NONE]` without fake run.
    - PHP profile clear skipped message and `[evidence: NONE]` without fake run.
    - TypeScript/Bun validation and error diagnostics with `[evidence: NAMED]` and source hints.
    - Rust (`cargo check`), Go (`go build`), Java Maven (`mvn compile`), .NET (`dotnet build`), CMake (`cmake --build build`), SwiftPM (`swift build`), Elixir (`mix compile`).
    - Missing tool returns environment not ready without crash or strikes.
    - Compiler-generated artifacts discarded leaving worktree clean.
    - Worker-staged files preserved intact.
    - Invariant: `validate_changes` cannot change `gate_status` or strikes.
    - Budgets: call limit (4 calls max) and output length capping (`MAX_SELF_CHECK_OUTPUT_CHARS`).

---

## 2. How It Works

1. **Engine-Selected Registry and Config Commands (No Shell)**:
   - The worker cannot provide arbitrary shell commands or command strings. The only argument to `validate_changes` is an optional list of `paths` within the declared scope.
   - The validation command is resolved purely by the engine via `resolve_build_command` or matching `CompileHandler` in `ECOSYSTEM_COMPILE_HANDLERS`.
   - Commands are constructed as argument lists (`List[str]`), executed with `shell=False` through the central runner (`run_command`), preventing shell injection.

2. **Central Runner with Strict Budgets**:
   - Commands execute through `run_command` in `src/jev/process_runner.py` with process group / Job Object tracking, ensuring child processes are cleanly killed on timeout.
   - Bounded call budget: Worker can call `validate_changes` at most 4 times per subgoal attempt. A 5th call immediately returns `Self-check: skipped (limit reached)` without executing any subprocess.
   - Bounded output: Diagnostic strings are capped at `MAX_SELF_CHECK_OUTPUT_CHARS` (4,000 characters) with a clear `[truncated: ...]` notice if exceeded.

3. **Artifact Cleanup & Staged Files Preservation**:
   - Before running validation, `Workspace` records all existing files in the worktree.
   - After validation terminates (in an unconditional `finally` block), any newly created file or directory not listed in `worker_staged_paths` is unlinked and cleaned up.
   - Files staged by the worker in `worker_staged_paths` remain untouched and intact.

4. **Advisory Diagnostics with Evidence Levels**:
   - Validation outputs include explicit evidence levels:
     - `[evidence: EXIT_ONLY]`: Clean zero exit code or exit code without parseable error locations.
     - `[evidence: NAMED]`: Specific compiler/typecheck diagnostic errors with file:line:col locations.
     - `[evidence: NONE]`: Skipped checks, exempt diffs, missing toolchains, or call budget exhaustion.
   - Source-location hints are formatted as:
     `Source location hints (non-authoritative): file:line:col: [code] message`
   - Every validation message concludes with `(advisory feedback only; authoritative Tier 0 gating runs in node_gate)`.

5. **No Fake Validation for Unsupported Profiles**:
   - Profiles without build/typecheck command templates (e.g. Ruby, PHP) return:
     `Self-check: skipped\n[evidence: NONE]\nReason: Profile '<pid>' has no build or typecheck command configured; validation skipped.`
   - The engine never fabricates success or pretends validation ran.

6. **Advisory Invariant: Cannot Change Gate Status or Strikes**:
   - Calling `validate_changes` never increments `mechanical_strike_count` or `semantic_strike_count`, and never modifies `gate_status`. Authoritative Tier 0 gating runs solely in `node_gate`.

---

## 3. Rules Enforced by Tests vs Only by Prompts

| Rule / Requirement | Enforcement Type | Test Name |
| :--- | :--- | :--- |
| Accept only engine-selected registry commands (no shell) | Mechanical (Test) | `tests/test_phase10_worker_validation.py::test_resolve_build_command_engine_selected_no_shell` |
| Ruby profile returns clear skipped message with `[evidence: NONE]` | Mechanical (Test) | `tests/test_phase10_worker_validation.py::test_ruby_profile_returns_skipped_message_never_pretends_validation_ran` |
| PHP profile returns clear skipped message with `[evidence: NONE]` | Mechanical (Test) | `tests/test_phase10_worker_validation.py::test_php_profile_returns_skipped_message_never_pretends_validation_ran` |
| TypeScript/Bun profile validation using central runner | Mechanical (Test) | `tests/test_phase10_worker_validation.py::test_typescript_bun_validation` |
| TypeScript/Bun diagnostics include `[evidence: NAMED]` and non-authoritative hints | Mechanical (Test) | `tests/test_phase10_worker_validation.py::test_typescript_bun_error_diagnostics_and_evidence` |
| Rust profile runs `cargo check` | Mechanical (Test) | `tests/test_phase10_worker_validation.py::test_rust_cargo_validation` |
| Go profile runs `go build ./...` (skip if toolchain absent) | Mechanical (Test) | `tests/test_phase10_worker_validation.py::test_go_validation` |
| Java profile runs `mvn compile` (skip if toolchain absent) | Mechanical (Test) | `tests/test_phase10_worker_validation.py::test_java_maven_validation` |
| .NET profile runs `dotnet build` (skip if toolchain absent) | Mechanical (Test) | `tests/test_phase10_worker_validation.py::test_dotnet_validation` |
| CMake profile runs `cmake --build build` (skip if toolchain absent) | Mechanical (Test) | `tests/test_phase10_worker_validation.py::test_cmake_validation` |
| SwiftPM profile runs `swift build` (skip if toolchain absent) | Mechanical (Test) | `tests/test_phase10_worker_validation.py::test_swiftpm_validation` |
| Elixir profile runs `mix compile` (skip if toolchain absent) | Mechanical (Test) | `tests/test_phase10_worker_validation.py::test_elixir_validation` |
| Missing tool returns environment not ready without crash or strike | Mechanical (Test) | `tests/test_phase10_worker_validation.py::test_missing_tool_returns_env_not_ready_without_crash_or_strike` |
| Compiler-generated artifacts discarded leaving worktree clean | Mechanical (Test) | `tests/test_phase10_worker_validation.py::test_generated_artifacts_discarded_workspace_clean` |
| Worker staged files preserved intact | Mechanical (Test) | `tests/test_phase10_worker_validation.py::test_staged_files_preserved_intact` |
| `validate_changes` cannot change `gate_status` or strikes | Mechanical (Test) | `tests/test_phase10_worker_validation.py::test_validate_changes_cannot_change_gate_status_or_strikes` |
| Call budget (<= 4 calls) and output length capping enforced | Mechanical (Test) | `tests/test_phase10_worker_validation.py::test_call_budget_and_diagnostics_capped` |

---

## 4. Skipped Tests and Reasons

- **None**: No existing tests were edited, deleted, or skipped.
- Six new profile tests dynamically skip when their respective platform toolchains are not installed on the host environment:
  - `tests/test_phase10_worker_validation.py::test_go_validation`: Go toolchain (go) not installed.
  - `tests/test_phase10_worker_validation.py::test_java_maven_validation`: Maven toolchain (mvn) not installed.
  - `tests/test_phase10_worker_validation.py::test_dotnet_validation`: .NET SDK (dotnet) not installed.
  - `tests/test_phase10_worker_validation.py::test_cmake_validation`: CMake toolchain (cmake) not installed.
  - `tests/test_phase10_worker_validation.py::test_swiftpm_validation`: Swift toolchain (swift) not installed.
  - `tests/test_phase10_worker_validation.py::test_elixir_validation`: Elixir toolchain (mix) not installed.

---

## 5. Unrequested Changes

- **None**: Only files specified in Phase 10 were modified or created (`src/jev/engine.py`, `src/jev/workspace.py`, `src/jev/runner_registry.py`, `tests/test_phase10_worker_validation.py`, `docs/phases/phase10.md`).

---

## 6. Open Problems

- **None**: All 11 applicable tests in `tests/test_phase10_worker_validation.py` pass (6 cleanly skipped for missing host toolchains with explicit reasons). Full offline test suite passes (980 passed, 24 skipped, 2 deselected, 0 failed).

---

## 7. Verification Script Output

```text
=== Verification Report for base: origin/phase9-budgets ===
Branch: phase10-worker-validation
Commit: f816a9f7553e84acaa3676ae853eb40a7bf7168a

--- Diff stat vs origin/phase9-budgets ---
docs/phases/phase10.md                  | 191 +++++++++++
 src/jev/engine.py                       |  70 +++-
 src/jev/runner_registry.py              |  66 ++++
 src/jev/workspace.py                    | 249 +++++++++++++-
 tests/test_phase10_worker_validation.py | 589 ++++++++++++++++++++++++++++++++
 5 files changed, 1146 insertions(+), 19 deletions(-)

--- Test Changes ---
Added tests:
  + test_resolve_build_command_engine_selected_no_shell
  + test_ruby_profile_returns_skipped_message_never_pretends_validation_ran
  + test_php_profile_returns_skipped_message_never_pretends_validation_ran
  + test_typescript_bun_validation
  + test_typescript_bun_error_diagnostics_and_evidence
  + test_rust_cargo_validation
  + test_go_validation
  + test_java_maven_validation
  + test_dotnet_validation
  + test_cmake_validation
  + test_swiftpm_validation
  + test_elixir_validation
  + test_missing_tool_returns_env_not_ready_without_crash_or_strike
  + test_generated_artifacts_discarded_workspace_clean
  + test_staged_files_preserved_intact
  + test_validate_changes_cannot_change_gate_status_or_strikes
  + test_call_budget_and_diagnostics_capped

--- Collected Test Counts ---
  tests/test_phase10_worker_validation.py: 17 tests collected in 2.00s

--- Grep Checks ---
Pattern 'git add -A' in src/jev/:
  None found (clean)
Pattern 'run_baseline_tests(' in src/jev/engine.py:
  None found in engine.py (clean)
Occurrences of 'FULL_SUITE_PASSED' in workspace.py and engine.py:
  src/jev/engine.py:4892:                verify_classification = "FULL_SUITE_PASSED"
  src/jev/workspace.py:2346:                    "outcome": TestOutcome.FULL_SUITE_PASSED.value,
  src/jev/workspace.py:5047:                        test_classification = TestOutcome.FULL_SUITE_PASSED.value
Occurrences of 'worktree_dir == self.repo_dir' in workspace.py (guard check):
  src/jev/workspace.py:1562:        if self.worktree_dir == self.repo_dir:
  src/jev/workspace.py:1599:        if self.worktree_dir == self.repo_dir:

--- Pytest Offline Suite Summary ---
SKIPPED [1] tests\test_runner_fixtures.py:361: gradle not found
SKIPPED [1] tests\test_runner_fixtures.py:361: dotnet not found
SKIPPED [1] tests\test_runner_fixtures.py:361: ctest not found
SKIPPED [1] tests\test_runner_fixtures.py:361: ruby not found
SKIPPED [1] tests\test_runner_fixtures.py:361: php not found
SKIPPED [1] tests\test_runner_fixtures.py:325: Profile 'swiftpm-test' not supported on win32 (platforms: ['linux', 'darwin'])
SKIPPED [1] tests\test_runner_fixtures.py:361: mix not found
SKIPPED [1] tests\test_runner_fixtures.py:325: Profile 'xcode-test' not supported on win32 (platforms: ['darwin'])
SKIPPED [1] tests\test_runner_fixtures.py:361: terraform not found
SKIPPED [1] tests\test_runner_fixtures.py:361: ansible-lint not found
SKIPPED [1] tests\test_runner_fixtures.py:361: helm not found
980 passed, 24 skipped, 2 deselected, 2 warnings in 776.66s (0:12:56)
```
