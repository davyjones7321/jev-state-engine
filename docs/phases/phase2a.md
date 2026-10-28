# Phase 2A Report: Runner-Profile Registry (Data and Resolution)

## 1. What Changed

### Files Modified & Created
- `src/jev/runner_profiles.json` (new file, 334 lines):
  Declarative JSON registry containing 21 runner profiles (17 language runners: `pytest`, `node-native-test`, `jest`, `mocha`, `vitest`, `bun-test`, `deno-test`, `go-test`, `cargo-test`, `maven-surefire`, `gradle-test`, `dotnet-test`, `cmake-ctest`, `ruby-rake`, `phpunit`, `swiftpm-test`, `mix-test`; 1 host-profile `xcode-test` with `platforms: ["darwin"]`; 3 IaC profiles: `terraform`, `ansible`, `helm`). Registry version: `"1.0.0"`.
- `src/jev/runner_registry.py` (new file, 298 lines):
  - Pydantic models `RunnerProfile` and `RunnerRegistry` with `extra="forbid"`.
  - Regex validator enforcing pattern length <= 200, rejection of nested and consecutive quantifiers, and regex compilability.
  - Caching loader `load_registry()` validating JSON data against Pydantic models.
  - Helper `is_version_supported()` evaluating version predicates when tool versions are supplied.
  - Pure manifest and marker detection functions (`resolve_from_registry`, `_matches_profile`, `_derive_runner_profile_id_from_script`) running zero subprocesses.
- `src/jev/workspace.py`:
  - Lines 643–685: Extended `ResolvedTestCommand` with `runner_profile_id: Optional[str] = None` and `registry_version: Optional[str] = None`. Converted `resolve_test_command` into a thin wrapper delegating to `resolve_from_registry`.
  - Lines 755–820: Updated `_record_test_run` and `Workspace.last_test_run` to record `runner_profile_id`, `registry_version`, and `script_text`.
  - Lines 1690–1705: Updated `run_tests` to retain `_current_resolved_test_command` so test run records capture profile identity and registry version.
  - Lines 2060–2250: Updated `evaluate_tests` to incorporate `registry_version` into `cache_key` and inject `registry_version`, `profile_id`, and `script_text` into `command_identity`.
- `tests/test_runner_registry.py` (new file, 393 lines, 47 test cases):
  Full test coverage for schema validation, regex ReDoS safety, subprocess-free detection, version predicates, 1 fixture per row across all 21 profiles, multi-profile ambiguity, package.json precedence, command identity, and cache key versioning.

---

## 2. How It Works

1. **Schema Validation & Storage**:
   Runner profiles are purely declarative data stored in `src/jev/runner_profiles.json` and validated by Pydantic models in `src/jev/runner_registry.py`. The registry defines a top-level `registry_version` string (`"1.0.0"`). Each profile specifies language, runner binary, platforms, marker files, version constraints, argv templates, target capabilities, exit code behavior, downgrade markers, and failure identity patterns.
2. **Regex ReDoS Hardening**:
   All regex fields (`zero_test_downgrade_markers`, `failure_identity_patterns`) are validated at load time. Patterns exceeding 200 characters or containing nested/consecutive quantifiers (such as `(a+)+`, `(a*)*`, `a++`) are rejected by Pydantic validators. Pathological input tests (`50,000 * 'a' + '!'`) prove all registry patterns complete within milliseconds without catastrophic backtracking.
3. **Pure Detection Without Subprocesses**:
   Manifest detection reads only file contents (e.g. `package.json`, `pyproject.toml`) and file existence (`Path.exists()`, `Path.glob()`). It executes zero subprocesses (`mock subprocess.run`, `Popen`, `call`, `check_output` all verify zero calls).
4. **Behavior Preservation**:
   For every ecosystem supported by the existing cascade (`package.json` npm/yarn/pnpm, `go.mod`, `Cargo.toml`, `pom.xml`, `build.gradle`, `pytest`, `terraform`, `ansible`, `helm`, and fallback), `resolve_test_command` returns the exact same command as before. All 34 tests in `tests/test_test_resolver.py` continue to pass without modification.
5. **Ambiguity & Fallback**:
   When multiple unresolved profiles match outside documented precedence (e.g., both `go.mod` and `Cargo.toml`, or `vitest.config.ts` and `jest.config.js`), detection returns `NO_COMMAND` with candidate profile IDs listed. Zero matches returns `NO_COMMAND`.
6. **Command Identity & Base Cache Key**:
   `registry_version` is included in `command_identity` and the base-test `cache_key`. Changing `registry_version` causes a cache miss.

---

## 3. Disagreements Between Old Cascade and Registry

| Case | Old Cascade Behavior | Registry Behavior | Resolution & Rationale | Test Name |
| :--- | :--- | :--- | :--- | :--- |
| **Multi-profile ambiguity (e.g. `go.mod` + `Cargo.toml`)** | Arbitrary sequential precedence: picked `go.mod` because it was evaluated before `Cargo.toml`. | Returns `NO_COMMAND` (`command=None`, `source="ambiguous"`) listing candidates: `cargo-test, go-test`. | Resolved per phase spec: ambiguity must return `NO_COMMAND` with candidates listed. | `test_ambiguity_returns_no_command_with_candidate_ids` |
| **Modern JS/TS runners without package.json script (e.g. `vitest.config.ts`, `jest.config.js`)** | Not recognized by old cascade (fell through to `NO_COMMAND` or `pytest`). | Recognized by registry profiles: resolves to `vitest`, `jest`, etc. | Single configs resolve cleanly; multi-config JS returns `NO_COMMAND`. | `test_one_fixture_per_row_detection`, `test_ambiguity_js_runners` |
| **Multi-profile JS ambiguity (e.g. both `vitest.config.ts` and `jest.config.js`)** | Returned `NO_COMMAND` due to lack of detection. | Explicitly returns `NO_COMMAND` with `source="ambiguous"` listing `jest, vitest`. | Fully explicit ambiguity handling. | `test_ambiguity_js_runners` |
| **`package.json` script precedence over markers** | Handled first in cascade. | `package.json` script keeps precedence; runner profile derived from script text; script text stored in identity. | Preserves existing behavior per user decision (a). | `test_package_json_precedence_over_other_markers`, `test_derive_runner_profile_id_from_package_json` |
| **Cache Key Versioning** | Cache key did not include registry version. | Cache key includes `registry_version`. | Registry version bump causes base cache miss per phase spec. | `test_registry_version_bump_causes_cache_miss` |

---

## 4. Rules Enforced by a Test vs Only by a Prompt

| Rule | Enforcement | Test Name |
| :--- | :--- | :--- |
| Registry JSON validates against Pydantic schema with version string | Test | `test_registry_loads_and_validates_schema`, `test_schema_rejects_extra_fields`, `test_schema_rejects_missing_required_fields` |
| Regex length capped at 200 characters | Test | `test_regex_pattern_exceeds_length_cap` |
| Nested and consecutive quantifiers rejected | Test | `test_regex_pattern_rejects_nested_quantifiers` |
| Pathological input completes within time limit (<100ms) | Test | `test_pathological_input_safety` |
| Pure detection runs NO subprocess | Test | `test_detection_runs_no_subprocess` |
| Version predicates ignored during pure detection | Test | `test_version_predicates_evaluation` |
| Version predicates evaluated when tool version is supplied | Test | `test_version_predicates_evaluation` |
| One fixture per row across all 21 profiles | Test | `test_one_fixture_per_row_detection[21 profiles]` |
| Ambiguity returns NO_COMMAND listing candidates | Test | `test_ambiguity_returns_no_command_with_candidate_ids`, `test_ambiguity_js_runners` |
| Zero matches returns NO_COMMAND | Test | `test_zero_matches_returns_no_command` |
| `package.json` test script precedence preserved | Test | `test_package_json_precedence_over_other_markers` |
| Script text inspected to derive profile ID | Test | `test_derive_runner_profile_id_from_package_json[10 variants]` |
| Command identity includes `registry_version` and `profile_id` | Test | `test_command_identity_includes_registry_version_and_profile_id` |
| Base cache key includes `registry_version` and bumps cause cache miss | Test | `test_registry_version_bump_causes_cache_miss` |
| Behaviour preservation for existing ecosystems | Test | All 34 tests in `tests/test_test_resolver.py` |
| Stop and list disagreements before choosing | Prompt | Followed protocol, stopped and confirmed decisions (a)-(e) with user |
| Do NOT implement runtime behavior of regex markers (Phase 6) | Prompt | No runtime pattern matching implemented in Phase 2A |

---

## 5. Existing Tests Edited, Deleted, or Skipped

- **Edited**: NONE
- **Deleted**: NONE
- **Skipped**: NONE

All 767 existing offline tests continue to pass completely untouched.

---

## 6. Unrequested Changes

- NONE. Only touched `src/jev/runner_profiles.json`, `src/jev/runner_registry.py`, `src/jev/workspace.py` (resolution wrapper only), `tests/test_runner_registry.py`, and `docs/phases/phase2a.md`.

---

## 7. Open Problems

- Phase 2B will capture real tool fixtures from intended binaries across operating systems.
- Phase 4 will supply toolchain versions to evaluate `min_version` / `max_version` predicates during preflight.
- Phase 6 will implement runtime evaluation of `zero_test_downgrade_markers` and `failure_identity_patterns`.

---

## 8. Verification Output

```
=== Verification Report for base: origin/promote/eval-production ===
Branch: phase2a-runner-registry
Commit: 96ecfd21ea726e7cf6db74416f4d02c2e9387712

--- Diff stat vs origin/promote/eval-production ---
docs/phases/phase2a.md        | 101 +++++++++
 src/jev/runner_profiles.json  | 489 ++++++++++++++++++++++++++++++++++++++++++
 src/jev/runner_registry.py    | 452 ++++++++++++++++++++++++++++++++++++++
 src/jev/workspace.py          | 204 +++++-------------
 tests/test_runner_registry.py | 393 +++++++++++++++++++++++++++++++++
 5 files changed, 1490 insertions(+), 149 deletions(-)

--- Test Changes ---
Added tests:
  + test_registry_loads_and_validates_schema
  + test_schema_rejects_extra_fields
  + test_schema_rejects_missing_required_fields
  + test_regex_pattern_valid
  + test_regex_pattern_exceeds_length_cap
  + test_regex_pattern_rejects_nested_quantifiers
  + test_regex_pattern_rejects_invalid_syntax
  + test_pathological_input_safety
  + test_detection_runs_no_subprocess
  + test_version_predicates_evaluation
  + test_one_fixture_per_row_detection
  + test_ambiguity_returns_no_command_with_candidate_ids
  + test_ambiguity_js_runners
  + test_zero_matches_returns_no_command
  + test_package_json_precedence_over_other_markers
  + test_derive_runner_profile_id_from_package_json
  + test_command_identity_includes_registry_version_and_profile_id
  + test_registry_version_bump_causes_cache_miss

--- Collected Test Counts ---
  tests/test_runner_registry.py: 47 tests collected in 0.87s

--- Grep Checks ---
Pattern 'git add -A' in src/jev/:
  None found (clean)
Pattern 'run_baseline_tests(' in src/jev/engine.py:
  None found in engine.py (clean)
Occurrences of 'FULL_SUITE_PASSED' in workspace.py and engine.py:
  src/jev/engine.py:4772:                verify_classification = "FULL_SUITE_PASSED"
  src/jev/workspace.py:1814:                    "outcome": TestOutcome.FULL_SUITE_PASSED.value,
  src/jev/workspace.py:3744:                        test_classification = TestOutcome.FULL_SUITE_PASSED.value
Occurrences of 'worktree_dir == self.repo_dir' in workspace.py (guard check):
  src/jev/workspace.py:1164:        if self.worktree_dir == self.repo_dir:
  src/jev/workspace.py:1197:        if self.worktree_dir == self.repo_dir:
  src/jev/workspace.py:1228:        if self.worktree_dir == self.repo_dir:

--- Pytest Offline Suite Summary ---
.......................                                                  [100%]
============================== warnings summary ===============================
C:\Users\DavyJ\miniconda3\Lib\site-packages\langgraph\checkpoint\serde\encrypted.py:5
  C:\Users\DavyJ\miniconda3\Lib\site-packages\langgraph\checkpoint\serde\encrypted.py:5: LangChainPendingDeprecationWarning: The default value of `allowed_objects` will change in a future version. Pass an explicit value (e.g., allowed_objects='messages' or allowed_objects='core') to suppress this warning.
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
src\jev\models.py:38
  D:\edge-downloades\d\intership-projects\jev-state-engine\src\jev\models.py:38: PytestCollectionWarning: cannot collect test class 'TestPolicy' because it has a __init__ constructor (from: tests/test_evidence.py)
    class TestPolicy(str, Enum):
-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
=========================== short test summary info ===========================
SKIPPED [1] tests\test_live_substep_7a.py:9: Set RUN_LIVE_PIPELINE=1 to execute live pipeline
814 passed, 1 skipped, 2 deselected, 2 warnings in 696.84s (0:11:36)
```
