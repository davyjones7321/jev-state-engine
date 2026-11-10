# MBPP+ adapter: first vertical slice

This adapter prepares a deterministic MBPP+ subset and scores exactly one
solution per selected task with the official EvalPlus evaluator.

## Pinned versions

- EvalPlus package: 0.3.1
- MBPP+ dataset: v0.2.0 (378 curated tasks)
- Official sources:
  - https://github.com/evalplus/evalplus
  - https://github.com/evalplus/evalplus/blob/master/docs/cli.md
  - https://pypi.org/project/evalplus/0.3.1/

The adapter uses EvalPlus's own data loader and official evaluate module. It
does not implement substitute tests or scoring logic.

## Install in a dedicated environment

~~~powershell
python -m venv .venv-evals
.\.venv-evals\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r evals/benchmarks/mbpp_plus/requirements.txt
~~~

## Prepare a three-task smoke subset

~~~powershell
python evals/benchmarks/mbpp_plus/adapter.py prepare --limit 3
~~~

By default, this deterministically selects the first three task IDs in numeric
order. You may provide explicit official IDs:

~~~powershell
python evals/benchmarks/mbpp_plus/adapter.py prepare --task-ids Mbpp/2,Mbpp/3,Mbpp/4
~~~

The adapter writes to evals/local/mbpp_plus/:

- agent-visible/tasks.jsonl: task IDs, entry points, and prompts only.
- evaluator-private/mbpp-plus-subset.jsonl: official evaluator data, including
  canonical solutions and test inputs. Never expose this directory to agents.
- task-manifest.json: selected IDs, versions, and hashes.

The local directory is git-ignored. Keep evaluator-private data outside agent
workspaces.

## Agent solution format

Create a JSONL file with exactly one row per selected task:

~~~json
{"task_id":"2","solution":"def ...:\n    ..."}
{"task_id":"3","solution":"def ...:\n    ..."}
{"task_id":"4","solution":"def ...:\n    ..."}
~~~

Use the exact canonical IDs emitted by prepare (for example, `Mbpp/2`). Each solution must contain the complete
Python implementation expected by the task entry point.

## Score with official EvalPlus

~~~powershell
python evals/benchmarks/mbpp_plus/adapter.py evaluate --samples path\to\agent-solutions.jsonl
~~~

The adapter validates task coverage, verifies the private dataset hash, and
runs the official EvalPlus evaluator with a restricted child-process
environment. API keys are not propagated to evaluator subprocesses.

## Limitations

- This is a three-task smoke slice, not the complete 12-task coding category.
- MBPP+ tests Python function generation, not repository-level issue resolution.
- This adapter does not yet invoke JEV, OpenCode, or Antigravity. Agent
  integration is a separate next step after validating the evaluator path.
- EvalPlus recommends Docker for safer execution. This adapter's restricted
  environment is not an OS/container security boundary. Do not execute
  untrusted generated code on a host holding secrets; use container isolation
  before treating the setup as hardened.
