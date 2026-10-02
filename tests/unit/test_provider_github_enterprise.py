"""GitHub Enterprise host honoring."""

from __future__ import annotations

# ruff: noqa: S105, S106
from gendia.config.schema import Account
from gendia.providers.github import GitHubProvider


def test_public_github_unchanged() -> None:
    account = Account(name="x", platform="github", credential_ref="X", org="o")
    p = GitHubProvider(account=account, token=None)
    assert p.clone_url("o/r") == "git@github.com:o/r.git"
    assert p.web_url("o/r") == "https://github.com/o/r"
    assert p._API_ROOT == "https://api.github.com"


def test_enterprise_host_drives_urls() -> None:
    account = Account(
        name="ghe",
        platform="github",
        credential_ref="X",
        org="acme",
        host="ghe.acme.example",
    )
    p = GitHubProvider(account=account, token=None)
    assert p.clone_url("acme/r") == "git@ghe.acme.example:acme/r.git"
    assert p.clone_url("acme/r", ssh=False) == "https://ghe.acme.example/acme/r.git"
    assert p.web_url("acme/r") == "https://ghe.acme.example/acme/r"
    assert p._API_ROOT == "https://ghe.acme.example/api/v3"
