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
    TrustRecord,
    compute_command_hash,
    compute_repo_id,
    get_trust_file_path,
    is_build_or_test_config,
    is_lockfile,
    is_protected_path,
    is_shared_test_config_or_fixture,
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


def _setup_trusted_test_repo(path: Path, test_body: str = "def test_ok(): pass\n") -> tuple[Path, RepoConfig]:
    repo_dir = _create_git_repo(path)
    (repo_dir / "test_suite.py").write_text(test_body, encoding="utf-8")
    cfg = RepoConfig(
        runner_profile_id="pytest",
        test_command=[sys.executable, "-m", "pytest", "-q", "test_suite.py"],
        cwd=".",
    )
    save_repo_config(cfg, repo_dir)
    record_trust(cfg, repo_dir=repo_dir)
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Add trusted test suite"], cwd=repo_dir, check=True, capture_output=True)
    return repo_dir, cfg


@pytest.fixture(autouse=True)
def isolate_jev_home(tmp_path, monkeypatch):
    """Isolate JEV_HOME trust storage for every test."""
    jev_home = tmp_path / ".jev_home"
    monkeypatch.setenv("JEV_HOME", str(jev_home))
    return jev_home


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
# 2. Lockfile & Config Classifications
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
    assert is_build_or_test_config("tsconfig.json") is True
    assert is_build_or_test_config("package.json") is True
    assert is_build_or_test_config("pyproject.toml") is True

    assert is_build_or_test_config("src/app.py") is False
    assert is_build_or_test_config("tests/test_feature.py") is False
    assert is_build_or_test_config("package-lock.json") is False


def test_shared_test_config_or_fixture_classification():
    assert is_shared_test_config_or_fixture("tests/conftest.py") is True
    assert is_shared_test_config_or_fixture("conftest.py") is True
    assert is_shared_test_config_or_fixture("tests/fixtures/data.json") is True
    assert is_shared_test_config_or_fixture("src/fixtures/mock.py") is True
    assert is_shared_test_config_or_fixture("setupTests.ts") is True
    assert is_shared_test_config_or_fixture("test/setupTests.js") is True

    assert is_shared_test_config_or_fixture("tests/test_main.py") is False
    assert is_shared_test_config_or_fixture("src/utils.py") is False


# ==============================================================================
# 3. Rule 1 & Rule 5: Trust Lives Outside the Repo; Embedded Trust Ignored
# ==============================================================================

def test_embedded_trust_is_ignored_and_never_runs(tmp_path):
    """A config that carries its own computed trust is NOT trusted and its command never runs."""
    repo_dir = _create_git_repo(tmp_path / "repo")
    marker_file = repo_dir / "executed.marker"

    # Write .jev/config.json carrying its own embedded trust field
    cfg_file = repo_dir / ".jev" / "config.json"
    cfg_file.parent.mkdir(parents=True, exist_ok=True)
    cfg_file.write_text(
        json.dumps({
            "runner_profile_id": "custom",
            "test_command": [sys.executable, "-c", f"from pathlib import Path; Path('{marker_file.as_posix()}').write_text('ran')"],
            "trust": {
                "repo_id": "fake",
                "command_hash": "deadbeef",
                "approved_by": "attacker",
            }
        }),
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "Add embedded trust"], cwd=repo_dir, check=True)

    # 1. Config loads, embedded trust is ignored and flagged
    cfg = load_repo_config(repo_dir)
    assert cfg is not None
    assert cfg.ignored_embedded_trust is True

    # 2. Resolving test command returns NO_COMMAND
    res = resolve_test_command(repo_dir)
    assert res.source == "untrusted_repo_config"
    assert res.command is None
    assert res.reason == "untrusted_repo_config: run scripts/trust_repo.py"

    # 3. Running evaluate_tests resolves to UNMEASURED(NO_COMMAND)
    ws = Workspace(repo_dir=repo_dir)
    ws.create_subgoal_worktree(subgoal_id="sg-embedded")
    subgoal = Subgoal(id="sg-embedded", description="verify untrusted", scope=["src/app.py"], expects_tests=True)
    ws.stage_file_mutation("src/app.py", "x = 1\n")
    ev = ws.evaluate_tests(subgoal=subgoal)

    assert ev is not None
    assert ev.mech_status == MechStatus.UNMEASURED
    assert ev.reason == MechReason.NO_COMMAND
    assert ev.approval_valid is False

    # 4. Command was never executed
    assert not marker_file.exists()


def test_external_trust_approval_and_command_invalidation(tmp_path):
    """Owner trust written outside repo makes it trusted; any change invalidates it; embedded trust is ignored."""
    repo_dir = _create_git_repo(tmp_path / "repo")
    cfg = RepoConfig(
        runner_profile_id="pytest",
        test_command=[sys.executable, "-c", "import sys; sys.exit(0)"],
        cwd=".",
    )
    # Save declarations to .jev/config.json
    save_repo_config(cfg, repo_dir)

    # Inject embedded trust into .jev/config.json to ensure it is ignored
    raw_cfg = json.loads((repo_dir / ".jev" / "config.json").read_text(encoding="utf-8"))
    raw_cfg["trust"] = {"bogus": "embedded"}
    (repo_dir / ".jev" / "config.json").write_text(json.dumps(raw_cfg), encoding="utf-8")

    # Record genuine owner trust in external trust store under JEV_HOME
    record = record_trust(cfg, repo_dir=repo_dir, approved_by="owner")
    repo_id = compute_repo_id(repo_dir)
    trust_path = get_trust_file_path(repo_id)
    assert trust_path.exists()
    assert str(trust_path).startswith(str(Path(os.environ["JEV_HOME"])))

    # Config is now trusted
    loaded = load_repo_config(repo_dir)
    assert loaded.ignored_embedded_trust is True
    is_valid, err = validate_trust(loaded, repo_dir=repo_dir)
    assert is_valid is True
    assert err is None

    res = resolve_test_command(repo_dir)
    assert res.source == "trusted_repo_config"
    assert res.command == [sys.executable, "-c", "import sys; sys.exit(0)"]

    # Tampering with test command invalidates external trust
    loaded.test_command = [sys.executable, "-c", "import sys; sys.exit(1)"]
    is_valid, err = validate_trust(loaded, repo_dir=repo_dir)
    assert is_valid is False
    assert "command changed: approval invalidated" in err

    # Tampering on disk
    raw_cfg["test_command"] = [sys.executable, "-c", "import sys; sys.exit(1)"]
    (repo_dir / ".jev" / "config.json").write_text(json.dumps(raw_cfg), encoding="utf-8")
    res_tampered = resolve_test_command(repo_dir)
    assert res_tampered.source == "untrusted_repo_config"
    assert res_tampered.command is None
    assert res_tampered.reason == "untrusted_repo_config: run scripts/trust_repo.py"


def test_trust_file_lives_under_jev_home_never_in_repo(tmp_path):
    """The trust file lives under JEV_HOME, never inside the repo."""
    repo_dir = _create_git_repo(tmp_path / "repo")
    cfg = RepoConfig(
        runner_profile_id="pytest",
        test_command=["pytest", "-q"],
        cwd=".",
    )
    save_repo_config(cfg, repo_dir)
    record = record_trust(cfg, repo_dir=repo_dir)

    repo_id = compute_repo_id(repo_dir)
    trust_file = get_trust_file_path(repo_id)

    # Must exist under JEV_HOME
    assert trust_file.exists()
    assert Path(os.environ["JEV_HOME"]) in trust_file.parents

    # Must NEVER exist inside repo
    assert not (repo_dir / ".jev" / "trust").exists()
    assert not (repo_dir / "trust").exists()
    assert not (repo_dir / f"{repo_id}.json").exists()

    # Saved repo config must have no trust field
    cfg_data = json.loads((repo_dir / ".jev" / "config.json").read_text(encoding="utf-8"))
    assert "trust" not in cfg_data


# ==============================================================================
# 4. Rule 3, 4, 5: Structural Config Parsing & Pass Preservation
# ==============================================================================

def test_pyproject_version_bump_green_passes_unflagged(tmp_path):
    """Source change plus pyproject version bump with green tests -> PASS, verifier_modified false."""
    repo_dir = _create_git_repo(tmp_path / "repo")
    (repo_dir / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "1.0.0"\n\n[tool.pytest.ini_options]\naddopts = "-q"\n',
        encoding="utf-8",
    )
    (repo_dir / "test_app.py").write_text("def test_ok(): pass\n", encoding="utf-8")
    (repo_dir / "app.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "Initial pyproject"], cwd=repo_dir, check=True)

    ws = Workspace(repo_dir=repo_dir)
    ws.create_subgoal_worktree(subgoal_id="sg-pyproj-bump")

    # Worker bumps version in pyproject.toml and modifies app.py
    ws.stage_file_mutation(
        "pyproject.toml",
        '[project]\nname = "demo"\nversion = "1.0.1"\n\n[tool.pytest.ini_options]\naddopts = "-q"\n',
    )
    ws.stage_file_mutation("app.py", "x = 2\n")

    subgoal = Subgoal(id="sg-pyproj-bump", description="bump version", scope=["pyproject.toml", "app.py"])
    ev = ws.evaluate_tests(subgoal=subgoal)

    assert ev is not None
    assert ev.mech_status == MechStatus.PASS
    assert ev.verifier_modified is False
    assert ev.comparison_valid is True


def test_package_scripts_edit_green_passes_flagged(tmp_path):
    """Package.json scripts edit with green tests -> PASS, verifier_modified true, mechanically_unverified."""
    repo_dir, cfg = _setup_trusted_test_repo(tmp_path / "repo_pkg_green", "def test_ok(): pass\n")
    (repo_dir / "package.json").write_text(
        '{\n  "scripts": {\n    "build": "echo 1"\n  }\n}\n',
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "Add package.json"], cwd=repo_dir, check=True)

    ws = Workspace(repo_dir=repo_dir)
    ws.create_subgoal_worktree(subgoal_id="sg-pkg-pass")

    # Worker edits package.json scripts
    ws.stage_file_mutation(
        "package.json",
        '{\n  "scripts": {\n    "build": "echo 2",\n    "test": "custom-runner"\n  }\n}\n',
    )

    subgoal = Subgoal(id="sg-pkg-pass", description="modify scripts", scope=["package.json"])
    ev = ws.evaluate_tests(subgoal=subgoal)

    assert ev is not None
    assert ev.mech_status == MechStatus.PASS
    assert ev.verifier_modified is True
    assert ev.mechanically_unverified is True
    assert ev.unverified_kind == "verifier_modified"
    assert ev.comparison_valid is False
    assert ev.approval_valid is True


def test_package_scripts_edit_failing_unmeasured_command_identity_changed(tmp_path):
    """The same edit with a failing test -> UNMEASURED(COMMAND_IDENTITY_CHANGED)."""
    repo_dir, cfg = _setup_trusted_test_repo(tmp_path / "repo_pkg_fail", "def test_fail(): assert False\n")
    (repo_dir / "package.json").write_text(
        '{\n  "scripts": {\n    "build": "echo 1"\n  }\n}\n',
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "Add package.json"], cwd=repo_dir, check=True)

    ws = Workspace(repo_dir=repo_dir)
    ws.create_subgoal_worktree(subgoal_id="sg-pkg-fail")

    # Worker edits package.json scripts
    ws.stage_file_mutation(
        "package.json",
        '{\n  "scripts": {\n    "build": "echo 2"\n  }\n}\n',
    )

    subgoal = Subgoal(id="sg-pkg-fail", description="break test script", scope=["package.json"])
    ev = ws.evaluate_tests(subgoal=subgoal)

    assert ev is not None
    assert ev.mech_status == MechStatus.UNMEASURED
    assert ev.reason == MechReason.COMMAND_IDENTITY_CHANGED
    assert ev.verifier_modified is True
    assert ev.comparison_valid is False
    assert ev.approval_valid is True


def test_dependency_only_package_json_edit_green_passes(tmp_path):
    """Dependency-only package.json edit, green -> PASS."""
    repo_dir, cfg = _setup_trusted_test_repo(tmp_path / "repo_pkg_deps", "def test_ok(): pass\n")
    (repo_dir / "package.json").write_text(
        '{\n  "dependencies": {\n    "dep-a": "1.0.0"\n  }\n}\n',
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "Add package.json with deps"], cwd=repo_dir, check=True)

    ws = Workspace(repo_dir=repo_dir)
    ws.create_subgoal_worktree(subgoal_id="sg-pkg-deps")

    # Worker edits only dependencies in package.json
    ws.stage_file_mutation(
        "package.json",
        '{\n  "dependencies": {\n    "dep-a": "1.0.1",\n    "dep-b": "2.0.0"\n  }\n}\n',
    )

    subgoal = Subgoal(id="sg-pkg-deps", description="update dependencies", scope=["package.json"])
    ev = ws.evaluate_tests(subgoal=subgoal)

    assert ev is not None
    assert ev.mech_status == MechStatus.PASS
    assert ev.verifier_modified is False
    assert ev.comparison_valid is True


def test_lockfile_only_green_passes_and_failing_baseline_unavailable(tmp_path):
    """Lockfile-only change, green -> PASS; failing -> UNMEASURED with comparison_valid false."""
    # Part 1: Green tests -> PASS
    repo_dir_green, _ = _setup_trusted_test_repo(tmp_path / "repo_lock_green", "def test_ok(): pass\n")
    (repo_dir_green / "package-lock.json").write_text('{"name": "test", "version": "1.0.0"}', encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir_green, check=True)
    subprocess.run(["git", "commit", "-m", "Initial lockfile"], cwd=repo_dir_green, check=True)

    ws_green = Workspace(repo_dir=repo_dir_green)
    ws_green.create_subgoal_worktree(subgoal_id="sg-lock-green")
    ws_green.stage_file_mutation("package-lock.json", '{"name": "test", "version": "1.0.1"}')

    subgoal_green = Subgoal(id="sg-lock-green", description="bump lock", scope=["package-lock.json"])
    ev_green = ws_green.evaluate_tests(subgoal=subgoal_green)

    assert ev_green is not None
    assert ev_green.mech_status == MechStatus.PASS
    assert ev_green.verifier_modified is False
    assert ev_green.lockfile_changed is True
    assert ev_green.mechanically_unverified is True
    assert ev_green.unverified_kind == "verifier_modified"
    assert ev_green.comparison_valid is False

    # Part 2: Failing tests -> UNMEASURED(BASELINE_UNAVAILABLE) with comparison_valid=False, approval_valid unchanged
    repo_dir_fail, _ = _setup_trusted_test_repo(tmp_path / "repo_lock_fail", "def test_fail(): assert False\n")
    (repo_dir_fail / "package-lock.json").write_text('{"name": "test", "version": "1.0.0"}', encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir_fail, check=True)
    subprocess.run(["git", "commit", "-m", "Initial lockfile fail"], cwd=repo_dir_fail, check=True)

    ws_fail = Workspace(repo_dir=repo_dir_fail)
    ws_fail.create_subgoal_worktree(subgoal_id="sg-lock-fail")
    ws_fail.stage_file_mutation("package-lock.json", '{"name": "test", "version": "1.0.1"}')

    subgoal_fail = Subgoal(id="sg-lock-fail", description="bump lock fail", scope=["package-lock.json"])
    ev_fail = ws_fail.evaluate_tests(subgoal=subgoal_fail)

    assert ev_fail is not None
    assert ev_fail.mech_status == MechStatus.UNMEASURED
    assert ev_fail.reason == MechReason.BASELINE_UNAVAILABLE
    assert ev_fail.comparison_valid is False
    assert ev_fail.approval_valid is True
    assert ev_fail.lockfile_changed is True
    assert ev_fail.verifier_modified is False


def test_tsconfig_edit_green_passes_flagged(tmp_path):
    """Tsconfig edit, green -> PASS flagged."""
    repo_dir, _ = _setup_trusted_test_repo(tmp_path / "repo_tsconfig", "def test_ok(): pass\n")
    (repo_dir / "tsconfig.json").write_text('{"compilerOptions": {"strict": true}}', encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "Initial tsconfig"], cwd=repo_dir, check=True)

    ws = Workspace(repo_dir=repo_dir)
    ws.create_subgoal_worktree(subgoal_id="sg-tsconfig")

    # Worker edits tsconfig.json
    ws.stage_file_mutation("tsconfig.json", '{"compilerOptions": {"strict": false}}')

    subgoal = Subgoal(id="sg-tsconfig", description="loosen tsconfig", scope=["tsconfig.json"])
    ev = ws.evaluate_tests(subgoal=subgoal)

    assert ev is not None
    assert ev.mech_status == MechStatus.PASS
    assert ev.verifier_modified is True
    assert ev.mechanically_unverified is True
    assert ev.unverified_kind == "verifier_modified"
    assert ev.comparison_valid is False


def test_shared_test_fixture_edit_failing_unmeasured_test_surface_changed(tmp_path):
    """Shared test fixture edit with failing tests -> UNMEASURED(TEST_SURFACE_CHANGED)."""
    repo_dir, _ = _setup_trusted_test_repo(tmp_path / "repo_fixture", "def test_fail(): assert False\n")
    fixtures_dir = repo_dir / "tests" / "fixtures"
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    (fixtures_dir / "base_data.json").write_text('{"v": 1}', encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "Initial fixture"], cwd=repo_dir, check=True)

    ws = Workspace(repo_dir=repo_dir)
    ws.create_subgoal_worktree(subgoal_id="sg-fixture")
    ws.stage_file_mutation("tests/fixtures/base_data.json", '{"v": 2}')

    subgoal = Subgoal(id="sg-fixture", description="edit fixture", scope=["tests/fixtures/base_data.json"])
    ev = ws.evaluate_tests(subgoal=subgoal)

    assert ev is not None
    assert ev.mech_status == MechStatus.UNMEASURED
    assert ev.reason == MechReason.TEST_SURFACE_CHANGED
    assert ev.test_surface_changed is True
    assert ev.verifier_modified is False
    assert ev.comparison_valid is False
    assert ev.approval_valid is True


def test_trust_repo_script_cli(tmp_path):
    """scripts/trust_repo.py CLI approves and records trust without running test command."""
    repo_dir = _create_git_repo(tmp_path / "repo")
    marker_file = repo_dir / "should_not_exist.txt"
    cfg = RepoConfig(
        runner_profile_id="pytest",
        test_command=[sys.executable, "-c", f"open('{marker_file.as_posix()}', 'w').write('ran')"],
        cwd=".",
    )
    save_repo_config(cfg, repo_dir)

    script_path = Path(__file__).resolve().parent.parent / "scripts" / "trust_repo.py"
    res = subprocess.run(
        [sys.executable, str(script_path), str(repo_dir), "--yes"],
        capture_output=True,
        text=True,
        check=True,
        env=os.environ,
    )

    # Trust record created
    repo_id = compute_repo_id(repo_dir)
    trust_file = get_trust_file_path(repo_id)
    assert trust_file.exists()

    # Command NEVER ran
    assert not marker_file.exists()
