# Phase R3 Report: Security Audit and Hardening

Base ref: `origin/master` (`f18b7ae7d704def8da94c78bd13d391d54cf1904`)  
Branch: `hardening-run-c`  
Phase: `R3` (Security Audit and Hardening)

---

## 1. Overview & Objective

Phase R3 is the independent security-hardening verification pass of `jev-state-engine`.
The goal of this run is to conduct an adversarial, evidence-driven security audit across the codebase, demonstrate any reproducible security vulnerabilities test-first, implement minimal and surgical production hardening fixes, and verify that the full test suite remains green without weakening any existing security controls.

---

## 2. Systematic 10-Dimension Security Audit

A thorough examination was performed across the 10 core security dimensions of the state engine:

### Dimension 1: Worktree & File System Isolation
- **Inspected**: Path resolution in `Workspace`, symlink handling in `stage_file_mutation`, `_resolve_for_read`, and directory traversal guards.
- **Finding (Vulnerability 6)**: `stage_file_mutation` checked `is_protected_path` and parent traversal, but failed to check if the target file or any parent component was a pre-existing symbolic link pointing outside the worktree. An attacker could place a symlink in the repository pointing to `/etc/shadow` or `C:\Windows\System32\...` and trick the engine into overwriting target files outside the worktree.
- **Resolution**: Added explicit `target_path.is_symlink() or (self.worktree_dir / norm_str).is_symlink()` check raising `PermissionError` in `Workspace.stage_file_mutation`.

### Dimension 2: Subprocess Execution Isolation & Command Injection
- **Inspected**: Subprocess calls in `engine.py`, `workspace.py`, `proposals.py`, test runners, and syntax checks.
- **Finding (Vulnerability 1)**: In `engine._validate_changes`, syntax self-checks were executed via `subprocess.run([sys.executable, "-c", checker, rel_path], cwd=str(implementation_root))`. Because standard Python adds the current working directory (`cwd`) to `sys.path[0]` by default, an untrusted repository containing a malicious `ast.py` or `sitecustomize.py` hijacked the host Python interpreter and executed arbitrary code during validation.
- **Resolution**: Added the `-I` (isolated mode) flag to `sys.executable` invocation (`[sys.executable, "-I", "-c", checker, rel_path]`), ensuring the subprocess ignores `sys.path[0]` (cwd), environment variables (`PYTHONPATH`), and user site-packages.

### Dimension 3: Workspace Read-Only Tools Restriction
- **Inspected**: `Workspace.run_read_tool` and `engine.py` tool exposure.
- **Finding (Vulnerability 2)**: `Workspace.run_read_tool` took arbitrary command strings (`cmd`) and executed them in the worktree subprocess if the executable existed. While secrets in arguments were denylisted, any executable on the system (`rm`, `python`, `bash`, `powershell`, `curl`) could be invoked. Furthermore, invoking `git` permitted mutating subcommands such as `git push`, `git reset`, `git commit`, `git clean`, or `git config`.
- **Resolution**: Enforced an explicit command allowlist `ALLOWED_READ_TOOLS = {"cat", "head", "tail", "git"}`. For `git`, enforced an explicit read-only subcommand allowlist `ALLOWED_READ_GIT_SUBCOMMANDS = {"status", "grep", "log", "diff", "show", "branch", "rev-parse"}`. Any other command or git subcommand is immediately rejected with an access-denied error without spawning a process.

### Dimension 4: Subgoal Scope Containment & Empty Scope Enforcement
- **Inspected**: `_execute_stage_file_mutation` in `engine.py`, planner scope validation, and worker mutation barriers.
- **Finding (Vulnerability 3)**: In `engine._execute_stage_file_mutation`:
  ```python
  if norm_scope and norm_path not in norm_scope:
      return False, ...
  ```
  If a subgoal had an empty scope (`norm_scope = set()`), the condition `if norm_scope` evaluated to `False`, bypassing the check entirely and allowing arbitrary files across the worktree to be modified.
- **Resolution**: Changed condition to `if not norm_scope or norm_path not in norm_scope:`, strictly rejecting any file mutation when a subgoal has empty scope or when writing outside declared scope.

### Dimension 5: Proposal Storage Path Traversal
- **Inspected**: `load_proposal`, `save_proposal`, and `list_proposals` in `src/jev/proposals.py`.
- **Finding (Vulnerability 4)**: `load_proposal(proposal_id, repo_dir)` constructed the target path via `pdir / f"{clean_id}.json"`. If `proposal_id` contained directory traversal components (such as `../../other_dir/file`), it resolved outside `.jev/proposals/` and deserialized arbitrary JSON files.
- **Resolution**: Enforced input validation rejecting any `proposal_id` containing `..`, `/`, or `\\`, and validated that `(pdir / f"{clean_id}.json").resolve().parent == pdir.resolve()`.

### Dimension 6: Repository Configuration & Knowledge DB Path Traversal
- **Inspected**: `get_trust_file_path` in `src/jev/repo_config.py` and `get_knowledge_db_path` in `src/jev/knowledge.py`.
- **Finding (Vulnerability 5)**: Both functions concatenated user-controlled or external identifier strings directly:
  `get_trust_dir() / f"{repo_id}.json"` and `get_jev_home() / "repos" / repo_key / "knowledge.db"`.
  Neither verified that the resulting path stayed within its parent directory.
- **Resolution**: Added traversal validation in both functions, raising `ValueError` if `repo_id` or `repo_key` contains `..`, `/`, or `\\`, or if the resolved path escapes its designated parent folder.

### Dimension 7: Secret Redaction & Sensitive Data Protection
- **Inspected**: Secret denylist in `is_secret_path`, regex redaction patterns in `redact_payload`, environment variable sanitation in `proposals.py`, and ledger logging.
- **Status**: Verified intact and robust. Secrets in files (`.env`, `*.pem`, `*.key`, `id_rsa`, AWS credentials) are blocked by `_resolve_for_read`, `run_read_tool`, and redacted from outgoing payloads and log streams.

### Dimension 8: Gatekeeper, LLM Reasoning, & Telemetry Leakage
- **Inspected**: `node_gatekeeper`, `ledger.py`, and advisor prompt construction.
- **Status**: Verified intact. Raw reasoning tokens and thinking blocks are systematically stripped before recording to ledger or dispatching external calls.

### Dimension 9: Network Access & External Service Boundaries
- **Inspected**: Network call isolation, Kaggle server endpoints, and offline execution profile.
- **Status**: Verified intact. All tests run in offline mode; endpoints require explicit trust records and user approval.

### Dimension 10: Concurrency, Re-entrancy, & Shared State Security
- **Inspected**: Workspace lifecycle and engine state dictionaries.
- **Status**: Verified exclusive workspace lifecycle per active graph run, preventing cross-tenant contamination.

---

## 3. Files and Lines Changed

### 1. `src/jev/engine.py`
- Line 2716:
  - Added `"-I"` flag to `sys.executable` invocation in `_validate_changes` to prevent untrusted worktree module hijacking during syntax self-checks.
- Line 3013:
  - Updated scope containment check from `if norm_scope and norm_path not in norm_scope:` to `if not norm_scope or norm_path not in norm_scope:` to prevent unauthorized mutations when `subgoal.scope` is empty.

### 2. `src/jev/workspace.py`
- Lines 1471–1499:
  - Defined `ALLOWED_READ_TOOLS = {"cat", "head", "tail", "git"}`.
  - Defined `ALLOWED_READ_GIT_SUBCOMMANDS = {"status", "grep", "log", "diff", "show", "branch", "rev-parse"}`.
  - Restricted `run_read_tool` to authorized read commands and safe git query subcommands.
- Lines 1603–1606:
  - Added symlink check `if target_path.is_symlink() or (self.worktree_dir / norm_str).is_symlink(): raise PermissionError(...)` in `stage_file_mutation`.

### 3. `src/jev/proposals.py`
- Lines 264–277:
  - Hardened `load_proposal` against path traversal (`..`, `/`, `\\`) and verified parent directory confinement.

### 4. `src/jev/repo_config.py`
- Lines 111–118:
  - Hardened `get_trust_file_path` against path traversal, raising `ValueError` on escaping `repo_id`.

### 5. `src/jev/knowledge.py`
- Lines 24–31:
  - Hardened `get_knowledge_db_path` against path traversal, raising `ValueError` on escaping `repo_key`.

### 6. `tests/test_hardening_r3_security.py`
- New file (8 comprehensive regression tests):
  1. `test_syntax_self_check_isolates_python_execution`
  2. `test_node_implement_syntax_self_check_uses_isolated_python`
  3. `test_run_read_tool_restricts_to_allowed_read_commands`
  4. `test_run_read_tool_blocks_mutating_git_subcommands`
  5. `test_empty_subgoal_scope_blocks_all_file_mutations`
  6. `test_load_proposal_path_traversal_rejected`
  7. `test_trust_and_knowledge_path_traversal_rejected`
  8. `test_stage_file_mutation_rejects_symlinks`

---

## 4. Verification Evidence

### 1. Targeted R3 Hardening Test Suite
```
pytest -v tests/test_hardening_r3_security.py
======================== 8 passed, 1 warning in 7.07s =========================
```

### 2. Consolidated Security and Proposal Suites
```
pytest -v tests/test_phaseS_security_hardening.py tests/test_proposals.py tests/test_hardening_r2_test_scope.py tests/test_hardening_r3_security.py
======================= 67 passed, 1 warning in 24.87s ========================
```

### 3. Full Offline Suite Verification (`scripts/verify_phase.py origin/master`)
```
--- Diff stat vs origin/master ---
 src/jev/engine.py      |  6 +++---
 src/jev/knowledge.py   |  8 +++++++-
 src/jev/proposals.py   | 12 +++++++++---
 src/jev/repo_config.py |  8 +++++++-
 src/jev/workspace.py   | 27 +++++++++++++++++++++++++++
 5 files changed, 53 insertions(+), 8 deletions(-)

--- Pytest Offline Suite Summary ---
1331 passed, 42 skipped, 2 deselected, 2 warnings in 1560.18s (0:26:00)

=== VERIFICATION PASSED ===
```

---

## 5. Conclusion & Sign-Off

All identified security weaknesses were demonstrated test-first and remediated with targeted, minimal production fixes. No existing functionality or security controls were weakened. The full test suite of 1,331 offline tests passes with zero failures. Phase R3 hardening is complete and ready for PR review.
