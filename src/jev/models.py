from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from jev.mech_evidence import (
    BaseStatus,
    Comparison,
    EvidenceLevel,
    MechanicalEvidence,
    MechReason,
    MechStatus,
)


class TestOutcome(str, Enum):
    __test__ = False
    PASSED = "PASSED"
    FAILED = "FAILED"
    NO_TESTS_COLLECTED = "NO_TESTS_COLLECTED"
    NO_TEST_FRAMEWORK = "NO_TEST_FRAMEWORK"
    ENV_NOT_READY = "ENV_NOT_READY"
    FULL_SUITE_PASSED = "FULL_SUITE_PASSED"
    REGRESSION_FREE_WITH_BASELINE_FAILURES = "REGRESSION_FREE_WITH_BASELINE_FAILURES"
    REGRESSION_DETECTED = "REGRESSION_DETECTED"
    EXEMPT_BY_POLICY = "EXEMPT_BY_POLICY"
    PASSED_EXIT_ONLY = "PASSED_EXIT_ONLY"


class CompileOutcome(str, Enum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    EXEMPT = "EXEMPT"
    NO_COMPILE_COMMAND = "NO_COMPILE_COMMAND"
    ENV_NOT_READY = "ENV_NOT_READY"


class TestPolicy(str, Enum):
    AUTO = "auto"
    VERIFY_ONLY = "verify-only"
    NEVER = "never"
    ALWAYS = "always"


class Subgoal(BaseModel):
    id: str = ""
    description: str = ""
    scope: List[str] = Field(default_factory=list)
    expects_tests: bool = True
    status: str = "pending"


class MechanicalCheckResult(BaseModel):
    passed: bool
    failed_check: Optional[str] = None  # "build" | "suppression" | "unresolved_import" | "compile" | "no_compile_command" | "env_not_ready" | "tests" | "no_tests_collected" | "scope"
    detail: str = ""
    checks_run: List[str] = Field(default_factory=list)
    checks: Dict[str, Any] = Field(default_factory=dict)
    test_runner_outcome: Optional[Dict[str, Any]] = None
    compile_outcome: Optional[Dict[str, Any]] = None
    test_classification: Optional[str] = None
    full_suite_clean: Optional[bool] = None
    baseline_failures: Optional[int] = None
    new_failures: Optional[int] = None
    baseline_failing_ids: Optional[List[str]] = None
    new_failing_ids: Optional[List[str]] = None
    mech_evidence: Optional[Any] = None


class ValidationVerdict(BaseModel):
    valid: bool
    probability: float = 1.0
    reason: Optional[str] = None
    model: Optional[str] = None
    raw_response: Optional[Dict[str, Any]] = None
    usage: Optional[Dict[str, Any]] = None
    kind: Optional[str] = None
    kind_confidence: Optional[float] = None
    kind_probabilities: Optional[Dict[str, float]] = None
    incomplete: Optional[float] = None
    wrong_behavior: Optional[float] = None
    conflicts_with_notes: Optional[float] = None
    extra_answers: Optional[Dict[str, Any]] = None


class JevFeedback(BaseModel):
    """Bounded, internal representation of feedback returned by Jev.

    This model is observational only: the existing ``ValidationVerdict.valid``
    value remains the source of gate authority.
    """

    verdict: Optional[bool] = None
    confidence: Optional[float] = None
    diagnostics: List[str] = Field(default_factory=list)
    questions: List[str] = Field(default_factory=list)
    unknowns: List[str] = Field(default_factory=list)


class State(TypedDict, total=False):
    ticket: str
    plan_queue: List[Subgoal]
    current_subgoal: Optional[Subgoal]
    mechanical_strike_count: int
    semantic_strike_count: int
    trajectory: List[Dict[str, Any]]
    gate_status: Optional[str]
    last_feedback: Optional[str]
    status: Optional[str]
    investigation_notes: Optional[str]
    investigated_directories: List[str]
    investigation_incomplete: bool
    current_worktree_path: Optional[str]
    current_worktree_branch: Optional[str]
    thread_id: Optional[str]
    base_commit: Optional[str]
    integration_branch: Optional[str]
    subgoal_base_commit: Optional[str]
    test_policy: Optional[str]
    run_id: Optional[str]
    worker_tokens_in: Optional[int]
    worker_tokens_out: Optional[int]
    failed_attempts: Optional[List[Dict[str, Any]]]
    retry_intel: Optional[str]
    repo_profile: Optional[str]
    repo_profile_text: Optional[str]
    dep_delta_jev: Optional[str]
    replan_count: int
    replan_budget: int
    committed_subgoals: List[Subgoal]
    pruned_subgoals: List[Subgoal]
    replan_history: List[Dict[str, Any]]
    role_provenance: Optional[Dict[str, Dict[str, Any]]]
    test_classification: Optional[str]
    full_suite_clean: Optional[bool]
    baseline_failures: Optional[int]
    new_failures: Optional[int]
    baseline_failing_ids: Optional[List[str]]
    new_failing_ids: Optional[List[str]]
