"""GitRepo wrapper: smoke tests against a real repo in a tmpdir."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gendia.git import GitRepo, NotAGitRepositoryError


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "--initial-branch=main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "README.md").write_text("hi", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "init")
    return tmp_path


def test_rejects_non_git_path(tmp_path: Path) -> None:
    with pytest.raises(NotAGitRepositoryError):
        GitRepo(tmp_path / "no-such-dir-abc-123")


def test_clean_branch_head(repo: Path) -> None:
    g = GitRepo(repo)
    assert g.is_clean()
    assert g.current_branch() == "main"
    assert len(g.head_sha()) == 40


def test_dirty_after_modify(repo: Path) -> None:
    (repo / "new.txt").write_text("x", encoding="utf-8")
    assert not GitRepo(repo).is_clean()


def test_tags_listed_in_order(repo: Path) -> None:
    _git(repo, "tag", "v0.1.0")
    _git(repo, "commit", "--allow-empty", "-m", "tick")
    _git(repo, "tag", "v0.2.0")
    g = GitRepo(repo)
    assert g.tags() == ["v0.1.0", "v0.2.0"]
