from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field
from typing_extensions import TypedDict


class TestOutcome(str, Enum):
    __test__ = False
    PASSED = "PASSED"
    FAILED = "FAILED"
    NO_TESTS_COLLECTED = "NO_TESTS_COLLECTED"
    NO_TEST_FRAMEWORK = "NO_TEST_FRAMEWORK"
    ENV_NOT_READY = "ENV_NOT_READY"


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
    description: str = ""
    scope: List[str] = Field(default_factory=list)
    expects_tests: bool = True


class MechanicalCheckResult(BaseModel):
    passed: bool
    failed_check: Optional[str] = None  # "build" | "compile" | "no_compile_command" | "env_not_ready" | "tests" | "no_tests_collected" | "scope"
    detail: str = ""
    checks_run: List[str] = Field(default_factory=list)
    checks: Dict[str, Any] = Field(default_factory=dict)
    test_runner_outcome: Optional[Dict[str, Any]] = None
    compile_outcome: Optional[Dict[str, Any]] = None


class ValidationVerdict(BaseModel):
    valid: bool
    probability: float = 1.0
    reason: Optional[str] = None


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


