# Jev State Engine — Setup & First Run

A deterministic FSM that orchestrates an LLM writing code, gated by a
two-tier check: deterministic mechanical checks (build/tests/scope), then
a real semantic judge (TypeSafe AI's Jev System One model) before
anything commits.

## ⚠️ Before you run this anywhere

**This system commits directly to the repo it's pointed at, and there is
no isolated worktree yet.** `Workspace.worktree_dir` currently defaults to
the same directory as the repo you point it at — `rollback_subgoal()`
runs `git reset --hard` and `git clean -fd` on that directory, and a
passed gate runs a real `git commit`. Real Git worktree isolation
(a separate branch per subgoal) is a known, tracked, not-yet-built gap.

**Do not point this at a real project you care about.** Set up a
throwaway sandbox repo specifically for testing it — see Step 3 below.

## 1. Clone and install

```bash
git clone <your-repo-url>
cd jev-state-engine
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
```

If `requirements.txt` isn't fully up to date, at minimum you'll need:
`pydantic`, `langgraph`, `langchain-core`, `langchain-google-genai`,
`httpx`, `python-dotenv`, `pytest`, `ruff`.

## 2. Set up your API keys

Create a `.env` file in the project root (this is gitignored — never
commit it):

```
JEV_API_URL=https://api.typesafe.ai/v1/systemone
JEV_API_KEY=your_typesafe_key_here
GOOGLE_API_KEY=your_gemini_key_here
```

- **JEV_API_KEY** — from `console.typesafe.ai`. TypeSafe AI access may be
  waitlist-gated; confirm you actually have a working key before running
  anything that reaches Step 4 of the pipeline (the real gate check).
- **GOOGLE_API_KEY** — from Google AI Studio (`aistudio.google.com`),
  not the Cloud Console, unless you're specifically using Vertex AI. This
  project currently uses `gemini-3.5-flash-lite`.

## 3. Set up a disposable sandbox repo to test against

Don't use this project's own repo as the test target. Create a separate,
throwaway one:

```bash
mkdir ~/jev-sandbox
cd ~/jev-sandbox
git init
git config user.name "Sandbox"
git config user.email "sandbox@example.com"
echo "def hello():\n    return 'hi'\n" > example.py
git add .
git commit -m "Initial commit"
```

This gives the FSM something real to investigate, plan against, and
mutate — safely, since it's disposable.

## 4. Run the test suite first

Before running anything against a live ticket, confirm the project's own
test suite is green in your environment:

```bash
pytest -v
```

You should see all tests pass (one test may show as `skipped` — that's
the live-API test for 7a, which only runs when explicitly enabled, so the
suite doesn't spend API calls on every run).

## 5. Run a real ticket

```bash
python -m src.main "Add a docstring to the hello function in example.py" \
    --repo-dir ~/jev-sandbox \
    --db-path checkpoints.db \
    --thread-id first-run
```

- `--repo-dir` points at your sandbox repo (Step 3), not this project's
  own directory.
- `--db-path` is where the LangGraph SQLite checkpoint is stored —
  defaults to `checkpoints.db` in the current directory if omitted.
- `--thread-id` identifies this run for checkpoint resumption — reuse the
  same ID to resume an interrupted run, or use a new one for a fresh run.

Exit code `0` means the ticket completed and committed. Exit code `1`
means it escalated (3-strike gate failure, or a planning failure) — check
`escalation.log` in the working directory for what happened and why.

## 6. What actually happens when you run it

1. **Investigate** — a real LLM call explores your sandbox repo read-only
   (`list_dir`, `grep`, `read_file`) and summarizes what it found.
2. **Plan** — a real LLM call turns the ticket into a validated list of
   subgoals, each with a declared file scope.
3. **Implement** — a real LLM call writes code for the first subgoal,
   directly into your sandbox repo's working directory.
4. **Gate** — deterministic checks run first (build/tests/scope); only if
   those pass does a real call go to Jev for a semantic verdict.
5. **Commit or retry** — a passing gate commits and moves to the next
   subgoal; a failing gate rolls back and retries (up to 3 strikes per
   failure type, tracked independently) before escalating.
6. Repeat until the plan queue is empty, then a final verification pass
   runs before the ticket is marked complete.

## Known limitations (not bugs — tracked, deferred work)

- **No real Git worktree isolation** — see the warning at the top.
- **No MCP-based tool gating** — write tools simply aren't bound to the
  Investigation-phase LLM call, rather than being physically unlinked at
  a protocol level. Functionally equivalent for now, architecturally
  weaker than the original design.
- **Path-containment logic is duplicated** across four locations
  (`read_file`, `run_read_tool`, `grep`, and the planner's scope
  validator) rather than centralized — tracked as a follow-up cleanup
  task in `docs/TASK.md`.
