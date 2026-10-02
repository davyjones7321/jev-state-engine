# Teammate Testing Guide: Jev State Engine

Welcome to the Jev State Engine testing guide. This document provides step-by-step instructions for teammates to verify, test, and run the engine locally against disposable repositories.

---

## 1. Introduction & Critical Sandbox Warning

The **Jev State Engine** is a deterministic, finite-state machine (FSM) runtime built on LangGraph that orchestrates autonomous software engineering tasks. It pairs an untrusted worker LLM (Gemini 3.5 Flash Lite) with strict Inversion of Control, real Git worktree isolation, multi-ecosystem mechanical validation, and TypeSafe AI's Jev System One semantic gatekeeper. Code is never committed directly to your default branch upon model generation alone; every staged diff must clear deterministic mechanical checks (Tier 0) and calibrated semantic validation (Tier 1) before landing.

> [!CAUTION]
> **CRITICAL SECURITY AND SANDBOX WARNING**
>
> The Jev State Engine executes target project build and test commands (such as `npm`, `tsc`, `go`, `cargo`, `mvn`, `gradle`, and `pytest`) directly on the host operating system.
>
> **The engine has NO OS-level sandbox or container isolation.**
>
> **Never point the engine at a production repository, a repository containing secrets or sensitive credentials (such as production `.env` files or SSH keys), or an untrusted external codebase.** Always point the engine (`--repo-dir`) at a dedicated, disposable demonstration repository created strictly for testing.

---

## 2. Prerequisites & Supported Versions

Ensure the following tools are installed and accessible on your system `PATH`:

| Requirement | Minimum / Recommended Version | Verification Command | Notes |
| :--- | :--- | :--- | :--- |
| **Python** | `3.10` or higher (`3.13+` supported) | `python --version` | Required to run the FSM runtime and pytest suite. |
| **Git** | `2.25` or higher | `git --version` | Required for Git worktree management (`git worktree`) and branch manipulation. |
| **Node.js & npm** | Node `18.0.0+`, npm `9.0.0+` | `node -v; npm -v` | Required for the TypeScript/Next.js demo target walkthrough below. |
| **PowerShell** | PowerShell `5.1+` or `PowerShell 7+` | `$PSVersionTable.PSVersion` | Primary shell target for this guide (Windows). |

> [!NOTE]
> **macOS & Linux Users:** All commands in this guide are provided in Windows PowerShell syntax. For bash or zsh, replace environment variable syntax `$env:VAR = "value"` with `export VAR="value"`, and directory path separators `\` with `/`.

---

## 3. Local Environment Setup

### Step 3.1: Clone the Repository & Enter Directory
Open PowerShell and clone the repository:
```powershell
git clone <repo-url> jev-state-engine
cd jev-state-engine
```
*(For macOS/Linux: `git clone <repo-url> jev-state-engine && cd jev-state-engine`)*

### Step 3.2: Create and Activate a Virtual Environment
Create a dedicated virtual environment to prevent package collisions:
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```
*(For macOS/Linux: `source .venv/bin/activate`)*

### Step 3.3: Install Dependencies
Install runtime and test dependencies from [requirements.txt](file:///d:/edge-downloades/d/intership-projects/jev-state-engine/requirements.txt), along with the Google Generative AI LangChain integration:
```powershell
pip install --upgrade pip
pip install -r requirements.txt langchain-google-genai
```

### Step 3.4: Configure Environment Variables (`.env`)
Create a `.env` file in the root of the repository (`jev-state-engine\.env`).

```ini
# ==============================================================================
# Jev State Engine Configuration
# ==============================================================================

# Mandatory: TypeSafe AI Jev System One Gatekeeper
JEV_API_URL=https://api.typesafe.ai/v1/systemone
JEV_API_KEY=your_typesafe_jev_api_key_here

# Optional / Required for live LLM generation:
# If omitted, unit tests still pass via mocks, but live execution defaults to llm=None
GOOGLE_API_KEY=your_gemini_api_key_here
```

**Key Classifications:**
* **`JEV_API_URL` & `JEV_API_KEY` (Mandatory):** Consumed by [src/jev/gatekeeper.py](file:///d:/edge-downloades/d/intership-projects/jev-state-engine/src/jev/gatekeeper.py) for Tier 1 semantic gating.
* **`GOOGLE_API_KEY` (Optional for unit tests / Required for live code generation):** Consumed by [src/main.py](file:///d:/edge-downloades/d/intership-projects/jev-state-engine/src/main.py) to instantiate `gemini-3.5-flash-lite`. If absent, unit tests and offline workflows run using mocks, while live runs warn that `ChatGoogleGenerativeAI` could not be initialized.

---

## 4. Run the Unit Tests

Before launching the engine against any target project, execute the full test suite to verify system integrity:

```powershell
pytest -q
```

### Expected Output
```text
........................................................................ [ 28%]
..................................................................s..... [ 57%]
........................................................................ [ 86%]
..................................                                       [100%]
249 passed, 1 skipped in ~30-60s
```

### Understanding the Skipped Test
* **`tests/test_live_substep_7c.py`**: Will display as `s` (skipped). This test requires a live integration script (`run_substep_7c.py`) and live API credentials via `pytest.importorskip("run_substep_7c")`. It is intentionally skipped during standard local unit testing.
* **All other 249 tests must pass.** If any unit test fails, see [Section 9: Troubleshooting FAQ](#9-troubleshooting-faq).

---

## 5. Create a Disposable Demo Target

To test the engine safely, create a clean, disposable TypeScript target repository in a directory **outside** the engine repository.

### Step 5.1: Initialize the Target Project (Windows PowerShell)
```powershell
# 1. Create and enter a separate disposable directory
mkdir ..\demo-target
cd ..\demo-target

# 2. Initialize npm package and install TypeScript locally
npm init -y
npm install --save-dev typescript

# 3. Create tsconfig.json
@"
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "NodeNext",
    "moduleResolution": "NodeNext",
    "strict": true,
    "noEmit": true,
    "skipLibCheck": true
  },
  "include": ["src/**/*"]
}
"@ | Set-Content -Encoding utf8 tsconfig.json

# 4. Configure package.json scripts for compile and test gates
$pkg = Get-Content package.json | ConvertFrom-Json
$pkg.scripts = [PSCustomObject]@{
  typecheck = "tsc --noEmit"
  test = "node --test"
}
$pkg | ConvertTo-Json -Depth 5 | Set-Content -Encoding utf8 package.json

# 5. Create basic source and test files
mkdir src, test
@"
export function add(a: number, b: number): number {
  return a + b;
}
"@ | Set-Content -Encoding utf8 src/index.ts

@"
const test = require('node:test');
const assert = require('node:assert');

test('add utility adds two numbers', () => {
  assert.strictEqual(1 + 1, 2);
});
"@ | Set-Content -Encoding utf8 test/index.test.js

# 6. Setup .gitignore (crucial for worktree junction safety)
@"
node_modules/
.jev-worktrees/
dist/
"@ | Set-Content -Encoding utf8 .gitignore

# 7. Initialize Git repository, commit baseline, and tag
git init -b main
git add .
git commit -m "feat: initial commit for demo target"
git tag baseline
```

*(For macOS/Linux: Replace `Set-Content` and `@"..."@` with standard `cat << 'EOF' > file` commands).*

### Step 5.2: How to Reset the Demo Target
Whenever you finish a test or want to wipe changes made by the engine:
```powershell
git reset --hard baseline
git clean -fd
```

---

## 6. Five Test Tickets to Try (Easiest to Hardest)

Run all tickets from your engine repository directory with your virtual environment active:
```powershell
cd path\to\jev-state-engine
.\.venv\Scripts\Activate.ps1
$env:PYTHONPATH = "src"
```

### Ticket A: Happy Path (Pure Utility Function)
* **Goal**: Add a simple string utility function with unit tests in `src/utils.ts` and `test/utils.test.js`.
* **Execution Command**:
  ```powershell
  python src/main.py "Add a pure capitalize(str: string): string utility function to src/utils.ts and verify it with a unit test in test/utils.test.js" --repo-dir "..\demo-target" --thread-id "ticket-a"
  ```
* **Expected Output on stdout**:
  ```text
  [SUCCESS] Ticket executed successfully. Final status: completed
  ```
* **Verification in Demo Target**:
  ```powershell
  cd ..\demo-target
  git status
  # Output: On branch main, nothing to commit, working tree clean
  git log -n 2 --oneline
  # Output shows the merged commit fast-forwarded to main
  npm run typecheck
  npm test
  cd ..\jev-state-engine
  ```

---

### Ticket B: Multi-File Refactor
* **Goal**: Touch multiple files in the same subsystem (export the utility in `src/index.ts` and update tests).
* **Execution Command**:
  ```powershell
  python src/main.py "Export the capitalize function from src/index.ts and add an integration test in test/index.test.js confirming add and capitalize are exported" --repo-dir "..\demo-target" --thread-id "ticket-b"
  ```
* **Expected Output on stdout**:
  ```text
  [SUCCESS] Ticket executed successfully. Final status: completed
  ```
* **Verification in Demo Target**:
  ```powershell
  cd ..\demo-target
  git diff HEAD~1 HEAD
  # Confirms changes span both src/index.ts and test/index.test.js cleanly
  cd ..\jev-state-engine
  ```

---

### Ticket C: Forced Escalation — Mechanical Failure (Type Error)
* **Goal**: Test the Tier 0 circuit breaker by demanding contradictory types that cannot compile.
* **Execution Command**:
  ```powershell
  python src/main.py "Modify src/index.ts so add() returns a boolean while its return type is strictly typed as string, e.g. return true as any but typed string without casts" --repo-dir "..\demo-target" --thread-id "ticket-c"
  ```
* **Expected Output on stdout**:
  ```text
  [ESCALATION] Engine halted. Triggering status: strike_limit_reached
  ```
  *(Process exits with exit code `1`).*
* **Verification**:
  1. Inspect `escalation.log` in the engine root:
     ```powershell
     Get-Content escalation.log | ConvertFrom-Json | Select-Object triggering_tier, integration_branch
     ```
     * `triggering_tier`: `"mechanical"`
     * `integration_branch`: `"jev-ticket-ticket-c"`
  2. Verify `main` was **not touched**:
     ```powershell
     cd ..\demo-target
     git status
     # Still clean on main
     git branch -a
     # Notice jev-ticket-ticket-c is preserved for debugging; main has NOT moved
     cd ..\jev-state-engine
     ```

---

### Ticket D: Forced Escalation — Semantic Failure (Scope / Intent Violation)
* **Goal**: Request a change that attempts to modify out-of-scope files or contradicts ticket intent, triggering Jev System One rejection.
* **Execution Command**:
  ```powershell
  python src/main.py "Update the README.md documentation only, but sneak in an unrequested rewrite of package.json dependencies" --repo-dir "..\demo-target" --thread-id "ticket-d"
  ```
* **Expected Output on stdout**:
  ```text
  [ESCALATION] Engine halted. Triggering status: strike_limit_reached
  ```
* **Verification**:
  Inspect `escalation.log`: `triggering_tier` will record `"semantic"` or `"scope_creep"`. The worktree is discarded, and `main` is untouched.

---

### Ticket E: Resume from Checkpoint
* **Goal**: Verify state machine recovery and resumption using SQLite checkpoints.
* **Execution Command (Part 1 - Interrupt or run initial step)**:
  ```powershell
  python src/main.py "Add a divide(a: number, b: number): number utility function to src/math.ts" --repo-dir "..\demo-target" --thread-id "resumable-thread-1"
  ```
* **Execution Command (Part 2 - Resuming)**:
  Run the exact same command with the identical `--thread-id`:
  ```powershell
  python src/main.py "Add a divide(a: number, b: number): number utility function to src/math.ts" --repo-dir "..\demo-target" --thread-id "resumable-thread-1"
  ```
* **Expected Output**:
  The engine loads the existing state graph from `checkpoints.db` for thread `"resumable-thread-1"` and resumes execution without re-running completed subgoals.

---

## 7. How to Verify It Worked

### 7.1 Inspecting `checkpoints.db`
The engine persists all state transitions, strike counts, and trajectory notes to SQLite via `SqliteSaver` in [src/jev/engine.py](file:///d:/edge-downloades/d/intership-projects/jev-state-engine/src/jev/engine.py).

#### Fast Python Checkpoint Inspection One-Liner:
```powershell
python -c "import sys; sys.path.insert(0, 'src'); from jev.engine import SqliteSaver; saver = SqliteSaver.from_conn_string('checkpoints.db'); t = saver.get_tuple({'configurable': {'thread_id': 'ticket-a'}}); print('Status:', t.checkpoint['channel_values']['status'] if t else 'Not found'); print('Gate status:', t.checkpoint['channel_values']['gate_status'] if t else None)"
```

#### Inspect Table Rows with SQLite3:
```powershell
sqlite3 checkpoints.db "SELECT thread_id, checkpoint_id, type FROM checkpoints ORDER BY rowid DESC LIMIT 5;"
```

### 7.2 Inspecting `escalation.log`
When an escalation occurs, `escalation.log` is generated in the working directory:
```powershell
Get-Content escalation.log | ConvertFrom-Json | Format-List triggering_tier, integration_branch
```

### 7.3 Inspecting Target Repository Git History
```powershell
cd ..\demo-target
git log --graph --oneline -n 5
git branch -a
```

---

## 8. Known Limitations & Sharp Edges

1. **Pre-Existing Type Errors or Test Failures (Baseline Worktrees)**:
   The compile gate computes diff-relative errors against a temporary detached worktree at `base_commit`. If the target repository has pre-existing errors on `base_commit`, only newly introduced errors count as strikes. However, if the baseline repository itself cannot build at all (e.g., missing dependencies), the gate returns `ENV_NOT_READY` and halts. Always ensure the baseline repository compiles before running.

2. **Deferred Main Fast-Forward (Integration Branch Architecture)**:
   Individual subgoals are merged into a temporary branch (`jev-ticket-<thread-id>`), never directly into `main`. The `main` branch moves **only once**, via a fast-forward merge after the entire ticket passes final verification (`node_verify`). If a ticket escalates at any stage, `main` remains untouched, while `jev-ticket-<thread-id>` is preserved for triage.

3. **Windows `node_modules` Junction Safety**:
   On Windows, symlinking `node_modules` into isolated Git worktrees (`.jev-worktrees/subgoal-<id>`) can fail without Administrator privileges. The engine uses Windows Directory Junctions (`_winapi.CreateJunction`). Before removing worktrees, the engine explicitly unlinks the junction via `os.rmdir` to prevent deleting the physical contents in your target repository's root `node_modules`.

4. **Gemini Free-Tier Rate Limits & Sampling Defaults**:
   Free-tier Google Gemini API keys are limited to 15 Requests Per Minute (RPM) and daily token ceilings. During complex multi-subgoal runs, you may encounter `429 ResourceExhausted` errors. The engine incorporates exponential backoff retries, but if limits are exceeded repeatedly, planning or implementation will escalate.

5. **Absence of OS Sandbox**:
   As highlighted in Section 1, the engine invokes compilers and test runners directly via `subprocess`. Never execute against untrusted repositories.

---

## 9. Troubleshooting FAQ

### Q1: `UserWarning: Model 'gemini-3.5-flash-lite' uses fixed sampling defaults...`
* **Cause**: `langchain-google-genai` logs this informational warning because `gemini-3.5-flash-lite` enforces fixed sampling defaults.
* **Fix**: This warning is harmless and can be safely ignored. It does not affect engine execution or output.

### Q2: `ModuleNotFoundError: No module named 'jev'`
* **Cause**: Python was invoked from a context where `src/` is not on the module search path.
* **Fix**: Ensure your environment variable is set in PowerShell before executing:
  ```powershell
  $env:PYTHONPATH = "src"
  ```
  *(For macOS/Linux: `export PYTHONPATH="src"`).*

### Q3: `ENV_NOT_READY: Environment not ready: node_modules is missing...`
* **Cause**: The target repository contains a `package.json` or `tsconfig.json`, but `node_modules` has not been installed.
* **Fix**: Enter the target repository and install dependencies:
  ```powershell
  cd ..\demo-target
  npm install
  cd ..\jev-state-engine
  ```

### Q4: `Executable 'npm' not found` or PowerShell Script Execution Blocked (`npm.cmd`)
* **Cause**: On Windows, PowerShell requires `.cmd` extensions or npm may not be in the current path.
* **Fix**: Verify `npm --version` works in your shell. If PowerShell restricts script execution, run:
  ```powershell
  Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
  ```

### Q5: Git Warning: `LF will be replaced by CRLF the next time Git touches it`
* **Cause**: Standard Git line-ending conversion on Windows.
* **Fix**: Configure Git line-ending normalization:
  ```powershell
  git config --global core.autocrlf true
  ```
  Or add a `.gitattributes` file in your target repository with `* text=auto`.

---

## 10. How to Report a Bug

If you encounter unexpected behavior or an unhandled failure, please create a report containing:

1. **State Engine Trajectory & Status**:
   Attach `escalation.log` (if present in the engine directory).
2. **Checkpoint State Database**:
   Attach `checkpoints.db`.
3. **Target Repository Diff & Branches**:
   From your target repository, provide the output of:
   ```powershell
   git status
   git branch -a
   git diff main..jev-ticket-<thread-id>
   ```
4. **Pytest Run Output**:
   Run `pytest -v` and attach the terminal output to confirm whether unit tests are green on your platform.
5. **Environment Information**:
   * OS version: `[System.Environment]::OSVersion.VersionString`
   * Python version: `python --version`
   * Git version: `git --version`
   * Node version: `node -v`
