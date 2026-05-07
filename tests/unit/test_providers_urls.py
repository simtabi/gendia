"""URL helpers + parser fixtures for the three VCS providers.

These tests are pure data-shape checks; they do not hit any network. The
provider classes are constructed with token=None and only the URL builder
methods + the static _to_repo_info parsers are exercised.

Test fixtures use string literals like 'alice:apppass123' as fake credentials;
they are not real secrets, so we silence ruff's S106 audit for the file.
"""
# ruff: noqa: S105, S106

from __future__ import annotations

from typing import Any

import pytest

from gendia.config.schema import Account
from gendia.providers.bitbucket import BitbucketProvider
from gendia.providers.github import GitHubProvider
from gendia.providers.gitlab import GitLabProvider

# --- GitHub ------------------------------------------------------------------


@pytest.fixture
def github_org_provider() -> GitHubProvider:
    account = Account(name="x", platform="github", credential_ref="X", org="myorg")
    return GitHubProvider(account=account, token=None)


def test_github_clone_url_ssh(github_org_provider: GitHubProvider) -> None:
    assert github_org_provider.clone_url("myorg/core") == "git@github.com:myorg/core.git"


def test_github_clone_url_https(github_org_provider: GitHubProvider) -> None:
    assert (
        github_org_provider.clone_url("myorg/core", ssh=False)
        == "https://github.com/myorg/core.git"
    )


def test_github_web_url(github_org_provider: GitHubProvider) -> None:
    assert github_org_provider.web_url("myorg/core") == "https://github.com/myorg/core"


def test_github_to_repo_info_round_trip() -> None:
    raw: dict[str, Any] = {
        "full_name": "owner/name",
        "ssh_url": "git@github.com:owner/name.git",
        "clone_url": "https://github.com/owner/name.git",
        "html_url": "https://github.com/owner/name",
        "default_branch": "main",
        "private": False,
    }
    info = GitHubProvider._to_repo_info(raw)
    assert info.slug == "owner/name"
    assert info.clone_url == "git@github.com:owner/name.git"  # ssh preferred
    assert info.web_url == "https://github.com/owner/name"
    assert info.default_branch == "main"
    assert info.private is False


def test_github_to_repo_info_falls_back_to_https_when_no_ssh_url() -> None:
    info = GitHubProvider._to_repo_info(
        {
            "full_name": "owner/name",
            "clone_url": "https://github.com/owner/name.git",
            "html_url": "https://github.com/owner/name",
            "default_branch": "main",
            "private": True,
        }
    )
    assert info.clone_url == "https://github.com/owner/name.git"
    assert info.private is True


# --- GitLab ------------------------------------------------------------------


@pytest.fixture
def gitlab_group_provider() -> GitLabProvider:
    account = Account(name="x", platform="gitlab", credential_ref="X", group="acme")
    return GitLabProvider(account=account, token=None)


@pytest.fixture
def gitlab_self_hosted_provider() -> GitLabProvider:
    account = Account(
        name="internal",
        platform="gitlab",
        credential_ref="X",
        group="tools",
        host="gitlab.example.com",
    )
    return GitLabProvider(account=account, token=None)


def test_gitlab_default_host(gitlab_group_provider: GitLabProvider) -> None:
    assert gitlab_group_provider.clone_url("acme/lib") == "git@gitlab.com:acme/lib.git"
    assert gitlab_group_provider.web_url("acme/lib") == "https://gitlab.com/acme/lib"


def test_gitlab_self_hosted_host(gitlab_self_hosted_provider: GitLabProvider) -> None:
    p = gitlab_self_hosted_provider
    assert p.clone_url("tools/widget") == "git@gitlab.example.com:tools/widget.git"
    assert p.web_url("tools/widget") == "https://gitlab.example.com/tools/widget"


def test_gitlab_to_repo_info_uses_path_with_namespace() -> None:
    info = GitLabProvider._to_repo_info(
        {
            "path_with_namespace": "group/sub/proj",
            "ssh_url_to_repo": "git@gitlab.com:group/sub/proj.git",
            "http_url_to_repo": "https://gitlab.com/group/sub/proj.git",
            "web_url": "https://gitlab.com/group/sub/proj",
            "default_branch": "main",
            "visibility": "private",
        }
    )
    assert info.slug == "group/sub/proj"
    assert info.private is True


# --- Bitbucket ---------------------------------------------------------------


@pytest.fixture
def bitbucket_provider() -> BitbucketProvider:
    account = Account(
        name="x",
        platform="bitbucket",
        credential_ref="X",
        workspace="acme",
    )
    return BitbucketProvider(account=account, token=None)


def test_bitbucket_urls(bitbucket_provider: BitbucketProvider) -> None:
    assert bitbucket_provider.clone_url("acme/lib") == "git@bitbucket.org:acme/lib.git"
    assert bitbucket_provider.web_url("acme/lib") == "https://bitbucket.org/acme/lib"


def test_bitbucket_basic_auth_header_when_colon_present() -> None:
    account = Account(
        name="x",
        platform="bitbucket",
        credential_ref="X",
        workspace="acme",
    )
    p = BitbucketProvider(account=account, token="alice:apppass123")
    header = p._auth_header()
    assert header.startswith("Basic ")


def test_bitbucket_bearer_auth_header_when_no_colon() -> None:
    account = Account(
        name="x",
        platform="bitbucket",
        credential_ref="X",
        workspace="acme",
    )
    p = BitbucketProvider(account=account, token="bbtok_xxxxxxxxxxxx")
    assert p._auth_header() == "Bearer bbtok_xxxxxxxxxxxx"


def test_bitbucket_to_repo_info_picks_ssh_clone_link() -> None:
    info = BitbucketProvider._to_repo_info(
        {
            "full_name": "ws/repo",
            "links": {
                "clone": [
                    {"name": "https", "href": "https://bitbucket.org/ws/repo.git"},
                    {"name": "ssh", "href": "git@bitbucket.org:ws/repo.git"},
                ],
                "html": {"href": "https://bitbucket.org/ws/repo"},
            },
            "mainbranch": {"name": "main"},
            "is_private": True,
        }
    )
    assert info.slug == "ws/repo"
    assert info.clone_url == "git@bitbucket.org:ws/repo.git"
    assert info.private is True


def test_bitbucket_to_repo_info_falls_back_to_https_when_no_ssh() -> None:
    info = BitbucketProvider._to_repo_info(
        {
            "full_name": "ws/repo",
            "links": {
                "clone": [
                    {"name": "https", "href": "https://bitbucket.org/ws/repo.git"},
                ],
                "html": {"href": "https://bitbucket.org/ws/repo"},
            },
        }
    )
    assert info.clone_url == "https://bitbucket.org/ws/repo.git"
    assert info.default_branch == "main"  # default when mainbranch absent
