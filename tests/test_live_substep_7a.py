import os
import sys
import json
import pytest
from pathlib import Path
from dotenv import dotenv_values

from jev.models import State, Subgoal
from jev.workspace import Workspace
from jev.gatekeeper import Gatekeeper
from jev.engine import node_implement, node_gate


def test_live_pipeline_execution():
    """Live end-to-end subgoal attempt:
    node_implement (real Gemini call using gemini-3.5-flash-lite)
    -> node_gate (real Tier 0 checks)
    -> Gatekeeper.validate_subgoal (real Jev API call).
    """
    import run_substep_7a
    report = run_substep_7a.run_pipeline()
    assert report is not None
    assert "diff_generated" in report
    assert "gate_status" in report
