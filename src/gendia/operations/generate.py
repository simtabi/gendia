"""`gendia generate` — emit copy-paste-ready git commands per detected host.

Walks `~/.ssh/config` (or a path passed via `--ssh-config`), filters down to
the hosts the forge registry recognizes, and prints a structured artifact:

  * `--format text`      human-readable banner sections (default)
  * `--format script`    executable bash; `set -e` and per-host comments
  * `--format json`      machine-parseable for tooling
  * `--format markdown`  docs-friendly with fenced code blocks

For each forge host the artifact contains:

  1. Identity setup (`git config user.name/email`)
  2. Clone commands (one per repo discovered via API + manual_repos)
  3. Remote add commands (for new local repos with no origin yet)
  4. Remote set-url commands (e.g. switching HTTPS → SSH)
  5. Template commands with `<repo-name>` placeholders
  6. Common workflows (init commit, branch, stash, undo, sync fork)
  7. SSH connection tests (`ssh -T ...`)

`--execute` runs the generated bash directly via `bash -e` (skips the
metadata sections; you typically want a `--dry-run` rehearsal first).
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from gendia.config.schema import Account, RepoSpec
from gendia.observability.logger import get_logger
from gendia.operations.base import Operation, OperationContext, OperationResult, RepoResult
from gendia.providers.base import GitProvider, ProviderError, RepoInfo
from gendia.ssh.config import SSH_DEFAULT_PORT, SSHConfig, SSHHost
from gendia.ssh.forges import ForgeRegistry, load_registry

_log = get_logger("ops.generate")

_FORMATS = ("text", "script", "json", "markdown")


# --- data shapes ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HostBlock:
    """Per-host bundle of derived commands."""

    host: SSHHost
    label: str
    platform: str  # gendia provider id (or "custom" / "unknown")
    registry_id: str
    repos: tuple[RepoInfo, ...] = field(default_factory=tuple)
    identity_name: str | None = None
    identity_email: str | None = None
    api_error: str | None = None

    @property
    def alias(self) -> str:
        return self.host.alias

    @property
    def hostname(self) -> str:
        return self.host.hostname


@dataclass(frozen=True, slots=True)
class GenerateRequest:
    fmt: str  # one of _FORMATS
    output_path: Path | None
    execute: bool
    default_branch: str
    ssh_config_path: Path | None
    forges_file: Path | None
    discover: bool  # call providers' list_repos for live discovery


# --- operation --------------------------------------------------------------


class GenerateOperation(Operation):
    name = "generate"

    def __init__(self, ctx: OperationContext, *, request: GenerateRequest) -> None:
        super().__init__(ctx)
        self._req = request

    def run(self) -> OperationResult:
        result = OperationResult(name=self.name, dry_run=self._ctx.dry_run)

        ssh_cfg = SSHConfig.load(self._req.ssh_config_path)
        registry = load_registry(self._req.forges_file)

        blocks = self._collect_blocks(ssh_cfg, registry)
        if not blocks:
            self._log.warning(
                "no forge hosts detected",
                extra={"sources": list(map(str, ssh_cfg.sources()))},
            )
            return result

        rendered = _render(blocks, fmt=self._req.fmt, default_branch=self._req.default_branch)
        self._emit(rendered)

        if self._req.execute:
            return self._execute(blocks, result)

        for block in blocks:
            result.repos.append(
                RepoResult(
                    dir=block.alias,
                    ok=block.api_error is None,
                    summary=f"{block.platform} repos={len(block.repos)}",
                    detail={
                        "label": block.label,
                        "platform": block.platform,
                        "registry_id": block.registry_id,
                        "hostname": block.hostname,
                        "repo_count": len(block.repos),
                    },
                    error=block.api_error,
                )
            )
        return result

    def _apply_to_repo(self, repo: RepoSpec) -> RepoResult:  # pragma: no cover
        raise NotImplementedError("generate.run() does its own fan-out across SSH hosts")

    # --- collection ---------------------------------------------------------

    def _collect_blocks(self, ssh_cfg: SSHConfig, registry: ForgeRegistry) -> list[HostBlock]:
        accounts_by_alias = self._index_accounts_by_alias(ssh_cfg, registry)
        out: list[HostBlock] = []
        for host in ssh_cfg.hosts().exclude_wildcards():
            registry_id = registry.detect(
                hostname=host.hostname,
                alias=host.alias,
                comment=host.comment,
                user=host.user or "",
            )
            if registry_id is None:
                continue
            provider_platform = registry.provider_platform(registry_id) or "custom"
            label = registry.derive_label(
                hostname=host.hostname,
                identity_file=host.primary_identity_file,
            )

            repos: tuple[RepoInfo, ...] = ()
            api_error: str | None = None
            account = accounts_by_alias.get(host.alias)
            if self._req.discover and account is not None:
                repos, api_error = self._discover_for_account(account)
            if account is not None and account.manual_repos:
                manual = _project_manual_to_repo_info(account, host.hostname)
                # Merge, deduping by slug.
                seen = {r.slug for r in repos}
                merged = list(repos) + [m for m in manual if m.slug not in seen]
                repos = tuple(merged)

            identity_name: str | None = None
            identity_email: str | None = None
            if account is not None:
                identity_name = account.git_identity.name
                identity_email = account.git_identity.email

            out.append(
                HostBlock(
                    host=host,
                    label=label,
                    platform=provider_platform,
                    registry_id=registry_id,
                    repos=repos,
                    identity_name=identity_name,
                    identity_email=identity_email,
                    api_error=api_error,
                )
            )
        return out

    def _index_accounts_by_alias(
        self, ssh_cfg: SSHConfig, registry: ForgeRegistry
    ) -> dict[str, Account]:
        """Match each configured account back to an SSH host alias.

        Resolution order, per account:
          1. explicit `git_identity.ssh_alias` matches the host alias
          2. `account.host` matches the SSH hostname (self-hosted forges)
          3. account's platform matches the registry's detection of the host,
             AND the account doesn't override `host` — covers the common
             "public github.com → personal account" case where the user
             never set `ssh_alias`
        """
        out: dict[str, Account] = {}
        for account in self._ctx.config.accounts.values():
            ssh_alias = account.git_identity.ssh_alias
            for host in ssh_cfg.hosts():
                if ssh_alias and host.alias == ssh_alias:
                    out[host.alias] = account
                    break
                if account.host and host.hostname == account.host:
                    out[host.alias] = account
                    break
            else:
                # No explicit alias / host match. Try a platform-level fallback
                # against any unclaimed host whose registry id maps to this
                # account's provider platform.
                if account.host:
                    continue  # account targets a specific host that wasn't found
                for host in ssh_cfg.hosts():
                    if host.alias in out:
                        continue
                    registry_id = registry.detect(
                        hostname=host.hostname,
                        alias=host.alias,
                        comment=host.comment,
                        user=host.user or "",
                    )
                    if registry_id is None:
                        continue
                    if registry.provider_platform(registry_id) == account.platform:
                        out[host.alias] = account
                        break
        return out

    def _discover_for_account(self, account: Account) -> tuple[tuple[RepoInfo, ...], str | None]:
        from gendia.auth import (  # noqa: PLC0415 — keep cli/ off cold-start path
            CredentialResolver,
            DotEnvCredentialStore,
        )
        from gendia.providers import build_provider  # noqa: PLC0415
        from gendia.util.paths import env_file_path  # noqa: PLC0415

        provider: GitProvider
        if self._ctx.provider.account.name == account.name:
            provider = self._ctx.provider
        else:
            resolver = CredentialResolver(DotEnvCredentialStore(env_file=env_file_path()))
            provider = build_provider(account, resolver)

        try:
            return tuple(provider.list_repos()), None
        except (ProviderError, RuntimeError, OSError) as exc:
            return (), str(exc)

    # --- output -------------------------------------------------------------

    def _emit(self, rendered: str) -> None:
        if self._req.output_path:
            self._req.output_path.parent.mkdir(parents=True, exist_ok=True)
            self._req.output_path.write_text(rendered, encoding="utf-8")
            print(f"wrote {self._req.output_path}")
        else:
            sys.stdout.write(rendered)
            if not rendered.endswith("\n"):
                sys.stdout.write("\n")

    def _execute(self, blocks: list[HostBlock], result: OperationResult) -> OperationResult:
        if self._ctx.dry_run:
            print("(dry-run) skipping execution; rerun without --dry-run to apply.")
            return result

        script = _render_script(blocks, default_branch=self._req.default_branch)
        proc = subprocess.run(
            ["bash", "-e"],
            input=script,
            capture_output=True,
            text=True,
            check=False,
        )
        sys.stdout.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        result.repos.append(
            RepoResult(
                dir="<execute>",
                ok=proc.returncode == 0,
                summary=f"bash exit={proc.returncode}",
                detail={"stdout_bytes": len(proc.stdout), "stderr_bytes": len(proc.stderr)},
                error=None if proc.returncode == 0 else f"bash exited with {proc.returncode}",
            )
        )
        return result


# --- rendering --------------------------------------------------------------


def _render(blocks: list[HostBlock], *, fmt: str, default_branch: str) -> str:
    if fmt == "text":
        return _render_text(blocks, default_branch=default_branch)
    if fmt == "script":
        return _render_script(blocks, default_branch=default_branch)
    if fmt == "json":
        return _render_json(blocks)
    if fmt == "markdown":
        return _render_markdown(blocks, default_branch=default_branch)
    raise ValueError(f"unknown format: {fmt!r}")


def _render_text(  # noqa: PLR0912, PLR0915 — banner sections emit many discrete branches
    blocks: list[HostBlock], *, default_branch: str
) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines: list[str] = []

    def hr(char: str = "─", width: int = 72) -> None:
        lines.append(char * width)

    def section(title: str) -> None:
        lines.append("")
        hr("═")
        lines.append(f"  {title}")
        hr("═")
        lines.append("")

    def subsection(title: str) -> None:
        lines.append("")
        hr("─")
        lines.append(f"  {title}")
        hr("─")
        lines.append("")

    hr("═")
    lines.append(f"  gendia generate — {now}")
    hr("═")

    section("SSH CONFIG HOSTS")
    for block in blocks:
        lines.append(f"  Host {block.alias}  [{block.platform}]  label={block.label}")
        lines.append(f"    HostName      {block.hostname}")
        lines.append(f"    User          {block.host.effective_user}")
        lines.append(f"    Port          {block.host.effective_port}")
        if block.host.primary_identity_file:
            lines.append(f"    IdentityFile  {block.host.primary_identity_file}")
        if block.identity_name or block.identity_email:
            lines.append(
                f"    Identity      {block.identity_name or '?'} <{block.identity_email or '?'}>"
            )
        lines.append("")

    section("GLOBAL GIT CONFIG (fallback)")
    lines.append(f"git config --global init.defaultBranch {default_branch}")
    lines.append("# Per-host identity is applied below to prevent accidental info leakage.")

    for block in blocks:
        section(
            f"{block.platform.upper()}: {block.alias} ({block.hostname}) — "
            f"{len(block.repos)} repos  [{block.label}]"
        )
        if block.api_error:
            lines.append(f"# API error: {block.api_error}")

        if block.identity_name and block.identity_email:
            subsection(f"Set identity — {block.alias}")
            lines.append("# Run inside any repo cloned from this host:")
            lines.append(f'git config user.name "{block.identity_name}"')
            lines.append(f"git config user.email {block.identity_email}")

        if block.repos:
            subsection(f"Clone — {block.alias}")
            for repo in block.repos:
                lines.append(f"git clone {_ssh_url(block, repo.slug)}")

            subsection(f"Remote add (new repos) — {block.alias}")
            lines.append("# git init first, then:")
            for repo in block.repos:
                lines.append(f"git remote add origin {_ssh_url(block, repo.slug)}")

            subsection(f"Remote set-url (existing repos) — {block.alias}")
            lines.append("# Use to switch HTTPS → SSH or correct an origin URL.")
            for repo in block.repos:
                lines.append(f"git remote set-url origin {_ssh_url(block, repo.slug)}")

        subsection(f"Template — {block.alias}")
        template = _template_url(block)
        lines.append(f"git clone {template}")
        lines.append(f"git remote add origin {template}")
        lines.append(f"git remote set-url origin {template}")

    section("COMMON WORKFLOWS")
    for branch in sorted({"main", "master", default_branch}):
        subsection(f"Initial commit & push to {branch}")
        lines.append("git add .")
        lines.append("git commit -m 'Initial release'")
        lines.append(f"git branch -M {branch}")
        lines.append(f"git push -u origin {branch}")

    subsection("Quick commit & push")
    lines.append("git add .")
    lines.append("git commit -m '<message>'")
    lines.append("git push")

    subsection("Create & push a new branch")
    lines.append("git checkout -b <branch-name>")
    lines.append("git push -u origin <branch-name>")

    subsection("Stash & restore")
    lines.append("git stash")
    lines.append("git stash pop")

    subsection("Undo last commit (keep changes)")
    lines.append("git reset --soft HEAD~1")

    subsection("Undo last commit (discard changes)")
    lines.append("git reset --hard HEAD~1")

    subsection("Sync fork with upstream")
    lines.append("git remote add upstream <upstream-url>")
    lines.append("git fetch upstream")
    lines.append(f"git merge upstream/{default_branch}")
    lines.append("git push")

    section("SSH CONNECTION TESTS")
    for block in blocks:
        port = block.host.effective_port
        user = block.host.effective_user
        if port != SSH_DEFAULT_PORT:
            lines.append(f"ssh -T -p {port} {user}@{block.alias}")
        else:
            lines.append(f"ssh -T {user}@{block.alias}")

    lines.append("")
    return "\n".join(lines) + "\n"


def _render_script(blocks: list[HostBlock], *, default_branch: str) -> str:
    """Bash output suitable for `bash -e ./script.sh`."""
    lines = [
        "#!/usr/bin/env bash",
        "# gendia generate — bash output. Inspect before running.",
        "set -e",
        f"git config --global init.defaultBranch {shlex.quote(default_branch)}",
    ]
    for block in blocks:
        lines.append(f"\n# ── {block.platform}: {block.alias} ({block.hostname}) ──")
        if block.identity_name:
            lines.append(f"# identity: {block.identity_name} <{block.identity_email or '?'}>")
        for repo in block.repos:
            lines.append(f"git clone {shlex.quote(_ssh_url(block, repo.slug))}")
    lines.append("")
    return "\n".join(lines)


def _render_json(blocks: list[HostBlock]) -> str:
    payload: dict[str, Any] = {
        "hosts": [
            {
                "alias": b.alias,
                "hostname": b.hostname,
                "platform": b.platform,
                "registry_id": b.registry_id,
                "label": b.label,
                "user": b.host.effective_user,
                "port": b.host.effective_port,
                "identity_files": list(b.host.identity_files),
                "identity": {"name": b.identity_name, "email": b.identity_email},
                "repos": [
                    {
                        "slug": r.slug,
                        "clone_url": r.clone_url,
                        "ssh_url": _ssh_url(b, r.slug),
                        "default_branch": r.default_branch,
                        "private": r.private,
                    }
                    for r in b.repos
                ],
                "api_error": b.api_error,
            }
            for b in blocks
        ]
    }
    return json.dumps(payload, indent=2) + "\n"


def _render_markdown(blocks: list[HostBlock], *, default_branch: str) -> str:
    lines = [f"# gendia generate ({datetime.now():%Y-%m-%d})", ""]
    for block in blocks:
        lines.append(
            f"## {block.platform}: `{block.alias}` ({block.hostname}) — {len(block.repos)} repos"
        )
        lines.append("")
        if block.identity_name:
            lines.append(f"Identity: {block.identity_name} &lt;{block.identity_email or '?'}&gt;")
            lines.append("")
        if block.repos:
            lines.append("```bash")
            for repo in block.repos:
                lines.append(f"git clone {_ssh_url(block, repo.slug)}")
            lines.append("```")
            lines.append("")
        lines.append("**Template (replace `<repo-name>`):**")
        lines.append("```bash")
        lines.append(f"git clone {_template_url(block)}")
        lines.append("```")
        lines.append("")

    lines.append("## Workflows")
    lines.append("")
    lines.append("```bash")
    lines.append(f"git push -u origin {default_branch}    # initial push")
    lines.append("git stash; git stash pop                # save / restore work")
    lines.append("git reset --soft HEAD~1                 # undo last commit")
    lines.append("```")
    lines.append("")
    return "\n".join(lines) + "\n"


# --- helpers ----------------------------------------------------------------


def _ssh_url(block: HostBlock, slug: str) -> str:
    user = block.host.effective_user
    port = block.host.effective_port
    if port != SSH_DEFAULT_PORT:
        return f"ssh://{user}@{block.alias}:{port}/{slug}.git"
    return f"{user}@{block.alias}:{slug}.git"


def _template_url(block: HostBlock) -> str:
    user = block.host.effective_user
    port = block.host.effective_port
    if port != SSH_DEFAULT_PORT:
        return f"ssh://{user}@{block.alias}:{port}/<org>/<repo-name>.git"
    return f"{user}@{block.alias}:<org>/<repo-name>.git"


def _project_manual_to_repo_info(account: Account, hostname: str) -> Iterable[RepoInfo]:
    for manual in account.manual_repos:
        for repo in manual.repos:
            slug = f"{manual.org}/{repo}"
            yield RepoInfo(
                slug=slug,
                clone_url=f"git@{hostname}:{slug}.git",
                web_url=f"https://{hostname}/{slug}",
                default_branch="main",
                private=True,
            )


# --- subparser --------------------------------------------------------------


def add_subparser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subparsers.add_parser(
        "generate",
        help=(
            "Emit copy-paste git commands per detected SSH host (text/script/json/markdown). "
            "Use --execute to run the bash form directly."
        ),
    )
    p.add_argument("--format", choices=_FORMATS, default="text", dest="fmt")
    p.add_argument("--output", type=Path, default=None, help="Write to FILE instead of stdout")
    p.add_argument("--execute", action="store_true", help="Run the generated bash directly")
    p.add_argument(
        "--default-branch",
        default="main",
        help="Branch name used in workflow snippets (default: main)",
    )
    p.add_argument("--ssh-config", type=Path, default=None, help="Path to SSH config")
    p.add_argument(
        "--no-discover",
        dest="discover",
        action="store_false",
        help="Skip the provider list_repos calls (template-only output)",
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


def request_from_args(args: argparse.Namespace, *, forges_file: Path | None) -> GenerateRequest:
    return GenerateRequest(
        fmt=args.fmt,
        output_path=args.output,
        execute=bool(args.execute),
        default_branch=args.default_branch,
        ssh_config_path=args.ssh_config,
        forges_file=forges_file,
        discover=bool(getattr(args, "discover", True)),
    )
