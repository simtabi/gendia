"""SyncPolicy schema + matching behaviour."""

from __future__ import annotations

import pytest

from gendia.config.schema import ConfigError, SyncPolicy


def test_default_is_explicit() -> None:
    p = SyncPolicy()
    assert p.mode == "explicit"
    assert p.include == ()
    assert p.exclude == ()
    assert p.include_private is True


def test_unknown_mode_raises() -> None:
    with pytest.raises(ConfigError, match="must be one of"):
        SyncPolicy(mode="weird")


def test_patterns_mode_requires_include() -> None:
    with pytest.raises(ConfigError, match="non-empty `include`"):
        SyncPolicy(mode="patterns")


def test_all_mode_includes_everything_by_default() -> None:
    p = SyncPolicy(mode="all")
    assert p.is_in_scope(name="anything", archived=False, fork=False, private=False)


def test_all_mode_respects_exclude_globs() -> None:
    p = SyncPolicy(mode="all", exclude=("legacy-*", "sandbox-*"))
    assert p.is_in_scope(name="core", archived=False, fork=False, private=False)
    assert not p.is_in_scope(name="legacy-thing", archived=False, fork=False, private=False)
    assert not p.is_in_scope(name="sandbox-x", archived=False, fork=False, private=False)


def test_all_mode_excludes_archived_by_default() -> None:
    p = SyncPolicy(mode="all")
    assert not p.is_in_scope(name="x", archived=True, fork=False, private=False)


def test_all_mode_includes_archived_when_flag_set() -> None:
    p = SyncPolicy(mode="all", include_archived=True)
    assert p.is_in_scope(name="x", archived=True, fork=False, private=False)


def test_all_mode_excludes_forks_by_default() -> None:
    p = SyncPolicy(mode="all")
    assert not p.is_in_scope(name="x", archived=False, fork=True, private=False)


def test_all_mode_excludes_private_when_flag_off() -> None:
    p = SyncPolicy(mode="all", include_private=False)
    assert p.is_in_scope(name="public", archived=False, fork=False, private=False)
    assert not p.is_in_scope(name="private", archived=False, fork=False, private=True)


def test_patterns_mode_only_matches_include_globs() -> None:
    p = SyncPolicy(mode="patterns", include=("package-*",))
    assert p.is_in_scope(name="package-tools", archived=False, fork=False, private=False)
    assert not p.is_in_scope(name="docs", archived=False, fork=False, private=False)


def test_patterns_mode_exclude_overrides_include() -> None:
    p = SyncPolicy(mode="patterns", include=("package-*",), exclude=("package-scratch",))
    assert p.is_in_scope(name="package-tools", archived=False, fork=False, private=False)
    assert not p.is_in_scope(name="package-scratch", archived=False, fork=False, private=False)


def test_explicit_mode_is_always_in_scope() -> None:
    p = SyncPolicy(mode="explicit")
    # `is_in_scope` returns True; the project.repos list (not this method)
    # decides what's actually tracked.
    assert p.is_in_scope(name="anything", archived=False, fork=False, private=True)


def test_from_dict_round_trip() -> None:
    raw = {
        "mode": "patterns",
        "include": ["package-*"],
        "exclude": ["package-scratch"],
        "include_archived": True,
        "include_forks": True,
        "include_private": False,
    }
    p = SyncPolicy.from_dict(raw)
    assert p.mode == "patterns"
    assert p.include == ("package-*",)
    assert p.exclude == ("package-scratch",)
    assert p.include_archived is True
    assert p.include_forks is True
    assert p.include_private is False


def test_from_dict_none_returns_defaults() -> None:
    p = SyncPolicy.from_dict(None)
    assert p.mode == "explicit"
