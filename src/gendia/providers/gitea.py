"""Gitea-compatible provider — covers Gitea, Forgejo, and Codeberg.

API: https://docs.gitea.com/api/1.20/  (Forgejo / Codeberg are wire-compatible)
Auth: PAT sent as `Authorization: token <token>`.

A single class handles all three because the wire shape is identical;
the only difference is the host. `Account.host` is required for self-hosted
instances (Forgejo / private Gitea); Codeberg uses `codeberg.org` as the
default. The `platform` attribute always reports "gitea" upstream — the
distinction between gitea / forgejo / codeberg is presentation, not API.
"""

from __future__ import annotations

import urllib.parse
from typing import Any

from gendia.providers.base import GitProvider, ProviderError, RepoInfo


class GiteaProvider(GitProvider):
    _DEFAULT_HOST = "codeberg.org"

    @property
    def platform(self) -> str:
        return "gitea"

    def _host(self) -> str:
        return self._account.host or self._DEFAULT_HOST

    def _api_root(self) -> str:
        return f"https://{self._host()}/api/v1"

    def _web_root(self) -> str:
        return f"https://{self._host()}"

    def _auth_header(self) -> str:
        return f"token {self._token}"

    # --- URL helpers ---------------------------------------------------------

    def clone_url(self, slug: str, *, ssh: bool = True) -> str:
        if ssh:
            return f"git@{self._host()}:{slug}.git"
        return f"{self._web_root()}/{slug}.git"

    def web_url(self, slug: str) -> str:
        return f"{self._web_root()}/{slug}"

    # --- queries -------------------------------------------------------------

    def list_repos(self) -> list[RepoInfo]:
        scope = self._account.scope
        if not scope:
            raise ProviderError("gitea: account has no org/username scope")

        # /orgs/{org}/repos works for orgs; /users/{user}/repos for personal.
        path = f"/orgs/{scope}/repos" if self._account.org else f"/users/{scope}/repos"
        return list(self._paginate(path, params={"limit": "50"}))

    def get_repo(self, slug: str) -> RepoInfo:
        url = f"{self._api_root()}/repos/{slug}"
        raw = self._request("GET", url)
        if not isinstance(raw, dict):
            raise ProviderError(f"gitea: unexpected response shape for {slug}")
        return self._to_repo_info(raw, host=self._host())

    # --- mutations -----------------------------------------------------------

    def create_repo(self, slug: str, *, private: bool = False, description: str = "") -> RepoInfo:
        owner, _, name = slug.partition("/")
        if not owner or not name:
            raise ProviderError(f"gitea: slug must be 'owner/repo', got {slug!r}")

        url = (
            f"{self._api_root()}/orgs/{owner}/repos"
            if self._account.org
            else f"{self._api_root()}/user/repos"
        )
        body = {"name": name, "private": private, "description": description}
        raw = self._request("POST", url, body=body, accept_codes=(201,))
        if not isinstance(raw, dict):
            raise ProviderError("gitea: create_repo returned unexpected shape")
        return self._to_repo_info(raw, host=self._host())

    # --- internals -----------------------------------------------------------

    def _paginate(self, path: str, *, params: dict[str, str]) -> list[RepoInfo]:
        out: list[RepoInfo] = []
        page = 1
        limit = int(params.get("limit", "50"))
        host = self._host()
        while True:
            qs = urllib.parse.urlencode({**params, "page": str(page)})
            url = f"{self._api_root()}{path}?{qs}"
            raw = self._request("GET", url)
            if not isinstance(raw, list) or not raw:
                break
            out.extend(self._to_repo_info(item, host=host) for item in raw)
            if len(raw) < limit:
                break
            page += 1
        return out

    @staticmethod
    def _to_repo_info(raw: dict[str, Any], *, host: str) -> RepoInfo:
        owner = raw.get("owner") or {}
        owner_name = owner.get("login") or owner.get("username") or ""
        name = raw.get("name") or ""
        slug = f"{owner_name}/{name}" if owner_name else (raw.get("full_name") or name)
        return RepoInfo(
            slug=slug,
            clone_url=raw.get("ssh_url") or raw.get("clone_url") or f"git@{host}:{slug}.git",
            web_url=raw.get("html_url") or f"https://{host}/{slug}",
            default_branch=raw.get("default_branch") or "main",
            private=bool(raw.get("private", False)),
        )
