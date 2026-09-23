import os
import pytest

import run_substep_7a


def test_live_pipeline_execution():
    if os.environ.get("SKIP_LIVE_TEST") == "1":
        pytest.skip("Skipping in child pytest runner to prevent recursive test loop")

    report = run_substep_7a.run_pipeline()
    assert report is not None
    assert "diff_generated" in report
    assert "gate_status" in report
