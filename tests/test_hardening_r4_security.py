"""tests/test_hardening_r4_security.py

Adversarial regression test suite for Phase R4 security verification and boundary hardening:
1. save_repo_config rejects .jev directory symlink pointing outside repository.
2. save_repo_config rejects config.json target symlink.
3. save_repo_config legitimate saving continues to work.
4. save_proposal uses atomic write replacement to prevent partial writes.
5. load_proposal and list_proposals reject symlinked proposal files.
6. KnowledgeStore rejects symlinked database files.
7. get_knowledge_db_path and get_trust_file_path reject DOS devices and NUL characters.
8. Workspace.resolve_in_worktree rejects Windows DOS reserved device names (CON, NUL, AUX, PRN, etc.).
9. Workspace.stage_file_mutation performs atomic write and rejects DOS device names.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from jev.knowledge import KnowledgeStore, get_knowledge_db_path
from jev.proposals import Proposal, list_proposals, load_proposal, save_proposal
from jev.repo_config import RepoConfig, get_trust_file_path, load_repo_config, save_repo_config
from jev.workspace import Workspace


# -----------------------------------------------------------------------------
# 1. save_repo_config Symlink & Path Containment Protection
# -----------------------------------------------------------------------------

def test_save_repo_config_rejects_jev_symlink_to_outside(tmp_path):
    """Verifies that a .jev symlink pointing outside the repo cannot cause save_repo_config to write outside."""
    repo = tmp_path / "repo"
    repo.mkdir()
    outside_jev = tmp_path / "outside_jev"
    outside_jev.mkdir()
    jev_link = repo / ".jev"
    try:
        os.symlink(str(outside_jev), str(jev_link), target_is_directory=True)
    except OSError as e:
        pytest.skip(f"Symlinks not supported on this environment: {e}")

    cfg = RepoConfig(runner_profile="pytest", test_command=["pytest"])

    with pytest.raises((PermissionError, ValueError), match="(?i)symlink|escape|outside"):
        save_repo_config(cfg, repo)

    assert not (outside_jev / "config.json").exists()


def test_save_repo_config_rejects_target_symlink(tmp_path):
    """Verifies that save_repo_config will not overwrite an external file through a symlinked config.json."""
    repo = tmp_path / "repo"
    jev_dir = repo / ".jev"
    jev_dir.mkdir(parents=True)
    outside_file = tmp_path / "sensitive.txt"
    outside_file.write_text("SENSITIVE HOST DATA", encoding="utf-8")

    cfg_link = jev_dir / "config.json"
    try:
        os.symlink(str(outside_file), str(cfg_link))
    except OSError as e:
        pytest.skip(f"Symlinks not supported on this environment: {e}")

    cfg = RepoConfig(runner_profile="pytest", test_command=["pytest"])

    with pytest.raises((PermissionError, ValueError), match="(?i)symlink|escape|outside"):
        save_repo_config(cfg, repo)

    assert outside_file.read_text(encoding="utf-8") == "SENSITIVE HOST DATA"


def test_save_repo_config_legitimate_save_works(tmp_path):
    """Verifies that normal save_repo_config works cleanly and maintains correct containment."""
    repo = tmp_path / "repo"
    repo.mkdir()

    cfg = RepoConfig(runner_profile="pytest", test_command=["pytest"])
    saved_path = save_repo_config(cfg, repo)

    assert saved_path.exists()
    assert saved_path == (repo / ".jev" / "config.json").resolve()
    data = json.loads(saved_path.read_text(encoding="utf-8"))
    assert data.get("runner_profile") == "pytest" or data.get("runner_profile_id") == "pytest"
    assert data["test_command"] == ["pytest"]


def test_load_repo_config_upward_search_rejects_parent_symlinks(tmp_path):
    """Verifies that load_repo_config rejects symlinks discovered during upward search."""
    root = tmp_path / "root"
    root.mkdir()
    sub_worktree = root / "worktrees" / "wt1"
    sub_worktree.mkdir(parents=True)

    outside_jev = tmp_path / "outside_jev"
    outside_jev.mkdir()
    outside_cfg = outside_jev / "config.json"
    outside_cfg.write_text(json.dumps({"runner_profile": "pytest", "test_command": ["pytest"]}), encoding="utf-8")

    parent_jev = root / ".jev"
    try:
        os.symlink(str(outside_jev), str(parent_jev), target_is_directory=True)
    except OSError as e:
        pytest.skip(f"Symlinks not supported on this environment: {e}")

    # load_repo_config from sub_worktree must not load the symlinked parent configuration
    assert load_repo_config(sub_worktree) is None

    # Also test symlinked config.json inside real parent .jev
    parent_jev.unlink()
    parent_jev.mkdir()
    parent_cfg_symlink = parent_jev / "config.json"
    os.symlink(str(outside_cfg), str(parent_cfg_symlink))

    assert load_repo_config(sub_worktree) is None


def test_load_repo_config_parent_worktree_discovery_works(tmp_path):
    """Verifies legitimate parent and gitdir worktree configuration discovery."""
    main_repo = tmp_path / "main_repo"
    main_repo.mkdir()
    main_jev = main_repo / ".jev"
    main_jev.mkdir()
    main_cfg = main_jev / "config.json"
    main_cfg.write_text(json.dumps({"runner_profile": "pytest", "test_command": ["pytest"]}), encoding="utf-8")

    # 1. Direct subdirectory
    sub_dir = main_repo / "src" / "package"
    sub_dir.mkdir(parents=True)
    loaded_sub = load_repo_config(sub_dir)
    assert loaded_sub is not None
    assert loaded_sub.test_command == ["pytest"]

    # 2. Git worktree with gitdir: pointer
    worktree_dir = tmp_path / "worktree_wt"
    worktree_dir.mkdir()
    fake_gitdir = main_repo / ".git" / "worktrees" / "wt"
    fake_gitdir.mkdir(parents=True)
    git_file = worktree_dir / ".git"
    git_file.write_text(f"gitdir: {fake_gitdir}\n", encoding="utf-8")

    loaded_wt = load_repo_config(worktree_dir)
    assert loaded_wt is not None
    assert loaded_wt.test_command == ["pytest"]


# -----------------------------------------------------------------------------
# 2. Proposal Atomic Write & Symlink Protection
# -----------------------------------------------------------------------------

def test_save_proposal_atomic_write_replacement(tmp_path):
    """Verifies save_proposal writes atomically, cleans up temp files on error, and prevents partial reads."""
    repo = tmp_path / "repo"
    repo.mkdir()

    prop = Proposal(
        id="prop-atomic-test",
        repo_id="test-repo",
        exact_argv=["pytest"],
        cwd=".",
        source_evidence="Makefile",
        platform=sys.platform,
        identity_hash="hash",
        created_at="2026-10-30T00:00:00Z",
    )

    # First save legitimately
    saved = save_proposal(prop, repo)
    assert saved is not None and saved.exists()
    original_content = saved.read_text(encoding="utf-8")
    pdir = repo / ".jev" / "proposals"

    # Simulate an error during atomic replacement / write
    with patch("os.replace", side_effect=OSError("Disk write failed")):
        with pytest.raises(OSError, match="Disk write failed"):
            save_proposal(prop, repo)

    # Original content should not be wiped or corrupted
    assert saved.read_text(encoding="utf-8") == original_content

    # Verify no dangling temporary files remain
    assert list(pdir.glob(".tmp_*")) == []


def test_save_proposal_atomic_concurrency(tmp_path):
    """Verifies that concurrent readers never observe partial or corrupted proposals during atomic replacement."""
    import concurrent.futures
    import threading
    import time

    repo = tmp_path / "repo"
    repo.mkdir()

    initial_evidence = "Evidence-initial-" + ("0" * 4000) + "-SNAPSHOT_END-initial"
    initial_argv = ["pytest", "--snapshot=initial"]
    prop = Proposal(
        id="prop-concurrent",
        repo_id="test-repo",
        exact_argv=list(initial_argv),
        cwd=".",
        source_evidence=initial_evidence,
        platform=sys.platform,
        identity_hash="hash-initial",
        created_at="2026-10-30T00:00:00Z",
    )
    save_proposal(prop, repo)

    valid_snapshots = {
        "initial": (initial_evidence, initial_argv)
    }
    for i in range(25):
        valid_snapshots[str(i)] = (
            f"Evidence-{i}-" + ("#" * 8000) + f"-SNAPSHOT_END-{i}",
            ["pytest", f"--snapshot={i}", "-v"],
        )

    start_barrier = threading.Barrier(3)
    stop_event = threading.Event()
    read_errors = []
    read_counts = [0]
    successful_writes = [0]
    failed_writes = [0]

    count_lock = threading.Lock()

    def reader(reader_id: int):
        start_barrier.wait()
        while not stop_event.is_set():
            try:
                p = None
                for _ in range(5):
                    p = load_proposal("prop-concurrent", repo)
                    if p is not None:
                        break
                    time.sleep(0.001)

                if p is None:
                    with count_lock:
                        read_errors.append(f"Reader {reader_id}: load_proposal returned None during concurrent write")
                else:
                    matched = False
                    for snap_key, (exp_ev, exp_argv) in valid_snapshots.items():
                        if p.source_evidence == exp_ev and p.exact_argv == exp_argv:
                            matched = True
                            break
                    if not matched:
                        with count_lock:
                            read_errors.append(f"Reader {reader_id}: Torn or invalid payload read: {p.source_evidence[:30]}... {p.exact_argv}")
                    with count_lock:
                        read_counts[0] += 1
            except Exception as e:
                with count_lock:
                    read_errors.append(f"Reader {reader_id} exception: {e}")
            time.sleep(0.002)

    def writer():
        start_barrier.wait()
        for i in range(25):
            exp_ev, exp_argv = valid_snapshots[str(i)]
            w_prop = Proposal(
                id="prop-concurrent",
                repo_id="test-repo",
                exact_argv=list(exp_argv),
                cwd=".",
                source_evidence=exp_ev,
                platform=sys.platform,
                identity_hash=f"hash-{i}",
                created_at="2026-10-30T00:00:00Z",
            )
            try:
                save_proposal(w_prop, repo)
                with count_lock:
                    successful_writes[0] += 1
            except PermissionError as e:
                if sys.platform != "win32":
                    raise
                # On Windows, os.replace can fail with WinError 5/32 if reader currently holds open handle
                with count_lock:
                    failed_writes[0] += 1
            time.sleep(0.01)

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        reader_futures = [executor.submit(reader, 1), executor.submit(reader, 2)]
        writer_future = executor.submit(writer)
        try:
            writer_future.result()
        finally:
            stop_event.set()
        for rf in reader_futures:
            rf.result()

    assert read_errors == []
    assert read_counts[0] >= 10
    assert successful_writes[0] >= 5


def test_load_proposal_rejects_symlink(tmp_path):
    """Verifies that load_proposal refuses to read proposals through internal aliases or external symlinks."""
    repo = tmp_path / "repo"
    pdir = repo / ".jev" / "proposals"
    pdir.mkdir(parents=True)

    # 1. External symlink target
    outside_file = tmp_path / "secret_proposal.json"
    prop_data = {
        "id": "prop-outside",
        "repo_id": "test-repo",
        "exact_argv": ["pytest"],
        "cwd": ".",
        "source_evidence": "Makefile",
        "platform": sys.platform,
        "identity_hash": "hash",
        "created_at": "2026-10-30T00:00:00Z",
    }
    outside_file.write_text(json.dumps(prop_data), encoding="utf-8")

    sym_target = pdir / "prop-outside.json"
    try:
        os.symlink(str(outside_file), str(sym_target))
    except OSError as e:
        pytest.skip(f"Symlinks not supported on this environment: {e}")

    assert load_proposal("prop-outside", repo) is None

    # 2. Internal symlink alias within .jev/proposals
    legit = Proposal(
        id="prop-legit",
        repo_id="test-repo",
        exact_argv=["pytest"],
        cwd=".",
        source_evidence="Makefile",
        platform=sys.platform,
        identity_hash="hash",
        created_at="2026-10-30T00:00:00Z",
    )
    saved_legit = save_proposal(legit, repo)
    assert saved_legit is not None

    internal_alias = pdir / "prop-alias.json"
    os.symlink(str(saved_legit), str(internal_alias))

    # Proves the symlink check itself rejects internal aliases whose resolved parent is expected_proposals_dir
    assert load_proposal("prop-alias", repo) is None


def test_list_proposals_omits_symlinks(tmp_path):
    """Verifies that list_proposals ignores symlinked proposal files."""
    repo = tmp_path / "repo"
    pdir = repo / ".jev" / "proposals"
    pdir.mkdir(parents=True)

    legit = Proposal(
        id="prop-legit",
        repo_id="test-repo",
        exact_argv=["pytest"],
        cwd=".",
        source_evidence="Makefile",
        platform=sys.platform,
        identity_hash="hash",
        created_at="2026-10-30T00:00:00Z",
    )
    save_proposal(legit, repo)

    outside_file = tmp_path / "outside_prop.json"
    outside_file.write_text(legit.model_dump_json(), encoding="utf-8")

    sym_link = pdir / "prop-symlink.json"
    try:
        os.symlink(str(outside_file), str(sym_link))
    except OSError as e:
        pytest.skip(f"Symlinks not supported on this environment: {e}")

    proposals = list_proposals(repo)
    ids = [p.id for p in proposals]
    assert "prop-legit" in ids
    assert "prop-symlink" not in ids


# -----------------------------------------------------------------------------
# 3. KnowledgeStore and get_knowledge_db_path Hardening
# -----------------------------------------------------------------------------

def test_knowledge_store_rejects_symlinked_db(tmp_path):
    """Verifies that KnowledgeStore refuses to open or unlink a symlinked knowledge.db."""
    outside_db = tmp_path / "outside_target.db"
    outside_db.write_text("SENSITIVE DATA", encoding="utf-8")

    sym_db = tmp_path / "knowledge.db"
    try:
        os.symlink(str(outside_db), str(sym_db))
    except OSError as e:
        pytest.skip(f"Symlinks not supported on this environment: {e}")

    with pytest.raises(PermissionError, match="(?i)symlink"):
        KnowledgeStore(sym_db, repo_key="test-repo-key")

    assert outside_db.read_text(encoding="utf-8") == "SENSITIVE DATA"


def test_get_knowledge_db_path_rejects_nul_and_dos_devices():
    """Verifies that get_knowledge_db_path rejects NUL characters and Windows DOS device names."""
    invalid_keys = [
        "repo\x00key",
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "COM1",
        "LPT1",
        "COM¹",
        "COM²",
        "COM³",
        "LPT¹",
        "LPT²",
        "LPT³",
        "con.txt",
        "nul.db",
        "com¹.db",
    ]
    for key in invalid_keys:
        with pytest.raises(ValueError, match="(?i)invalid|traversal|device"):
            get_knowledge_db_path(key)


def test_get_trust_file_path_rejects_nul_and_dos_devices():
    """Verifies that get_trust_file_path rejects NUL characters and Windows DOS device names."""
    invalid_ids = [
        "repo\x00id",
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "COM1",
        "LPT1",
        "COM¹",
        "COM²",
        "COM³",
        "LPT¹",
        "LPT²",
        "LPT³",
        "con.json",
        "lpt¹.json",
    ]
    for rid in invalid_ids:
        with pytest.raises(ValueError, match="(?i)invalid|traversal|device"):
            get_trust_file_path(rid)


# -----------------------------------------------------------------------------
# 4. Workspace DOS Device Name Containment
# -----------------------------------------------------------------------------

def test_workspace_resolve_in_worktree_rejects_dos_device_names(tmp_path):
    """Verifies resolve_in_worktree rejects Windows DOS reserved device names."""
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = Workspace(repo_dir=repo)

    device_names = [
        "CON",
        "con",
        "PRN",
        "prn",
        "AUX",
        "aux",
        "NUL",
        "nul",
        "CONIN$",
        "conin$",
        "CONOUT$",
        "conout$",
        "CLOCK$",
        "clock$",
        "COM1",
        "com9",
        "LPT1",
        "lpt9",
        "COM¹",
        "com¹",
        "COM²",
        "com²",
        "COM³",
        "com³",
        "LPT¹",
        "lpt¹",
        "LPT²",
        "lpt²",
        "LPT³",
        "lpt³",
        "COM¹.txt",
        "COM²:",
        "COM³   ",
        "LPT¹...",
        "LPT².:",
        "COM¹:stream",
        "sub/COM¹",
        "sub/COM².txt",
        "CON.txt",
        "con.log",
        "conin$.txt",
        "CONOUT$.log",
        "sub/CON",
        "sub/aux.py",
        "sub/conin$.txt",
    ]

    for dev in device_names:
        with pytest.raises(ValueError, match="(?i)device|reserved|outside"):
            ws.resolve_in_worktree(dev)

    # Verify legitimate files with similar prefixes and near-misses are not blocked
    legitimate_names = [
        "conference.py", "connection.ts", "clock.py", "nulled.json", "context.md",
        "COM⁴", "com⁴", "LPT⁴", "lpt⁴", "COM10", "LPT10", "COM10.txt"
    ]
    for leg in legitimate_names:
        resolved = ws.resolve_in_worktree(leg)
        assert resolved == repo / leg


def test_workspace_stage_file_mutation_rejects_dos_device_names(tmp_path):
    """Verifies stage_file_mutation rejects mutating DOS device names."""
    repo = tmp_path / "repo"
    repo.mkdir()
    # Initialize git repo for workspace
    subprocess.run(["git", "init"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo), check=True, capture_output=True)
    (repo / "README.md").write_text("# Test\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), check=True, capture_output=True)

    ws = Workspace(repo_dir=repo)

    for dev in [
        "NUL", "AUX", "PRN", "COM1", "CONIN$", "CLOCK$", "CON.txt", "conin$.txt", "sub/CON.log",
        "COM¹", "com¹", "COM²", "com²", "COM³", "com³",
        "LPT¹", "lpt¹", "LPT²", "lpt²", "LPT³", "lpt³",
        "COM¹.txt", "COM²:", "sub/COM¹",
    ]:
        with pytest.raises((ValueError, PermissionError), match="(?i)device|reserved|outside|blocked"):
            ws.stage_file_mutation(dev, "test content")

    # Verify staging legitimate near-miss does not trigger DOS device rejection
    ws.stage_file_mutation("COM10.txt", "legitimate content")
    assert (repo / "COM10.txt").read_text(encoding="utf-8") == "legitimate content" 
