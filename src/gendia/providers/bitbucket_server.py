"""Bitbucket Server / Data Center provider.

API: https://docs.atlassian.com/bitbucket-server/rest/  (path: `/rest/api/1.0`)
Auth: PAT or Basic; sent as `Authorization: Bearer <token>` for HTTP-token,
`Basic <b64>` when `username:password` is supplied via the credential.

Differs from Bitbucket Cloud (api.bitbucket.org) in URL shape and pagination.
Slugs are `<projectKey>/<repoSlug>`.
"""

from __future__ import annotations

import base64
import urllib.parse
from typing import Any

from gendia.providers.base import GitProvider, ProviderError, RepoInfo


class BitbucketServerProvider(GitProvider):
    @property
    def platform(self) -> str:
        return "bitbucket-server"

    def _host(self) -> str:
        if not self._account.host:
            raise ProviderError("bitbucket-server: account.host is required")
        return self._account.host

    def _api_root(self) -> str:
        return f"https://{self._host()}/rest/api/1.0"

    def _auth_header(self) -> str:
        if not self._token:
            return ""
        if ":" in self._token:
            encoded = base64.b64encode(self._token.encode("utf-8")).decode("ascii")
            return f"Basic {encoded}"
        return f"Bearer {self._token}"

    # --- URL helpers ---------------------------------------------------------

    def clone_url(self, slug: str, *, ssh: bool = True) -> str:
        project, _, repo = slug.partition("/")
        if ssh:
            return f"ssh://git@{self._host()}:7999/{project.lower()}/{repo}.git"
        return f"https://{self._host()}/scm/{project.lower()}/{repo}.git"

    def web_url(self, slug: str) -> str:
        project, _, repo = slug.partition("/")
        return f"https://{self._host()}/projects/{project}/repos/{repo}"

    # --- queries -------------------------------------------------------------

    def list_repos(self) -> list[RepoInfo]:
        scope = self._account.scope
        if scope:
            url = f"{self._api_root()}/projects/{urllib.parse.quote(scope)}/repos?limit=100"
            return list(self._paginate(url, project=scope))

        # No scope: walk all visible repos.
        url = f"{self._api_root()}/repos?limit=100"
        return list(self._paginate(url, project=None))

    def get_repo(self, slug: str) -> RepoInfo:
        project, _, repo = slug.partition("/")
        if not project or not repo:
            raise ProviderError(f"bitbucket-server: slug must be 'project/repo', got {slug!r}")
        url = f"{self._api_root()}/projects/{project}/repos/{repo}"
        raw = self._request("GET", url)
        if not isinstance(raw, dict):
            raise ProviderError(f"bitbucket-server: unexpected response shape for {slug}")
        return self._to_repo_info(raw, project=project)

    # --- internals -----------------------------------------------------------

    def _paginate(self, url: str, *, project: str | None) -> list[RepoInfo]:
        out: list[RepoInfo] = []
        next_url: str | None = url
        seen_offsets: set[int] = set()
        while next_url:
            raw = self._request("GET", next_url)
            if not isinstance(raw, dict):
                break
            for item in raw.get("values") or []:
                if isinstance(item, dict):
                    proj = project or (item.get("project") or {}).get("key", "")
                    out.append(self._to_repo_info(item, project=str(proj)))
            if raw.get("isLastPage", True):
                break
            next_offset = int(raw.get("nextPageStart") or 0)
            if next_offset in seen_offsets:
                break
            seen_offsets.add(next_offset)
            sep = "&" if "?" in url else "?"
            next_url = f"{url}{sep}start={next_offset}"
        return out

    def _to_repo_info(self, raw: dict[str, Any], *, project: str) -> RepoInfo:
        slug_name = raw.get("slug") or raw.get("name") or ""
        full_slug = f"{project}/{slug_name}" if project else slug_name
        clone_url = ""
        for link in (raw.get("links") or {}).get("clone") or []:
            if isinstance(link, dict) and link.get("name") == "ssh":
                clone_url = str(link.get("href", ""))
                break
        if not clone_url:
            for link in (raw.get("links") or {}).get("clone") or []:
                if isinstance(link, dict) and link.get("name") == "http":
                    clone_url = str(link.get("href", ""))
                    break
        web = (raw.get("links") or {}).get("self") or []
        web_url = (
            str(web[0].get("href")) if web and isinstance(web[0], dict) else self.web_url(full_slug)
        )
        return RepoInfo(
            slug=full_slug,
            clone_url=clone_url or self.clone_url(full_slug),
            web_url=web_url,
            default_branch="main",
            private=not bool(raw.get("public", False)),
        )
