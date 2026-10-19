import argparse
import os
import sys
from pathlib import Path
from typing import List, Optional

from dotenv import dotenv_values

from jev.dependency_delta import format_dependency_summary
from jev.engine import JevEngine
from jev.evidence import format_evidence_summary
from jev.gatekeeper import Gatekeeper
from jev.ledger import NullRecorder, SqliteLedger
from jev.advisory import AdvisoryService
from jev.providers import OpenAICompatibleProvider
from jev.workspace import Workspace


import uuid
from datetime import datetime, timezone


def generate_default_thread_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rnd = uuid.uuid4().hex[:4]
    return f"run-{ts}-{rnd}"


def positive_int(value: str) -> int:
    try:
        val = int(value)
        if val <= 0:
            raise ValueError()
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError(f"'{value}' is not a positive integer")
    return val


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
        default=generate_default_thread_id(),
        help="Execution thread identifier for checkpoint persistence (defaults to run-<utc-timestamp>-<4 hex>)",
    )
    parser.add_argument(
        "--test-policy",
        type=str,
        choices=["auto", "verify-only", "never", "always"],
        default="auto",
        help="Execution policy for repository test suites (auto, verify-only, never, always; defaults to auto)",
    )
    parser.add_argument(
        "--no-ledger",
        action="store_true",
        default=False,
        help="Disable recording to SQLite ledger",
    )
    parser.add_argument(
        "--retry-intel",
        choices=["on", "off"],
        default="on",
        help="Enable or disable retry intelligence (dead-end block, duplicate-diff guard, typed semantic feedback). Default: on.",
    )
    parser.add_argument(
        "--repo-profile",
        choices=["on", "off"],
        default="off",
        help="Inject repository profile into plan and implement prompts (on, off; defaults to off).",
    )
    parser.add_argument(
        "--test-timeout",
        type=positive_int,
        default=None,
        help="Timeout in seconds for test suite execution (positive integer; defaults to JEV_TEST_TIMEOUT or 600).",
    )
    parser.add_argument(
        "--dep-delta-jev",
        choices=["on", "off"],
        default="off",
        help="Send dependency delta to Jev as state evidence (on, off; defaults to off).",
    )
    parser.add_argument("--advisory-base-url", default=None, help="Optional OpenAI-compatible advisory API base URL")
    parser.add_argument("--advisory-model", default=None, help="Optional advisory model name")
    parser.add_argument("--advisory-api-key", default=None, help="Optional advisory API key (never recorded)")
    return parser


def build_advisory_service(base_url: Optional[str], model: Optional[str], api_key: Optional[str] = None):
    """Build the optional advisory adapter; incomplete configuration stays disabled."""
    if not base_url or not model:
        return None
    try:
        return AdvisoryService(OpenAICompatibleProvider(base_url, model, api_key=api_key))
    except (TypeError, ValueError):
        return None



def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    repo_dir = Path(args.repo_dir).resolve() if args.repo_dir else Path.cwd().resolve()

    # Wire Dependency Injection
    workspace = Workspace(repo_dir=repo_dir, test_timeout=args.test_timeout)
    engine_env = Path.cwd() / ".env"
    if not engine_env.exists():
        engine_env = Path(__file__).resolve().parent.parent / ".env"
    env_file = engine_env if engine_env.exists() else ((repo_dir / ".env") if (repo_dir / ".env").exists() else None)
    gatekeeper = Gatekeeper(env_file=env_file)

    parsed_env = dotenv_values(env_file) if env_file and env_file.exists() else {}
    advisory_base_url = args.advisory_base_url or os.environ.get("ADVISORY_BASE_URL") or parsed_env.get("ADVISORY_BASE_URL")
    advisory_model = args.advisory_model or os.environ.get("ADVISORY_MODEL") or parsed_env.get("ADVISORY_MODEL")
    advisory_key = args.advisory_api_key or os.environ.get("ADVISORY_API_KEY") or parsed_env.get("ADVISORY_API_KEY")
    advisory = build_advisory_service(advisory_base_url, advisory_model, advisory_key)

    if args.no_ledger:
        recorder = NullRecorder()
    else:
        try:
            recorder = SqliteLedger(repo_dir=repo_dir)
        except Exception as e:
            print(f"ledger disabled: {e}", file=sys.stderr, flush=True)
            recorder = NullRecorder()

    # Wire Gemini LLM (gemini-3.5-flash-lite) via langchain-google-genai
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
        gatekeeper=gatekeeper,
        llm=llm,
        recorder=recorder,
        db_path=args.db_path,
        test_policy=args.test_policy,
        retry_intel=args.retry_intel,
        repo_profile=args.repo_profile,
        dep_delta_jev=args.dep_delta_jev,
        advisory=advisory,
    )


    final_state = engine.execute(ticket=args.ticket, thread_id=args.thread_id)

    status = final_state.get("status")
    gate_status = final_state.get("gate_status")

    target_run_id = final_state.get("run_id") or args.thread_id

    ev_level = None
    if hasattr(recorder, "get_run_evidence_level"):
        ev_level = recorder.get_run_evidence_level(target_run_id)
    print(format_evidence_summary(ev_level, test_policy=args.test_policy))

    dep_delta_json = None
    if hasattr(recorder, "get_run_dependency_delta"):
        dep_delta_json = recorder.get_run_dependency_delta(target_run_id)
    print(format_dependency_summary(dep_delta_json))

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
