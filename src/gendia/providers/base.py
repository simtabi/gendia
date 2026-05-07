"""Abstract VCS-platform contract.

Every concrete provider (GitHub / GitLab / Bitbucket) implements this surface.
Operations depend on `GitProvider`, never on a concrete class, so swapping
backends is a one-line change at the factory.
"""

from __future__ import annotations

import contextlib
import json
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from gendia import __version__ as _gendia_version_str
from gendia.config.schema import Account
from gendia.observability.logger import get_logger


def _gendia_version() -> str:
    return _gendia_version_str


class ProviderError(RuntimeError):
    """Raised when an upstream API call fails or returns unexpected data."""


@dataclass(frozen=True, slots=True)
class RepoInfo:
    """Lightweight remote-side view of a repository."""

    slug: str  # 'owner/repo' or platform-equivalent
    clone_url: str
    web_url: str
    default_branch: str
    private: bool


class GitProvider(ABC):
    """Read-mostly interface; mutations are explicit and fail loudly when
    not yet implemented for a backend.
    """

    def __init__(self, account: Account, token: str | None) -> None:
        self._account = account
        self._token = token
        self._log = get_logger(f"providers.{self.platform}")

    # --- identity ------------------------------------------------------------

    @property
    @abstractmethod
    def platform(self) -> str:
        """Stable string id, e.g. 'github'."""

    @property
    def account(self) -> Account:
        return self._account

    # --- URL helpers ---------------------------------------------------------

    @abstractmethod
    def clone_url(self, slug: str, *, ssh: bool = True) -> str:
        """Return a git-cloneable URL for `slug` (without protocol auth)."""

    @abstractmethod
    def web_url(self, slug: str) -> str:
        """Return the human-readable URL for `slug`."""

    # --- queries -------------------------------------------------------------

    @abstractmethod
    def list_repos(self) -> list[RepoInfo]:
        """List repositories under the configured scope (org / group / user)."""

    @abstractmethod
    def get_repo(self, slug: str) -> RepoInfo:
        """Return one repo's metadata; raise ProviderError if absent."""

    # --- mutations (optional; subclasses MAY raise NotImplementedError) ----

    def create_repo(self, slug: str, *, private: bool = False, description: str = "") -> RepoInfo:
        raise NotImplementedError(f"{self.platform}: create_repo not yet implemented")

    def set_secret(self, slug: str, name: str, value: str) -> None:
        raise NotImplementedError(f"{self.platform}: set_secret not yet implemented")

    # --- transport (shared by concrete providers) ---------------------------

    def _request(
        self,
        method: str,
        url: str,
        *,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
        accept_codes: tuple[int, ...] = (200, 201, 202, 204),
    ) -> dict[str, Any] | list[Any] | None:
        """Issue an HTTP request, parse JSON, and surface errors uniformly."""
        all_headers = {
            "Accept": "application/json",
            "User-Agent": f"gendia/{self._user_agent_suffix()}",
        }
        if headers:
            all_headers.update(headers)
        if self._token:
            all_headers.setdefault("Authorization", self._auth_header())

        data: bytes | None = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            all_headers.setdefault("Content-Type", "application/json")

        # URL is built from provider config (constants + scope strings); we
        # do not accept arbitrary user URLs. Schemes are always https://.
        if not url.startswith("https://"):
            raise ProviderError(f"refuse non-https URL: {url}")

        req = urllib.request.Request(  # noqa: S310 — scheme is validated above
            url,
            data=data,
            headers=all_headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — scheme validated
                if resp.status not in accept_codes:
                    raise ProviderError(f"{method} {url}: unexpected status {resp.status}")
                raw = resp.read()
                if not raw:
                    return None
                try:
                    parsed: dict[str, Any] | list[Any] = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise ProviderError(f"{method} {url}: invalid JSON: {exc}") from exc
                return parsed
        except urllib.error.HTTPError as exc:
            body_text = ""
            with contextlib.suppress(Exception):
                body_text = exc.read().decode("utf-8", errors="replace")
            raise ProviderError(f"{method} {url}: HTTP {exc.code}: {body_text[:200]}") from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"{method} {url}: network error: {exc}") from exc

    @abstractmethod
    def _auth_header(self) -> str:
        """Return the value for the Authorization header."""

    def _user_agent_suffix(self) -> str:
        return _gendia_version()
