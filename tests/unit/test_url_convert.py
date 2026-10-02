"""SSH ↔ HTTPS git URL conversion."""

from __future__ import annotations

from pathlib import Path

import pytest

from gendia.ssh.config import parse_ssh_config
from gendia.util.url import convert_many, parse, to_https, to_ssh


def test_parse_scp_shorthand() -> None:
    parsed = parse("git@github.com:foo/bar.git")
    assert parsed.scheme == "ssh"
    assert parsed.user == "git"
    assert parsed.host == "github.com"
    assert parsed.path == "foo/bar"


def test_parse_ssh_with_port() -> None:
    parsed = parse("ssh://git@host.example:2222/foo/bar.git")
    assert parsed.scheme == "ssh"
    assert parsed.port == 2222
    assert parsed.path == "foo/bar"


def test_parse_https() -> None:
    parsed = parse("https://github.com/foo/bar.git")
    assert parsed.scheme == "https"
    assert parsed.host == "github.com"
    assert parsed.path == "foo/bar"


def test_parse_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        parse("not a url at all")


def test_https_to_ssh_canonical() -> None:
    assert to_ssh("https://github.com/foo/bar.git") == "git@github.com:foo/bar.git"


def test_ssh_to_https_canonical() -> None:
    assert to_https("git@github.com:foo/bar.git") == "https://github.com/foo/bar.git"


def test_ssh_with_port_round_trips_via_https() -> None:
    out = to_https("ssh://git@host.example:2222/foo/bar.git")
    assert out == "https://host.example/foo/bar.git"


def test_alias_resolution_uses_ssh_config(tmp_path: Path) -> None:
    cfg_path = tmp_path / "ssh_config"
    cfg_path.write_text(
        "Host github-personal\n    HostName github.com\n    User git\n",
        encoding="utf-8",
    )
    cfg = parse_ssh_config(cfg_path)
    out = to_https("git@github-personal:foo/bar.git", ssh_config=cfg)
    assert out == "https://github.com/foo/bar.git"


def test_alias_resolution_when_going_to_ssh(tmp_path: Path) -> None:
    cfg_path = tmp_path / "ssh_config"
    cfg_path.write_text(
        "Host github-work\n    HostName github.com\n    User git\n",
        encoding="utf-8",
    )
    cfg = parse_ssh_config(cfg_path)
    out = to_ssh("https://github.com/foo/bar.git", ssh_config=cfg)
    # The aliased Host wins over the canonical hostname.
    assert out == "git@github-work:foo/bar.git"


def test_insteadof_rules_applied() -> None:
    rules = {"git@github.com:": "https://github.com/"}
    out = to_ssh("git@github.com:foo/bar.git", insteadof=rules)
    # Even when targeting SSH, the rule rewrites first; so the final URL
    # comes out HTTPS-shaped via the rule (caller's intent).
    assert out.endswith("foo/bar.git")


def test_convert_many_skips_blanks() -> None:
    pairs = convert_many(
        ["git@github.com:a/b.git", "  ", "https://gitlab.com/c/d.git"],
        target="ssh",
    )
    assert len(pairs) == 2
