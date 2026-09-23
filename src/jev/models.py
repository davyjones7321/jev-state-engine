from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field
from typing_extensions import TypedDict


class TestOutcome(str, Enum):
    __test__ = False
    PASSED = "PASSED"
    FAILED = "FAILED"
    NO_TESTS_COLLECTED = "NO_TESTS_COLLECTED"


class Subgoal(BaseModel):
    description: str = ""
    scope: List[str] = Field(default_factory=list)
    expects_tests: bool = True


class MechanicalCheckResult(BaseModel):
    passed: bool
    failed_check: Optional[str] = None  # "build" | "tests" | "no_tests_collected" | "scope"
    detail: str = ""


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
