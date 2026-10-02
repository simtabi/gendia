"""Filesystem scanner for git repos."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gendia.util.repo_scan import scan


def _git_init(path: Path, *, remote: str | None = None) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    if remote:
        subprocess.run(["git", "remote", "add", "origin", remote], cwd=path, check=True)


@pytest.fixture(autouse=True)
def _isolate_git_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Avoid leaking the user's global git config into tests."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")


def test_finds_git_dirs(tmp_path: Path) -> None:
    _git_init(tmp_path / "alpha", remote="git@github.com:foo/alpha.git")
    _git_init(tmp_path / "beta")  # orphan
    _git_init(tmp_path / "gamma", remote="https://github.com/foo/gamma.git")

    repos = scan(tmp_path)
    by_name = {repo.path.name: repo for repo in repos}
    assert set(by_name) == {"alpha", "beta", "gamma"}

    assert by_name["alpha"].classify() == "clean"
    assert by_name["beta"].classify() == "orphan"
    assert by_name["gamma"].classify() == "https-candidate"


def test_skips_well_known_build_dirs(tmp_path: Path) -> None:
    nested = tmp_path / "node_modules" / "innocent"
    _git_init(nested, remote="git@github.com:x/y.git")

    repos = scan(tmp_path)
    assert all("node_modules" not in str(r.path) for r in repos)


def test_dirty_detection_when_requested(tmp_path: Path) -> None:
    _git_init(tmp_path / "dirty", remote="git@github.com:x/y.git")
    untracked = tmp_path / "dirty" / "scratch.txt"
    untracked.write_text("hello", encoding="utf-8")

    repos = scan(tmp_path, with_dirty=True)
    assert len(repos) == 1
    assert repos[0].dirty is True
    assert repos[0].classify() == "dirty"


def test_origin_extraction(tmp_path: Path) -> None:
    _git_init(tmp_path / "alpha", remote="git@github.com:foo/alpha.git")
    repos = scan(tmp_path)
    assert repos[0].origin == "git@github.com:foo/alpha.git"
