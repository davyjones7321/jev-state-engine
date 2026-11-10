# Feasibility Review

**Review date:** 2026-11-10  
**Repository:** `davyjones7321/jev-state-engine`  
**Branch:** `agent-evaluation-poc`  
**Status:** Research-only review. No benchmark was installed or run, no cloud environment was provisioned, and no paid service was used.

## Executive decision

The original three-benchmark plan is not ready to execute under the strict zero-additional-cost constraint. The main blockers are the TypeSafe semantic gate required by full JEV, the resource demands of SWE-bench Verified and WebArena, and the unverified remaining quotas on the user's accounts.

A smaller pilot may be feasible with:
- a lightweight coding benchmark such as MBPP+ or a selected LiveCodeBench code-generation subset (this changes the claim from repository-level issue resolution to code-generation / code-repair),
- 10 authorized GAIA validation/development tasks,
- 10 AssistantBench validation tasks using live public websites instead of self-hosted WebArena services.

These are candidates only. The user must approve any change from repository-level coding to function/code-generation tasks.

## 1. Cloud compute

### GitHub Actions

This repository is public. GitHub's current documentation says standard GitHub-hosted runners are free and unlimited for public repositories. The standard Linux public runner has 4 CPUs, 16 GB RAM, and 14 GB SSD. That is suitable for lightweight Python evaluation, browser automation with Playwright, and small test suites, but not a substitute for SWE-bench's recommended 120 GB free disk.

Source: [GitHub-hosted runners reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)

GitHub Actions is non-interactive CI rather than the user's requested SSH-accessible development environment. It is a useful runner for repeatable, bounded tasks, not the only environment.

### GitHub Codespaces

For personal GitHub accounts, the published included quota is 120 core-hours and 15 GB-month on GitHub Free (180 core-hours and 20 GB-month on Pro). The smallest listed machine is 2 cores / 8 GB RAM / 32 GB storage; the largest is 32 cores / 128 GB RAM / 128 GB storage.

The quota is measured in core-hours: a 2-core codespace uses 2 core-hours per active hour, while a 32-core codespace uses 32. Therefore 120 included core-hours would allow at most 60 active hours on 2 cores, 15 hours on 8 cores, or 3.75 hours on 32 cores, before accounting for other use. This is a theoretical ceiling, not the user's remaining quota.

Codespaces free quota is for personal accounts, not organization/enterprise accounts. Current account plan, remaining monthly quota, existing codespace storage, payment-method status, and spending safeguards are not visible to this review. If a payment method is attached, the user must ensure overage spending is stopped; otherwise exceeding included quota can incur charges. Do not create a codespace until the remaining quota and spending protection are checked.

Sources:
- [Codespaces machine types](https://docs.github.com/en/codespaces/about-codespaces/what-are-codespaces)
- [Codespaces billing and included quotas](https://docs.github.com/en/billing/concepts/product-billing/github-codespaces)
- [Included usage by plan](https://docs.github.com/en/billing/reference/product-usage-included)

**Recommendation:** Use Codespaces for interactive setup, SSH, and browser-enabled development only after verifying the account's remaining free quota and zero-overage protection. Use public GitHub Actions runners for lightweight repeatable tasks. Do not assume Codespaces quotas are unused.

## 2. Coding benchmark

### SWE-bench Verified

The official SWE-bench repository recommends an x86_64 machine with at least 120 GB free storage, 16 GB RAM, and 8 CPU cores. The public GitHub Actions runner has 14 GB SSD. A Codespace can be configured with more storage and CPU, but the largest 32-core machine would consume the full 120-core-hour Free quota in 3.75 active hours, and the user's available quota is unknown.

This does not prove that a single hand-picked task is impossible, but it makes the original plan of up to 12 SWE-bench Verified tasks an unsafe assumption for a free-tier pilot. No task image was pulled and no benchmark environment was built.

Sources:
- [SWE-bench official setup guidance](https://github.com/SWE-bench/SWE-bench#setup)
- [SWE-bench Docker setup guide](https://github.com/SWE-bench/SWE-bench/blob/main/docs/guides/docker_setup.md)

**Status:** Not approved for the initial 12-task allocation without a measured, zero-cost environment test and verified available quota.

### Lightweight alternative: MBPP+ or LiveCodeBench

- [MBPP+ / EvalPlus](https://github.com/evalplus/evalplus) augments the sanitized MBPP problems with roughly 35 times as many tests on average. Its current v0.2.0 set has 378 tasks. It is small enough to be a more plausible candidate for a 10–12-task pilot, but it measures Python function generation, not repository-level issue resolution. Its prompts are public and old, so contamination is a limitation.
- [LiveCodeBench](https://github.com/livecodebench/livecodebench) provides versioned coding tasks and code-generation, code-execution, test-output-prediction, and self-repair scenarios. It is a better candidate if the goal is coding reasoning/code repair rather than real repository patching. The official runner can use closed API models, but generated code still needs isolated evaluation. Its published releases must be pinned and its task age/model cutoff disclosed.

Neither alternative should be silently presented as SWE-bench or as repository-level software engineering.

## 3. Reasoning / research benchmark: GAIA

The official [GAIA dataset card](https://huggingface.co/datasets/gaia-benchmark/GAIA) describes public development/validation material and a gated dataset. Access requires the operator to accept the dataset conditions; the card specifically prohibits resharing the submissions set outside a gated or private repository. It also warns against redistributing the validation or test set in a crawlable format.

**Status:** Conditional candidate for up to 10 tasks from an authorized public development/validation split, after the operator accepts the terms. Do not commit task content, attachments, answer keys, or gated data to this public repository. Keep task IDs and non-sensitive metadata only.

## 4. Browser benchmark

### WebArena

The original WebArena environment uses multiple self-hosted website services. Its official Docker guide recommends an AWS `t3a.xlarge` instance and a 1,000 GB EBS root volume for the preinstalled environment. This is not compatible with the strict zero-spend requirement if provisioned as described. WebArena-Verified slim images reduce some image sizes but still include large services.

Sources:
- [WebArena Docker environment guide](https://github.com/web-arena-x/webarena/blob/main/environment_docker/README.md)
- [WebArena-Verified environment sizes](https://servicenow.github.io/webarena-verified/getting_started/environments/)

**Status:** No-go for the initial free-tier plan.

### Better-fit candidate: AssistantBench

[AssistantBench](https://assistantbench.github.io/) has 214 realistic web tasks and publishes a development/validation set with answers, URLs, and explanations; its test answers remain hidden. The [BrowserGym AssistantBench integration](https://pypi.org/project/browsergym-assistantbench/) says the benchmark uses open-ended tasks and requires only installing the package, rather than self-hosting the WebArena website stack. BrowserGym then uses Playwright/Chromium.

A public [Inspect Evals implementation](https://ukgovernmentbeis.github.io/inspect_evals/evals/assistant_bench/index.html) reports that the publicly accessible validation set has 33 samples. That is enough to select 10 tasks, but it is a small sample and depends on live websites remaining accessible and materially unchanged.

**Status:** Conditional candidate for up to 10 validation tasks. Record the retrieval date, URLs, browser version, and task/evaluator versions. Mark tasks invalid or unavailable if the target page has materially changed; do not replace them silently.

## 5. Model availability and cost

### JEV and OpenCode worker

The JEV README and `src/main.py` configure the worker model as `gemini-3.5-flash-lite`. Google's current Gemini API pricing page lists this exact model with free-tier input and output tokens. The free tier is limited by model-specific quotas, and Google states that free-tier content may be used to improve its products. Google Search grounding is not available for this model on the free tier.

Sources:
- [Gemini 3.5 Flash-Lite model page](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite)
- [Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing)

This makes the model a plausible zero-cost option for JEV/ OpenCode worker calls, provided the user's Google AI Studio project actually has free-tier access and remaining quota. Account-specific access and limits were not inspected. Do not enable paid tier or grounding.

### Antigravity

The official Antigravity model selector lists **Gemini 3.8 Flash** (not "Gemini 3.8 Flash-Lite") as available on Free and Google AI Plus plans. It displays five-hour and weekly usage limits. The user's plan and remaining quota are unknown.

Source: [Antigravity model selection](https://antigravity.google/docs/models)

For the comparison, record the exact label as `Gemini 3.8 Flash`. Do not write "3.8 Flash-Lite" unless the product itself exposes that exact model name.

## 6. Full JEV gate: hard zero-cost blocker

The checked-in JEV README requires `JEV_API_URL` and `JEV_API_KEY` for the production semantic gate. The code in `src/jev/gatekeeper.py` fails initialization if either is missing; there is no local fallback. The official TypeSafe support article, updated November 3, 2026, says Jev costs $0.042 per million input tokens and that no free or trial credits are offered.

Sources:
- [JEV README](https://github.com/davyjones7321/jev-state-engine/blob/master/README.md)
- [JEV gatekeeper implementation](https://github.com/davyjones7321/jev-state-engine/blob/master/src/jev/gatekeeper.py)
- [TypeSafe: How much does Jev cost?](https://help.typesafe.ai/articles/5187202169-how-much-does-jev-cost)

**Status:** Full production JEV is blocked under a strict zero-additional-cost policy unless the user already has prepaid credit that they explicitly authorize using. Do not make a paid request to test whether the key works.

A Tier-0-only or alternate-judge JEV variant could be studied as a separate ablation, but it is not the production JEV system and must be labelled separately. Do not silently bypass the semantic gate.

## 7. Additional JEV execution-isolation blocker

The JEV README warns that target build/test commands execute directly on the host and that Git worktrees are not an OS sandbox. Source inspection confirms:

- `src/main.py` places `GOOGLE_API_KEY` in the process environment when initializing the Gemini worker.
- `src/jev/process_runner.py` passes `env=None` to `subprocess.Popen` by default, which means child commands inherit the parent environment.

Therefore, running untrusted benchmark repository code or generated code as JEV target commands while provider credentials are present can expose those credentials to the executed code. Process-group cleanup and worktree isolation do not prevent credential access.

**Status:** Do not run JEV against untrusted benchmark code until a separately reviewed isolation/credential-boundary solution is established. This review does not change JEV runtime code.

## 8. Recommended revised pilot, subject to approval

| Category | Candidate | Proposed distinct tasks | Status |
|---|---|---:|---|
| Coding | MBPP+ or LiveCodeBench code-generation / self-repair subset | 12 | Candidate only; not repository-level issue resolution |
| Reasoning / research | GAIA authorized public validation/development subset | 10 | Conditional on accepting access terms |
| Browser / web | AssistantBench public validation subset | 10 | Conditional on live-page stability |

This yields 32 distinct task instances. If three agents are eligible on every task, it can produce up to 96 agent-task runs. Eligibility must be decided per agent and per category; do not assume JEV has browser/research tools merely because it can run code.

If repository-level software engineering is non-negotiable, retain SWE-bench as a later phase and do not replace it with MBPP+/LiveCodeBench without clearly changing the claim.

## 9. Items not verifiable from connected repository or public documentation

The following require the user's account view or explicit confirmation:

- Remaining Codespaces compute/storage quota and whether the GitHub account is personal Free/Pro or organization-managed.
- Whether any payment method is attached and whether overage spending is stopped.
- Remaining Gemini API free-tier quota for the user's Google AI Studio project.
- Antigravity plan and remaining five-hour/weekly quota.
- Whether the user already has unused prepaid TypeSafe Jev credits.
- Whether OpenCode and Antigravity are configured with the same browser tools and whether Antigravity CLI can be operated in the chosen remote environment.

No credentials or account secrets should be pasted into chat.

## 10. Decision gate before implementation

Do not install benchmark packages, create a Codespace, start an Action workflow, invoke model APIs, run benchmark code, or provision any cloud resource until:

1. The user approves the revised benchmark scope or explicitly retains the heavier original suites.
2. The user confirms the TypeSafe credit situation or approves a clearly labelled JEV ablation.
3. The user verifies actual free-tier quota and overage protection.
4. The execution design protects provider credentials from benchmark/task code.
