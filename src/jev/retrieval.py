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


SECRET_DENYLIST_PARTS = {".aws", ".kube", ".ssh", "secrets", ".secrets", ".gnupg"}
SECRET_DENYLIST_NAMES = {"credentials.json", "secrets.json", "id_rsa", "id_dsa", "id_ed25519"}
SECRET_DENYLIST_EXTENSIONS = (
    ".pem", ".key", ".pfx", ".p12", ".pkcs12", ".tfstate", ".tfstate.backup",
    ".tfvars", ".kdbx", ".jks", ".keystore",
)


def is_secret_path(path: str | Path) -> bool:
    """Return whether a path is a known credential, key, state, or secret file."""
    if not path:
        return False
    raw = str(path).replace("\\", "/").strip()
    path_obj = Path(path)
    name = path_obj.name.lower()

    # Path parts from Path and from posix normalized split
    parts = {part.lower() for part in path_obj.parts if part}
    raw_parts = [part.lower() for part in raw.split("/") if part]
    posix_parts = set(raw_parts)
    all_parts = parts | posix_parts

    # Directory patterns: .aws/, .kube/, .ssh/
    if any(secret_dir in all_parts for secret_dir in SECRET_DENYLIST_PARTS):
        return True
    for secret_dir in SECRET_DENYLIST_PARTS:
        if f"/{secret_dir}/" in f"/{raw}/" or raw.startswith(f"{secret_dir}/") or raw == secret_dir:
            return True

    # .env* (name starts with .env or any path component starts with .env)
    if name.startswith(".env") or any(part.startswith(".env") for part in all_parts):
        return True

    # Secret directories are denied recursively.
    if any(secret_dir in all_parts for secret_dir in SECRET_DENYLIST_PARTS):
        return True

    # Exact names: credentials.json, secrets.json, id_rsa, id_dsa, id_ed25519
    if name in SECRET_DENYLIST_NAMES or any(part in SECRET_DENYLIST_NAMES for part in all_parts):
        return True

    # Common package, VCS, container, and deployment credential files.
    if name in {".npmrc", ".pypirc", ".netrc"}:
        return True
    if len(raw_parts) >= 2 and raw_parts[-2:] == [".git", "config"]:
        return True
    if raw.lower().endswith("/.docker/config.json") or raw.lower() == ".docker/config.json":
        return True
    if name.startswith("service-account") and name.endswith(".json"):
        return True
    if name.endswith("-key.json") or ("credentials" in name and name.endswith(".json")):
        return True

    # id_rsa* variants (e.g. id_rsa.pub, id_rsa_backup)
    if name in ("id_ecdsa", "id_dsa") or name.startswith(("id_rsa", "id_dsa", "id_ed25519")) or any(
        part.startswith(("id_rsa", "id_dsa", "id_ed25519")) for part in all_parts
    ):
        return True

    # Extensions: *.pem, *.key (and .pfx, .p12, .pkcs12)
    if name.endswith(SECRET_DENYLIST_EXTENSIONS) or any(
        part.endswith(SECRET_DENYLIST_EXTENSIONS) for part in all_parts
    ):
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
                dir_names[:] = [
                    name for name in dir_names
                    if not (Path(directory) / name).is_symlink()
                    and not is_secret_path(Path(directory) / name)
                ]
                for name in names:
                    file_path = Path(directory) / name
                    if file_path.suffix == ".py" and not file_path.is_symlink() and not is_secret_path(file_path):
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
        clean_raw = raw[2:] if raw.startswith("./") else (raw[1:] if raw.startswith("/") else raw)
        candidate = (self._root / clean_raw).resolve()
        try:
            candidate.relative_to(self._root)
        except ValueError as exc:
            raise PermissionError("path resolves outside worktree") from exc
        if Path(self._root / clean_raw).is_symlink():
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


__all__ = ["AstWorkspaceRetriever", "is_secret_path", "Definition", "Import", "Reference", "RetrievalIssue",
           "RetrievalLimits", "RetrievalResult", "StructureRetriever", "Symbol", "retrieve_workspace"]
