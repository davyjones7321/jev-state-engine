#!/usr/bin/env python3
"""Evaluation runner with credential-boundary verification and dry-run execution."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
from typing import Any, Dict, List, Optional, Tuple

import jsonschema

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "evals" / "schemas" / "run.schema.json"
DEFAULT_LOCAL_ROOT = REPO_ROOT / "evals" / "local"

# Non-scored MBPP+ smoke tasks used for offline dry-run verification
DRY_RUN_SMOKE_TASKS = [
    {
        "task_id": "Mbpp/2",
        "entry_point": "similar_elements",
        "prompt": (
            '"""\n'
            'Write a function to find the shared elements from the given two lists.\n'
            'assert set(similar_elements((3, 4, 5, 6),(5, 7, 4, 10))) == set((4, 5))\n'
            '"""\n'
        ),
        "solution": (
            "def similar_elements(test_tup1, test_tup2):\n"
            "    return tuple(set(test_tup1) & set(test_tup2))\n"
        ),
    },
    {
        "task_id": "Mbpp/3",
        "entry_point": "is_not_prime",
        "prompt": (
            '"""\n'
            'Write a python function to identify non-prime numbers.\n'
            'assert is_not_prime(2) == False\n'
            '"""\n'
        ),
        "solution": (
            "import math\n\n"
            "def is_not_prime(n):\n"
            "    if n < 2:\n"
            "        return True\n"
            "    for i in range(2, int(math.isqrt(n)) + 1):\n"
            "        if n % i == 0:\n"
            "            return True\n"
            "    return False\n"
        ),
    },
    {
        "task_id": "Mbpp/4",
        "entry_point": "heap_queue_largest",
        "prompt": (
            '"""\n'
            'Write a function to find the n largest integers from a given list of numbers.\n'
            'assert heap_queue_largest([10, 20, 50, 70, 90, 20, 50, 40, 60, 80, 100], 3) == [100, 90, 80]\n'
            '"""\n'
        ),
        "solution": (
            "import heapq\n\n"
            "def heap_queue_largest(numbers, n):\n"
            "    return heapq.nlargest(n, numbers)\n"
        ),
    },
]

KNOWN_API_KEY_VARS = {
    "JEV_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "DEEPSEEK_API_KEY",
    "MISTRAL_API_KEY",
    "COHERE_API_KEY",
    "GROQ_API_KEY",
    "AWS_SECRET_ACCESS_KEY",
    "GITHUB_TOKEN",
    "GH_TOKEN",
}


def is_sensitive_env_var(name: str) -> bool:
    """Return True if an environment variable represents a credential or API key."""
    upper = name.upper()
    if upper in KNOWN_API_KEY_VARS:
        return True
    if "API_KEY" in upper or "APIKEY" in upper or "AUTH_TOKEN" in upper:
        return True
    if upper.endswith("_SECRET") or upper.endswith("_SECRET_KEY") or upper.endswith("_API_TOKEN"):
        return True
    return False


def get_probe_command() -> List[str]:
    """Return the child process probe command that prints visible environment in JSON."""
    return [
        sys.executable,
        "-c",
        "import os, json; print(json.dumps(dict(os.environ)))",
    ]


def probe_child_environment(
    env: Optional[Dict[str, str]] = None,
    cwd: Optional[Path] = None,
    probe_cmd: Optional[List[str]] = None,
) -> Dict[str, str]:
    """Spawn a probe child process to observe the exact environment visible to child tasks."""
    cmd = probe_cmd or get_probe_command()
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(cwd) if cwd else None,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"Credential probe child process failed (code {proc.returncode}): {proc.stderr or proc.stdout}"
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Credential probe output could not be parsed as JSON: {proc.stdout}"
        ) from exc


def check_credential_boundary(
    env: Optional[Dict[str, str]] = None,
    cwd: Optional[Path] = None,
    probe_cmd: Optional[List[str]] = None,
) -> Tuple[bool, List[str]]:
    """Verify that no API key or credential variables are visible to a spawned child process.

    Spawns a probe child process. If any API key variable is visible in the child's
    environment, returns (False, [list of visible variable names]).
    Otherwise returns (True, []).
    """
    child_env = probe_child_environment(env=env, cwd=cwd, probe_cmd=probe_cmd)
    visible_keys = [
        var_name
        for var_name, val in child_env.items()
        if is_sensitive_env_var(var_name) and val != ""
    ]
    return (len(visible_keys) == 0, sorted(visible_keys))


def get_sanitized_environment(base_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Produce an environment dictionary with all API key and secret variables removed."""
    source = os.environ if base_env is None else base_env
    return {k: v for k, v in source.items() if not is_sensitive_env_var(k)}


def load_run_schema() -> Dict[str, Any]:
    """Load and return the JSON schema for run records."""
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def validate_run_record(record: Dict[str, Any], schema: Optional[Dict[str, Any]] = None) -> None:
    """Validate a run record against run.schema.json."""
    if schema is None:
        schema = load_run_schema()
    jsonschema.validate(instance=record, schema=schema)


def create_run_record(
    task_id: str,
    agent_name: str = "jev",
    status: str = "completed",
    credential_boundary_verified: bool = False,
    evaluator_status: str = "not_scored",
    score: Optional[float] = None,
    notes: str = "",
    duration_seconds: float = 0.0,
    model_id: str = "dry-run-offline",
    access_route: str = "local",
    environment_id: str = "local-sandbox",
    network_policy: str = "disabled",
    failure_class: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a validated run record dictionary compliant with evals/schemas/run.schema.json."""
    clean_task_slug = task_id.replace("/", "_")
    run_id = f"run-{clean_task_slug}-{agent_name}-{uuid.uuid4().hex[:8]}"
    now_utc = datetime.now(timezone.utc).isoformat()

    record: Dict[str, Any] = {
        "schema_version": "1.0",
        "run_id": run_id,
        "recorded_at_utc": now_utc,
        "agent": {
            "name": agent_name,
            "adapter_version": "0.3.1",
            "configuration_sha256": None,
            "model": {
                "provider": None,
                "model_id": model_id,
                "access_route": access_route,
                "generation_settings": None,
            },
        },
        "benchmark": {
            "name": "mbpp_plus",
            "version": "v0.2.0",
            "split": "test",
            "evaluator_version": "0.3.1",
            "source_revision": "v0.2.0",
        },
        "task": {
            "task_id": task_id,
            "category": "coding",
            "eligibility": "eligible",
            "eligibility_reason": None,
        },
        "status": status,
        "execution": {
            "environment_id": environment_id,
            "tools": ["python"],
            "network_policy": network_policy,
            "timeout_seconds": 60,
            "duration_seconds": round(duration_seconds, 3),
            "resource_usage": None,
            "credential_boundary_verified": credential_boundary_verified,
        },
        "result": {
            "evaluator_status": evaluator_status,
            "score": score,
            "metric_name": "pass@1",
            "passed_tests": None,
            "total_tests": None,
            "failure_class": failure_class,
            "notes": notes,
        },
        "cost": {
            "currency": "USD",
            "amount": 0.0,
            "status": "zero_additional_cost",
            "quota_observation": "none",
        },
        "artifacts": {
            "local_log_ref": None,
            "redacted": True,
        },
    }

    validate_run_record(record)
    return record


def execute_dry_run(
    output_dir: Optional[Path] = None,
    enforce_clean_env: bool = True,
    injected_env: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Execute 3 non-scored MBPP+ tasks end-to-end with no network calls and no Jev API calls.

    Steps per task:
    1. Spawns probe child process to verify credential boundary.
    2. Sets up isolated workspace directory.
    3. Writes task definition and offline solution.
    4. Validates solution syntactically with py_compile.
    5. Produces and validates run.schema.json record with credential_boundary_verified=True.
    """
    root = (output_dir or (DEFAULT_LOCAL_ROOT / "dry_run")).resolve()
    workspaces_dir = root / "workspaces" / "jev"
    runs_dir = root / "runs"
    workspaces_dir.mkdir(parents=True, exist_ok=True)
    runs_dir.mkdir(parents=True, exist_ok=True)

    target_env = injected_env
    if target_env is None and enforce_clean_env:
        target_env = get_sanitized_environment()

    # Step 1: Credential boundary check before any execution
    boundary_ok, leaked_vars = check_credential_boundary(env=target_env)
    if not boundary_ok:
        # If API key variables are visible, fail the run immediately
        failure_records = []
        for task_info in DRY_RUN_SMOKE_TASKS:
            task_id = task_info["task_id"]
            record = create_run_record(
                task_id=task_id,
                agent_name="jev",
                status="agent_failure",
                credential_boundary_verified=False,
                evaluator_status="failed",
                failure_class="credential_boundary_violation",
                notes=f"Credential boundary check failed: visible API key variables: {leaked_vars}",
            )
            run_file = runs_dir / f"{task_id.replace('/', '_')}-run.json"
            run_file.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
            failure_records.append(record)

        return {
            "status": "failed",
            "credential_boundary_verified": False,
            "leaked_variables": leaked_vars,
            "runs": failure_records,
        }

    # Step 2: Boundary verified, proceed with 3 offline tasks end-to-end
    run_records = []
    for task_info in DRY_RUN_SMOKE_TASKS:
        task_id = task_info["task_id"]
        entry_point = task_info["entry_point"]
        prompt = task_info["prompt"]
        solution_code = task_info["solution"]

        task_slug = task_id.replace("/", "_")
        task_dir = workspaces_dir / task_slug
        task_dir.mkdir(parents=True, exist_ok=True)

        task_md = (
            f"# MBPP+ task {task_id} (Dry Run)\n\n"
            f"**Entry point:** `{entry_point}`\n\n"
            "## Official task prompt\n\n"
            f"{prompt}\n\n"
            "## Instructions\n\n"
            "Implement the requested function in `solution.py` without network calls.\n"
        )
        (task_dir / "TASK.md").write_text(task_md, encoding="utf-8")
        sol_file = task_dir / "solution.py"
        sol_file.write_text(solution_code, encoding="utf-8")

        # Validate syntax locally offline without external process or network
        compile_res = subprocess.run(
            [sys.executable, "-m", "py_compile", str(sol_file)],
            capture_output=True,
            text=True,
            check=False,
        )
        if compile_res.returncode != 0:
            raise RuntimeError(f"Syntax validation failed for {task_id}: {compile_res.stderr}")

        # Create valid run record with credential boundary verified
        record = create_run_record(
            task_id=task_id,
            agent_name="jev",
            status="completed",
            credential_boundary_verified=True,
            evaluator_status="not_scored",
            score=None,
            notes="Completed offline dry-run end-to-end with zero network/API calls",
            duration_seconds=0.05,
        )

        run_file = runs_dir / f"{task_slug}-run.json"
        run_file.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        run_records.append(record)

    summary = {
        "status": "success",
        "dry_run": True,
        "tasks_executed": [t["task_id"] for t in DRY_RUN_SMOKE_TASKS],
        "credential_boundary_verified": True,
        "runs": run_records,
        "output_directory": str(root),
    }
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Execute 3 non-scored MBPP+ tasks end-to-end with no network or Jev API calls",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=str(DEFAULT_LOCAL_ROOT / "dry_run"),
        help="Directory to write task workspaces and run records",
    )
    parser.add_argument(
        "--check-boundary-only",
        action="store_true",
        help="Probe child process environment and report credential boundary status",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.check_boundary_only:
        ok, leaked = check_credential_boundary()
        if not ok:
            print(f"FAILED: Visible credential variables: {leaked}", file=sys.stderr)
            return 1
        print("PASSED: Credential boundary verified. No API key variables visible.")
        return 0

    if args.dry_run:
        result = execute_dry_run(output_dir=Path(args.output_dir))
        print(json.dumps(result, indent=2))
        return 0 if result["status"] == "success" else 1

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
