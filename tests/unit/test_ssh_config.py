"""SSH config parser fixtures."""

from __future__ import annotations

from pathlib import Path

from gendia.ssh.config import SSHConfig, parse_ssh_config


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config"
    p.write_text(body, encoding="utf-8")
    return p


def test_parses_basic_host_block(tmp_path: Path) -> None:
    cfg = parse_ssh_config(
        _write(
            tmp_path,
            """
            Host github.com
                HostName github.com
                User git
                IdentityFile ~/.ssh/id_ed25519
            """,
        )
    )
    hosts = cfg.hosts()
    assert len(hosts) == 1
    h = hosts[0]
    assert h.alias == "github.com"
    assert h.hostname == "github.com"
    assert h.user == "git"
    assert h.primary_identity_file is not None
    assert h.primary_identity_file.endswith("id_ed25519")


def test_handles_multi_host_aliases(tmp_path: Path) -> None:
    cfg = parse_ssh_config(
        _write(
            tmp_path,
            """
            Host github.com gitlab.com bitbucket.org
                User git
            """,
        )
    )
    aliases = sorted(h.alias for h in cfg.hosts())
    assert aliases == ["bitbucket.org", "github.com", "gitlab.com"]


def test_picks_up_comment_above_host(tmp_path: Path) -> None:
    cfg = parse_ssh_config(
        _write(
            tmp_path,
            """
            # Work GitHub Enterprise
            Host work-ghe
                HostName ghe.example.com
                User git
            """,
        )
    )
    h = cfg.hosts().by_alias("work-ghe")
    assert h is not None
    assert "github enterprise" in h.comment.lower()


def test_handles_custom_port_and_user(tmp_path: Path) -> None:
    cfg = parse_ssh_config(
        _write(
            tmp_path,
            """
            Host alt
                HostName alt.example.com
                User unccgit
                Port 443
                IdentityFile ~/.ssh/keys/work/id_ed25519
            """,
        )
    )
    h = cfg.hosts().by_alias("alt")
    assert h is not None
    assert h.user == "unccgit"
    assert h.port == 443
    assert h.effective_user == "unccgit"
    assert h.effective_port == 443


def test_excludes_wildcards(tmp_path: Path) -> None:
    cfg = parse_ssh_config(
        _write(
            tmp_path,
            """
            Host *
                IdentityFile ~/.ssh/id_ed25519
            Host github.com
                User git
            """,
        )
    )
    hosts = cfg.hosts().exclude_wildcards()
    assert {h.alias for h in hosts} == {"github.com"}


def test_with_identity_file_filter(tmp_path: Path) -> None:
    cfg = parse_ssh_config(
        _write(
            tmp_path,
            """
            Host has-key
                IdentityFile ~/.ssh/k
            Host no-key
                User git
            """,
        )
    )
    aliases = {h.alias for h in cfg.hosts().with_identity_file()}
    assert aliases == {"has-key"}


def test_missing_file_returns_empty_config(tmp_path: Path) -> None:
    cfg = SSHConfig.load(tmp_path / "does-not-exist")
    assert cfg.hosts() == ()


def test_filter_kwargs(tmp_path: Path) -> None:
    cfg = parse_ssh_config(
        _write(
            tmp_path,
            """
            Host a
                HostName a.example
                User alice
            Host b
                HostName b.example
                User bob
            """,
        )
    )
    matched = cfg.hosts().filter(user="alice")
    assert {h.alias for h in matched} == {"a"}
