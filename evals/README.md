# Agent Evaluation PoC

This directory contains an evaluation project for comparing JEV, OpenCode, and
Antigravity. It is intentionally isolated from the production JEV runtime and
its existing test suite.

## Goals

- Run a small pilot of approximately 30–40 benchmark tasks in total.
- Cover software engineering, general reasoning/research, and browser/tool use.
- Use benchmark-provided task definitions and official evaluators where
  practical and permitted.
- Record reproducible configurations, outcomes, execution time, resource use,
  and failure evidence.
- Compare agents only on tasks they can legitimately execute.

This is a pilot demonstration, not a statistically conclusive ranking.

## Comparison policy

JEV and OpenCode are intended to use the same model where supported.
Antigravity may use a different model because of its available integrations.
Every run must record the agent, exact model identifier, model/provider
configuration, task identifier, timeout, tool permissions, and evaluator
version. Results with different models must be labelled as end-to-end system
comparisons, not controlled framework comparisons.

Do not combine coding, reasoning, and browser results into one unqualified
overall score. Report results by benchmark/category and disclose missing or
ineligible runs.

## Isolation and safety

- Do not change JEV runtime behavior to make an evaluation pass.
- Do not place benchmark-specific dependencies in the production runtime
  requirements unless separately justified.
- Keep benchmark workspaces and generated artifacts separate from the engine's
  normal execution paths.
- Never commit API keys, access tokens, credentials, or private benchmark data.
- Respect each benchmark's license, access rules, and evaluator requirements.
- Do not start paid infrastructure or paid model usage without explicit
  approval.
- Prefer a small, validated subset over attempting to deploy a resource-heavy
  benchmark that exceeds the free-tier budget.

## Planned layout

```text
evals/
  benchmarks/   # Benchmark definitions and pinned evaluator adapters (not populated yet)
  agents/       # JEV, OpenCode, and Antigravity integration boundaries (not populated yet)
  runner/       # Execution orchestration and run isolation (not populated yet)
  scoring/      # Official evaluator adapters and normalized metrics (not populated yet)
  schemas/      # Versioned run-record schema
  manifests/    # Candidate task/run manifest; no task IDs selected yet
  results/      # Sanitized summaries only; raw outputs remain local
  reports/      # Reproducible summaries and comparison reports
  README.md
```

The layout is a starting boundary, not a commitment to implement every
directory immediately. Add components only when the first vertical slice
requires them.

## Initial benchmark candidates

- SWE-bench Verified: software-engineering task pilot.
- GAIA: general reasoning, research, and tool-use pilot.
- WebArena: browser-interaction pilot.

The final task subset and execution feasibility must be validated before any
benchmark environments are installed or runs are launched.

## Development sequence

1. Document the evaluation protocol and task-selection criteria.
2. Validate benchmark licenses, evaluator access, and free-tier feasibility.
3. Define a versioned task/run/result schema.
4. Implement one small end-to-end vertical slice.
5. Add agent adapters and expand toward the 30–40-task pilot.
6. Produce category-level results with limitations and reproducibility details.
