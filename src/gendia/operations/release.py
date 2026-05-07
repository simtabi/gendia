"""`gendia release <repo> <version>`: tag, push, notify.

Targets a single repo (the `<repo>` arg = repo `dir` in the project config).
Validates: clean tree, on main, version is semver-shaped, tag doesn't exist
locally or on origin. Adds a CHANGELOG entry, commits, tags, pushes, then
delegates to a sync run for the registry notification.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from gendia.config.schema import RepoSpec
from gendia.git.exceptions import NotAGitRepositoryError
from gendia.git.repo import GitRepo
from gendia.operations.base import Operation, OperationContext, RepoResult
from gendia.registries.base import PublishCapable, RegistryError, WebhookCapable
from gendia.state import SyncRecord, SyncStateStore

_SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$")


class ReleaseOperation(Operation):
    name = "release"

    def __init__(self, ctx: OperationContext, *, target_dir: str, version: str) -> None:
        super().__init__(ctx)
        if not _SEMVER.match(version):
            raise ValueError(f"version must be x.y.z[-pre], got {version!r}")
        self._target_dir = target_dir
        self._version = version

    def _apply_to_repo(self, repo: RepoSpec) -> RepoResult:
        # Filter: only the requested repo runs.
        if repo.dir != self._target_dir:
            return RepoResult(dir=repo.dir, ok=True, summary="skipped (not target)")

        path = self._ctx.repo_path(repo)
        api_token = self._ctx.provider._token  # noqa: SLF001 — internal contract
        try:
            with self._ctx.build_git_env(api_token=api_token) as auth:
                env = dict(auth.env)
                git = GitRepo(path, env_provider=lambda: env)
                return self._release_one(repo, git, path, transport=auth.transport)
        except NotAGitRepositoryError as exc:
            return RepoResult(dir=repo.dir, ok=False, error=str(exc))

    def _release_one(  # noqa: PLR0911, PLR0912 — early-return validation + state-record branches
        self,
        repo: RepoSpec,
        git: GitRepo,
        path: Path,
        *,
        transport: str,
    ) -> RepoResult:
        if not git.is_clean():
            return RepoResult(dir=repo.dir, ok=False, error="dirty tree")
        if git.current_branch() != "main":
            return RepoResult(dir=repo.dir, ok=False, error="not on main")
        tag_name = f"v{self._version}"
        if tag_name in git.tags():
            return RepoResult(dir=repo.dir, ok=False, error=f"tag {tag_name} already local")
        if tag_name in git.remote_tags():
            return RepoResult(dir=repo.dir, ok=False, error=f"tag {tag_name} already on origin")

        changelog = path / "CHANGELOG.md"
        if changelog.is_file():
            self._prepend_changelog(changelog, self._version)
            if not self._ctx.dry_run:
                git.commit_all(f"chore: release v{self._version}")

        if self._ctx.dry_run:
            self._log.info("would tag + push", extra={"dir": repo.dir, "tag": tag_name})
            return RepoResult(
                dir=repo.dir,
                ok=True,
                summary=f"DRY-RUN release v{self._version} (transport={transport})",
            )

        git.tag(tag_name)
        git.push(remote="origin", ref="main")
        git.push_tag(tag_name)

        # Two registry kinds, two paths. Webhook-style (Packagist) just gets
        # told the repo URL. Publish-style (npm, PyPI) gets the package
        # contents pushed via its native CLI.
        registry_outcome = "no registry"
        registry = self._ctx.registry
        if registry is not None:
            try:
                if isinstance(registry, WebhookCapable):
                    web_url = self._ctx.provider.web_url(repo.package or repo.dir)
                    registry.notify(web_url)
                    registry_outcome = f"notified {registry.kind}"
                elif isinstance(registry, PublishCapable):
                    registry.publish(path, version=self._version)
                    registry_outcome = f"published to {registry.kind}"
                else:
                    registry_outcome = f"registry {registry.kind} not supported"
            except RegistryError as exc:
                registry_outcome = f"{registry.kind} failed"
                self._log.warning(
                    "registry step failed",
                    extra={"kind": registry.kind, "error": str(exc)},
                )

        # Persist the release record so `gendia inventory` can surface the tag.
        if not self._ctx.dry_run:
            store = SyncStateStore()
            store.upsert(
                SyncRecord.now(
                    package=repo.package or repo.dir,
                    account=self._ctx.provider.account.name,
                    action="release",
                    status="ok",
                    tag=tag_name,
                    transport=transport,
                    extra={"dir": repo.dir, "registry_outcome": registry_outcome},
                )
            )
            try:
                store.save()
            except OSError as exc:
                self._log.warning("could not persist release state", extra={"error": str(exc)})

        return RepoResult(
            dir=repo.dir,
            ok=True,
            summary=f"released v{self._version} via {transport}, {registry_outcome}",
            detail={
                "tag": tag_name,
                "transport": transport,
                "registry_outcome": registry_outcome,
            },
        )

    @staticmethod
    def _prepend_changelog(path: Path, version: str) -> None:
        existing = path.read_text(encoding="utf-8")
        block = (
            f"## [{version}] - {date.today().isoformat()}\n\n"
            "### Added\n\n- (describe what changed)\n\n"
        )

        # Insert above the first existing version block; if none, prepend after
        # the file's H1 + intro paragraph.
        marker_idx = existing.find("\n## [")
        if marker_idx == -1:
            new = existing.rstrip() + "\n\n" + block
        else:
            new = existing[: marker_idx + 1] + block + existing[marker_idx + 1 :]
        path.write_text(new, encoding="utf-8")
