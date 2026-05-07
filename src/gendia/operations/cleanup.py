"""`gendia cleanup`: remove ephemeral build / cache / OS-junk artifacts.

Globs come from `repo.cleanup_globs` then `project.cleanup_globs` (per-repo
overrides win). All deletions are confined to the repo's directory; absolute
paths and `..` segments are rejected to prevent foot-guns.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from gendia.config.schema import RepoSpec
from gendia.operations.base import Operation, RepoResult


class CleanupOperation(Operation):
    name = "cleanup"

    def _apply_to_repo(self, repo: RepoSpec) -> RepoResult:
        path = self._ctx.repo_path(repo)
        if not path.is_dir():
            return RepoResult(dir=repo.dir, ok=False, error=f"not a directory: {path}")

        globs = tuple(repo.cleanup_globs) or tuple(self._ctx.project.cleanup_globs)
        if not globs:
            return RepoResult(dir=repo.dir, ok=True, summary="no cleanup_globs configured")

        removed: list[str] = []
        skipped: list[str] = []

        path_resolved = path.resolve()

        for glob in globs:
            if Path(glob).is_absolute() or ".." in Path(glob).parts:
                skipped.append(f"{glob} (absolute or contains ..)")
                continue
            for match in path.glob(glob):
                # Guard against symlinks pointing outside the repo.
                if not match.resolve().is_relative_to(path_resolved):
                    skipped.append(f"{match} (escapes repo root)")
                    continue
                removed.append(str(match.relative_to(path)))
                if not self._ctx.dry_run:
                    if match.is_dir() and not match.is_symlink():
                        shutil.rmtree(match, ignore_errors=True)
                    else:
                        match.unlink(missing_ok=True)

        return RepoResult(
            dir=repo.dir,
            ok=True,
            summary=f"{len(removed)} removed, {len(skipped)} skipped",
            detail={"removed": removed, "skipped": skipped},
        )
