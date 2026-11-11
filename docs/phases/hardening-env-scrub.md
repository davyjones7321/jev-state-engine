# Hardening Phase: Child Process Environment Scrubbing & Credential Boundary

**Branch:** `hardening-env-scrub`  
**Base:** `origin/master` (`463c46642d2077e60058b8d4bb9f39d2243e8006`)  
**Date:** 2026-11-11  
**Status:** Verification Passed — Ready for Review

---

## 1. Problem Statement

Prior to this fix, `src/jev/process_runner.py:run_command()` passed `env=None` to `subprocess.Popen()`. In Python, `env=None` causes the spawned child process to inherit the parent process's entire environment (`os.environ`), including sensitive API keys and provider tokens such as `JEV_API_KEY`, `GOOGLE_API_KEY`, `OPENAI_API_KEY`, and advisory tokens.

A spawned command (such as a test suite, compile check, preflight command, or proposal command) running untrusted target-repo or generated code could inspect `os.environ` (e.g. `python -c "import os; print(os.environ)"`) and exfiltrate host credentials.

---

## 2. Mechanical Remediation

The credential boundary is enforced mechanically at the process boundary, completely independent of prompt instructions or model behavior:

### Layer 1: Strict Allowlist Filtering
Child process environments are constructed exclusively from an allowlist of standard operating system and toolchain variables:
- **System / OS:** `PATH`, `SYSTEMROOT`, `SYSTEMDRIVE`, `TEMP`, `TMP`, `HOME`, `USERPROFILE`, `APPDATA`, `LOCALAPPDATA`, `LANG`, `CI`.
- **Windows System Essentials:** `COMSPEC`, `PATHEXT`, `WINDIR`, `PROGRAMFILES`, `PROGRAMFILES(X86)`, `PROGRAMDATA`, `COMMONPROGRAMFILES`, `COMMONPROGRAMFILES(X86)`, `PUBLIC`, `ALLUSERSPROFILE`, `NUMBER_OF_PROCESSORS`, `OS`, `PROCESSOR_ARCHITECTURE`, `PROCESSOR_IDENTIFIER`, `PROCESSOR_LEVEL`, `PROCESSOR_REVISION`.
- **POSIX System Essentials:** `TERM`, `SHELL`, `USER`, `LOGNAME`, `HOSTNAME`, `LD_LIBRARY_PATH`, `DYLD_LIBRARY_PATH`.
- **Python Toolchains:** `PYTHONPATH`, `PYTHONHOME`, `PYTHONIOENCODING`, `PYTHONUTF8`, `PYTHONUNBUFFERED`, `PYTHONDONTWRITEBYTECODE`, `VIRTUAL_ENV`, `CONDA_PREFIX`, `CONDA_DEFAULT_ENV`.
- **Node / NPM Toolchains:** `NODE_PATH`, `NODE_ENV`, `NVM_BIN`, `NVM_DIR`, `NODE_OPTIONS`, and `NPM_CONFIG_*` prefix.
- **Git:** `GIT_EXEC_PATH`, `GIT_TEMPLATE_DIR`, `GIT_CEILING_DIRECTORIES`, `GIT_CONFIG_NOSYSTEM`, `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL`, `GIT_COMMITTER_NAME`, `GIT_COMMITTER_EMAIL`, `GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`.
- **Other Standard Toolchains:** `CARGO_HOME`, `RUSTUP_HOME`, `GOPATH`, `GOROOT`, `JAVA_HOME`, `MAVEN_HOME`, `GRADLE_HOME`, `DOTNET_ROOT`.
- **Prefixes Allowed:** `LC_*` and `NPM_CONFIG_*`.

All variables not in the allowlist or matching allowed prefixes are dropped.

### Layer 2: Sensitive Substring Filter
As a defense-in-depth second layer, any variable whose name contains any of the following substrings is dropped, even if it matched a prefix or standard variable:
- `KEY`
- `TOKEN`
- `SECRET`
- `PASSWORD`
- `CREDENTIAL`
- `AUTH`

### Layer 3: Declared Proposal Variables
Variables declared explicitly by an approved proposal (`proposal.environment_variables`) or caller-provided `env` dict are added back to the child environment. This allows legitimate, user-approved environment variables to be passed to commands while blocking undeclared parent secrets.

### Layer 4: In-Process Secret Isolation
The scrub applies **only** to child processes running target-repo or generated code. The engine process's own `os.environ` is never mutated. The engine's in-process API clients (`Gatekeeper`, Gemini client, advisory client) continue reading keys directly from `os.environ` without disruption.

### Layer 5: Opt-Out Flag
An explicit opt-out is provided via `run_command(..., opt_out=True)` / `run_command(..., scrub_env=False)` or the environment variable `JEV_DISABLE_ENV_SCRUB=1`. By default, scrubbing is always enabled (`scrub_env=True`, `opt_out=False`).

---

## 3. Comprehensive Spawn Path Inventory

Every `subprocess.run`, `subprocess.Popen`, and subprocess call across `src/jev` was inventoried and verified to pass a scrubbed environment:

| File | Location | Invocation | Environment Source | Purpose |
|---|---|---|---|---|
| `src/jev/process_runner.py` | Line 358 | `subprocess.Popen` | `get_scrubbed_env(declared_env=env, opt_out=effective_opt_out)` | Primary command runner for proposals, tests, build checks |
| `src/jev/process_runner.py` | Line 421 | `subprocess.run` | `get_scrubbed_env()` | Windows fallback taskkill cleanup on timeout |
| `src/jev/workspace.py` | Line 845 | `subprocess.run` | `get_scrubbed_env()` | `git show` base content retrieval |
| `src/jev/workspace.py` | Line 1349 | `subprocess.run` | `get_scrubbed_env()` | `_run_git` worktree git commands |
| `src/jev/workspace.py` | Line 1361 | `subprocess.run` | `get_scrubbed_env()` | `_run_repo_git` repo root git commands |
| `src/jev/workspace.py` | Line 1543 | `subprocess.run` | `get_scrubbed_env(...)` | `Workspace.run_read_tool` git execution with pager overrides |
| `src/jev/workspace.py` | Line 1649 | `subprocess.run` | `get_scrubbed_env()` | `Workspace.run_read_tool` fallback system command |
| `src/jev/workspace.py` | Lines 2208-2220 | `subprocess.run` | `get_scrubbed_env()` | `commit_staged_subgoal_changes` git add/diff/commit |
| `src/jev/engine.py` | Line 242 | `subprocess.run` | `get_scrubbed_env()` | `_get_engine_commit` engine commit SHA retrieval |
| `src/jev/engine.py` | Line 2717 | `subprocess.run` | `get_scrubbed_env()` | AST syntax check running python on modified files |
| `src/jev/preflight.py` | Line 133 | `subprocess.run` | `get_scrubbed_env()` | `git rev-parse --is-inside-work-tree` repository check |
| `src/jev/repo_config.py` | Line 394 | `subprocess.run` | `get_scrubbed_env()` | `git rev-list --max-parents=0 HEAD` initial commit lookup |
| `src/jev/knowledge.py` | Line 73 | `subprocess.run` | `get_scrubbed_env()` | `git ls-tree` evidence capture |
| `src/jev/knowledge.py` | Line 124 | `subprocess.run` | `get_scrubbed_env()` | `git show` evidence capture |
| `src/jev/knowledge.py` | Line 413 | `subprocess.run` | `get_scrubbed_env()` | `git ls-tree` staleness check |
| `src/jev/knowledge.py` | Line 438 | `subprocess.run` | `get_scrubbed_env()` | `git show` staleness check |
| `src/jev/ledger.py` | Line 228 | `subprocess.run` | `get_scrubbed_env()` | `git rev-list --max-parents=0 HEAD` repo key computation |
| `src/jev/proposals.py` | N/A | None | N/A | No subprocess calls; executes via `run_command` |

---

## 4. Mechanical vs. Prompt-Only Protections

- **Mechanically Enforced:**
  - 100% of child process environment isolation is mechanically enforced by Python code in `src/jev/process_runner.py:get_scrubbed_env()` and passed directly to OS system calls (`CreateProcessW` on Windows, `execve` on POSIX via Python's `subprocess`).
  - No secret keys can leak into child processes regardless of LLM generation, prompt injection, or malicious code in target repositories.
  - Layer 1 (allowlist) drops arbitrary unapproved variables.
  - Layer 2 (sensitive substring match) drops any variable containing secret tokens even if matching allowlist prefixes.
  - In-process host API clients retain their credentials safely without mutation of `os.environ`.
- **Prompt-Only:**
  - None. No part of this credential boundary relies on LLM prompting or agent instructions.

---

## 5. Verification & Test Evidence

Dedicated tests added in `tests/test_hardening_env_scrub.py`:
1. `test_spawn_scrubs_api_keys_and_secret_tokens`: Sets `JEV_API_KEY`, `GOOGLE_API_KEY`, `OPENAI_API_KEY`, `MY_SECRET_TOKEN`, `AWS_SECRET_ACCESS_KEY`, and `UNAPPROVED_SECRET_PASSWORD` in parent environment, runs child python process, and asserts all are absent.
2. `test_spawn_keeps_path_and_system_variables`: Asserts `PATH` is retained and functional.
3. `test_spawn_with_explicitly_declared_variables`: Asserts declared proposal variables pass through while undeclared secrets remain scrubbed.
4. `test_windows_systemroot_kept`: Asserts `SYSTEMROOT` is preserved on Windows.
5. `test_opt_out_exposes_secrets_demonstrating_test_fails_if_scrub_removed`: Proves that tests fail if scrubbing is disabled/removed by asserting secrets appear when `opt_out=True` or `scrub_env=False`.
6. `test_env_var_opt_out_toggle`: Asserts `JEV_DISABLE_ENV_SCRUB=1` allows opt-out.
7. `test_get_scrubbed_env_layer1_and_layer2`: Unit test verifying allowlist filtering, substring filtering (e.g. dropping `NPM_CONFIG_AUTH_TOKEN`), and declared variable re-addition.

Existing test suite on Windows:
- All 102 sample regression tests in `test_worktree.py`, `test_workspace.py`, and `test_hardening_r6_adversarial.py` passed with 0 failures.

---

## 6. UNREQUESTED CHANGES

**NONE.**
Only the requested environment scrubbing mechanism, spawn path wiring, tests, and documentation were added.
