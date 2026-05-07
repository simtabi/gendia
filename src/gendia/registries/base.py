"""Package-registry abstractions.

Two distinct interaction patterns exist; we model each with a separate mix-in
(Interface Segregation Principle):

  - WebhookCapable  - registry pulls when notified (Packagist).
  - PublishCapable  - tool pushes the artifact to the registry (npm, PyPI).

A concrete registry implements one or both. Operations check
`isinstance(registry, WebhookCapable)` to decide which path to take.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from gendia.config.schema import Registry
from gendia.observability.logger import get_logger


class RegistryError(RuntimeError):
    """Raised when a registry interaction fails."""


class PackageRegistry(ABC):
    """Common identity + auth for every registry."""

    def __init__(self, config: Registry, token: str | None) -> None:
        self._config = config
        self._token = token
        self._log = get_logger(f"registries.{self.kind}")

    @property
    @abstractmethod
    def kind(self) -> str:
        """Stable string id, e.g. 'packagist'."""

    @property
    def config(self) -> Registry:
        return self._config


class WebhookCapable(PackageRegistry):
    """Registries that learn about new versions by being told a repo URL."""

    @abstractmethod
    def notify(self, repo_url: str) -> None:
        """Tell the registry to refresh metadata for `repo_url`."""


class PublishCapable(PackageRegistry):
    """Registries that ingest pushed artifacts (npm publish, twine upload)."""

    @abstractmethod
    def publish(self, package_path: Path, *, version: str) -> None:
        """Upload the package living at `package_path` (a project directory)."""
