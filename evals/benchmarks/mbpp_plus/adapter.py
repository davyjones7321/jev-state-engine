#!/usr/bin/env python3
"""Prepare and score a small MBPP+ subset with the official EvalPlus evaluator."""
from __future__ import annotations
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys

EVALPLUS_VERSION = "0.3.1"
MBPP_PLUS_VERSION = "v0.2.0"
DEFAULT_ROOT = Path("evals/local/mbpp_plus")


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


def clean_evaluator_environment(dataset_path: Path) -> dict[str, str]:
    allowed = ("PATH", "HOME", "USERPROFILE", "TEMP", "TMP", "TMPDIR", "SYSTEMROOT", "WINDIR")
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env.update({"MBPP_OVERRIDE_PATH": str(dataset_path), "PYTHONIOENCODING": "utf-8",
                "PYTHONHASHSEED": "0", "TOKENIZERS_PARALLELISM": "false"})
    return env


def evaluate(args) -> int:
    require_evalplus()
    manifest_path = Path(args.manifest).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("benchmark") != "mbpp_plus":
        raise ValueError("Not an MBPP+ manifest")
    if manifest.get("evalplus_version") != EVALPLUS_VERSION or manifest.get("mbpp_plus_dataset_version") != MBPP_PLUS_VERSION:
        raise ValueError("Manifest version does not match the pinned evaluator")
    private_path = Path(manifest["evaluator_private_dataset"]).resolve()
    expected = manifest["evaluator_private_dataset_sha256"]
    actual = hashlib.sha256(private_path.read_bytes()).hexdigest()
    if actual != expected:
        raise ValueError("Evaluator-private dataset hash mismatch")
    samples_path = Path(args.samples).resolve()
    validate_samples(manifest["selected_task_ids"], read_jsonl(samples_path))

    output_path = Path(args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "-m", "evalplus.evaluate", "--dataset", "mbpp",
               "--samples", str(samples_path), "--parallel", str(args.parallel),
               "--output_file", str(output_path)]
    completed = subprocess.run(command, env=clean_evaluator_environment(private_path), check=False)
    if completed.returncode:
        print(f"EvalPlus exited with status {completed.returncode}", file=sys.stderr)
        return completed.returncode
    if not output_path.is_file():
        raise RuntimeError("EvalPlus returned success without creating its result file")
    result = json.loads(output_path.read_text(encoding="utf-8"))
    print(json.dumps({"status": "scored", "benchmark": "mbpp_plus", "task_ids": manifest["selected_task_ids"],
                      "evalplus_version": EVALPLUS_VERSION, "dataset_version": MBPP_PLUS_VERSION,
                      "result_file": str(output_path), "pass_at_k": result.get("pass_at_k", {})}, indent=2))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("prepare")
    p.add_argument("--limit", type=int, default=3)
    p.add_argument("--task-ids")
    p.add_argument("--output-dir", default=str(DEFAULT_ROOT))
    p.set_defaults(func=prepare)
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
