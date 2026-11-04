import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from main import build_parser, main
from jev.engine import JevEngine
from jev.proposals import Proposal


def test_three_no_op_flags_are_rejected_by_parser():
    """--registry-resolution, --approved-proposals, and --same-command-authority (and their aliases)
    are no-ops and must be rejected by the CLI argument parser.
    """
    parser = build_parser()
    disallowed_flags = [
        "--registry-resolution",
        "--runner-registry",
        "--approved-proposals",
        "--proposals",
        "--same-command-authority",
    ]
    for flag in disallowed_flags:
        with pytest.raises(SystemExit):
            parser.parse_args([flag])


def test_require_tests_cli_parsing():
    """--require-tests supports flag notation (--require-tests -> on), explicit values
    (--require-tests on / --require-tests off), and defaults to 'off'.
    """
    parser = build_parser()
    assert parser.parse_args([]).require_tests == "off"
    assert parser.parse_args(["--require-tests"]).require_tests == "on"
    assert parser.parse_args(["--require-tests", "on"]).require_tests == "on"
    assert parser.parse_args(["--require-tests", "off"]).require_tests == "off"


def test_proposal_hint_prints_for_no_command_run_without_switch(capsys, tmp_path):
    """The proposal hint 'Proposal: <id> (run 'python scripts/approve.py <id>' to review and approve)'
    must print whenever no test command exists, without requiring any command-line switch.
    """
    dummy_prop = Proposal(
        id="prop-test-1234",
        repo_id="repo-1",
        exact_argv=["pytest"],
        source_evidence="pytest.ini",
        platform="win32",
        identity_hash="abc1234",
        created_at="2026-11-04T00:00:00Z",
    )

    with patch("main.JevEngine") as mock_engine_cls, \
         patch("main.Workspace") as mock_ws_cls, \
         patch("main.Gatekeeper"), \
         patch("main.SqliteLedger"), \
         patch("jev.proposals.list_proposals", return_value=[dummy_prop]):

        mock_engine = MagicMock()
        mock_engine.execute.return_value = {
            "status": "completed",
            "gate_status": "passed",
            "trajectory": [
                {
                    "node": "gate",
                    "mech_evidence": {"reason": "NO_COMMAND"},
                }
            ],
            "committed_subgoals": [],
            "mechanically_unverified_subgoals": ["sg-1"],
            "mechanically_unverified_kinds": {},
        }
        mock_engine_cls.return_value = mock_engine

        mock_ws = MagicMock()
        mock_ws.repo_dir = tmp_path
        mock_ws_cls.return_value = mock_ws

        # Execute main with no proposal switch
        exit_code = main(["Ticket no command", "--repo-dir", str(tmp_path)])
        captured = capsys.readouterr().out

        assert "Evidence: semantic-only (no tests ran: no test framework configured)" in captured or \
               "no test framework configured" in captured
        assert "Proposal: prop-test-1234 (run 'python scripts/approve.py prop-test-1234' to review and approve)" in captured


def test_engine_does_not_contain_no_op_switch_attributes():
    """JevEngine.__init__ and initial_state must no longer contain the three no-op switches."""
    ws = MagicMock()
    ws.repo_dir = Path("/tmp/mock_repo")
    engine = JevEngine(workspace=ws, gatekeeper=MagicMock())

    assert not hasattr(engine, "registry_resolution")
    assert not hasattr(engine, "approved_proposals")
    assert not hasattr(engine, "same_command_authority")

    with patch.object(engine.app, "invoke") as mock_invoke:
        mock_invoke.return_value = {"trajectory": []}
        engine.execute("Ticket test", "thread-1")
        initial_state = mock_invoke.call_args[0][0]
        assert "registry_resolution" not in initial_state
        assert "approved_proposals" not in initial_state
        assert "same_command_authority" not in initial_state
        assert initial_state.get("require_tests") == "off"
