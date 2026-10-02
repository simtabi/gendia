"""Generate's account-to-host mapping fallbacks (no ssh_alias case)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from gendia.config.schema import (
    Account,
    Defaults,
    GendiaConfig,
    GitIdentity,
    Project,
    RepoSpec,
)
from gendia.operations.base import OperationContext
from gendia.operations.generate import GenerateOperation, GenerateRequest
from gendia.ssh.config import SSHConfig, parse_ssh_config
from gendia.ssh.forges import builtin_registry


def _ssh_config(tmp_path: Path) -> SSHConfig:
    p = tmp_path / "ssh_config"
    p.write_text(
        "Host github.com\n    HostName github.com\n    User git\n",
        encoding="utf-8",
    )
    return parse_ssh_config(p)


def _stub_ctx(account: Account) -> OperationContext:
    project = Project(
        name="<no-project>",
        account=account.name,
        root=Path.cwd(),
        repos=(RepoSpec(dir="."),),
    )
    config = GendiaConfig(
        accounts={account.name: account},
        registries={},
        project=project,
        defaults=Defaults(),
    )
    return OperationContext(
        config=config,
        project=project,
        provider=MagicMock(),
        registry=None,
    )


def test_account_maps_to_github_com_via_platform_fallback(tmp_path: Path) -> None:
    """An account with no ssh_alias still binds to a public host that matches its platform."""
    account = Account(
        name="personal",
        platform="github",
        credential_ref="X",
        username="me",
    )
    ctx = _stub_ctx(account)
    op = GenerateOperation(
        ctx,
        request=GenerateRequest(
            fmt="json",
            output_path=None,
            execute=False,
            default_branch="main",
            ssh_config_path=tmp_path / "ssh_config",
            forges_file=None,
            discover=False,
        ),
    )
    indexed = op._index_accounts_by_alias(_ssh_config(tmp_path), builtin_registry())
    assert "github.com" in indexed
    assert indexed["github.com"].name == "personal"


def test_account_with_explicit_ssh_alias_takes_precedence(tmp_path: Path) -> None:
    """When ssh_alias is set, only that alias matches (not the platform fallback)."""
    p = tmp_path / "ssh_config"
    p.write_text(
        "Host github-work\n"
        "    HostName github.com\n"
        "    User git\n"
        "Host github.com\n"
        "    HostName github.com\n"
        "    User git\n",
        encoding="utf-8",
    )
    account = Account(
        name="work",
        platform="github",
        credential_ref="X",
        org="acme",
        git_identity=GitIdentity(name=None, email=None, ssh_alias="github-work"),
    )
    ctx = _stub_ctx(account)
    op = GenerateOperation(
        ctx,
        request=GenerateRequest(
            fmt="json",
            output_path=None,
            execute=False,
            default_branch="main",
            ssh_config_path=p,
            forges_file=None,
            discover=False,
        ),
    )
    indexed = op._index_accounts_by_alias(parse_ssh_config(p), builtin_registry())
    assert indexed.get("github-work") is not None
    # github.com shouldn't get the same account assigned (alias claim is exclusive).
    assert "github.com" not in indexed
