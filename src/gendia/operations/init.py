"""`gendia init`: scaffold a `gendia.json` in the current directory.

Doesn't fan out per-repo; it's a one-shot. We slot it into the same Operation
hierarchy for CLI uniformity but override `run()` directly.
"""

from __future__ import annotations

import json
from pathlib import Path

from gendia.config.schema import RepoSpec
from gendia.operations.base import Operation, OperationContext, OperationResult, RepoResult

_TEMPLATE = {
    "name": "my-project",
    "account": "personal",
    "registry": None,
    "root": ".",
    "repos": [
        {
            "dir": "package-one",
            "package": "vendor/package-one",
            "verify": ["composer validate"],
        }
    ],
    "cleanup_globs": ["vendor", "node_modules", ".phpunit.cache", ".DS_Store"],
}


class InitOperation(Operation):
    name = "init"

    def __init__(self, ctx: OperationContext, *, target: Path, force: bool = False) -> None:
        super().__init__(ctx)
        self._target = target
        self._force = force

    def run(self) -> OperationResult:
        result = OperationResult(name=self.name, dry_run=self._ctx.dry_run)
        if self._target.exists() and not self._force:
            result.repos.append(
                RepoResult(
                    dir=str(self._target),
                    ok=False,
                    error=f"{self._target} exists; use --force to overwrite",
                )
            )
            return result

        if self._ctx.dry_run:
            result.repos.append(
                RepoResult(
                    dir=str(self._target),
                    ok=True,
                    summary=f"would write {self._target}",
                )
            )
            return result

        self._target.write_text(
            json.dumps(_TEMPLATE, indent=2) + "\n",
            encoding="utf-8",
        )
        result.repos.append(
            RepoResult(
                dir=str(self._target),
                ok=True,
                summary=f"scaffolded {self._target}",
            )
        )
        return result

    def _apply_to_repo(self, repo: RepoSpec) -> RepoResult:  # pragma: no cover
        raise NotImplementedError("init.run() handles scaffolding directly")
