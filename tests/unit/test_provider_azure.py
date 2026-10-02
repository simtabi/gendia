"""Azure DevOps provider URL helpers + auth header."""

from __future__ import annotations

# ruff: noqa: S105, S106
import base64

import pytest

from gendia.config.schema import Account
from gendia.providers.azure import AzureDevOpsProvider


@pytest.fixture
def azure_provider() -> AzureDevOpsProvider:
    account = Account(
        name="myorg",
        platform="azure",
        credential_ref="X",
        org="myorg",
        workspace="MyProject",
    )
    return AzureDevOpsProvider(account=account, token="azurePAT123")


def test_ssh_clone_url(azure_provider: AzureDevOpsProvider) -> None:
    url = azure_provider.clone_url("MyProject/widget")
    assert url == "git@ssh.dev.azure.com:v3/myorg/MyProject/widget"


def test_https_clone_url(azure_provider: AzureDevOpsProvider) -> None:
    url = azure_provider.clone_url("MyProject/widget", ssh=False)
    assert url == "https://dev.azure.com/myorg/MyProject/_git/widget"


def test_web_url(azure_provider: AzureDevOpsProvider) -> None:
    assert azure_provider.web_url("MyProject/widget") == (
        "https://dev.azure.com/myorg/MyProject/_git/widget"
    )


def test_auth_header_is_basic_with_empty_user(azure_provider: AzureDevOpsProvider) -> None:
    header = azure_provider._auth_header()
    assert header.startswith("Basic ")
    decoded = base64.b64decode(header.removeprefix("Basic ")).decode("ascii")
    assert decoded == ":azurePAT123"
