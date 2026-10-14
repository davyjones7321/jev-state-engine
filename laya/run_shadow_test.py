import argparse
import os
import sys
from pathlib import Path

# Setup paths
repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(repo_root / "src"))
sys.path.insert(0, str(repo_root / "laya"))

# Ensure HF cache is set safely if not already present
if "HF_HOME" not in os.environ:
    candidate_cache = Path("D:/edge-downloades/huggingface_cache")
    if candidate_cache.parent.exists():
        os.environ["HF_HOME"] = str(candidate_cache)

from dotenv import dotenv_values
import laya

from jev.engine import JevEngine
from jev.gatekeeper import Gatekeeper
from jev.models import Subgoal
from jev.workspace import Workspace
from shadow_gatekeeper import ShadowGatekeeper


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Shadow Testing Runner: Dual Jev + Laya Gatekeeper"
    )
    parser.add_argument(
        "ticket",
        type=str,
        nargs="?",
        default="Add a docstring to format_subgoal_summary in src/jev/models.py",
        help="The ticket or task description for the engine to execute",
    )
    parser.add_argument(
        "--test-eval-only",
        action="store_true",
        help="Quickly evaluate sample diffs through both Jev and Laya without running full LLM worker loop",
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default="checkpoints.db",
        help="Path to SQLite checkpoint database",
    )
    parser.add_argument(
        "--thread-id",
        type=str,
        default="shadow-test-thread",
        help="Execution thread identifier",
    )
    parser.add_argument(
        "--test-policy",
        type=str,
        choices=["auto", "verify-only", "never", "always"],
        default="auto",
    )
    return parser


def run_synthetic_eval(shadow: ShadowGatekeeper) -> None:
    print("\n--- Running Quick Dual-Evaluation Test ---")

    # 1. Valid diff test
    subgoal_valid = Subgoal(
        description="Add a docstring to format_summary in src/jev/models.py",
        scope=["src/jev/models.py"],
        expects_tests=False,
    )
    diff_valid = """
--- a/src/jev/models.py
+++ b/src/jev/models.py
@@ -10,3 +10,5 @@ def format_summary():
+    \"\"\"Formats the summary of subgoals into markdown.\"\"\"
     pass
"""
    print("\n[TEST 1] Evaluating Valid Diff:")
    verdict1 = shadow.validate_subgoal(subgoal_valid, diff_valid)
    print(f"Engine receives: valid={verdict1.valid}, prob={getattr(verdict1, 'probability', 1.0)}")

    # 2. Invalid diff test (scope creep)
    subgoal_invalid = Subgoal(
        description="Add a docstring in models.py",
        scope=["src/jev/models.py"],
        expects_tests=False,
    )
    diff_invalid = """
--- a/src/jev/workspace.py
+++ b/src/jev/workspace.py
@@ -1,3 +1,4 @@
+import secrets
"""
    print("\n[TEST 2] Evaluating Out-of-Scope Diff:")
    verdict2 = shadow.validate_subgoal(subgoal_invalid, diff_invalid)
    print(f"Engine receives: valid={verdict2.valid}, prob={getattr(verdict2, 'probability', 1.0)}")

    print(f"\n[DONE] Comparison log written to: {shadow.log_path}")


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    repo_dir = repo_root.resolve()
    env_file = repo_dir / ".env"

    print("Initializing Primary Gatekeeper (Jev)...")
    primary_gatekeeper = Gatekeeper(env_file=env_file if env_file.exists() else None)

    print("Initializing Shadow Evaluator (Laya on CPU)...")
    laya_agent = laya.load("convaiinnovations/laya-typed-decisions")

    log_file = repo_dir / "laya" / "judge_comparison.jsonl"
    shadow_gatekeeper = ShadowGatekeeper(
        primary_gatekeeper=primary_gatekeeper,
        laya_agent=laya_agent,
        log_path=log_file,
    )

    if args.test_eval_only:
        run_synthetic_eval(shadow_gatekeeper)
        return 0

    # Full Engine Run
    workspace = Workspace(repo_dir=repo_dir)

    google_api_key = os.environ.get("GOOGLE_API_KEY")
    if not google_api_key and env_file.exists():
        parsed_env = dotenv_values(env_file)
        google_api_key = parsed_env.get("GOOGLE_API_KEY")

    llm = None
    if google_api_key:
        os.environ["GOOGLE_API_KEY"] = google_api_key
        try:
            from langchain_google_genai import ChatGoogleGenerativeAI
            llm = ChatGoogleGenerativeAI(
                model="gemini-3.5-flash-lite",
                google_api_key=google_api_key,
            )
        except Exception as e:
            print(f"[WARNING] Could not initialize ChatGoogleGenerativeAI: {e}")

    engine = JevEngine(
        workspace=workspace,
        gatekeeper=shadow_gatekeeper,
        llm=llm,
        db_path=args.db_path,
        test_policy=args.test_policy,
    )

    print(f"\nStarting Jev Engine with Shadow Gatekeeper on Ticket: '{args.ticket}'")
    final_state = engine.execute(ticket=args.ticket, thread_id=args.thread_id)

    print(f"\nEngine execution finished with status: {final_state.get('status')}")
    print(f"Dual evaluation metrics logged to: {log_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
