import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from jev.models import MechanicalCheckResult, Subgoal, TestOutcome
from jev.mech_evidence import Comparison, EvidenceLevel, MechanicalEvidence, MechReason, MechStatus
from jev.repo_config import (
    PROTECTED_FILES,
    PROTECTED_PREFIXES,
    RepoConfig,
    TrustBinding,
    compute_approval_hash,
    compute_repo_id,
    is_build_or_test_config,
    is_lockfile,
    is_protected_path,
    load_repo_config,
    record_trust,
    save_repo_config,
    validate_trust,
)
from jev.workspace import Workspace, resolve_test_command


def _create_git_repo(path: Path, initial_text: str = "# Test Repo\n") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=path, check=True, capture_output=True)
    (path / "README.md").write_text(initial_text, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=path, check=True, capture_output=True)
    return path


# ==============================================================================
# 1. Protected Paths Cannot Be Staged
# ==============================================================================

def test_protected_paths_classification():
    assert is_protected_path(".jev/config.json") is True
    assert is_protected_path(".jev/proposals/p1.json") is True
    assert is_protected_path(".jev") is True
    assert is_protected_path(".git/HEAD") is True
    assert is_protected_path(".git/config") is True
    assert is_protected_path("node_modules/foo/index.js") is True
    assert is_protected_path("runner_profiles.json") is True
    assert is_protected_path("src/jev/runner_profiles.json") is True
    assert is_protected_path("runner_registry.py") is True
    assert is_protected_path("src/jev/runner_registry.py") is True
    assert is_protected_path("repo_config.py") is True
    assert is_protected_path("src/jev/repo_config.py") is True
    assert is_protected_path("process_runner.py") is True
    assert is_protected_path("src/jev/process_runner.py") is True

    # Non-protected files
    assert is_protected_path("src/app.py") is False
    assert is_protected_path("tests/test_app.py") is False
    assert is_protected_path("package.json") is False
    assert is_protected_path("pytest.ini") is False
    assert is_protected_path("package-lock.json") is False


def test_protected_paths_cannot_be_staged(tmp_path):
    repo_dir = _create_git_repo(tmp_path / "repo")
    ws = Workspace(repo_dir=repo_dir)
    ws.create_subgoal_worktree(subgoal_id="subgoal-prot")

    # .jev directory
    with pytest.raises(PermissionError):
        ws.stage_file_mutation(".jev/config.json", '{"runner_profile_id": "pytest"}')

    with pytest.raises(PermissionError):
        ws.stage_file_mutation(".jev/proposals/test.json", "dirty")

    # Engine command and registry files
    with pytest.raises(PermissionError):
        ws.stage_file_mutation("runner_profiles.json", "{}")

    with pytest.raises(PermissionError):
        ws.stage_file_mutation("src/jev/runner_profiles.json", "{}")

    with pytest.raises(PermissionError):
        ws.stage_file_mutation("runner_registry.py", "x = 1")

    with pytest.raises(PermissionError):
        ws.stage_file_mutation("repo_config.py", "x = 1")

    with pytest.raises(PermissionError):
        ws.stage_file_mutation("process_runner.py", "x = 1")

    with pytest.raises(PermissionError):
        ws.stage_file_mutation("src/jev/process_runner.py", "x = 1")


def test_protected_path_in_diff_is_scope_violation(tmp_path):
    repo_dir = _create_git_repo(tmp_path / "repo")
    ws = Workspace(repo_dir=repo_dir)
    ws.create_subgoal_worktree(subgoal_id="subgoal-scope-prot")

    # Manually touch .jev/config.json in git worktree bypassing stage_file_mutation
    cfg_file = ws.worktree_dir / ".jev" / "config.json"
    cfg_file.parent.mkdir(parents=True, exist_ok=True)
    cfg_file.write_text('{"hacked": true}', encoding="utf-8")
    subprocess.run(["git", "add", ".jev/config.json"], cwd=ws.worktree_dir, check=True)

    diff = ws.get_staged_diff()
    scope_res = ws.check_scope(diff=diff, scope=[".jev/config.json", "src/app.py"])
    assert scope_res.passed is False
    assert "Scope violation: protected path '.jev/config.json' cannot be touched" in scope_res.detail

    # Run mechanical checks on this subgoal -> scope check fails
    subgoal = Subgoal(id="subgoal-scope-prot", description="tamper protected", scope=[".jev/config.json"], expects_tests=False)
    mech_res = ws.run_mechanical_checks(subgoal)
    assert mech_res.passed is False
    assert mech_res.failed_check == "scope"


# ==============================================================================
# 2. Command Change Invalidates Approval
# ==============================================================================

def test_command_change_invalidates_approval(tmp_path):
    repo_dir = _create_git_repo(tmp_path / "repo")
    cfg = RepoConfig(
        runner_profile_id="pytest",
        test_command=["pytest", "-q"],
        cwd=".",
    )
    cfg = record_trust(cfg, repo_dir=repo_dir)
    assert cfg.trust is not None
    is_valid, err = validate_trust(cfg, repo_dir=repo_dir)
    assert is_valid is True
    assert err is None

    # Change test command without owner re-approval
    cfg.test_command = ["pytest", "-v", "--capture=no"]
    is_valid, err = validate_trust(cfg, repo_dir=repo_dir)
    assert is_valid is False
    assert "command changed: approval invalidated" in err


def test_untrusted_repo_config_returns_no_command(tmp_path):
    repo_dir = _create_git_repo(tmp_path / "repo")
    cfg = RepoConfig(
        runner_profile_id="pytest",
        test_command=["pytest", "-q"],
        cwd=".",
    )
    cfg = record_trust(cfg, repo_dir=repo_dir)
    # Save valid config
    save_repo_config(cfg, repo_dir)

    # Valid config resolves trusted command
    res = resolve_test_command(repo_dir)
    assert res.source == "trusted_repo_config"
    assert res.command == ["pytest", "-q"]

    # Tamper with test command in .jev/config.json directly
    tampered_data = json.loads((repo_dir / ".jev" / "config.json").read_text(encoding="utf-8"))
    tampered_data["test_command"] = ["pytest", "--tampered-flag"]
    (repo_dir / ".jev" / "config.json").write_text(json.dumps(tampered_data), encoding="utf-8")

    # Now resolving returns untrusted config with command=None
    res_tampered = resolve_test_command(repo_dir)
    assert res_tampered.source == "untrusted_repo_config"
    assert res_tampered.command is None
    assert "approval_invalidated" in res_tampered.reason

    # Workspace evaluate_tests produces UNMEASURED with approval_valid=False
    ws = Workspace(repo_dir=repo_dir)
    ws.create_subgoal_worktree(subgoal_id="sg-untrusted")
    subgoal = Subgoal(id="sg-untrusted", description="test untrusted", scope=["src/app.py"])
    ws.stage_file_mutation("src/app.py", "x = 1\n")
    ev = ws.evaluate_tests(subgoal=subgoal)
    assert ev is not None
    assert ev.mech_status == MechStatus.UNMEASURED
    assert ev.reason == MechReason.NO_COMMAND
    assert ev.approval_valid is False


# ==============================================================================
# 3. Lockfile Change Keeps Approval and Invalidates Comparison
# ==============================================================================

def test_lockfile_classification():
    assert is_lockfile("package-lock.json") is True
    assert is_lockfile("yarn.lock") is True
    assert is_lockfile("pnpm-lock.yaml") is True
    assert is_lockfile("Cargo.lock") is True
    assert is_lockfile("poetry.lock") is True
    assert is_lockfile("sub/package-lock.json") is True

    assert is_lockfile("package.json") is False
    assert is_lockfile("Cargo.toml") is False
    assert is_lockfile("src/app.py") is False


def test_lockfile_change_keeps_approval_and_invalidates_comparison(tmp_path):
    repo_dir = _create_git_repo(tmp_path / "repo")
    (repo_dir / "package.json").write_text('{"name": "test-pkg", "scripts": {"test": "echo test"}}', encoding="utf-8")
    (repo_dir / "package-lock.json").write_text('{"name": "test-pkg", "version": "1.0.0", "lockfileVersion": 3}', encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "Add package files"], cwd=repo_dir, check=True)

    ws = Workspace(repo_dir=repo_dir)
    ws.create_subgoal_worktree(subgoal_id="subgoal-lockfile")

    # Worker stages lockfile-only change
    ws.stage_file_mutation("package-lock.json", '{"name": "test-pkg", "version": "1.0.1", "lockfileVersion": 3}')

    subgoal = Subgoal(id="subgoal-lockfile", description="update lockfile", scope=["package-lock.json"])
    ev = ws.evaluate_tests(subgoal=subgoal)

    assert ev is not None
    assert ev.mech_status == MechStatus.UNMEASURED
    assert ev.reason == MechReason.COMMAND_IDENTITY_CHANGED
    assert ev.approval_valid is True
    assert ev.comparison_valid is False
    assert ev.verifier_modified is False

    # Also test cache miss: lockfile hash in worktree differs from repo_dir
    hash_repo = ws._compute_lockfile_hash(ws.repo_dir)
    hash_wt = ws._compute_lockfile_hash(ws.worktree_dir)
    assert hash_repo != hash_wt


# ==============================================================================
# 4. Config Edit Sets verifier_modified
# ==============================================================================

def test_build_test_config_classification():
    assert is_build_or_test_config("pytest.ini") is True
    assert is_build_or_test_config(".pytest.ini") is True
    assert is_build_or_test_config("setup.cfg") is True
    assert is_build_or_test_config("tox.ini") is True
    assert is_build_or_test_config("jest.config.js") is True
    assert is_build_or_test_config("jest.config.ts") is True
    assert is_build_or_test_config("vitest.config.ts") is True
    assert is_build_or_test_config("Makefile") is True
    assert is_build_or_test_config("Taskfile.yml") is True
    assert is_build_or_test_config("Justfile") is True
    assert is_build_or_test_config("package.json") is True
    assert is_build_or_test_config("pyproject.toml") is True

    assert is_build_or_test_config("src/app.py") is False
    assert is_build_or_test_config("tests/test_feature.py") is False
    assert is_build_or_test_config("package-lock.json") is False


def test_config_edit_sets_verifier_modified(tmp_path):
    repo_dir = _create_git_repo(tmp_path / "repo")
    (repo_dir / "pytest.ini").write_text("[pytest]\naddopts = -q\n", encoding="utf-8")
    (repo_dir / "test_app.py").write_text("def test_ok(): pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "Add pytest files"], cwd=repo_dir, check=True)

    ws = Workspace(repo_dir=repo_dir)
    ws.create_subgoal_worktree(subgoal_id="subgoal-config-mod")

    # Worker edits pytest.ini
    ws.stage_file_mutation("pytest.ini", "[pytest]\naddopts = -v --capture=no\n")

    subgoal = Subgoal(id="subgoal-config-mod", description="tune pytest options", scope=["pytest.ini"])
    ev = ws.evaluate_tests(subgoal=subgoal)

    assert ev is not None
    assert ev.mech_status == MechStatus.UNMEASURED
    assert ev.reason == MechReason.COMMAND_IDENTITY_CHANGED
    assert ev.verifier_modified is True
    assert ev.comparison_valid is False
    assert ev.approval_valid is True


def test_package_scripts_edit_sets_verifier_modified(tmp_path):
    repo_dir = _create_git_repo(tmp_path / "repo")
    (repo_dir / "package.json").write_text('{\n  "scripts": {\n    "test": "node --test"\n  }\n}\n', encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "Add package.json"], cwd=repo_dir, check=True)

    ws = Workspace(repo_dir=repo_dir)
    ws.create_subgoal_worktree(subgoal_id="subgoal-pkg-scripts")

    # Worker edits package.json scripts
    ws.stage_file_mutation("package.json", '{\n  "scripts": {\n    "test": "node --test --bail"\n  }\n}\n')

    subgoal = Subgoal(id="subgoal-pkg-scripts", description="modify test script", scope=["package.json"])
    ev = ws.evaluate_tests(subgoal=subgoal)

    assert ev is not None
    assert ev.mech_status == MechStatus.UNMEASURED
    assert ev.reason == MechReason.COMMAND_IDENTITY_CHANGED
    assert ev.verifier_modified is True
    assert ev.comparison_valid is False


# ==============================================================================
# 5. Trust Binding Integrity & Precedence
# ==============================================================================

def test_trusted_repo_config_precedence(tmp_path):
    repo_dir = _create_git_repo(tmp_path / "repo")
    # Add cargo manifest to test that trusted repo config overrides cargo detection
    (repo_dir / "Cargo.toml").write_text('[package]\nname = "test"\nversion = "0.1.0"\n', encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "Add cargo manifest"], cwd=repo_dir, check=True)

    # Without .jev/config.json, resolves cargo
    res_default = resolve_test_command(repo_dir)
    assert res_default.runner_profile_id == "cargo-test"

    # Add trusted repo config specifying custom runner
    cfg = RepoConfig(
        runner_profile_id="custom-runner",
        test_command=["./custom_test_runner", "--all"],
        cwd=".",
    )
    cfg = record_trust(cfg, repo_dir=repo_dir)
    save_repo_config(cfg, repo_dir)

    # Now trusted repo config takes precedence
    res_trusted = resolve_test_command(repo_dir)
    assert res_trusted.source == "trusted_repo_config"
    assert res_trusted.runner_profile_id == "custom-runner"
    assert res_trusted.command == ["./custom_test_runner", "--all"]


def test_trust_binding_mismatches(tmp_path):
    repo_dir = _create_git_repo(tmp_path / "repo")
    cfg = RepoConfig(
        runner_profile_id="pytest",
        test_command=["pytest"],
        cwd=".",
    )
    cfg = record_trust(cfg, repo_dir=repo_dir)

    # Platform mismatch
    is_valid, err = validate_trust(cfg, repo_dir=repo_dir, platform="fake_os_99")
    assert is_valid is False
    assert "platform mismatch" in err

    # Repo ID mismatch
    other_repo = _create_git_repo(tmp_path / "other_repo", initial_text="# Other Repo Different Content\n")

    is_valid, err = validate_trust(cfg, repo_dir=other_repo)
    assert is_valid is False
    assert "repo_id mismatch" in err

    # Tampered hash
    cfg_tampered = cfg.model_copy(deep=True)
    cfg_tampered.trust.approval_hash = "0" * 64
    is_valid, err = validate_trust(cfg_tampered, repo_dir=repo_dir)
    assert is_valid is False
    assert "approval hash mismatch" in err
