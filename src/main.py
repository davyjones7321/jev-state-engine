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


DEFAULT_WORKER_MODEL = "gemini-3.5-flash-lite"


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


def non_empty_str(value: str) -> str:
    if value is None or not value.strip():
        raise argparse.ArgumentTypeError("Model name cannot be empty or whitespace-only")
    return value.strip()


def resolve_worker_model(cli_model: Optional[str] = None) -> str:
    if cli_model is not None:
        if not cli_model.strip():
            raise argparse.ArgumentTypeError("Model name cannot be empty or whitespace-only")
        return cli_model.strip()
    env_model = os.environ.get("JEV_WORKER_MODEL")
    if env_model is not None:
        if not env_model.strip():
            raise argparse.ArgumentTypeError("JEV_WORKER_MODEL cannot be empty or whitespace-only")
        return env_model.strip()
    return DEFAULT_WORKER_MODEL


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Jev State Engine: Deterministic State Machine with Two-Tier Gating"
    )
    parser.add_argument(
        "ticket",
        type=str,
        nargs="?",
        default="",
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
    parser.add_argument(
        "--model",
        "--worker-model",
        dest="model",
        type=non_empty_str,
        default=None,
        help="Worker LLM model name (defaults to JEV_WORKER_MODEL or gemini-3.5-flash-lite).",
    )
    parser.add_argument("--advisory-base-url", default=None, help="Optional OpenAI-compatible advisory API base URL")
    parser.add_argument("--advisory-model", default=None, help="Optional advisory model name")
    parser.add_argument("--advisory-api-key", default=None, help="Optional advisory API key (never recorded)")
    parser.add_argument(
        "--advisory-policy",
        choices=["off", "on", "required"],
        default="on",
        help="Advisory policy for diagnostic assistance on failure (off, on, required; defaults to on).",
    )
    parser.add_argument(
        "--memory-policy",
        choices=["off", "on"],
        default="off",
        help="Episodic in-run memory policy (off, on; defaults to off).",
    )
    parser.add_argument(
        "--replanning-policy",
        choices=["off", "on"],
        default="off",
        help="Dynamic replanning policy on dead ends (off, on; defaults to off).",
    )
    parser.add_argument(
        "--advisor-healthcheck",
        action="store_true",
        default=False,
        help="Perform a one-shot healthcheck against the configured advisory endpoint and exit.",
    )
    parser.add_argument(
        "--env-file",
        type=str,
        default=None,
        help="Path to an explicit environment file containing engine credentials.",
    )
    return parser


def build_advisory_service(base_url: Optional[str], model: Optional[str], api_key: Optional[str] = None):
    """Build the optional advisory adapter; incomplete configuration stays disabled."""
    if not base_url or not model:
        return None
    try:
        return AdvisoryService(OpenAICompatibleProvider(base_url, model, api_key=api_key, timeout=20.0))
    except (TypeError, ValueError):
        return None


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Resolve Engine Root and Environment strictly without cwd/target-repo discovery
    engine_root = Path(__file__).resolve().parent.parent
    engine_env = engine_root / ".env"
    env_file = Path(args.env_file).resolve() if args.env_file else (engine_env if engine_env.exists() else None)
    parsed_env = dotenv_values(env_file) if env_file and env_file.exists() else {}

    advisory_base_url = args.advisory_base_url or os.environ.get("ADVISORY_BASE_URL") or parsed_env.get("ADVISORY_BASE_URL")
    advisory_model = args.advisory_model or os.environ.get("ADVISORY_MODEL") or parsed_env.get("ADVISORY_MODEL")
    advisory_key = args.advisory_api_key or os.environ.get("ADVISORY_API_KEY") or parsed_env.get("ADVISORY_API_KEY")

    if args.advisor_healthcheck:
        if not advisory_base_url or not advisory_model:
            print("[ADVISOR HEALTHCHECK] FAILED: Advisory service is not configured (missing base URL or model).", file=sys.stderr)
            return 1
        advisory = build_advisory_service(advisory_base_url, advisory_model, advisory_key)
        if not advisory:
            print("[ADVISOR HEALTHCHECK] FAILED: Could not initialize advisory service.", file=sys.stderr)
            return 1
        res = advisory.request_repair_brief("Healthcheck probe: Return a brief repair brief for a dummy error.")
        if res.ok:
            print(f"[ADVISOR HEALTHCHECK] SUCCESS: Advisory model '{res.model}' responded successfully.")
            return 0
        else:
            print(f"[ADVISOR HEALTHCHECK] FAILED: {res.error_code} - {res.error_message}", file=sys.stderr)
            return 1

    if not args.ticket:
        parser.error("the following arguments are required: ticket")

    try:
        model = resolve_worker_model(args.model)
    except argparse.ArgumentTypeError as e:
        parser.error(str(e))

    repo_dir = Path(args.repo_dir).resolve() if args.repo_dir else Path.cwd().resolve()

    # Wire Dependency Injection
    workspace = Workspace(repo_dir=repo_dir, test_timeout=args.test_timeout, strict_provenance=True)
    gatekeeper = Gatekeeper(env_file=env_file)

    if args.advisory_policy == "off":
        advisory = None
    else:
        advisory = build_advisory_service(advisory_base_url, advisory_model, advisory_key)
        if args.advisory_policy == "required" and advisory is None:
            print("[ERROR] Advisory policy is 'required' but advisory service could not be initialized.", file=sys.stderr)
            return 1

    if args.no_ledger:
        recorder = NullRecorder()
    else:
        try:
            recorder = SqliteLedger(repo_dir=repo_dir)
        except Exception as e:
            print(f"ledger disabled: {e}", file=sys.stderr, flush=True)
            recorder = NullRecorder()

    # Wire Gemini LLM via langchain-google-genai
    google_api_key = os.environ.get("GOOGLE_API_KEY")
    if not google_api_key and env_file and env_file.exists():
        google_api_key = parsed_env.get("GOOGLE_API_KEY")

    llm = None
    if google_api_key:
        os.environ["GOOGLE_API_KEY"] = google_api_key
        try:
            from langchain_google_genai import ChatGoogleGenerativeAI

            llm = ChatGoogleGenerativeAI(
                model=model,
                google_api_key=google_api_key,
            )
        except Exception as e:
            print(f"[WARNING] Could not initialize ChatGoogleGenerativeAI: {e}")
            llm = None

    replan_enabled = (args.replanning_policy == "on")
    memory = None
    if args.memory_policy == "on":
        try:
            from jev.memory import EpisodicMemory
            memory = EpisodicMemory(repo_dir / ".jev_memory.jsonl")
        except Exception:
            memory = None

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
        advisory_policy=args.advisory_policy,
        replan_enabled=replan_enabled,
        memory=memory,
    )

    final_state = engine.execute(ticket=args.ticket, thread_id=args.thread_id)

    status = final_state.get("status")
    gate_status = final_state.get("gate_status")

    target_run_id = final_state.get("run_id") or args.thread_id

    ev_level = None
    if hasattr(recorder, "get_run_evidence_level"):
        ev_level = recorder.get_run_evidence_level(target_run_id)
    if ev_level and ev_level not in ("tests+compile", "tests-only") and args.test_policy == "auto":
        committed_sgs = final_state.get("committed_subgoals") or []
        unv_kinds = final_state.get("mechanically_unverified_kinds") or {}
        if committed_sgs and all(not getattr(sg, "expects_tests", True) for sg in committed_sgs):
            print(f"Evidence: {ev_level} (no tests ran: expects_tests=false)")
        elif any(k == "untested" for k in unv_kinds.values()):
            print(f"Evidence: {ev_level} (no tests ran: subgoals do not expect tests)")
        else:
            traj = final_state.get("trajectory") or []
            no_cmd = any(
                (entry.get("mech_evidence") or {}).get("reason") in ("NO_COMMAND", "MISSING_TOOL")
                for entry in traj if isinstance(entry, dict)
            )
            if no_cmd:
                print(f"Evidence: {ev_level} (no tests ran: no test framework configured)")
            else:
                print(f"Evidence: {ev_level} (no tests ran: subgoals do not expect tests)")
    else:
        print(format_evidence_summary(ev_level, test_policy=args.test_policy))

    unverified_subgoals = final_state.get("mechanically_unverified_subgoals") or []
    if len(unverified_subgoals) > 0:
        ids_str = ", ".join(unverified_subgoals)
        print(f"Mechanically unverified subgoals: {len(unverified_subgoals)} ({ids_str})")

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
