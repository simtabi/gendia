"""`gendia status`: a quick read-only view of every repo."""

from __future__ import annotations

from gendia.config.schema import RepoSpec
from gendia.git.exceptions import NotAGitRepositoryError
from gendia.git.repo import GitRepo
from gendia.operations.base import Operation, RepoResult


class StatusOperation(Operation):
    name = "status"

    def _apply_to_repo(self, repo: RepoSpec) -> RepoResult:
        path = self._ctx.repo_path(repo)
        try:
            git = GitRepo(path)
        except NotAGitRepositoryError as exc:
            return RepoResult(dir=repo.dir, ok=False, error=str(exc))

        clean = git.is_clean()
        branch = git.current_branch() or "DETACHED"
        try:
            state = git.remote_state(branch=branch)
            ahead = state.ahead
            behind = state.behind
        except Exception:  # noqa: BLE001
            ahead = behind = 0

        local_tags = git.tags()
        latest = local_tags[-1] if local_tags else "—"

        summary = (
            f"{branch} {'clean' if clean else 'dirty'} "
            f"ahead={ahead} behind={behind} latest_tag={latest}"
        )
        return RepoResult(
            dir=repo.dir,
            ok=clean,
            summary=summary,
            detail={
                "branch": branch,
                "clean": clean,
                "ahead": ahead,
                "behind": behind,
                "latest_tag": latest,
                "head": git.head_sha(),
            },
        )
