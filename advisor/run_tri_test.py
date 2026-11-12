import argparse
import os
import sys
from pathlib import Path

# Add project root and src/ to sys.path
repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(repo_root))
sys.path.insert(0, str(repo_root / "src"))

from dotenv import dotenv_values
from jev.engine import JevEngine
from jev.gatekeeper import Gatekeeper
from jev.workspace import Workspace
from advisor.advisor_gatekeeper import AdvisorGatekeeper
from advisor.tri_shadow_gatekeeper import TriShadowGatekeeper


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run Jev State Engine with Tri-Shadow Evaluation (Jev + Advisor Model + Alternative Decision Model)"
    )
    parser.add_argument(
        "ticket",
        type=str,
        help="The ticket or task description for the engine to execute",
    )
    parser.add_argument(
        "--repo-dir",
        type=str,
        default=None,
        help="Path to the repository to execute within",
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default="checkpoints.db",
        help="Path to the SQLite checkpoint database file",
    )
    parser.add_argument(
        "--thread-id",
        type=str,
        default="tri-shadow-run-1",
        help="Execution thread identifier",
    )
    parser.add_argument(
        "--test-policy",
        type=str,
        choices=["auto", "verify-only", "never", "always"],
        default="verify-only",
        help="Execution policy for repository test suites",
    )
    parser.add_argument(
        "--advisor-url",
        "--deepseek-url",
        dest="advisor_url",
        type=str,
        default="https://letter-consensus-bottle-hammer.trycloudflare.com",
        help="Public Cloudflare Tunnel URL for Kaggle Advisor Model endpoint",
    )
    parser.add_argument(
        "--skip-alternative-model",
        "--skip-laya",
        dest="skip_alternative",
        action="store_true",
        help="Skip local Alternative Decision Model CPU evaluation for faster iteration (evaluates Jev + Advisor Model)",
    )
    parser.add_argument(
        "--test-eval-only",
        action="store_true",
        help="Run a single synthetic shadow evaluation test without running the full state engine",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    repo_dir = Path(args.repo_dir).resolve() if args.repo_dir else repo_root.resolve()
    engine_env = repo_root / ".env"
    env_file = engine_env if engine_env.exists() else ((repo_dir / ".env") if (repo_dir / ".env").exists() else None)

    print("Initializing Primary Gatekeeper (Jev)...")
    primary_gatekeeper = Gatekeeper(env_file=env_file)

    print(f"Connecting to Advisor Model (Kaggle GPU) at {args.advisor_url}...")
    advisor_gatekeeper = AdvisorGatekeeper(endpoint_url=args.advisor_url)

    alt_agent = None
    if not args.skip_alternative:
        print("Initializing Alternative Decision Model (Local CPU)...")
        try:
            import laya
            alt_agent = laya.load("convaiinnovations/laya-typed-decisions")
        except Exception as e:
            print(f"[WARNING] Could not load Alternative Decision Model: {e}. Proceeding with Jev and Advisor Model only.")
            alt_agent = None
    else:
        print("Alternative Decision Model (CPU) evaluation skipped.")

    # Target comparison log
    log_file = repo_dir / "advisor" / "judge_tri_comparison.jsonl" if args.repo_dir else repo_root / "judge_tri_comparison.jsonl"
    tri_gatekeeper = TriShadowGatekeeper(
        primary_gatekeeper=primary_gatekeeper,
        advisor_gatekeeper=advisor_gatekeeper,
        alternative_model_agent=alt_agent,
        log_path=log_file,
    )

    if args.test_eval_only:
        print("\n--- Running Test Evaluation across all active judges ---")
        from jev.models import Subgoal
        test_sg = Subgoal(description="Create a test contact component", scope=["src/app/contact/page.tsx"])
        sample_diff = "+export default function Contact() { return <div>Contact</div>; }"
        verdict = tri_gatekeeper.validate_subgoal(test_sg, diff=sample_diff)
        print(f"\nFinal authoritative verdict returned to engine: valid={verdict.valid}")
        return 0

    workspace = Workspace(repo_dir=repo_dir)

    google_api_key = os.environ.get("GOOGLE_API_KEY")
    if not google_api_key and env_file and env_file.exists():
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
            llm = None

    engine = JevEngine(
        workspace=workspace,
        gatekeeper=tri_gatekeeper,
        llm=llm,
        db_path=args.db_path,
        test_policy=args.test_policy,
    )

    print(f"\nStarting Jev Engine with Tri-Shadow Gatekeeper on Ticket: '{args.ticket}'")
    final_state = engine.execute(ticket=args.ticket, thread_id=args.thread_id)

    status = final_state.get("status")
    gate_status = final_state.get("gate_status")
    print(f"\nEngine execution finished with status: {status} (gate_status: {gate_status})")
    print(f"Tri-Evaluation metrics logged to: {log_file}")
    return 0 if status == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
