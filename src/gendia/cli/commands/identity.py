"""`gendia identity` — per-account git identity isolation.

Solves the multi-account leak problem: when you have personal + work +
client-X GitHub accounts, a flat `~/.gitconfig` can only spell one
identity. This command takes each repo in `gendia.json`, looks up its
account's `git_identity` block, and writes the right `.git/config` so
committing under the wrong identity is structurally impossible.

Verbs:

    list        Print each account's declared identity.
    check       Compare each tracked repo's current `.git/config` against
                its account's identity; flag mismatches.
    apply       Write the right `user.name` / `user.email` (and optional
                signing key + SSH URL rewrite) into every tracked repo.
    setup       Print a global `~/.gitconfig` `includeIf` snippet derived
                from your project roots. Paste it once and per-path
                identity selection becomes automatic for new clones.

`apply --dry-run` shows the plan without touching anything.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from gendia.config import GendiaConfig, GitIdentity, load_config
from gendia.config.schema import Account, ConfigError, RepoSpec
from gendia.git.exceptions import GitCommandError
from gendia.git.shell import run as shell_run
from gendia.observability.logger import get_logger

_log = get_logger("cli.identity")


# --- subparser --------------------------------------------------------------


def add_subparser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subparsers.add_parser(
        "identity",
        help="Per-account git identity (list / check / apply / setup).",
    )
    p.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to gendia.json (default: search cwd then ~/.config/gendia/)",
    )

    sub = p.add_subparsers(dest="identity_verb", required=True)

    sub.add_parser("list", help="Print each account's declared identity.")
    sub.add_parser("setup", help="Print a `~/.gitconfig` includeIf snippet.")

    p_check = sub.add_parser(
        "check",
        help="Verify each tracked repo's git config matches its account's identity.",
    )
    p_check.add_argument(
        "--strict", action="store_true", help="Exit non-zero on warnings, not just errors."
    )

    p_apply = sub.add_parser(
        "apply",
        help="Write the right git config into every tracked repo.",
    )
    p_apply.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be written without touching anything.",
    )
    p_apply.add_argument("--only", default=None, help="Comma-separated repo dirs to limit to.")

    p_init = sub.add_parser(
        "init",
        help=(
            "Initialize an account's identity at a chosen scope: globally, "
            "for one or more directory trees (via includeIf), or for the "
            "current project's repos (per-repo .git/config)."
        ),
    )
    p_init.add_argument("account", help="Account name from gendia.json")
    grp = p_init.add_mutually_exclusive_group()
    grp.add_argument(
        "--global",
        dest="global_",
        action="store_true",
        help="Write user.name/email/signingkey directly to ~/.gitconfig (catchall identity).",
    )
    grp.add_argument(
        "--project",
        action="store_true",
        help="Apply per-repo .git/config to every repo in the current project (default).",
    )
    p_init.add_argument(
        "--path",
        action="append",
        default=[],
        help="Add an includeIf entry to ~/.gitconfig for this directory tree. "
        "Repeatable. Combine with --global for both effects.",
    )
    p_init.add_argument("--dry-run", action="store_true", help="Show the plan, change nothing.")


def dispatch(args: argparse.Namespace) -> int:
    try:
        config = load_config(project_path=args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    handlers: dict[str, Callable[[], int]] = {
        "list": lambda: _cmd_list(config),
        "check": lambda: _cmd_check(config, strict=args.strict),
        "apply": lambda: _cmd_apply(config, dry_run=args.dry_run, only=args.only),
        "setup": lambda: _cmd_setup(config),
        "init": lambda: _cmd_init(
            config,
            account_name=args.account,
            global_=args.global_,
            project=args.project,
            paths=tuple(args.path),
            dry_run=args.dry_run,
        ),
    }
    handler = handlers.get(args.identity_verb)
    if handler is None:
        raise ValueError(f"unknown identity verb: {args.identity_verb}")
    return handler()


# --- per-verb implementations -----------------------------------------------


def _cmd_list(config: GendiaConfig) -> int:
    if not config.accounts:
        print("(no accounts configured)")
        return 0

    for name, account in config.accounts.items():
        ident = account.git_identity
        print(f"== {name} ({account.platform}: {account.scope}) ==")
        if ident.is_empty():
            print("  (no git_identity block — global `~/.gitconfig` will apply)")
        else:
            for label, value in (
                ("name", ident.name),
                ("email", ident.email),
                ("ssh_alias", ident.ssh_alias),
                ("signing_key", ident.signing_key),
            ):
                if value:
                    print(f"  {label:<12} {value}")
            if ident.sign_commits:
                print("  sign_commits true")
            if ident.sign_tags:
                print("  sign_tags    true")
        print()
    return 0


def _cmd_check(config: GendiaConfig, *, strict: bool) -> int:
    findings = list(_iter_findings(config))

    for finding in findings:
        glyph = {"ok": "✓", "warn": "⚠", "error": "✗"}[finding.severity]
        line = f"{glyph} {finding.repo:<25} {finding.message}"
        print(line)
        if finding.fix:
            print(f"      fix: {finding.fix}")

    has_error = any(f.severity == "error" for f in findings)
    has_warn = any(f.severity == "warn" for f in findings)
    if has_error:
        return 2
    return 1 if (has_warn and strict) else 0


def _cmd_apply(config: GendiaConfig, *, dry_run: bool, only: str | None) -> int:
    if config.project is None:
        print("error: no project block in config; cannot apply per-repo", file=sys.stderr)
        return 2

    only_set = {s.strip() for s in (only or "").split(",") if s.strip()}
    rc = 0
    for repo in config.project.repos:
        if only_set and repo.dir not in only_set:
            continue
        account = _account_for(config, repo)
        if account is None or account.git_identity.is_empty():
            continue

        path = config.project.root / repo.dir
        if not (path / ".git").exists():
            print(f"  ⚠ {repo.dir:<25} no .git directory; skipping")
            continue

        applied = _apply_to_repo(path, account.git_identity, dry_run=dry_run)
        prefix = "DRY-RUN" if dry_run else "set"
        for key, value in applied.items():
            print(f"  ✓ {repo.dir:<25} {prefix} {key} = {value}")
    return rc


def _cmd_init(  # noqa: PLR0913 — wizard surface; each scope is meaningful
    config: GendiaConfig,
    *,
    account_name: str,
    global_: bool,
    project: bool,
    paths: tuple[str, ...],
    dry_run: bool,
) -> int:
    """Initialize an account's git identity at the chosen scope(s)."""
    if account_name not in config.accounts:
        print(f"error: unknown account {account_name!r}", file=sys.stderr)
        print(f"  known: {', '.join(sorted(config.accounts))}", file=sys.stderr)
        return 2
    account = config.accounts[account_name]
    ident = account.git_identity
    if ident.is_empty():
        print(
            f"error: account {account_name!r} has no git_identity block in gendia.json",
            file=sys.stderr,
        )
        print(
            "  add at least name + email; see `gendia identity list` for current state.",
            file=sys.stderr,
        )
        return 2

    # Default scope: --project when nothing else is specified.
    if not global_ and not paths and not project:
        project = True

    print(f"initializing identity for account {account_name!r}")
    print(f"  identity: {ident.name or '?'} <{ident.email or '?'}>")
    print()

    snippet = _write_account_snippet(account, dry_run=dry_run)
    print(f"  {'would write' if dry_run else 'wrote'} per-account snippet: {snippet}")

    if global_:
        _apply_globally(account, dry_run=dry_run)
        print(f"  {'would set' if dry_run else 'set'} ~/.gitconfig user.name/email/signingkey")

    for path in paths:
        resolved = Path(path).expanduser().resolve()
        _apply_includeif(snippet, resolved, dry_run=dry_run)
        print(f"  {'would add' if dry_run else 'added'} includeIf gitdir:{resolved}/")

    if project:
        if config.project is None:
            print("  ⚠ no project block; skipping per-repo .git/config", file=sys.stderr)
        else:
            count = 0
            for repo in config.project.repos:
                repo_path = config.project.root / repo.dir
                if not (repo_path / ".git").exists():
                    continue
                applied = _apply_to_repo(repo_path, ident, dry_run=dry_run)
                if applied:
                    count += 1
            print(
                f"  {'would apply' if dry_run else 'applied'} per-repo "
                f".git/config to {count} repo(s)"
            )

    if dry_run:
        print()
        print("DRY-RUN: no files modified. Re-run without --dry-run to commit.")
    return 0


def _write_account_snippet(account: Account, *, dry_run: bool) -> Path:
    """Write `~/.config/gendia/identity-<account>.gitconfig` with the account's identity."""
    target_dir = Path.home() / ".config" / "gendia"
    target = target_dir / f"identity-{account.name}.gitconfig"
    ident = account.git_identity

    lines = ["[user]"]
    if ident.name:
        lines.append(f"    name = {ident.name}")
    if ident.email:
        lines.append(f"    email = {ident.email}")
    if ident.signing_key:
        lines.append(f"    signingkey = {ident.signing_key}")
    if ident.sign_commits:
        lines += ["[commit]", "    gpgsign = true"]
    if ident.sign_tags:
        lines += ["[tag]", "    gpgsign = true"]
    if ident.signing_key and ident.signing_key.endswith(".pub"):
        lines += ["[gpg]", "    format = ssh"]
    content = "\n".join(lines) + "\n"

    if not dry_run:
        target_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        target.chmod(0o600)
    return target


def _apply_globally(account: Account, *, dry_run: bool) -> None:
    """Set the account's identity as the catchall global identity in ~/.gitconfig."""
    ident = account.git_identity
    pairs = []
    if ident.name:
        pairs.append(("user.name", ident.name))
    if ident.email:
        pairs.append(("user.email", ident.email))
    if ident.signing_key:
        pairs.append(("user.signingkey", ident.signing_key))
    if ident.sign_commits:
        pairs.append(("commit.gpgsign", "true"))
    if ident.sign_tags:
        pairs.append(("tag.gpgsign", "true"))
    if ident.signing_key and ident.signing_key.endswith(".pub"):
        pairs.append(("gpg.format", "ssh"))

    if dry_run:
        return
    for key, value in pairs:
        try:
            shell_run(["git", "config", "--global", key, value], check=True, timeout=10.0)
        except GitCommandError as exc:
            _log.warning("git config --global failed", extra={"key": key, "error": str(exc)})


def _apply_includeif(snippet: Path, dir_path: Path, *, dry_run: bool) -> None:
    """Add `[includeIf "gitdir:<dir_path>/"] path = <snippet>` to ~/.gitconfig."""
    if dry_run:
        return
    # The trailing slash on the gitdir is critical: without it, includeIf
    # won't match subdirectories. git wants `gitdir:<dir>/` not `gitdir:<dir>`.
    key = f"includeIf.gitdir:{dir_path}/.path"
    try:
        shell_run(
            ["git", "config", "--global", "--add", key, str(snippet)],
            check=True,
            timeout=10.0,
        )
    except GitCommandError as exc:
        _log.warning("git config --global --add includeIf failed", extra={"error": str(exc)})


def _cmd_setup(config: GendiaConfig) -> int:
    """Print a `~/.gitconfig` includeIf snippet derived from the project roots."""
    if config.project is None:
        print("(no project block; nothing to suggest)", file=sys.stderr)
        return 1

    print("# --- paste into ~/.gitconfig ----------------------------------------")
    print("# Auto-selects identity per directory tree. Each project's account's")
    print("# git_identity goes into a sibling file under ~/.config/gendia/")
    print()

    seen: set[str] = set()
    for repo in config.project.repos:
        account = _account_for(config, repo)
        if account is None or account.git_identity.is_empty():
            continue
        # Use the project root as the includeIf scope. Per-account `.git/config`
        # written by `gendia identity apply` takes precedence over this anyway.
        if account.name in seen:
            continue
        seen.add(account.name)
        snippet_path = f"~/.config/gendia/identity-{account.name}.gitconfig"
        print(f'[includeIf "gitdir:{config.project.root.resolve()}/"]')
        print(f"    path = {snippet_path}")
        print()

    print("# Then write the per-account snippets:")
    for account in config.accounts.values():
        if account.git_identity.is_empty():
            continue
        snippet_path = f"~/.config/gendia/identity-{account.name}.gitconfig"
        print(f"# {snippet_path}")
        print("[user]")
        if account.git_identity.name:
            print(f"    name = {account.git_identity.name}")
        if account.git_identity.email:
            print(f"    email = {account.git_identity.email}")
        if account.git_identity.signing_key:
            print(f"    signingkey = {account.git_identity.signing_key}")
        if account.git_identity.sign_commits:
            print("[commit]")
            print("    gpgsign = true")
        if account.git_identity.sign_tags:
            print("[tag]")
            print("    gpgsign = true")
        if account.git_identity.signing_key and account.git_identity.signing_key.endswith(".pub"):
            print("[gpg]")
            print("    format = ssh")
        print()

    print("# Use `gendia identity apply` to also write per-repo `.git/config`")
    print("# overrides, which take precedence over the includeIf chain above.")
    return 0


# --- helpers ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Finding:
    severity: str
    repo: str
    message: str
    fix: str = ""


def _iter_findings(config: GendiaConfig):  # type: ignore[no-untyped-def]
    if config.project is None:
        yield _Finding("warn", "(no project)", "no project block to check")
        return

    for repo in config.project.repos:
        account = _account_for(config, repo)
        if account is None:
            yield _Finding("warn", repo.dir, "no account resolved for this repo")
            continue
        ident = account.git_identity
        path = config.project.root / repo.dir

        if not (path / ".git").exists():
            yield _Finding("warn", repo.dir, f"no .git directory at {path}")
            continue

        if ident.is_empty():
            yield _Finding(
                "ok",
                repo.dir,
                f"account {account.name!r} has no git_identity (using globals)",
            )
            continue

        actual = _read_git_config(path, ("user.name", "user.email"))
        for key, expected in (("user.name", ident.name), ("user.email", ident.email)):
            if expected is None:
                continue
            current = actual.get(key)
            if current is None:
                yield _Finding(
                    "error",
                    repo.dir,
                    f"{key} unset; account expects {expected!r}",
                    fix=f"gendia identity apply --only={repo.dir}",
                )
            elif current != expected:
                yield _Finding(
                    "error",
                    repo.dir,
                    f"{key} = {current!r} but account expects {expected!r}",
                    fix=f"gendia identity apply --only={repo.dir}",
                )
            else:
                yield _Finding("ok", repo.dir, f"{key} matches account identity")


def _account_for(config: GendiaConfig, repo: RepoSpec) -> Account | None:
    """The repo's owning account, derived from its package slug or the project default."""
    if repo.package and "/" in repo.package:
        scope = repo.package.split("/", 1)[0]
        for account in config.accounts.values():
            if account.scope == scope:
                return account
    if config.project and config.project.account in config.accounts:
        return config.accounts[config.project.account]
    return None


def _apply_to_repo(path: Path, ident: GitIdentity, *, dry_run: bool) -> dict[str, str]:
    """Write the per-repo overrides; return the (key -> value) map applied."""
    plan: dict[str, str] = {}
    if ident.name:
        plan["user.name"] = ident.name
    if ident.email:
        plan["user.email"] = ident.email
    if ident.signing_key:
        plan["user.signingkey"] = ident.signing_key
    if ident.sign_commits:
        plan["commit.gpgsign"] = "true"
    if ident.sign_tags:
        plan["tag.gpgsign"] = "true"
    if ident.signing_key and ident.signing_key.endswith(".pub"):
        plan["gpg.format"] = "ssh"

    if dry_run:
        return plan

    for key, value in plan.items():
        try:
            shell_run(["git", "config", key, value], cwd=path, check=True, timeout=10.0)
        except GitCommandError as exc:
            _log.warning("git config failed", extra={"key": key, "error": str(exc)})
    return plan


def _read_git_config(path: Path, keys: tuple[str, ...]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key in keys:
        try:
            result = shell_run(["git", "config", "--get", key], cwd=path, check=False, timeout=5.0)
        except GitCommandError:
            continue
        if result.exit_code == 0:
            out[key] = result.stdout.strip()
    return out
