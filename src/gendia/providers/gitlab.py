"""GitLab provider (gitlab.com or self-hosted).

API: https://docs.gitlab.com/ee/api/
Auth: Personal access token, sent as `Authorization: Bearer <token>`.
"""

from __future__ import annotations

import urllib.parse
from typing import Any

from gendia.providers.base import GitProvider, ProviderError, RepoInfo


class GitLabProvider(GitProvider):
    @property
    def platform(self) -> str:
        return "gitlab"

    def _api_root(self) -> str:
        host = self._account.host or "gitlab.com"
        return f"https://{host}/api/v4"

    def _web_root(self) -> str:
        host = self._account.host or "gitlab.com"
        return f"https://{host}"

    def _auth_header(self) -> str:
        return f"Bearer {self._token}"

    # --- URL helpers ---------------------------------------------------------

    def clone_url(self, slug: str, *, ssh: bool = True) -> str:
        host = self._account.host or "gitlab.com"
        if ssh:
            return f"git@{host}:{slug}.git"
        return f"{self._web_root()}/{slug}.git"

    def web_url(self, slug: str) -> str:
        return f"{self._web_root()}/{slug}"

    # --- queries -------------------------------------------------------------

    def list_repos(self) -> list[RepoInfo]:
        scope = self._account.group or self._account.username
        if not scope:
            raise ProviderError("gitlab: account has no group or username scope")

        if self._account.group:
            path = f"/groups/{urllib.parse.quote(scope, safe='')}/projects"
            params = {"per_page": "100", "include_subgroups": "true"}
        else:
            path = f"/users/{urllib.parse.quote(scope, safe='')}/projects"
            params = {"per_page": "100"}

        return list(self._paginate(path, params=params))

    def get_repo(self, slug: str) -> RepoInfo:
        url = f"{self._api_root()}/projects/{urllib.parse.quote(slug, safe='')}"
        raw = self._request("GET", url)
        if not isinstance(raw, dict):
            raise ProviderError(f"gitlab: unexpected response shape for {slug}")
        return self._to_repo_info(raw)

    # --- mutations -----------------------------------------------------------

    def create_repo(self, slug: str, *, private: bool = False, description: str = "") -> RepoInfo:
        owner, _, name = slug.partition("/")
        if not owner or not name:
            raise ProviderError(f"gitlab: slug must be 'namespace/project', got {slug!r}")

        # Resolve namespace_id from the namespace path.
        ns_url = f"{self._api_root()}/namespaces?search={urllib.parse.quote(owner)}"
        namespaces = self._request("GET", ns_url)
        if not isinstance(namespaces, list) or not namespaces:
            raise ProviderError(f"gitlab: namespace {owner!r} not found")
        # GitLab returns multiple matches on a search; pick the exact path match.
        match = next((ns for ns in namespaces if ns.get("full_path") == owner), namespaces[0])
        namespace_id = match["id"]

        body = {
            "name": name,
            "path": name,
            "namespace_id": namespace_id,
            "visibility": "private" if private else "public",
            "description": description,
        }
        raw = self._request("POST", f"{self._api_root()}/projects", body=body, accept_codes=(201,))
        if not isinstance(raw, dict):
            raise ProviderError("gitlab: create_repo returned unexpected shape")
        return self._to_repo_info(raw)

    def set_secret(self, slug: str, name: str, value: str) -> None:
        url = f"{self._api_root()}/projects/{urllib.parse.quote(slug, safe='')}/variables"
        self._request(
            "POST",
            url,
            body={"key": name, "value": value, "masked": True, "protected": False},
            accept_codes=(201,),
        )

    # --- internals -----------------------------------------------------------

    def _paginate(self, path: str, *, params: dict[str, str]) -> list[RepoInfo]:
        out: list[RepoInfo] = []
        page = 1
        per_page = int(params.get("per_page", "100"))
        while True:
            qs = urllib.parse.urlencode({**params, "page": str(page)})
            url = f"{self._api_root()}{path}?{qs}"
            raw = self._request("GET", url)
            if not isinstance(raw, list) or not raw:
                break
            out.extend(self._to_repo_info(item) for item in raw)
            if len(raw) < per_page:
                break
            page += 1
        return out

    @staticmethod
    def _to_repo_info(raw: dict[str, Any]) -> RepoInfo:
        return RepoInfo(
            slug=raw["path_with_namespace"],
            clone_url=raw.get("ssh_url_to_repo") or raw["http_url_to_repo"],
            web_url=raw["web_url"],
            default_branch=raw.get("default_branch") or "main",
            private=raw.get("visibility") == "private",
        )
