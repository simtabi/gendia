"""`gendia audit`: lint each repo against ecosystem hygiene rules.

Read-only. Reports findings as a list of strings per repo; `ok=False` when
any finding is found.

Checks (extend by adding methods to AuditOperation):
  - working tree clean
  - on main, in sync with origin
  - latest tag present locally and on origin
  - composer.json (if present) is valid JSON
  - LICENSE present
  - README.md present
"""

from __future__ import annotations

import json

from gendia.config.schema import RepoSpec
from gendia.git.exceptions import NotAGitRepositoryError
from gendia.git.repo import GitRepo
from gendia.operations.base import Operation, RepoResult


class AuditOperation(Operation):
    name = "audit"

    def _apply_to_repo(self, repo: RepoSpec) -> RepoResult:
        path = self._ctx.repo_path(repo)
        findings: list[str] = []

        try:
            git = GitRepo(path)
        except NotAGitRepositoryError:
            return RepoResult(dir=repo.dir, ok=False, error="not a git repo")

        if not git.is_clean():
            findings.append("uncommitted changes in working tree")

        branch = git.current_branch()
        if branch != "main":
            findings.append(f"not on main (current: {branch or 'DETACHED'})")
        else:
            state = git.remote_state(branch=branch)
            if state.ahead:
                findings.append(f"local main is {state.ahead} commit(s) ahead of origin")
            if state.behind:
                findings.append(f"local main is {state.behind} commit(s) behind origin")

        for fname in ("LICENSE", "README.md"):
            if not (path / fname).is_file():
                findings.append(f"missing {fname}")

        composer = path / "composer.json"
        if composer.is_file():
            try:
                json.loads(composer.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                findings.append(f"composer.json invalid: {exc}")

        local_tags = git.tags()
        remote_tags = git.remote_tags()
        latest_local = local_tags[-1] if local_tags else None
        if latest_local and latest_local not in remote_tags:
            findings.append(f"latest tag {latest_local} missing on origin")

        ok = not findings
        return RepoResult(
            dir=repo.dir,
            ok=ok,
            summary=("clean" if ok else f"{len(findings)} finding(s)"),
            detail={"findings": findings},
        )
