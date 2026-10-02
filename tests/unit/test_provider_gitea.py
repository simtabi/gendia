"""Gitea / Forgejo / Codeberg provider URL helpers."""

from __future__ import annotations

# ruff: noqa: S105, S106
from typing import Any

import pytest

from gendia.config.schema import Account
from gendia.providers.gitea import GiteaProvider


@pytest.fixture
def codeberg_provider() -> GiteaProvider:
    account = Account(name="x", platform="gitea", credential_ref="X", username="u")
    return GiteaProvider(account=account, token=None)


@pytest.fixture
def self_hosted_provider() -> GiteaProvider:
    account = Account(
        name="internal",
        platform="gitea",
        credential_ref="X",
        org="ops",
        host="git.example.com",
    )
    return GiteaProvider(account=account, token=None)


def test_default_host_is_codeberg(codeberg_provider: GiteaProvider) -> None:
    assert codeberg_provider.clone_url("u/repo") == "git@codeberg.org:u/repo.git"
    assert codeberg_provider.web_url("u/repo") == "https://codeberg.org/u/repo"


def test_self_hosted_host(self_hosted_provider: GiteaProvider) -> None:
    p = self_hosted_provider
    assert p.clone_url("ops/widget") == "git@git.example.com:ops/widget.git"
    assert p.clone_url("ops/widget", ssh=False) == "https://git.example.com/ops/widget.git"
    assert p.web_url("ops/widget") == "https://git.example.com/ops/widget"


def test_to_repo_info_round_trip() -> None:
    raw: dict[str, Any] = {
        "owner": {"login": "ops"},
        "name": "widget",
        "full_name": "ops/widget",
        "ssh_url": "git@git.example.com:ops/widget.git",
        "clone_url": "https://git.example.com/ops/widget.git",
        "html_url": "https://git.example.com/ops/widget",
        "default_branch": "main",
        "private": True,
    }
    info = GiteaProvider._to_repo_info(raw, host="git.example.com")
    assert info.slug == "ops/widget"
    assert info.clone_url == "git@git.example.com:ops/widget.git"
    assert info.private is True


def test_auth_header_uses_token_format() -> None:
    account = Account(name="x", platform="gitea", credential_ref="X", username="u")
    p = GiteaProvider(account=account, token="abc123")
    assert p._auth_header() == "token abc123"
