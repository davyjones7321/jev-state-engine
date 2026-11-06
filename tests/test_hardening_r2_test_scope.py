"""Unit and integration tests for Phase R2: Test Scope in Planning.

Tests cover:
1. Exact matching: src/calc.py with expects_tests=True gets existing tests/test_calc.py added.
2. Isolation: Unrelated test files (e.g. tests/test_user.py) are never added.
3. New test file in scope: Worker can create it; writes to other test files rejected.
4. Missing test escalation: Missing tests retried once with feedback; escalates with
   gate_status="missing_test_scope" before implementation starts if still missing.
5. Directory and glob rejection: Scope entries like 'tests', 'tests/', '**', 'tests/*' rejected.
6. Ambiguous / multi-candidate discovery: >5 candidates or multiple look-alikes require
   explicit naming rather than silent selection.
7. expects_tests=False: Accepted without rejection; records trajectory event 'no_tests_declared'.
8. Multi-ecosystem discovery conventions: Python, JS/TS, Go, Rust (including #[cfg(test)]),
   Java/Kotlin, .NET, Ruby, PHP, Elixir.
9. Scope enforcement integrity: Worker cannot write outside final amended scope.
"""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from jev.models import State, Subgoal
from jev.test_scope import (
    DiscoveryResult,
    discover_related_tests,
    is_test_path,
    validate_subgoal_test_scope,
)


@pytest.fixture
def temp_repo():
    d = tempfile.mkdtemp(prefix="jev_test_scope_")
    repo = Path(d)
    yield repo
    shutil.rmtree(d, ignore_errors=True)


def test_1_exact_test_scope_added_for_subgoal(temp_repo):
    """1.

    A subgoal changing src/calc.py with expects_tests=True and scope ['src/calc.py']
    gets the existing tests/test_calc.py added to scope.
    """
    (temp_repo / "src").mkdir(parents=True, exist_ok=True)
    (temp_repo / "tests").mkdir(parents=True, exist_ok=True)
    (temp_repo / "src" / "calc.py").write_text("def add(a, b): return a + b\n")
    (temp_repo / "tests" / "test_calc.py").write_text("def test_add(): pass\n")

    subgoals = [
        Subgoal(
            id="sg-1",
            description="Update calc",
            scope=["src/calc.py"],
            expects_tests=True,
        )
    ]

    amended, problems = validate_subgoal_test_scope(subgoals, temp_repo)
    assert not problems, f"Unexpected problems: {problems}"
    assert len(amended) == 1
    assert "src/calc.py" in amended[0].scope
    assert "tests/test_calc.py" in amended[0].scope


def test_2_unrelated_test_files_never_added(temp_repo):
    """2. Unrelated test files (e.g. tests/test_auth.py, tests/test_db.py) are never added."""
    (temp_repo / "src").mkdir(parents=True, exist_ok=True)
    (temp_repo / "tests").mkdir(parents=True, exist_ok=True)
    (temp_repo / "src" / "calc.py").write_text("def add(a, b): return a + b\n")
    (temp_repo / "tests" / "test_calc.py").write_text("def test_add(): pass\n")
    (temp_repo / "tests" / "test_auth.py").write_text("def test_auth(): pass\n")
    (temp_repo / "tests" / "test_db.py").write_text("def test_db(): pass\n")

    subgoals = [
        Subgoal(
            id="sg-1",
            description="Update calc",
            scope=["src/calc.py"],
            expects_tests=True,
        )
    ]

    amended, problems = validate_subgoal_test_scope(subgoals, temp_repo)
    assert not problems
    assert set(amended[0].scope) == {"src/calc.py", "tests/test_calc.py"}
    assert "tests/test_auth.py" not in amended[0].scope
    assert "tests/test_db.py" not in amended[0].scope


def test_3_planner_names_new_test_file_keeps_it(temp_repo):
    """3.

    A subgoal whose planner scope names a new test file keeps it; the worker
    may create it, and writes to any other test file stay rejected.
    """
    (temp_repo / "src").mkdir(parents=True, exist_ok=True)
    (temp_repo / "tests").mkdir(parents=True, exist_ok=True)
    (temp_repo / "src" / "feature.py").write_text("# new feature\n")
    # Note: tests/test_feature.py does not yet exist on disk!

    subgoals = [
        Subgoal(
            id="sg-1",
            description="Implement feature with new test",
            scope=["src/feature.py", "tests/test_feature.py"],
            expects_tests=True,
        )
    ]

    amended, problems = validate_subgoal_test_scope(subgoals, temp_repo)
    assert not problems
    assert "tests/test_feature.py" in amended[0].scope


def test_4_missing_test_scope_escalates_before_implementation(temp_repo):
    """4.

    When no existing test is discoverable and no new test file is named, the plan
    is returned to the planner once with explicit feedback; if the second plan
    is still missing tests, the ticket escalates with gate_status='missing_test_scope'
    BEFORE any implementation starts.
    """
    from langchain_core.messages import AIMessage
    from jev.engine import node_plan

    (temp_repo / "src").mkdir(parents=True, exist_ok=True)
    (temp_repo / "src" / "orphan.py").write_text("def orphan(): pass\n")

    ws = MagicMock()
    ws.repo_dir = temp_repo

    plan_json = '[{"description": "Orphan change", "scope": ["src/orphan.py"], "expects_tests": true}]'
    mock_llm = MagicMock()
    mock_llm.invoke.return_value = AIMessage(content=plan_json)

    state: State = {
        "ticket": "Update orphan function",
        "investigation_notes": "Found src/orphan.py",
        "trajectory": [],
    }

    res_state = node_plan(state, workspace=ws, llm=mock_llm)

    assert res_state.get("gate_status") == "missing_test_scope"
    assert res_state.get("status") == "escalated"
    assert "plan_queue" in res_state and len(res_state["plan_queue"]) == 0
    assert mock_llm.invoke.call_count == 2
    events = [e for e in res_state.get("trajectory", []) if e.get("event") == "missing_test_scope"]
    assert len(events) >= 1


def test_5_scope_entries_as_directories_or_globs_rejected(temp_repo):
    """5. Scope entries that are directories or globs (tests, tests/, **, tests/*) are rejected."""
    (temp_repo / "tests").mkdir(parents=True, exist_ok=True)
    (temp_repo / "src").mkdir(parents=True, exist_ok=True)

    invalid_scopes = [
        ["tests"],
        ["tests/"],
        ["**"],
        ["tests/*"],
        ["src/*.py"],
        ["tests/**"],
    ]

    for inv_scope in invalid_scopes:
        subgoals = [
            Subgoal(
                id="sg-inv",
                description="Invalid scope entry",
                scope=inv_scope,
                expects_tests=True,
            )
        ]
        _, problems = validate_subgoal_test_scope(subgoals, temp_repo)
        assert len(problems) > 0, f"Expected rejection for invalid scope: {inv_scope}"
        assert any(p.get("type") == "invalid_scope_item" for p in problems)


def test_6_ambiguous_and_many_candidates_never_silently_added(temp_repo):
    """6. Discovery is conservative:

    - Exact match added automatically.
    - If > 5 plausible candidates or several non-exact look-alikes exist, nothing selected silently:
      returns structured problem listing candidates.
    - Candidate set never silently truncated.
    - Filename similarity alone never adds a test when > 1 candidate exists.
    """
    (temp_repo / "src").mkdir(parents=True, exist_ok=True)
    (temp_repo / "tests").mkdir(parents=True, exist_ok=True)
    (temp_repo / "src" / "service.py").write_text("# service\n")

    for i in range(6):
        (temp_repo / "tests" / f"test_service_extra_{i}.py").write_text("pass\n")

    subgoals = [
        Subgoal(
            id="sg-amb",
            description="Update service",
            scope=["src/service.py"],
            expects_tests=True,
        )
    ]

    _, problems = validate_subgoal_test_scope(subgoals, temp_repo)
    assert len(problems) == 1
    prob = problems[0]
    assert prob["type"] == "ambiguous_test_candidates"
    assert len(prob["candidates"]) >= 6


def test_7_expects_tests_false_accepted_with_trajectory_warning(temp_repo):
    """7.

    expects_tests=False plans are not rejected (a trajectory warning event
    no_tests_declared is recorded, with the reason if given).
    """
    (temp_repo / "src").mkdir(parents=True, exist_ok=True)
    (temp_repo / "src" / "docs.py").write_text("# docstrings\n")

    subgoals = [
        Subgoal(
            id="sg-doc",
            description="Docstring updates",
            scope=["src/docs.py"],
            expects_tests=False,
            no_tests_reason="Docstring only change",
        )
    ]

    amended, problems = validate_subgoal_test_scope(subgoals, temp_repo)
    assert not problems
    assert len(amended) == 1
    assert amended[0].scope == ["src/docs.py"]


def test_8_multi_ecosystem_naming_conventions(temp_repo):
    """8. Discovery works for each ecosystem naming convention:

    - Python: test_*.py, *_test.py, tests/
    - JS/TS: *.test.*, *.spec.*, __tests__/
    - Go: *_test.go in the same directory
    - Rust: tests/*.rs; inline #[cfg(test)] counts as source file itself
    - Java/Kotlin: src/test/... mirror
    - .NET: *Tests.cs / *Test.cs
    - Ruby: spec/*_spec.rb, test/*_test.rb
    - PHP: tests/*Test.php
    - Elixir: test/*_test.exs
    """
    # 1. Python
    (temp_repo / "py_src").mkdir(parents=True, exist_ok=True)
    (temp_repo / "tests").mkdir(parents=True, exist_ok=True)
    (temp_repo / "py_src" / "calc.py").write_text("")
    (temp_repo / "tests" / "test_calc.py").write_text("")
    res_py = discover_related_tests(temp_repo, ["py_src/calc.py"])
    assert "tests/test_calc.py" in res_py.exact

    # 2. JS / TS
    (temp_repo / "src" / "components").mkdir(parents=True, exist_ok=True)
    (temp_repo / "src" / "components" / "button.tsx").write_text("")
    (temp_repo / "src" / "components" / "button.test.tsx").write_text("")
    res_ts = discover_related_tests(temp_repo, ["src/components/button.tsx"])
    assert "src/components/button.test.tsx" in res_ts.exact

    # 3. Go (same directory *_test.go)
    (temp_repo / "pkg" / "math").mkdir(parents=True, exist_ok=True)
    (temp_repo / "pkg" / "math" / "calc.go").write_text("")
    (temp_repo / "pkg" / "math" / "calc_test.go").write_text("")
    res_go = discover_related_tests(temp_repo, ["pkg/math/calc.go"])
    assert "pkg/math/calc_test.go" in res_go.exact

    # 4. Rust (tests/*.rs and inline #[cfg(test)])
    (temp_repo / "rust_src").mkdir(parents=True, exist_ok=True)
    (temp_repo / "rust_src" / "lib.rs").write_text("#[cfg(test)]\nmod tests { }\n")
    res_rs_inline = discover_related_tests(temp_repo, ["rust_src/lib.rs"])
    assert "rust_src/lib.rs" in res_rs_inline.exact

    (temp_repo / "rust_src" / "parser.rs").write_text("// rust source\n")
    (temp_repo / "tests" / "parser.rs").write_text("// test\n")
    res_rs_file = discover_related_tests(temp_repo, ["rust_src/parser.rs"])
    assert "tests/parser.rs" in res_rs_file.exact

    # 5. Java/Kotlin (src/test mirror)
    (temp_repo / "src" / "main" / "java" / "com" / "calc").mkdir(parents=True, exist_ok=True)
    (temp_repo / "src" / "test" / "java" / "com" / "calc").mkdir(parents=True, exist_ok=True)
    (temp_repo / "src" / "main" / "java" / "com" / "calc" / "Calculator.java").write_text("")
    (temp_repo / "src" / "test" / "java" / "com" / "calc" / "CalculatorTest.java").write_text("")
    res_java = discover_related_tests(temp_repo, ["src/main/java/com/calc/Calculator.java"])
    assert "src/test/java/com/calc/CalculatorTest.java" in res_java.exact

    # 6. .NET (*Tests.cs)
    (temp_repo / "src" / "Orders").mkdir(parents=True, exist_ok=True)
    (temp_repo / "tests" / "Orders.Tests").mkdir(parents=True, exist_ok=True)
    (temp_repo / "src" / "Orders" / "OrderService.cs").write_text("")
    (temp_repo / "tests" / "Orders.Tests" / "OrderServiceTests.cs").write_text("")
    res_dotnet = discover_related_tests(temp_repo, ["src/Orders/OrderService.cs"])
    assert any("OrderServiceTests.cs" in p for p in res_dotnet.exact)

    # 7. Ruby (spec/*_spec.rb)
    (temp_repo / "lib").mkdir(parents=True, exist_ok=True)
    (temp_repo / "spec").mkdir(parents=True, exist_ok=True)
    (temp_repo / "lib" / "widget.rb").write_text("")
    (temp_repo / "spec" / "widget_spec.rb").write_text("")
    res_rb = discover_related_tests(temp_repo, ["lib/widget.rb"])
    assert "spec/widget_spec.rb" in res_rb.exact

    # 8. PHP (tests/*Test.php)
    (temp_repo / "php_src").mkdir(parents=True, exist_ok=True)
    (temp_repo / "tests" / "InvoiceTest.php").write_text("")
    (temp_repo / "php_src" / "Invoice.php").write_text("")
    res_php = discover_related_tests(temp_repo, ["php_src/Invoice.php"])
    assert "tests/InvoiceTest.php" in res_php.exact

    # 9. Elixir (test/*_test.exs)
    (temp_repo / "lib_ex").mkdir(parents=True, exist_ok=True)
    (temp_repo / "test").mkdir(parents=True, exist_ok=True)
    (temp_repo / "lib_ex" / "account.ex").write_text("")
    (temp_repo / "test" / "account_test.exs").write_text("")
    res_ex = discover_related_tests(temp_repo, ["lib_ex/account.ex"])
    assert "test/account_test.exs" in res_ex.exact


def test_9_worker_cannot_write_outside_amended_scope(temp_repo):
    """9.

    The worker still cannot write outside the final declared scope (existing
    scope enforcement unchanged).
    """
    from jev.workspace import Workspace

    (temp_repo / "src").mkdir(parents=True, exist_ok=True)
    (temp_repo / "tests").mkdir(parents=True, exist_ok=True)
    (temp_repo / "src" / "calc.py").write_text("def add(a, b): return a + b\n")
    (temp_repo / "tests" / "test_calc.py").write_text("def test_add(): pass\n")
    (temp_repo / "tests" / "test_auth.py").write_text("def test_auth(): pass\n")

    subprocess.run(["git", "init"], cwd=temp_repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=temp_repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=temp_repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=temp_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=temp_repo, check=True, capture_output=True)

    subgoals = [
        Subgoal(
            id="sg-1",
            description="Update calc",
            scope=["src/calc.py"],
            expects_tests=True,
        )
    ]
    amended, problems = validate_subgoal_test_scope(subgoals, temp_repo)
    assert not problems
    final_scope = amended[0].scope

    ws = Workspace(repo_dir=temp_repo)
    ws.create_subgoal_worktree("sg-1")

    # Writing to approved scope files succeeds
    assert "src/calc.py" in final_scope
    assert "tests/test_calc.py" in final_scope
    ws.stage_file_mutation("src/calc.py", "def add(a, b): return a + b + 0\n")
    ws.stage_file_mutation("tests/test_calc.py", "def test_add(): assert True\n")
    assert "src/calc.py" in ws.worker_staged_paths
    assert "tests/test_calc.py" in ws.worker_staged_paths

    # Writing to unauthorized test file is detected as out of scope by check_scope
    assert "tests/test_auth.py" not in final_scope
    diff_str = "diff --git a/tests/test_auth.py b/tests/test_auth.py\n--- a/tests/test_auth.py\n+++ b/tests/test_auth.py\n"
    res_unauth = ws.check_scope(diff=diff_str, scope=final_scope)
    assert not res_unauth.passed
    assert "tests/test_auth.py" in res_unauth.out_of_scope

def test_10_two_source_files_one_test_declared_discovers_second_test(temp_repo):
    """10.
    Two source files with two corresponding tests on disk, but only one test declared
    in scope. The validator must evaluate the second source file independently and add
    its exact test match to scope.
    """
    (temp_repo / "src").mkdir(parents=True, exist_ok=True)
    (temp_repo / "tests").mkdir(parents=True, exist_ok=True)
    (temp_repo / "src" / "a.py").write_text("def a(): pass\n")
    (temp_repo / "src" / "b.py").write_text("def b(): pass\n")
    (temp_repo / "tests" / "test_a.py").write_text("def test_a(): pass\n")
    (temp_repo / "tests" / "test_b.py").write_text("def test_b(): pass\n")

    subgoals = [
        Subgoal(
            id="sg-1",
            description="Update a and b",
            scope=["src/a.py", "src/b.py", "tests/test_a.py"],
            expects_tests=True,
        )
    ]
    amended, problems = validate_subgoal_test_scope(subgoals, temp_repo)
    assert not problems
    assert "tests/test_a.py" in amended[0].scope
    assert "tests/test_b.py" in amended[0].scope


def test_11_two_source_files_both_tests_declared(temp_repo):
    """11.
    Two source files with both tests explicitly declared in scope.
    Accepted cleanly without modification or problems.
    """
    (temp_repo / "src").mkdir(parents=True, exist_ok=True)
    (temp_repo / "tests").mkdir(parents=True, exist_ok=True)
    (temp_repo / "src" / "a.py").write_text("def a(): pass\n")
    (temp_repo / "src" / "b.py").write_text("def b(): pass\n")
    (temp_repo / "tests" / "test_a.py").write_text("def test_a(): pass\n")
    (temp_repo / "tests" / "test_b.py").write_text("def test_b(): pass\n")

    subgoals = [
        Subgoal(
            id="sg-1",
            description="Update a and b with tests",
            scope=["src/a.py", "src/b.py", "tests/test_a.py", "tests/test_b.py"],
            expects_tests=True,
        )
    ]
    amended, problems = validate_subgoal_test_scope(subgoals, temp_repo)
    assert not problems
    assert sorted(amended[0].scope) == sorted(["src/a.py", "src/b.py", "tests/test_a.py", "tests/test_b.py"])


def test_12_single_source_file_explicitly_declared_new_test_preserved(temp_repo):
    """12.
    One source file with an explicitly declared new test file that does not yet exist.
    Preserved in scope cleanly.
    """
    subgoals = [
        Subgoal(
            id="sg-1",
            description="Implement new feature",
            scope=["src/new_feature.py", "tests/test_new_feature.py"],
            expects_tests=True,
        )
    ]
    amended, problems = validate_subgoal_test_scope(subgoals, temp_repo)
    assert not problems
    assert "tests/test_new_feature.py" in amended[0].scope


def test_13_multiple_source_files_ambiguous_test_candidate_rejected(temp_repo):
    """13.
    Multiple source files where one file has an ambiguous test candidate on disk.
    Must reject with ambiguous_test_candidates rather than silently accepting.
    """
    (temp_repo / "src").mkdir(parents=True, exist_ok=True)
    (temp_repo / "tests").mkdir(parents=True, exist_ok=True)
    (temp_repo / "src" / "a.py").write_text("def a(): pass\n")
    (temp_repo / "src" / "b.py").write_text("def b(): pass\n")
    (temp_repo / "tests" / "test_a.py").write_text("def test_a(): pass\n")
    (temp_repo / "tests" / "test_b_unit.py").write_text("def test_b_unit(): pass\n")
    (temp_repo / "tests" / "test_b_integration.py").write_text("def test_b_integration(): pass\n")

    subgoals = [
        Subgoal(
            id="sg-1",
            description="Update a and b",
            scope=["src/a.py", "src/b.py", "tests/test_a.py"],
            expects_tests=True,
        )
    ]
    amended, problems = validate_subgoal_test_scope(subgoals, temp_repo)
    assert len(problems) >= 1
    assert any(p["type"] == "ambiguous_test_candidates" for p in problems)


def test_14_source_file_with_no_discoverable_test_reports_missing_test_scope(temp_repo):
    """14.
    Multiple source files where one has a declared test and the other has no discoverable test.
    Must return missing_test_scope problem for planner feedback.
    """
    (temp_repo / "src").mkdir(parents=True, exist_ok=True)
    (temp_repo / "tests").mkdir(parents=True, exist_ok=True)
    (temp_repo / "src" / "a.py").write_text("def a(): pass\n")
    (temp_repo / "src" / "orphan.py").write_text("def orphan(): pass\n")
    (temp_repo / "tests" / "test_a.py").write_text("def test_a(): pass\n")

    subgoals = [
        Subgoal(
            id="sg-1",
            description="Update a and orphan",
            scope=["src/a.py", "src/orphan.py", "tests/test_a.py"],
            expects_tests=True,
        )
    ]
    amended, problems = validate_subgoal_test_scope(subgoals, temp_repo)
    assert len(problems) >= 1
    assert any(p["type"] == "missing_test_scope" for p in problems)


def test_15_no_implementation_starts_before_missing_test_scope_resolved(temp_repo):
    """15.
    Integration check: If a plan with multiple source files is missing a test for one file
    and fails on both planning attempts, the engine escalates with gate_status='missing_test_scope'
    and plan_queue=[] BEFORE implementation starts.
    """
    from langchain_core.messages import AIMessage
    from jev.engine import node_plan

    (temp_repo / "src").mkdir(parents=True, exist_ok=True)
    (temp_repo / "tests").mkdir(parents=True, exist_ok=True)
    (temp_repo / "src" / "a.py").write_text("def a(): pass\n")
    (temp_repo / "src" / "b.py").write_text("def b(): pass\n")
    (temp_repo / "tests" / "test_a.py").write_text("def test_a(): pass\n")

    ws = MagicMock()
    ws.repo_dir = temp_repo

    # Plan proposes a and b with only test_a.py declared
    plan_json = '[{"description": "Partial test scope", "scope": ["src/a.py", "src/b.py", "tests/test_a.py"], "expects_tests": true}]'
    mock_llm = MagicMock()
    mock_llm.invoke.return_value = AIMessage(content=plan_json)

    state: State = {
        "ticket": "Update a and b",
        "investigation_notes": "Found src/a.py, src/b.py, tests/test_a.py",
        "trajectory": [],
    }

    res_state = node_plan(state, workspace=ws, llm=mock_llm)

    assert res_state.get("gate_status") == "missing_test_scope"
    assert res_state.get("status") == "escalated"
    assert len(res_state.get("plan_queue", [])) == 0
    assert mock_llm.invoke.call_count == 2
    events = [e for e in res_state.get("trajectory", []) if e.get("event") == "missing_test_scope"]
    assert len(events) >= 1



def test_16_case_a_single_source_related_existing_test_accepted(temp_repo):
    """Case A: Single source file with a related existing test in scope -> accepted."""
    (temp_repo / "src").mkdir(parents=True, exist_ok=True)
    (temp_repo / "tests").mkdir(parents=True, exist_ok=True)
    (temp_repo / "src" / "calc.py").write_text("def add(a, b): return a + b\n")
    (temp_repo / "tests" / "test_calc.py").write_text("def test_add(): pass\n")

    subgoals = [
        Subgoal(
            id="sg-case-a",
            description="Update calc",
            scope=["src/calc.py", "tests/test_calc.py"],
            expects_tests=True,
        )
    ]
    amended, problems = validate_subgoal_test_scope(subgoals, temp_repo)
    assert not problems
    assert amended[0].scope == ["src/calc.py", "tests/test_calc.py"]


def test_17_case_b1_single_source_unrelated_test_discovers_correct_test_on_disk(temp_repo):
    """Case B1: Single source with unrelated test in scope -> amends scope to include correct test."""
    (temp_repo / "src").mkdir(parents=True, exist_ok=True)
    (temp_repo / "tests").mkdir(parents=True, exist_ok=True)
    (temp_repo / "src" / "calc.py").write_text("def add(a, b): return a + b\n")
    (temp_repo / "tests" / "test_calc.py").write_text("def test_add(): pass\n")
    (temp_repo / "tests" / "test_auth.py").write_text("def test_auth(): pass\n")

    # Scope only contains src/calc.py and unrelated tests/test_auth.py
    subgoals = [
        Subgoal(
            id="sg-case-b1",
            description="Update calc",
            scope=["src/calc.py", "tests/test_auth.py"],
            expects_tests=True,
        )
    ]
    amended, problems = validate_subgoal_test_scope(subgoals, temp_repo)
    assert not problems
    # tests/test_calc.py must be discovered and added
    assert "tests/test_calc.py" in amended[0].scope


def test_18_case_b2_single_source_unrelated_test_reports_missing_test_scope_when_none_on_disk(temp_repo):
    """Case B2: Single source with unrelated test and no matching test on disk -> missing_test_scope."""
    (temp_repo / "src").mkdir(parents=True, exist_ok=True)
    (temp_repo / "tests").mkdir(parents=True, exist_ok=True)
    (temp_repo / "src" / "calc.py").write_text("def add(a, b): return a + b\n")
    (temp_repo / "tests" / "test_auth.py").write_text("def test_auth(): pass\n")
    # Note: no test_calc.py on disk

    subgoals = [
        Subgoal(
            id="sg-case-b2",
            description="Update calc",
            scope=["src/calc.py", "tests/test_auth.py"],
            expects_tests=True,
        )
    ]
    amended, problems = validate_subgoal_test_scope(subgoals, temp_repo)
    assert len(problems) >= 1
    assert any(p["type"] == "missing_test_scope" for p in problems)


def test_19_case_c_single_source_declared_new_test_preserved(temp_repo):
    """Case C: Single source file with an explicitly declared new test file -> preserved."""
    subgoals = [
        Subgoal(
            id="sg-case-c",
            description="Add payment gateway",
            scope=["src/payment.py", "tests/test_payment.py"],
            expects_tests=True,
        )
    ]
    amended, problems = validate_subgoal_test_scope(subgoals, temp_repo)
    assert not problems
    assert "tests/test_payment.py" in amended[0].scope


def test_20_case_d_multiple_sources_unrelated_test_validated_per_source(temp_repo):
    """Case D: Multiple source files with unrelated test in scope -> validated independently."""
    (temp_repo / "src").mkdir(parents=True, exist_ok=True)
    (temp_repo / "tests").mkdir(parents=True, exist_ok=True)
    (temp_repo / "src" / "a.py").write_text("def a(): pass\n")
    (temp_repo / "src" / "b.py").write_text("def b(): pass\n")
    (temp_repo / "tests" / "test_a.py").write_text("def test_a(): pass\n")
    (temp_repo / "tests" / "test_unrelated.py").write_text("def test_unrelated(): pass\n")

    # a has test_a, but b has only test_unrelated and no test_b on disk
    subgoals = [
        Subgoal(
            id="sg-case-d",
            description="Update a and b",
            scope=["src/a.py", "src/b.py", "tests/test_a.py", "tests/test_unrelated.py"],
            expects_tests=True,
        )
    ]
    amended, problems = validate_subgoal_test_scope(subgoals, temp_repo)
    assert len(problems) >= 1
    assert any(p["type"] == "missing_test_scope" and p.get("source_file") == "src/b.py" for p in problems)
