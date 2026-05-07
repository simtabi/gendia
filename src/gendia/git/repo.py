"""High-level git operations bound to a single working tree.

`GitRepo` is the canonical handle a gendia operation receives. It exposes
exactly the verbs we need: status, fetch, push, tags. New verbs land here,
not as ad-hoc subprocess calls scattered through operations.

Auth is injected via the `auth_env` argument to `__init__`: a callable that
returns the dict of env vars to layer on top of the base process env. This
keeps GitRepo decoupled from the Account/AuthEnvironment types and makes it
trivial to test without any auth at all (default: identity function).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from gendia.git.exceptions import NotAGitRepositoryError
from gendia.git.shell import CommandResult, run

EnvProvider = Callable[[], dict[str, str]]


@dataclass(frozen=True, slots=True)
class RemoteState:
    """Comparison of local vs remote at one point in time."""

    branch: str
    ahead: int
    behind: int
    remote_branch: str | None  # None when remote has no matching branch yet


class GitRepo:
    """Bound to a `.git` working tree; all methods operate on `path`."""

    def __init__(self, path: Path, *, env_provider: EnvProvider | None = None) -> None:
        if not (path / ".git").exists():
            raise NotAGitRepositoryError(f"{path} is not a git repository")
        self._path = path.resolve()
        # When no provider is given, use the host process env unmodified.
        # Operations that need auth pass a real provider built from
        # `gendia.git.auth.build_environment`.
        self._env_provider = env_provider or (lambda: {})

    @property
    def path(self) -> Path:
        return self._path

    # --- queries -------------------------------------------------------------

    def is_clean(self) -> bool:
        """True iff there are no uncommitted, staged, or untracked changes."""
        return not self.run("status", "--porcelain").stdout.strip()

    def current_branch(self) -> str:
        """Return current branch name; empty string when detached HEAD."""
        result = self.run("symbolic-ref", "--short", "HEAD", check=False)
        return result.stdout.strip() if result.exit_code == 0 else ""

    def head_sha(self) -> str:
        return self.run("rev-parse", "HEAD").stdout.strip()

    def commit_count(self, ref: str = "HEAD") -> int:
        result = self.run("rev-list", "--count", ref, check=False)
        return int(result.stdout.strip() or 0) if result.exit_code == 0 else 0

    def last_commit_subject(self) -> str:
        return self.run("log", "-1", "--format=%s").stdout.strip()

    def remote_url(self, name: str = "origin") -> str | None:
        result = self.run("remote", "get-url", name, check=False)
        return result.stdout.strip() if result.exit_code == 0 else None

    def tags(self, pattern: str = "v*") -> list[str]:
        return self.run("tag", "--list", pattern).stdout_lines

    def remote_tags(self, name: str = "origin") -> list[str]:
        result = self.run("ls-remote", "--tags", name, check=False)
        if result.exit_code != 0:
            return []
        out = []
        for line in result.stdout_lines:
            ref = line.split(maxsplit=1)[-1] if line else ""
            if not ref.startswith("refs/tags/"):
                continue
            tag = ref.removeprefix("refs/tags/").removesuffix("^{}")
            if tag and tag not in out:
                out.append(tag)
        return out

    def remote_state(self, *, remote: str = "origin", branch: str | None = None) -> RemoteState:
        """Compute ahead/behind vs `remote/branch` after a fetch."""
        branch = branch or self.current_branch() or "main"
        self.run("fetch", remote, "--quiet", check=False, timeout=120.0)

        ref = f"{remote}/{branch}"
        check = self.run("rev-parse", "--verify", ref, check=False)
        if check.exit_code != 0:
            return RemoteState(branch=branch, ahead=0, behind=0, remote_branch=None)

        ahead = int(self.run("rev-list", "--count", f"{ref}..HEAD").stdout.strip() or 0)
        behind = int(self.run("rev-list", "--count", f"HEAD..{ref}").stdout.strip() or 0)
        return RemoteState(branch=branch, ahead=ahead, behind=behind, remote_branch=ref)

    # --- mutations -----------------------------------------------------------

    def push(
        self, *, remote: str = "origin", ref: str | None = None, force: bool = False
    ) -> CommandResult:
        args = ["push"]
        if force:
            args.append("--force")
        args.append(remote)
        if ref is not None:
            args.append(ref)
        return self.run(*args, timeout=180.0)

    def push_tag(self, tag: str, *, remote: str = "origin", force: bool = False) -> CommandResult:
        return self.push(remote=remote, ref=(f"+refs/tags/{tag}" if force else tag))

    def tag(self, name: str, *, force: bool = False, message: str | None = None) -> CommandResult:
        args = ["tag"]
        if force:
            args.append("-f")
        if message:
            args += ["-a", name, "-m", message]
        else:
            args.append(name)
        return self.run(*args)

    def commit_all(self, message: str) -> CommandResult:
        self.run("add", "-A")
        return self.run("commit", "-m", message)

    # --- low-level escape hatch ---------------------------------------------

    def run(self, *args: str, check: bool = True, timeout: float = 60.0) -> CommandResult:
        env = self._env_provider() or None
        return run(["git", *args], cwd=self._path, check=check, timeout=timeout, env=env)
