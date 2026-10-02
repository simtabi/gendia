"""Walk a directory tree, find git repos, classify each.

Used by `gendia scan` and `gendia setup --from-ssh` to surface repos
that aren't yet under gendia's management. We deliberately avoid running
`git status` per repo (slow on a large tree); we only call git when the
caller asks for `with_dirty=True`.
"""

from __future__ import annotations

import configparser
import os
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from gendia.util.url import parse


@dataclass(frozen=True, slots=True)
class ScannedRepo:
    """One git repo discovered on disk."""

    path: Path
    remotes: tuple[tuple[str, str], ...] = field(default_factory=tuple)  # (name, url) pairs
    dirty: bool | None = None  # None when status not checked

    @property
    def origin(self) -> str | None:
        for name, url in self.remotes:
            if name == "origin":
                return url
        return None

    @property
    def is_orphan(self) -> bool:
        """No remotes at all — never pushed anywhere."""
        return not self.remotes

    @property
    def is_https_candidate(self) -> bool:
        """`origin` is an https URL that could be flipped to SSH."""
        origin = self.origin
        if not origin:
            return False
        try:
            parsed = parse(origin)
        except ValueError:
            return False
        return parsed.scheme == "https"

    def classify(self) -> str:
        if self.is_orphan:
            return "orphan"
        if self.dirty is True:
            return "dirty"
        if self.is_https_candidate:
            return "https-candidate"
        return "clean"


def scan(
    root: Path,
    *,
    with_dirty: bool = False,
    skip_dirs: tuple[str, ...] = (
        "node_modules",
        "vendor",
        ".venv",
        "venv",
        "__pycache__",
        "dist",
        "build",
    ),
) -> list[ScannedRepo]:
    """Walk `root` and return one `ScannedRepo` per `.git` directory found.

    `.git` files (worktree pointer files) are followed — we treat the
    pointed-at gitdir as the repo root for remote-reading.
    """
    return list(_walk(root.resolve(), with_dirty=with_dirty, skip_dirs=set(skip_dirs)))


def _walk(root: Path, *, with_dirty: bool, skip_dirs: set[str]) -> Iterator[ScannedRepo]:
    if not root.is_dir():
        return

    for dirpath, dirnames, _ in os.walk(root):
        dpath = Path(dirpath)
        # Don't descend into well-known build/cache trees.
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]

        if ".git" in dirnames or (dpath / ".git").exists():
            git_dir = _resolve_git_dir(dpath)
            if git_dir is None:
                continue
            remotes = _read_remotes(git_dir)
            dirty = _is_dirty(dpath) if with_dirty else None
            yield ScannedRepo(path=dpath, remotes=remotes, dirty=dirty)
            # Don't descend into the .git dir or nested submodules; we
            # captured the parent and submodule scanning is a separate concern.
            dirnames[:] = []


def _resolve_git_dir(repo_root: Path) -> Path | None:
    """Resolve `.git` to the actual gitdir (handles worktrees + submodules)."""
    git_path = repo_root / ".git"
    if git_path.is_dir():
        return git_path
    if git_path.is_file():
        try:
            text = git_path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if text.startswith("gitdir:"):
            target = Path(text.split(":", 1)[1].strip())
            if not target.is_absolute():
                target = repo_root / target
            return target.resolve() if target.exists() else None
    return None


def _read_remotes(git_dir: Path) -> tuple[tuple[str, str], ...]:
    config_path = git_dir / "config"
    if not config_path.is_file():
        return ()

    parser = configparser.ConfigParser(strict=False)
    try:
        parser.read(config_path, encoding="utf-8")
    except (configparser.Error, OSError):
        return ()

    out: list[tuple[str, str]] = []
    for section in parser.sections():
        if not section.startswith('remote "') or not section.endswith('"'):
            continue
        name = section[len('remote "') : -1]
        url = parser.get(section, "url", fallback="")
        if url:
            out.append((name, url))
    return tuple(out)


def _is_dirty(repo_root: Path) -> bool | None:
    """Return True if `git status --porcelain` reports any changes."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return bool(proc.stdout.strip())


def to_dict(repo: ScannedRepo) -> dict[str, object]:
    """Render a `ScannedRepo` as JSON-friendly data."""
    return {
        "path": str(repo.path),
        "classification": repo.classify(),
        "origin": repo.origin,
        "remotes": [{"name": n, "url": u} for n, u in repo.remotes],
        "dirty": repo.dirty,
        "is_orphan": repo.is_orphan,
        "is_https_candidate": repo.is_https_candidate,
    }
