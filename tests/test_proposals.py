"""tests/test_proposals.py

Tests for Phase 3B: Command proposals and the approval interface.
Verifies that:
1. Proposals never execute without approval.
2. Approval screen data is complete, formatted (one arg per line), and secrets are redacted.
3. The disclaimer "approval authorizes execution, not a mechanical pass" is enforced.
4. Approval writes trusted config into .jev/config.json and records external trust under JEV_HOME.
5. Approval survives an unchanged run.
6. Any identity change or tampering invalidates the proposal and approval.
7. Proposals are generated from Makefile, CI workflows, manifests, and README.
8. CLI scripts/approve.py operates correctly in interactive and non-interactive (--yes) modes.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from jev.models import Subgoal, TestOutcome
from jev.mech_evidence import MechReason, MechStatus
from jev.proposals import (
    APPROVAL_DISCLAIMER,
    Proposal,
    approve_proposal,
    build_proposal_from_repo,
    compute_proposal_identity_hash,
    format_proposal_screen,
    get_proposals_dir,
    inspect_executable,
    list_proposals,
    load_proposal,
    redact_data_structure,
    redact_secrets_in_env,
    redact_secrets_in_text,
    save_proposal,
)
from jev.repo_config import (
    RepoConfig,
    compute_repo_id,
    get_trust_file_path,
    load_repo_config,
    save_repo_config,
    validate_trust,
)
from jev.workspace import Workspace, resolve_test_command


def _create_git_repo(path: Path, initial_text: str = "# Unknown Repo\n") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=path, check=True, capture_output=True)
    (path / "README.md").write_text(initial_text, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=path, check=True, capture_output=True)
    return path


@pytest.fixture(autouse=True)
def isolate_jev_home(tmp_path, monkeypatch):
    """Isolate JEV_HOME trust storage for every test."""
    jev_home = tmp_path / ".jev_home"
    monkeypatch.setenv("JEV_HOME", str(jev_home))
    return jev_home


# ==============================================================================
# 1. Proposals Never Execute Without Approval
# ==============================================================================

def test_proposals_never_execute_without_approval(tmp_path, monkeypatch):
    """Proposals never execute without approval; unapproved commands never run on host."""
    monkeypatch.setenv("JEV_HOME", str(tmp_path / ".jev_home"))
    repo = _create_git_repo(tmp_path / "unknown_repo")
    marker_file = repo / "executed.marker"

    # Create a proposal with a command that would create a marker file if executed
    malicious_cmd = [
        sys.executable,
        "-c",
        f"from pathlib import Path; Path('{marker_file.as_posix()}').write_text('ran')",
    ]
    repo_id = compute_repo_id(repo)
    ident_hash = compute_proposal_identity_hash(repo_id, malicious_cmd)
    prop = Proposal(
        id=f"prop-{ident_hash[:12]}",
        repo_id=repo_id,
        exact_argv=malicious_cmd,
        cwd=".",
        source_evidence="Makefile target 'test'",
        platform=sys.platform,
        identity_hash=ident_hash,
        created_at="2026-10-30T00:00:00Z",
    )
    save_proposal(prop, repo)

    # 1. Pure detection does NOT execute the command
    res = resolve_test_command(repo)
    assert res.command is None
    assert not marker_file.exists()

    # 2. Evaluation / run_tests never executes unapproved proposal
    ws = Workspace(repo_dir=repo)
    outcome = ws.run_tests()
    assert outcome == TestOutcome.NO_TESTS_COLLECTED
    assert not marker_file.exists()

    # 3. If an untrusted config is present, evaluation returns UNMEASURED(NO_COMMAND)
    cfg = RepoConfig(runner_profile_id="custom", test_command=malicious_cmd)
    save_repo_config(cfg, repo)
    ws.create_subgoal_worktree(subgoal_id="sg-prop-unapproved")
    ws.stage_file_mutation("app.py", "x = 1\n")
    subgoal = Subgoal(id="sg-prop-unapproved", description="test unapproved proposal", scope=["app.py"])
    ev = ws.evaluate_tests(subgoal=subgoal)
    assert ev is not None
    assert ev.mech_status == MechStatus.UNMEASURED
    assert ev.reason == MechReason.NO_COMMAND

    # 4. In no circumstance did the unapproved command execute
    assert not marker_file.exists()


# ==============================================================================
# 2. Approval Screen Data Complete, One Arg Per Line, and Redacted
# ==============================================================================

def test_approval_screen_data_complete_and_redacted(tmp_path):
    """Approval screen displays all required fields, one arg per line, disclaimer, and redacts secrets."""
    repo = _create_git_repo(tmp_path / "secret_repo")
    repo_id = compute_repo_id(repo)

    # Command containing sensitive credentials
    secret_token = "ghp_superSecretToken1234567890"
    secret_key = "sk-ant-secretKey9876543210fedcba"
    bearer_token = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.dummysecret12345"
    password_val = "MySuperSecretPasswd123!"

    argv = [
        "python",
        "-m",
        "custom_runner",
        f"--token={secret_token}",
        f"--auth={bearer_token}",
    ]
    env = {
        "API_KEY": secret_key,
        "DATABASE_PASSWORD": password_val,
        "SAFE_ENV_VAR": "production_cluster_1",
    }
    source_ev = f"CI workflow line: run tests with {secret_token}"
    expanded_script = f"jest --auth={secret_key}"

    ident_hash = compute_proposal_identity_hash(repo_id, argv, cwd=".", platform=sys.platform, env=env)
    prop = Proposal(
        id=f"prop-{ident_hash[:12]}",
        repo_id=repo_id,
        exact_argv=argv,
        cwd=".",
        source_evidence=source_ev,
        expanded_package_script=expanded_script,
        tool_path="/usr/bin/python3",
        tool_version="Python 3.11.2",
        platform=sys.platform,
        target_rule="all_units",
        scope=["tests/"],
        environment_variables=env,
        network_assumptions="offline-only",
        timeout=300,
        generated_paths=["coverage.xml"],
        identity_hash=ident_hash,
        created_at="2026-10-30T00:00:00Z",
    )
    save_proposal(prop, repo)

    screen = format_proposal_screen(prop)

    # 1. Verify all required fields are present
    assert "Proposal ID:" in screen
    assert "Repository ID:" in screen
    assert "Platform:" in screen
    assert "Working Directory:" in screen
    assert "Source Evidence:" in screen
    assert "Expanded Script:" in screen
    assert "Tool Path/Version:" in screen
    assert "Target Rule:" in screen
    assert "Scope:" in screen
    assert "Environment Variables:" in screen
    assert "Network Assumptions:" in screen
    assert "Timeout:" in screen
    assert "Generated Paths:" in screen
    assert "Identity Hash:" in screen

    # 2. Verify exact argv is formatted ONE ARGUMENT PER LINE
    assert "[0] python" in screen
    assert "[1] -m" in screen
    assert "[2] custom_runner" in screen

    # 3. Mandatory disclaimer statement
    assert APPROVAL_DISCLAIMER in screen
    assert "approval authorizes execution, not a mechanical pass" in screen

    # 4. Redaction of secrets: raw secrets must NEVER appear in screen output
    assert secret_token not in screen
    assert secret_key not in screen
    assert password_val not in screen
    assert "[REDACTED]" in screen

    # Non-secret items are retained
    assert "production_cluster_1" in screen
    assert "custom_runner" in screen


# ==============================================================================
# 3. Approval Writes Trusted Config and External Trust
# ==============================================================================

def test_approval_writes_trusted_config_and_enables_execution(tmp_path, monkeypatch):
    """Approval writes .jev/config.json and records external trust under JEV_HOME, enabling execution."""
    jev_home = tmp_path / ".jev_home"
    monkeypatch.setenv("JEV_HOME", str(jev_home))
    repo = _create_git_repo(tmp_path / "app_repo")
    repo_id = compute_repo_id(repo)

    test_cmd = [sys.executable, "-c", "import sys; sys.exit(0)"]
    ident_hash = compute_proposal_identity_hash(repo_id, test_cmd)
    prop = Proposal(
        id=f"prop-{ident_hash[:12]}",
        repo_id=repo_id,
        exact_argv=test_cmd,
        cwd=".",
        source_evidence="Makefile target 'test'",
        platform=sys.platform,
        identity_hash=ident_hash,
        created_at="2026-10-30T00:00:00Z",
    )
    save_proposal(prop, repo)

    # Approve the proposal
    cfg, trust_rec = approve_proposal(prop, repo, approved_by="owner")

    # 1. .jev/config.json is written
    cfg_file = repo / ".jev" / "config.json"
    assert cfg_file.exists()
    saved_cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert saved_cfg["test_command"] == test_cmd

    # 2. External trust record exists under JEV_HOME
    trust_path = get_trust_file_path(repo_id)
    assert trust_path.exists()
    assert str(trust_path).startswith(str(jev_home))

    # 3. Now resolve_test_command returns trusted_repo_config
    res = resolve_test_command(repo)
    assert res.source == "trusted_repo_config"
    assert res.command == test_cmd

    # 4. Proposal status is marked approved
    reloaded = load_proposal(prop.id, repo)
    assert reloaded is not None
    assert reloaded.status == "approved"
    assert reloaded.approved_by == "owner"


# ==============================================================================
# 4. Approval Survives an Unchanged Run
# ==============================================================================

def test_approval_survives_unchanged_run(tmp_path, monkeypatch):
    """Approval survives subsequent runs on unchanged repository configuration."""
    monkeypatch.setenv("JEV_HOME", str(tmp_path / ".jev_home"))
    repo = _create_git_repo(tmp_path / "survive_repo")
    repo_id = compute_repo_id(repo)

    test_cmd = [sys.executable, "-c", "import sys; sys.exit(0)"]
    ident_hash = compute_proposal_identity_hash(repo_id, test_cmd)
    prop = Proposal(
        id=f"prop-{ident_hash[:12]}",
        repo_id=repo_id,
        exact_argv=test_cmd,
        cwd=".",
        source_evidence="CI workflow step",
        platform=sys.platform,
        identity_hash=ident_hash,
        created_at="2026-10-30T00:00:00Z",
    )
    save_proposal(prop, repo)
    approve_proposal(prop, repo, approved_by="admin")

    # First resolution
    res1 = resolve_test_command(repo)
    assert res1.source == "trusted_repo_config"
    assert res1.command == test_cmd

    # Simulate subsequent run/worktree checks
    cfg = load_repo_config(repo)
    is_valid, err = validate_trust(cfg, repo)
    assert is_valid is True
    assert err is None

    res2 = resolve_test_command(repo)
    assert res2.source == "trusted_repo_config"
    assert res2.command == test_cmd


# ==============================================================================
# 5. Identity Change Invalidates Proposal and Approval
# ==============================================================================

def test_identity_change_invalidates_proposal_and_approval(tmp_path, monkeypatch):
    """Any identity change invalidates proposal verification and approval."""
    monkeypatch.setenv("JEV_HOME", str(tmp_path / ".jev_home"))
    repo = _create_git_repo(tmp_path / "tamper_repo")
    repo_id = compute_repo_id(repo)

    original_cmd = ["make", "test"]
    ident_hash = compute_proposal_identity_hash(repo_id, original_cmd)
    prop = Proposal(
        id=f"prop-{ident_hash[:12]}",
        repo_id=repo_id,
        exact_argv=original_cmd,
        cwd=".",
        source_evidence="Makefile",
        platform=sys.platform,
        identity_hash=ident_hash,
        created_at="2026-10-30T00:00:00Z",
    )
    save_proposal(prop, repo)

    # 1. Tamper with proposal's exact_argv on disk
    prop_file = repo / ".jev" / "proposals" / f"{prop.id}.json"
    data = json.loads(prop_file.read_text(encoding="utf-8"))
    data["exact_argv"] = ["rm", "-rf", "/"]
    prop_file.write_text(json.dumps(data), encoding="utf-8")

    tampered_prop = load_proposal(prop.id, repo)
    assert tampered_prop is not None
    is_valid, err = tampered_prop.verify_identity()
    assert is_valid is False
    assert "identity_hash mismatch" in err

    # Approving tampered proposal raises ValueError
    with pytest.raises(ValueError, match="Cannot approve proposal: identity_hash mismatch"):
        approve_proposal(tampered_prop, repo)

    # 2. Tampering with trusted config after approval invalidates execution
    # Restore valid proposal and approve
    valid_prop = Proposal(
        id=f"prop-{ident_hash[:12]}",
        repo_id=repo_id,
        exact_argv=original_cmd,
        cwd=".",
        source_evidence="Makefile",
        platform=sys.platform,
        identity_hash=ident_hash,
        created_at="2026-10-30T00:00:00Z",
    )
    save_proposal(valid_prop, repo)
    approve_proposal(valid_prop, repo)

    # Tamper with test_command in .jev/config.json
    cfg_data = json.loads((repo / ".jev" / "config.json").read_text(encoding="utf-8"))
    cfg_data["test_command"] = ["malicious_binary"]
    (repo / ".jev" / "config.json").write_text(json.dumps(cfg_data), encoding="utf-8")

    res_tampered = resolve_test_command(repo)
    assert res_tampered.source == "untrusted_repo_config"
    assert res_tampered.command is None


# ==============================================================================
# 6. Proposal Generation from Makefile, CI, and README
# ==============================================================================

def test_proposal_generation_from_makefile(tmp_path):
    """Proposals are extracted from Makefile test target."""
    repo = _create_git_repo(tmp_path / "makefile_repo")
    (repo / "Makefile").write_text("all:\n\techo 1\n\ntest:\n\tpytest\n", encoding="utf-8")

    prop = build_proposal_from_repo(repo)
    assert prop is not None
    assert prop.exact_argv == ["make", "test"]
    assert "Makefile target 'test'" in prop.source_evidence

    # Saved to .jev/proposals/<id>.json
    assert (repo / ".jev" / "proposals" / f"{prop.id}.json").exists()


def test_proposal_generation_from_ci_workflow(tmp_path):
    """Proposals are extracted from GitHub Actions workflow file."""
    repo = _create_git_repo(tmp_path / "ci_repo")
    wf_dir = repo / ".github" / "workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    (wf_dir / "test.yml").write_text(
        "name: CI\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - run: pytest -v tests/\n",
        encoding="utf-8",
    )

    prop = build_proposal_from_repo(repo)
    assert prop is not None
    assert prop.exact_argv == ["pytest", "-v", "tests/"]
    assert "test.yml" in prop.source_evidence


def test_proposal_generation_from_readme(tmp_path):
    """Proposals are extracted from README testing instructions."""
    repo = _create_git_repo(tmp_path / "readme_repo")
    (repo / "README.md").write_text(
        "# My Project\n\n## Running Tests\n\n```bash\npytest -q\n```\n",
        encoding="utf-8",
    )

    prop = build_proposal_from_repo(repo)
    assert prop is not None
    assert prop.exact_argv == ["pytest", "-q"]
    assert "README.md code block" in prop.source_evidence


# ==============================================================================
# 7. CLI scripts/approve.py End-to-End
# ==============================================================================

def test_cli_approve_script_interactive_and_yes(tmp_path, monkeypatch):
    """scripts/approve.py CLI displays required fields and approves proposal with --yes."""
    jev_home = tmp_path / ".jev_home"
    monkeypatch.setenv("JEV_HOME", str(jev_home))
    test_env = {**os.environ, "JEV_HOME": str(jev_home)}
    repo = _create_git_repo(tmp_path / "cli_repo")
    (repo / "Makefile").write_text("test:\n\techo testing\n", encoding="utf-8")

    prop = build_proposal_from_repo(repo)
    assert prop is not None

    script_path = Path(__file__).resolve().parent.parent / "scripts" / "approve.py"

    # Run with --yes
    res = subprocess.run(
        [sys.executable, str(script_path), prop.id, "--repo-dir", str(repo), "--yes"],
        capture_output=True,
        text=True,
        check=True,
        env=test_env,
    )
    assert "[SUCCESS]" in res.stdout
    assert APPROVAL_DISCLAIMER in res.stdout
    assert "approval authorizes execution, not a mechanical pass" in res.stdout

    # Verify repository is now configured and trusted
    res_cmd = resolve_test_command(repo)
    assert res_cmd.source == "trusted_repo_config"
    assert res_cmd.command == ["make", "test"]

    # Re-running approve on already-approved proposal notifies user
    res_repeat = subprocess.run(
        [sys.executable, str(script_path), prop.id, "--repo-dir", str(repo), "--yes"],
        capture_output=True,
        text=True,
        check=True,
        env=test_env,
    )
    assert "already approved" in res_repeat.stdout


def test_proposal_creation_never_starts_subprocess(tmp_path):
    """Building a proposal from a repo whose CI names a program never starts any subprocess."""
    repo = _create_git_repo(tmp_path / "ci_no_subprocess_repo")
    wf_dir = repo / ".github" / "workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    (wf_dir / "ci.yml").write_text(
        "name: CI\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - run: python -m pytest tests/\n",
        encoding="utf-8",
    )

    with patch("subprocess.run") as mock_run, patch("subprocess.Popen") as mock_popen:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "abc123rootcommit\n"
        prop = build_proposal_from_repo(repo)
        assert prop is not None
        assert prop.exact_argv == ["python", "-m", "pytest", "tests/"]
        assert prop.tool_version is None
        # Assert that the proposed tool was never executed to inspect its version
        for call_args in mock_run.call_args_list:
            args = call_args[0][0]
            assert "--version" not in args
            assert "python" not in args
        mock_popen.assert_not_called()

    # And verify inspect_executable never starts any subprocess
    with patch("subprocess.run") as mock_run:
        inspect_executable(["python", "-m", "pytest"])
        mock_run.assert_not_called()


def test_secrets_in_exact_argv_refuses_save_and_approval_and_writes_no_secrets(tmp_path, monkeypatch):
    """Proposal with secrets in exact_argv refuses save and approval, writing nothing to repo or JEV_HOME."""
    jev_home = tmp_path / "jev_home"
    monkeypatch.setenv("JEV_HOME", str(jev_home))
    repo = _create_git_repo(tmp_path / "secret_cmd_repo")
    repo_id = compute_repo_id(repo)

    secret_token = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    cmd = ["./run-tests.sh", f"--token={secret_token}"]

    ident_hash = compute_proposal_identity_hash(repo_id, cmd, cwd=".", platform=sys.platform)
    prop = Proposal(
        id=f"prop-{ident_hash[:12]}",
        repo_id=repo_id,
        exact_argv=cmd,
        cwd=".",
        source_evidence="CI workflow line",
        platform=sys.platform,
        identity_hash=ident_hash,
        created_at="2026-11-02T00:00:00Z",
    )

    # 1. save_proposal refuses to save and writes no proposal file
    saved = save_proposal(prop, repo)
    assert saved is None
    proposals_dir = repo / ".jev" / "proposals"
    assert not list(proposals_dir.glob("*.json"))

    # Calling save_proposal with raise_on_secret=True raises ValueError
    with pytest.raises(ValueError, match="exact_argv contains secret"):
        save_proposal(prop, repo, raise_on_secret=True)

    # 2. approve_proposal refuses to approve
    with pytest.raises(ValueError, match="exact_argv contains secret"):
        approve_proposal(prop, repo, approved_by="owner")

    # 3. .jev/config.json is never written
    assert not (repo / ".jev" / "config.json").exists()

    # 4. No file under repo or JEV_HOME contains the token
    for root_dir in (repo, jev_home):
        if not root_dir.exists():
            continue
        for file_path in root_dir.rglob("*"):
            if file_path.is_file():
                content = file_path.read_text(encoding="utf-8", errors="ignore")
                assert secret_token not in content, f"Secret token leaked in {file_path}"


def test_save_proposal_redacts_stored_fields_and_env_vars(tmp_path):
    """save_proposal redacts secrets in all stored text fields and env vars, maintaining identity hash validity."""
    repo = _create_git_repo(tmp_path / "redact_storage_repo")
    repo_id = compute_repo_id(repo)

    secret_token = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    secret_key = "sk-1234567890abcdef1234567890abcdef"
    bearer_val = "Bearer 12345678901234567890abcdef"

    cmd = ["pytest", "tests/"]
    env = {
        "API_KEY": secret_key,
        "SAFE_ENV_VAR": "safe_value",
    }
    source_ev = f"CI run with {secret_token}"
    expanded_script = f"test runner with {bearer_val}"
    scope = [f"tests/{secret_token}"]

    ident_hash = compute_proposal_identity_hash(repo_id, cmd, cwd=".", platform=sys.platform, env=env)
    prop = Proposal(
        id=f"prop-{ident_hash[:12]}",
        repo_id=repo_id,
        exact_argv=cmd,
        cwd=".",
        source_evidence=source_ev,
        expanded_package_script=expanded_script,
        tool_path="/usr/bin/pytest",
        platform=sys.platform,
        target_rule="full_suite",
        scope=scope,
        environment_variables=env,
        identity_hash=ident_hash,
        created_at="2026-11-02T00:00:00Z",
    )

    saved_file = save_proposal(prop, repo)
    assert saved_file is not None
    assert saved_file.exists()

    # Verify no secret tokens are stored unredacted in the proposal JSON file
    file_text = saved_file.read_text(encoding="utf-8")
    assert secret_token not in file_text
    assert secret_key not in file_text
    assert bearer_val not in file_text
    assert "[REDACTED]" in file_text

    # Loading the proposal preserves identity hash validity
    loaded = load_proposal(prop.id, repo)
    assert loaded is not None
    is_valid, err = loaded.verify_identity()
    assert is_valid, f"Proposal identity verification failed: {err}"
    assert loaded.environment_variables["API_KEY"] == "[REDACTED]"
    assert loaded.environment_variables["SAFE_ENV_VAR"] == "safe_value"


# ==============================================================================
# 9. Fix 6: Show and Bind Recipe, Makefile Exact Targets, CI Parsing, Win Make
# ==============================================================================

def test_makefile_latest_before_test_proposes_make_test(tmp_path):
    """Makefile with latest: before test: proposes make test, never make latest."""
    repo = _create_git_repo(tmp_path / "repo_latest_makefile")
    mf_content = (
        "latest:\n"
        "\techo 'building latest'\n\n"
        "test:\n"
        "\tpytest -v\n\tflake8 src/\n\n"
        "all: test\n"
    )
    (repo / "Makefile").write_text(mf_content, encoding="utf-8")
    prop = build_proposal_from_repo(repo)
    assert prop is not None
    assert prop.exact_argv == ["make", "test"]
    assert "test" in prop.source_evidence
    assert prop.recipe_lines == ["\tpytest -v", "\tflake8 src/"]
    assert prop.recipe_text == "\tpytest -v\n\tflake8 src/"


def test_makefile_recipe_shown_on_approval_screen(tmp_path):
    """Approval screen displays the extracted recipe lines under the chosen target."""
    repo = _create_git_repo(tmp_path / "repo_recipe_screen")
    mf_content = (
        "test:\n"
        "\tpytest --verbose tests/\n"
        "\tpython -m coverage report\n"
    )
    (repo / "Makefile").write_text(mf_content, encoding="utf-8")
    prop = build_proposal_from_repo(repo)
    assert prop is not None
    screen = format_proposal_screen(prop)
    assert "Makefile Recipe Lines:" in screen
    assert "pytest --verbose tests/" in screen
    assert "python -m coverage report" in screen


def test_editing_recipe_after_approval_invalidates_trust(tmp_path, monkeypatch):
    """Editing the Makefile recipe after approval makes the trusted config go stale and removes trust."""
    monkeypatch.setenv("JEV_HOME", str(tmp_path / ".jev_home"))
    repo = _create_git_repo(tmp_path / "repo_recipe_stale")
    mf_path = repo / "Makefile"
    mf_path.write_text("test:\n\tpytest -v\n", encoding="utf-8")

    prop = build_proposal_from_repo(repo)
    assert prop is not None
    cfg, trust_rec = approve_proposal(prop, repo)

    # Trust is valid before edit
    is_valid, err = validate_trust(cfg, repo)
    assert is_valid is True
    assert err is None

    # Tamper with recipe in Makefile
    mf_path.write_text("test:\n\tpytest -v --different-flag\n", encoding="utf-8")

    # validate_trust detects recipe change and invalidates trust (deleting trust file)
    is_valid, err = validate_trust(cfg, repo)
    assert is_valid is False
    assert "Makefile recipe changed: approval invalidated" in err

    # Trust file is now removed (stale)
    trust_file = get_trust_file_path(compute_repo_id(repo))
    assert not trust_file.exists()


def test_ci_install_line_before_pytest_proposes_pytest(tmp_path):
    """CI workflow with pip install before pytest ignores install and proposes pytest."""
    repo = _create_git_repo(tmp_path / "repo_ci_install")
    wf_dir = repo / ".github" / "workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    wf_yaml = (
        "name: CI Test\n"
        "on: [push]\n"
        "jobs:\n"
        "  build-and-test:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - run: pip install pytest-cov\n"
        "      - run: pytest -v\n"
    )
    (wf_dir / "ci.yml").write_text(wf_yaml, encoding="utf-8")

    prop = build_proposal_from_repo(repo)
    assert prop is not None
    assert prop.exact_argv == ["pytest", "-v"]
    assert "pytest" in prop.source_evidence


def test_ci_multiline_run_block_extracts_test_command(tmp_path):
    """CI workflow with multi-line run: | block extracts test command and ignores installs."""
    repo = _create_git_repo(tmp_path / "repo_ci_multiline")
    wf_dir = repo / ".github" / "workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    wf_yaml = (
        "name: MultiLine CI\n"
        "on: [pull_request]\n"
        "jobs:\n"
        "  test:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: |\n"
        "          pip install -r requirements.txt\n"
        "          pip install pytest-cov\n"
        "          pytest tests/ --cov=src\n"
    )
    (wf_dir / "test.yaml").write_text(wf_yaml, encoding="utf-8")

    prop = build_proposal_from_repo(repo)
    assert prop is not None
    assert prop.exact_argv == ["pytest", "tests/", "--cov=src"]


def test_ci_workflow_expression_left_unexpanded_and_shown_on_screen(tmp_path):
    """CI ${{ ... }} expressions are left unexpanded in proposal and flagged on screen."""
    repo = _create_git_repo(tmp_path / "repo_ci_expr")
    wf_dir = repo / ".github" / "workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    wf_yaml = (
        "name: Matrix CI\n"
        "on: [push]\n"
        "jobs:\n"
        "  test:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: pytest --python-version ${{ matrix.python-version }}\n"
    )
    (wf_dir / "matrix.yml").write_text(wf_yaml, encoding="utf-8")

    prop = build_proposal_from_repo(repo)
    assert prop is not None
    assert "${{" in " ".join(prop.exact_argv)
    screen = format_proposal_screen(prop)
    assert "NOTICE: CI workflow expressions (${{ ... }}) left unexpanded." in screen


def test_windows_make_lookup_resolves_mingw32_make(tmp_path, monkeypatch):
    """On Windows, when make is not on PATH, inspect_executable falls back to mingw32-make."""
    def fake_which(cmd):
        if cmd in ("make", "make.exe"):
            return None
        if cmd == "mingw32-make":
            return r"C:\msys64\mingw64\bin\mingw32-make.exe"
        return None

    monkeypatch.setattr(shutil, "which", fake_which)
    monkeypatch.setattr(sys, "platform", "win32")

    exe_path, exe_ver = inspect_executable(["make", "test"])
    assert exe_path == r"C:\msys64\mingw64\bin\mingw32-make.exe"
    assert exe_ver is None

    prop = Proposal(
        id="prop-win-make",
        repo_id="repo-1",
        exact_argv=["make", "test"],
        cwd=".",
        source_evidence="Makefile target 'test'",
        tool_path=exe_path,
        platform="win32",
        identity_hash="dummyhash",
        created_at="2026-11-02T00:00:00Z",
    )
    screen = format_proposal_screen(prop)
    assert "resolved mingw32-make for make on Windows" in screen



