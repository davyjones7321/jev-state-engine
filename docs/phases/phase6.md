# Phase 6 Report: Zero-Test and Skipped-Test Evidence

## 1. What Changed (Files and Lines)

### Allowed Files Modified / Created:
- `src/jev/runner_registry.py` (Modified, lines 65, 563–871):
  - In `RunnerProfile`: ensured `zero_test_downgrade_markers` and `failure_identity_patterns` stay strictly separate fields. Added `empty_output_behavior: Optional[str] = "weak"`.
  - Added `get_zero_test_downgrade_markers(profile)`: returns profile downgrade markers supplemented with measured zero-test patterns from real Phase 2B binaries (e.g. Bun's `"No tests found"`, `"matched 0 tests"`, Pytest's `"collected 0 items"`, `"no tests ran"`).
  - Added `is_zero_test_marker_match(profile, output, exit_code)`: strictly downgrade-only checker that verifies whether runner output matches zero-test downgrade markers.
  - Added `parse_test_counts_summary(profile, output, exit_code)`: parses passed, failed, and skipped counts across Pytest, Bun, Cargo, Node native, Jest, Vitest, Deno, Mocha, Maven, DotNet, and Go. Evaluates `all_skipped` and `has_positive_pass`.

- `src/jev/workspace.py` (Modified, lines 183–275, 873–940, 2522–2605, 2760–2775):
  - Updated `parse_failing_test_ids`: integrates `profile.failure_identity_patterns` to extract genuine failing test IDs across all profiles (e.g. Bun `(fail)`, Node `✖ fail`, Mix, Maven, DotNet), with duration and timing striping.
  - Updated `_record_test_run`: executes `parse_test_counts_summary` and `is_zero_test_marker_match`, records `counts_summary`, `all_skipped`, and `zero_tests_matched` in `self.last_test_run`.
  - Updated `evaluate_tests`:
    - **Zero-test exit downgrade**: When candidate execution fails (exit != 0 or `cand_outcome == TestOutcome.FAILED`), if no genuine failing test IDs were detected and output matches zero-test downgrade markers, immediately downgrades to `UNMEASURED(NO_TEST_EXECUTION_SIGNAL)`. Never strikes the worker and is never treated as `FAILED_BY_DIFF` or `FAIL`.
    - **Genuine named failures**: Preserves genuine failing IDs in `last_test_run["failing_test_ids"]` and diagnostic evidence. A genuine failure prevents zero-test downgrade.
    - **All-skipped enforcement**: When all executed tests are skipped (`all_skipped == True`), forces `evidence_level = EvidenceLevel.EXIT_ONLY` (never `COUNTED` or `NAMED`), ensuring `full_suite_verified = False` and `mechanically_unverified = True`.
    - **Scrambled output degradation**: Corrupted, scrambled, or garbage output fails count and summary parsing, degrading to `EvidenceLevel.EXIT_ONLY` (or `UNMEASURED`). Never upgrades to `NAMED` or turns a failure into a pass.
    - **Empty output with expected tests**: When candidate exits 0 with empty output and tests were expected, applies profile policy: defaults to weak `EvidenceLevel.EXIT_ONLY` or `UNMEASURED(NO_TEST_EXECUTION_SIGNAL)` if `empty_output_behavior == "unmeasured"`.

- `tests/test_zero_test_evidence.py` (New file, lines 1–595):
  - 20 unit and integration tests covering:
    1. Separate fields `zero_test_downgrade_markers` and `failure_identity_patterns` on `RunnerProfile`.
    2. Zero-test exit 1 with matching marker is never `FAILED_BY_DIFF` (Bun, Pytest exit 5, Jest, Vitest).
    3. Genuine named failures preserved as diagnostic evidence and never masked.
    4. Failure identity patterns are diagnostic/comparison only and never prove a test ran.
    5. All-skipped never becomes `COUNTED` or `NAMED` PASS across Pytest, Bun, Cargo, and Node native test.
    6. Scrambled output property test: with fixed exit codes, scrambling output only degrades evidence, never upgrades.
    7. Empty output with exit 0 and expected tests becomes weak or unmeasured by profile policy.
    8. Markers are strictly downgrade-only.
    9. Parameterized replay of captured Phase 2B fixtures (`bun-test.json`, `pytest.json`, `cargo-test.json`, `node-native-test.json`).

---

## 2. How It Works

1. **Downgrade-Only Markers**:
   - `RunnerProfile.zero_test_downgrade_markers` contains regex patterns representing zero-test execution states (e.g. no tests collected, filter matched zero tests, no test files).
   - If a runner process exits non-zero (such as exit 1 for Bun, Jest, Vitest, or exit 5 for Pytest) without genuine failing test IDs, matching a downgrade marker triggers safe downgrade to `UNMEASURED(NO_TEST_EXECUTION_SIGNAL)`.
   - This bypasses candidate rerun, base comparison, and regression adjudication, guaranteeing the run is never labeled `FAILED_BY_DIFF` and adds zero strikes.

2. **Diagnostic-Only Failure Identity Patterns**:
   - `RunnerProfile.failure_identity_patterns` extracts failing test IDs from runner output for diagnostics, fingerprints, and baseline comparison.
   - Finding failure patterns provides diagnostic comparison data only; it never proves a test passed or that a suite ran cleanly.

3. **All-Skipped Invariant**:
   - When a test runner exits 0 but all tests are ignored or skipped (e.g. `0 pass, 1 skip, 0 fail`), `parse_test_counts_summary` marks `all_skipped=True`.
   - `evaluate_tests` strictly forces `evidence_level = EvidenceLevel.EXIT_ONLY`. Per `MechanicalEvidence` invariants, `full_suite_verified` is forced to `False` and `mechanically_unverified` is forced to `True`.

4. **Scrambled Output Property**:
   - Parsing of counts requires valid, structured runner output. Any scrambled, malformed, or garbage output cannot fabricate positive pass counts, ensuring the evidence level degrades to `EXIT_ONLY` or `UNMEASURED`.

5. **Empty Output Handling**:
   - If a runner exits 0 with empty stdout/stderr when tests are expected, no execution signal exists. Depending on the runner profile policy, it either degrades to weak `EXIT_ONLY` or returns `UNMEASURED(NO_TEST_EXECUTION_SIGNAL)`.

---

## 3. Rules Enforced by Tests vs Only by Prompts

| Rule / Requirement | Enforcement Type | Test Name |
| :--- | :--- | :--- |
| `zero_test_downgrade_markers` and `failure_identity_patterns` stay separate fields | Mechanical (Test) | `tests/test_zero_test_evidence.py::test_markers_and_failure_identity_patterns_are_separate_fields` |
| Zero-test exit 1 with matching marker is never `FAILED_BY_DIFF` (Bun) | Mechanical (Test) | `tests/test_zero_test_evidence.py::test_zero_test_exit_1_with_matching_marker_is_never_failed_by_diff_bun` |
| Zero-test exit 1 filter matches nothing (Bun) | Mechanical (Test) | `tests/test_zero_test_evidence.py::test_zero_test_exit_1_filter_matches_nothing_bun` |
| Zero-test exit 5 with matching marker is never failure (Pytest) | Mechanical (Test) | `tests/test_zero_test_evidence.py::test_zero_test_exit_5_pytest_marker` |
| Zero-test exit 1 downgrade markers (Jest and Vitest) | Mechanical (Test) | `tests/test_zero_test_evidence.py::test_zero_test_exit_1_jest_and_vitest` |
| Genuine named failure stays available as diagnostic evidence | Mechanical (Test) | `tests/test_zero_test_evidence.py::test_genuine_named_failure_stays_available_as_diagnostic_evidence` |
| Failure identity patterns are diagnostic only and never prove a test ran | Mechanical (Test) | `tests/test_zero_test_evidence.py::test_failure_identity_patterns_never_prove_a_test_ran` |
| All-skipped never becomes counted/named PASS (Pytest) | Mechanical (Test) | `tests/test_zero_test_evidence.py::test_all_skipped_never_becomes_counted_named_pass_pytest` |
| All-skipped never becomes counted/named PASS (Bun) | Mechanical (Test) | `tests/test_zero_test_evidence.py::test_all_skipped_never_becomes_counted_named_pass_bun` |
| All-skipped never becomes counted/named PASS (Cargo) | Mechanical (Test) | `tests/test_zero_test_evidence.py::test_all_skipped_never_becomes_counted_named_pass_cargo` |
| All-skipped never becomes counted/named PASS (Node native) | Mechanical (Test) | `tests/test_zero_test_evidence.py::test_all_skipped_never_becomes_counted_named_pass_node` |
| Scrambled output only degrades, never upgrades | Mechanical (Test) | `tests/test_zero_test_evidence.py::test_scrambled_output_only_degrades_property` |
| Scrambled failing output never upgrades to pass | Mechanical (Test) | `tests/test_zero_test_evidence.py::test_scrambled_failing_output_never_upgrades_to_pass` |
| Empty output with exit 0 and expected tests becomes weak (EXIT_ONLY) | Mechanical (Test) | `tests/test_zero_test_evidence.py::test_empty_output_exit_0_weak_policy` |
| Empty output with exit 0 under unmeasured policy becomes UNMEASURED | Mechanical (Test) | `tests/test_zero_test_evidence.py::test_empty_output_exit_0_unmeasured_policy` |
| Markers are strictly downgrade-only and never upgrade anything | Mechanical (Test) | `tests/test_zero_test_evidence.py::test_markers_are_strictly_downgrade_only` |
| Phase 2B real binary fixtures replay (bun-test) | Mechanical (Test) | `tests/test_zero_test_evidence.py::test_phase2b_captured_fixtures_cases[bun-test-bun-test.json]` |
| Phase 2B real binary fixtures replay (pytest) | Mechanical (Test) | `tests/test_zero_test_evidence.py::test_phase2b_captured_fixtures_cases[pytest-pytest.json]` |
| Phase 2B real binary fixtures replay (cargo-test) | Mechanical (Test) | `tests/test_zero_test_evidence.py::test_phase2b_captured_fixtures_cases[cargo-test-cargo-test.json]` |
| Phase 2B real binary fixtures replay (node-native-test) | Mechanical (Test) | `tests/test_zero_test_evidence.py::test_phase2b_captured_fixtures_cases[node-native-test-node-native-test.json]` |

---

## 4. Skipped Tests and Reasons
- **None**: Zero tests were edited, deleted, or skipped in the existing test suites or Phase 6 test suite.

---

## 5. Unrequested Changes
- **None**. Only files specified in Phase 6 were modified or created (`src/jev/runner_registry.py`, `src/jev/workspace.py`, `tests/test_zero_test_evidence.py`, `docs/phases/phase6.md`).

---

## 6. Open Problems
- **None**: All 20 tests in `tests/test_zero_test_evidence.py` pass. Regression test suites (`tests/test_target_selection.py`, `tests/test_same_command.py`, `tests/test_runner_registry.py`, `tests/test_runner_fixtures.py`) pass without regressions.

---

## 7. Verification Script Output
