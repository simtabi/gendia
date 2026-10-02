"""Azure DevOps provider.

API: https://learn.microsoft.com/en-us/rest/api/azure/devops/
Auth: Personal Access Token over HTTP Basic, with empty username and the
token as the password. We send `Authorization: Basic <b64(":<token>")>`.

`Account.org` is the Azure DevOps organization. For repo discovery the
project is required: we read it from `Account.workspace` (Azure terminology
is "project") or, if absent, treat the org as a single-project default.
"""

from __future__ import annotations

import base64
import urllib.parse
from typing import Any

from gendia.providers.base import GitProvider, ProviderError, RepoInfo


class AzureDevOpsProvider(GitProvider):
    _API_HOST = "dev.azure.com"
    _SSH_HOST = "ssh.dev.azure.com"

    @property
    def platform(self) -> str:
        return "azure"

    def _auth_header(self) -> str:
        # Azure DevOps PAT: empty username, token as password, base64(`:token`).
        if not self._token:
            return ""
        encoded = base64.b64encode(f":{self._token}".encode()).decode("ascii")
        return f"Basic {encoded}"

    def _api_root(self) -> str:
        org = self._account.org
        if not org:
            raise ProviderError("azure: account.org is required")
        return f"https://{self._API_HOST}/{urllib.parse.quote(org)}"

    def _web_root(self) -> str:
        org = self._account.org
        if not org:
            raise ProviderError("azure: account.org is required")
        return f"https://{self._API_HOST}/{urllib.parse.quote(org)}"

    # --- URL helpers ---------------------------------------------------------

    def clone_url(self, slug: str, *, ssh: bool = True) -> str:
        # Azure slugs in gendia carry "project/repo".
        org = self._account.org or ""
        if ssh:
            return f"git@{self._SSH_HOST}:v3/{org}/{slug}"
        # The HTTPS clone URL embeds the org and project: org@dev.azure.com/.../_git/repo
        project, _, repo = slug.partition("/")
        return f"https://{self._API_HOST}/{org}/{project}/_git/{repo}"

    def web_url(self, slug: str) -> str:
        org = self._account.org or ""
        project, _, repo = slug.partition("/")
        return f"https://{self._API_HOST}/{org}/{project}/_git/{repo}"

    # --- queries -------------------------------------------------------------

    def list_repos(self) -> list[RepoInfo]:
        # If a project is set (account.workspace), list under that project.
        # Otherwise enumerate projects and walk each one.
        project = self._account.workspace
        if project:
            return list(self._list_in_project(project))

        projects = self._list_projects()
        out: list[RepoInfo] = []
        for proj in projects:
            out.extend(self._list_in_project(proj))
        return out

    def get_repo(self, slug: str) -> RepoInfo:
        project, _, repo = slug.partition("/")
        if not project or not repo:
            raise ProviderError(f"azure: slug must be 'project/repo', got {slug!r}")
        url = (
            f"{self._api_root()}/{urllib.parse.quote(project)}/_apis/git/repositories/"
            f"{urllib.parse.quote(repo)}?api-version=7.1"
        )
        raw = self._request("GET", url)
        if not isinstance(raw, dict):
            raise ProviderError(f"azure: unexpected response shape for {slug}")
        return self._to_repo_info(raw, project=project)

    # --- internals -----------------------------------------------------------

    def _list_projects(self) -> list[str]:
        url = f"{self._api_root()}/_apis/projects?api-version=7.1"
        raw = self._request("GET", url)
        if not isinstance(raw, dict):
            raise ProviderError("azure: unexpected response shape for projects list")
        values = raw.get("value") or []
        return [str(p["name"]) for p in values if isinstance(p, dict) and p.get("name")]

    def _list_in_project(self, project: str) -> list[RepoInfo]:
        url = (
            f"{self._api_root()}/{urllib.parse.quote(project)}/_apis/git/repositories"
            f"?api-version=7.1"
        )
        raw = self._request("GET", url)
        if not isinstance(raw, dict):
            return []
        out: list[RepoInfo] = []
        for item in raw.get("value") or []:
            if isinstance(item, dict):
                out.append(self._to_repo_info(item, project=project))
        return out

    def _to_repo_info(self, raw: dict[str, Any], *, project: str) -> RepoInfo:
        name = raw.get("name") or ""
        slug = f"{project}/{name}"
        ssh_url = raw.get("sshUrl") or f"git@{self._SSH_HOST}:v3/{self._account.org}/{slug}"
        web_url = raw.get("webUrl") or self.web_url(slug)
        default_branch = (raw.get("defaultBranch") or "refs/heads/main").rsplit("/", 1)[-1]
        return RepoInfo(
            slug=slug,
            clone_url=ssh_url,
            web_url=web_url,
            default_branch=default_branch,
            private=True,  # Azure DevOps repos are private by default
        )
