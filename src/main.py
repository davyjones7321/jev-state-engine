import argparse
import os
import sys
from pathlib import Path
from typing import List, Optional

from dotenv import dotenv_values

from jev.engine import JevEngine
from jev.gatekeeper import Gatekeeper
from jev.workspace import Workspace


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Jev State Engine: Deterministic State Machine with Two-Tier Gating"
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
        help="Path to the repository to execute within (defaults to current working directory)",
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default="checkpoints.db",
        help="Path to the SQLite checkpoint database file (defaults to checkpoints.db)",
    )
    parser.add_argument(
        "--thread-id",
        type=str,
        default="main-thread",
        help="Execution thread identifier for checkpoint persistence",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    repo_dir = Path(args.repo_dir).resolve() if args.repo_dir else Path.cwd().resolve()

    # Wire Dependency Injection
    workspace = Workspace(repo_dir=repo_dir)
    env_file = (repo_dir / ".env") if (repo_dir / ".env").exists() else (Path.cwd() / ".env" if (Path.cwd() / ".env").exists() else None)
    gatekeeper = Gatekeeper(env_file=env_file)

    # Wire Gemini LLM (gemini-3.5-flash) via langchain-google-genai
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
                model="gemini-3.5-flash",
                google_api_key=google_api_key,
            )
        except Exception as e:
            print(f"[WARNING] Could not initialize ChatGoogleGenerativeAI: {e}")
            llm = None

    engine = JevEngine(
        workspace=workspace,
        gatekeeper=gatekeeper,
        llm=llm,
        db_path=args.db_path,
    )

    final_state = engine.execute(ticket=args.ticket, thread_id=args.thread_id)

    status = final_state.get("status")
    gate_status = final_state.get("gate_status")

    if status == "escalated":
        print(f"[ESCALATION] Engine halted. Triggering status: {gate_status}")
        return 1
    elif status != "completed":
        print(f"[FAILURE] Engine halted with status: {status}, gate_status: {gate_status}")
        return 1

    print(f"[SUCCESS] Ticket executed successfully. Final status: {status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
