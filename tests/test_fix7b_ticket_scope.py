"""Strict authorization for changes to pre-existing test files."""

import pytest

from jev.workspace import Workspace


@pytest.mark.parametrize(
    "file_path,ticket,authorized",
    [
        ("tests.py", "Fix the login bug, add more tests for cart", False),
        ("tests/api.py", "update the api docs", False),
        ("tests/test_login.py", "fix login flow", False),
        ("tests/test_cart.py", "update tests/test_cart.py", True),
        ("tests/test_cart.py", "update `test_cart.py`", True),
        ("test_cart.py", "update test_cart.py", True),
        ("tests/test_cart.py", "update old_test_cart.py", False),
        ("tests/test_cart.py", "update test_cart.py.bak", False),
    ],
)
def test_ticket_text_requires_whole_path_or_filename(file_path, ticket, authorized):
    ws = Workspace.__new__(Workspace)
    assert ws._is_test_file_in_ticket_scope(file_path, ticket=ticket) is authorized


@pytest.mark.parametrize("scope_kind", ["ticket_scope", "subgoal_scope"])
@pytest.mark.parametrize(
    "file_path,scope,authorized",
    [
        ("tests/test_cart.py", "tests/test_cart.py", True),
        ("tests/test_cart.py", "./tests/../tests/test_cart.py", True),
        ("tests/test_cart.py", "tests\\test_cart.py", True),
        ("tests/test_cart.py", "tests/", True),
        ("tests/unit/test_cart.py", "tests", True),
        ("tests/test_cart.py", "test_cart.py", False),
        ("tests/test_cart.py", "other/tests/test_cart.py", False),
        ("tests/test_cart.py", "test_cart", False),
        ("tests_extra/test_cart.py", "tests/", False),
    ],
)
def test_explicit_scope_requires_normalized_equality_or_directory_prefix(
    scope_kind, file_path, scope, authorized
):
    ws = Workspace.__new__(Workspace)
    assert ws._is_test_file_in_ticket_scope(file_path, **{scope_kind: [scope]}) is authorized


def test_ticket_dict_scope_uses_same_strict_rules():
    ws = Workspace.__new__(Workspace)
    assert ws._is_test_file_in_ticket_scope(
        "tests/test_cart.py", ticket={"scope": ["tests/"]}
    )
    assert not ws._is_test_file_in_ticket_scope(
        "tests/test_cart.py", ticket={"ticket_scope": ["test_cart.py"]}
    )
