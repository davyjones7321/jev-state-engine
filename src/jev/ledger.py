from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Tuple, Union

from jev.evidence import weakest
from jev.mech_evidence import (
    BaseStatus,
    Comparison,
    EvidenceLevel,
    MechanicalEvidence,
    MechReason,
    MechStatus,
    TargetConfidence,
)
from jev.models import Subgoal
from jev.process_runner import get_scrubbed_env

MAX_EVIDENCE_CHARS = 64_000


def _compute_rates(measured: int, eligible: int, total: int) -> Tuple[Optional[float], Optional[float]]:
    rate_all = (measured / total) if total > 0 else None
    rate_eligible = (measured / eligible) if eligible > 0 else None
    return rate_all, rate_eligible


def phase11_metrics_report(
    conn: sqlite3.Connection, *, since: Optional[str] = None, until: Optional[str] = None,
) -> Dict[str, Any]:
    """Read attempt metrics, including old schemas, without modifying the database.

    The time window is UTC [since, until); omitted boundaries are unbounded.
    Correctness and safety observations must be independently supplied, not
    inferred from Jev agreement or a passing command. Missing observations are unknown.
    """
    def timestamp(value: str) -> datetime:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)

    start = timestamp(since) if since else None
    end = timestamp(until) if until else None
    if start and end and start >= end:
        raise ValueError("since must be earlier than until")

    def dictionaries(query: str) -> List[Dict[str, Any]]:
        cursor = conn.execute(query)
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]

    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    observations = {}
    if "attempt_assessments" in tables:
        observations = {
            (row["run_id"], row["subgoal_id"], row["attempt_no"]): row
            for row in dictionaries("SELECT * FROM attempt_assessments")
        }

    def accumulator() -> Dict[str, int]:
        return dict.fromkeys((
            "attempts_all", "measured", "attempts_eligible", "unmeasured", "exit_only_success_count",
            "tooling_uncertainty", "verifier_modified", "worker_rejections_total",
            "worker_rejections_assessed", "false_worker_rejections", "mechanical_acceptances_total",
            "mechanical_acceptances_assessed", "false_mechanical_acceptances",
            "unsafe_commit_count", "process_leak_count", "unsafe_commit_assessed_attempts",
            "process_leak_assessed_attempts", "safety_assessed_attempts", "eligibility_unknown_count",
        ), 0)

    totals = accumulator()
    groups: Dict[Tuple[Any, ...], Dict[str, int]] = {}
    invalid_timestamps = 0
    for row in dictionaries("SELECT * FROM attempts"):
        if start or end:
            try:
                created = timestamp(row.get("created_at") or "")
            except (ValueError, TypeError):
                invalid_timestamps += 1
                continue
            if (start and created < start) or (end and created >= end):
                continue
        observation = observations.get((row["run_id"], row["subgoal_id"], row["attempt_no"]), {})
        try:
            identity = json.loads(row.get("mech_command_identity_json") or "{}")
            if not isinstance(identity, dict):
                identity = {}
        except (TypeError, ValueError):
            identity = {}
        status = row.get("mech_status")
        reason = row.get("mech_reason")
        level = row.get("mech_evidence_level")
        profile_id = row.get("mech_runner_profile_id") or identity.get("profile_id")
        outside = (
            identity.get("preflight_status") in ("OUTSIDE_PROFILE", "PROFILE_UNKNOWN")
            or not profile_id
        )
        eligible = status in ("PASS", "FAIL", "UNMEASURED") and not outside and reason not in {
            "MISSING_TOOL", "NO_COMMAND", "ENVIRONMENT_UNAVAILABLE",
        }
        strong = (
            eligible and status in ("PASS", "FAIL") and level in ("counted", "named")
            and not row.get("mech_mechanically_unverified")
            and not row.get("mech_verifier_modified")
            and row.get("mech_approval_valid") != 0
            and row.get("mech_comparison_valid") != 0
        )
        key = (
            row.get("mech_language"),
            profile_id,
            identity.get("platform") or observation.get("platform"),
            row.get("mech_registry_version") or identity.get("registry_version"),
        )
        group = groups.setdefault(key, accumulator())
        rejected = row.get("outcome") in ("mechanical_failure", "semantic_failure", "failed", "rejected")
        for counts in (totals, group):
            counts["attempts_all"] += 1
            counts["measured"] += int(strong)
            counts["attempts_eligible"] += int(eligible)
            counts["eligibility_unknown_count"] += int(status is None)
            counts["unmeasured"] += int(status == "UNMEASURED")
            counts["exit_only_success_count"] += int(status == "PASS" and level == "exit_only")
            counts["tooling_uncertainty"] += int(status == "UNMEASURED" or status is None or outside)
            counts["verifier_modified"] += int(bool(row.get("mech_verifier_modified")))
            counts["worker_rejections_total"] += int(rejected)
            counts["mechanical_acceptances_total"] += int(status == "PASS")
            if rejected and observation.get("worker_should_accept") is not None:
                counts["worker_rejections_assessed"] += 1
                counts["false_worker_rejections"] += int(observation["worker_should_accept"] == 1)
            if status == "PASS" and observation.get("mechanical_should_accept") is not None:
                counts["mechanical_acceptances_assessed"] += 1
                counts["false_mechanical_acceptances"] += int(observation["mechanical_should_accept"] == 0)
            for metric in ("unsafe_commit", "process_leak"):
                count = observation.get(metric + "_count")
                if count is not None:
                    counts[metric + "_count"] += count
                    counts[metric + "_assessed_attempts"] += 1
            counts["safety_assessed_attempts"] += int(
                observation.get("unsafe_commit_count") is not None and observation.get("process_leak_count") is not None
            )

    def finish(counts: Dict[str, int]) -> Dict[str, Any]:
        result: Dict[str, Any] = dict(counts)
        for name, numerator, denominator in (
            ("measured_rate_all", "measured", "attempts_all"),
            ("measured_rate_eligible", "measured", "attempts_eligible"),
            ("false_worker_rejection_rate", "false_worker_rejections", "worker_rejections_assessed"),
            ("false_mechanical_acceptance_rate", "false_mechanical_acceptances", "mechanical_acceptances_assessed"),
            ("tooling_uncertainty_rate", "tooling_uncertainty", "attempts_all"),
            ("verifier_modified_rate", "verifier_modified", "attempts_all"),
        ):
            n, d = counts[numerator], counts[denominator]
            result[name] = {"numerator": n, "denominator": d, "value": n / d if d else None}
        for metric in ("unsafe_commit", "process_leak"):
            if not counts[metric + "_assessed_attempts"]:
                result[metric + "_count"] = None
        return result

    window = {"since": start.isoformat() if start else None, "until": end.isoformat() if end else None}
    return {
        "schema_version": 1,
        "time_window": window,
        "excluded_invalid_timestamp_count": invalid_timestamps,
        "totals": finish(totals),
        "groups": [
            dict(zip(("language", "runner_profile", "platform", "registry_version"), key),
                 time_window=window, **finish(counts))
            for key, counts in sorted(groups.items(), key=lambda item: tuple(str(v or "") for v in item[0]))
        ],
    }


REASONING_KEYS = {"cot", "chain_of_thought", "reasoning_trace", "hidden_reasoning"}
THINK_TAG_PATTERN = re.compile(r"(?s)<think>.*?(?:</think>|\Z)", re.IGNORECASE)


def strip_raw_reasoning(text: str) -> str:
    """Strip <think> blocks and reasoning trace markers from text."""
    if not text:
        return text
    return THINK_TAG_PATTERN.sub("", text).strip()


def exclude_raw_reasoning_data(data: Any) -> Any:
    """Recursively exclude raw reasoning keys and strip <think> content from data structures."""
    if isinstance(data, dict):
        return {k: exclude_raw_reasoning_data(v) for k, v in data.items() if str(k).lower() not in REASONING_KEYS}
    elif isinstance(data, (list, tuple)):
        return [exclude_raw_reasoning_data(item) for item in data]
    elif isinstance(data, str):
        return strip_raw_reasoning(data)
    return data


def _bounded_text(value: Any, limit: int = MAX_EVIDENCE_CHARS) -> Optional[str]:
    """Serialize evidence without allowing one diagnostic to consume the ledger, redacting secrets and excluding raw reasoning."""
    if value is None:
        return None
    from jev.proposals import redact_secrets_in_text
    clean_val = exclude_raw_reasoning_data(value)
    text = clean_val if isinstance(clean_val, str) else json.dumps(clean_val, ensure_ascii=False, default=str)
    text = redact_secrets_in_text(strip_raw_reasoning(text))
    return text if len(text) <= limit else text[:limit] + "\n[truncated]"


def _bounded_json(value: Any) -> Optional[str]:
    return _bounded_text(value)


def get_jev_home() -> Path:
    """Returns the JEV_HOME directory (defaults to ~/.jev)."""
    return Path(os.environ.get("JEV_HOME", Path.home() / ".jev")).expanduser().resolve()


def compute_repo_key(repo_dir: Union[str, Path]) -> str:
    """Computes a stable repository key: first 16 hex chars of sha256 over sorted root commit hashes.

    Runs git rev-list --max-parents=0 HEAD. If it cannot be computed, returns 'unknown'.
    """
    try:
        res = subprocess.run(
            ["git", "rev-list", "--max-parents=0", "HEAD"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            check=True,
            env=get_scrubbed_env(),
        )
        lines = [line.strip() for line in res.stdout.strip().splitlines() if line.strip()]
        if not lines:
            return "unknown"
        lines.sort()
        combined = "\n".join(lines).encode("utf-8")
        return hashlib.sha256(combined).hexdigest()[:16]
    except Exception:
        return "unknown"


class Recorder(Protocol):
    """Protocol defining the recording interface for the run ledger."""

    def record_run_start(
        self,
        run_id: str,
        thread_id: str,
        ticket: str,
        base_commit: Optional[str] = None,
        engine_commit: Optional[str] = None,
        worker_model: Optional[str] = None,
        test_policy: Optional[str] = None,
        started_at: Optional[str] = None,
        retry_intel: Optional[str] = None,
        repo_profile: Optional[str] = None,
        profile_chars: Optional[int] = None,
        dep_delta_jev: Optional[str] = None,
        provenance: Optional[str] = None,
        model_version: Optional[str] = None,
        failure_evidence: Optional[Any] = None,
        retry_metadata: Optional[Any] = None,
        tool_evidence: Optional[Any] = None,
        read_evidence: Optional[Any] = None,
        self_check_evidence: Optional[Any] = None,
        hypothesis_change: Optional[Any] = None,
    ) -> None: ...


    def record_run_end(
        self,
        run_id: str,
        final_status: str,
        final_commit: Optional[str] = None,
        ended_at: Optional[str] = None,
        notes_chars: Optional[int] = None,
        evidence_level: Optional[str] = None,
        dependency_delta: Optional[str] = None,
    ) -> None: ...

    def record_subgoals(
        self,
        run_id: str,
        subgoals: List[Subgoal],
    ) -> None: ...

    def record_subgoal_committed(
        self,
        run_id: str,
        subgoal_id: str,
        parent_sha: Optional[str] = None,
        commit_sha: Optional[str] = None,
    ) -> None: ...

    def record_subgoal_status(
        self,
        run_id: str,
        subgoal_id: str,
        status: str,
    ) -> None: ...

    def record_attempt(
        self,
        run_id: str,
        subgoal_id: str,
        tier: str,
        outcome: str,
        failed_check: Optional[str] = None,
        diff: str = "",
        diff_sha: str = "",
        files: Optional[List[str]] = None,
        compile_errors: Optional[Any] = None,
        tests_ran: bool = False,
        test_outcome: Optional[str] = None,
        jev_valid: Optional[bool] = None,
        jev_probability: Optional[float] = None,
        jev_model: Optional[str] = None,
        jev_raw: Optional[str] = None,
        jev_tokens_in: Optional[int] = None,
        jev_tokens_out: Optional[int] = None,
        feedback_sent: Optional[str] = None,
        tokens_in: Optional[int] = None,
        tokens_out: Optional[int] = None,
        created_at: Optional[str] = None,
        evidence_level: Optional[str] = None,
        dependency_delta: Optional[str] = None,
        jev_state_keys: Optional[str] = None,
        provenance: Optional[str] = None,
        model_version: Optional[str] = None,
        retry_number: Optional[int] = None,
        evidence_added: Optional[bool] = None,
        hypothesis_changed: Optional[bool] = None,
        hypothesis_change: Optional[Any] = None,
        failure_evidence: Optional[Any] = None,
        tool_evidence: Optional[Any] = None,
        read_evidence: Optional[Any] = None,
        self_check_evidence: Optional[Any] = None,
        retry_metadata: Optional[Any] = None,
        baseline_failures: Optional[int] = None,
        new_failures: Optional[int] = None,
        full_suite_clean: Optional[bool] = None,
        mech_evidence: Optional[MechanicalEvidence] = None,
        unverified_kind: Optional[str] = None,
    ) -> None: ...

    def get_attempt_mech_evidence(
        self,
        run_id: str,
        subgoal_id: Optional[str] = None,
        attempt_no: Optional[int] = None,
    ) -> Optional[MechanicalEvidence]: ...

    def measured_rate_report(
        self,
        since_days: int = 90,
    ) -> Dict[Tuple[Optional[str], Optional[str]], Dict[str, Any]]: ...

    def has_run(self, run_id: str) -> bool: ...

    def get_run_evidence_level(self, run_id: str) -> Optional[str]: ...

    def get_run_dependency_delta(self, run_id: str) -> Optional[str]: ...

    def get_run_provenance(self, run_id: str) -> Optional[str]: ...



class NullRecorder:
    """Default no-op recorder for JevEngine."""

    def record_run_start(
        self,
        run_id: str,
        thread_id: str,
        ticket: str,
        base_commit: Optional[str] = None,
        engine_commit: Optional[str] = None,
        worker_model: Optional[str] = None,
        test_policy: Optional[str] = None,
        started_at: Optional[str] = None,
        retry_intel: Optional[str] = None,
        repo_profile: Optional[str] = None,
        profile_chars: Optional[int] = None,
        dep_delta_jev: Optional[str] = None,
        provenance: Optional[str] = None,
        model_version: Optional[str] = None,
        failure_evidence: Optional[Any] = None,
        retry_metadata: Optional[Any] = None,
        tool_evidence: Optional[Any] = None,
        read_evidence: Optional[Any] = None,
        self_check_evidence: Optional[Any] = None,
        hypothesis_change: Optional[Any] = None,
    ) -> None:
        pass


    def record_run_end(
        self,
        run_id: str,
        final_status: str,
        final_commit: Optional[str] = None,
        ended_at: Optional[str] = None,
        notes_chars: Optional[int] = None,
        evidence_level: Optional[str] = None,
        dependency_delta: Optional[str] = None,
    ) -> None:
        pass

    def record_subgoals(
        self,
        run_id: str,
        subgoals: List[Subgoal],
    ) -> None:
        pass

    def record_subgoal_committed(
        self,
        run_id: str,
        subgoal_id: str,
        parent_sha: Optional[str] = None,
        commit_sha: Optional[str] = None,
    ) -> None:
        pass

    def record_subgoal_status(
        self,
        run_id: str,
        subgoal_id: str,
        status: str,
    ) -> None:
        pass

    def record_attempt(
        self,
        run_id: str,
        subgoal_id: str,
        tier: str,
        outcome: str,
        failed_check: Optional[str] = None,
        diff: str = "",
        diff_sha: str = "",
        files: Optional[List[str]] = None,
        compile_errors: Optional[Any] = None,
        tests_ran: bool = False,
        test_outcome: Optional[str] = None,
        jev_valid: Optional[bool] = None,
        jev_probability: Optional[float] = None,
        jev_model: Optional[str] = None,
        jev_raw: Optional[str] = None,
        jev_tokens_in: Optional[int] = None,
        jev_tokens_out: Optional[int] = None,
        feedback_sent: Optional[str] = None,
        tokens_in: Optional[int] = None,
        tokens_out: Optional[int] = None,
        created_at: Optional[str] = None,
        evidence_level: Optional[str] = None,
        dependency_delta: Optional[str] = None,
        jev_state_keys: Optional[str] = None,
        provenance: Optional[str] = None,
        model_version: Optional[str] = None,
        retry_number: Optional[int] = None,
        evidence_added: Optional[bool] = None,
        hypothesis_changed: Optional[bool] = None,
        hypothesis_change: Optional[Any] = None,
        failure_evidence: Optional[Any] = None,
        tool_evidence: Optional[Any] = None,
        read_evidence: Optional[Any] = None,
        self_check_evidence: Optional[Any] = None,
        retry_metadata: Optional[Any] = None,
        baseline_failures: Optional[int] = None,
        new_failures: Optional[int] = None,
        full_suite_clean: Optional[bool] = None,
        mech_evidence: Optional[MechanicalEvidence] = None,
        unverified_kind: Optional[str] = None,
    ) -> None:
        pass

    def get_attempt_mech_evidence(
        self,
        run_id: str,
        subgoal_id: Optional[str] = None,
        attempt_no: Optional[int] = None,
    ) -> Optional[MechanicalEvidence]:
        return None

    def measured_rate_report(
        self,
        since_days: int = 90,
    ) -> Dict[Tuple[Optional[str], Optional[str]], Dict[str, Any]]:
        return {}

    def has_run(self, run_id: str) -> bool:
        return False

    def get_run_evidence_level(self, run_id: str) -> Optional[str]:
        return None

    def get_run_dependency_delta(self, run_id: str) -> Optional[str]:
        return None

    def get_run_provenance(self, run_id: str) -> Optional[str]:
        return None


class SqliteLedger:
    """SQLite implementation of the run and failure ledger."""

    def __init__(
        self,
        repo_dir: Optional[Union[str, Path]] = None,
        repo_key: Optional[str] = None,
        db_path: Optional[Union[str, Path]] = None,
    ):
        if db_path is not None:
            self.db_path = Path(db_path).resolve()
        else:
            if not repo_key:
                repo_key = compute_repo_key(repo_dir) if repo_dir else "unknown"
            self.db_path = get_jev_home() / "repos" / repo_key / "ledger.db"

        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                  run_id TEXT PRIMARY KEY,
                  thread_id TEXT,
                  ticket TEXT,
                  base_commit TEXT,
                  final_commit TEXT,
                  engine_commit TEXT,
                  worker_model TEXT,
                  test_policy TEXT,
                  retry_intel TEXT,
                  repo_profile TEXT,
                  profile_chars INTEGER,
                  evidence_level TEXT,
                  status TEXT,
                  started_at TEXT,
                  ended_at TEXT,
                   notes_chars INTEGER,
                   dependency_delta TEXT,
                   dep_delta_jev TEXT DEFAULT NULL,
                   provenance TEXT,
                   model_version TEXT,
                   failure_evidence TEXT,
                   retry_metadata TEXT,
                   tool_evidence TEXT,
                   read_evidence TEXT,
                   self_check_evidence TEXT,
                   hypothesis_change TEXT
                );
                """
            )
            # Additive migrations for existing databases
            cols = [r[1] for r in conn.execute("PRAGMA table_info(runs)").fetchall()]
            if "retry_intel" not in cols:
                conn.execute("ALTER TABLE runs ADD COLUMN retry_intel TEXT DEFAULT NULL")
            if "evidence_level" not in cols:
                conn.execute("ALTER TABLE runs ADD COLUMN evidence_level TEXT DEFAULT NULL")
            if "notes_chars" not in cols:
                conn.execute("ALTER TABLE runs ADD COLUMN notes_chars INTEGER")
            if "repo_profile" not in cols:
                conn.execute("ALTER TABLE runs ADD COLUMN repo_profile TEXT DEFAULT NULL")
            if "profile_chars" not in cols:
                conn.execute("ALTER TABLE runs ADD COLUMN profile_chars INTEGER DEFAULT NULL")
            if "dependency_delta" not in cols:
                conn.execute("ALTER TABLE runs ADD COLUMN dependency_delta TEXT DEFAULT NULL")
            if "dep_delta_jev" not in cols:
                conn.execute("ALTER TABLE runs ADD COLUMN dep_delta_jev TEXT DEFAULT NULL")
            for name in ("provenance", "model_version", "failure_evidence", "retry_metadata", "tool_evidence", "read_evidence", "self_check_evidence", "hypothesis_change"):
                if name not in cols:
                    conn.execute(f"ALTER TABLE runs ADD COLUMN {name} TEXT DEFAULT NULL")

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS subgoals (
                  run_id TEXT,
                  subgoal_id TEXT,
                  ordinal INTEGER,
                  description TEXT,
                  scope_json TEXT,
                  expects_tests INTEGER,
                  status TEXT,
                  parent_sha TEXT,
                  commit_sha TEXT,
                  PRIMARY KEY (run_id, subgoal_id)
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS attempts (
                  run_id TEXT,
                  subgoal_id TEXT,
                  attempt_no INTEGER,
                  tier TEXT,
                  outcome TEXT,
                  failed_check TEXT,
                  diff TEXT,
                  diff_sha TEXT,
                  files_json TEXT,
                  compile_errors_json TEXT,
                  tests_ran INTEGER,
                  test_outcome TEXT,
                  jev_valid INTEGER,
                  jev_probability REAL,
                  jev_model TEXT,
                  jev_raw TEXT,
                  jev_tokens_in INTEGER,
                  jev_tokens_out INTEGER,
                  feedback_sent TEXT,
                  tokens_in INTEGER,
                  tokens_out INTEGER,
                  created_at TEXT,
                   evidence_level TEXT,
                   dependency_delta TEXT,
                   jev_state_keys TEXT DEFAULT NULL,
                   provenance TEXT,
                   model_version TEXT,
                   retry_number INTEGER,
                   evidence_added INTEGER,
                   hypothesis_changed INTEGER,
                   hypothesis_change TEXT,
                   failure_evidence TEXT,
                   tool_evidence TEXT,
                   read_evidence TEXT,
                   self_check_evidence TEXT,
                   retry_metadata TEXT,
                   baseline_failures INTEGER,
                   new_failures INTEGER,
                   full_suite_clean INTEGER,
                   mech_schema_version INTEGER DEFAULT NULL,
                   mech_status TEXT DEFAULT NULL,
                   mech_reason TEXT DEFAULT NULL,
                   mech_evidence_level TEXT DEFAULT NULL,
                   mech_base_status TEXT DEFAULT NULL,
                   mech_comparison TEXT DEFAULT NULL,
                   mech_command_identity_json TEXT DEFAULT NULL,
                   mech_target_scope_json TEXT DEFAULT NULL,
                   mech_target_rule TEXT DEFAULT NULL,
                   mech_target_confidence REAL DEFAULT NULL,
                   mech_runner_profile_id TEXT DEFAULT NULL,
                   mech_registry_version TEXT DEFAULT NULL,
                   mech_language TEXT DEFAULT NULL,
                   mech_worker_staged_json TEXT DEFAULT NULL,
                   mech_engine_touched_json TEXT DEFAULT NULL,
                   mech_test_authorship TEXT DEFAULT NULL,
                   mech_test_surface_changed INTEGER DEFAULT NULL,
                   mech_verifier_modified INTEGER DEFAULT NULL,
                   mech_approval_valid INTEGER DEFAULT NULL,
                   mech_comparison_valid INTEGER DEFAULT NULL,
                   mech_mechanically_unverified INTEGER DEFAULT NULL,
                   mech_unverified_kind TEXT DEFAULT NULL,
                   mech_verification_inconclusive INTEGER DEFAULT NULL,
                   PRIMARY KEY (run_id, subgoal_id, attempt_no)
                );
                """
            )
            att_cols = [r[1] for r in conn.execute("PRAGMA table_info(attempts)").fetchall()]
            if "evidence_level" not in att_cols:
                conn.execute("ALTER TABLE attempts ADD COLUMN evidence_level TEXT DEFAULT NULL")
            if "dependency_delta" not in att_cols:
                conn.execute("ALTER TABLE attempts ADD COLUMN dependency_delta TEXT DEFAULT NULL")
            if "jev_state_keys" not in att_cols:
                conn.execute("ALTER TABLE attempts ADD COLUMN jev_state_keys TEXT DEFAULT NULL")
            for name, definition in (("provenance", "TEXT"), ("model_version", "TEXT"), ("retry_number", "INTEGER"), ("evidence_added", "INTEGER"), ("hypothesis_changed", "INTEGER"), ("hypothesis_change", "TEXT"), ("failure_evidence", "TEXT"), ("tool_evidence", "TEXT"), ("read_evidence", "TEXT"), ("self_check_evidence", "TEXT"), ("retry_metadata", "TEXT"), ("baseline_failures", "INTEGER"), ("new_failures", "INTEGER"), ("full_suite_clean", "INTEGER")):
                if name not in att_cols:
                    conn.execute(f"ALTER TABLE attempts ADD COLUMN {name} {definition} DEFAULT NULL")
            mech_cols = (
                ("mech_schema_version", "INTEGER"),
                ("mech_status", "TEXT"),
                ("mech_reason", "TEXT"),
                ("mech_evidence_level", "TEXT"),
                ("mech_base_status", "TEXT"),
                ("mech_comparison", "TEXT"),
                ("mech_command_identity_json", "TEXT"),
                ("mech_target_scope_json", "TEXT"),
                ("mech_target_rule", "TEXT"),
                ("mech_target_confidence", "REAL"),
                ("mech_runner_profile_id", "TEXT"),
                ("mech_registry_version", "TEXT"),
                ("mech_language", "TEXT"),
                ("mech_worker_staged_json", "TEXT"),
                ("mech_engine_touched_json", "TEXT"),
                ("mech_test_authorship", "TEXT"),
                ("mech_test_surface_changed", "INTEGER"),
                ("mech_verifier_modified", "INTEGER"),
                ("mech_approval_valid", "INTEGER"),
                ("mech_comparison_valid", "INTEGER"),
                ("mech_mechanically_unverified", "INTEGER"),
                ("mech_unverified_kind", "TEXT"),
                ("mech_verification_inconclusive", "INTEGER"),
            )
            for name, col_type in mech_cols:
                if name not in att_cols:
                    conn.execute(f"ALTER TABLE attempts ADD COLUMN {name} {col_type} DEFAULT NULL")

            conn.execute(
                """CREATE TABLE IF NOT EXISTS attempt_assessments (
                    run_id TEXT, subgoal_id TEXT, attempt_no INTEGER,
                    platform TEXT, worker_should_accept INTEGER,
                    mechanical_should_accept INTEGER, unsafe_commit_count INTEGER,
                    process_leak_count INTEGER,
                    PRIMARY KEY (run_id, subgoal_id, attempt_no)
                )"""
            )

    def record_attempt_assessment(
        self, run_id: str, subgoal_id: str, attempt_no: int, *,
        platform: Optional[str] = None,
        worker_should_accept: Optional[bool] = None,
        mechanical_should_accept: Optional[bool] = None,
        unsafe_commit_count: Optional[int] = None,
        process_leak_count: Optional[int] = None,
    ) -> None:
        """Attach independent evaluation labels; omitted fields preserve prior values.

        A caller must supply correctness/safety ground truth from a reviewed run
        or replay. Neither a Jev verdict nor command success is ground truth.
        """
        values = {
            "platform": platform, "worker_should_accept": worker_should_accept,
            "mechanical_should_accept": mechanical_should_accept,
            "unsafe_commit_count": unsafe_commit_count, "process_leak_count": process_leak_count,
        }
        for value in (worker_should_accept, mechanical_should_accept):
            if value is not None and type(value) is not bool:
                raise ValueError("correctness assessments must be booleans")
        for value in (unsafe_commit_count, process_leak_count):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("safety counts must be nonnegative integers")
        if platform is not None and (not isinstance(platform, str) or not platform.strip()):
            raise ValueError("platform must be a nonempty string")
        with self._get_connection() as conn:
            if not conn.execute(
                "SELECT 1 FROM attempts WHERE run_id=? AND subgoal_id=? AND attempt_no=?",
                (run_id, subgoal_id, attempt_no),
            ).fetchone():
                raise ValueError("assessment must reference an existing attempt")
            conn.execute(
                "INSERT OR IGNORE INTO attempt_assessments (run_id, subgoal_id, attempt_no) VALUES (?, ?, ?)",
                (run_id, subgoal_id, attempt_no),
            )
            for column, value in values.items():
                if value is not None:
                    conn.execute(
                        f"UPDATE attempt_assessments SET {column}=? WHERE run_id=? AND subgoal_id=? AND attempt_no=?",
                        (value, run_id, subgoal_id, attempt_no),
                    )

    def phase11_metrics_report(
        self, *, since: Optional[str] = None, until: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Return strong-evidence Phase 11 metrics; the legacy report is unchanged."""
        with self._get_connection() as conn:
            return phase11_metrics_report(conn, since=since, until=until)

    def has_run(self, run_id: str) -> bool:
        with self._get_connection() as conn:
            row = conn.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            return row is not None

    def record_run_start(
        self,
        run_id: str,
        thread_id: str,
        ticket: str,
        base_commit: Optional[str] = None,
        engine_commit: Optional[str] = None,
        worker_model: Optional[str] = None,
        test_policy: Optional[str] = None,
        started_at: Optional[str] = None,
        retry_intel: Optional[str] = None,
        repo_profile: Optional[str] = None,
        profile_chars: Optional[int] = None,
        dep_delta_jev: Optional[str] = None,
        provenance: Optional[str] = None,
        model_version: Optional[str] = None,
        failure_evidence: Optional[Any] = None,
        retry_metadata: Optional[Any] = None,
        tool_evidence: Optional[Any] = None,
        read_evidence: Optional[Any] = None,
        self_check_evidence: Optional[Any] = None,
        hypothesis_change: Optional[Any] = None,
    ) -> None:
        from jev.proposals import redact_secrets_in_text
        ticket = redact_secrets_in_text(strip_raw_reasoning(ticket))
        start_time = started_at or datetime.now(timezone.utc).isoformat()
        if not isinstance(dep_delta_jev, str):
            dep_delta_jev = "off"
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO runs (
                  run_id, thread_id, ticket, base_commit, engine_commit,
                   worker_model, test_policy, retry_intel, repo_profile, profile_chars, status, started_at, dep_delta_jev,
                   provenance, model_version, failure_evidence, retry_metadata, tool_evidence, read_evidence, self_check_evidence, hypothesis_change
                 ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'in_progress', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                  thread_id=excluded.thread_id,
                  ticket=excluded.ticket,
                  base_commit=excluded.base_commit,
                  engine_commit=excluded.engine_commit,
                  worker_model=excluded.worker_model,
                  test_policy=excluded.test_policy,
                  retry_intel=excluded.retry_intel,
                  repo_profile=excluded.repo_profile,
                  profile_chars=excluded.profile_chars,
                  status='in_progress',
                   started_at=excluded.started_at,
                   dep_delta_jev=excluded.dep_delta_jev,
                   provenance=excluded.provenance,
                   model_version=excluded.model_version,
                   failure_evidence=excluded.failure_evidence,
                   retry_metadata=excluded.retry_metadata,
                   tool_evidence=excluded.tool_evidence,
                   read_evidence=excluded.read_evidence,
                   self_check_evidence=excluded.self_check_evidence,
                   hypothesis_change=excluded.hypothesis_change
                """,
                (
                    run_id,
                    thread_id,
                    ticket,
                    base_commit,
                    engine_commit,
                    worker_model,
                    test_policy,
                    retry_intel or "on",
                    repo_profile or "off",
                    profile_chars,
                    start_time,
                    dep_delta_jev or "off",
                    provenance,
                    model_version or worker_model,
                    _bounded_text(failure_evidence),
                    _bounded_json(retry_metadata),
                    _bounded_json(tool_evidence),
                    _bounded_json(read_evidence),
                    _bounded_json(self_check_evidence),
                    _bounded_json(hypothesis_change),
                ),
            )


    def record_run_end(
        self,
        run_id: str,
        final_status: str,
        final_commit: Optional[str] = None,
        ended_at: Optional[str] = None,
        notes_chars: Optional[int] = None,
        evidence_level: Optional[str] = None,
        dependency_delta: Optional[str] = None,
    ) -> None:
        end_time = ended_at or datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            if evidence_level is None:
                rows = conn.execute(
                    """
                    SELECT a.evidence_level
                    FROM attempts a
                    JOIN subgoals s ON a.run_id = s.run_id AND a.subgoal_id = s.subgoal_id
                    WHERE a.run_id = ? AND s.status = 'committed'
                      AND a.attempt_no = (
                          SELECT MAX(attempt_no) FROM attempts
                          WHERE run_id = a.run_id AND subgoal_id = a.subgoal_id
                      )
                    ORDER BY s.ordinal
                    """,
                    (run_id,),
                ).fetchall()
                if rows:
                    evidence_level = weakest([r[0] for r in rows])
                else:
                    evidence_level = None

            conn.execute(
                """
                UPDATE runs SET
                  status = ?,
                  final_commit = ?,
                  ended_at = ?,
                  notes_chars = ?,
                  evidence_level = ?,
                  dependency_delta = ?
                WHERE run_id = ?
                """,
                (final_status, final_commit, end_time, notes_chars, evidence_level, dependency_delta, run_id),
            )

    def record_subgoals(
        self,
        run_id: str,
        subgoals: List[Subgoal],
    ) -> None:
        with self._get_connection() as conn:
            for idx, sg in enumerate(subgoals):
                sg_id = sg.id or f"sg-{idx + 1}"
                scope_json = json.dumps(sg.scope)
                expects_tests = 1 if sg.expects_tests else 0
                conn.execute(
                    """
                    INSERT INTO subgoals (
                      run_id, subgoal_id, ordinal, description, scope_json,
                      expects_tests, status, parent_sha, commit_sha
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                    ON CONFLICT(run_id, subgoal_id) DO UPDATE SET
                      ordinal=excluded.ordinal,
                      description=excluded.description,
                      scope_json=excluded.scope_json,
                      expects_tests=excluded.expects_tests,
                      status=excluded.status
                    """,
                    (
                        run_id,
                        sg_id,
                        idx + 1,
                        sg.description,
                        scope_json,
                        expects_tests,
                        sg.status or "pending",
                    ),
                )

    def record_subgoal_committed(
        self,
        run_id: str,
        subgoal_id: str,
        parent_sha: Optional[str] = None,
        commit_sha: Optional[str] = None,
    ) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE subgoals SET
                  status = 'committed',
                  parent_sha = ?,
                  commit_sha = ?
                WHERE run_id = ? AND subgoal_id = ?
                """,
                (parent_sha, commit_sha, run_id, subgoal_id),
            )

    def record_subgoal_status(
        self,
        run_id: str,
        subgoal_id: str,
        status: str,
    ) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE subgoals SET
                  status = ?
                WHERE run_id = ? AND subgoal_id = ?
                """,
                (status, run_id, subgoal_id),
            )

    def record_attempt(
        self,
        run_id: str,
        subgoal_id: str,
        tier: str,
        outcome: str,
        failed_check: Optional[str] = None,
        diff: str = "",
        diff_sha: str = "",
        files: Optional[List[str]] = None,
        compile_errors: Optional[Any] = None,
        tests_ran: bool = False,
        test_outcome: Optional[str] = None,
        jev_valid: Optional[bool] = None,
        jev_probability: Optional[float] = None,
        jev_model: Optional[str] = None,
        jev_raw: Optional[str] = None,
        jev_tokens_in: Optional[int] = None,
        jev_tokens_out: Optional[int] = None,
        feedback_sent: Optional[str] = None,
        tokens_in: Optional[int] = None,
        tokens_out: Optional[int] = None,
        created_at: Optional[str] = None,
        evidence_level: Optional[str] = None,
        dependency_delta: Optional[str] = None,
        jev_state_keys: Optional[str] = None,
        provenance: Optional[str] = None,
        model_version: Optional[str] = None,
        retry_number: Optional[int] = None,
        evidence_added: Optional[bool] = None,
        hypothesis_changed: Optional[bool] = None,
        hypothesis_change: Optional[Any] = None,
        failure_evidence: Optional[Any] = None,
        tool_evidence: Optional[Any] = None,
        read_evidence: Optional[Any] = None,
        self_check_evidence: Optional[Any] = None,
        retry_metadata: Optional[Any] = None,
        baseline_failures: Optional[int] = None,
        new_failures: Optional[int] = None,
        full_suite_clean: Optional[bool] = None,
        mech_evidence: Optional[MechanicalEvidence] = None,
        unverified_kind: Optional[str] = None,
    ) -> None:
        from jev.proposals import redact_secrets_in_text
        diff = redact_secrets_in_text(strip_raw_reasoning(diff))
        if feedback_sent:
            feedback_sent = redact_secrets_in_text(strip_raw_reasoning(feedback_sent))
        if jev_raw:
            jev_raw = redact_secrets_in_text(strip_raw_reasoning(jev_raw))
        now_ts = created_at or datetime.now(timezone.utc).isoformat()
        if not isinstance(jev_state_keys, str):
            jev_state_keys = None
        if not diff_sha:
            diff_sha = hashlib.sha256(diff.encode("utf-8")).hexdigest() if diff else hashlib.sha256(b"").hexdigest()

        files_json = json.dumps(files if files is not None else [])
        compile_json = None
        if compile_errors is not None:
            compile_json = _bounded_text(compile_errors)

        int_jev_valid = None
        if jev_valid is not None:
            int_jev_valid = 1 if jev_valid else 0

        m_schema_ver = None
        m_status = None
        m_reason = None
        m_ev_level = None
        m_base_status = None
        m_comparison = None
        m_cmd_identity = None
        m_target_scope = None
        m_target_rule = None
        m_target_conf = None
        m_runner_prof = None
        m_reg_ver = None
        m_lang = None
        m_worker_staged = None
        m_engine_touched = None
        m_test_author = None
        m_test_surface = None
        m_verifier_mod = None
        m_approval_valid = None
        m_comparison_valid = None
        m_mech_unverified = None
        m_unverified_kind = unverified_kind
        m_verif_inconclusive = None

        if mech_evidence is not None:
            m_schema_ver = mech_evidence.schema_version
            m_status = mech_evidence.mech_status.value if isinstance(mech_evidence.mech_status, Enum) else mech_evidence.mech_status
            m_reason = mech_evidence.reason.value if isinstance(mech_evidence.reason, Enum) else mech_evidence.reason
            m_ev_level = mech_evidence.evidence_level.value if isinstance(mech_evidence.evidence_level, Enum) else mech_evidence.evidence_level
            m_base_status = mech_evidence.base_status.value if isinstance(mech_evidence.base_status, Enum) else mech_evidence.base_status
            m_comparison = mech_evidence.comparison.value if isinstance(mech_evidence.comparison, Enum) else mech_evidence.comparison
            m_cmd_identity = json.dumps(mech_evidence.command_identity) if mech_evidence.command_identity is not None else None
            m_target_scope = json.dumps(mech_evidence.target_scope) if mech_evidence.target_scope is not None else None
            m_target_rule = mech_evidence.target_rule
            m_target_conf = mech_evidence.target_confidence.value if isinstance(mech_evidence.target_confidence, Enum) else mech_evidence.target_confidence
            m_runner_prof = mech_evidence.runner_profile_id
            m_reg_ver = mech_evidence.registry_version
            m_lang = mech_evidence.language
            m_worker_staged = json.dumps(mech_evidence.worker_staged_paths) if mech_evidence.worker_staged_paths is not None else None
            m_engine_touched = json.dumps(mech_evidence.engine_touched_paths) if mech_evidence.engine_touched_paths is not None else None
            m_test_author = mech_evidence.test_authorship
            m_test_surface = None if mech_evidence.test_surface_changed is None else (1 if mech_evidence.test_surface_changed else 0)
            m_verifier_mod = None if mech_evidence.verifier_modified is None else (1 if mech_evidence.verifier_modified else 0)
            m_approval_valid = None if mech_evidence.approval_valid is None else (1 if mech_evidence.approval_valid else 0)
            m_comparison_valid = None if mech_evidence.comparison_valid is None else (1 if mech_evidence.comparison_valid else 0)
            m_mech_unverified = None if mech_evidence.mechanically_unverified is None else (1 if mech_evidence.mechanically_unverified else 0)
            if m_unverified_kind is None and hasattr(mech_evidence, "unverified_kind"):
                m_unverified_kind = mech_evidence.unverified_kind
            m_verif_inconclusive = None if mech_evidence.verification_inconclusive is None else (1 if mech_evidence.verification_inconclusive else 0)

        with self._get_connection() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM attempts WHERE run_id = ? AND subgoal_id = ?",
                (run_id, subgoal_id),
            ).fetchone()[0]
            attempt_no = count + 1

            conn.execute(
                """
                INSERT INTO attempts (
                  run_id, subgoal_id, attempt_no, tier, outcome, failed_check,
                  diff, diff_sha, files_json, compile_errors_json,
                  tests_ran, test_outcome,
                  jev_valid, jev_probability, jev_model, jev_raw,
                  jev_tokens_in, jev_tokens_out,
                  feedback_sent, tokens_in, tokens_out, created_at,
                  evidence_level, dependency_delta, jev_state_keys, provenance, model_version, retry_number,
                  evidence_added, hypothesis_changed, hypothesis_change, failure_evidence, tool_evidence,
                  read_evidence, self_check_evidence, retry_metadata,
                  baseline_failures, new_failures, full_suite_clean,
                  mech_schema_version, mech_status, mech_reason, mech_evidence_level,
                  mech_base_status, mech_comparison, mech_command_identity_json,
                  mech_target_scope_json, mech_target_rule, mech_target_confidence,
                  mech_runner_profile_id, mech_registry_version, mech_language,
                  mech_worker_staged_json, mech_engine_touched_json,
                  mech_test_authorship, mech_test_surface_changed,
                  mech_verifier_modified, mech_approval_valid, mech_comparison_valid,
                  mech_mechanically_unverified, mech_unverified_kind, mech_verification_inconclusive
                 ) VALUES (
                   ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                   ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                   ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                   ?, ?, ?,
                   ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                   ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                 )
                """,
                (
                    run_id,
                    subgoal_id,
                    attempt_no,
                    tier,
                    outcome,
                    failed_check,
                    _bounded_text(diff) or "",
                    diff_sha,
                    files_json,
                    compile_json,
                    1 if tests_ran else 0,
                    test_outcome,
                    int_jev_valid,
                    jev_probability,
                    jev_model,
                    _bounded_text(jev_raw),
                    jev_tokens_in,
                    jev_tokens_out,
                    _bounded_text(feedback_sent),
                    tokens_in,
                    tokens_out,
                    now_ts,
                    evidence_level,
                    dependency_delta,
                    jev_state_keys,
                    provenance,
                    model_version or jev_model,
                    retry_number,
                    None if evidence_added is None else (1 if evidence_added else 0),
                    None if hypothesis_changed is None else (1 if hypothesis_changed else 0),
                    _bounded_json(hypothesis_change),
                    _bounded_text(failure_evidence),
                    _bounded_json(tool_evidence),
                    _bounded_json(read_evidence),
                    _bounded_json(self_check_evidence),
                    _bounded_json(retry_metadata),
                    baseline_failures,
                    new_failures,
                    None if full_suite_clean is None else (1 if full_suite_clean else 0),
                    m_schema_ver,
                    m_status,
                    m_reason,
                    m_ev_level,
                    m_base_status,
                    m_comparison,
                    m_cmd_identity,
                    m_target_scope,
                    m_target_rule,
                    m_target_conf,
                    m_runner_prof,
                    m_reg_ver,
                    m_lang,
                    m_worker_staged,
                    m_engine_touched,
                    m_test_author,
                    m_test_surface,
                    m_verifier_mod,
                    m_approval_valid,
                    m_comparison_valid,
                    m_mech_unverified,
                    m_unverified_kind,
                    m_verif_inconclusive,
                ),
            )
            conn.execute(
                "INSERT INTO attempt_assessments (run_id, subgoal_id, attempt_no, platform) VALUES (?, ?, ?, ?)",
                (run_id, subgoal_id, attempt_no, sys.platform),
            )

    def get_run_evidence_level(self, run_id: str) -> Optional[str]:
        with self._get_connection() as conn:
            row = conn.execute("SELECT evidence_level FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            return row[0] if row else None

    def get_run_dependency_delta(self, run_id: str) -> Optional[str]:
        with self._get_connection() as conn:
            row = conn.execute("SELECT dependency_delta FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            return row[0] if row else None

    def get_run_provenance(self, run_id: str) -> Optional[str]:
        with self._get_connection() as conn:
            row = conn.execute("SELECT provenance FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            return row[0] if row else None


    def get_attempt_mech_evidence(
        self,
        run_id: str,
        subgoal_id: Optional[str] = None,
        attempt_no: Optional[int] = None,
    ) -> Optional[MechanicalEvidence]:
        with self._get_connection() as conn:
            conn.row_factory = sqlite3.Row
            query = "SELECT * FROM attempts WHERE run_id = ?"
            params: List[Any] = [run_id]
            if subgoal_id is not None:
                query += " AND subgoal_id = ?"
                params.append(subgoal_id)
            if attempt_no is not None:
                query += " AND attempt_no = ?"
                params.append(attempt_no)
            query += " ORDER BY attempt_no DESC LIMIT 1"
            row = conn.execute(query, params).fetchone()
            if not row or row["mech_status"] is None:
                return None

            def _parse_json(val: Optional[str]) -> Any:
                if not val:
                    return None
                try:
                    return json.loads(val)
                except Exception:
                    return None

            def _to_bool(val: Optional[int]) -> Optional[bool]:
                if val is None:
                    return None
                return bool(val)

            return MechanicalEvidence(
                schema_version=row["mech_schema_version"] or 1,
                mech_status=MechStatus(row["mech_status"]),
                reason=MechReason(row["mech_reason"]) if row["mech_reason"] else None,
                evidence_level=EvidenceLevel(row["mech_evidence_level"]) if row["mech_evidence_level"] else None,
                base_status=BaseStatus(row["mech_base_status"]) if row["mech_base_status"] else None,
                comparison=Comparison(row["mech_comparison"]) if row["mech_comparison"] else None,
                command_identity=_parse_json(row["mech_command_identity_json"]),
                target_scope=_parse_json(row["mech_target_scope_json"]),
                target_rule=row["mech_target_rule"],
                target_confidence=TargetConfidence(row["mech_target_confidence"]) if row["mech_target_confidence"] else None,
                runner_profile_id=row["mech_runner_profile_id"],
                registry_version=row["mech_registry_version"],
                language=row["mech_language"],
                worker_staged_paths=_parse_json(row["mech_worker_staged_json"]),
                engine_touched_paths=_parse_json(row["mech_engine_touched_json"]),
                test_authorship=row["mech_test_authorship"],
                test_surface_changed=_to_bool(row["mech_test_surface_changed"]),
                verifier_modified=_to_bool(row["mech_verifier_modified"]),
                approval_valid=_to_bool(row["mech_approval_valid"]),
                comparison_valid=_to_bool(row["mech_comparison_valid"]),
                mechanically_unverified=_to_bool(row["mech_mechanically_unverified"]),
                unverified_kind=row["mech_unverified_kind"] if "mech_unverified_kind" in row.keys() else None,
                verification_inconclusive=_to_bool(row["mech_verification_inconclusive"]),
            )

    def measured_rate_report(
        self,
        since_days: int = 90,
    ) -> Dict[Tuple[Optional[str], Optional[str]], Dict[str, Any]]:
        """Legacy verdict-availability report, including exit-only verdicts.

        Use phase11_metrics_report for strong-evidence coverage and error rates.
        """
        with self._get_connection() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT
                    mech_language,
                    mech_runner_profile_id,
                    mech_status,
                    mech_reason,
                    mech_evidence_level,
                    created_at
                FROM attempts
                WHERE mech_status IS NOT NULL
                """
            ).fetchall()

        cutoff_dt = None
        if since_days is not None and since_days > 0:
            cutoff_dt = datetime.now(timezone.utc) - timedelta(days=since_days)

        ineligible_reasons = {
            MechReason.MISSING_TOOL.value,
            MechReason.NO_COMMAND.value,
            MechReason.ENVIRONMENT_UNAVAILABLE.value,
        }

        groups: Dict[Tuple[Optional[str], Optional[str]], Dict[str, Any]] = {}

        for row in rows:
            created_at_str = row["created_at"]
            if cutoff_dt and created_at_str:
                try:
                    row_dt = datetime.fromisoformat(created_at_str)
                    if row_dt.tzinfo is None:
                        row_dt = row_dt.replace(tzinfo=timezone.utc)
                    if row_dt < cutoff_dt:
                        continue
                except Exception:
                    pass

            key = (row["mech_language"], row["mech_runner_profile_id"])
            if key not in groups:
                groups[key] = {
                    "attempts_all": 0,
                    "measured": 0,
                    "unmeasured": 0,
                    "attempts_eligible": 0,
                    "exit_only_pass_count": 0,
                }

            g = groups[key]
            g["attempts_all"] += 1

            status = row["mech_status"]
            reason = row["mech_reason"]
            ev_level = row["mech_evidence_level"]

            if status in (MechStatus.PASS.value, MechStatus.FAIL.value):
                g["measured"] += 1
            elif status == MechStatus.UNMEASURED.value:
                g["unmeasured"] += 1

            if reason not in ineligible_reasons:
                g["attempts_eligible"] += 1

            if ev_level == EvidenceLevel.EXIT_ONLY.value and status == MechStatus.PASS.value:
                g["exit_only_pass_count"] += 1

        report: Dict[Tuple[Optional[str], Optional[str]], Dict[str, Any]] = {}
        for key, g in groups.items():
            rate_all, rate_eligible = _compute_rates(
                g["measured"], g["attempts_eligible"], g["attempts_all"]
            )
            report[key] = {
                "attempts_all": g["attempts_all"],
                "measured": g["measured"],
                "unmeasured": g["unmeasured"],
                "attempts_eligible": g["attempts_eligible"],
                "measured_rate_all": rate_all,
                "measured_rate_eligible": rate_eligible,
                "exit_only_pass_count": g["exit_only_pass_count"],
            }

        return report
