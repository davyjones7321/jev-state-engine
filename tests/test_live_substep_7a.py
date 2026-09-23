import os
import pytest

import run_substep_7a


def test_live_pipeline_execution():
    if os.environ.get("RUN_LIVE_PIPELINE") != "1" or os.environ.get("SKIP_LIVE_TEST") == "1":
        pytest.skip("Set RUN_LIVE_PIPELINE=1 to execute live pipeline")

    report = run_substep_7a.run_pipeline()
    assert report is not None
    assert report.get("committed") is True
    assert report.get("gate_status") == "passed"
    assert "Executes read-only CLI commands in worktree_dir and returns stdout or stderr." in report.get("diff_generated", "")
