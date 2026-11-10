# Evaluation Protocol (PoC)

**Status:** Draft protocol; no benchmark runs authorized by this document.  
**Branch:** `agent-evaluation-poc`

## 1. Purpose and claims

Compare three complete agentic systems:

- JEV (including its orchestration/state engine, worker, configured tools, and any semantic gate used in the run)
- OpenCode (including its configured agent workflow and tools)
- Antigravity (including its configured agent workflow and tools)

The first scored run is **MBPP+ only (10 new, distinct benchmark task instances total)**: 10 MBPP+ coding tasks. GAIA reasoning/research and AssistantBench browser tasks are marked `not_built` and deferred for subsequent phases; no tasks or adapters are invented for them. This is 10 tasks total, not 10 per agent. With all three systems eligible for every task, the evaluation produces up to 30 agent-task runs (10 tasks across 3 agents). Report both task count and run count. The earlier MBPP+ smoke tasks Mbpp/2, Mbpp/3, and Mbpp/4 are excluded from scored results.

This is a feasibility and comparative pilot, not a statistically conclusive ranking or a claim of general agent capability.

## 2. Non-negotiable cost and safety gates

A task or execution environment is excluded unless all of the following are true:

1. Benchmark data and evaluator can be accessed lawfully under the benchmark's current terms.
2. Required compute, storage, browser environment, and model inference fit within resources already available at **zero additional cost**.
3. No paid API, cloud service, hosted evaluator, or automatic billing is triggered.
4. Credentials are supplied by the operator through local secrets or supported secret stores; they are never committed, printed in logs, or copied into task artifacts.
5. The task can run in an isolated workspace with a bounded timeout and resource limits.
6. The official evaluator (or a documented benchmark-author-approved evaluator) is available and its version can be recorded.

A free trial is not treated as a zero-cost guarantee if it can silently convert to paid usage. The user has authorized use of an existing paid Jev subscription for JEV semantic-gate calls only, subject to confirming the API entitlement and any usage/overage terms. Do not add a new payment method, enable additional billing, or incur costs outside that authorization.

## 3. Benchmark selection: conditional shortlist

The 10/10/10 allocation is approved as the proof-of-concept target. Benchmark execution and individual task selection remain conditional on the access, licensing, evaluator, resource, and safety gates below:

| Category | Candidate | Proposed pilot size | Gate / concern | Status |
|---|---|---:|---|---|
| Coding | MBPP+ / EvalPlus | 10 | Python function generation; not repository-level issue resolution. Pin EvalPlus and use its official evaluator. | Ready (frozen 10 tasks) |
| Reasoning / research / tool use | GAIA public development/validation material | 10 | Dataset access is gated; accept terms and use only authorized public material with available answer keys. | `not_built` (deferred) |
| Browser / web interaction | AssistantBench public validation tasks | 10 | Pin the Inspect Evals release and browser implementation; document Playwright MCP browser differences. | `not_built` (deferred) |

The first scored run is MBPP+ only (10 tasks, up to 30 runs across 3 agents). GAIA and AssistantBench are marked `not_built` in the manifest; no tasks or adapters are invented for them. If any category fails its gates, stop and report the shortfall rather than substituting invented or home-grown tasks and labeling them benchmark tasks.

For every selected task, record the canonical benchmark name, dataset/split, immutable task identifier, source revision or dataset version, license/terms reference, and evaluator version. Keep gated/private task data out of Git.

## 4. Agent eligibility and fair comparison

Before selecting the final task subset, run a capability check for each agent:

- Can it receive the benchmark's native task input?
- Can it access the required tools (repository, terminal, browser, files, web) without violating the benchmark rules?
- Can its output be captured in the format expected by the official evaluator?
- Can the complete workflow run without paid services?

Mark an agent-task pair as `not_eligible` with a reason when any answer is no. Do not force a coding-only system to perform browser tasks, or a browser-only system to produce repository patches.

JEV must be treated as the complete configured JEV system, not merely as a Gemini prompt. Record whether its worker, state machine, deterministic checks, and semantic gate were enabled. The user has stated that their existing paid Jev subscription is intended to cover evaluation calls and has authorized its use for the tests. Before the first request, verify that the subscription actually includes the required API entitlement/usage and determine whether any overage or separate metering applies. Use only the covered entitlement the user has authorized; do not enable additional billing or incur unapproved charges. If the production semantic-gate service is unavailable under that entitlement, stop and report the blocker; do not silently bypass a required JEV component and still call it the same system.

### Model controls

- Use the same exact model identifier for JEV and OpenCode where both support it and the account has no-cost access.
- Record Antigravity's actual available model identifier and configuration; do not mislabel it as the same model.
- Record provider, model ID/version, inference access route, relevant generation settings, and observed quota/cost status for every run.
- Separate **controlled comparisons** (same model and task conditions) from **end-to-end system comparisons** (different model or tool stack).
- If model identity or access route cannot be verified, label it unknown and exclude the run from controlled comparisons.

No model is assumed to be free merely because a free tier or trial is advertised. Verify the user's actual no-cost access and quota before execution.

## 5. Task selection and run protocol

1. Freeze the benchmark and evaluator versions before the first scored run.
2. Select tasks before observing any agent's result. Avoid cherry-picking tasks based on expected agent strengths.
3. Use a small pilot sample across difficulty levels and task types where the benchmark supports those labels.
4. Use the same task prompt, starting repository/environment state, allowed tool classes, timeout, and evaluator for all eligible agents.
5. Reset the task environment between agents. No agent may see another agent's patch, transcript, or answer.
6. Keep provider credentials in the trusted agent/orchestrator process only. Do not expose them to benchmark repositories, generated code, shell commands, browser pages, or task-controlled subprocesses. If the current JEV process model cannot enforce this boundary, do not run untrusted benchmark code through JEV until a narrow isolation wrapper or separate execution boundary is available.
7. Do not give one agent benchmark-specific hints or hidden expected outputs unavailable to the others.
8. Run each eligible agent-task pair once for the initial pilot. Any repeat must be predeclared or clearly marked as a repeat; do not silently keep the best attempt.
9. Preserve raw outputs and logs locally with secrets redacted. Commit only sanitized aggregate results and reproducibility metadata.
10. Score using the official evaluator wherever possible. A separate reviewer may audit the process but must not replace the evaluator's result with a subjective pass.
11. Record infrastructure or provider failures separately from agent task failures. Do not score an interrupted run as a normal task failure.

## 6. Required run record

Each run must capture at least:

- `run_id`, timestamp (UTC), agent name/version, adapter version, and agent configuration hash
- exact model/provider identifier and inference access route (or `unknown`)
- benchmark, split, task ID, dataset revision, evaluator version
- eligibility status and reason if not run
- clean starting-state identifier (commit/container/image/environment version)
- enabled tools and permissions; network access policy
- timeout and resource limits
- exit status, completion status, evaluator result, and normalized metric
- duration, resource usage when measurable, and observed quota/cost status
- artifact/log locations and redaction status
- failure classification and concise notes

Do not store API keys, cookies, authorization headers, personal data, or private benchmark answers in the run record.

## 7. Metrics and reporting

Report each benchmark/category separately. Do not create a single blended score across coding, reasoning, and browser tasks.

Minimum reporting:

- eligible tasks and completed runs per agent
- official benchmark score (for example, resolved rate or exact-answer accuracy) with numerator and denominator
- completion rate and infrastructure/provider failure rate
- median and range of duration where enough observations exist
- resource consumption and observed cost (target: zero additional spend)
- model/tool configuration differences
- task-level results where publication/benchmark terms permit
- limitations, missing tasks, and any deviations from protocol

Do not interpret a small difference on 10–12 tasks as statistically meaningful. Do not compare percentages with different denominators without showing those denominators.

## 8. Repository boundaries

All PoC code, manifests, and documentation live under `evals/`. Do not alter JEV runtime code, its dependency manifests, or its existing tests for this evaluation setup. Agent-specific workspaces and benchmark artifacts must live outside the JEV source tree or in an explicitly isolated subdirectory.

Do not commit benchmark datasets, generated patches, full transcripts, credentials, or large Docker artifacts. Keep raw run artifacts local and add only sanitized summaries when appropriate.

## 9. Implementation sequence

1. Review this protocol and approve the final benchmark candidates.
2. Check actual zero-cost model access and any JEV semantic-gate dependency.
3. Measure available cloud CPU, RAM, storage, Docker support, and browser support before choosing tasks.
4. Confirm benchmark terms and evaluator access.
5. Create a versioned task manifest and run-record schema.
6. Implement one minimal end-to-end task adapter and validate the evaluator using an official known-good/reference example where allowed.
7. Freeze the 10 MBPP+ task manifest before scored runs (GAIA and AssistantBench deferred as `not_built`).

**No benchmark environment installation, model execution, paid resource provisioning, or scored run is authorized by this protocol alone.**


## 10. Feasibility review update (2026-11-10)

See [FEASIBILITY.md](FEASIBILITY.md) for the source-backed review.

Current recommendation:
- Do not assume SWE-bench Verified's 120 GB / 16 GB RAM / 8 CPU recommended environment fits the available free compute.
- Do not use the full self-hosted WebArena stack under the zero-cost constraint; assess AssistantBench validation tasks as the browser candidate instead.
- GAIA remains conditional on the operator accepting its gated dataset terms and using only authorized public development/validation material.
- MBPP+ / EvalPlus is the approved coding benchmark for this proof of concept. It measures Python function generation, not repository-level issue resolution; SWE-bench Verified remains deferred.
- Full production JEV is blocked unless existing prepaid TypeSafe Jev credit is explicitly authorized; TypeSafe publishes no free/trial credits.
- Do not run JEV target commands against untrusted benchmark code while provider credentials are present. Current process execution inherits the parent environment; see FEASIBILITY.md.

The approved 10/10/10 target allocation remains conditional on benchmark access, account quotas, evaluator availability, zero-additional-cost coverage, and credential isolation.


## 11. Scored run task allocation (2026-11-10)

The first scored run is **MBPP+ only (10 new, distinct benchmark tasks)**:

- Coding: 10 MBPP+ / EvalPlus tasks (Python function generation; not repository issue resolution). Task IDs are frozen in `evals/manifests/pilot.json` using fixed seed 42 over canonical sorted IDs, excluding Mbpp/2, Mbpp/3, and Mbpp/4.
- Reasoning/research: GAIA is marked `not_built`. No tasks or adapters are invented.
- Browser/research: AssistantBench is marked `not_built`. No tasks or adapters are invented.

The target is 10 tasks total for the first scored run. With three eligible agents per task, the maximum is 30 agent-task runs. Actual run count depends on each agent's documented eligibility.

The earlier MBPP+ development smoke tasks Mbpp/2, Mbpp/3, and Mbpp/4 are explicitly excluded from this proof of concept's scored dataset, comparative results, and final evaluation findings. They are exercised exclusively via offline dry-run checks and adapter development smoke tests. Do not combine their results with the 10-task evaluation.

Freeze and record the exact 10 MBPP+ task IDs, dataset version (v0.2.0), EvalPlus version (0.3.1), evaluator version (jev-mbpp-evaluator:0.3.1), selection seed (42), and per-agent eligibility before any scored run.

The user also stated that an existing paid Jev subscription is intended to cover JEV semantic-gate calls and authorized its use for evaluation. Before making any call, confirm that the subscription includes the required API entitlement and check whether there are separate usage charges or overages. No additional billing or unapproved spend is authorized.

The user is willing to defer broader JEV security hardening. That does not authorize exposing API credentials to untrusted benchmark code. Provider credentials must remain outside task-controlled child processes and benchmark workspaces; otherwise JEV runs remain blocked until a narrow isolation boundary is in place.
