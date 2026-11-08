"""Phase R5 adversarial security regression tests."""

import subprocess
from pathlib import Path
import pytest
from jev.models import Subgoal
from jev.workspace import Workspace


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    (repo / "README.md").write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo, check=True)
    return repo


def make_dir_link(target: Path, link_path: Path):
    import sys
    if sys.platform == "win32":
        import _winapi
        _winapi.CreateJunction(str(target), str(link_path))
    else:
        import os
        os.symlink(str(target), str(link_path), target_is_directory=True)


class TestR5SubgoalWorktreeContainment:
    """VULN-1: Prevent directory traversal and arbitrary deletion via subgoal_id."""

    def test_subgoal_id_path_traversal_blocked(self, git_repo: Path, tmp_path: Path):
        victim_dir = tmp_path / "victim_external"
        victim_dir.mkdir()
        canary = victim_dir / "canary.txt"
        canary.write_text("CRITICAL_EXTERNAL_DATA", encoding="utf-8")

        ws = Workspace(repo_dir=git_repo)

        # Attempt to reach victim_dir via traversal
        malicious_ids = [
            "x/../../../../victim_external",
            r"x\..\..\..\..\victim_external",
            "/victim_external",
            "../victim_external",
            "subgoal/nested",
            "id with spaces",
            "id;semicolon",
            "",
            "   ",
        ]

        for bad_id in malicious_ids:
            with pytest.raises(ValueError, match=r"Invalid subgoal_id|escapes worktree"):
                ws.create_subgoal_worktree(bad_id)

        # Verify canary and victim directory are untouched
        assert victim_dir.exists(), "Victim directory was unexpectedly deleted!"
        assert canary.read_text(encoding="utf-8") == "CRITICAL_EXTERNAL_DATA"

    def test_subgoal_model_validation(self):
        # Empty id is permitted as default
        sg = Subgoal()
        assert sg.id == ""

        # Valid ids
        assert Subgoal(id="sg-1").id == "sg-1"
        assert Subgoal(id="valid_123_test").id == "valid_123_test"

        # Traversal / invalid ids rejected by model
        for bad_id in ["../traversal", "x/../y", "has space", "bad;char"]:
            with pytest.raises(ValueError):
                Subgoal(id=bad_id)


class TestR5NodeModulesContainment:
    """VULN-2: Prevent host read escape via external node_modules symlink/junction."""

    def test_node_modules_external_link_creation_blocked(self, tmp_path: Path):
        import sys
        repo = tmp_path / "repo"
        repo.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("SECRET_OUTSIDE", encoding="utf-8")

        nm = repo / "node_modules"
        if sys.platform == "win32":
            import _winapi
            _winapi.CreateJunction(str(outside), str(nm))
        else:
            import os
            os.symlink(str(outside), str(nm), target_is_directory=True)

        wt = tmp_path / "wt"
        wt.mkdir()

        Workspace._link_node_modules(repo, wt)
        assert not (wt / "node_modules").exists(), "_link_node_modules created link to external target!"

    def test_node_modules_external_symlink_read_blocked(self, tmp_path: Path):
        import sys
        repo = tmp_path / "repo"
        repo.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("SECRET_OUTSIDE", encoding="utf-8")

        nm = repo / "node_modules"
        wt = tmp_path / "wt"
        wt.mkdir()
        wt_nm = wt / "node_modules"

        if sys.platform == "win32":
            import _winapi
            _winapi.CreateJunction(str(outside), str(nm))
            _winapi.CreateJunction(str(outside), str(wt_nm))
        else:
            import os
            os.symlink(str(outside), str(nm), target_is_directory=True)
            os.symlink(str(outside), str(wt_nm), target_is_directory=True)

        ws = Workspace(repo_dir=repo)
        ws.worktree_dir = wt

        with pytest.raises((ValueError, PermissionError)):
            ws._resolve_for_read("node_modules/secret.txt")

        res = ws.run_read_tool("cat", ["node_modules/secret.txt"])
        assert "SECRET_OUTSIDE" not in res


class TestR5TrustRecordPersistence:
    """VULN-3: Prevent symlink attacks and partial writes on trust records."""

    def test_record_trust_rejects_symlinked_target(self, tmp_path: Path, monkeypatch):
        from jev.repo_config import record_trust, RepoConfig, compute_repo_id
        jev_home = tmp_path / "jev_home"
        monkeypatch.setenv("JEV_HOME", str(jev_home))

        repo = tmp_path / "repo"
        repo.mkdir()
        victim = tmp_path / "victim_data.txt"
        victim.write_text("CANARY_VICTIM_DATA", encoding="utf-8")

        repo_id = compute_repo_id(repo)
        trust_dir = jev_home / "trust"
        trust_dir.mkdir(parents=True, exist_ok=True)
        trust_file = trust_dir / f"{repo_id}.json"

        # Create target symlink pointing to victim file (e.g., inside or outside trust dir)
        # We test with another file inside trust_dir as well
        victim_inside = trust_dir / "other_repo.json"
        victim_inside.write_text("OTHER_REPO_TRUST_DATA", encoding="utf-8")
        trust_file.symlink_to(victim_inside)

        cfg = RepoConfig(build_command="echo safe")
        with pytest.raises(PermissionError, match=r"cannot be a symlink"):
            record_trust(cfg, repo)

        assert victim_inside.read_text(encoding="utf-8") == "OTHER_REPO_TRUST_DATA", "Symlink target was overwritten!"

    def test_validate_trust_rejects_symlink(self, tmp_path: Path, monkeypatch):
        from jev.repo_config import validate_trust, RepoConfig, compute_repo_id
        jev_home = tmp_path / "jev_home"
        monkeypatch.setenv("JEV_HOME", str(jev_home))

        repo = tmp_path / "repo"
        repo.mkdir()
        repo_id = compute_repo_id(repo)
        trust_dir = jev_home / "trust"
        trust_dir.mkdir(parents=True, exist_ok=True)
        trust_file = trust_dir / f"{repo_id}.json"

        valid_file = trust_dir / "valid.json"
        valid_file.write_text('{"repo_id": "' + repo_id + '"}', encoding="utf-8")
        trust_file.symlink_to(valid_file)

        cfg = RepoConfig()
        is_trusted, reason = validate_trust(cfg, repo)
        assert not is_trusted
        assert "symlink" in str(reason).lower() or "untrusted" in str(reason).lower()

    def test_record_trust_atomic_replace_and_roundtrip(self, tmp_path: Path, monkeypatch):
        from jev.repo_config import record_trust, validate_trust, RepoConfig, get_trust_file_path, compute_repo_id
        jev_home = tmp_path / "jev_home"
        monkeypatch.setenv("JEV_HOME", str(jev_home))

        repo = tmp_path / "repo"
        repo.mkdir()

        cfg = RepoConfig(build_command="echo build_success")
        rec = record_trust(cfg, repo)
        assert rec.repo_id == compute_repo_id(repo)

        trust_file = get_trust_file_path(rec.repo_id)
        assert trust_file.exists()
        assert not trust_file.is_symlink()

        is_trusted, err = validate_trust(cfg, repo)
        assert is_trusted, f"Validation failed: {err}"

    def test_record_trust_rejects_symlinked_trust_dir(self, tmp_path: Path, monkeypatch):
        from jev.repo_config import record_trust, RepoConfig
        jev_home = tmp_path / "jev_home"
        monkeypatch.setenv("JEV_HOME", str(jev_home))

        outside_dir = tmp_path / "outside_dir"
        outside_dir.mkdir()

        trust_dir = jev_home / "trust"
        jev_home.mkdir(parents=True, exist_ok=True)
        trust_dir.symlink_to(outside_dir, target_is_directory=True)

        repo = tmp_path / "repo"
        repo.mkdir()
        cfg = RepoConfig()
        with pytest.raises(PermissionError, match=r"symlinked trust directory|cannot be a symlink"):
            record_trust(cfg, repo)

    def test_record_trust_rejects_junctioned_trust_dir(self, tmp_path: Path, monkeypatch):
        from jev.repo_config import record_trust, RepoConfig
        jev_home = tmp_path / "jev_home"
        monkeypatch.setenv("JEV_HOME", str(jev_home))

        outside_dir = tmp_path / "outside_dir"
        outside_dir.mkdir()

        trust_dir = jev_home / "trust"
        jev_home.mkdir(parents=True, exist_ok=True)
        make_dir_link(outside_dir, trust_dir)

        repo = tmp_path / "repo"
        repo.mkdir()
        cfg = RepoConfig()
        with pytest.raises(PermissionError, match=r"symlink|junction"):
            record_trust(cfg, repo)

    def test_validate_trust_rejects_junctioned_trust_dir(self, tmp_path: Path, monkeypatch):
        from jev.repo_config import validate_trust, RepoConfig, compute_repo_id
        jev_home = tmp_path / "jev_home"
        monkeypatch.setenv("JEV_HOME", str(jev_home))

        outside_dir = tmp_path / "outside_dir"
        outside_dir.mkdir()

        trust_dir = jev_home / "trust"
        jev_home.mkdir(parents=True, exist_ok=True)
        make_dir_link(outside_dir, trust_dir)

        repo = tmp_path / "repo"
        repo.mkdir()
        repo_id = compute_repo_id(repo)

        (outside_dir / f"{repo_id}.json").write_text('{"repo_id": "' + repo_id + '"}', encoding="utf-8")

        cfg = RepoConfig()
        is_trusted, reason = validate_trust(cfg, repo)
        assert not is_trusted
        assert "symlink" in str(reason).lower() or "junction" in str(reason).lower()

    def test_is_junction_or_symlink_detects_broken_junction(self, tmp_path: Path):
        import sys
        if sys.platform != "win32":
            pytest.skip("Windows NTFS junction test only")
        from jev.repo_config import _is_junction_or_symlink
        tgt = tmp_path / "target_dir"
        tgt.mkdir()
        junc = tmp_path / "junc_dir"
        make_dir_link(tgt, junc)
        tgt.rmdir()
        assert not junc.exists()
        assert _is_junction_or_symlink(junc) is True


class TestR5WorktreeDiscardContainment:
    """VULN-4: Prevent deletion of repository root or external directories via discard functions."""

    def test_discard_verify_worktree_refuses_repo_root(self, git_repo: Path):
        ws = Workspace(repo_dir=git_repo)
        with pytest.raises(ValueError, match=r"Refusing to delete repository root"):
            ws.discard_verify_worktree(git_repo)
        assert git_repo.exists(), "Repository root was deleted!"

    def test_discard_verify_worktree_refuses_external_path(self, git_repo: Path, tmp_path: Path):
        outside = tmp_path / "outside_dir"
        outside.mkdir()
        canary = outside / "canary.txt"
        canary.write_text("CANARY", encoding="utf-8")

        ws = Workspace(repo_dir=git_repo)
        with pytest.raises(ValueError, match=r"escapes worktree directory|Refusing"):
            ws.discard_verify_worktree(outside)
        assert outside.exists(), "External directory was deleted!"
        assert canary.exists()

    def test_discard_subgoal_worktree_refuses_repo_root(self, git_repo: Path):
        ws = Workspace(repo_dir=git_repo)
        with pytest.raises(ValueError, match=r"Refusing to delete repository root"):
            ws.discard_subgoal_worktree(git_repo)
        assert git_repo.exists(), "Repository root was deleted!"

    def test_discard_subgoal_worktree_refuses_external_path(self, git_repo: Path, tmp_path: Path):
        outside = tmp_path / "outside_dir"
        outside.mkdir()
        canary = outside / "canary.txt"
        canary.write_text("CANARY", encoding="utf-8")

        ws = Workspace(repo_dir=git_repo)
        with pytest.raises(ValueError, match=r"escapes worktree directory|Refusing"):
            ws.discard_subgoal_worktree(outside)
        assert outside.exists(), "External directory was deleted!"
        assert canary.exists()


class TestR5WorktreeBaseSymlinkBoundary:
    """Tests for worktree base directory symlink/junction and symlinked worktree target defenses."""

    def test_discard_through_symlinked_base_rejected(self, git_repo: Path, tmp_path: Path):
        outside = tmp_path / "outside_worktrees"
        outside.mkdir()
        canary = outside / "canary.txt"
        canary.write_text("OUTSIDE_CANARY", encoding="utf-8")
        victim_sub = outside / "subgoal-1"
        victim_sub.mkdir()
        victim_file = victim_sub / "important.txt"
        victim_file.write_text("DO_NOT_DELETE", encoding="utf-8")

        base_link = git_repo / ".jev-worktrees"
        make_dir_link(outside, base_link)

        ws = Workspace(repo_dir=git_repo)
        target_path = base_link / "subgoal-1"

        with pytest.raises((ValueError, PermissionError), match=r"symlink|junction|escapes|refusing"):
            ws.discard_subgoal_worktree(target_path)

        with pytest.raises((ValueError, PermissionError), match=r"symlink|junction|escapes|refusing"):
            ws.discard_verify_worktree(target_path)

        assert outside.exists(), "Outside directory was removed!"
        assert canary.exists(), "Canary file in outside directory was deleted!"
        assert victim_file.exists(), "Victim file was deleted!"

    def test_create_subgoal_through_symlinked_base_rejected(self, git_repo: Path, tmp_path: Path):
        outside = tmp_path / "outside_worktrees"
        outside.mkdir()
        canary = outside / "canary.txt"
        canary.write_text("OUTSIDE_CANARY", encoding="utf-8")

        base_link = git_repo / ".jev-worktrees"
        make_dir_link(outside, base_link)

        ws = Workspace(repo_dir=git_repo)

        with pytest.raises((ValueError, PermissionError), match=r"symlink|junction|escapes|refusing"):
            ws.create_subgoal_worktree("sg1")

        assert canary.exists()
        assert not (outside / "subgoal-sg1").exists(), "Worktree directory was created inside outside dir!"

    def test_discard_symlinked_worktree_target_rejected(self, git_repo: Path, tmp_path: Path):
        outside = tmp_path / "outside_victim"
        outside.mkdir()
        canary = outside / "canary.txt"
        canary.write_text("VICTIM_CANARY", encoding="utf-8")

        worktrees_dir = git_repo / ".jev-worktrees"
        worktrees_dir.mkdir(parents=True, exist_ok=True)
        target_link = worktrees_dir / "subgoal-symtarget"
        make_dir_link(outside, target_link)

        ws = Workspace(repo_dir=git_repo)

        with pytest.raises((ValueError, PermissionError), match=r"symlink|junction|escapes|refusing"):
            ws.discard_subgoal_worktree(target_link)

        with pytest.raises((ValueError, PermissionError), match=r"symlink|junction|escapes|refusing"):
            ws.discard_verify_worktree(target_link)

        assert outside.exists(), "Outside victim directory was deleted!"
        assert canary.exists()
