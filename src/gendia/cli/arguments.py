"""argparse setup + verb dispatch.

We use stdlib argparse to keep the dependency footprint at zero. Each verb is
registered in `_VERBS`; adding a new operation means adding a new entry there
plus a new module under `operations/`.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from collections.abc import Callable
from pathlib import Path

from gendia import __version__
from gendia.auth import (
    CredentialResolver,
    DotEnvCredentialStore,
    KeyringCredentialStore,
)
from gendia.auth.base import CredentialStore
from gendia.cli.banner import emit as emit_banner
from gendia.cli.commands import config as config_cmd
from gendia.cli.commands import doctor as doctor_cmd
from gendia.cli.commands import identity as identity_cmd
from gendia.cli.commands import setup as setup_cmd
from gendia.config import load_config
from gendia.config.schema import (
    Account,
    ConfigError,
    Defaults,
    GendiaConfig,
    Project,
    RepoSpec,
)
from gendia.observability.logger import get_logger
from gendia.operations import (
    AuditOperation,
    CleanupOperation,
    ConvertOperation,
    GenerateOperation,
    InitOperation,
    InventoryOperation,
    MirrorOperation,
    Operation,
    OperationContext,
    OperationResult,
    ReleaseOperation,
    ScanOperation,
    StatusOperation,
    SyncOperation,
    VerifyOperation,
)
from gendia.operations import (
    conventions as conventions_op,
)
from gendia.operations import (
    convert as convert_op,
)
from gendia.operations import (
    generate as generate_op,
)
from gendia.operations import (
    scan as scan_op,
)
from gendia.providers import build_provider
from gendia.registries import build_registry
from gendia.util.paths import env_file_path

_log = get_logger("cli")


class _BannerHelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Default formatter that prints the banner above standard help text."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gendia",
        description=(
            "CI/CD-friendly Packagist (and npm + PyPI) dev-workflow handler "
            "for polyrepo ecosystems across GitHub, GitLab, and Bitbucket."
        ),
        formatter_class=_BannerHelpFormatter,
    )
    parser.add_argument("-V", "--version", action="version", version=f"gendia {__version__}")
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default=None,
    )
    parser.add_argument(
        "--log-format",
        choices=["human", "json"],
        default=None,
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to a project gendia.json (default: search cwd, then ~/.config/gendia/)",
    )
    parser.add_argument(
        "--no-banner",
        action="store_true",
        help="Suppress the banner on top-level help / bare invocation.",
    )

    # Hook the banner into the help output. The original `print_help` is kept
    # intact below; we simply emit the banner first when called.
    _wire_banner(parser)

    sub = parser.add_subparsers(dest="command", required=True)

    # status / sync / audit / cleanup / verify all share the per-op modifiers.
    for verb in ("status", "sync", "audit", "cleanup", "verify"):
        p = sub.add_parser(verb, help=_DESCRIPTIONS[verb])
        _add_common_op_args(p)

    # release <repo> <version>
    p_rel = sub.add_parser("release", help=_DESCRIPTIONS["release"])
    _add_common_op_args(p_rel)
    p_rel.add_argument("repo", help="Repo dir name as defined in gendia.json")
    p_rel.add_argument("version", help="Semver version, e.g. 1.0.1")

    # mirror --to <dir>
    p_mir = sub.add_parser("mirror", help=_DESCRIPTIONS["mirror"])
    _add_common_op_args(p_mir)
    p_mir.add_argument("--to", type=Path, required=True, help="Target directory")

    # inventory [--account=NAME]
    p_inv = sub.add_parser("inventory", help=_DESCRIPTIONS["inventory"])
    _add_common_op_args(p_inv)
    p_inv.add_argument(
        "--account",
        default=None,
        help="Limit to one account name (default: all configured accounts).",
    )

    # init [--force] [--scaffold ID --var KEY=VALUE...]
    p_init = sub.add_parser("init", help=_DESCRIPTIONS["init"])
    p_init.add_argument("--force", action="store_true", help="Overwrite existing files")
    p_init.add_argument("--out", type=Path, default=Path("gendia.json"), help="Path to write")
    p_init.add_argument(
        "--scaffold",
        default=None,
        help="Bundled scaffold id (e.g. `bare`, `python-uv`). See --list-scaffolds.",
    )
    p_init.add_argument(
        "--list-scaffolds",
        action="store_true",
        help="Print bundled scaffold ids and exit.",
    )
    p_init.add_argument(
        "--var",
        dest="scaffold_vars",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Scaffold variable (repeatable). Example: --var name=my-pkg",
    )
    p_init.add_argument(
        "--target",
        type=Path,
        default=None,
        help="Scaffold target directory (default: ./<name>).",
    )
    p_init.add_argument(
        "--merge",
        action="store_true",
        help="Scaffold mode: skip files that already exist (retrofit-friendly).",
    )
    p_init.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be written without touching disk.",
    )

    # config <verb> ...
    config_cmd.add_subparser(sub)

    # setup --shape=...
    setup_cmd.add_subparser(sub)

    # doctor (system-wide health check)
    doctor_cmd.add_subparser(sub)

    # identity {list, check, apply, setup}
    identity_cmd.add_subparser(sub)

    # conventions [PATH] [--rules FILE] [--strict] [--json]
    conventions_op.add_subparser(sub)

    # generate [--format ...] [--output FILE] [--execute] ...
    generate_op.add_subparser(sub)

    # scan [PATH] [--check-dirty] [--json]
    scan_op.add_subparser(sub)

    # convert URL... [--to ssh|https]
    convert_op.add_subparser(sub)

    # ssh {list, test, inspect, bootstrap}
    from gendia.cli.commands import ssh as ssh_cmd  # noqa: PLC0415

    ssh_cmd.add_subparser(sub)

    return parser


def _add_common_op_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--dry-run", action="store_true", help="Show what would happen, do nothing.")
    p.add_argument("--concurrency", type=int, default=None)
    p.add_argument(
        "--only",
        type=_csv,
        default=frozenset(),
        help="Comma-separated list of repo dirs to include",
    )
    p.add_argument(
        "--skip",
        type=_csv,
        default=frozenset(),
        help="Comma-separated list of repo dirs to exclude",
    )
    p.add_argument(
        "--no-keyring", action="store_true", help="Skip the OS keyring backend; use env / .env only"
    )


def _csv(value: str) -> frozenset[str]:
    return frozenset(s.strip() for s in value.split(",") if s.strip())


_DESCRIPTIONS = {
    "status": "Print branch / clean / ahead / latest-tag for every repo.",
    "sync": "Push pending commits + tags to origin and notify the registry.",
    "inventory": (
        "List + categorise every package per account "
        "(synced, pending, untracked, missing, ignored)."
    ),
    "audit": "Run hygiene checks (clean tree, latest tag pushed, composer.json valid, etc.).",
    "cleanup": "Remove ephemeral build/cache artifacts (vendor/, node_modules/, …).",
    "verify": "Run each repo's `verify[]` commands (tests, lints).",
    "release": "Tag, push, notify the registry. Requires <repo> and <version>.",
    "mirror": "Clone every repo defined in the project to a target directory.",
    "init": "Scaffold a fresh gendia.json in the current directory.",
    "conventions": (
        "Lint repo hygiene: GitHub-special files, naming, ban-list glyphs, "
        "spec filename rules, sub-folder readmes, shell-script shebangs."
    ),
    "generate": (
        "Emit copy-paste git commands per detected SSH host "
        "(text/script/json/markdown; --execute runs the bash form)."
    ),
    "scan": "Find git repos on disk and classify each (clean / dirty / orphan / https).",
    "convert": "Flip git URLs between SSH and HTTPS (alias + insteadOf aware).",
}


# --- dispatcher --------------------------------------------------------------


def _wire_banner(parser: argparse.ArgumentParser) -> None:
    """Make `parser.print_help()` emit the banner first.

    We wrap the bound method so the banner only appears on the top-level
    parser; subcommand `--help` stays narrowly scoped.
    """
    original_print_help = parser.print_help

    def print_help_with_banner(file: object | None = None) -> None:
        emit_banner()
        original_print_help(file)  # type: ignore[arg-type]

    parser.print_help = print_help_with_banner  # type: ignore[method-assign]


def dispatch(args: argparse.Namespace) -> int:  # noqa: PLR0911 — many sidecar verbs
    """Route the parsed args to the correct operation, returning an exit code."""
    if args.command == "init":
        return _run_init(args)
    if args.command == "config":
        return config_cmd.dispatch(args)
    if args.command == "setup":
        return setup_cmd.dispatch(args)
    if args.command == "doctor":
        return doctor_cmd.dispatch(args)
    if args.command == "identity":
        return identity_cmd.dispatch(args)
    if args.command == "conventions":
        return conventions_op.dispatch(args)
    if args.command == "ssh":
        from gendia.cli.commands import ssh as ssh_cmd  # noqa: PLC0415

        return ssh_cmd.dispatch(args)

    if args.command in {"generate", "scan", "convert"}:
        return _run_project_optional(args)

    try:
        config = load_config(project_path=args.config)
    except ConfigError as exc:
        _log.error("config error", extra={"error": str(exc)})
        return 2

    if config.project is None:
        _log.error("no project block found; run `gendia init` to scaffold one")
        return 2

    credentials = _build_credentials(args)
    account = config.account(config.project.account)
    provider = build_provider(account, credentials)
    registry = (
        build_registry(config.registry(config.project.registry), credentials)
        if config.project.registry
        else None
    )

    ctx = OperationContext(
        config=config,
        project=config.project,
        provider=provider,
        registry=registry,
        dry_run=getattr(args, "dry_run", False),
        workers=(args.concurrency or config.defaults.concurrency),
        only=getattr(args, "only", frozenset()) or frozenset(),
        skip=getattr(args, "skip", frozenset()) or frozenset(),
    )

    operation = _build_operation(args, ctx)
    result = operation.run()

    _print_result(result, args)
    return 0 if result.all_ok else 1


_VerbFactory = Callable[[argparse.Namespace, OperationContext], Operation]


# Verb -> factory that builds the right Operation. Adding a verb is one new
# entry here plus a new file under `operations/`.
_VERB_FACTORIES: dict[str, _VerbFactory] = {
    "status": lambda args, ctx: StatusOperation(ctx),
    "sync": lambda args, ctx: SyncOperation(ctx),
    "audit": lambda args, ctx: AuditOperation(ctx),
    "cleanup": lambda args, ctx: CleanupOperation(ctx),
    "verify": lambda args, ctx: VerifyOperation(ctx),
    "release": lambda args, ctx: ReleaseOperation(ctx, target_dir=args.repo, version=args.version),
    "mirror": lambda args, ctx: MirrorOperation(ctx, target=args.to),
    "inventory": lambda args, ctx: InventoryOperation(ctx, account_filter=args.account),
}


def _build_operation(args: argparse.Namespace, ctx: OperationContext) -> Operation:
    factory = _VERB_FACTORIES.get(args.command)
    if factory is None:
        raise ValueError(f"unknown command: {args.command}")
    return factory(args, ctx)


def _build_credentials(args: argparse.Namespace) -> CredentialResolver:
    backends: list[CredentialStore] = [DotEnvCredentialStore(env_file=env_file_path())]
    if not getattr(args, "no_keyring", False):
        with contextlib.suppress(RuntimeError):
            # Keyring import failure means the optional dep isn't installed;
            # we silently skip and rely on env / .env, which is the right
            # answer for VPS deployments where there's no keychain to talk to.
            backends.append(KeyringCredentialStore())
    return CredentialResolver(*backends)


def _run_project_optional(args: argparse.Namespace) -> int:
    """Run `generate` / `scan` / `convert`, which don't require a project block.

    These ops still want a config (for accounts + forges_file). When loading
    fails or no project exists, we fall back to a stub project so the existing
    OperationContext shape stays usable.
    """
    config: GendiaConfig | None = None
    try:
        config = load_config(project_path=args.config)
    except ConfigError as exc:
        _log.warning("config load failed; continuing without it", extra={"error": str(exc)})

    if config is None or config.project is None:
        # Build a stub config that still preserves any accounts the user
        # already configured (so generate can discover repos via providers).
        accounts = config.accounts if config else {}
        registries = config.registries if config else {}
        defaults = config.defaults if config else Defaults()
        forges_file = config.forges_file if config else None
        stub_project = Project(
            name="<no-project>",
            account=next(iter(accounts), "<none>") or "<none>",
            root=Path.cwd(),
            repos=(RepoSpec(dir="."),),
        )
        if not accounts:
            accounts = {
                "<none>": Account(
                    name="<none>",
                    platform="github",
                    credential_ref="STUB",
                    org="<none>",
                )
            }
        config = GendiaConfig(
            accounts=accounts,
            registries=registries,
            project=stub_project,
            defaults=defaults,
            forges_file=forges_file,
        )

    credentials = _build_credentials(args)
    project = config.project
    if project is None:
        # Defensive: the rebuild above guarantees a stub project, but mypy
        # can't see that across the conditional reassignment.
        raise RuntimeError("internal: project should not be None at this point")

    account_name = project.account
    if account_name not in config.accounts:
        # Pick any available account as the provider seed; ops that don't
        # need provider calls will still function.
        account_name = next(iter(config.accounts))
    account = config.account(account_name)
    provider = build_provider(account, credentials)
    registry = (
        build_registry(config.registry(project.registry), credentials) if project.registry else None
    )

    ctx = OperationContext(
        config=config,
        project=project,
        provider=provider,
        registry=registry,
        dry_run=getattr(args, "dry_run", False),
        workers=(getattr(args, "concurrency", None) or config.defaults.concurrency),
        only=getattr(args, "only", frozenset()) or frozenset(),
        skip=getattr(args, "skip", frozenset()) or frozenset(),
    )

    operation: Operation
    suppress_summary = False
    if args.command == "generate":
        forges_path = Path(config.forges_file).expanduser() if config.forges_file else None
        operation = GenerateOperation(
            ctx,
            request=generate_op.request_from_args(args, forges_file=forges_path),
        )
        # The operation writes its own structured output; the summary line
        # would only confuse json/script consumers.
        suppress_summary = getattr(args, "fmt", "text") in {"json", "script"} or bool(
            getattr(args, "output", None)
        )
    elif args.command == "scan":
        operation = ScanOperation(ctx, request=scan_op.request_from_args(args))
        suppress_summary = bool(getattr(args, "json_out", False))
    elif args.command == "convert":
        operation = ConvertOperation(ctx, request=convert_op.request_from_args(args))
        suppress_summary = bool(getattr(args, "json_out", False))
    else:
        raise ValueError(f"unexpected command: {args.command}")

    result = operation.run()
    if not suppress_summary:
        _print_result(result, args)
    return 0 if result.all_ok else 1


def _run_init(args: argparse.Namespace) -> int:
    """Init doesn't need a config; build a minimal stub context."""
    if getattr(args, "list_scaffolds", False):
        from gendia import scaffolds  # noqa: PLC0415 — keep cli/cold-start path small

        ids = scaffolds.list_bundled_scaffolds()
        if not ids:
            print("(no bundled scaffolds found)")
            return 1
        print("Bundled scaffolds (use --scaffold ID):")
        for sid in ids:
            spec = scaffolds.load_bundled_scaffold(sid)
            description = spec.description if spec else ""
            print(f"  {sid:<20} {description}")
        return 0

    stub_account = Account(name="stub", platform="github", credential_ref="STUB", org="stub")
    stub_project = Project(
        name="stub",
        account="stub",
        root=Path.cwd(),
        repos=(RepoSpec(dir="stub"),),
    )
    stub_config = GendiaConfig(
        accounts={"stub": stub_account},
        registries={},
        project=stub_project,
        defaults=Defaults(),
    )

    class _Stub:
        def web_url(self, *_a: object, **_kw: object) -> str:
            return ""

        def clone_url(self, *_a: object, **_kw: object) -> str:
            return ""

    ctx = OperationContext(
        config=stub_config,
        project=stub_project,
        provider=_Stub(),  # type: ignore[arg-type]
        registry=None,
        dry_run=getattr(args, "dry_run", False),
        workers=1,
    )

    scaffold_request = None
    if getattr(args, "scaffold", None):
        from gendia import scaffolds  # noqa: PLC0415
        from gendia.operations.init import ScaffoldRequest  # noqa: PLC0415

        try:
            user_vars = scaffolds.parse_var_assignments(args.scaffold_vars or [])
        except ValueError as exc:
            print(f"init: {exc}", file=sys.stderr)
            return 2
        scaffold_request = ScaffoldRequest(
            scaffold_id=args.scaffold,
            target=args.target,
            variables=user_vars,
            merge=bool(getattr(args, "merge", False)),
            force=bool(getattr(args, "force", False)),
        )

    op = InitOperation(ctx, target=args.out, force=args.force, scaffold=scaffold_request)
    result = op.run()
    _print_result(result, args)
    return 0 if result.all_ok else 1


def _print_result(result: OperationResult, args: argparse.Namespace) -> None:
    if getattr(args, "log_format", None) == "json":
        json.dump(result.to_dict(), sys.stdout)
        sys.stdout.write("\n")
        return

    print(
        f"{result.name} {'(dry-run) ' if result.dry_run else ''}—",
        "ok" if result.all_ok else "FAILED",
    )
    for r in result.repos:
        flag = "✓" if r.ok else "✗"
        line = f"  {flag} {r.dir:<22} {r.summary}"
        if r.error:
            line += f"  [error: {r.error}]"
        print(line)
