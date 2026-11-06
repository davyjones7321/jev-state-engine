"""Mechanical test scope discovery and validation for Phase R2.

Enforces that subgoals expecting automated tests explicitly declare their
relevant test files in scope, either by exact automatic matching or by
explicit planner declaration, preventing silent omissions.
"""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from jev.models import Subgoal


@dataclass
class DiscoveryResult:
    exact: List[str] = field(default_factory=list)
    ambiguous: List[str] = field(default_factory=list)


EXCLUDED_DISCOVERY_DIRS: Set[str] = {
    ".git", "node_modules", ".venv", "venv", "env", ".env",
    "build", "dist", "target", "__pycache__", ".tox",
    ".pytest_cache", ".mypy_cache", ".cargo", "vendor", "Pods",
}


def is_test_path(path_str: str) -> bool:
    """Check if a path conforms to standard test file naming conventions."""
    p = path_str.replace("\\", "/").lower()
    parts = p.split("/")
    filename = parts[-1]

    # Directories typically reserved for tests
    test_dirs = {"tests", "test", "__tests__", "spec", "specs"}
    if any(d in test_dirs for d in parts[:-1]):
        return True

    # Ecosystem test naming patterns:
    # Python: test_*.py, *_test.py
    # JS/TS: *.test.*, *.spec.*
    # Go: *_test.go
    # Rust: tests/*.rs
    # .NET: *Tests.cs, *Test.cs
    # Ruby: *_spec.rb, *_test.rb
    # PHP: *Test.php
    # Elixir: *_test.exs
    if re.search(r"(^test_.*\.py$|_test\.py$)", filename):
        return True
    if re.search(r"(\.test\.|\.spec\.)", filename):
        return True
    if filename.endswith("_test.go"):
        return True
    if filename.endswith("tests.cs") or filename.endswith("test.cs"):
        return True
    if filename.endswith("_spec.rb") or filename.endswith("_test.rb"):
        return True
    if filename.endswith("test.php"):
        return True
    if filename.endswith("_test.exs"):
        return True

    return False


def is_directory_or_glob(item: str) -> bool:
    """Detect if a scope item is a directory, glob pattern, or unsafe path rather than an exact file."""
    s = item.strip().replace("\\", "/")
    if any(ch in s for ch in ("*", "?", "[", "]")):
        return True
    if s.endswith("/"):
        return True
    # Traversal, absolute, or drive letter paths
    parts = [p for p in s.split("/") if p]
    if ".." in parts:
        return True
    if s.startswith("/") or bool(re.match(r"^[a-zA-Z]:", s)):
        return True
    return False


def _is_compatible_package_path(src_norm: str, test_norm: str) -> bool:
    """Check whether src_norm and test_norm belong to the same package / directory hierarchy."""
    src_parts = [p for p in src_norm.split("/")[:-1] if p and p != "."]
    test_parts = [p for p in test_norm.split("/")[:-1] if p and p != "."]

    # Both at root level
    if not src_parts and not test_parts:
        return True

    # Sibling files in identical directory
    if src_parts == test_parts:
        return True

    TEST_DIR_NAMES = {"tests", "test", "__tests__", "spec", "specs"}

    # Case 1: Root-level test directory
    if test_parts and test_parts[0] in TEST_DIR_NAMES:
        # If src is inside a multi-package folder (packages/a, apps/b, services/c, etc.)
        if src_parts and src_parts[0] in ("packages", "apps", "services", "modules", "crates"):
            # Root tests only match if mirroring the package path
            trimmed_src = [p for p in src_parts if p not in ("src", "lib")]
            trimmed_test = [p for p in test_parts[1:] if p not in ("src", "lib")]
            return trimmed_src == trimmed_test or test_parts[1:] == src_parts
        return True

    # Case 2: Maven / Gradle src/main vs src/test mirroring
    if len(src_parts) >= 2 and len(test_parts) >= 2:
        if src_parts[0] == "src" and src_parts[1] == "main" and test_parts[0] == "src" and test_parts[1] == "test":
            return src_parts[2:] == test_parts[2:]

    # Find common prefix parts
    common_len = 0
    while common_len < len(src_parts) and common_len < len(test_parts):
        if src_parts[common_len] == test_parts[common_len]:
            common_len += 1
        else:
            break

    src_rem = src_parts[common_len:]
    test_rem = test_parts[common_len:]

    # If common prefix diverged at a package boundary
    if test_rem and test_rem[0] in TEST_DIR_NAMES:
        return True

    # Co-located test directory: e.g. src/__tests__/
    if test_rem and set(test_rem).issubset(TEST_DIR_NAMES):
        return True

    return False


def discover_related_tests(
    worktree_dir: Optional[Path],
    scope_files: Sequence[str],
) -> DiscoveryResult:
    """Discover related test files for a list of scoped source files."""
    if not worktree_dir or not Path(worktree_dir).exists():
        return DiscoveryResult()

    root = Path(worktree_dir).resolve()
    exact_matches: Set[str] = set()
    ambiguous_matches: Set[str] = set()

    for scope_file in scope_files:
        norm_scope = scope_file.replace("\\", "/").strip().lstrip("./")
        if not norm_scope or is_test_path(norm_scope):
            continue

        src_path = root / norm_scope
        src_stem = Path(norm_scope).stem
        src_ext = Path(norm_scope).suffix.lower()

        # Check Rust inline test: inline #[cfg(test)] counts as source file itself
        if src_ext == ".rs" and src_path.is_file():
            try:
                txt = src_path.read_text(encoding="utf-8", errors="ignore")
                if "#[cfg(test)]" in txt:
                    exact_matches.add(norm_scope)
            except Exception:
                pass

        # Determine relevant search roots for this specific source file
        search_dirs: List[Path] = []
        if src_path.parent.is_dir():
            search_dirs.append(src_path.parent)

        curr = src_path.parent
        while curr != root and curr != curr.parent:
            for sub in ("tests", "test", "spec", "__tests__", "src/test"):
                pkg_sub = curr / sub
                if pkg_sub.is_dir() and pkg_sub not in search_dirs:
                    search_dirs.append(pkg_sub)
            if curr.parent == root or curr.parent.name in ("packages", "apps", "services", "modules", "crates"):
                break
            curr = curr.parent

        for sub in ("tests", "test", "spec", "__tests__", "src/test"):
            root_sub = root / sub
            if root_sub.is_dir() and root_sub not in search_dirs:
                search_dirs.append(root_sub)

        if not search_dirs:
            search_dirs.append(root)

        found_paths: Set[Path] = set()
        for sdir in search_dirs:
            if not sdir.exists():
                continue
            for root_dir, dirnames, filenames in os.walk(sdir):
                dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DISCOVERY_DIRS]
                for fn in filenames:
                    found_paths.add(Path(root_dir) / fn)

        candidates_for_file: List[Tuple[str, bool]] = []  # (rel_posix, is_exact)

        for fpath in found_paths:
            try:
                rel = fpath.relative_to(root).as_posix()
            except ValueError:
                continue

            if not is_test_path(rel):
                continue

            if not _is_compatible_package_path(norm_scope, rel):
                continue

            f_stem = fpath.stem
            f_name = fpath.name

            # Ecosystem exact matching rules:
            is_exact = False
            # Python:
            if src_ext == ".py":
                if f_stem == f"test_{src_stem}" or f_stem == f"{src_stem}_test":
                    is_exact = True
            # JS/TS:
            elif src_ext in (".ts", ".tsx", ".js", ".jsx"):
                if f_name.startswith(f"{src_stem}.test.") or f_name.startswith(f"{src_stem}.spec."):
                    is_exact = True
            # Go:
            elif src_ext == ".go":
                if fpath.parent == src_path.parent and f_name == f"{src_stem}_test.go":
                    is_exact = True
            # Rust:
            elif src_ext == ".rs":
                if fpath.parent.name in ("tests", "test") and (f_stem == src_stem or f_stem == f"test_{src_stem}"):
                    is_exact = True
            # Java/Kotlin:
            elif src_ext in (".java", ".kt"):
                if f_stem in (f"{src_stem}Test", f"{src_stem}Tests", f"Test{src_stem}"):
                    is_exact = True
            # .NET:
            elif src_ext == ".cs":
                if f_stem in (f"{src_stem}Tests", f"{src_stem}Test"):
                    is_exact = True
            # Ruby:
            elif src_ext == ".rb":
                if f_stem in (f"{src_stem}_spec", f"{src_stem}_test", f"test_{src_stem}"):
                    is_exact = True
            # PHP:
            elif src_ext == ".php":
                if f_stem in (f"{src_stem}Test", f"Test{src_stem}"):
                    is_exact = True
            # Elixir:
            elif src_ext == ".ex":
                if f_stem == f"{src_stem}_test" and f_name.endswith(".exs"):
                    is_exact = True

            if is_exact:
                candidates_for_file.append((rel, True))
            else:
                # Plausible look-alikes / partial name matches (e.g. test_service_extra_1.py)
                if src_stem.lower() in f_stem.lower():
                    candidates_for_file.append((rel, False))

        exact_cands = [p for p, exact in candidates_for_file if exact]
        non_exact_cands = [p for p, exact in candidates_for_file if not exact]

        if len(exact_cands) == 1 and not non_exact_cands:
            exact_matches.add(exact_cands[0])
        elif len(exact_cands) == 1 and len(non_exact_cands) > 0:
            if len(non_exact_cands) > 1 or (len(exact_cands) + len(non_exact_cands)) > 5:
                ambiguous_matches.update(exact_cands)
                ambiguous_matches.update(non_exact_cands)
            else:
                exact_matches.add(exact_cands[0])
        elif len(exact_cands) > 1:
            ambiguous_matches.update(exact_cands)
            ambiguous_matches.update(non_exact_cands)
        elif not exact_cands and non_exact_cands:
            ambiguous_matches.update(non_exact_cands)

    return DiscoveryResult(
        exact=sorted(list(exact_matches)),
        ambiguous=sorted(list(ambiguous_matches)),
    )


CODE_EXTENSIONS: Set[str] = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs",
    ".java", ".kt", ".cs", ".rb", ".php", ".ex", ".exs",
}


def matches_test_file(src_path_str: str, test_path_str: str) -> bool:
    """Check if test_path_str corresponds to src_path_str based on naming conventions and package hierarchy."""
    src_norm = src_path_str.replace("\\", "/").strip().lstrip("./")
    test_norm = test_path_str.replace("\\", "/").strip().lstrip("./")

    if not _is_compatible_package_path(src_norm, test_norm):
        return False

    src_stem = Path(src_norm).stem.lower()
    src_ext = Path(src_norm).suffix.lower()
    test_stem = Path(test_norm).stem.lower()
    test_name = Path(test_norm).name.lower()

    # Python
    if src_ext == ".py":
        if test_stem in (f"test_{src_stem}", f"{src_stem}_test", src_stem):
            return True
    # JS/TS
    elif src_ext in (".ts", ".tsx", ".js", ".jsx"):
        if test_name.startswith(f"{src_stem}.test.") or test_name.startswith(f"{src_stem}.spec."):
            return True
        if test_stem in (f"test_{src_stem}", f"{src_stem}_test", f"{src_stem}test", f"{src_stem}tests", src_stem):
            return True
    # Go
    elif src_ext == ".go":
        if test_name == f"{src_stem}_test.go":
            return True
    # Rust
    elif src_ext == ".rs":
        if test_stem in (src_stem, f"test_{src_stem}", f"{src_stem}_test"):
            return True
    # Java / Kotlin
    elif src_ext in (".java", ".kt"):
        if test_stem in (f"{src_stem}test", f"{src_stem}tests", f"test{src_stem}", src_stem):
            return True
    # .NET
    elif src_ext == ".cs":
        if test_stem in (f"{src_stem}tests", f"{src_stem}test", src_stem):
            return True
    # Ruby
    elif src_ext == ".rb":
        if test_stem in (f"{src_stem}_spec", f"{src_stem}_test", f"test_{src_stem}", src_stem):
            return True
    # PHP
    elif src_ext == ".php":
        if test_stem in (f"{src_stem}test", f"test{src_stem}", src_stem):
            return True
    # Elixir
    elif src_ext == ".ex":
        if test_stem == f"{src_stem}_test":
            return True

    # Generic convention fallback
    if test_stem == f"test_{src_stem}" or test_stem == f"{src_stem}_test":
        return True

    return False


def validate_subgoal_test_scope(
    subgoals: List[Subgoal],
    worktree_dir: Optional[Path],
) -> Tuple[List[Subgoal], List[Dict[str, Any]]]:
    """Validate and mechanically amend subgoal scopes with test files, evaluating every source file independently."""
    problems: List[Dict[str, Any]] = []
    amended_subgoals: List[Subgoal] = []
    root = Path(worktree_dir).resolve() if worktree_dir else None

    for sg in subgoals:
        sg_copy = sg.model_copy(deep=True)
        scope = list(sg_copy.scope)

        # 1. Reject directories, glob patterns, and unsafe paths in scope
        has_invalid_item = False
        for item in scope:
            clean_item = item.strip().replace("\\", "/")
            parts = [p for p in clean_item.split("/") if p]
            is_unsafe = (
                ".." in parts
                or clean_item.startswith("/")
                or bool(re.match(r"^[a-zA-Z]:", clean_item))
                or is_directory_or_glob(item)
            )
            if is_unsafe:
                problems.append({
                    "subgoal_id": sg_copy.id,
                    "type": "invalid_scope_item",
                    "item": item,
                    "message": f"Scope entry '{item}' is a directory, glob pattern, or unsafe path. Exact repository-relative file paths required.",
                })
                has_invalid_item = True
            elif root:
                norm_item = clean_item.lstrip("./")
                target_p = (root / norm_item).resolve()
                try:
                    target_p.relative_to(root)
                except ValueError:
                    problems.append({
                        "subgoal_id": sg_copy.id,
                        "type": "invalid_scope_item",
                        "item": item,
                        "message": f"Scope entry '{item}' resolves outside the repository root.",
                    })
                    has_invalid_item = True
                    continue
                if target_p.is_dir():
                    problems.append({
                        "subgoal_id": sg_copy.id,
                        "type": "invalid_scope_item",
                        "item": item,
                        "message": f"Scope entry '{item}' is a directory. Exact file paths required.",
                    })
                    has_invalid_item = True

        if has_invalid_item:
            amended_subgoals.append(sg_copy)
            continue

        # 2. Check expects_tests
        if not sg_copy.expects_tests:
            amended_subgoals.append(sg_copy)
            continue

        test_files_in_scope = [f for f in scope if is_test_path(f)]
        source_files_in_scope = [f for f in scope if not is_test_path(f)]

        # If scope contains only test files, it satisfies test scope
        if not source_files_in_scope:
            amended_subgoals.append(sg_copy)
            continue

        # Filter for code files that require tests
        code_sources = [
            f for f in source_files_in_scope
            if Path(f).suffix.lower() in CODE_EXTENSIONS
        ]
        target_sources = code_sources if code_sources else source_files_in_scope

        # Validate every source file independently
        for src_file in target_sources:
            # Check if this source file is already covered by a matching test in scope
            covered = any(matches_test_file(src_file, tf) for tf in test_files_in_scope)
            if covered:
                continue

            # Run discovery for this individual source file
            discovery = discover_related_tests(root, [src_file])

            if len(discovery.ambiguous) > 0 or len(discovery.exact) > 5:
                all_cands = sorted(list(set(discovery.ambiguous + discovery.exact)))
                problems.append({
                    "subgoal_id": sg_copy.id,
                    "type": "ambiguous_test_candidates",
                    "candidates": all_cands,
                    "source_file": src_file,
                    "message": (
                        f"Ambiguous test candidates found for source file '{src_file}' in scope: {all_cands}. "
                        "Please explicitly name the intended test file in the subgoal scope."
                    ),
                })
            elif len(discovery.exact) == 1:
                # Add unambiguous exact match to scope
                for test_path in discovery.exact:
                    if test_path not in scope:
                        scope.append(test_path)
            else:
                problems.append({
                    "subgoal_id": sg_copy.id,
                    "type": "missing_test_scope",
                    "source_file": src_file,
                    "message": (
                        f"Subgoal '{sg_copy.id}' expects tests, but source file '{src_file}' has no corresponding test files in scope "
                        "and no matching existing test files could be discovered on disk. "
                        "Either add the test file to scope or set expects_tests=false if tests are exempt."
                    ),
                })

        sg_copy.scope = scope
        amended_subgoals.append(sg_copy)

    return amended_subgoals, problems
