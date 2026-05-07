"""Operation contract: every CLI verb is a subclass of `Operation`.

Operations are stateless except for their `OperationContext`; instantiate
once, call `.run()` once, throw away. The context bundles everything an op
needs (config, provider, registry, dry-run flag) so we don't pass long arg
lists.

Subclasses implement `_apply_to_repo(repo)`, the per-repo step. The base
class handles fan-out concurrency, error capture, and result aggregation.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Self

from gendia.config.schema import GendiaConfig, Project, RepoSpec
from gendia.git.auth import AuthEnvironment, build_environment
from gendia.observability.logger import get_logger
from gendia.providers.base import GitProvider
from gendia.registries.base import PackageRegistry
from gendia.util.concurrency import map_repos


@dataclass(frozen=True, slots=True)
class OperationContext:
    """Inputs an operation needs to do its job."""

    config: GendiaConfig
    project: Project
    provider: GitProvider
    registry: PackageRegistry | None
    dry_run: bool = False
    workers: int = 4
    only: frozenset[str] = frozenset()  # filter to these repo dirs
    skip: frozenset[str] = frozenset()  # exclude these repo dirs

    def selected_repos(self) -> tuple[RepoSpec, ...]:
        """Apply `only` / `skip` filters to the project's repo list."""
        repos = self.project.repos
        if self.only:
            repos = tuple(r for r in repos if r.dir in self.only)
        if self.skip:
            repos = tuple(r for r in repos if r.dir not in self.skip)
        return repos

    def repo_path(self, repo: RepoSpec) -> Path:
        return self.project.root / repo.dir

    def build_git_env(self, *, api_token: str | None = None) -> AuthEnvironment:
        """Build the env vars git subprocesses should run with for this account.

        Use as a context manager so temp ASKPASS scripts are cleaned up:

            with ctx.build_git_env(api_token=token) as auth:
                git = GitRepo(path, env_provider=lambda: auth.env)
                git.push(...)
        """
        return build_environment(self.provider.account, api_token=api_token)


@dataclass
class RepoResult:
    """Outcome of one repo's contribution to an operation."""

    dir: str
    ok: bool
    summary: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class OperationResult:
    name: str
    dry_run: bool
    repos: list[RepoResult] = field(default_factory=list)

    @property
    def all_ok(self) -> bool:
        return all(r.ok for r in self.repos)

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.name,
            "dry_run": self.dry_run,
            "all_ok": self.all_ok,
            "repos": [
                {
                    "dir": r.dir,
                    "ok": r.ok,
                    "summary": r.summary,
                    "detail": r.detail,
                    "error": r.error,
                }
                for r in self.repos
            ],
        }


class Operation(ABC):
    """Stateless verb. One instance = one run."""

    name: str = ""

    def __init__(self, ctx: OperationContext) -> None:
        if not self.name:
            raise TypeError(f"{type(self).__name__}: must set `name`")
        self._ctx = ctx
        self._log = get_logger(f"ops.{self.name}")

    # --- chainable mutators (return self) -----------------------------------

    def with_dry_run(self, value: bool = True) -> Self:
        object.__setattr__(self, "_ctx", _replace_ctx(self._ctx, dry_run=value))
        return self

    def with_workers(self, n: int) -> Self:
        object.__setattr__(self, "_ctx", _replace_ctx(self._ctx, workers=n))
        return self

    # --- public contract ----------------------------------------------------

    def run(self) -> OperationResult:
        """Run across the selected repos and aggregate results."""
        result = OperationResult(name=self.name, dry_run=self._ctx.dry_run)
        repos = self._ctx.selected_repos()
        if not repos:
            self._log.warning("no repos selected (only/skip filters too narrow?)")
            return result

        self._log.info(
            "operation start",
            extra={"op": self.name, "repos": len(repos), "dry_run": self._ctx.dry_run},
        )

        for repo, outcome in map_repos(self._apply_safely, repos, workers=self._ctx.workers):
            if isinstance(outcome, Exception):
                result.repos.append(RepoResult(dir=repo.dir, ok=False, error=str(outcome)))
                self._log.error(
                    "repo failed",
                    extra={"dir": repo.dir, "op": self.name, "error": str(outcome)},
                )
            else:
                result.repos.append(outcome)

        self._log.info(
            "operation done",
            extra={
                "op": self.name,
                "ok": sum(1 for r in result.repos if r.ok),
                "failed": sum(1 for r in result.repos if not r.ok),
            },
        )
        return result

    # --- subclass hook ------------------------------------------------------

    @abstractmethod
    def _apply_to_repo(self, repo: RepoSpec) -> RepoResult:
        """Per-repo step. Return a RepoResult; raise to mark the repo failed."""

    # --- internals ----------------------------------------------------------

    def _apply_safely(self, repo: RepoSpec) -> RepoResult:
        return self._apply_to_repo(repo)


def _replace_ctx(ctx: OperationContext, **changes: Any) -> OperationContext:
    """Return a copy of `ctx` with the given fields replaced.

    Used by the chainable `.with_*` setters to keep contexts immutable.
    """
    base = {f.name: getattr(ctx, f.name) for f in fields(ctx)}
    base.update(changes)
    return OperationContext(**base)
