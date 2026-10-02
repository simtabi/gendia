"""`gendia init`: scaffold a `gendia.json` (and optionally a full repo) in cwd.

Two modes:

  1. Default — write a minimal `gendia.json` template in the current directory.
  2. `--scaffold ID` — render a bundled scaffold template (`bare`, `python-uv`,
     ...) into `--target` (defaults to a sibling directory named after the
     scaffold's `name` variable). Optionally also writes `gendia.json`.

Doesn't fan out per-repo; it's a one-shot. We slot it into the same Operation
hierarchy for CLI uniformity but override `run()` directly.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class ScaffoldRequest:
    """Optional `--scaffold` payload for `gendia init`."""

    scaffold_id: str
    target: Path | None
    variables: dict[str, str]
    merge: bool = False
    force: bool = False


class InitOperation(Operation):
    name = "init"

    def __init__(  # noqa: PLR0913 — every flag is independent and meaningful
        self,
        ctx: OperationContext,
        *,
        target: Path,
        force: bool = False,
        scaffold: ScaffoldRequest | None = None,
    ) -> None:
        super().__init__(ctx)
        self._target = target
        self._force = force
        self._scaffold = scaffold

    def run(self) -> OperationResult:
        result = OperationResult(name=self.name, dry_run=self._ctx.dry_run)

        if self._scaffold is not None:
            return self._run_scaffold(result)

        return self._run_template(result)

    def _run_template(self, result: OperationResult) -> OperationResult:
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

    def _run_scaffold(self, result: OperationResult) -> OperationResult:
        from gendia import scaffolds  # noqa: PLC0415 — keep cold-start path small

        if self._scaffold is None:  # pragma: no cover — narrows the type
            raise RuntimeError("internal: _run_scaffold called without scaffold request")

        spec = scaffolds.load_bundled_scaffold(self._scaffold.scaffold_id)
        if spec is None:
            available = ", ".join(scaffolds.list_bundled_scaffolds()) or "(none)"
            result.repos.append(
                RepoResult(
                    dir=self._scaffold.scaffold_id,
                    ok=False,
                    error=(
                        f"unknown scaffold {self._scaffold.scaffold_id!r}; available: {available}"
                    ),
                )
            )
            return result

        target = self._scaffold.target or (
            Path.cwd() / self._scaffold.variables.get("name", spec.id)
        )

        try:
            ops = scaffolds.apply(
                spec,
                target,
                self._scaffold.variables,
                dry_run=self._ctx.dry_run,
                merge=self._scaffold.merge,
                force=self._scaffold.force,
            )
        except ValueError as exc:
            result.repos.append(
                RepoResult(
                    dir=spec.id,
                    ok=False,
                    error=str(exc),
                )
            )
            return result

        target_resolved = target.resolve()

        # Header line: tells the user where the new repo landed before
        # listing per-file operations. Crucial when --target is implicit.
        result.repos.append(
            RepoResult(
                dir=str(target_resolved),
                ok=True,
                summary=f"scaffold={spec.id} files={len(ops)}",
            )
        )

        # `merge` mode treats skip-existing as a deliberate user choice —
        # not a failure. `path-escape` is always a failure (security).
        skip_is_failure = not self._scaffold.merge
        bad_kinds = {"missing-source", "path-escape"}
        if skip_is_failure:
            bad_kinds.add("skip-existing")

        for op in ops:
            try:
                rel = str(op.dest.relative_to(target_resolved))
            except ValueError:
                rel = str(op.dest)
            summary = f"{op.kind} ({op.bytes_written}B)" if op.bytes_written else op.kind
            result.repos.append(
                RepoResult(
                    dir=rel,
                    ok=op.kind not in bad_kinds,
                    summary=summary,
                )
            )
        return result

    def _apply_to_repo(self, repo: RepoSpec) -> RepoResult:  # pragma: no cover
        raise NotImplementedError("init.run() handles scaffolding directly")
