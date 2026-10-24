from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, model_validator


class MechStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNMEASURED = "UNMEASURED"


class MechReason(str, Enum):
    MISSING_TOOL = "MISSING_TOOL"
    NO_COMMAND = "NO_COMMAND"
    BASELINE_UNAVAILABLE = "BASELINE_UNAVAILABLE"
    TIMEOUT = "TIMEOUT"
    TOOLING_ERROR = "TOOLING_ERROR"
    FLAKY = "FLAKY"
    NO_TEST_EXECUTION_SIGNAL = "NO_TEST_EXECUTION_SIGNAL"
    ENGINE_WORKER_PATH_COLLISION = "ENGINE_WORKER_PATH_COLLISION"
    ENVIRONMENT_UNAVAILABLE = "ENVIRONMENT_UNAVAILABLE"
    COMMAND_IDENTITY_CHANGED = "COMMAND_IDENTITY_CHANGED"
    TEST_SURFACE_CHANGED = "TEST_SURFACE_CHANGED"


class EvidenceLevel(str, Enum):
    COUNTED = "counted"
    NAMED = "named"
    EXIT_ONLY = "exit_only"
    NONE = "none"


class BaseStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNMEASURED = "UNMEASURED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    UNKNOWN = "UNKNOWN"


class Comparison(str, Enum):
    SAME = "SAME"
    CANDIDATE_ONLY = "CANDIDATE_ONLY"
    INCONCLUSIVE = "INCONCLUSIVE"
    NOT_COMPARED = "NOT_COMPARED"


class MechanicalEvidence(BaseModel):
    schema_version: int = 1
    mech_status: MechStatus
    reason: Optional[MechReason] = None
    evidence_level: Optional[EvidenceLevel] = None
    base_status: Optional[BaseStatus] = None
    comparison: Optional[Comparison] = None
    command_identity: Optional[Dict[str, Any]] = None
    target_scope: Optional[Dict[str, Any]] = None
    target_rule: Optional[str] = None
    target_confidence: Optional[float] = None
    runner_profile_id: Optional[str] = None
    registry_version: Optional[str] = None
    language: Optional[str] = None
    worker_staged_paths: Optional[List[str]] = None
    engine_touched_paths: Optional[List[str]] = None
    test_authorship: Optional[str] = None
    test_surface_changed: Optional[bool] = None
    verifier_modified: Optional[bool] = None
    approval_valid: Optional[bool] = None
    comparison_valid: Optional[bool] = None
    mechanically_unverified: Optional[bool] = None
    verification_inconclusive: Optional[bool] = None
    full_suite_verified: bool = False

    @model_validator(mode="after")
    def validate_invariants(self) -> MechanicalEvidence:
        # a. mech_status==UNMEASURED requires a reason
        if self.mech_status == MechStatus.UNMEASURED and self.reason is None:
            raise ValueError("mech_status UNMEASURED requires a reason")

        # b. mech_status in (PASS, FAIL) must not carry a reason
        if self.mech_status in (MechStatus.PASS, MechStatus.FAIL) and self.reason is not None:
            raise ValueError(f"mech_status {self.mech_status.value} must not carry a reason")

        # c. UNMEASURED forces mechanically_unverified=True
        if self.mech_status == MechStatus.UNMEASURED:
            if self.mechanically_unverified is False:
                raise ValueError("UNMEASURED forces mechanically_unverified=True; cannot be False")
            self.mechanically_unverified = True

        # d. evidence_level in (exit_only, none) forces full_suite_verified=False
        # and forces mechanically_unverified=True when status is PASS
        if self.evidence_level in (EvidenceLevel.EXIT_ONLY, EvidenceLevel.NONE):
            if self.full_suite_verified is True:
                raise ValueError(f"evidence_level {self.evidence_level.value} forces full_suite_verified=False")
            self.full_suite_verified = False
            if self.mech_status == MechStatus.PASS:
                if self.mechanically_unverified is False:
                    raise ValueError(
                        f"evidence_level {self.evidence_level.value} forces mechanically_unverified=True when status is PASS"
                    )
                self.mechanically_unverified = True

        return self
