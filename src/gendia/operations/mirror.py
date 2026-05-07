"""`gendia mirror`: clone every repo defined in a project to a target dir."""

from __future__ import annotations

from pathlib import Path

from gendia.config.schema import RepoSpec
from gendia.git.shell import run as shell_run
from gendia.operations.base import Operation, OperationContext, RepoResult


class MirrorOperation(Operation):
    name = "mirror"

    def __init__(self, ctx: OperationContext, *, target: Path) -> None:
        super().__init__(ctx)
        self._target = target.expanduser().resolve()
        self._target.mkdir(parents=True, exist_ok=True)

    def _apply_to_repo(self, repo: RepoSpec) -> RepoResult:
        dest = self._target / repo.dir
        clone_url = self._ctx.provider.clone_url(repo.package or repo.dir, ssh=True)

        if dest.exists():
            return RepoResult(
                dir=repo.dir,
                ok=True,
                summary=f"already exists at {dest}",
            )

        if self._ctx.dry_run:
            return RepoResult(
                dir=repo.dir,
                ok=True,
                summary=f"would clone {clone_url} -> {dest}",
            )

        shell_run(["git", "clone", clone_url, str(dest)], timeout=600.0)
        return RepoResult(dir=repo.dir, ok=True, summary=f"cloned to {dest}")
