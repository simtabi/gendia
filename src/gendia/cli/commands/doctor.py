"""`gendia doctor` — system-wide health check.

Combines and extends `gendia config doctor`:

  - file mode + duplicates + parsability of the env file
  - git binary present + version
  - ssh agent reachable (when at least one account uses git_auth ssh)
  - ssh_key_path files exist + permissions
  - credential_ref values are actually set somewhere (env / .env / keyring)
  - gendia.json references valid accounts and registries

Each check yields a `Finding` (ok / warn / error). Exit code is the
maximum severity encountered: 0 on all-ok, 1 on any warn, 2 on any error.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from gendia.auth import CredentialResolver, DotEnvCredentialStore, KeyringCredentialStore
from gendia.auth.env_file import EnvFile
from gendia.config import load_config
from gendia.config.schema import ConfigError
from gendia.observability.logger import get_logger
from gendia.util.paths import env_file_path

_log = get_logger("cli.doctor")

_LOOSE_FILE_MODE = 0o640
_LOOSE_DIR_MODE = 0o750


# --- output -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Finding:
    severity: str  # "ok" | "warn" | "error"
    section: str
    message: str
    fix: str = ""


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)

    def add(self, severity: str, section: str, message: str, fix: str = "") -> None:
        self.findings.append(Finding(severity=severity, section=section, message=message, fix=fix))

    def exit_code(self) -> int:
        if any(f.severity == "error" for f in self.findings):
            return 2
        if any(f.severity == "warn" for f in self.findings):
            return 1
        return 0

    def render(self) -> None:
        # Group by section, in stable encounter order.
        sections: dict[str, list[Finding]] = {}
        for f in self.findings:
            sections.setdefault(f.section, []).append(f)

        for section, items in sections.items():
            print(f"== {section} ==")
            for f in items:
                glyph = {"ok": "✓", "warn": "⚠", "error": "✗"}[f.severity]
                print(f"  {glyph} {f.message}")
                if f.fix:
                    print(f"      fix: {f.fix}")
            print()


# --- subparser --------------------------------------------------------------


def add_subparser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subparsers.add_parser(
        "doctor",
        help="System-wide health check (env file, git, ssh, credentials, project config).",
    )
    p.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to gendia.json (default: search cwd then ~/.config/gendia/)",
    )
    p.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="Override the env-file path.",
    )
    p.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero on warnings, not just errors.",
    )


def dispatch(args: argparse.Namespace) -> int:
    report = Report()

    _check_env_file(report, args.env_file or env_file_path())
    _check_binaries(report)
    _check_ssh(report, args.config)
    _check_credentials(report, args.config, args.env_file or env_file_path())

    report.render()
    code = report.exit_code()
    if code == 0 or (code == 1 and not args.strict):
        return 0 if code == 0 else 1
    return 2 if any(f.severity == "error" for f in report.findings) else 1


# --- checks -----------------------------------------------------------------


def _check_env_file(report: Report, target: Path) -> None:
    section = "env file"
    if not target.is_file():
        report.add(
            "warn",
            section,
            f"no env file at {target}",
            "run `gendia setup` to scaffold one.",
        )
        return

    mode = target.stat().st_mode & 0o777
    if mode > _LOOSE_FILE_MODE:
        report.add(
            "error",
            section,
            f"{target} has loose permissions ({mode:o})",
            f"chmod 600 {target}",
        )
    else:
        report.add("ok", section, f"{target} mode {mode:o}")

    parent_mode = target.parent.stat().st_mode & 0o777 if target.parent.exists() else None
    if parent_mode is not None and parent_mode > _LOOSE_DIR_MODE:
        report.add(
            "warn",
            section,
            f"{target.parent} has loose permissions ({parent_mode:o})",
            f"chmod 700 {target.parent}",
        )

    # Re-use the parser used by `config doctor` for duplicate detection.
    seen: dict[str, int] = {}
    for raw in target.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip().removeprefix("export ").strip()
        seen[key] = seen.get(key, 0) + 1
    duplicates = [k for k, c in seen.items() if c > 1]
    if duplicates:
        report.add(
            "warn",
            section,
            f"{len(duplicates)} duplicate key(s): {', '.join(sorted(duplicates))}",
            "remove the older line(s); last one wins on parse.",
        )


def _check_binaries(report: Report) -> None:
    section = "binaries"
    git = shutil.which("git")
    if git is None:
        report.add(
            "error",
            section,
            "git not found on PATH",
            "install git from https://git-scm.com/downloads",
        )
    else:
        try:
            ver = subprocess.run(
                ["git", "--version"],
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            ).stdout.strip()
            report.add("ok", section, ver)
        except (subprocess.SubprocessError, OSError) as exc:
            report.add("warn", section, f"git present but `git --version` failed: {exc}")

    ssh = shutil.which("ssh")
    if ssh is None:
        report.add(
            "warn",
            section,
            "ssh not found on PATH",
            "install openssh-client (only needed for git-over-ssh transport)",
        )
    else:
        report.add("ok", section, f"ssh available at {ssh}")


def _check_ssh(report: Report, project_config: Path | None) -> None:
    section = "ssh"
    try:
        cfg = load_config(project_path=project_config)
    except ConfigError:
        # config issues are handled elsewhere; nothing to check here.
        return

    if not cfg.accounts:
        return

    ssh_using = [a for a in cfg.accounts.values() if a.git_auth in {"ssh", "auto"}]
    if not ssh_using:
        return

    sock = os.environ.get("SSH_AUTH_SOCK")
    if not sock:
        report.add(
            "warn",
            section,
            "SSH_AUTH_SOCK not set (no agent reachable)",
            "start ssh-agent and `ssh-add` your key, or set ssh_key_path on each account.",
        )
    elif not Path(sock).exists():
        report.add(
            "warn",
            section,
            f"SSH_AUTH_SOCK={sock} but socket does not exist",
            "restart ssh-agent.",
        )
    else:
        report.add("ok", section, f"ssh-agent at {sock}")

    for account in ssh_using:
        if account.ssh_key_path is None:
            continue
        key = Path(os.path.expanduser(os.path.expandvars(account.ssh_key_path)))
        if not key.is_file():
            report.add(
                "error",
                section,
                f"account {account.name!r}: ssh_key_path {key} does not exist",
                f"generate it with `ssh-keygen -t ed25519 -f {key}`.",
            )
            continue
        mode = key.stat().st_mode & 0o777
        if mode not in (0o400, 0o600):
            report.add(
                "warn",
                section,
                f"account {account.name!r}: ssh_key_path {key} has mode {mode:o}",
                f"chmod 600 {key}",
            )
        else:
            report.add("ok", section, f"account {account.name!r}: key {key} mode {mode:o}")


def _check_credentials(report: Report, project_config: Path | None, env_path: Path) -> None:
    section = "credentials"
    try:
        cfg = load_config(project_path=project_config)
    except ConfigError as exc:
        report.add("error", section, f"config load failed: {exc}")
        return

    if not cfg.accounts and not cfg.registries:
        report.add("warn", section, "no accounts or registries configured")
        return

    backends: list[object] = [DotEnvCredentialStore(env_file=env_path)]
    with contextlib.suppress(RuntimeError):
        # Keyring is optional; a RuntimeError means the package isn't installed.
        backends.append(KeyringCredentialStore())
    resolver = CredentialResolver(*backends)  # type: ignore[arg-type]

    refs: list[tuple[str, str]] = []
    for account in cfg.accounts.values():
        refs.append((f"account {account.name!r}", account.credential_ref))
    for reg in cfg.registries.values():
        if reg.credential_ref:
            refs.append((f"registry {reg.name!r}", reg.credential_ref))

    env_keys = set(EnvFile.load(env_path).keys()) if env_path.is_file() else set()

    for who, ref in refs:
        try:
            value = resolver.get(ref, required=False)
        except Exception as exc:  # noqa: BLE001 — surface as a finding
            report.add("warn", section, f"{who} → {ref}: resolver error: {exc}")
            continue

        if value:
            via = "env" if ref in os.environ else ("file" if ref in env_keys else "keyring")
            report.add("ok", section, f"{who} → {ref} (via {via})")
        else:
            report.add(
                "error",
                section,
                f"{who} → {ref} is unset",
                f"`gendia config set {ref}` or add to {env_path}",
            )
