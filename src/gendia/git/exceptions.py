"""Typed exceptions raised by the git wrapper."""

from __future__ import annotations


class GitError(Exception):
    """Base class for everything raised by `gendia.git`."""


class NotAGitRepositoryError(GitError):
    """Raised when an operation targets a path that isn't a git repo."""


class GitCommandError(GitError):
    """Raised when a `git` subprocess exits non-zero."""

    def __init__(self, command: list[str], exit_code: int, stdout: str, stderr: str) -> None:
        self.command = command
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        snippet = (stderr or stdout).strip().splitlines()
        first = snippet[0] if snippet else ""
        super().__init__(f"git {' '.join(command[1:])!s} exited {exit_code}: {first}")
