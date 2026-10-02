"""`gendia convert` — flip git URLs between SSH and HTTPS.

Reads URLs from positional args or stdin (one per line) and prints the
converted form on stdout. Honours `~/.gitconfig` `insteadOf` rules and
SSH `Host` aliases when a config is provided.

Doesn't fan out per repo; runs once.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass

from gendia.config.schema import RepoSpec
from gendia.observability.logger import get_logger
from gendia.operations.base import Operation, OperationContext, OperationResult, RepoResult
from gendia.ssh.config import SSHConfig
from gendia.util.url import convert_many, load_insteadof_rules

_log = get_logger("ops.convert")


@dataclass(frozen=True, slots=True)
class ConvertRequest:
    urls: tuple[str, ...]
    target: str  # "ssh" | "https"
    use_insteadof: bool
    use_ssh_config: bool
    json_out: bool


class ConvertOperation(Operation):
    name = "convert"

    def __init__(self, ctx: OperationContext, *, request: ConvertRequest) -> None:
        super().__init__(ctx)
        self._req = request

    def run(self) -> OperationResult:
        result = OperationResult(name=self.name, dry_run=self._ctx.dry_run)

        urls = self._req.urls or _read_stdin()
        if not urls:
            print("convert: no URLs given (pass on argv or pipe via stdin)", file=sys.stderr)
            result.repos.append(
                RepoResult(
                    dir="<no-input>",
                    ok=False,
                    error="no URLs given",
                )
            )
            return result

        ssh_cfg = SSHConfig.load() if self._req.use_ssh_config else None
        rules = load_insteadof_rules() if self._req.use_insteadof else None

        pairs = convert_many(urls, target=self._req.target, ssh_config=ssh_cfg, insteadof=rules)

        if self._req.json_out:
            payload = {
                "target": self._req.target,
                "conversions": [{"input": i, "output": o} for i, o in pairs],
            }
            sys.stdout.write(json.dumps(payload, indent=2) + "\n")
        else:
            for _src, dst in pairs:
                print(dst)

        for src, dst in pairs:
            result.repos.append(
                RepoResult(
                    dir=src,
                    ok=True,
                    summary=dst,
                    detail={"input": src, "output": dst, "target": self._req.target},
                )
            )
        return result

    def _apply_to_repo(self, repo: RepoSpec) -> RepoResult:  # pragma: no cover
        raise NotImplementedError("convert.run() reads URLs from argv/stdin")


# --- subparser --------------------------------------------------------------


def add_subparser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subparsers.add_parser(
        "convert",
        help="Flip git URLs between SSH and HTTPS (alias + insteadOf aware).",
    )
    p.add_argument("urls", nargs="*", help="URLs to convert (or pipe via stdin)")
    p.add_argument(
        "--to",
        dest="target",
        choices=("ssh", "https"),
        default="ssh",
        help="Target form (default: ssh)",
    )
    p.add_argument(
        "--no-insteadof",
        dest="use_insteadof",
        action="store_false",
        help="Skip ~/.gitconfig insteadOf rules.",
    )
    p.add_argument(
        "--no-ssh-config",
        dest="use_ssh_config",
        action="store_false",
        help="Skip ~/.ssh/config alias resolution.",
    )
    p.add_argument(
        "--json", dest="json_out", action="store_true", help="Emit JSON instead of plain output."
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


def request_from_args(args: argparse.Namespace) -> ConvertRequest:
    return ConvertRequest(
        urls=tuple(args.urls),
        target=args.target,
        use_insteadof=bool(getattr(args, "use_insteadof", True)),
        use_ssh_config=bool(getattr(args, "use_ssh_config", True)),
        json_out=bool(args.json_out),
    )


def _read_stdin() -> tuple[str, ...]:
    if sys.stdin.isatty():
        return ()
    return tuple(line.strip() for line in sys.stdin if line.strip())
