"""Construct the right registry instance for a registry config block."""

from __future__ import annotations

from gendia.auth.resolver import CredentialResolver
from gendia.config.schema import Registry
from gendia.registries.base import PackageRegistry
from gendia.registries.npm import NpmRegistry
from gendia.registries.packagist import PackagistRegistry
from gendia.registries.pypi import PyPIRegistry

_REGISTRIES: dict[str, type[PackageRegistry]] = {
    "packagist": PackagistRegistry,
    "npm": NpmRegistry,
    "pypi": PyPIRegistry,
}


def build_registry(config: Registry, credentials: CredentialResolver) -> PackageRegistry:
    cls = _REGISTRIES.get(config.kind)
    if cls is None:
        raise ValueError(f"no registry registered for kind {config.kind!r}")
    token = (
        credentials.get(config.credential_ref, required=False) if config.credential_ref else None
    )
    return cls(config=config, token=token)


def supported_kinds() -> tuple[str, ...]:
    return tuple(_REGISTRIES.keys())
