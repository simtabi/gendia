"""Build the environment a `git` subprocess needs for SSH or HTTPS-with-token auth.

Two transports, two patterns:

  SSH:
    git operations use `ssh -i <key> -o IdentitiesOnly=yes`. We feed git via
    the `GIT_SSH_COMMAND` env var; no global ~/.ssh/config edits.

  HTTPS-with-token:
    git invokes `GIT_ASKPASS` whenever it needs a credential. We provide a
    one-line shell script that prints the token. The token never appears in
    argv (where it would be visible in `ps`) or in the URL (where it would
    leak into reflogs). The script lives in a tmpdir for the duration of one
    operation and is removed in `cleanup_environment`.

Both helpers also force `GIT_TERMINAL_PROMPT=0` so a misconfigured deployment
fails loudly instead of hanging on a tty prompt.
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from gendia.config.schema import Account


@dataclass
class AuthEnvironment:
    """Bundle of env vars + temp files used for one git invocation."""

    env: dict[str, str]
    transport: str  # "ssh" | "https" | "default"
    _tmpdir: Path | None = None

    def cleanup(self) -> None:
        """Remove the temp ASKPASS script (no-op for SSH)."""
        if self._tmpdir is not None and self._tmpdir.exists():
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None

    def __enter__(self) -> AuthEnvironment:
        return self

    def __exit__(self, *exc: object) -> None:
        self.cleanup()


def _expand(value: str) -> str:
    return os.path.expanduser(os.path.expandvars(value))


def _resolve_transport(account: Account, *, https_token_present: bool) -> str:
    if account.git_auth == "ssh":
        return "ssh"
    if account.git_auth == "https":
        return "https"
    # auto: prefer ssh when key available, else https-with-token.
    if account.ssh_key_path:
        return "ssh"
    home_ssh = Path("~/.ssh").expanduser()
    if home_ssh.is_dir() and any(home_ssh.iterdir()):
        return "ssh"
    return "https" if https_token_present else "default"


def build_environment(
    account: Account,
    *,
    api_token: str | None,
    base_env: dict[str, str] | None = None,
) -> AuthEnvironment:
    """Return env vars to pass to `subprocess.run` for git operations.

    `base_env` defaults to a copy of `os.environ`. The returned env preserves
    everything in `base_env` and layers our auth keys on top.
    """
    env = dict(base_env if base_env is not None else os.environ)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")

    transport = _resolve_transport(account, https_token_present=bool(api_token))

    if transport == "ssh":
        ssh_cmd_parts = ["ssh"]
        if account.ssh_key_path:
            ssh_cmd_parts += [
                "-i",
                _expand(account.ssh_key_path),
                "-o",
                "IdentitiesOnly=yes",
            ]
        if not account.ssh_known_hosts_strict:
            ssh_cmd_parts += [
                "-o",
                "StrictHostKeyChecking=accept-new",
                "-o",
                "UserKnownHostsFile=/dev/null",
            ]
        env["GIT_SSH_COMMAND"] = " ".join(ssh_cmd_parts)
        return AuthEnvironment(env=env, transport="ssh")

    if transport == "https":
        if not api_token:
            raise ValueError(
                f"account {account.name!r}: git_auth=https but no API token resolved "
                f"from credential_ref={account.credential_ref!r}"
            )
        tmpdir = Path(tempfile.mkdtemp(prefix="gendia-askpass-"))
        askpass = tmpdir / "askpass.sh"
        askpass.write_text(
            "#!/bin/sh\n"
            'case "$1" in\n'
            "  Username*) printf '%s\\n' x-access-token ;;\n"
            "  Password*) printf '%s\\n' \"$GENDIA_GIT_TOKEN\" ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        askpass.chmod(askpass.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        env["GIT_ASKPASS"] = str(askpass)
        env["GENDIA_GIT_TOKEN"] = api_token
        # Disable the system credential helper so it can't shadow us.
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "credential.helper"
        env["GIT_CONFIG_VALUE_0"] = ""
        return AuthEnvironment(env=env, transport="https", _tmpdir=tmpdir)

    # default: no auth env layered on top; rely on whatever the host has.
    return AuthEnvironment(env=env, transport="default")
