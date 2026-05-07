"""GitIdentity schema + Account integration."""

from __future__ import annotations

from gendia.config.schema import Account, GitIdentity


def test_default_is_empty() -> None:
    g = GitIdentity()
    assert g.is_empty()
    assert g.name is None
    assert g.email is None


def test_from_dict_round_trip() -> None:
    g = GitIdentity.from_dict(
        {
            "name": "Your Name",
            "email": "<id>+yourhandle@users.noreply.github.com",
            "ssh_alias": "github-personal",
            "signing_key": "~/.ssh/id_ed25519.pub",
            "sign_commits": True,
            "sign_tags": True,
        }
    )
    assert not g.is_empty()
    assert g.email == "<id>+yourhandle@users.noreply.github.com"
    assert g.ssh_alias == "github-personal"
    assert g.sign_commits is True
    assert g.sign_tags is True


def test_from_dict_none() -> None:
    assert GitIdentity.from_dict(None).is_empty()
    assert GitIdentity.from_dict({}).is_empty()


def test_account_has_default_empty_identity() -> None:
    a = Account(name="x", platform="github", credential_ref="X", org="o")
    assert a.git_identity.is_empty()


def test_account_carries_identity_through_from_dict() -> None:
    a = Account.from_dict(
        "myorg",
        {
            "platform": "github",
            "org": "myorg",
            "credential_ref": "GITHUB_TOKEN_MYORG",
            "git_identity": {
                "name": "Your Name",
                "email": "<id>+yourhandle@users.noreply.github.com",
            },
        },
    )
    assert a.git_identity.email == "<id>+yourhandle@users.noreply.github.com"
    assert a.git_identity.name == "Your Name"
    assert not a.git_identity.is_empty()


def test_partial_identity_only_name() -> None:
    g = GitIdentity(name="X")
    assert not g.is_empty()
    assert g.email is None
