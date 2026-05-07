"""`gendia verify`: run each repo's `verify[]` commands and report.

Configured commands run as the user's shell would invoke them. Use this in CI
or pre-release to gate a `release` op behind tests + lints.
"""

from __future__ import annotations

import shlex
from typing import Any

from gendia.config.schema import RepoSpec
from gendia.git.exceptions import GitCommandError
from gendia.git.shell import run as shell_run
from gendia.operations.base import Operation, RepoResult


class VerifyOperation(Operation):
    name = "verify"

    def _apply_to_repo(self, repo: RepoSpec) -> RepoResult:
        path = self._ctx.repo_path(repo)
        if not path.is_dir():
            return RepoResult(dir=repo.dir, ok=False, error="path not found")
        if not repo.verify:
            return RepoResult(dir=repo.dir, ok=True, summary="no verify[] commands configured")

        results: list[dict[str, Any]] = []
        ok = True
        for cmd_str in repo.verify:
            argv = shlex.split(cmd_str)
            if self._ctx.dry_run:
                results.append({"cmd": cmd_str, "exit": "DRY-RUN"})
                continue
            try:
                outcome = shell_run(argv, cwd=path, timeout=600.0, check=True)
                results.append({"cmd": cmd_str, "exit": outcome.exit_code})
            except GitCommandError as exc:
                ok = False
                results.append(
                    {
                        "cmd": cmd_str,
                        "exit": exc.exit_code,
                        "stderr": exc.stderr.splitlines()[-3:],
                    }
                )
                break  # fail-fast: don't run the rest if one command fails

        return RepoResult(
            dir=repo.dir,
            ok=ok,
            summary=("all passed" if ok else f"failed at {len(results)}/{len(repo.verify)}"),
            detail={"results": results},
        )
