# Evaluation Protocol (PoC)

**Status:** Draft protocol; no benchmark runs authorized by this document.  
**Branch:** `agent-evaluation-poc`

## 1. Purpose and claims

Compare three complete agentic systems:

- JEV (including its orchestration/state engine, worker, configured tools, and any semantic gate used in the run)
- OpenCode (including its configured agent workflow and tools)
- Antigravity (including its configured agent workflow and tools)

The target is a pilot of **30–40 distinct benchmark task instances total**, not 30–40 tasks per agent. If all three systems are eligible for every task, 32 tasks would produce up to 96 agent-task runs. Report both task count and run count.

This is a feasibility and comparative pilot, not a statistically conclusive ranking or a claim of general agent capability.

## 2. Non-negotiable cost and safety gates

A task or execution environment is excluded unless all of the following are true:

1. Benchmark data and evaluator can be accessed lawfully under the benchmark's current terms.
2. Required compute, storage, browser environment, and model inference fit within resources already available at **zero additional cost**.
3. No paid API, cloud service, hosted evaluator, or automatic billing is triggered.
4. Credentials are supplied by the operator through local secrets or supported secret stores; they are never committed, printed in logs, or copied into task artifacts.
5. The task can run in an isolated workspace with a bounded timeout and resource limits.
6. The official evaluator (or a documented benchmark-author-approved evaluator) is available and its version can be recorded.

A free trial is not treated as a zero-cost guarantee if it can silently convert to paid usage. Do not add a payment method or enable billing for this PoC. Stop rather than silently substituting a paid service.

## 3. Benchmark selection: conditional shortlist

The following are candidates, not approved dependencies:

| Category | Candidate | Proposed pilot size | Gate / concern |
|---|---|---:|---|
| Coding | SWE-bench Verified | Up to 12 | Official harness uses Docker. Current project guidance recommends about 120 GB free storage, 16 GB RAM, and 8 CPU cores. Do not attempt a full setup on a smaller free environment. First investigate whether a small, reproducible subset can be evaluated with prebuilt images and no paid compute. |
| Reasoning / research / tool use | GAIA public development/validation material | Up to 10 | Dataset access is gated; access terms must be accepted by the operator. Use only authorized public material with available answer keys. Do not use held-out test answers or redistribute dataset contents. |
| Browser / web interaction | WebArena or a smaller compatible official task subset | Up to 10 | Self-hosted site environments can require substantial Docker images and storage. Do not provision the full environment until a measured resource check proves it fits. If it does not, replace it with a documented lighter browser benchmark rather than claiming WebArena coverage. |

The initial 12/10/10 allocation is a planning ceiling only. The final manifest may contain fewer tasks if access, licensing, reproducibility, or cost gates fail. Do not fill the quota with invented or home-grown tasks and label them as benchmark tasks.

For every selected task, record the canonical benchmark name, dataset/split, immutable task identifier, source revision or dataset version, license/terms reference, and evaluator version. Keep gated/private task data out of Git.

## 4. Agent eligibility and fair comparison

Before selecting the final task subset, run a capability check for each agent:

- Can it receive the benchmark's native task input?
- Can it access the required tools (repository, terminal, browser, files, web) without violating the benchmark rules?
- Can its output be captured in the format expected by the official evaluator?
- Can the complete workflow run without paid services?

Mark an agent-task pair as `not_eligible` with a reason when any answer is no. Do not force a coding-only system to perform browser tasks, or a browser-only system to produce repository patches.

JEV must be treated as the complete configured JEV system, not merely as a Gemini prompt. Record whether its worker, state machine, deterministic checks, and semantic gate were enabled. If a production semantic-gate credential or paid service is required and no zero-cost authorized route exists, mark that configuration unavailable; do not silently bypass a required JEV component and still call it the same system.

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
6. Do not give one agent benchmark-specific hints or hidden expected outputs unavailable to the others.
7. Run each eligible agent-task pair once for the initial pilot. Any repeat must be predeclared or clearly marked as a repeat; do not silently keep the best attempt.
8. Preserve raw outputs and logs locally with secrets redacted. Commit only sanitized aggregate results and reproducibility metadata.
9. Score using the official evaluator wherever possible. A separate reviewer may audit the process but must not replace the evaluator's result with a subjective pass.
10. Record infrastructure or provider failures separately from agent task failures. Do not score an interrupted run as a normal task failure.

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
7. Only then expand toward the 30–40-task pilot.

**No benchmark environment installation, model execution, paid resource provisioning, or scored run is authorized by this protocol alone.**


## 10. Feasibility review update (2026-11-10)

See [FEASIBILITY.md](FEASIBILITY.md) for the source-backed review.

Current recommendation:
- Do not assume SWE-bench Verified's 120 GB / 16 GB RAM / 8 CPU recommended environment fits the available free compute.
- Do not use the full self-hosted WebArena stack under the zero-cost constraint; assess AssistantBench validation tasks as the browser candidate instead.
- GAIA remains conditional on the operator accepting its gated dataset terms and using only authorized public development/validation material.
- MBPP+ or LiveCodeBench may replace SWE-bench only if the user explicitly accepts that these measure function/code generation or code repair, not repository-level issue resolution.
- Full production JEV is blocked unless existing prepaid TypeSafe Jev credit is explicitly authorized; TypeSafe publishes no free/trial credits.
- Do not run JEV target commands against untrusted benchmark code while provider credentials are present. Current process execution inherits the parent environment; see FEASIBILITY.md.

The revised 12/10/10 candidate allocation is not approved or executable until the user confirms benchmark scope, account quotas, and a safe credential-isolation design.
