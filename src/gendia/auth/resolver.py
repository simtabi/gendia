"""Multi-backend credential resolver.

Tries each backend in order; first hit wins. Backends are constructed by the
caller and injected here, so tests use stub backends without monkey-patching.
"""

from __future__ import annotations

from gendia.auth.base import CredentialNotFoundError, CredentialStore
from gendia.observability.logger import get_logger

_log = get_logger("auth")


class CredentialResolver:
    """Iterate over a list of backends and return the first non-empty hit."""

    def __init__(self, *backends: CredentialStore) -> None:
        if not backends:
            raise ValueError("CredentialResolver: at least one backend required")
        self._backends = tuple(backends)

    @property
    def backends(self) -> tuple[CredentialStore, ...]:
        return self._backends

    def get(self, key: str, *, required: bool = True) -> str | None:
        for backend in self._backends:
            value = backend.get(key)
            if value:
                _log.debug("credential resolved", extra={"key": key, "backend": backend.name})
                return value
        if required:
            raise CredentialNotFoundError(
                f"credential {key!r} not found in any backend "
                f"({', '.join(b.name for b in self._backends)})"
            )
        return None
