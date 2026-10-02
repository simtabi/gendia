"""`gendia ssh` — inspect and bootstrap from your SSH config.

Verbs:

    list       Show every Host block, with detected forge label / platform.
    test       Run `ssh -T` against each git-forge host and report success.
    inspect    Audit IdentityFile permissions + key strength + shared use.
    bootstrap  Print proposed gendia accounts synthesized from SSH hosts.

`bootstrap --write` merges the proposal into `~/.config/gendia/gendia.json`
(or the path passed via --out); without `--write` it only prints the JSON
so you can pipe / review.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from gendia.config import load_config
from gendia.config.schema import ConfigError
from gendia.observability.logger import get_logger
from gendia.ssh.bootstrap import propose_accounts, render_proposed_config
from gendia.ssh.config import SSH_DEFAULT_PORT, SSHConfig
from gendia.ssh.forges import ForgeRegistry, load_registry
from gendia.ssh.inspector import KeyAudit, audit_keys
from gendia.util.paths import config_home

_log = get_logger("cli.ssh")


def add_subparser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subparsers.add_parser(
        "ssh",
        help="Inspect / test / bootstrap from your SSH config (list, test, inspect, bootstrap).",
    )
    p.add_argument(
        "--ssh-config",
        type=Path,
        default=None,
        help="Path to an alternate SSH config (default: ~/.ssh/config)",
    )
    p.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to gendia.json (only used to honour `forges_file`)",
    )

    sub = p.add_subparsers(dest="ssh_verb", required=True)

    sub.add_parser("list", help="List Host entries with detected forge labels.")
    p_test = sub.add_parser("test", help="Run `ssh -T` against detected forge hosts.")
    p_test.add_argument(
        "--timeout",
        type=int,
        default=10,
        help="Per-host timeout in seconds (default: 10)",
    )
    sub.add_parser("inspect", help="Audit identity-file permissions + key strength + reuse.")

    p_boot = sub.add_parser(
        "bootstrap",
        help="Propose gendia accounts synthesized from SSH config.",
    )
    p_boot.add_argument(
        "--write",
        action="store_true",
        help="Merge the proposal into ~/.config/gendia/gendia.json (or --out).",
    )
    p_boot.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Target gendia.json (default: ~/.config/gendia/gendia.json when --write).",
    )
    p_boot.add_argument("--dry-run", action="store_true")


# --- dispatch ---------------------------------------------------------------


def dispatch(args: argparse.Namespace) -> int:
    cfg = SSHConfig.load(args.ssh_config)

    forges_file = _resolve_forges_file(args)
    registry = load_registry(forges_file)

    verb = args.ssh_verb
    if verb == "list":
        return _cmd_list(cfg, registry)
    if verb == "test":
        return _cmd_test(cfg, registry, timeout=args.timeout)
    if verb == "inspect":
        return _cmd_inspect(cfg)
    if verb == "bootstrap":
        return _cmd_bootstrap(
            cfg=cfg,
            registry=registry,
            forges_file=forges_file,
            write=args.write,
            out=args.out,
            dry_run=args.dry_run,
        )
    raise ValueError(f"unknown ssh verb: {verb!r}")


# --- list ------------------------------------------------------------------


def _cmd_list(cfg: SSHConfig, registry: ForgeRegistry) -> int:
    hosts = cfg.hosts().exclude_wildcards()
    if not hosts:
        print("(no Host entries; is ~/.ssh/config present?)")
        return 1
    print(f"{'ALIAS':<25} {'HOSTNAME':<35} {'PLATFORM':<22} LABEL")
    for host in hosts:
        registry_id = registry.detect(
            hostname=host.hostname,
            alias=host.alias,
            comment=host.comment,
            user=host.user or "",
        )
        platform = registry.provider_platform(registry_id) or "—"
        label = registry.derive_label(
            hostname=host.hostname,
            identity_file=host.primary_identity_file,
        )
        print(
            f"{host.alias:<25} {host.hostname:<35} "
            f"{(registry_id or '—'):<10} → {platform:<10} {label}"
        )
    return 0


# --- test ------------------------------------------------------------------


def _cmd_test(cfg: SSHConfig, registry: ForgeRegistry, *, timeout: int) -> int:
    if shutil.which("ssh") is None:
        print("ssh: not found on PATH", file=sys.stderr)
        return 2

    failures = 0
    for host in cfg.hosts().exclude_wildcards():
        registry_id = registry.detect(
            hostname=host.hostname,
            alias=host.alias,
            comment=host.comment,
            user=host.user or "",
        )
        if registry_id is None:
            continue
        cmd = ["ssh", "-T", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={timeout}"]
        if host.port and host.port != SSH_DEFAULT_PORT:
            cmd += ["-p", str(host.port)]
        cmd.append(f"{host.effective_user}@{host.alias}")
        try:
            proc = subprocess.run(  # noqa: S603 — argv is built from typed input only
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout + 5,
                check=False,
            )
        except subprocess.TimeoutExpired:
            print(f"  ✗ {host.alias:<25} timeout after {timeout}s")
            failures += 1
            continue
        # Many forges return non-zero on success (no shell granted). We use
        # stderr text containing "successfully authenticated" as the signal.
        out = (proc.stdout + proc.stderr).strip().lower()
        ok = (
            "successfully authenticated" in out
            or "you've successfully authenticated" in out
            or "logged in as" in out
            or "welcome" in out
        )
        glyph = "✓" if ok else "✗"
        if not ok:
            failures += 1
        first = (proc.stderr or proc.stdout).strip().splitlines()[:1]
        msg = first[0] if first else ""
        print(f"  {glyph} {host.alias:<25} {msg}")
    return 0 if failures == 0 else 1


# --- inspect ----------------------------------------------------------------


def _cmd_inspect(cfg: SSHConfig) -> int:
    findings = audit_keys(cfg.hosts().exclude_wildcards())
    if not findings:
        print("(no IdentityFile entries to audit)")
        return 0
    by_section: dict[str, list[KeyAudit]] = defaultdict(list)
    for finding in findings:
        by_section[finding.section].append(finding)

    glyph = {"ok": "✓", "warn": "⚠", "error": "✗"}
    rc = 0
    for section, items in by_section.items():
        print(f"== {section} ==")
        for finding in items:
            print(f"  {glyph[finding.severity]} {finding.path}: {finding.message}")
            if finding.fix:
                print(f"      fix: {finding.fix}")
            if finding.severity == "error":
                rc = max(rc, 2)
            elif finding.severity == "warn":
                rc = max(rc, 1)
        print()
    return rc


# --- bootstrap --------------------------------------------------------------


def _cmd_bootstrap(
    *,
    cfg: SSHConfig,
    registry: ForgeRegistry,
    forges_file: Path | None,
    write: bool,
    out: Path | None,
    dry_run: bool,
) -> int:
    stubs = propose_accounts(config=cfg, registry=registry, forges_file=forges_file)
    if not stubs:
        print("(no recognized forge hosts in SSH config; nothing to propose)")
        return 1

    rendered = render_proposed_config(stubs)
    if not write or dry_run:
        sys.stdout.write(rendered)
        if dry_run:
            print(f"\n(dry-run) would merge into {out or (config_home() / 'gendia.json')}")
        return 0

    target = (out or (config_home() / "gendia.json")).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)

    existing: dict[str, Any] = {}
    if target.is_file():
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"  ✗ {target} is not valid JSON: {exc}", file=sys.stderr)
            return 2

    proposed = json.loads(rendered)
    merged = dict(existing)
    accounts = dict(merged.get("accounts") or {})
    accounts.update(proposed["accounts"])
    merged["accounts"] = accounts

    target.write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"  ✓ wrote {len(stubs)} proposed account(s) to {target}")
    return 0


# --- helpers ----------------------------------------------------------------


def _resolve_forges_file(args: argparse.Namespace) -> Path | None:
    """Honor `forges_file` from gendia.json when present."""
    try:
        cfg = load_config(project_path=args.config)
    except ConfigError:
        return None
    return Path(cfg.forges_file).expanduser() if cfg.forges_file else None
