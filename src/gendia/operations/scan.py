"""`gendia scan` — find git repos on disk and classify each.

Output groups by classification:

  * **clean**            — has remote, working tree clean
  * **dirty**            — uncommitted changes (only when --check-dirty given)
  * **https-candidate**  — origin uses HTTPS (could be flipped to SSH)
  * **orphan**           — no remotes; never pushed anywhere

Default scan path is the current directory. Pass a path arg to widen.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from gendia.config.schema import RepoSpec
from gendia.observability.logger import get_logger
from gendia.operations.base import Operation, OperationContext, OperationResult, RepoResult
from gendia.util.repo_scan import ScannedRepo, scan, to_dict

_log = get_logger("ops.scan")


@dataclass(frozen=True, slots=True)
class ScanRequest:
    root: Path
    check_dirty: bool
    json_out: bool


class ScanOperation(Operation):
    name = "scan"

    def __init__(self, ctx: OperationContext, *, request: ScanRequest) -> None:
        super().__init__(ctx)
        self._req = request

    def run(self) -> OperationResult:
        result = OperationResult(name=self.name, dry_run=self._ctx.dry_run)

        repos = scan(self._req.root, with_dirty=self._req.check_dirty)
        if self._req.json_out:
            payload = {"root": str(self._req.root), "repos": [to_dict(r) for r in repos]}
            sys.stdout.write(json.dumps(payload, indent=2))
            sys.stdout.write("\n")
        else:
            self._render_human(repos)

        for repo in repos:
            result.repos.append(
                RepoResult(
                    dir=str(repo.path),
                    ok=True,
                    summary=repo.classify(),
                    detail=to_dict(repo),
                )
            )
        return result

    def _apply_to_repo(self, repo: RepoSpec) -> RepoResult:  # pragma: no cover
        raise NotImplementedError("scan.run() handles its own filesystem walk")

    def _render_human(self, repos: list[ScannedRepo]) -> None:
        groups: dict[str, list[ScannedRepo]] = defaultdict(list)
        for repo in repos:
            groups[repo.classify()].append(repo)

        glyph = {"clean": "✓", "dirty": "⚠", "https-candidate": "→", "orphan": "◌"}
        for category in ("dirty", "orphan", "https-candidate", "clean"):
            entries = groups.get(category, [])
            if not entries:
                continue
            print(f"== {category} ({len(entries)}) ==")
            for repo in entries:
                origin = repo.origin or "(no remote)"
                print(f"  {glyph.get(category, '?')} {repo.path}")
                print(f"      origin: {origin}")
            print()

        if not repos:
            print(f"(no git repos under {self._req.root})")


# --- subparser --------------------------------------------------------------


def add_subparser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subparsers.add_parser(
        "scan",
        help="Walk a directory tree, find git repos, classify each (clean/dirty/orphan/https).",
    )
    p.add_argument(
        "root",
        nargs="?",
        type=Path,
        default=Path.cwd(),
        help="Directory to scan (default: cwd)",
    )
    p.add_argument(
        "--check-dirty",
        action="store_true",
        help="Run `git status` in each repo (slower; needed to flag dirty trees).",
    )
    p.add_argument(
        "--json",
        dest="json_out",
        action="store_true",
        help="Emit JSON instead of the human report.",
    )
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--concurrency", type=int, default=None)
    p.add_argument(
        "--only",
        type=lambda v: frozenset(s.strip() for s in v.split(",") if s.strip()),
        default=frozenset(),
    )
    p.add_argument(
        "--skip",
        type=lambda v: frozenset(s.strip() for s in v.split(",") if s.strip()),
        default=frozenset(),
    )
    p.add_argument("--no-keyring", action="store_true")


def request_from_args(args: argparse.Namespace) -> ScanRequest:
    return ScanRequest(
        root=Path(args.root).expanduser().resolve(),
        check_dirty=bool(args.check_dirty),
        json_out=bool(args.json_out),
    )
