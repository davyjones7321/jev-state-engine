import os
from pathlib import Path
import pytest

import run_substep_7c


def test_live_substep_7c_investigate_to_plan():
    if os.environ.get("SKIP_LIVE_TEST") == "1":
        pytest.skip("Skipping live pipeline test")

    report = run_substep_7c.run_pipeline()

    assert report is not None
    assert report.get("gate_status") != "planning_failed"
    plan_queue = report.get("plan_queue", [])
    assert len(plan_queue) >= 1

    # Verify each subgoal has non-empty description and declared scope
    for sg in plan_queue:
        assert len(sg["description"]) > 0
        assert len(sg["scope"]) > 0
        assert isinstance(sg["expects_tests"], bool)
