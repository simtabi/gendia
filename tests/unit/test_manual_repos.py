"""Schema invariants for ManualRepo and Account.manual_repos plumbing."""

from __future__ import annotations

import pytest

from gendia.config.schema import Account, ConfigError, ManualRepo


def test_manual_repo_requires_host() -> None:
    with pytest.raises(ConfigError, match="host is required"):
        ManualRepo(host="", org="o", repos=("r",))


def test_manual_repo_requires_org() -> None:
    with pytest.raises(ConfigError, match="org is required"):
        ManualRepo(host="h", org="", repos=("r",))


def test_manual_repo_requires_non_empty_repos() -> None:
    with pytest.raises(ConfigError, match="repos\\[\\] cannot be empty"):
        ManualRepo(host="h", org="o", repos=())


def test_manual_repo_from_dict() -> None:
    m = ManualRepo.from_dict({"host": "github-personal", "org": "me", "repos": ["a", "b"]})
    assert m.host == "github-personal"
    assert m.org == "me"
    assert m.repos == ("a", "b")


def test_account_default_manual_repos_is_empty() -> None:
    a = Account(name="x", platform="github", credential_ref="X", org="o")
    assert a.manual_repos == ()


def test_account_from_dict_round_trips_manual_repos() -> None:
    payload = {
        "platform": "github",
        "credential_ref": "X",
        "org": "myorg",
        "manual_repos": [
            {"host": "github-personal", "org": "me", "repos": ["dotfiles"]},
            {"host": "self-hosted", "org": "ops", "repos": ["secret-1", "secret-2"]},
        ],
    }
    a = Account.from_dict("personal", payload)
    assert len(a.manual_repos) == 2
    assert a.manual_repos[0].host == "github-personal"
    assert a.manual_repos[1].repos == ("secret-1", "secret-2")
