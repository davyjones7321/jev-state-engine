"""tests/test_hardening_r2_audit_corrections.py

Regression test suite for R2 audit corrections:
1. llm=None planning path validates test scope and amends/escalates properly.
2. Monorepo cross-package filename collisions: tests in package B never satisfy package A.
3. Monorepo multi-package scope: a single test cannot satisfy multiple unrelated packages.
4. Monorepo package-local test discovery: exact match inside package is selected unambiguously.
5. Path safety: traversal (../), absolute paths, and drive paths are strictly rejected.
6. Vendored / dependency directories (node_modules, .venv, etc.) are excluded from discovery.
"""

import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from jev.engine import node_plan
from jev.models import State, Subgoal
from jev.test_scope import (
    discover_related_tests,
    is_test_path,
    matches_test_file,
    validate_subgoal_test_scope,
)


@pytest.fixture
def temp_repo():
    d = tempfile.mkdtemp(prefix="jev_r2_audit_")
    repo = Path(d)
    yield repo
    shutil.rmtree(d, ignore_errors=True)


# -------------------------------------------------------------------------
# A. llm=None planning path validation
# -------------------------------------------------------------------------

def test_llm_none_plan_missing_test_scope_escalates(temp_repo):
    """When llm=None and a supplied plan has missing test scope, node_plan escalates."""
    (temp_repo / "src").mkdir(parents=True, exist_ok=True)
    (temp_repo / "src" / "orphan.py").write_text("def orphan(): pass\n")

    ws = MagicMock()
    ws.repo_dir = temp_repo

    state: State = {
        "ticket": "Update orphan function",
        "plan_queue": [
            Subgoal(
                id="sg-1",
                description="Change orphan",
                scope=["src/orphan.py"],
                expects_tests=True,
            )
        ],
        "trajectory": [],
    }

    res_state = node_plan(state, workspace=ws, llm=None)
    assert res_state.get("gate_status") == "missing_test_scope", (
        f"Expected gate_status='missing_test_scope', got {res_state.get('gate_status')}"
    )
    assert res_state.get("status") == "escalated"


def test_llm_none_plan_exact_test_discovered_and_amended(temp_repo):
    """When llm=None and a supplied plan omits an existing test on disk, it is discovered and amended."""
    (temp_repo / "src").mkdir(parents=True, exist_ok=True)
    (temp_repo / "tests").mkdir(parents=True, exist_ok=True)
    (temp_repo / "src" / "calc.py").write_text("def add(a, b): return a + b\n")
    (temp_repo / "tests" / "test_calc.py").write_text("def test_add(): pass\n")

    ws = MagicMock()
    ws.repo_dir = temp_repo

    state: State = {
        "ticket": "Update calc",
        "plan_queue": [
            Subgoal(
                id="sg-1",
                description="Update calc",
                scope=["src/calc.py"],
                expects_tests=True,
            )
        ],
        "trajectory": [],
    }

    res_state = node_plan(state, workspace=ws, llm=None)
    assert res_state.get("gate_status") is None
    assert len(res_state["plan_queue"]) == 1
    assert "tests/test_calc.py" in res_state["plan_queue"][0].scope


# -------------------------------------------------------------------------
# B. Monorepo cross-package filename collisions
# -------------------------------------------------------------------------

def test_monorepo_test_in_package_b_does_not_satisfy_package_a(temp_repo):
    """A test file in packages/b cannot satisfy a source file in packages/a."""
    (temp_repo / "packages" / "a" / "src").mkdir(parents=True, exist_ok=True)
    (temp_repo / "packages" / "b" / "tests").mkdir(parents=True, exist_ok=True)

    src_a = "packages/a/src/client.py"
    test_b = "packages/b/tests/test_client.py"
    (temp_repo / src_a).write_text("class ClientA: pass\n")
    (temp_repo / test_b).write_text("def test_client_b(): pass\n")

    # 1. matches_test_file must be False across different package roots
    assert not matches_test_file(src_a, test_b), "Test in package B must not match source in package A"

    # 2. discover_related_tests for package A must not find package B's test
    discovery = discover_related_tests(temp_repo, [src_a])
    assert test_b not in discovery.exact
    assert test_b not in discovery.ambiguous

    # 3. validate_subgoal_test_scope must report missing_test_scope for package A
    subgoals = [
        Subgoal(
            id="sg-1",
            description="Change package A client",
            scope=[src_a],
            expects_tests=True,
        )
    ]
    amended, problems = validate_subgoal_test_scope(subgoals, temp_repo)
    assert any(p.get("type") == "missing_test_scope" for p in problems), (
        f"Expected missing_test_scope problem, got: {problems}"
    )


def test_monorepo_single_test_cannot_satisfy_multiple_unrelated_packages(temp_repo):
    """Declaring test for package A cannot silently satisfy an uncovered source file in package B."""
    (temp_repo / "packages" / "a" / "src").mkdir(parents=True, exist_ok=True)
    (temp_repo / "packages" / "a" / "tests").mkdir(parents=True, exist_ok=True)
    (temp_repo / "packages" / "b" / "src").mkdir(parents=True, exist_ok=True)

    src_a = "packages/a/src/client.py"
    test_a = "packages/a/tests/test_client.py"
    src_b = "packages/b/src/client.py"

    (temp_repo / src_a).write_text("class ClientA: pass\n")
    (temp_repo / test_a).write_text("def test_client_a(): pass\n")
    (temp_repo / src_b).write_text("class ClientB: pass\n")

    subgoals = [
        Subgoal(
            id="sg-1",
            description="Change client in both packages",
            scope=[src_a, src_b, test_a],  # test_b is missing!
            expects_tests=True,
        )
    ]

    amended, problems = validate_subgoal_test_scope(subgoals, temp_repo)
    assert any(p.get("type") == "missing_test_scope" and p.get("source_file") == src_b for p in problems), (
        f"Expected missing_test_scope for {src_b}, got: {problems}"
    )


def test_monorepo_exact_tests_in_both_packages_discovered_unambiguously(temp_repo):
    """When both packages have matching test files, discovery for package A selects package A's test."""
    (temp_repo / "packages" / "a" / "src").mkdir(parents=True, exist_ok=True)
    (temp_repo / "packages" / "a" / "tests").mkdir(parents=True, exist_ok=True)
    (temp_repo / "packages" / "b" / "src").mkdir(parents=True, exist_ok=True)
    (temp_repo / "packages" / "b" / "tests").mkdir(parents=True, exist_ok=True)

    src_a = "packages/a/src/client.py"
    test_a = "packages/a/tests/test_client.py"
    src_b = "packages/b/src/client.py"
    test_b = "packages/b/tests/test_client.py"

    (temp_repo / src_a).write_text("class ClientA: pass\n")
    (temp_repo / test_a).write_text("def test_client_a(): pass\n")
    (temp_repo / src_b).write_text("class ClientB: pass\n")
    (temp_repo / test_b).write_text("def test_client_b(): pass\n")

    discovery_a = discover_related_tests(temp_repo, [src_a])
    assert discovery_a.exact == [test_a]
    assert not discovery_a.ambiguous

    discovery_b = discover_related_tests(temp_repo, [src_b])
    assert discovery_b.exact == [test_b]
    assert not discovery_b.ambiguous


# -------------------------------------------------------------------------
# C. Path safety: traversal and absolute paths
# -------------------------------------------------------------------------

@pytest.mark.parametrize(
    "bad_path",
    [
        "../../etc/passwd",
        "src/../../secret.py",
        "/etc/shadow",
        "C:\\Windows\\System32\\calc.exe",
        "D:/root/secret.txt",
    ],
)
def test_path_safety_traversal_and_absolute_paths_rejected(temp_repo, bad_path):
    """Scope items containing traversal (../) or absolute / drive paths must be rejected."""
    subgoals = [
        Subgoal(
            id="sg-bad",
            description="Malicious path subgoal",
            scope=[bad_path],
            expects_tests=False,
        )
    ]

    amended, problems = validate_subgoal_test_scope(subgoals, temp_repo)
    assert len(problems) > 0, f"Expected path '{bad_path}' to be rejected as invalid_scope_item"
    assert any(p.get("type") == "invalid_scope_item" for p in problems)


# -------------------------------------------------------------------------
# D. Vendored / dependency directories excluded from discovery
# -------------------------------------------------------------------------

def test_vendor_directories_excluded_from_discovery(temp_repo):
    """Tests located inside node_modules, .venv, target, etc. must never be discovered for user code."""
    (temp_repo / "src").mkdir(parents=True, exist_ok=True)
    (temp_repo / "src" / "calc.py").write_text("def add(a, b): return a + b\n")

    # Test file inside node_modules
    (temp_repo / "node_modules" / "pkg" / "tests").mkdir(parents=True, exist_ok=True)
    (temp_repo / "node_modules" / "pkg" / "tests" / "test_calc.py").write_text("def test_calc(): pass\n")

    # Test file inside .venv
    (temp_repo / ".venv" / "lib" / "site-packages").mkdir(parents=True, exist_ok=True)
    (temp_repo / ".venv" / "lib" / "site-packages" / "test_calc.py").write_text("def test_calc(): pass\n")

    discovery = discover_related_tests(temp_repo, ["src/calc.py"])
    assert not discovery.exact, f"Vendored tests must not be discovered as exact: {discovery.exact}"
    assert not discovery.ambiguous, f"Vendored tests must not be discovered as ambiguous: {discovery.ambiguous}"
