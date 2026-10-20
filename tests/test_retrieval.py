from pathlib import Path

import pytest

from jev.retrieval import AstWorkspaceRetriever, RetrievalLimits


def test_extracts_symbols_definitions_imports_and_callers(tmp_path: Path) -> None:
    (tmp_path / "sample.py").write_text(
        "import os\nfrom math import sqrt as root\n\nclass Box:\n    def run(self):\n        return helper()\n\ndef helper():\n    return os.path.join('a', 'b')\n",
        encoding="utf-8",
    )

    result = AstWorkspaceRetriever(tmp_path).retrieve()

    assert {item.name for item in result.symbols} == {"Box", "run", "helper"}
    assert {item.name for item in result.definitions} == {"Box", "run", "helper"}
    assert {item.name for item in result.imports} == {"os", "sqrt"}
    assert any(item.name == "helper" and item.caller == "Box.run" for item in result.references)


def test_symbol_filter_and_bounds_are_applied(tmp_path: Path) -> None:
    (tmp_path / "many.py").write_text("\n".join(f"def fn_{index}(): pass" for index in range(8)), encoding="utf-8")

    result = AstWorkspaceRetriever(tmp_path, limits=RetrievalLimits(max_items=2)).retrieve(symbol="fn_7")

    assert [item.name for item in result.symbols] == ["fn_7"]
    assert len(result.definitions) <= 1
    assert result.truncated is False


def test_malformed_python_is_reported_without_stopping_other_files(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text("def broken(:\n", encoding="utf-8")
    (tmp_path / "good.py").write_text("def valid(): pass\n", encoding="utf-8")

    result = AstWorkspaceRetriever(tmp_path).retrieve()

    assert any(item.name == "valid" for item in result.symbols)
    assert len(result.issues) == 1
    assert result.issues[0].path == "bad.py"


def test_path_traversal_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        AstWorkspaceRetriever(tmp_path).retrieve(path="../outside.py")


def test_retrieval_does_not_mutate_files(tmp_path: Path) -> None:
    source = tmp_path / "source.py"
    source.write_text("def keep(): pass\n", encoding="utf-8")
    before = source.read_bytes()

    AstWorkspaceRetriever(tmp_path).retrieve()

    assert source.read_bytes() == before
