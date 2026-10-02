"""Forge registry detection logic + JSON loader."""

from __future__ import annotations

import json
from pathlib import Path

from gendia.ssh.forges import builtin_registry, load_registry


def test_exact_match_wins() -> None:
    reg = builtin_registry()
    assert reg.detect(hostname="github.com") == "github"
    assert reg.detect(hostname="gitlab.com") == "gitlab"
    assert reg.detect(hostname="bitbucket.org") == "bitbucket"
    assert reg.detect(hostname="codeberg.org") == "codeberg"


def test_pattern_match_for_self_hosted() -> None:
    reg = builtin_registry()
    assert reg.detect(hostname="gitlab.example.com") == "gitlab"
    assert reg.detect(hostname="github.work.local") == "github-enterprise"


def test_comment_keyword_detection() -> None:
    reg = builtin_registry()
    # Hostname doesn't match any pattern; comment supplies the platform.
    assert (
        reg.detect(hostname="ssh.acme.example", comment="# Acme GitHub Enterprise")
        == "github-enterprise"
    )
    assert reg.detect(hostname="git.acme.local", comment="# Internal Forgejo") == "forgejo"


def test_user_git_falls_back_to_custom() -> None:
    reg = builtin_registry()
    assert reg.detect(hostname="repos.private.example", user="git") == "custom"


def test_unrecognized_host_returns_none() -> None:
    reg = builtin_registry()
    assert reg.detect(hostname="example.com") is None


def test_provider_platform_aliases() -> None:
    reg = builtin_registry()
    assert reg.provider_platform("github") == "github"
    assert reg.provider_platform("github-enterprise") == "github"
    assert reg.provider_platform("forgejo") == "gitea"
    assert reg.provider_platform("codeberg") == "gitea"
    assert reg.provider_platform("azure-devops") == "azure"
    assert reg.provider_platform("custom") is None


def test_derive_label_from_identity_file() -> None:
    reg = builtin_registry()
    label = reg.derive_label(
        hostname="github.com",
        identity_file="~/.ssh/keys/personal/id_ed25519",
    )
    assert label == "personal"


def test_derive_label_from_self_hosted_hostname() -> None:
    reg = builtin_registry()
    label = reg.derive_label(hostname="gitlab.acme-corp.example.com")
    # Most-significant chunk excluding TLDs.
    assert label in {"example", "acme-corp", "gitlab"}


def test_load_registry_from_disk(tmp_path: Path) -> None:
    payload = {
        "known_forges_exact": {"git.example": "github-enterprise"},
        "known_forges_patterns": [],
        "public_forges": [],
        "comment_keywords": [],
        "tlds": ["com"],
    }
    path = tmp_path / "forges.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    reg = load_registry(path)
    assert reg.detect(hostname="git.example") == "github-enterprise"


def test_load_registry_falls_back_on_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    reg = load_registry(path)
    # Falls back to the bundled (or built-in) registry.
    assert reg.detect(hostname="github.com") == "github"
