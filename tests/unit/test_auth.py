"""Credential resolver + env-store + Docker secrets convention."""

from __future__ import annotations

from pathlib import Path

import pytest

from gendia.auth import CredentialNotFoundError, CredentialResolver, DotEnvCredentialStore


def test_env_store_reads_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_TOKEN", "from-env")
    store = DotEnvCredentialStore()
    assert store.get("MY_TOKEN") == "from-env"


def test_env_store_reads_from_file(tmp_path: Path) -> None:
    f = tmp_path / ".env"
    f.write_text('MY_TOKEN="from-file"\nOTHER=xx\n', encoding="utf-8")
    store = DotEnvCredentialStore(env_file=f)
    assert store.get("MY_TOKEN") == "from-file"
    assert store.get("OTHER") == "xx"
    assert store.get("MISSING") is None


def test_env_store_docker_secrets_convention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = tmp_path / "secret"
    secret.write_text("from-secret-file\n", encoding="utf-8")
    monkeypatch.setenv("MY_TOKEN_FILE", str(secret))
    monkeypatch.delenv("MY_TOKEN", raising=False)
    store = DotEnvCredentialStore()
    assert store.get("MY_TOKEN") == "from-secret-file"


def test_resolver_first_hit_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_TOKEN", "value")
    res = CredentialResolver(DotEnvCredentialStore())
    assert res.get("MY_TOKEN") == "value"


def test_resolver_missing_required_raises() -> None:
    res = CredentialResolver(DotEnvCredentialStore())
    with pytest.raises(CredentialNotFoundError):
        res.get("DEFINITELY_MISSING")


def test_resolver_missing_optional_returns_none() -> None:
    res = CredentialResolver(DotEnvCredentialStore())
    assert res.get("DEFINITELY_MISSING", required=False) is None
