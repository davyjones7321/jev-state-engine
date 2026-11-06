"""Focused unit tests for scripts/verify_phase.py.

Verifies:
1. Verifier exits with code 0 and prints '=== VERIFICATION PASSED ===' when pytest succeeds.
2. Verifier exits with non-zero code and prints '=== VERIFICATION FAILED ... ===' when pytest fails.
3. session-1-v2.md alone is not reported as dirty.
4. Any other untracked file (for example "HARDENING_PLAN (1).md" or hash-ticket.txt) IS reported as a dirty-tree warning.
5. Dirty tree warnings do not fail the verification script.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure scripts dir is accessible
scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))

import verify_phase


def test_verifier_passes_when_pytest_succeeds(capsys):
    """When the offline pytest suite succeeds (returncode 0), verifier prints PASSED and exits 0."""
    def fake_run(cmd, *args, **kwargs):
        mock_res = MagicMock()
        mock_res.returncode = 0
        cmd_str = " ".join(cmd)
        if "git status" in cmd_str:
            mock_res.stdout = "?? session-1-v2.md\n"
        elif "git rev-parse" in cmd_str:
            mock_res.stdout = "hardening-run-a\n"
        elif "git diff" in cmd_str:
            mock_res.stdout = ""
        elif "git grep" in cmd_str:
            mock_res.stdout = ""
        elif "pytest" in cmd_str and "--collect-only" in cmd_str:
            mock_res.stdout = "collected 5 tests\n"
        elif "pytest" in cmd_str:
            mock_res.stdout = "100 passed in 10s\n"
        else:
            mock_res.stdout = ""
        mock_res.stderr = ""
        return mock_res

    with patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(SystemExit) as exc_info:
            verify_phase.main(["verify_phase.py", "origin/master"])
        assert exc_info.value.code == 0

    captured = capsys.readouterr()
    assert "=== VERIFICATION PASSED ===" in captured.out
    assert "WARNING: Working tree is dirty!" not in captured.out


def test_verifier_fails_when_pytest_fails(capsys):
    """When the offline pytest suite fails (returncode 1), verifier prints FAILED and exits non-zero."""
    def fake_run(cmd, *args, **kwargs):
        mock_res = MagicMock()
        cmd_str = " ".join(cmd)
        if "git status" in cmd_str:
            mock_res.returncode = 0
            mock_res.stdout = "?? session-1-v2.md\n"
        elif "git rev-parse" in cmd_str:
            mock_res.returncode = 0
            mock_res.stdout = "hardening-run-a\n"
        elif "git diff" in cmd_str:
            mock_res.returncode = 0
            mock_res.stdout = ""
        elif "git grep" in cmd_str:
            mock_res.returncode = 0
            mock_res.stdout = ""
        elif "pytest" in cmd_str and "--collect-only" in cmd_str:
            mock_res.returncode = 0
            mock_res.stdout = "collected 5 tests\n"
        elif "pytest" in cmd_str:
            # Simulate a pytest test failure
            mock_res.returncode = 1
            mock_res.stdout = (
                "FAILED tests/test_core.py::test_bar - AssertionError\n"
                "=== short test summary info ===\n"
                "FAILED tests/test_core.py::test_bar\n"
                "1 failed, 99 passed in 10s\n"
            )
        else:
            mock_res.returncode = 0
            mock_res.stdout = ""
        mock_res.stderr = ""
        return mock_res

    with patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(SystemExit) as exc_info:
            verify_phase.main(["verify_phase.py", "origin/master"])
        assert exc_info.value.code == 1

    captured = capsys.readouterr()
    assert "=== VERIFICATION FAILED (pytest exited with code 1) ===" in captured.out
    assert "FAILED tests/test_core.py::test_bar" in captured.out


def test_verifier_session_1_v2_alone_not_reported_dirty(capsys):
    """session-1-v2.md alone does not trigger dirty-tree warning."""
    def fake_run(cmd, *args, **kwargs):
        mock_res = MagicMock()
        mock_res.returncode = 0
        cmd_str = " ".join(cmd)
        if "git status" in cmd_str:
            mock_res.stdout = "?? session-1-v2.md\n"
        elif "pytest" in cmd_str:
            mock_res.stdout = "10 passed\n"
        else:
            mock_res.stdout = ""
        mock_res.stderr = ""
        return mock_res

    with patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(SystemExit) as exc_info:
            verify_phase.main(["verify_phase.py", "origin/master"])
        assert exc_info.value.code == 0

    captured = capsys.readouterr()
    assert "WARNING: Working tree is dirty!" not in captured.out


def test_verifier_warns_on_other_untracked_files(capsys):
    """Any other untracked file (e.g. HARDENING_PLAN (1).md or hash-ticket.txt) IS reported dirty, but exit is 0."""
    def fake_run(cmd, *args, **kwargs):
        mock_res = MagicMock()
        mock_res.returncode = 0
        cmd_str = " ".join(cmd)
        if "git status" in cmd_str:
            mock_res.stdout = (
                '?? session-1-v2.md\n'
                '?? "HARDENING_PLAN (1).md"\n'
                '?? hash-ticket.txt\n'
            )
        elif "pytest" in cmd_str:
            mock_res.stdout = "10 passed\n"
        else:
            mock_res.stdout = ""
        mock_res.stderr = ""
        return mock_res

    with patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(SystemExit) as exc_info:
            verify_phase.main(["verify_phase.py", "origin/master"])
        assert exc_info.value.code == 0

    captured = capsys.readouterr()
    assert "WARNING: Working tree is dirty!" in captured.out


def test_verifier_warns_on_unrecognized_dirty_files(capsys):
    """Unrecognized dirty or untracked files trigger warning, but exit is 0."""
    def fake_run(cmd, *args, **kwargs):
        mock_res = MagicMock()
        mock_res.returncode = 0
        cmd_str = " ".join(cmd)
        if "git status" in cmd_str:
            mock_res.stdout = (
                '?? session-1-v2.md\n'
                ' M src/jev/engine.py\n'
                '?? untracked_script.py\n'
            )
        elif "pytest" in cmd_str:
            mock_res.stdout = "10 passed\n"
        else:
            mock_res.stdout = ""
        mock_res.stderr = ""
        return mock_res

    with patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(SystemExit) as exc_info:
            verify_phase.main(["verify_phase.py", "origin/master"])
        assert exc_info.value.code == 0

    captured = capsys.readouterr()
    assert "WARNING: Working tree is dirty!" in captured.out
