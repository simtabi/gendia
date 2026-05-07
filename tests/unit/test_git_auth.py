"""Auth-environment selection logic.

These tests use string literals like 'ghp_secret' and 't' as test fixtures —
they are not real secrets. We tell ruff to skip the S105/S106 audit for the
file rather than annotate every line.
"""
# ruff: noqa: S105, S106

from __future__ import annotations

import os
import stat

import pytest

from gendia.config.schema import Account
from gendia.git.auth import build_environment


def _account(**kwargs) -> Account:
    base = {"name": "x", "platform": "github", "credential_ref": "X", "org": "o"}
    base.update(kwargs)
    return Account(**base)


def test_ssh_default_sets_git_ssh_command(tmp_path) -> None:
    key = tmp_path / "id_ed25519"
    key.write_text("dummy", encoding="utf-8")
    a = _account(ssh_key_path=str(key), git_auth="ssh")

    with build_environment(a, api_token="not-used", base_env={}) as env:
        assert env.transport == "ssh"
        assert env.env["GIT_SSH_COMMAND"].startswith("ssh -i ")
        assert "IdentitiesOnly=yes" in env.env["GIT_SSH_COMMAND"]
        assert env.env["GIT_TERMINAL_PROMPT"] == "0"


def test_https_with_token_writes_executable_askpass() -> None:
    a = _account(git_auth="https")

    with build_environment(a, api_token="ghp_secret", base_env={}) as env:
        assert env.transport == "https"
        askpass = env.env["GIT_ASKPASS"]
        assert os.path.isfile(askpass)
        mode = os.stat(askpass).st_mode
        assert mode & stat.S_IXUSR
        assert env.env["GENDIA_GIT_TOKEN"] == "ghp_secret"
        # The system credential helper must be neutralised.
        assert env.env["GIT_CONFIG_KEY_0"] == "credential.helper"
        assert env.env["GIT_CONFIG_VALUE_0"] == ""

    # After context exit, the askpass tempdir is cleaned up.
    assert not os.path.exists(askpass)


def test_https_without_token_fails_loudly() -> None:
    a = _account(git_auth="https")
    with pytest.raises(ValueError, match="no API token resolved"):
        build_environment(a, api_token=None, base_env={})


def test_auto_prefers_ssh_when_key_path_set(tmp_path) -> None:
    key = tmp_path / "id_ed25519"
    key.write_text("dummy", encoding="utf-8")
    a = _account(ssh_key_path=str(key), git_auth="auto")

    with build_environment(a, api_token="t", base_env={}) as env:
        assert env.transport == "ssh"


def test_auto_falls_back_to_https_when_no_ssh(tmp_path, monkeypatch) -> None:
    # Pretend ~/.ssh doesn't exist for this test.
    monkeypatch.setattr("pathlib.Path.is_dir", lambda self: False)
    a = _account(git_auth="auto")

    with build_environment(a, api_token="t", base_env={}) as env:
        assert env.transport == "https"
