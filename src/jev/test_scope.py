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
    """Detect if a scope item is a directory or glob pattern rather than an exact file."""
    s = item.strip().replace("\\", "/")
    if any(ch in s for ch in ("*", "?", "[", "]")):
        return True
    if s.endswith("/"):
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

        # Search the worktree for candidate test files matching ecosystem conventions
        search_dirs = [root]
        for sub in ["tests", "test", "spec", "__tests__", "src/test"]:
            sub_p = root / sub
            if sub_p.is_dir():
                search_dirs.append(sub_p)
        if src_path.parent.is_dir() and src_path.parent != root:
            search_dirs.append(src_path.parent)

        found_paths: Set[Path] = set()
        for sdir in search_dirs:
            try:
                for entry in sdir.rglob("*"):
                    if entry.is_file():
                        found_paths.add(entry)
            except Exception:
                pass

        candidates_for_file: List[Tuple[str, bool]] = []  # (rel_posix, is_exact)

        for fpath in found_paths:
            try:
                rel = fpath.relative_to(root).as_posix()
            except ValueError:
                continue

            if not is_test_path(rel):
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


def validate_subgoal_test_scope(
    subgoals: List[Subgoal],
    worktree_dir: Optional[Path],
) -> Tuple[List[Subgoal], List[Dict[str, Any]]]:
    """Validate and mechanically amend subgoal scopes with test files."""
    problems: List[Dict[str, Any]] = []
    amended_subgoals: List[Subgoal] = []
    root = Path(worktree_dir).resolve() if worktree_dir else None

    for sg in subgoals:
        sg_copy = sg.model_copy(deep=True)
        scope = list(sg_copy.scope)

        # 1. Reject directories and glob patterns in scope
        has_invalid_item = False
        for item in scope:
            if is_directory_or_glob(item):
                problems.append({
                    "subgoal_id": sg_copy.id,
                    "type": "invalid_scope_item",
                    "item": item,
                    "message": f"Scope entry '{item}' is a directory or glob pattern. Exact file paths required.",
                })
                has_invalid_item = True
            elif root:
                target_p = root / item.replace("\\", "/").strip().lstrip("./")
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

        # Check if scope already names a test file (existing or new)
        test_files_in_scope = [f for f in scope if is_test_path(f)]

        if test_files_in_scope:
            # Already names at least one test file
            amended_subgoals.append(sg_copy)
            continue

        # If no test file in scope, discover related tests
        discovery = discover_related_tests(root, scope)

        if len(discovery.ambiguous) > 0 or len(discovery.exact) > 5:
            all_cands = sorted(list(set(discovery.ambiguous + discovery.exact)))
            problems.append({
                "subgoal_id": sg_copy.id,
                "type": "ambiguous_test_candidates",
                "candidates": all_cands,
                "message": (
                    f"Ambiguous test candidates found for scope {scope}: {all_cands}. "
                    "Please explicitly name the intended test file in the subgoal scope."
                ),
            })
            amended_subgoals.append(sg_copy)
        elif len(discovery.exact) >= 1:
            for test_path in discovery.exact:
                if test_path not in scope:
                    scope.append(test_path)
            sg_copy.scope = scope
            amended_subgoals.append(sg_copy)
        else:
            problems.append({
                "subgoal_id": sg_copy.id,
                "type": "missing_test_scope",
                "message": (
                    f"Subgoal '{sg_copy.id}' expects tests, but scope {scope} contains no test files "
                    "and no matching existing test files could be discovered. "
                    "Either add the test file to scope or set expects_tests=false if tests are exempt."
                ),
            })
            amended_subgoals.append(sg_copy)

    return amended_subgoals, problems
