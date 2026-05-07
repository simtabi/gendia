"""Credential-store abstractions.

Multiple backends can coexist; the resolver tries them in order. Each backend
implements the same minimal protocol so adding a new one (e.g. HashiCorp
Vault, AWS Secrets Manager, Docker secrets) is one new file.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class CredentialNotFoundError(LookupError):
    """Raised when a referenced credential cannot be resolved by any backend."""


class CredentialStore(ABC):
    """One-way read interface for retrieving secrets by name.

    Backends are read-only by design. Writing secrets is intentionally outside
    gendia's scope: secrets should be provisioned by platform tooling
    (`pass`, `op`, Vault CLI, Docker secrets, etc.), not by orchestration tools.
    """

    @abstractmethod
    def get(self, key: str) -> str | None:
        """Return the secret for `key`, or `None` if unavailable.

        Implementations MUST NOT raise on missing keys; the resolver decides
        whether absence is fatal.
        """

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier for logs (e.g. 'env', 'keyring', 'docker-secret')."""
