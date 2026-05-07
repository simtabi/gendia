"""Construct the right `GitProvider` for an account."""

from __future__ import annotations

from gendia.auth.resolver import CredentialResolver
from gendia.config.schema import Account
from gendia.providers.base import GitProvider
from gendia.providers.bitbucket import BitbucketProvider
from gendia.providers.github import GitHubProvider
from gendia.providers.gitlab import GitLabProvider

_PROVIDERS: dict[str, type[GitProvider]] = {
    "github": GitHubProvider,
    "gitlab": GitLabProvider,
    "bitbucket": BitbucketProvider,
}


def build_provider(account: Account, credentials: CredentialResolver) -> GitProvider:
    """Resolve credentials and instantiate the concrete provider class."""
    cls = _PROVIDERS.get(account.platform)
    if cls is None:
        raise ValueError(f"no provider registered for platform {account.platform!r}")

    token = credentials.get(account.credential_ref, required=False)
    return cls(account=account, token=token)


def supported_platforms() -> tuple[str, ...]:
    """Return the list of provider platform IDs."""
    return tuple(_PROVIDERS.keys())
