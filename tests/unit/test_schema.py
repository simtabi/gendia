"""Schema invariants."""

from __future__ import annotations

from pathlib import Path

import pytest

from gendia.config.schema import (
    Account,
    ConfigError,
    Defaults,
    Project,
    Registry,
    RepoSpec,
)


def test_account_requires_a_scope() -> None:
    with pytest.raises(ConfigError, match="must set one of"):
        Account(name="bare", platform="github", credential_ref="X")


def test_account_rejects_unknown_platform() -> None:
    with pytest.raises(ConfigError, match="unsupported platform"):
        Account(name="x", platform="codeberg", credential_ref="X", org="o")


def test_account_scope_prefers_org_then_workspace_then_group_then_user() -> None:
    a = Account(name="x", platform="github", credential_ref="X", org="o", username="u")
    assert a.scope == "o"
    b = Account(name="x", platform="bitbucket", credential_ref="X", workspace="w")
    assert b.scope == "w"
    c = Account(name="x", platform="gitlab", credential_ref="X", group="g")
    assert c.scope == "g"


def test_registry_validates_kind() -> None:
    Registry(name="ok", kind="packagist")
    with pytest.raises(ConfigError, match="unsupported kind"):
        Registry(name="bad", kind="rubygems")


def test_project_requires_repos() -> None:
    with pytest.raises(ConfigError, match="repos"):
        Project(name="p", account="a", root=Path("."), repos=())


def test_project_repo_lookup() -> None:
    p = Project(
        name="x",
        account="a",
        root=Path("."),
        repos=(RepoSpec(dir="core"), RepoSpec(dir="browser")),
    )
    assert p.repo("core").dir == "core"
    with pytest.raises(ConfigError):
        p.repo("nonexistent")


def test_defaults_concurrency_must_be_positive() -> None:
    Defaults(concurrency=1)
    with pytest.raises(ConfigError):
        Defaults(concurrency=0)


def test_repospec_serialises_round_trip() -> None:
    raw = {"dir": "core", "package": "vendor/core", "verify": ["pest"]}
    r = RepoSpec.from_dict(raw)
    assert r.dir == "core"
    assert r.package == "vendor/core"
    assert r.verify == ("pest",)
