"""`gendia sync`: fast-forward push and notify webhook registries.

Idempotent. Never force-pushes. Never edits history. Refuses to act on a
dirty working tree.
"""

from __future__ import annotations

from gendia.config.schema import RepoSpec
from gendia.git.exceptions import NotAGitRepositoryError
from gendia.git.repo import GitRepo
from gendia.operations.base import Operation, OperationContext, RepoResult
from gendia.providers.base import ProviderError
from gendia.registries.base import RegistryError, WebhookCapable
from gendia.state import SyncRecord, SyncStateStore


class SyncOperation(Operation):
    name = "sync"

    def __init__(self, ctx: OperationContext) -> None:
        super().__init__(ctx)
        self._state = SyncStateStore()

    def _apply_to_repo(self, repo: RepoSpec) -> RepoResult:
        path = self._ctx.repo_path(repo)
        try:
            # Build a fresh auth env per repo so SSH/HTTPS choices stay scoped.
            api_token = self._ctx.provider._token  # noqa: SLF001 — internal contract
            with self._ctx.build_git_env(api_token=api_token) as auth:
                env = dict(auth.env)
                git = GitRepo(path, env_provider=lambda: env)
                result = self._sync_one(repo, git, transport=auth.transport)
                self._record(repo, result, transport=auth.transport)
                return result
        except NotAGitRepositoryError as exc:
            err = RepoResult(dir=repo.dir, ok=False, error=str(exc))
            self._record(repo, err, transport=None)
            return err

    def _record(self, repo: RepoSpec, result: RepoResult, *, transport: str | None) -> None:
        """Persist the per-package outcome so `gendia inventory` can surface it."""
        if self._ctx.dry_run:
            return
        package = repo.package or repo.dir
        status = "ok" if result.ok else ("dirty" if "dirty" in (result.error or "") else "failed")
        last_tag = (result.detail.get("pushed_tags") or [None])[-1] if result.ok else None
        self._state.upsert(
            SyncRecord.now(
                package=package,
                account=self._ctx.provider.account.name,
                action="sync",
                status=status,
                tag=last_tag,
                transport=transport,
                error=result.error,
                extra={"dir": repo.dir},
            )
        )
        try:
            self._state.save()
        except OSError as exc:
            self._log.warning("could not persist sync state", extra={"error": str(exc)})

    def _sync_one(  # noqa: PLR0912 — orchestration verb naturally branches on push/tag/notify state
        self,
        repo: RepoSpec,
        git: GitRepo,
        *,
        transport: str,
    ) -> RepoResult:
        if not git.is_clean():
            return RepoResult(
                dir=repo.dir,
                ok=False,
                error="dirty working tree; commit or stash first",
            )

        branch = git.current_branch()
        if branch != "main":
            return RepoResult(
                dir=repo.dir,
                ok=False,
                error=f"not on main (on {branch!r}); won't push",
            )

        state = git.remote_state(branch=branch)
        pushed_branch = False
        if state.ahead > 0:
            if self._ctx.dry_run:
                self._log.info("would push branch", extra={"dir": repo.dir, "ahead": state.ahead})
            else:
                git.push(remote="origin", ref="main")
            pushed_branch = True

        local_tags = set(git.tags())
        remote_tags = set(git.remote_tags())
        new_tags = sorted(local_tags - remote_tags)

        pushed_tags: list[str] = []
        for tag in new_tags:
            if self._ctx.dry_run:
                self._log.info("would push tag", extra={"dir": repo.dir, "tag": tag})
            else:
                git.push_tag(tag)
            pushed_tags.append(tag)

        notified = False
        if pushed_tags and self._ctx.registry is not None:
            try:
                self._notify(repo, git)
                notified = True
            except (RegistryError, ProviderError) as exc:
                self._log.warning(
                    "registry notify failed",
                    extra={"dir": repo.dir, "error": str(exc)},
                )

        summary_bits = [f"transport={transport}"]
        if pushed_branch:
            summary_bits.append(f"branch+{state.ahead}")
        if pushed_tags:
            summary_bits.append(f"tags={','.join(pushed_tags)}")
        if notified:
            summary_bits.append("notified")
        if not pushed_branch and not pushed_tags:
            summary_bits.append("in sync")

        return RepoResult(
            dir=repo.dir,
            ok=True,
            summary=" ".join(summary_bits),
            detail={
                "transport": transport,
                "ahead": state.ahead,
                "behind": state.behind,
                "pushed_branch": pushed_branch,
                "pushed_tags": pushed_tags,
                "notified": notified,
            },
        )

    def _notify(self, repo: RepoSpec, git: GitRepo) -> None:
        registry = self._ctx.registry
        if not isinstance(registry, WebhookCapable):
            return  # publish-style registries handled by `release`, not `sync`
        if self._ctx.dry_run:
            self._log.info("would notify registry", extra={"dir": repo.dir, "kind": registry.kind})
            return

        # Use the HTTPS URL form for Packagist/etc., regardless of the SSH
        # remote we push to. Strip any user-info components.
        web_url = self._ctx.provider.web_url(repo.package or repo.dir)
        registry.notify(web_url)
