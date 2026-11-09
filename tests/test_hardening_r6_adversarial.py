import os
import pytest
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

from jev.engine import (
    JevEngine,
    SqliteSaver,
    node_gate,
    node_implement,
    node_verify,
    route_gate,
    route_plan,
)
from jev.models import (
    MechanicalCheckResult,
    State,
    Subgoal,
    TestOutcome,
    ValidationVerdict,
)
from jev.repo_config import (
    is_dos_device_name,
    is_protected_path,
)
from jev.workspace import Workspace


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "test_repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    readme = repo / "README.md"
    readme.write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo, check=True)
    return repo


# ==============================================================================
# ADV-01: Protected-file modification resistance
# ==============================================================================
class TestR6ProtectedFileModificationResistance:
    """Evaluates resistance to direct, indirect, traversal, alternate representations,
    trailing dots/spaces, NTFS ADS, and tool chain mutations on protected paths.
    """

    def test_direct_protected_prefix_and_file_blocked(self, git_repo: Path):
        ws = Workspace(repo_dir=git_repo)
        protected_targets = [
            ".git/hooks/pre-commit",
            ".jev/config.json",
            "node_modules/package/index.js",
            "repo_config.py",
            "runner_registry.py",
            "runner_profiles.json",
            "process_runner.py",
            "src/jev/repo_config.py",
        ]
        for target in protected_targets:
            assert is_protected_path(target), f"Expected is_protected_path=True for {target}"
            with pytest.raises(PermissionError):
                ws.stage_file_mutation(target, "malicious content")

    def test_case_variation_protected_paths_blocked(self, git_repo: Path):
        ws = Workspace(repo_dir=git_repo)
        case_variations = [
            "REPO_CONFIG.PY",
            "Repo_Config.Py",
            "RUNNER_REGISTRY.PY",
            "PROCESS_RUNNER.PY",
            ".GIT/config",
            ".Git/HEAD",
            ".JEV/trust.json",
            "NODE_MODULES/evil.js",
            "src/jev/REPO_CONFIG.PY",
        ]
        for target in case_variations:
            assert is_protected_path(target), f"Expected case-insensitive protection for {target}"
            with pytest.raises(PermissionError):
                ws.stage_file_mutation(target, "malicious content")

    def test_windows_trailing_dots_and_spaces_protected_paths(self, git_repo: Path):
        """Windows filesystem trims trailing dots and spaces, which could alias protected files."""
        trailing_variants = [
            "repo_config.py.",
            "repo_config.py ",
            "runner_registry.py.",
            "runner_profiles.json.",
            "process_runner.py.",
            ".git.",
            ".git./hooks/pre-commit",
            ".jev.",
            ".jev./config.json",
            "node_modules.",
            "node_modules./evil.js",
        ]
        for target in trailing_variants:
            assert is_protected_path(target), f"Expected is_protected_path=True for trailing dot/space variant {target}"

    def test_ntfs_alternate_data_stream_protected_paths(self, git_repo: Path):
        """NTFS Alternate Data Streams (e.g. repo_config.py:stream) must be treated as protected."""
        ads_variants = [
            "repo_config.py:stream",
            "runner_registry.py:$DATA",
            "process_runner.py:hidden",
            "runner_profiles.json:stream",
        ]
        for target in ads_variants:
            assert is_protected_path(target), f"Expected is_protected_path=True for NTFS ADS {target}"

    def test_path_traversal_mutation_strictly_rejected(self, git_repo: Path):
        ws = Workspace(repo_dir=git_repo)
        traversal_attempts = [
            "../outside.txt",
            "../../etc/passwd",
            "foo/../../bar.txt",
            "..\\outside.txt",
            "sub/../../repo_config.py",
        ]
        for target in traversal_attempts:
            with pytest.raises((ValueError, PermissionError)):
                ws.stage_file_mutation(target, "escape")

    def test_dos_device_name_mutation_strictly_rejected(self, git_repo: Path):
        ws = Workspace(repo_dir=git_repo)
        dos_targets = ["CON", "PRN", "AUX", "NUL", "COM1", "LPT1", "aux.py", "sub/nul.txt"]
        for target in dos_targets:
            assert is_dos_device_name(target), f"Expected is_dos_device_name=True for {target}"
            with pytest.raises((ValueError, PermissionError)):
                ws.stage_file_mutation(target, "device data")

    def test_symlink_creation_and_mutation_blocked(self, git_repo: Path, tmp_path: Path):
        ws = Workspace(repo_dir=git_repo)
        outside_file = tmp_path / "outside_target.txt"
        outside_file.write_text("secret", encoding="utf-8")
        symlink_target = git_repo / "link_outside.txt"
        try:
            os.symlink(outside_file, symlink_target)
        except (OSError, NotImplementedError):
            pytest.skip("Symlinks not supported in this environment")

        with pytest.raises(PermissionError):
            ws.stage_file_mutation("link_outside.txt", "hijacked")


# ==============================================================================
# ADV-02: Retry integrity and execution accounting
# ==============================================================================
class TestR6RetryIntegrityAndAccounting:
    """Evaluates strike counters, repeated failures, duplicate runs, and deadlock escalation."""

    def test_strike_counter_escalation_at_three_mechanical_strikes(self):
        state: State = {
            "ticket": "test mechanical strikes",
            "status": "in_progress",
            "mechanical_strike_count": 2,
            "semantic_strike_count": 0,
            "plan_queue": [],
            "current_subgoal": Subgoal(id="sg-1", description="test subgoal", scope=["a.py"]),
            "trajectory": [],
        }
        ws = MagicMock()
        ws.get_staged_diff.return_value = "diff --git a/a.py b/a.py\n+x = 1"
        ws.run_mechanical_checks.return_value = MechanicalCheckResult(
            passed=False, failed_check="compile", detail="SyntaxError"
        )
        gk = MagicMock()

        new_state = node_gate(state, workspace=ws, gatekeeper=gk)
        assert new_state["mechanical_strike_count"] == 3
        gk.escalate_deadlock.assert_called_once()
        assert route_gate(new_state) == "escalate"

    def test_strike_counter_escalation_at_three_semantic_strikes(self):
        state: State = {
            "ticket": "test semantic strikes",
            "status": "in_progress",
            "mechanical_strike_count": 0,
            "semantic_strike_count": 2,
            "plan_queue": [],
            "current_subgoal": Subgoal(id="sg-1", description="test subgoal", scope=["a.py"]),
            "trajectory": [],
        }
        ws = MagicMock()
        ws.get_staged_diff.return_value = "diff --git a/a.py b/a.py\n+x = 1"
        ws.run_mechanical_checks.return_value = MechanicalCheckResult(passed=True)
        gk = MagicMock()
        gk.validate_subgoal.return_value = ValidationVerdict(valid=False, reason="Incomplete logic")

        new_state = node_gate(state, workspace=ws, gatekeeper=gk)
        assert new_state["semantic_strike_count"] == 3
        gk.escalate_deadlock.assert_called_once()
        assert route_gate(new_state) == "escalate"

    def test_duplicate_diff_advances_strike_count_and_prevents_infinite_loop(self):
        diff_text = "diff --git a/a.py b/a.py\n+x = 1"
        import hashlib
        d_sha = hashlib.sha256(diff_text.encode("utf-8")).hexdigest()

        state: State = {
            "ticket": "test duplicate diff",
            "status": "in_progress",
            "mechanical_strike_count": 1,
            "failed_attempts": [{"diff_sha": d_sha, "tier": "mechanical", "failed_check": "compile", "attempt_no": 1}],
            "current_subgoal": Subgoal(id="sg-1", description="test subgoal", scope=["a.py"]),
            "trajectory": [],
        }
        ws = MagicMock()
        ws.get_staged_diff.return_value = diff_text
        gk = MagicMock()

        new_state = node_gate(state, workspace=ws, gatekeeper=gk)
        assert new_state["mechanical_strike_count"] == 2
        assert new_state["gate_status"] == "mechanical_failure"
        assert "Duplicate diff detected" in new_state["last_feedback"]

    def test_successful_subgoal_resets_both_strike_counters(self):
        state: State = {
            "ticket": "test strike reset",
            "status": "in_progress",
            "mechanical_strike_count": 2,
            "semantic_strike_count": 2,
            "plan_queue": [Subgoal(id="sg-2", description="next subgoal", scope=["b.py"])],
            "current_subgoal": Subgoal(id="sg-1", description="first subgoal", scope=["a.py"]),
            "trajectory": [],
        }
        ws = MagicMock()
        ws.get_staged_diff.return_value = "diff --git a/a.py b/a.py\n+x = 1"
        ws.run_mechanical_checks.return_value = MechanicalCheckResult(passed=True)
        gk = MagicMock()
        gk.validate_subgoal.return_value = ValidationVerdict(valid=True, reason="Looks great")

        new_state = node_gate(state, workspace=ws, gatekeeper=gk)
        assert new_state["mechanical_strike_count"] == 0
        assert new_state["semantic_strike_count"] == 0
        assert new_state["gate_status"] == "passed"
        assert route_gate(new_state) == "implement"


# ==============================================================================
# ADV-03: False test claims and verification integrity
# ==============================================================================
class TestR6VerificationIntegrityAndFalseClaims:
    """Evaluates resistance to unexecuted tests, failed tests claiming success,
    gatekeeper overriding test failures, and exit-code manipulation.
    """

    def test_failing_unit_tests_cannot_be_verified_even_if_gatekeeper_approves(self):
        """A test suite that executed and failed must NEVER result in gate_status='verified',
        even if an LLM gatekeeper hallucinates or returns valid=True.
        """
        ws = MagicMock()
        ws.check_compile.return_value = {"outcome": "PASSED"}
        ws.run_tests.return_value = TestOutcome.FAILED
        ws.get_cumulative_diff.return_value = "diff --git a/a.py b/a.py\n+x = 1"
        ws.get_staged_diff.return_value = ""
        ws.last_test_run = {
            "ran": True,
            "exit_code": 1,
            "outcome": "FAILED",
            "output_tail": "FAILED test_feature.py - AssertionError",
        }

        # Gatekeeper mistakenly or maliciously validates the ticket
        gk = MagicMock()
        gk.verify_ticket.return_value = ValidationVerdict(valid=True, reason="Looks fine to me")

        state: State = {
            "ticket": "Implement feature",
            "integration_branch": "jev-ticket-1",
            "status": "in_progress",
            "trajectory": [],
        }

        out = node_verify(state, workspace=ws, gatekeeper=gk)
        assert out.get("gate_status") != "verified", "Failed tests must never be marked verified!"
        assert out.get("gate_status") == "verification_failed"
        assert out.get("status") in ("verification_failed", "escalated")
        ws.fast_forward_main.assert_not_called()

    def test_failing_unit_tests_without_gatekeeper_never_verifies_or_lands(self):
        """When gatekeeper is None, failing tests must never fall into the verify/fast-forward branch."""
        ws = MagicMock()
        ws.check_compile.return_value = {"outcome": "PASSED"}
        ws.run_tests.return_value = TestOutcome.FAILED
        ws.get_cumulative_diff.return_value = "diff --git a/a.py b/a.py\n+x = 1"
        ws.get_staged_diff.return_value = ""
        ws.last_test_run = {
            "ran": True,
            "exit_code": 1,
            "outcome": "FAILED",
            "output_tail": "FAILED test_feature.py - AssertionError",
        }

        state: State = {
            "ticket": "Implement feature without gatekeeper",
            "integration_branch": "jev-ticket-1",
            "status": "in_progress",
            "trajectory": [],
        }

        out = node_verify(state, workspace=ws, gatekeeper=None)
        assert out.get("gate_status") != "verified", "Failed tests without gatekeeper must never land!"
        assert out.get("gate_status") == "verification_failed"
        ws.fast_forward_main.assert_not_called()

    def test_test_timeout_fails_verification_and_escalates(self):
        ws = MagicMock()
        ws.check_compile.return_value = {"outcome": "PASSED"}
        ws.run_tests.side_effect = subprocess.TimeoutExpired(cmd=["pytest"], timeout=30)
        ws.get_cumulative_diff.return_value = "diff --git a/a.py b/a.py\n+x = 1"
        ws.get_staged_diff.return_value = ""

        gk = MagicMock()
        state: State = {
            "ticket": "Test timeout",
            "integration_branch": "jev-ticket-1",
            "status": "in_progress",
            "trajectory": [],
        }

        out = node_verify(state, workspace=ws, gatekeeper=gk)
        assert out.get("gate_status") == "verification_failed"
        ws.fast_forward_main.assert_not_called()

    def test_missing_integration_branch_refuses_verification_on_main(self):
        ws = MagicMock()
        gk = MagicMock()
        state: State = {
            "ticket": "Direct main verification attempt",
            "integration_branch": None,
            "trajectory": [],
        }
        out = node_verify(state, workspace=ws, gatekeeper=gk)
        assert out.get("gate_status") == "verification_failed"
        ws.create_verify_worktree.assert_not_called()


# ==============================================================================
# ADV-04: Interruption and recovery integrity
# ==============================================================================
class TestR6InterruptionAndRecoveryIntegrity:
    """Evaluates checkpoint persistence, thread restart isolation, and crash resilience."""

    def test_corrupted_checkpoint_payload_fails_safely(self, tmp_path: Path):
        db_file = tmp_path / "corrupt_checkpoints.db"
        saver = SqliteSaver.from_conn_string(str(db_file))

        # Manually corrupt the checkpoint blob in SQLite
        with saver.cursor(transaction=True) as cursor:
            cursor.execute(
                """
                INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                ("corrupt-thread", "", "cp-1", None, "json", b"{not valid json!@#$", "{}"),
            )

        with pytest.raises(Exception):
            saver.get_tuple({"configurable": {"thread_id": "corrupt-thread"}})

    def test_thread_restart_wipes_stale_integration_branch(self, git_repo: Path):
        """Restarting an existing thread id recreates integration branch and rejects dirty state."""
        ws = Workspace(repo_dir=git_repo)
        thread_id = "test-restart-thread"
        branch_name = f"jev-ticket-{thread_id}"

        ws.create_integration_branch(branch_name)
        # Modify something on the integration branch
        test_file = git_repo / "branch_marker.txt"
        test_file.write_text("old work\n", encoding="utf-8")
        ws._run_repo_git(["add", "branch_marker.txt"])
        ws._run_repo_git(["commit", "-m", "stale work on integration branch"])

        engine = JevEngine(workspace=ws, db_path=None)
        # Mock LLM to avoid real API calls
        engine.app = MagicMock()
        engine.app.invoke.return_value = {"status": "escalated", "gate_status": "planning_failed"}

        # Running engine with same thread_id should wipe stale branch
        engine.execute("test ticket", thread_id=thread_id)
        # Integration branch should have been recreated from base_commit
        rev_branch = ws._run_repo_git(["rev-parse", branch_name]).stdout.strip()
        assert rev_branch == ws.base_commit


# ==============================================================================
# ADV-05: Illegal state transitions
# ==============================================================================
class TestR6IllegalStateTransitions:
    """Evaluates FSM routing invariants, preventing skipping of required nodes or illegal jumps."""

    def test_planning_failed_routes_only_to_escalate(self):
        state: State = {"gate_status": "planning_failed"}
        assert route_plan(state) == "escalate"

    def test_planning_success_routes_only_to_implement(self):
        state: State = {"gate_status": None}
        assert route_plan(state) == "implement"

    def test_gate_passed_routes_to_verify_only_when_queue_empty(self):
        state_empty_queue: State = {"gate_status": "passed", "plan_queue": []}
        assert route_gate(state_empty_queue) == "verify"

        state_with_queue: State = {
            "gate_status": "passed",
            "plan_queue": [Subgoal(id="sg-2", description="second", scope=["b.py"])],
        }
        assert route_gate(state_with_queue) == "implement"

    def test_terminal_escalated_state_cannot_reenter_implement_or_verify(self):
        state_escalated: State = {"status": "escalated", "gate_status": "passed"}
        assert route_gate(state_escalated) == "escalate"

        state_merge_failed: State = {"gate_status": "merge_failed"}
        assert route_gate(state_merge_failed) == "escalate"

        state_inconclusive: State = {"gate_status": "verification_inconclusive"}
        assert route_gate(state_inconclusive) == "escalate"


# ==============================================================================
# ADV-06: Permission escalation and authorization boundaries
# ==============================================================================
class TestR6PermissionEscalationAndBoundaries:
    """Evaluates enforcement of tool permissions, read-only guarantees, secret protection,
    and out-of-scope mutation blocking.
    """

    def test_read_tools_cannot_execute_arbitrary_commands(self, git_repo: Path):
        ws = Workspace(repo_dir=git_repo)
        unauthorized_commands = ["bash", "sh", "python", "curl", "wget", "rm", "powershell", "cmd"]
        for cmd in unauthorized_commands:
            out = ws.run_read_tool(cmd, ["-c", "echo pwned"])
            assert "Access denied" in out or "not an authorized read-only tool" in out

    def test_git_read_tool_blocks_dangerous_options_and_subcommands(self, git_repo: Path):
        ws = Workspace(repo_dir=git_repo)
        blocked_calls = [
            ("clone", ["https://evil.com"]),
            ("commit", ["-m", "evil"]),
            ("push", ["origin", "master"]),
            ("diff", ["-c", "alias.x=!id"]),
            ("status", ["--output=/tmp/evil"]),
            ("log", ["--exec-path=/tmp"]),
            ("diff", ["--ext-diff"]),
            ("show", ["--paginate"]),
        ]
        for subcmd, args in blocked_calls:
            out = ws.run_read_tool("git", [subcmd] + args)
            assert "Access denied" in out or "not an authorized read-only git command" in out

    def test_read_tools_block_protected_secret_files(self, git_repo: Path):
        ws = Workspace(repo_dir=git_repo)
        secret_targets = [".env", ".env.local", "id_rsa", "aws_credentials.json", "private.key"]
        for sec in secret_targets:
            out = ws.run_read_tool("cat", [sec])
            assert "Access denied to protected secret file" in out

    def test_read_tools_block_path_traversal_outside_worktree(self, git_repo: Path):
        ws = Workspace(repo_dir=git_repo)
        traversal_reads = ["../outside.txt", "../../etc/shadow", "foo/../../bar.txt"]
        for target in traversal_reads:
            out = ws.run_read_tool("cat", [target])
            assert "Access denied outside workspace" in out or "traverses outside" in out

    def test_stage_file_mutation_rejects_out_of_scope_files(self, git_repo: Path):
        """In node_implement, mutation is strictly limited to active subgoal scope."""
        ws = Workspace(repo_dir=git_repo)
        # Create worktree
        wt_path = ws.create_subgoal_worktree("sg1")
        ws.worktree_dir = wt_path

        state: State = {
            "ticket": "Implement feature in scope",
            "current_subgoal": Subgoal(id="sg-1", description="edit a.py", scope=["a.py"]),
            "plan_queue": [],
            "trajectory": [],
            "current_worktree_path": str(wt_path),
        }

        # Mock LLM to invoke stage_file_mutation on b.py (out of scope)
        mock_llm = MagicMock()
        tool_call_resp = MagicMock()
        tool_call_resp.tool_calls = [
            {
                "id": "c1",
                "name": "stage_file_mutation",
                "args": {"path": "b.py", "content": "out of scope edit"},
            },
            {
                "id": "c2",
                "name": "submit_subgoal",
                "args": {},
            },
        ]
        mock_llm.bind_tools.return_value.invoke.return_value = tool_call_resp

        node_implement(state, workspace=ws, llm=mock_llm)
        # Confirm b.py was NOT staged
        staged_files = ws.worker_staged_paths
        assert "b.py" not in staged_files


# ==============================================================================
# ADV-07: Windows Protected-Path Security & Disk Boundary Integrity
# ==============================================================================
class TestR6WindowsProtectedPathDiskIntegrity:
    """Exercises the actual stage_file_mutation() boundary on disk with Windows
    trailing-dot, trailing-space, NTFS alternate data stream (ADS), and case aliases.
    Verifies protected files cannot be modified through aliases and remain untouched on disk.
    Executes on Windows and Linux.
    """

    def test_stage_file_mutation_rejects_trailing_dot_aliases_and_preserves_disk(self, git_repo: Path):
        ws = Workspace(repo_dir=git_repo)
        wt = ws.create_subgoal_worktree("sg-dot-test")
        ws.worktree_dir = wt

        # Create real protected files on disk with known baseline content
        cfg_file = wt / "src" / "jev" / "repo_config.py"
        cfg_file.parent.mkdir(parents=True, exist_ok=True)
        baseline_content = "# ORIGINAL REPO_CONFIG CONTENT\nSAFE_INVARIANT = True\n"
        cfg_file.write_text(baseline_content, encoding="utf-8")

        jev_cfg = wt / ".jev" / "config.json"
        jev_cfg.parent.mkdir(parents=True, exist_ok=True)
        baseline_jev = '{"safe": true}\n'
        jev_cfg.write_text(baseline_jev, encoding="utf-8")

        trailing_dot_aliases = [
            "src/jev/repo_config.py.",
            "src/jev/repo_config.py..",
            "src/jev/repo_config.py...",
            "repo_config.py.",
            "runner_registry.py.",
            "runner_profiles.json.",
            "process_runner.py.",
            ".git.",
            ".git./hooks/pre-commit",
            ".jev.",
            ".jev./config.json",
            "node_modules.",
            "node_modules./evil.js",
        ]

        for alias in trailing_dot_aliases:
            assert is_protected_path(alias), f"Expected is_protected_path=True for {alias}"
            with pytest.raises(PermissionError):
                ws.stage_file_mutation(alias, "MALICIOUS_OVERWRITE_PAYLOAD")

        # Verify on-disk integrity: original files remain completely intact and untouched
        assert cfg_file.read_text(encoding="utf-8") == baseline_content
        assert jev_cfg.read_text(encoding="utf-8") == baseline_jev

    def test_stage_file_mutation_rejects_trailing_space_aliases_and_preserves_disk(self, git_repo: Path):
        ws = Workspace(repo_dir=git_repo)
        wt = ws.create_subgoal_worktree("sg-space-test")
        ws.worktree_dir = wt

        cfg_file = wt / "src" / "jev" / "repo_config.py"
        cfg_file.parent.mkdir(parents=True, exist_ok=True)
        baseline_content = "# PROTECTED CONFIG\n"
        cfg_file.write_text(baseline_content, encoding="utf-8")

        trailing_space_aliases = [
            "src/jev/repo_config.py ",
            "src/jev/repo_config.py   ",
            "src/jev/repo_config.py.. ",
            "src/jev/repo_config.py . .",
            "repo_config.py ",
            "runner_registry.py ",
            ".git ",
            ".git /config",
            ".jev ",
            ".jev /config.json",
            "node_modules ",
            "node_modules /package.json",
        ]

        for alias in trailing_space_aliases:
            assert is_protected_path(alias), f"Expected is_protected_path=True for {alias}"
            with pytest.raises(PermissionError):
                ws.stage_file_mutation(alias, "MALICIOUS_OVERWRITE_PAYLOAD")

        assert cfg_file.read_text(encoding="utf-8") == baseline_content

    def test_stage_file_mutation_rejects_ntfs_ads_aliases_and_preserves_disk(self, git_repo: Path):
        ws = Workspace(repo_dir=git_repo)
        wt = ws.create_subgoal_worktree("sg-ads-test")
        ws.worktree_dir = wt

        cfg_file = wt / "src" / "jev" / "repo_config.py"
        cfg_file.parent.mkdir(parents=True, exist_ok=True)
        baseline_content = "# SAFE CONFIG CONTENT\n"
        cfg_file.write_text(baseline_content, encoding="utf-8")

        ads_aliases = [
            "src/jev/repo_config.py:stream",
            "src/jev/repo_config.py:$DATA",
            "src/jev/repo_config.py:hidden",
            "src/jev/repo_config.py.:stream",
            "src/jev/repo_config.py :stream",
            "src/jev/repo_config.py.. :$DATA",
            "repo_config.py:stream",
            "runner_registry.py:$DATA",
            "process_runner.py:stream",
            "runner_profiles.json:stream",
        ]

        for alias in ads_aliases:
            assert is_protected_path(alias), f"Expected is_protected_path=True for {alias}"
            with pytest.raises(PermissionError):
                ws.stage_file_mutation(alias, "MALICIOUS_ADS_PAYLOAD")

        assert cfg_file.read_text(encoding="utf-8") == baseline_content

    def test_stage_file_mutation_rejects_case_variation_and_preserves_disk(self, git_repo: Path):
        ws = Workspace(repo_dir=git_repo)
        wt = ws.create_subgoal_worktree("sg-case-test")
        ws.worktree_dir = wt

        cfg_file = wt / "src" / "jev" / "repo_config.py"
        cfg_file.parent.mkdir(parents=True, exist_ok=True)
        baseline_content = "# ORIGINAL CASE-INSENSITIVE PROTECTED CONTENT\n"
        cfg_file.write_text(baseline_content, encoding="utf-8")

        case_aliases = [
            "src/jev/REPO_CONFIG.PY",
            "SRC/JEV/repo_config.py",
            "SRC/JEV/REPO_CONFIG.PY",
            "REPO_CONFIG.PY",
            "RUNNER_REGISTRY.PY",
            "PROCESS_RUNNER.PY",
            "RUNNER_PROFILES.JSON",
            ".GIT/config",
            ".GIT/hooks/pre-commit",
            ".JEV/config.json",
            "NODE_MODULES/package/index.js",
            "src/jev/REPO_CONFIG.PY.",
            "src/jev/REPO_CONFIG.PY ",
        ]

        for alias in case_aliases:
            assert is_protected_path(alias), f"Expected is_protected_path=True for {alias}"
            with pytest.raises(PermissionError):
                ws.stage_file_mutation(alias, "MALICIOUS_CASE_PAYLOAD")

        assert cfg_file.read_text(encoding="utf-8") == baseline_content


# ==============================================================================
# ADV-08: Verification Integrity & Runner Metadata Validation
# ==============================================================================
class TestR6VerificationIntegrityAndMetadataValidation:
    """Evaluates strict verification integrity:
    1. Explicit nonzero exit codes fail verification.
    2. Contradictory runner metadata fails verification.
    3. Malformed/incomplete runner metadata fails verification.
    4. A passing gatekeeper verdict must never override failed test execution.
    """

    def test_explicit_nonzero_exit_code_fails_verification_even_if_outcome_claims_passed(self):
        """Runner exit_code != 0 must cause verification failure even if outcome claims PASSED."""
        ws = MagicMock()
        ws.check_compile.return_value = {"outcome": "PASSED"}
        ws.run_tests.return_value = TestOutcome.PASSED
        ws.get_cumulative_diff.return_value = "diff --git a/a.py b/a.py\n+x = 1"
        ws.get_staged_diff.return_value = ""
        ws.last_test_run = {
            "ran": True,
            "exit_code": 1,  # Nonzero exit code
            "outcome": "PASSED",
            "output_tail": "Tests passed according to output tail",
        }

        gk = MagicMock()
        gk.verify_ticket.return_value = ValidationVerdict(valid=True, probability=0.99, reason="Gatekeeper approved")

        state: State = {
            "ticket": "Implement feature",
            "integration_branch": "jev-ticket-1",
            "status": "in_progress",
            "trajectory": [],
        }

        out = node_verify(state, workspace=ws, gatekeeper=gk)
        assert out.get("gate_status") == "verification_failed", "Nonzero exit code must fail verification!"
        assert out.get("status") in ("verification_failed", "escalated")
        assert out.get("full_suite_clean") is False
        assert out.get("test_classification") == "FAILED"
        ws.fast_forward_main.assert_not_called()
        ws.delete_integration_branch.assert_not_called()

    def test_contradictory_metadata_new_failures_fails_verification(self):
        """new_failures > 0 with outcome claiming PASSED must fail verification."""
        ws = MagicMock()
        ws.check_compile.return_value = {"outcome": "PASSED"}
        ws.run_tests.return_value = TestOutcome.PASSED
        ws.get_cumulative_diff.return_value = "diff --git a/a.py b/a.py\n+x = 1"
        ws.get_staged_diff.return_value = ""
        ws.last_test_run = {
            "ran": True,
            "exit_code": 0,
            "new_failures": 2,  # Contradictory new failures!
            "outcome": "PASSED",
        }

        gk = MagicMock()
        gk.verify_ticket.return_value = ValidationVerdict(valid=True, probability=0.99)

        state: State = {
            "ticket": "Implement feature",
            "integration_branch": "jev-ticket-1",
            "status": "in_progress",
            "trajectory": [],
        }

        out = node_verify(state, workspace=ws, gatekeeper=gk)
        assert out.get("gate_status") == "verification_failed", "Contradictory new_failures must fail verification!"
        ws.fast_forward_main.assert_not_called()

    def test_contradictory_metadata_failing_test_ids_fails_verification(self):
        """failing_test_ids non-empty with outcome claiming PASSED must fail verification."""
        ws = MagicMock()
        ws.check_compile.return_value = {"outcome": "PASSED"}
        ws.run_tests.return_value = TestOutcome.PASSED
        ws.get_cumulative_diff.return_value = "diff --git a/a.py b/a.py\n+x = 1"
        ws.get_staged_diff.return_value = ""
        ws.last_test_run = {
            "ran": True,
            "exit_code": 0,
            "failing_test_ids": ["tests/test_auth.py::test_bypass"],
            "outcome": "PASSED",
        }

        gk = MagicMock()
        gk.verify_ticket.return_value = ValidationVerdict(valid=True, probability=0.99)

        state: State = {
            "ticket": "Implement feature",
            "integration_branch": "jev-ticket-1",
            "status": "in_progress",
            "trajectory": [],
        }

        out = node_verify(state, workspace=ws, gatekeeper=gk)
        assert out.get("gate_status") == "verification_failed"
        ws.fast_forward_main.assert_not_called()

    def test_contradictory_runner_outcome_failed_fails_verification(self):
        """Runner outcome FAILED while run_tests claims PASSED must fail verification."""
        ws = MagicMock()
        ws.check_compile.return_value = {"outcome": "PASSED"}
        ws.run_tests.return_value = TestOutcome.PASSED
        ws.get_cumulative_diff.return_value = "diff --git a/a.py b/a.py\n+x = 1"
        ws.get_staged_diff.return_value = ""
        ws.last_test_run = {
            "ran": True,
            "exit_code": 0,
            "outcome": "FAILED",
        }

        gk = MagicMock()
        gk.verify_ticket.return_value = ValidationVerdict(valid=True, probability=0.99)

        state: State = {
            "ticket": "Implement feature",
            "integration_branch": "jev-ticket-1",
            "status": "in_progress",
            "trajectory": [],
        }

        out = node_verify(state, workspace=ws, gatekeeper=gk)
        assert out.get("gate_status") == "verification_failed"
        ws.fast_forward_main.assert_not_called()

    def test_malformed_metadata_not_dict_fails_verification(self):
        """Non-dict last_test_run when tests ran must fail verification."""
        ws = MagicMock()
        ws.check_compile.return_value = {"outcome": "PASSED"}
        ws.run_tests.return_value = None
        ws.get_cumulative_diff.return_value = "diff --git a/a.py b/a.py\n+x = 1"
        ws.get_staged_diff.return_value = ""
        ws.last_test_run = "MALFORMED_NON_DICT_STRING"

        gk = MagicMock()
        gk.verify_ticket.return_value = ValidationVerdict(valid=True, probability=0.99)

        state: State = {
            "ticket": "Implement feature",
            "integration_branch": "jev-ticket-1",
            "status": "in_progress",
            "trajectory": [],
        }

        out = node_verify(state, workspace=ws, gatekeeper=gk)
        assert out.get("gate_status") == "verification_failed"
        ws.fast_forward_main.assert_not_called()

    def test_malformed_metadata_unknown_outcome_fails_verification(self):
        """Outcome UNKNOWN must fail verification."""
        ws = MagicMock()
        ws.check_compile.return_value = {"outcome": "PASSED"}
        ws.run_tests.return_value = "UNKNOWN"
        ws.get_cumulative_diff.return_value = "diff --git a/a.py b/a.py\n+x = 1"
        ws.get_staged_diff.return_value = ""
        ws.last_test_run = {
            "ran": True,
            "exit_code": None,
            "outcome": "UNKNOWN",
        }

        gk = MagicMock()
        gk.verify_ticket.return_value = ValidationVerdict(valid=True, probability=0.99)

        state: State = {
            "ticket": "Implement feature",
            "integration_branch": "jev-ticket-1",
            "status": "in_progress",
            "trajectory": [],
        }

        out = node_verify(state, workspace=ws, gatekeeper=gk)
        assert out.get("gate_status") == "verification_failed"
        ws.fast_forward_main.assert_not_called()

    def test_malformed_metadata_ran_false_fails_verification(self):
        """Metadata claiming ran=False when tests were executed must fail verification."""
        ws = MagicMock()
        ws.check_compile.return_value = {"outcome": "PASSED"}
        ws.run_tests.return_value = None
        ws.get_cumulative_diff.return_value = "diff --git a/a.py b/a.py\n+x = 1"
        ws.get_staged_diff.return_value = ""
        ws.last_test_run = {
            "ran": False,
            "exit_code": None,
            "outcome": "PASSED",
        }

        gk = MagicMock()
        gk.verify_ticket.return_value = ValidationVerdict(valid=True, probability=0.99)

        state: State = {
            "ticket": "Implement feature",
            "integration_branch": "jev-ticket-1",
            "status": "in_progress",
            "trajectory": [],
        }

        out = node_verify(state, workspace=ws, gatekeeper=gk)
        assert out.get("gate_status") == "verification_failed"
        ws.fast_forward_main.assert_not_called()

    def test_passing_gatekeeper_verdict_never_overrides_failed_test_execution(self):
        """Adversarial test: A perfect Gatekeeper verdict (probability 1.0, valid=True)
        must NEVER override failed unit test execution, whether from nonzero exit code,
        test failure output, or contradictory metadata.
        """
        ws = MagicMock()
        ws.check_compile.return_value = {"outcome": "PASSED"}
        ws.run_tests.return_value = TestOutcome.FAILED
        ws.get_cumulative_diff.return_value = "diff --git a/a.py b/a.py\n+x = 1"
        ws.get_staged_diff.return_value = ""
        ws.last_test_run = {
            "ran": True,
            "exit_code": 2,
            "outcome": "FAILED",
            "output_tail": "2 failed, 10 passed in 1.2s",
        }

        # Gatekeeper returns highest possible approval
        gk = MagicMock()
        gk.verify_ticket.return_value = ValidationVerdict(
            valid=True,
            probability=1.0,
            reason="Flawless code changes, 100% approved by gatekeeper",
        )

        state: State = {
            "ticket": "Security critical feature",
            "integration_branch": "jev-ticket-security",
            "status": "in_progress",
            "trajectory": [],
        }

        out = node_verify(state, workspace=ws, gatekeeper=gk)
        assert out.get("gate_status") == "verification_failed", "Gatekeeper MUST NOT override failed test execution!"
        assert out.get("status") in ("verification_failed", "escalated")
        assert "Verification rejected: automated unit tests failed." in out.get("last_feedback", "")
        ws.fast_forward_main.assert_not_called()
        ws.delete_integration_branch.assert_not_called()

    def test_tests_ran_with_last_test_run_none_and_outcome_passed_fails_verification(self):
        """tests_ran=True, last_test_run=None, reported outcome PASSED must fail verification."""
        ws = MagicMock()
        ws.check_compile.return_value = {"outcome": "PASSED"}
        ws.run_tests.return_value = TestOutcome.PASSED
        ws.get_cumulative_diff.return_value = "diff --git a/a.py b/a.py\n+x = 1"
        ws.get_staged_diff.return_value = ""
        ws.last_test_run = None

        gk = MagicMock()
        gk.verify_ticket.return_value = ValidationVerdict(valid=True, probability=0.99)

        state: State = {
            "ticket": "Implement feature",
            "integration_branch": "jev-ticket-1",
            "status": "in_progress",
            "trajectory": [],
        }

        out = node_verify(state, workspace=ws, gatekeeper=gk)
        assert out.get("gate_status") == "verification_failed"
        assert out.get("status") in ("verification_failed", "escalated")
        ws.fast_forward_main.assert_not_called()

    def test_tests_ran_with_last_test_run_empty_dict_and_outcome_passed_fails_verification(self):
        """tests_ran=True, last_test_run={}, reported outcome PASSED must fail verification."""
        ws = MagicMock()
        ws.check_compile.return_value = {"outcome": "PASSED"}
        ws.run_tests.return_value = TestOutcome.PASSED
        ws.get_cumulative_diff.return_value = "diff --git a/a.py b/a.py\n+x = 1"
        ws.get_staged_diff.return_value = ""
        ws.last_test_run = {}

        gk = MagicMock()
        gk.verify_ticket.return_value = ValidationVerdict(valid=True, probability=0.99)

        state: State = {
            "ticket": "Implement feature",
            "integration_branch": "jev-ticket-1",
            "status": "in_progress",
            "trajectory": [],
        }

        out = node_verify(state, workspace=ws, gatekeeper=gk)
        assert out.get("gate_status") == "verification_failed"
        assert out.get("status") in ("verification_failed", "escalated")
        ws.fast_forward_main.assert_not_called()

    def test_tests_ran_missing_required_runner_evidence_no_exit_code_fails_verification(self):
        """Dictionary missing required runner evidence, including an actual exit code, must fail verification."""
        ws = MagicMock()
        ws.check_compile.return_value = {"outcome": "PASSED"}
        ws.run_tests.return_value = TestOutcome.PASSED
        ws.get_cumulative_diff.return_value = "diff --git a/a.py b/a.py\n+x = 1"
        ws.get_staged_diff.return_value = ""
        ws.last_test_run = {
            "ran": True,
            "outcome": "PASSED",
            # exit_code missing!
        }

        gk = MagicMock()
        gk.verify_ticket.return_value = ValidationVerdict(valid=True, probability=0.99)

        state: State = {
            "ticket": "Implement feature",
            "integration_branch": "jev-ticket-1",
            "status": "in_progress",
            "trajectory": [],
        }

        out = node_verify(state, workspace=ws, gatekeeper=gk)
        assert out.get("gate_status") == "verification_failed"
        assert out.get("status") in ("verification_failed", "escalated")
        ws.fast_forward_main.assert_not_called()

    def test_tests_ran_exit_code_none_with_claim_passed_fails_verification(self):
        """Explicit exit_code=None when tests ran and claimed PASSED must fail verification."""
        ws = MagicMock()
        ws.check_compile.return_value = {"outcome": "PASSED"}
        ws.run_tests.return_value = TestOutcome.PASSED
        ws.get_cumulative_diff.return_value = "diff --git a/a.py b/a.py\n+x = 1"
        ws.get_staged_diff.return_value = ""
        ws.last_test_run = {
            "ran": True,
            "command": ["pytest"],
            "exit_code": None,
            "outcome": "PASSED",
        }

        gk = MagicMock()
        gk.verify_ticket.return_value = ValidationVerdict(valid=True, probability=0.99)

        state: State = {
            "ticket": "Implement feature",
            "integration_branch": "jev-ticket-1",
            "status": "in_progress",
            "trajectory": [],
        }

        out = node_verify(state, workspace=ws, gatekeeper=gk)
        assert out.get("gate_status") == "verification_failed"
        assert out.get("status") in ("verification_failed", "escalated")
        ws.fast_forward_main.assert_not_called()

    def test_gatekeeper_approval_cannot_override_missing_runner_evidence(self):
        """A gatekeeper approval must never override missing or empty runner evidence."""
        ws = MagicMock()
        ws.check_compile.return_value = {"outcome": "PASSED"}
        ws.run_tests.return_value = TestOutcome.PASSED
        ws.get_cumulative_diff.return_value = "diff --git a/a.py b/a.py\n+x = 1"
        ws.get_staged_diff.return_value = ""
        ws.last_test_run = None

        gk = MagicMock()
        gk.verify_ticket.return_value = ValidationVerdict(valid=True, probability=1.0, reason="Flawless")

        state: State = {
            "ticket": "Security feature",
            "integration_branch": "jev-ticket-sec",
            "status": "in_progress",
            "trajectory": [],
        }

        out = node_verify(state, workspace=ws, gatekeeper=gk)
        assert out.get("gate_status") == "verification_failed"
        assert out.get("status") in ("verification_failed", "escalated")
        ws.fast_forward_main.assert_not_called()
        ws.delete_integration_branch.assert_not_called()

    def test_exit_code_float_fails_verification(self):
        """exit_code=0.5 with claimed PASSED must fail verification (strict integer required)."""
        ws = MagicMock()
        ws.check_compile.return_value = {"outcome": "PASSED"}
        ws.run_tests.return_value = TestOutcome.PASSED
        ws.get_cumulative_diff.return_value = "diff --git a/a.py b/a.py\n+x = 1"
        ws.get_staged_diff.return_value = ""
        ws.last_test_run = {
            "ran": True,
            "command": ["pytest"],
            "exit_code": 0.5,
            "outcome": "PASSED",
        }

        gk = MagicMock()
        gk.verify_ticket.return_value = ValidationVerdict(valid=True, probability=0.99)

        state: State = {
            "ticket": "Implement feature",
            "integration_branch": "jev-ticket-1",
            "status": "in_progress",
            "trajectory": [],
        }

        out = node_verify(state, workspace=ws, gatekeeper=gk)
        assert out.get("gate_status") == "verification_failed"
        assert out.get("status") in ("verification_failed", "escalated")
        ws.fast_forward_main.assert_not_called()

    def test_exit_code_boolean_false_fails_verification(self):
        """exit_code=False with claimed PASSED must fail verification (booleans explicitly excluded)."""
        ws = MagicMock()
        ws.check_compile.return_value = {"outcome": "PASSED"}
        ws.run_tests.return_value = TestOutcome.PASSED
        ws.get_cumulative_diff.return_value = "diff --git a/a.py b/a.py\n+x = 1"
        ws.get_staged_diff.return_value = ""
        ws.last_test_run = {
            "ran": True,
            "command": ["pytest"],
            "exit_code": False,
            "outcome": "PASSED",
        }

        gk = MagicMock()
        gk.verify_ticket.return_value = ValidationVerdict(valid=True, probability=0.99)

        state: State = {
            "ticket": "Implement feature",
            "integration_branch": "jev-ticket-1",
            "status": "in_progress",
            "trajectory": [],
        }

        out = node_verify(state, workspace=ws, gatekeeper=gk)
        assert out.get("gate_status") == "verification_failed"
        assert out.get("status") in ("verification_failed", "escalated")
        ws.fast_forward_main.assert_not_called()

    def test_exit_code_string_zero_fails_verification(self):
        """exit_code='0' with claimed PASSED must fail verification (strings rejected)."""
        ws = MagicMock()
        ws.check_compile.return_value = {"outcome": "PASSED"}
        ws.run_tests.return_value = TestOutcome.PASSED
        ws.get_cumulative_diff.return_value = "diff --git a/a.py b/a.py\n+x = 1"
        ws.get_staged_diff.return_value = ""
        ws.last_test_run = {
            "ran": True,
            "command": ["pytest"],
            "exit_code": "0",
            "outcome": "PASSED",
        }

        gk = MagicMock()
        gk.verify_ticket.return_value = ValidationVerdict(valid=True, probability=0.99)

        state: State = {
            "ticket": "Implement feature",
            "integration_branch": "jev-ticket-1",
            "status": "in_progress",
            "trajectory": [],
        }

        out = node_verify(state, workspace=ws, gatekeeper=gk)
        assert out.get("gate_status") == "verification_failed"
        assert out.get("status") in ("verification_failed", "escalated")
        ws.fast_forward_main.assert_not_called()

    def test_exit_code_zero_with_only_ecosystem_fails_verification(self):
        """exit_code=0 with only ecosystem (no command/argv/output) must fail verification."""
        ws = MagicMock()
        ws.check_compile.return_value = {"outcome": "PASSED"}
        ws.run_tests.return_value = TestOutcome.PASSED
        ws.get_cumulative_diff.return_value = "diff --git a/a.py b/a.py\n+x = 1"
        ws.get_staged_diff.return_value = ""
        ws.last_test_run = {
            "ran": True,
            "exit_code": 0,
            "ecosystem": "pytest",
            "outcome": "PASSED",
        }

        gk = MagicMock()
        gk.verify_ticket.return_value = ValidationVerdict(valid=True, probability=0.99)

        state: State = {
            "ticket": "Implement feature",
            "integration_branch": "jev-ticket-1",
            "status": "in_progress",
            "trajectory": [],
        }

        out = node_verify(state, workspace=ws, gatekeeper=gk)
        assert out.get("gate_status") == "verification_failed"
        assert out.get("status") in ("verification_failed", "escalated")
        ws.fast_forward_main.assert_not_called()

    def test_legitimate_producer_generated_metadata_passes_verification(self):
        """Legitimate integer exit_code=0 with command evidence from producer passes verification."""
        ws = MagicMock()
        ws.check_compile.return_value = {"outcome": "PASSED"}
        ws.run_tests.return_value = TestOutcome.PASSED
        ws.get_cumulative_diff.return_value = "diff --git a/a.py b/a.py\n+x = 1"
        ws.get_staged_diff.return_value = ""
        ws.last_test_run = {
            "ecosystem": "pytest",
            "command": ["pytest"],
            "executed_command": ["pytest"],
            "exit_code": 0,
            "stdout": "5 passed in 0.2s",
            "stderr": "",
            "ran": True,
            "outcome": "PASSED",
        }

        gk = MagicMock()
        gk.verify_ticket.return_value = ValidationVerdict(valid=True, probability=0.99)

        state: State = {
            "ticket": "Implement feature",
            "integration_branch": "jev-ticket-1",
            "status": "in_progress",
            "trajectory": [],
        }

        out = node_verify(state, workspace=ws, gatekeeper=gk)
        assert out.get("gate_status") == "verified"
        assert out.get("status") == "completed"
        ws.fast_forward_main.assert_called_once()
