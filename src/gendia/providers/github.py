"""GitHub provider, primary platform.

API: https://docs.github.com/en/rest
Auth: PAT or fine-grained PAT, sent as `Authorization: Bearer <token>`.
"""

from __future__ import annotations

import urllib.parse
from typing import Any

from gendia.providers.base import GitProvider, ProviderError, RepoInfo


class GitHubProvider(GitProvider):
    _PUBLIC_HOST = "github.com"
    _PUBLIC_API = "https://api.github.com"

    @property
    def platform(self) -> str:
        return "github"

    @property
    def _is_enterprise(self) -> bool:
        host = self._account.host
        return bool(host) and host != self._PUBLIC_HOST

    @property
    def _host(self) -> str:
        return self._account.host or self._PUBLIC_HOST

    @property
    def _API_ROOT(self) -> str:  # noqa: N802 — name kept for backwards compat
        # GitHub Enterprise Server exposes its REST API at /api/v3.
        if self._is_enterprise:
            return f"https://{self._host}/api/v3"
        return self._PUBLIC_API

    def _auth_header(self) -> str:
        return f"Bearer {self._token}"

    # --- URL helpers ---------------------------------------------------------

    def clone_url(self, slug: str, *, ssh: bool = True) -> str:
        host = self._host
        if ssh:
            return f"git@{host}:{slug}.git"
        return f"https://{host}/{slug}.git"

    def web_url(self, slug: str) -> str:
        return f"https://{self._host}/{slug}"

    # --- queries -------------------------------------------------------------

    def list_repos(self) -> list[RepoInfo]:
        scope = self._account.scope
        if not scope:
            raise ProviderError("github: account has no org/username scope")

        path = f"/orgs/{scope}/repos" if self._account.org else f"/users/{scope}/repos"
        return list(self._paginate_repos(path, params={"per_page": "100", "type": "all"}))

    def get_repo(self, slug: str) -> RepoInfo:
        url = f"{self._API_ROOT}/repos/{slug}"
        raw = self._request("GET", url)
        if not isinstance(raw, dict):
            raise ProviderError(f"github: unexpected response shape for {slug}")
        return self._to_repo_info(raw)

    # --- mutations -----------------------------------------------------------

    def create_repo(self, slug: str, *, private: bool = False, description: str = "") -> RepoInfo:
        owner, _, name = slug.partition("/")
        if not owner or not name:
            raise ProviderError(f"github: slug must be 'owner/repo', got {slug!r}")

        if self._account.org:
            url = f"{self._API_ROOT}/orgs/{owner}/repos"
        else:
            url = f"{self._API_ROOT}/user/repos"

        body = {"name": name, "private": private, "description": description}
        raw = self._request("POST", url, body=body, accept_codes=(201,))
        if not isinstance(raw, dict):
            raise ProviderError("github: create_repo returned unexpected shape")
        return self._to_repo_info(raw)

    def set_secret(self, slug: str, name: str, value: str) -> None:
        # Real implementation requires libsodium for sealed-box encryption.
        # We document this rather than ship a half-working version.
        raise NotImplementedError(
            "github: setting Actions secrets requires libsodium sealed-box encryption; "
            "use `gh secret set` for now (see docs/recipes/github-secrets.md)"
        )

    # --- internals -----------------------------------------------------------

    def _paginate_repos(self, path: str, *, params: dict[str, str]) -> list[RepoInfo]:
        out: list[RepoInfo] = []
        page = 1
        while True:
            qs = urllib.parse.urlencode({**params, "page": str(page)})
            url = f"{self._API_ROOT}{path}?{qs}"
            raw = self._request("GET", url)
            if not isinstance(raw, list) or not raw:
                break
            out.extend(self._to_repo_info(item) for item in raw)
            if len(raw) < int(params.get("per_page", "100")):
                break
            page += 1
        return out

    @staticmethod
    def _to_repo_info(raw: dict[str, Any]) -> RepoInfo:
        return RepoInfo(
            slug=raw["full_name"],
            clone_url=raw.get("ssh_url") or raw["clone_url"],
            web_url=raw["html_url"],
            default_branch=raw.get("default_branch") or "main",
            private=bool(raw.get("private", False)),
        )

    @property
    def host(self) -> str:
        """Public alias for the resolved host (used by tests + URL helpers)."""
        return self._host
