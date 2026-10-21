# Jev State Engine

Jev State Engine is a deterministic finite-state-machine runtime for executing software-engineering tickets against a separate Git repository. It combines an untrusted worker LLM with read-only investigation, scoped mutations, isolated Git worktrees, deterministic Tier 0 checks, and TypeSafe AI Jev semantic validation.

> **Safety warning:** the engine runs repository build and test commands directly on the host. Git worktrees are not an OS sandbox. Use a disposable target repository; never point `--repo-dir` at production, a repository containing secrets, or an untrusted codebase.

## How a run works

```text
investigate -> plan -> implement -> Tier 0 checks -> Jev Tier 1 gate
                                      |                    |
                                      +---- retry ---------+
                                              |
                         final verification -> fast-forward landing
                                              |
                                           escalate
```

### Roles and authority

- **Engine/FSM:** owns state transitions, budgets, retries, checkpointing, and branch lifecycle.
- **Worker:** investigates, plans, and implements inside declared scope. It is not an authority.
- **Tier 0:** deterministic local checks for syntax/build, compile, scope, dependencies, and tests. It is authoritative for mechanical facts.
- **Jev:** the production semantic judge. It evaluates a staged diff after Tier 0 passes and performs final ticket verification.
- **Optional advisory:** an OpenAI-compatible model, such as DeepSeek, may return bounded repair guidance. It cannot approve a diff, override Tier 0 or Jev, or write files.

## Prerequisites

- Python 3.10 or newer (Python 3.13 is supported by the current test environment).
- Git 2.25 or newer, available on `PATH`.
- PowerShell 5.1 or PowerShell 7+ on Windows.
- Tools required by the **target repository**, only when its files are changed or its tests are detected: Node/npm, Go, Rust/Cargo, Maven or Gradle, Terraform, Ansible, or Helm.

Check the local installation:

```powershell
python --version
git --version
$PSVersionTable.PSVersion
```

## Windows PowerShell setup

From PowerShell:

```powershell
git clone <repository-url> jev-state-engine
Set-Location .\jev-state-engine

python -m venv .venv
.\.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

If activation is blocked for the current PowerShell process:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

The repository's `pytest.ini` adds `src` to pytest's import path. The most reliable direct CLI command is `python src/main.py` from the engine root. If you prefer module-style invocation with `python -m src.main`, keep this variable set:

```powershell
$env:PYTHONPATH = "src"
```

For macOS/Linux, use `source .venv/bin/activate`; the Python commands are otherwise the same.

## Environment variables

Create `.env` in the engine root. The CLI first looks in the current working directory, then the engine root, and finally the target repository directory. Environment variables already exported in the process take precedence for the provider that reads them.

```dotenv
# Required for the production semantic gate
JEV_API_URL=https://api.typesafe.ai/v1/systemone
JEV_API_KEY=your_typesafe_api_key

# Required for live worker generation; unit tests can run without it
GOOGLE_API_KEY=your_google_ai_api_key

# Optional non-authoritative repair advisor
ADVISORY_BASE_URL=https://api.deepseek.com
ADVISORY_MODEL=deepseek-flash
ADVISORY_API_KEY=your_advisory_key
```

- `JEV_API_URL` and `JEV_API_KEY` configure the Jev HTTP gatekeeper. There is no source-code fallback URL or key.
- `GOOGLE_API_KEY` enables the Gemini worker (`gemini-3.5-flash-lite`) in `src/main.py`. Without it, the CLI can initialize but the worker LLM remains unavailable.
- The advisory is enabled only when both `ADVISORY_BASE_URL` and `ADVISORY_MODEL` are present. `ADVISORY_API_KEY` is optional and is sent as `Authorization: Bearer ...` when supplied.
- `ADVISORY_BASE_URL` is an OpenAI-compatible **base URL**. The provider appends `/chat/completions`; configure `https://host/v1`, not the complete `/chat/completions` URL.
- The advisory must return a chat-completions response whose first choice contains JSON with `summary`, `cause`, and a non-empty `actions` list. Provider failures are non-fatal and fall back to deterministic feedback.

### DeepSeek advisory use

To use a DeepSeek-compatible endpoint as the optional advisor, set `ADVISORY_BASE_URL`, `ADVISORY_MODEL`, and, if required, `ADVISORY_API_KEY`. DeepSeek remains diagnostic only: it supplies a bounded repair brief and never decides whether a diff passes.

The separate `deepseek/run_tri_test.py` script is an experimental Jev/DeepSeek/Laya comparison runner. It is not the default CLI path and should not be described as the production authority model.

## CLI usage

Run from the engine repository with the virtual environment active:

```powershell
python src/main.py "<ticket description>" [options]
```

Equivalent module-style invocation:

```powershell
$env:PYTHONPATH = "src"
python -m src.main "<ticket description>" [options]
```

| Option | Default | Meaning |
|---|---|---|
| `ticket` | required | The task description. It is the work request, not a checkpoint identifier. |
| `--repo-dir PATH` | current directory | Target Git repository. Prefer an absolute path or a disposable sibling directory. |
| `--db-path PATH` | `checkpoints.db` | SQLite checkpoint path. |
| `--thread-id ID` | `run-<UTC timestamp>-<4 hex>` | Run identity used for checkpoints, ledger records, branches, and escalation artifacts. |
| `--test-policy VALUE` | `auto` | `auto`, `verify-only`, `never`, or `always`. |
| `--no-ledger` | off | Disable the SQLite run ledger; checkpoints still use `--db-path`. |
| `--retry-intel on\|off` | `on` | Enable duplicate-diff/dead-end guards and typed feedback handling. |
| `--repo-profile on\|off` | `off` | Inject the detected target-repository profile into worker prompts. |
| `--test-timeout SECONDS` | `JEV_TEST_TIMEOUT` or 600 | Positive timeout for target test commands. |
| `--dep-delta-jev on\|off` | `off` | Include dependency-manifest changes in Jev evidence when supported. |
| `--advisory-base-url URL` | env value | Override `ADVISORY_BASE_URL`. |
| `--advisory-model NAME` | env value | Override `ADVISORY_MODEL`. |
| `--advisory-api-key KEY` | env value | Override `ADVISORY_API_KEY`; it is not recorded in advisory telemetry. |

### Ticket versus `--thread-id`

The positional `ticket` is the natural-language task to execute. `--thread-id` is only the stable identifier for one run's state and artifacts.

- Omit `--thread-id` for a new generated ID.
- Reusing an existing ID **does not resume** a completed or interrupted run. The engine restarts from scratch and removes/recreates the corresponding integration branch.
- Use a new ID for a new attempt. Keep the same ID only when you intentionally want a clean restart with the same run label.

Examples:

```powershell
# Run a ticket against a disposable sibling repository
python src/main.py "Add a unit-tested string utility" `
  --repo-dir ..\demo-target `
  --thread-id ticket-string-utils

# Skip target test runners while retaining compile, scope, and other Tier 0 checks
python src/main.py "Update documentation only" `
  --repo-dir ..\demo-target `
  --thread-id ticket-docs `
  --test-policy never

# Run tests only during final verification
python src/main.py "Refactor the authentication helper" `
  --repo-dir ..\demo-target `
  --thread-id ticket-auth `
  --test-policy verify-only
```

### Test-policy semantics

- **`auto`**: run tests for subgoals whose plan says `expects_tests=true`; skip intermediate tests for subgoals that do not expect tests. Final verification runs the applicable tests.
- **`verify-only`**: skip intermediate test execution and run tests during final verification.
- **`always`**: run the detected test suite for every subgoal, even when `expects_tests=false`.
- **`never`**: do not invoke a target test runner during intermediate or final verification. This is an explicitly **excluded** test result, not a passing test result. Tier 0 still enforces syntax/build, compile/type checks where available, scope, suppression, and unresolved-import checks; Jev receives the exclusion marker.

`never` is appropriate for a target with known or unavailable tests only when you accept that the run has no test evidence. It does not disable all validation and does not make an otherwise invalid diff safe.

## Target repository workflow and safety

1. Create or clone a disposable target repository **outside** this repository.
2. Install that target's dependencies and verify its baseline is usable.
3. Commit a clean baseline before running the engine.
4. Run the CLI with `--repo-dir` pointing to the target.
5. Inspect the target branch and the ledger/escalation artifacts after the run.

The engine executes detected commands such as `npm test`, `go test ./...`, `cargo test`, Maven/Gradle tests, `pytest -q`, `terraform validate`, Ansible checks, or `helm lint`. Missing tools or dependencies produce environment-not-ready evidence; they are not silently treated as successful tests.

Do not use `--repo-dir` for the engine repository itself while experimenting with destructive or intentionally failing tickets. The engine can modify files, create worktrees, run arbitrary repository-defined commands, and update Git refs in the target.

## Branch and merge policy

- At startup, the engine records the current target `HEAD` as the base commit and creates `jev-ticket-<thread-id>` as an integration branch.
- Each subgoal runs in `.jev-worktrees/subgoal-<id>` on `jev-subgoal-<id>`.
- A subgoal that passes Tier 0 and Jev is committed and fast-forwarded into the integration branch. Its temporary subgoal worktree and branch are then removed.
- The checked-out target branch is not changed during intermediate subgoals.
- Final verification runs from a separate verification worktree. Only after final checks and Jev verification pass does the engine fast-forward the checked-out branch to the integration branch. The checked-out branch must have no tracked modifications and must still match the recorded base commit.
- On escalation or final-verification failure, the checked-out branch remains untouched and the integration branch is preserved for triage.

This is a fast-forward-only workflow; it does not create a merge commit and does not resolve concurrent changes automatically.

## Architecture and evidence

The implementation is organized around `src/jev/engine.py`, `workspace.py`, `gatekeeper.py`, and the provider/advisory adapters:

1. **Investigation:** read-only repository inspection with path confinement.
2. **Planning:** typed subgoals with declared mutation scope and test expectations.
3. **Implementation:** scoped writes in an isolated worktree.
4. **Gate:** deterministic Tier 0 checks, followed by Jev Tier 1 semantic validation.
5. **Verification:** cumulative diff and final target evidence, then fast-forward or escalation.

SQLite checkpoints persist graph state by thread ID. The run ledger records attempts, evidence, diffs, gate outcomes, and role provenance unless `--no-ledger` is supplied. Escalations are written to `escalation.log` for compatibility and, when a thread ID is available, to `$JEV_HOME/runs/<thread-id>/escalation.json`.

For the longer architectural and research rationale, see:

- [ARCHITECTURE.md](ARCHITECTURE.md)
- [plan.md](plan.md)
- [docs/RESEARCH_INTEGRATION_PLAN.md](docs/RESEARCH_INTEGRATION_PLAN.md)

## Tests

Run the repository's non-live tests:

```powershell
python -m pytest -q -m "not live"
```

Run all discovered tests, including tests marked `live`:

```powershell
python -m pytest -q
```

Useful focused commands:

```powershell
python -m pytest tests/test_test_policy.py -q
python -m pytest tests/test_integration_branch.py -q
python -m pytest tests/test_advisory_provider.py -q
```

The current suite includes unit, integration, AST, worktree, policy, ledger, and provider tests. Do not rely on a fixed test count in documentation; use the command output as the source of truth for the current branch.

## Troubleshooting

### `ModuleNotFoundError: No module named 'jev'`

Run from the engine root, activate `.venv`, or set `$env:PYTHONPATH = "src"` before invoking a script directly.

### `JEV_API_URL must be configured` or `JEV_API_KEY must be configured`

Check that `.env` is in the engine root/current working directory, that the variable names are exact, and that the values are not blank. The gatekeeper has no hardcoded fallback.

### The worker is unavailable or the CLI warns about Gemini initialization

Set `GOOGLE_API_KEY`, confirm `langchain-google-genai` installed from `requirements.txt`, and verify the key has access to the configured Gemini model. Unit tests can use mocks without this key.

### `ENV_NOT_READY` for a target

Install the target repository's dependencies and required toolchain in the target directory. For a Node target, for example:

```powershell
Set-Location ..\demo-target
npm install
Set-Location ..\jev-state-engine
```

### PowerShell cannot run `Activate.ps1` or an npm command

Use the process-scoped execution-policy command shown above and verify `python`, `npm`, or the target tool works directly from the same shell.

### The engine escalates with `main_diverged` or tracked modifications

The checked-out target branch changed after the run started, or it contains tracked edits. Commit/stash those edits, reset the disposable target to its baseline, and start a new run with a new thread ID.

### Advisory errors

Advisory errors are non-fatal. Confirm that `ADVISORY_BASE_URL` is the provider root (the adapter appends `/chat/completions`), that `ADVISORY_MODEL` is valid, and that the endpoint returns the required JSON repair-brief shape. The engine continues with deterministic feedback when the advisor times out, rejects the request, or returns malformed JSON.

## Related documentation

- [docs/TEAM_TESTING.md](docs/TEAM_TESTING.md): Windows walkthrough and disposable demo target.
- [docs/TEST_PLAN.md](docs/TEST_PLAN.md): test intent and quality gates.
- [docs/BUILD_ORDER.md](docs/BUILD_ORDER.md): historical implementation sequencing.
- [docs/RESEARCH_INTEGRATION_PLAN.md](docs/RESEARCH_INTEGRATION_PLAN.md): research and roadmap intent.
