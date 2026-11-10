#!/usr/bin/env python3
"""Prepare and score a small MBPP+ subset with the official EvalPlus evaluator."""
from __future__ import annotations
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

EVALPLUS_VERSION = "0.3.1"
MBPP_PLUS_VERSION = "v0.2.0"
DOCKER_IMAGE = "jev-mbpp-evaluator:0.3.1"
DEFAULT_ROOT = Path("evals/local/mbpp_plus")
AGENTS = ("jev", "opencode", "antigravity")


def read_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open("r", encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{number}: invalid JSON: {exc}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{number}: row must be an object")
            rows.append(value)
    return rows


def write_jsonl(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def require_evalplus() -> None:
    try:
        installed = importlib.metadata.version("evalplus")
    except importlib.metadata.PackageNotFoundError as exc:
        raise RuntimeError("Install the pinned package from evals/benchmarks/mbpp_plus/requirements.txt") from exc
    if installed != EVALPLUS_VERSION:
        raise RuntimeError(f"Expected evalplus=={EVALPLUS_VERSION}, found {installed}")


def load_dataset():
    require_evalplus()
    from evalplus.data import get_mbpp_plus, get_mbpp_plus_hash
    from evalplus.data.mbpp import _ready_mbpp_plus_path
    from evalplus.data.utils import stream_jsonl
    tasks = get_mbpp_plus(version="default")
    raw_path = _ready_mbpp_plus_path(version="default")
    raw_tasks = {row["task_id"]: row for row in stream_jsonl(raw_path)}
    return tasks, raw_tasks, get_mbpp_plus_hash(version="default")


def select_ids(tasks: dict, explicit: list[str] | None, limit: int) -> list[str]:
    selected = explicit if explicit else sorted(tasks, key=lambda value: int(value.split("/")[-1]))[:limit]
    unknown = sorted(set(selected) - set(tasks))
    if unknown:
        raise ValueError(f"Unknown MBPP+ task IDs: {', '.join(unknown)}")
    if not selected or len(selected) != len(set(selected)):
        raise ValueError("Task IDs must be non-empty and unique")
    if len(selected) > 12:
        raise ValueError("The pilot adapter is capped at 12 tasks")
    return selected


def prepare(args) -> int:
    tasks, raw_tasks, dataset_hash = load_dataset()
    explicit = [x.strip() for x in args.task_ids.split(",") if x.strip()] if args.task_ids else None
    selected = select_ids(tasks, explicit, args.limit)
    root = Path(args.output_dir).resolve()
    prompts_path = root / "agent-visible" / "tasks.jsonl"
    private_path = root / "evaluator-private" / "mbpp-plus-subset.jsonl"
    manifest_path = root / "task-manifest.json"

    existing_workspaces = [str(root / "workspaces" / agent) for agent in AGENTS if (root / "workspaces" / agent).exists()]
    if existing_workspaces:
        raise ValueError(f"Agent workspaces already exist; remove them explicitly before preparing again: {', '.join(existing_workspaces)}")

    prompt_rows, private_rows = [], []
    for task_id in selected:
        task = tasks[task_id]
        prompt_rows.append({"task_id": task_id, "entry_point": task["entry_point"], "prompt": task["prompt"]})
        # Preserve the raw serialized dataset record. EvalPlus deserializes
        # base_input/plus_input when it loads MBPP_OVERRIDE_PATH; writing the
        # already-deserialized task here would convert tuple/set values twice.
        private_rows.append(raw_tasks[task_id])

    write_jsonl(prompts_path, prompt_rows)
    write_jsonl(private_path, private_rows)

    # Give each agent an independent workspace containing only public prompts.
    # Refuse to overwrite existing work so a repeated prepare cannot erase edits.
    for agent in AGENTS:
        agent_root = root / "workspaces" / agent
        for task_id in selected:
            task = tasks[task_id]
            task_dir = agent_root / task_id.replace("/", "_")
            task_dir.mkdir(parents=True, exist_ok=True)
            task_doc = (
                f"# MBPP+ task {task_id}\n\n"
                f"**Entry point:** `{task['entry_point']}`\n\n"
                "## Official task prompt\n\n"
                f"{task['prompt']}\n\n"
                "## Instructions\n\n"
                "Implement the requested Python function in `solution.py`. "
                "Return a complete implementation with the required entry point. "
                "Do not look for or attempt to infer hidden tests or reference solutions. "
                "Do not access files outside this task workspace.\n"
            )
            (task_dir / "TASK.md").write_text(task_doc, encoding="utf-8")
            (task_dir / "solution.py").write_text(
                f"# Implement the {task['entry_point']} function described in TASK.md.\n",
                encoding="utf-8",
            )
    manifest = {
        "schema_version": "1.0",
        "benchmark": "mbpp_plus",
        "evalplus_version": EVALPLUS_VERSION,
        "mbpp_plus_dataset_version": MBPP_PLUS_VERSION,
        "source_dataset_hash_md5": dataset_hash,
        "selected_task_ids": selected,
        "task_count": len(selected),
        "agent_visible_prompts": str(prompts_path),
        "evaluator_private_dataset": str(private_path),
        "evaluator_private_dataset_sha256": hashlib.sha256(private_path.read_bytes()).hexdigest(),
        "selection": "explicit task IDs" if explicit else f"first {len(selected)} task IDs in numeric order",
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "prepared", "task_ids": selected, "manifest": str(manifest_path),
                      "agent_visible_prompts": str(prompts_path), "evaluator_private_dataset": str(private_path)}, indent=2))
    return 0


def collect(args) -> int:
    root = Path(args.local_root).resolve()
    manifest_path = Path(args.manifest).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if args.agent not in AGENTS:
        raise ValueError(f"Unknown agent: {args.agent}")
    rows = []
    for task_id in manifest["selected_task_ids"]:
        solution_path = root / "workspaces" / args.agent / task_id.replace("/", "_") / "solution.py"
        if not solution_path.is_file():
            raise ValueError(f"Missing solution file for {args.agent} {task_id}: {solution_path}")
        rows.append({"task_id": task_id, "solution": solution_path.read_text(encoding="utf-8")})
    validate_samples(manifest["selected_task_ids"], rows)
    output_path = Path(args.output).resolve()
    if output_path.exists():
        raise ValueError(f"Samples file already exists; choose a new --output path: {output_path}")
    write_jsonl(output_path, rows)
    print(json.dumps({"status": "collected", "agent": args.agent, "task_count": len(rows),
                      "task_ids": manifest["selected_task_ids"], "samples": str(output_path)}, indent=2))
    return 0


def validate_samples(task_ids: list[str], sample_rows: list[dict]) -> None:
    seen = []
    for number, row in enumerate(sample_rows, 1):
        if not isinstance(row.get("task_id"), str):
            raise ValueError(f"Sample row {number}: task_id must be a string")
        if not isinstance(row.get("solution"), str) or not row["solution"].strip():
            raise ValueError(f"Sample row {number}: solution must be a non-empty string")
        seen.append(row["task_id"])
    if len(seen) != len(set(seen)):
        raise ValueError("Samples contain duplicate task IDs")
    if set(seen) != set(task_ids):
        raise ValueError(f"Sample/task mismatch; missing={sorted(set(task_ids)-set(seen))}, extra={sorted(set(seen)-set(task_ids))}")


def build_docker_command(
    input_dir: Path,
    output_dir: Path,
    output_name: str,
    parallel: int,
) -> list[str]:
    input_mount = (
        f"type=bind,source={input_dir.resolve().as_posix()},"
        "target=/input,readonly"
    )
    output_mount = (
        f"type=bind,source={output_dir.resolve().as_posix()},"
        "target=/output"
    )

    return [
        "docker", "run", "--rm",
        "--network", "none",
        "--read-only",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
        "--memory=1g",
        "--cpus=2",
        "--pids-limit=128",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--user=65534:65534",
        "--env", "HOME=/tmp",
        "--env", "PYTHONIOENCODING=utf-8",
        "--env", "PYTHONHASHSEED=0",
        "--env", "TOKENIZERS_PARALLELISM=false",
        "--env", "MBPP_OVERRIDE_PATH=/input/mbpp-plus-subset.jsonl",
        "--mount", input_mount,
        "--mount", output_mount,
        DOCKER_IMAGE,
        "python", "-m", "evalplus.evaluate",
        "--dataset", "mbpp",
        "--samples", f"/output/.{Path(output_name).stem}.samples.jsonl",
        "--parallel", str(parallel),
    ]


def evaluate(args) -> int:
    manifest_path = Path(args.manifest).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    if manifest.get("benchmark") != "mbpp_plus":
        raise ValueError("Not an MBPP+ manifest")

    if (
        manifest.get("evalplus_version") != EVALPLUS_VERSION
        or manifest.get("mbpp_plus_dataset_version") != MBPP_PLUS_VERSION
    ):
        raise ValueError("Manifest version does not match the pinned evaluator")

    private_path = Path(manifest["evaluator_private_dataset"]).resolve()
    expected = manifest["evaluator_private_dataset_sha256"]
    actual = hashlib.sha256(private_path.read_bytes()).hexdigest()

    if actual != expected:
        raise ValueError("Evaluator-private dataset hash mismatch")

    samples_path = Path(args.samples).resolve()
    validate_samples(
        manifest["selected_task_ids"],
        read_jsonl(samples_path),
    )

    output_path = Path(args.output).resolve()

    if output_path.exists():
        raise ValueError(
            f"Result file already exists; choose a new --output path: {output_path}"
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        image_check = subprocess.run(
            ["docker", "image", "inspect", DOCKER_IMAGE],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("Docker CLI was not found") from exc

    if image_check.returncode != 0:
        raise RuntimeError(
            f"Docker image {DOCKER_IMAGE} is unavailable. "
            "Build the pinned evaluator image before scoring."
        )

    staged_samples_path = (
        output_path.parent / f".{output_path.stem}.samples.jsonl"
    )
    evalplus_result_path = (
        output_path.parent
        / f".{output_path.stem}.samples_eval_results.json"
    )

    if staged_samples_path.exists() or evalplus_result_path.exists():
        raise ValueError(
            "Temporary EvalPlus files already exist; inspect them before retrying"
        )

    shutil.copyfile(samples_path, staged_samples_path)

    try:
        with tempfile.TemporaryDirectory(prefix="jev-mbpp-eval-") as temporary:
            input_dir = Path(temporary)

            shutil.copyfile(
                private_path,
                input_dir / "mbpp-plus-subset.jsonl",
            )

            command = build_docker_command(
                input_dir,
                output_path.parent,
                output_path.name,
                args.parallel,
            )

            completed = subprocess.run(command, check=False)
    finally:
        staged_samples_path.unlink(missing_ok=True)

    if completed.returncode:
        print(
            f"Docker EvalPlus exited with status {completed.returncode}",
            file=sys.stderr,
        )
        return completed.returncode

    if not evalplus_result_path.is_file():
        raise RuntimeError(
            "EvalPlus returned success without creating its result file"
        )

    result = json.loads(evalplus_result_path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise RuntimeError("EvalPlus result must be a JSON object")

    eval_results = result.get("eval")
    if not isinstance(eval_results, dict):
        raise RuntimeError("EvalPlus result is missing its task-level eval data")

    expected_task_ids = set(manifest["selected_task_ids"])
    if set(eval_results) != expected_task_ids:
        raise RuntimeError("EvalPlus result task IDs do not match the manifest")

    task_results = []
    for task_id in manifest["selected_task_ids"]:
        candidates = eval_results.get(task_id)
        if not isinstance(candidates, list) or len(candidates) != 1:
            raise RuntimeError(
                f"Expected exactly one candidate result for {task_id}"
            )

        candidate = candidates[0]
        if not isinstance(candidate, dict):
            raise RuntimeError(f"Invalid candidate result for {task_id}")
        if candidate.get("base_status") not in ("pass", "fail"):
            raise RuntimeError(f"Invalid base status for {task_id}")
        if candidate.get("plus_status") not in ("pass", "fail"):
            raise RuntimeError(f"Invalid Plus status for {task_id}")

        task_results.append(candidate)

    shutil.move(str(evalplus_result_path), str(output_path))

    total = len(task_results)
    base_passed = sum(
        item["base_status"] == "pass" for item in task_results
    )
    plus_passed = sum(
        item["plus_status"] == "pass" for item in task_results
    )

    print(json.dumps({
        "status": "scored",
        "benchmark": "mbpp_plus",
        "task_ids": manifest["selected_task_ids"],
        "evalplus_version": EVALPLUS_VERSION,
        "dataset_version": MBPP_PLUS_VERSION,
        "result_file": str(output_path),
        "pass_at_1": {
            "base": base_passed / total,
            "plus": plus_passed / total,
            "base_passed": base_passed,
            "plus_passed": plus_passed,
            "total": total,
        },
    }, indent=2))

    return 0


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("prepare")
    p.add_argument("--limit", type=int, default=3)
    p.add_argument("--task-ids")
    p.add_argument("--output-dir", default=str(DEFAULT_ROOT))
    p.set_defaults(func=prepare)
    collect_parser = commands.add_parser("collect")
    collect_parser.add_argument("--agent", choices=AGENTS, required=True)
    collect_parser.add_argument("--manifest", default=str(DEFAULT_ROOT / "task-manifest.json"))
    collect_parser.add_argument("--local-root", default=str(DEFAULT_ROOT))
    collect_parser.add_argument("--output", required=True)
    collect_parser.set_defaults(func=collect)
    e = commands.add_parser("evaluate")
    e.add_argument("--manifest", default=str(DEFAULT_ROOT / "task-manifest.json"))
    e.add_argument("--samples", required=True)
    e.add_argument("--output", default=str(DEFAULT_ROOT / "results" / "evalplus-results.json"))
    e.add_argument("--parallel", type=int, default=1)
    e.set_defaults(func=evaluate)
    return parser


def main():
    args = build_parser().parse_args()
    if getattr(args, "limit", 1) < 1 or getattr(args, "parallel", 1) < 1:
        raise SystemExit("limit and parallel must be positive integers")
    try:
        return args.func(args)
    except (ValueError, RuntimeError, OSError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
