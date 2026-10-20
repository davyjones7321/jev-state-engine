"""Bounded, engine-owned re-planning primitives.

The planner is deliberately injected as a callable.  This module only decides
whether the recorded evidence permits one bounded replacement of the remaining
queue; it never grants authority to the worker or to an advisory model.
"""

from dataclasses import dataclass
from typing import Any, Callable, List, Mapping, Optional, Sequence

from jev.models import State, Subgoal


DEFAULT_REPLAN_THRESHOLD = 2
MAX_REPLAN_BUDGET = 1
MAX_REPLAN_SUBGOALS = 4


@dataclass(frozen=True)
class ReplanResult:
    """Result of one bounded re-plan attempt."""

    replanned: bool
    reason: str
    queue: List[Subgoal]
    pruned: List[Subgoal]
    error: Optional[str] = None


def should_replan(
    state: Mapping[str, Any],
    failure_evidence: Mapping[str, Any],
    *,
    threshold: int = DEFAULT_REPLAN_THRESHOLD,
) -> bool:
    """Return true only for recorded dead-ends or near-limit failures.

    A single ordinary failure is intentionally insufficient.  Malformed or
    absent evidence cannot trigger a worker-controlled topology change.
    """
    if not isinstance(failure_evidence, Mapping):
        return False
    attempts = failure_evidence.get("attempt_count", len(state.get("failed_attempts", []) or []))
    if failure_evidence.get("dead_end") is True and isinstance(attempts, int) and attempts >= 2:
        return True
    try:
        limit = max(1, int(threshold))
        strikes = max(
            int(state.get("mechanical_strike_count", 0)),
            int(state.get("semantic_strike_count", 0)),
        )
    except (TypeError, ValueError):
        return False
    return strikes >= limit and bool(failure_evidence.get("failure_class"))


def _coerce_subgoals(raw: Any) -> List[Subgoal]:
    if isinstance(raw, Mapping):
        raw = raw.get("subgoals", raw.get("plan"))
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ValueError("re-planner output must be a JSON array of subgoals")
    if not raw:
        raise ValueError("re-planner output must contain at least one subgoal")
    result: List[Subgoal] = []
    for item in raw:
        if isinstance(item, Subgoal):
            candidate = item.model_copy(deep=True)
        elif isinstance(item, Mapping):
            candidate = Subgoal.model_validate(item)
        else:
            raise ValueError("each replacement subgoal must be an object")
        if not candidate.description.strip() or not candidate.scope:
            raise ValueError("replacement subgoals require a description and non-empty scope")
        candidate.status = "pending"
        result.append(candidate)
    return result


def replan_remaining_queue(
    state: State,
    failure_evidence: Mapping[str, Any],
    planner: Callable[[Mapping[str, Any], Sequence[Subgoal]], Any],
    *,
    validate_scope: Callable[[List[Subgoal]], Any],
    threshold: int = DEFAULT_REPLAN_THRESHOLD,
    max_budget: int = 1,
    max_subgoals: int = MAX_REPLAN_SUBGOALS,
) -> ReplanResult:
    """Replace only the uncommitted queue after a bounded engine decision.

    ``validate_scope`` must be the engine's normal grounding validator.  The
    planner output is untrusted and is never applied until schema, count, and
    grounding checks all succeed.
    """
    try:
        budget = min(max(0, int(max_budget)), MAX_REPLAN_BUDGET)
        used = int(state.get("replan_count", 0) or 0)
    except (TypeError, ValueError):
        return ReplanResult(False, "invalid_replan_budget", list(state.get("plan_queue", [])), [])
    if not should_replan(state, failure_evidence, threshold=threshold):
        return ReplanResult(False, "evidence_threshold_not_reached", list(state.get("plan_queue", [])), [])
    if used >= budget:
        return ReplanResult(False, "replan_budget_exhausted", list(state.get("plan_queue", [])), [])

    remaining = [Subgoal.model_validate(item) if isinstance(item, Mapping) else item.model_copy(deep=True)
                 for item in state.get("plan_queue", [])]
    committed = [item for item in state.get("committed_subgoals", [])]
    try:
        replacements = _coerce_subgoals(planner(failure_evidence, remaining))
        if len(replacements) > min(max(1, int(max_subgoals)), MAX_REPLAN_SUBGOALS):
            raise ValueError("re-planner output exceeds the replacement subgoal cap")
        validate_scope(replacements)
    except Exception as exc:
        return ReplanResult(False, "invalid_replan_output", remaining, [], str(exc))

    pruned = []
    for item in remaining:
        item.status = "pruned"
        pruned.append(item)
    state["plan_queue"] = replacements
    state["replan_count"] = used + 1
    state["replan_budget"] = budget
    state["pruned_subgoals"] = list(state.get("pruned_subgoals", [])) + pruned
    state["replan_history"] = list(state.get("replan_history", [])) + [{
        "failure_class": failure_evidence.get("failure_class"),
        "reason": failure_evidence.get("reason") or failure_evidence.get("dead_end", False),
        "replacement_count": len(replacements),
        "committed_count": len(committed),
    }]
    return ReplanResult(True, "replanned", replacements, pruned)
