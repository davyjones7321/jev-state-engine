import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
import pytest

from jev.mech_evidence import MechanicalEvidence, MechStatus, TargetConfidence
from jev.process_runner import CommandResult, run_command
from jev.workspace import (
    PROTECTED_PATHS,
    PathCollisionError,
    Workspace,
    _run_test_subprocess,
)


# ---------------------------------------------------------------------------
# 0. TargetConfidence Enum and validation
# ---------------------------------------------------------------------------

def test_target_confidence_enum_and_validation(tmp_path):
    # Valid enum values
    for val in ("exact", "repository_declared", "declared_full_suite", "heuristic"):
        conf = TargetConfidence(val)
        ev = MechanicalEvidence(
            mech_status=MechStatus.PASS,
            target_confidence=conf,
        )
        assert ev.target_confidence == conf
        assert ev.target_confidence.value == val

    # Direct string coercion
    ev2 = MechanicalEvidence(
        mech_status=MechStatus.PASS,
        target_confidence="exact",
    )
    assert ev2.target_confidence == TargetConfidence.EXACT

    # Invalid value rejected
    with pytest.raises(ValueError):
        MechanicalEvidence(
            mech_status=MechStatus.PASS,
            target_confidence="invalid_confidence",
        )

    # Ledger round-trip with TargetConfidence
    from jev.ledger import SqliteLedger
    db_path = tmp_path / "ledger.db"
    ledger = SqliteLedger(db_path=db_path)
    ledger.record_run_start("run-tc", "t1", "ticket-tc")
    ev_save = MechanicalEvidence(
        mech_status=MechStatus.PASS,
        target_confidence=TargetConfidence.EXACT,
        language="python",
        runner_profile_id="pytest",
    )
    ledger.record_attempt(
        run_id="run-tc",
        subgoal_id="sg-tc",
        tier="mechanical",
        outcome="passed",
        mech_evidence=ev_save,
    )
    ev_read = ledger.get_attempt_mech_evidence("run-tc", "sg-tc", 1)
    assert ev_read is not None
    assert ev_read.target_confidence == TargetConfidence.EXACT


# ---------------------------------------------------------------------------
# Helper: create a real git repository
# ---------------------------------------------------------------------------

def _create_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=path, check=True, capture_output=True)
    (path / "README.md").write_text("# Test Repo\n", encoding="utf-8")
    (path / ".gitignore").write_text("*.log\nnode_modules/\n.jev-worktrees/\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=path, check=True, capture_output=True)
    return path


# ---------------------------------------------------------------------------
# 1. Provenance: Generated untracked artifact ignored, scope passes, discarded
# ---------------------------------------------------------------------------

def test_generated_untracked_artifact_ignored_and_discarded(tmp_path):
    repo_dir = _create_git_repo(tmp_path / "repo")
    ws = Workspace(repo_dir=repo_dir)
    ws.create_subgoal_worktree(subgoal_id="subgoal-1")

    # Worker stages a valid in-scope file
    ws.stage_file_mutation("src/valid.py", "def valid(): pass\n")

    # Simulate an engine/build tool creating an untracked generated artifact and an ignored file
    gen_dir = ws.worktree_dir / "dist"
    gen_dir.mkdir(parents=True, exist_ok=True)
    gen_file = gen_dir / ".bun-build"
    gen_file.write_text("binary artifact\n", encoding="utf-8")

    ignored_file = ws.worktree_dir / "debug.log"
    ignored_file.write_text("log info\n", encoding="utf-8")

    # 1. get_staged_diff must NOT contain the generated artifact
    staged_diff = ws.get_staged_diff()
    assert ".bun-build" not in staged_diff
    assert "src/valid.py" in staged_diff

    # 2. check_scope must NOT fail due to the generated artifact
    from jev.models import Subgoal
    subgoal = Subgoal(id="subgoal-1", description="add valid", scope=["src/valid.py"])
    scope_res = ws.check_scope(diff=None, scope=subgoal.scope)
    assert scope_res.passed is True
    assert ".bun-build" not in scope_res.out_of_scope

    # 3. Run mechanical checks: provenance must record engine_touched_paths
    mech_res = ws.run_mechanical_checks(subgoal)
    prov = mech_res.checks.get("provenance", {})
    assert "src/valid.py" in prov.get("worker_staged_paths", [])
    assert any("dist/.bun-build" in p for p in prov.get("engine_touched_paths", []))

    # 4. After checks finish, untracked engine-touched artifact must be removed, but .gitignore'd file stays
    assert not gen_file.exists()
    assert ignored_file.exists()

    # 5. Commit subgoal: only worker-staged path is committed
    ws.commit_subgoal("Commit valid")
    res = subprocess.run(["git", "show", "--stat", "HEAD"], cwd=ws.worktree_dir, capture_output=True, text=True, check=True)
    assert "src/valid.py" in res.stdout
    assert ".bun-build" not in res.stdout


# ---------------------------------------------------------------------------
# 2. Provenance: Tracked file modified by engine command reverted afterwards
# ---------------------------------------------------------------------------

def test_tracked_file_modified_by_engine_reverted(tmp_path):
    repo_dir = _create_git_repo(tmp_path / "repo")
    # Add a tracked config file in main branch
    config_file = repo_dir / "config.json"
    config_file.write_text('{"version": 1}\n', encoding="utf-8")
    subprocess.run(["git", "add", "config.json"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Add config"], cwd=repo_dir, check=True, capture_output=True)

    ws = Workspace(repo_dir=repo_dir)
    ws.create_subgoal_worktree(subgoal_id="subgoal-mod")

    # Worker stages a feature file
    ws.stage_file_mutation("src/feature.py", "x = 1\n")

    # An engine tool mutates the tracked config.json
    wt_config = ws.worktree_dir / "config.json"
    wt_config.write_text('{"version": 999, "dirty": true}\n', encoding="utf-8")

    # Diff must only contain worker-staged file
    staged_diff = ws.get_staged_diff()
    assert "config.json" not in staged_diff
    assert "src/feature.py" in staged_diff

    # Mechanical checks
    from jev.models import Subgoal
    subgoal = Subgoal(id="subgoal-mod", description="feat", scope=["src/feature.py"])
    mech_res = ws.run_mechanical_checks(subgoal)
    prov = mech_res.checks.get("provenance", {})
    assert "config.json" in prov.get("engine_touched_paths", [])

    # Config must be reverted to original content
    assert wt_config.read_text(encoding="utf-8") == '{"version": 1}\n'

    # Commit must not contain config.json
    ws.commit_subgoal("Commit feat")
    show = subprocess.run(["git", "show", "--name-only", "HEAD"], cwd=ws.worktree_dir, capture_output=True, text=True, check=True)
    assert "src/feature.py" in show.stdout
    assert "config.json" not in show.stdout


# ---------------------------------------------------------------------------
# 3. Provenance: Worker-staged in-scope passes, out-of-scope fails
# ---------------------------------------------------------------------------

def test_worker_staged_scope_enforcement(tmp_path):
    repo_dir = _create_git_repo(tmp_path / "repo")
    ws = Workspace(repo_dir=repo_dir)
    ws.create_subgoal_worktree(subgoal_id="subgoal-scope")

    # Stage out of scope file
    ws.stage_file_mutation("extra.py", "secret = True\n")
    from jev.models import Subgoal
    subgoal = Subgoal(id="subgoal-scope", description="scope check", scope=["src/app.py"])

    res = ws.check_scope(diff=None, scope=subgoal.scope)
    assert res.passed is False
    assert "extra.py" in res.out_of_scope


# ---------------------------------------------------------------------------
# 4. Provenance: Several staged files tracked, paths normalized
# ---------------------------------------------------------------------------

def test_several_staged_files_normalization(tmp_path):
    repo_dir = _create_git_repo(tmp_path / "repo")
    ws = Workspace(repo_dir=repo_dir)
    ws.create_subgoal_worktree(subgoal_id="subgoal-norm")

    ws.stage_file_mutation("./src/a.py", "a = 1\n")
    ws.stage_file_mutation("src\\b.py", "b = 2\n")
    ws.stage_file_mutation("src/sub/../c.py", "c = 3\n")

    assert "src/a.py" in ws.worker_staged_paths
    assert "src/b.py" in ws.worker_staged_paths
    assert "src/c.py" in ws.worker_staged_paths
    # No raw backslashes or ./ in recorded paths
    for p in ws.worker_staged_paths:
        assert "\\" not in p
        assert not p.startswith("./")
        assert ".." not in p


# ---------------------------------------------------------------------------
# 5. Provenance: Collision detection and PathCollisionError
# ---------------------------------------------------------------------------

def test_collision_detection_and_refuse_commit(tmp_path):
    repo_dir = _create_git_repo(tmp_path / "repo")
    ws = Workspace(repo_dir=repo_dir)
    ws.create_subgoal_worktree(subgoal_id="subgoal-col")

    # Worker stages file A
    ws.stage_file_mutation("src/critical.py", "# original worker content\n")

    # A command rewrites file A
    (ws.worktree_dir / "src" / "critical.py").write_text("# tampered by engine\n", encoding="utf-8")

    from jev.models import Subgoal
    subgoal = Subgoal(id="subgoal-col", description="col test", scope=["src/critical.py"])
    mech_res = ws.run_mechanical_checks(subgoal)
    prov = mech_res.checks.get("provenance", {})
    assert "src/critical.py" in prov.get("collision_paths", [])

    # Attempting to commit must raise PathCollisionError
    with pytest.raises(PathCollisionError):
        ws.commit_subgoal("Should fail")


# ---------------------------------------------------------------------------
# 6. Provenance: Protected paths and traversal rejected
# ---------------------------------------------------------------------------

def test_protected_paths_and_traversal_rejected(tmp_path):
    repo_dir = _create_git_repo(tmp_path / "repo")
    ws = Workspace(repo_dir=repo_dir)
    ws.create_subgoal_worktree(subgoal_id="subgoal-prot")

    # .git
    with pytest.raises(PermissionError):
        ws.stage_file_mutation(".git/config", "dirty")

    with pytest.raises(PermissionError):
        ws.stage_file_mutation("./.git/HEAD", "dirty")

    with pytest.raises(PermissionError):
        ws.stage_file_mutation(".git\\hooks\\pre-commit", "dirty")

    # .jev
    with pytest.raises(PermissionError):
        ws.stage_file_mutation(".jev/knowledge.db", "dirty")

    # node_modules
    with pytest.raises(PermissionError):
        ws.stage_file_mutation("node_modules/pkg/index.js", "dirty")

    # .. traversal escaping workspace
    with pytest.raises(PermissionError):
        ws.stage_file_mutation("../../outside.py", "dirty")

    with pytest.raises(PermissionError):
        ws.stage_file_mutation("src/../../.git/config", "dirty")


# ---------------------------------------------------------------------------
# 7. Process Runner: Exit codes and missing executable launch_failed
# ---------------------------------------------------------------------------

def test_process_runner_exit_code_and_launch_failed():
    # Normal exit code 42
    res = run_command([sys.executable, "-c", "import sys; sys.exit(42)"])
    assert res.exit_code == 42
    assert res.launch_failed is False
    assert res.timed_out is False
    assert res.cleanup_method == "none"

    # Missing executable
    res_missing = run_command(["non_existent_executable_binary_123456789"])
    assert res_missing.launch_failed is True
    assert res_missing.exit_code is None
    assert res_missing.timed_out is False
    assert res_missing.termination_reason == "launch_failed"
    assert res_missing.cleanup_method == "none"


# ---------------------------------------------------------------------------
# 8. Process Runner: Timeout kills child and grandchild (repeat 20 times)
# ---------------------------------------------------------------------------

def test_process_runner_timeout_and_descendants_20_times():
    expected_cleanup = "job_object" if sys.platform == "win32" else "process_group"

    # Script that spawns a grandchild sleeping for 60s, then itself sleeps for 60s
    spawn_grandchild_code = """
import subprocess, sys, time
p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
time.sleep(60)
"""

    for _ in range(20):
        res = run_command(
            [sys.executable, "-c", spawn_grandchild_code],
            timeout=1.0,
        )
        assert res.timed_out is True
        assert res.termination_reason == "timeout"
        assert res.cleanup_method == expected_cleanup
        assert res.descendants_remaining == 0


# ---------------------------------------------------------------------------
# 9. Process Runner: Bounded tails and digests
# ---------------------------------------------------------------------------

def test_process_runner_bounded_tails_and_digests():
    # Produce >16k of stdout and stderr
    code = """
import sys
sys.stdout.write("A" * 10000 + "TAIL_STDOUT")
sys.stderr.write("B" * 10000 + "TAIL_STDERR")
"""
    res = run_command([sys.executable, "-c", code])
    assert res.exit_code == 0
    assert len(res.stdout_tail) <= 8000
    assert res.stdout_tail.endswith("TAIL_STDOUT")
    assert len(res.stderr_tail) <= 8000
    assert res.stderr_tail.endswith("TAIL_STDERR")

    expected_out_sha = hashlib.sha256(("A" * 10000 + "TAIL_STDOUT").encode("utf-8")).hexdigest()
    expected_err_sha = hashlib.sha256(("B" * 10000 + "TAIL_STDERR").encode("utf-8")).hexdigest()
    assert res.stdout_sha256 == expected_out_sha
    assert res.stderr_sha256 == expected_err_sha


# ---------------------------------------------------------------------------
# 10. Process Runner: _run_test_subprocess raises RuntimeError on timeout
# ---------------------------------------------------------------------------

def test_run_test_subprocess_raises_runtime_error_on_timeout(tmp_path):
    cmd = [sys.executable, "-c", "import time; time.sleep(10)"]
    with pytest.raises(RuntimeError, match="timed out after 1 seconds"):
        _run_test_subprocess(cmd, cwd=tmp_path, timeout=1)
