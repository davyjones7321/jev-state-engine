"""Bounded, read-only structural retrieval for Python workspaces.

The module deliberately returns metadata rather than source text.  It parses only
Python files under the configured worktree, never imports discovered code, and
continues past unreadable or malformed files with a bounded diagnostic.
"""

from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Protocol, Sequence


MAX_FILE_BYTES = 256_000
MAX_FILES = 500
MAX_ITEMS = 1_000


@dataclass(frozen=True)
class RetrievalLimits:
    """Resource limits applied to one retrieval operation."""

    max_files: int = MAX_FILES
    max_file_bytes: int = MAX_FILE_BYTES
    max_items: int = MAX_ITEMS

    def __post_init__(self) -> None:
        if min(self.max_files, self.max_file_bytes, self.max_items) <= 0:
            raise ValueError("retrieval limits must be positive")


@dataclass(frozen=True)
class Symbol:
    name: str
    kind: str
    path: str
    line: int
    end_line: int
    qualified_name: str


@dataclass(frozen=True)
class Definition:
    name: str
    path: str
    line: int
    end_line: int
    qualified_name: str


@dataclass(frozen=True)
class Import:
    name: str
    path: str
    line: int
    alias: Optional[str] = None
    module: Optional[str] = None


@dataclass(frozen=True)
class Reference:
    name: str
    path: str
    line: int
    caller: Optional[str] = None
    kind: str = "reference"


@dataclass(frozen=True)
class RetrievalIssue:
    path: str
    message: str


@dataclass(frozen=True)
class RetrievalResult:
    symbols: tuple[Symbol, ...] = ()
    definitions: tuple[Definition, ...] = ()
    imports: tuple[Import, ...] = ()
    references: tuple[Reference, ...] = ()
    issues: tuple[RetrievalIssue, ...] = ()
    files_scanned: int = 0
    truncated: bool = False


class StructureRetriever(Protocol):
    """Small injectable contract for future engine consumers."""

    def retrieve(
        self, *, path: str | Path | None = None, symbol: str | None = None, query: str | None = None
    ) -> RetrievalResult: ...


ReadText = Callable[[Path], str]


def is_secret_path(path: str | Path) -> bool:
    name = Path(path).name.lower()
    parts = [part.lower() for part in Path(path).parts]
    if any(part in (".aws", ".kube", ".ssh") for part in parts):
        return True
    if name.startswith(".env") or name in ("credentials.json", "secrets.json", "id_rsa", "id_dsa", "id_ed25519"):
        return True
    if name.endswith((".pem", ".key", ".pfx", ".p12", ".pkcs12")):
        return True
    return False


class AstWorkspaceRetriever:
    """Read-only AST retriever confined to one worktree."""

    def __init__(self, worktree: str | Path, *, limits: RetrievalLimits | None = None,
                 read_text: ReadText | None = None) -> None:
        root = Path(worktree).resolve()
        if not root.is_dir():
            raise ValueError("worktree must be an existing directory")
        self._root = root
        self._limits = limits or RetrievalLimits()
        self._read_text = read_text or self._default_read_text

    def retrieve(self, *, path: str | Path | None = None, symbol: str | None = None,
                 query: str | None = None) -> RetrievalResult:
        files = self._python_files(path)
        symbols: list[Symbol] = []
        definitions: list[Definition] = []
        imports: list[Import] = []
        references: list[Reference] = []
        issues: list[RetrievalIssue] = []
        truncated = len(files) > self._limits.max_files
        scanned = 0
        for file_path in files[: self._limits.max_files]:
            scanned += 1
            rel = file_path.relative_to(self._root).as_posix()
            try:
                tree = ast.parse(self._read_text(file_path), filename=rel)
            except (OSError, UnicodeError, SyntaxError, ValueError) as exc:
                issues.append(RetrievalIssue(rel, type(exc).__name__))
                continue
            extracted = _extract(tree, rel)
            symbols.extend(extracted[0]); definitions.extend(extracted[1])
            imports.extend(extracted[2]); references.extend(extracted[3])
        values = _filter_items(symbols, definitions, imports, references, symbol=symbol, query=query)
        was_capped = sum(len(items) for items in values) > self._limits.max_items
        remaining = self._limits.max_items
        capped: list[list[object]] = []
        for items in values:
            capped.append(items[:remaining])
            remaining -= len(capped[-1])
        return RetrievalResult(tuple(capped[0]), tuple(capped[1]), tuple(capped[2]), tuple(capped[3]),
                               tuple(issues[: self._limits.max_items]), scanned, truncated or was_capped)

    def _python_files(self, path: str | Path | None) -> list[Path]:
        if path is not None:
            candidate = self._confined(path)
            if candidate.is_file():
                return [candidate] if candidate.suffix == ".py" else []
            if not candidate.is_dir():
                return []
            roots = [candidate]
        else:
            roots = [self._root]
        files: list[Path] = []
        for base in roots:
            for directory, dir_names, names in os.walk(base, followlinks=False):
                dir_names[:] = [name for name in dir_names if not (Path(directory) / name).is_symlink()]
                for name in names:
                    file_path = Path(directory) / name
                    if file_path.suffix == ".py" and not file_path.is_symlink():
                        try:
                            if file_path.stat().st_size <= self._limits.max_file_bytes:
                                relative = file_path.relative_to(self._root)
                                files.append(self._confined(relative))
                        except OSError:
                            continue
        return sorted(set(files), key=lambda item: item.relative_to(self._root).as_posix())

    def _confined(self, path: str | Path) -> Path:
        raw = str(path).replace("\\", "/")
        if Path(path).is_absolute() or re.match(r"^[A-Za-z]:", raw) or ".." in raw.split("/"):
            raise ValueError(f"path is outside worktree: {path}")
        candidate = (self._root / raw.lstrip("./")).resolve()
        try:
            candidate.relative_to(self._root)
        except ValueError as exc:
            raise PermissionError("path resolves outside worktree") from exc
        if Path(self._root / raw.lstrip("./")).is_symlink():
            raise PermissionError("symlink paths are not allowed")
        if is_secret_path(candidate):
            raise PermissionError("access to protected secret files is denied")
        return candidate

    @staticmethod
    def _default_read_text(path: Path) -> str:
        return path.read_text(encoding="utf-8")


def retrieve_workspace(worktree: str | Path, *, path: str | Path | None = None,
                       symbol: str | None = None, query: str | None = None,
                       limits: RetrievalLimits | None = None) -> RetrievalResult:
    """Convenience facade around :class:`AstWorkspaceRetriever`."""
    return AstWorkspaceRetriever(worktree, limits=limits).retrieve(path=path, symbol=symbol, query=query)


def _extract(tree: ast.AST, path: str) -> tuple[list[Symbol], list[Definition], list[Import], list[Reference]]:
    symbols: list[Symbol] = []
    definitions: list[Definition] = []
    imports: list[Import] = []
    references: list[Reference] = []

    def visit(node: ast.AST, parents: tuple[str, ...], caller: str | None) -> None:
        current = caller
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            qualified = ".".join((*parents, node.name))
            kind = "class" if isinstance(node, ast.ClassDef) else "function"
            item = Symbol(node.name, kind, path, node.lineno, getattr(node, "end_lineno", node.lineno), qualified)
            symbols.append(item); definitions.append(Definition(node.name, path, node.lineno, item.end_line, qualified))
            current = qualified
            parents = (*parents, node.name)
        if isinstance(node, ast.Import):
            imports.extend(Import(alias.name, path, node.lineno, alias.asname, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = "." * node.level + (node.module or "")
            imports.extend(Import(alias.name, path, node.lineno, alias.asname, module) for alias in node.names)
        if isinstance(node, ast.Call):
            name = _reference_name(node.func)
            if name:
                references.append(Reference(name, path, node.lineno, current, "call"))
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            references.append(Reference(node.id, path, node.lineno, current))
        for child in ast.iter_child_nodes(node):
            visit(child, parents, current)

    visit(tree, (), None)
    return symbols, definitions, imports, references


def _reference_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _reference_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return None


def _filter_items(*groups: Sequence[object], symbol: str | None, query: str | None) -> list[list[object]]:
    needle = (query or symbol or "").casefold()
    result: list[list[object]] = []
    for group in groups:
        if not needle:
            result.append(list(group))
            continue
        result.append([item for item in group if needle in _item_text(item).casefold()])
    return result


def _item_text(item: object) -> str:
    if isinstance(item, (Symbol, Definition)):
        return f"{item.name} {item.qualified_name} {item.path}"
    if isinstance(item, Import):
        return f"{item.name} {item.module or ''} {item.path}"
    return f"{item.name} {item.caller or ''} {item.path}"


__all__ = ["AstWorkspaceRetriever", "Definition", "Import", "Reference", "RetrievalIssue",
           "RetrievalLimits", "RetrievalResult", "StructureRetriever", "Symbol", "retrieve_workspace"]
