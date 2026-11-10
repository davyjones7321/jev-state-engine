# Agent Evaluation PoC

This is an isolated evaluation project for comparing JEV, OpenCode, and
Antigravity. It does not modify the production JEV runtime or its existing
tests.

## Goal

Run a proof of concept with **10 new, distinct benchmark task instances total**: 10 MBPP+ coding tasks. GAIA reasoning/research and AssistantBench browser tasks are marked `not_built` and deferred from the first scored run. This is a feasibility demonstration, not a statistically conclusive ranking. With all three agents eligible for every task, the maximum is 30 agent-task runs (10 tasks across 3 agents). The earlier MBPP+ development smoke tasks Mbpp/2, Mbpp/3, and Mbpp/4 are excluded from scored results.

## Approved scope direction

The user approved deferring repository-level coding benchmarks and prioritizing
a practical proof of concept:

- **Coding:** MBPP+ / EvalPlus, 10 Python function-generation tasks.
  This is not repository-level issue resolution.
- **Reasoning/research:** GAIA, status `not_built`. No tasks or adapters invented.
- **Browser/research:** AssistantBench, status `not_built`. No tasks or adapters invented.

The first scored run is MBPP+ only (10 tasks, 30 runs maximum). SWE-bench Verified and WebArena are deferred. Exact MBPP+ task IDs are frozen in `evals/manifests/pilot.json` using a fixed seed (42) over canonical sorted IDs, excluding smoke tasks Mbpp/2, Mbpp/3, and Mbpp/4.

## Model and cost policy

- JEV and OpenCode should use the same exact model ID where supported.
- The Antigravity model must be recorded exactly as exposed by the product;
  current documentation lists **Gemini 3.8 Flash**, not Gemini 3.8 Flash-Lite.
- Record model ID, provider, access route, configuration, and quota/cost status
  for every run.
- The user has authorized using their existing paid Jev subscription for JEV
  semantic-gate calls. Verify that the subscription includes the required API
  entitlement and check for separate metering/overages before the first call.
  Do not enable additional billing or make an unapproved paid probe.
- Do not assume model API access is free because a free tier exists; account
  quotas are not visible to this repository.

## Isolation and safety

- Keep all PoC code and artifacts under `evals/`.
- Do not change JEV runtime behavior or existing tests for evaluation convenience.
- Keep benchmark workspaces and generated code separate from the engine source.
- Never commit API keys, access tokens, cookies, private benchmark data, or
  unredacted transcripts.
- Do not expose provider credentials to benchmark-controlled code, shell
  commands, browser pages, or task-controlled subprocesses. Deleting a key after
  a run does not undo exposure.
- A completed run record must attest that the credential boundary was verified.
  If the current JEV execution path cannot enforce this, do not run untrusted
  benchmark code through JEV until an isolated execution boundary is available.
- Respect benchmark licenses, access restrictions, and evaluator requirements.

## Current files

```text
evals/
  README.md
  PROTOCOL.md
  FEASIBILITY.md
  manifests/
    pilot.json
  schemas/
    run.schema.json
```

- `PROTOCOL.md` defines task selection, eligibility, execution, scoring, and
  reporting rules.
- `FEASIBILITY.md` records infrastructure, benchmark, model, subscription,
  and JEV execution constraints.
- `manifests/pilot.json` records the conditional candidate allocation. It
  contains no selected task IDs or benchmark data.
- `schemas/run.schema.json` defines the versioned run record. It requires a
  verified credential boundary for any run marked completed.
- `benchmarks/mbpp_plus/` contains the first coding benchmark adapter, pinned
  to EvalPlus 0.3.1 and MBPP+ v0.2.0, plus focused unit tests.
- `local/.gitignore` prevents generated datasets, private evaluator inputs,
  samples, and result JSON from being committed.

EvalPlus has been pinned and installed by the dedicated GitHub Actions unit-test workflow. The initial workflow run passed; later revisions to the adapter and tests are queued for CI verification. Three MBPP+ development smoke tasks (Mbpp/2, Mbpp/3, and Mbpp/4) have been exercised and are excluded from scored results. A dry-run mode executes these 3 non-scored smoke tasks end-to-end with zero network calls and no Jev API calls. No scored run of the new 10-task proof of concept has been executed, and no cloud development environment has been provisioned.
