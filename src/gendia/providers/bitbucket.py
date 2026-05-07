"""Bitbucket Cloud provider.

API: https://developer.atlassian.com/cloud/bitbucket/rest/
Auth: an "App Password" sent via HTTP Basic auth, or a workspace
access token as Bearer. We support both.
"""

from __future__ import annotations

import base64
import urllib.parse
from typing import Any

from gendia.providers.base import GitProvider, ProviderError, RepoInfo


class BitbucketProvider(GitProvider):
    _API_ROOT = "https://api.bitbucket.org/2.0"

    @property
    def platform(self) -> str:
        return "bitbucket"

    def _auth_header(self) -> str:
        if not self._token:
            return ""
        # If the token is `username:app_password`, treat as Basic; else Bearer.
        if ":" in self._token:
            encoded = base64.b64encode(self._token.encode("utf-8")).decode("ascii")
            return f"Basic {encoded}"
        return f"Bearer {self._token}"

    # --- URL helpers ---------------------------------------------------------

    def clone_url(self, slug: str, *, ssh: bool = True) -> str:
        if ssh:
            return f"git@bitbucket.org:{slug}.git"
        return f"https://bitbucket.org/{slug}.git"

    def web_url(self, slug: str) -> str:
        return f"https://bitbucket.org/{slug}"

    # --- queries -------------------------------------------------------------

    def list_repos(self) -> list[RepoInfo]:
        workspace = self._account.workspace
        if not workspace:
            raise ProviderError("bitbucket: account has no workspace set")
        url = f"{self._API_ROOT}/repositories/{urllib.parse.quote(workspace)}?pagelen=100"
        return list(self._paginate(url))

    def get_repo(self, slug: str) -> RepoInfo:
        url = f"{self._API_ROOT}/repositories/{slug}"
        raw = self._request("GET", url)
        if not isinstance(raw, dict):
            raise ProviderError(f"bitbucket: unexpected response shape for {slug}")
        return self._to_repo_info(raw)

    # --- mutations -----------------------------------------------------------

    def create_repo(self, slug: str, *, private: bool = False, description: str = "") -> RepoInfo:
        url = f"{self._API_ROOT}/repositories/{slug}"
        body = {
            "scm": "git",
            "is_private": private,
            "description": description,
        }
        raw = self._request("POST", url, body=body, accept_codes=(200, 201))
        if not isinstance(raw, dict):
            raise ProviderError("bitbucket: create_repo returned unexpected shape")
        return self._to_repo_info(raw)

    def set_secret(self, slug: str, name: str, value: str) -> None:
        url = f"{self._API_ROOT}/repositories/{slug}/pipelines_config/variables/"
        self._request(
            "POST",
            url,
            body={"key": name, "value": value, "secured": True},
            accept_codes=(200, 201),
        )

    # --- internals -----------------------------------------------------------

    def _paginate(self, url: str | None) -> list[RepoInfo]:
        out: list[RepoInfo] = []
        next_url = url
        while next_url:
            raw = self._request("GET", next_url)
            if not isinstance(raw, dict):
                break
            for item in raw.get("values") or []:
                out.append(self._to_repo_info(item))
            next_url = raw.get("next")
        return out

    @staticmethod
    def _to_repo_info(raw: dict[str, Any]) -> RepoInfo:
        clone_url = ""
        for link in raw.get("links", {}).get("clone", []):
            if link.get("name") == "ssh":
                clone_url = link.get("href", "")
                break
        if not clone_url:
            for link in raw.get("links", {}).get("clone", []):
                if link.get("name") == "https":
                    clone_url = link.get("href", "")
                    break
        return RepoInfo(
            slug=raw["full_name"],
            clone_url=clone_url,
            web_url=raw.get("links", {}).get("html", {}).get("href", ""),
            default_branch=(raw.get("mainbranch") or {}).get("name") or "main",
            private=bool(raw.get("is_private", False)),
        )
