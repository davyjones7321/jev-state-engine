# Jev State Engine: Research-Informed Integration Plan

**Status:** Planning only; no source-code implementation is included here.

**Date:** 2026-10-16

**Purpose:** Integrate lessons from open-source coding agents and recent software-engineering-agent research into the existing Jev State Engine roadmap without weakening deterministic gates, Git isolation, or scope enforcement.

## 1. Executive decision

The research supports the current direction, but it changes the order and emphasis:

1. Add demand-driven repository inspection to the implementation worker.
2. Add a bounded repair loop with immediate validation before semantic judging.
3. Normalize mechanical and semantic failures into structured evidence packets.
4. Use DeepSeek as an advisory diagnostician, not as an authority and not as a patch writer.
5. Keep Jev authoritative in production and Laya as the local/open-source replacement mode.
6. Add program-structure-aware retrieval only after basic read tools and telemetry are proven.
7. Build a failure/trajectory benchmark before tuning strike limits or adding complex re-planning.

The central research conclusion is:

> Reliable coding agents are improved more by better interfaces, targeted retrieval, executable feedback, and transparent trajectories than by simply adding another reasoning model or increasing context size.

## 2. Current system problem being addressed

The current implementation worker can mutate declared files and submit a subgoal, but its implementation-time read capability is narrower than its investigation-time read capability. When a candidate fails compilation or Jev rejects it, the worker can receive feedback without necessarily being able to inspect the repository evidence needed to understand the failure.

This creates a blind retry pattern:

```text
write candidate
  -> mechanical or semantic rejection
  -> worktree rollback
  -> textual feedback
  -> worker guesses again without new repository evidence
  -> repeated failure or escalation
```

The plan therefore treats the problem as an interface-and-feedback problem, not primarily as a missing chain-of-thought problem.

## 3. Research reviewed

### 3.1 SWE-agent and mini-SWE-agent

- Repository: https://github.com/SWE-agent/SWE-agent
- Current simplified successor: https://github.com/SWE-agent/mini-swe-agent
- Paper: https://arxiv.org/abs/2405.15793

Relevant findings:

- The agent-computer interface materially affects repository navigation, editing, and test execution.
- Repository work is handled through interaction with the environment rather than by injecting the entire repository into the context.
- The newer mini-SWE-agent emphasizes a small, linear, inspectable trajectory and independent subprocess actions.
- The project explicitly values simple, hackable control flow and trajectory inspection.

Integration lesson:

- Give the Jev implementation worker demand-driven read and validation capabilities.
- Keep every action visible in the trajectory and ledger.
- Prefer simple independent commands/checks over hidden stateful shell sessions.
- Do not equate a larger prompt with better context.

### 3.2 AutoCodeRover

- Repository: https://github.com/AutoCodeRoverSG/auto-code-rover
- Paper: https://arxiv.org/abs/2404.05427

Relevant findings:

- The workflow separates context retrieval from patch generation.
- Program-structure-aware search over classes and methods is more useful than plain whole-file search for many repair tasks.
- Test availability can support fault localization and narrow the context supplied to the model.

Integration lesson:

- Keep retrieval separate from mutation.
- Add a later retrieval layer that can return symbols, definitions, callers, exports, and tests instead of only raw files.
- Use test failures and compiler diagnostics to prioritize retrieval targets.
- Do not make AST retrieval a prerequisite for the first repair-loop improvement; it is a later optimization.

### 3.3 Agentless

- Repository: https://github.com/OpenAutoCoder/Agentless
- Paper: https://arxiv.org/abs/2407.01489

Relevant findings:

- A simple staged pipeline can be competitive with more complicated autonomous agents.
- The reported workflow separates localization, repair, and patch validation.
- Localization is hierarchical: repository/file, class/function, then edit location.
- Candidate patches can be sampled and re-ranked using validation results.

Integration lesson:

- Do not add agents or states merely to increase complexity.
- Make localization an explicit, evidence-producing stage.
- Consider multiple candidate patches only after deterministic validation and retry telemetry show that a single repair path is insufficient.
- Keep patch validation independent from patch generation.

### 3.4 OpenHands

- Repository: https://github.com/OpenHands/OpenHands

Relevant findings:

- The project separates agent execution, tools, workspaces, server orchestration, and sandbox/backends.
- It supports local, Docker, VM, and other execution placements.
- The project warns that running without a sandbox gives the agent full filesystem access.

Integration lesson:

- Preserve the distinction between agent policy, workspace execution, and orchestration.
- Treat execution placement and sandboxing as first-class configuration, not an incidental detail.
- The current Jev worktree isolation is useful but is not an OS-level sandbox; this should remain explicit in the roadmap.

### 3.5 SWE-smith and SWE-agent-LM

- Repository: https://github.com/SWE-bench/SWE-smith
- Paper: https://arxiv.org/abs/2504.21798

Relevant findings:

- Large-scale task and trajectory generation can be used to train specialized software-engineering models.
- The project provides repository-specific environments, task instances, and trajectories.
- Training data is not limited to final patches; interaction trajectories are valuable.

Integration lesson:

- Preserve complete attempt trajectories, not only final outcomes.
- Treat mechanical failure packets, semantic rejections, successful repairs, and tool traces as future training/evaluation data.
- Do not start model fine-tuning before the ledger has stable schemas and high-quality labels.

### 3.6 Agentic Program Repair from Test Failures at Scale

- Paper: https://arxiv.org/abs/2507.18755

Relevant findings:

- A production-oriented repair loop combines static analysis, test failures, multiple actions, and repeated feedback iterations.
- Symbolic information from static analysis and test traces improves the neural repair loop.
- The work uses an LLM judge and human review as separate quality controls.

Integration lesson:

- DeepSeek should receive structured symbolic evidence, not only raw stderr.
- A repair loop should be allowed multiple evidence-driven iterations, while still having hard budgets.
- Semantic judging and human/escalation review remain separate from repair generation.

### 3.7 Thought-action-result trajectory study

- Paper: https://arxiv.org/abs/2506.18824

Relevant findings:

- Successful and failed coding-agent trajectories contain distinguishable action patterns and anti-patterns.
- Failure diagnosis and feedback integration should be measurable from recorded trajectories.
- Token use, iteration count, action sequences, and coherence are useful evaluation dimensions.

Integration lesson:

- Add metrics for whether a retry actually changed its evidence, tools, hypothesis, and diff.
- Detect repeated guessing and tool misuse as trajectory anti-patterns.
- Use the ledger to evaluate repair policies, not only final pass/fail.

### 3.8 SWE-Bench+ and benchmark quality warnings

- Paper: https://arxiv.org/abs/2410.06992

Relevant findings:

- Benchmark success can be inflated by solution leakage and weak tests.
- Passing tests alone is not sufficient evidence of correctness.

Integration lesson:

- Do not evaluate Jev, Laya, or DeepSeek only by test pass rate.
- Include hidden/held-out review labels, scope correctness, semantic completeness, and false-pass analysis.
- Maintain a locally curated failure set based on real Jev-State-Engine trajectories.

## 4. Revised architecture target

The target pipeline should be:

```text
Investigation
  -> plan mutation-producing subgoals
  -> implementation repair loop
       -> demand-driven read tools
       -> scoped mutation
       -> bounded local validation
       -> submit candidate
  -> deterministic Tier 0
       -> pass: Jev/Laya semantic gate
       -> fail: structured diagnosis and repair
  -> final verification
  -> merge only after final checks
```

DeepSeek is placed as an advisory component on failure paths:

```text
mechanical failure or semantic rejection
  -> collect deterministic evidence
  -> optionally call DeepSeek diagnostician
  -> produce bounded repair brief
  -> worker inspects repository and retries
```

DeepSeek must not:

- override Tier 0;
- approve a non-compiling diff;
- replace Jev's authority in production;
- write the patch directly;
- be assumed to know Jev's private reasoning when Jev returns only a boolean/probability;
- receive the entire repository by default.

## 5. Integration with the existing prioritized roadmap

The existing priority list was:

1. controlled read-only tools;
2. same-turn verification;
3. structured failure packets;
4. structured Jev follow-up questions;
5. selective DeepSeek diagnosis;
6. retry strategy enforcement;
7. Laya calibration.

The research-informed ordering is:

### Phase A — Measurement and contract freeze

**Goal:** make the current failure loop measurable before changing it.

Plan:

- Freeze the current authority model:
  - Jev authoritative in production;
  - Laya replacement mode for local/open-source evaluation;
  - DeepSeek advisory only.
- Define a single failure evidence schema for:
  - build/syntax;
  - import/module resolution;
  - compilation/typecheck;
  - tests;
  - scope;
  - semantic rejection;
  - environment failure;
  - empty diff.
- Record every attempt’s:
  - candidate diff hash;
  - touched files;
  - tools used;
  - read paths;
  - diagnostics;
  - model/provider/version;
  - judge result;
  - retry number;
  - whether the next attempt changed its hypothesis.
- Define baseline metrics:
  - first-attempt mechanical failure rate;
  - repeated identical-diff rate;
  - failures resolved after one retry;
  - failures resolved after diagnostic feedback;
  - escalation rate;
  - final-verification false-pass rate;
  - average latency and token cost.

**Why first:** trajectory research shows that feedback integration and anti-patterns must be measurable before claiming an improvement.

### Phase B — Demand-driven implementation context

**Goal:** solve the worker’s blindness without injecting the whole repository.

Plan:

- Expose bounded read-only implementation tools:
  - directory listing;
  - file read;
  - repository search;
  - optionally symbol/export lookup later.
- Keep writes restricted to declared subgoal scope.
- Enforce path containment, output caps, ignored directories, and tool-call budgets.
- Add a clear rule:
  - read scope may be broader than write scope;
  - write scope remains exact and mechanically enforced.
- Record read-tool calls and returned evidence in the trajectory.
- Make the worker inspect relevant files after a module/import/type failure rather than guess.

**Research basis:** SWE-agent, mini-SWE-agent, and AutoCodeRover all support environment interaction or targeted retrieval instead of relying on a whole-repository prompt.

**Exit criteria:** on a cross-file import fixture, the worker can discover the actual export/module boundary without receiving the entire repository in its initial prompt.

### Phase C — Bounded same-turn validation

**Goal:** catch obvious failures before consuming a gate strike.

Plan:

- Add a controlled validation action selected by repository profile.
- Run the smallest meaningful check first:
  - syntax/parse;
  - targeted compile/typecheck;
  - targeted tests where safely discoverable.
- Return structured diagnostics to the worker.
- Allow a bounded number of write-check-revise cycles before submission.
- Keep final Tier 0 independent; worker self-checks do not replace authoritative checks.
- Preserve every self-check result for analysis.

**Research basis:** SWE-agent’s environment interaction, the production repair-loop paper, and Agentless patch validation.

**Exit criteria:** common local syntax/import/type failures are corrected before the candidate reaches the semantic gate, without allowing the worker to bypass scope controls.

### Phase D — Structured repair evidence

**Goal:** turn raw failures into actionable, language-agnostic repair context.

Plan:

- Normalize diagnostics into:
  - failure class;
  - command/check;
  - affected files and locations;
  - baseline versus new errors;
  - relevant output excerpt;
  - known facts;
  - unknowns;
  - required inspection steps;
  - previous attempt hashes;
  - retry constraints.
- Add deterministic facts for semantic failures:
  - declared versus touched scope;
  - requirements not represented in the diff where mechanically detectable;
  - test evidence level;
  - dependency delta;
  - public interface changes.
- Reject repeated identical diffs before expensive checks.
- Treat empty implementation diffs as a planning/validation classification, not a mysterious semantic rejection.

**Research basis:** Agentless’s staged localization/repair/validation and the trajectory study’s emphasis on feedback integration.

**Exit criteria:** a worker retry receives a compact, structured evidence packet rather than only “strike N” and raw stderr.

### Phase E — Jev diagnostic contract

**Goal:** obtain more information from the authority before asking another model to infer it.

Plan:

- If the Jev API supports multiple typed questions, request structured failure signals in the same semantic call or an explicit follow-up call.
- Suggested categories:
  - incomplete;
  - wrong behavior;
  - exceeds scope;
  - contradicts ticket;
  - contradicts repository conventions;
  - missing validation;
  - unnecessary change;
  - low quality.
- Store raw answers, probabilities, model version, and request state keys in the ledger.
- Distinguish:
  - Jev-reported diagnosis;
  - deterministic diagnosis;
  - DeepSeek-inferred diagnosis.

**Why before DeepSeek:** an advisor should not be asked to guess a reason that the authoritative judge could expose directly.

**Exit criteria:** semantic rejection feedback has typed evidence whenever the provider supports it; otherwise the system clearly marks the reason as inferred.

### Phase F — DeepSeek diagnostic advisor

**Goal:** use DeepSeek for reasoning about evidence, not for authority or direct patching.

Plan:

- Call DeepSeek selectively:
  - repeated mechanical failure;
  - ambiguous compiler/test diagnostics;
  - semantic rejection with weak/no Jev reason;
  - repeated failure of the same hypothesis.
- Give DeepSeek only a bounded evidence packet:
  - ticket and subgoal;
  - relevant diff excerpt;
  - deterministic diagnostics;
  - Jev typed verdict if available;
  - selected repository evidence;
  - previous failed-attempt summaries.
- Require structured output:
  - confirmed facts;
  - likely root cause;
  - alternative hypotheses;
  - evidence still required;
  - retry constraints;
  - confidence.
- Do not require or persist raw hidden chain-of-thought as a system contract.
- Treat DeepSeek failure or timeout as non-fatal; deterministic feedback remains available.
- Do not let DeepSeek produce or apply file mutations.

**Exit criteria:** DeepSeek-assisted retries improve resolution rate or reduce repeated guesses without increasing false passes or scope violations.

### Phase G — Evidence-driven retry policy

**Goal:** ensure retries are meaningfully different.

Plan:

- On first failure, return deterministic evidence and enable targeted inspection.
- On repeated same-class failure, call DeepSeek and require a changed investigation action.
- On identical diff, short-circuit and count the original failure tier.
- Track whether the next attempt:
  - read new relevant files;
  - changed its hypothesis;
  - changed the touched diff;
  - addressed the reported failure class.
- Escalate when the worker repeats a failed hypothesis without new evidence, even if the nominal strike budget is not exhausted.
- Do not simply increase the strike limit.

**Research basis:** trajectory anti-patterns, iterative repair studies, and Agentless candidate validation.

### Phase H — Structure-aware retrieval

**Goal:** reduce context volume while improving relevance across languages.

Plan:

- Start with repository-native structure where available:
  - TypeScript/JavaScript exports/imports;
  - Python definitions/imports;
  - Go packages and symbols;
  - Rust modules/use statements;
  - Java/Kotlin classes/packages;
  - test-to-symbol relationships.
- Add symbol-aware retrieval behind a feature flag.
- Use compiler/test locations to rank retrieval targets.
- Return minimal context slices with source paths and line ranges.
- Fall back to plain search when parsing fails.
- Never make parser failure block the worker’s ability to inspect files.

**Research basis:** AutoCodeRover’s AST-aware search and hierarchical localization.

**Exit criteria:** structure-aware retrieval reduces prompt size or repair attempts on a representative multi-language fixture set without reducing successful fixes.

### Phase I — Laya replacement calibration

**Goal:** make local/open-source semantic judging useful without confusing it with Jev authority.

Plan:

- Create explicit modes:
  - Jev authority;
  - Laya authority;
  - Jev authority plus Laya shadow;
  - diagnostic-only.
- Build a labeled set of candidate diffs from the ledger.
- Compare Jev, Laya, and human labels by:
  - false pass;
  - false reject;
  - incomplete implementation detection;
  - scope detection;
  - final-verification correlation.
- Calibrate thresholds per model/version.
- Keep Laya’s decision separate from DeepSeek’s diagnosis.

**Exit criteria:** Laya can run as a documented local replacement with known limitations and measured disagreement rates.

### Phase J — Re-planning and learning

**Goal:** add recovery only after evidence shows where it helps.

Plan:

- Add engine-initiated re-planning only for:
  - repeated subgoal failure;
  - final verification rejection;
  - clearly oversized or incorrectly decomposed subgoals.
- Preserve committed subgoals and their lineage.
- Use the ledger to build repair examples and failure signatures.
- Consider SWE-smith-style synthetic tasks only after real failure categories are stable.
- Evaluate memory and learned retry hints offline before enabling them by default.

**Exit criteria:** re-planning reduces escalations without increasing scope creep, false passes, or repeated work.

## 6. What not to integrate yet

Do not initially integrate:

- whole-repository prompt injection;
- unrestricted shell access from the worker;
- DeepSeek as an approval authority;
- raw hidden chain-of-thought as a required persisted artifact;
- automatic patch generation by the diagnostic model;
- embeddings or a vector database before exact retrieval is insufficient;
- more strikes without evidence-change enforcement;
- model fine-tuning before trajectory and outcome labels are trustworthy;
- benchmark scores as the only success metric.

## 7. Suggested experiment matrix

Evaluate on a fixed set of disposable repositories and tickets across JavaScript/TypeScript, Python, Go, Rust, and JVM projects.

| Variant | Read tools | Same-turn check | Structured packet | DeepSeek | Structure retrieval | Purpose |
|---|---:|---:|---:|---:|---:|---|
| Baseline | no | no | no | no | no | Current behavior |
| A | yes | no | no | no | no | Measure context access alone |
| B | yes | yes | no | no | no | Measure pre-submit validation |
| C | yes | yes | yes | no | no | Measure evidence formatting |
| D | yes | yes | yes | selective | no | Measure diagnostic advisor |
| E | yes | yes | yes | selective | yes | Measure structure-aware retrieval |
| F | yes | yes | yes | selective | yes | Laya authority mode |

For every variant record:

- completion rate;
- first-attempt pass rate;
- mechanical strikes per subgoal;
- semantic strikes per subgoal;
- repeated-guess rate;
- DeepSeek calls and latency;
- total model cost/tokens;
- final verification failures;
- false-pass and false-reject labels;
- scope violations;
- environment-related escalations.

## 8. Success criteria

The integration should be considered successful only if it demonstrates all of the following on held-out tasks:

1. Lower repeated mechanical failures.
2. Lower repeated identical or near-identical diffs.
3. Fewer escalations caused by missing repository context.
4. No increase in scope violations.
5. No increase in false semantic passes.
6. No bypass of deterministic Tier 0 checks.
7. Measurable improvement across more than JavaScript/TypeScript.
8. DeepSeek failures do not prevent deterministic recovery or escalation.
9. Laya’s disagreement with Jev is measured rather than assumed.
10. Final verification remains the authority for landing changes.

## 9. Final recommendation

The next practical milestone should not be “add DeepSeek to every Tier 0 failure.” It should be:

```text
implementation read tools
  + bounded self-check
  + structured failure packet
  + ledger measurement
```

After that baseline is measurable, add selective DeepSeek diagnosis. This follows the strongest common pattern in the reviewed work:

```text
targeted context
  -> executable feedback
  -> iterative repair
  -> independent validation
  -> recorded trajectory
```

DeepSeek is valuable as a reasoning advisor, but the system should first ensure that the worker can inspect evidence and that the engine can represent failures precisely. Otherwise DeepSeek will be asked to compensate for missing information and may produce plausible but incorrect explanations.

## 10. Sources

- SWE-agent repository: https://github.com/SWE-agent/SWE-agent
- mini-SWE-agent repository: https://github.com/SWE-agent/mini-swe-agent
- SWE-agent paper: https://arxiv.org/abs/2405.15793
- OpenHands repository: https://github.com/OpenHands/OpenHands
- AutoCodeRover repository: https://github.com/AutoCodeRoverSG/auto-code-rover
- AutoCodeRover paper: https://arxiv.org/abs/2404.05427
- Agentless repository: https://github.com/OpenAutoCoder/Agentless
- Agentless paper: https://arxiv.org/abs/2407.01489
- SWE-smith repository: https://github.com/SWE-bench/SWE-smith
- SWE-smith paper: https://arxiv.org/abs/2504.21798
- Agentic Program Repair from Test Failures at Scale: https://arxiv.org/abs/2507.18755
- Understanding Software Engineering Agents: https://arxiv.org/abs/2506.18824
- SWE-Bench+: https://arxiv.org/abs/2410.06992
