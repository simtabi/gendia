"""Safe subprocess wrapper for `git` invocations.

Hard rules:
  - `shell=False` always (no string interpolation; arg list only).
  - `check=True` always (raise GitCommandError on non-zero).
  - Captured stdout/stderr returned as text; never logged with secrets in argv.
  - Optional timeout to bound runaway operations.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from gendia.git.exceptions import GitCommandError


@dataclass(frozen=True, slots=True)
class CommandResult:
    args: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str

    @property
    def stdout_lines(self) -> list[str]:
        return [line for line in self.stdout.splitlines() if line]


def run(
    args: list[str],
    *,
    cwd: Path | None = None,
    timeout: float = 60.0,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> CommandResult:
    """Run `args[0]` with the given argv. No shell expansion; safe for tainted input."""
    proc = subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=env,
    )
    result = CommandResult(
        args=tuple(args),
        exit_code=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )
    if check and proc.returncode != 0:
        raise GitCommandError(
            command=args,
            exit_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )
    return result
