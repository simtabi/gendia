"""Frozen dataclass schemas for gendia configuration.

The config layers in two levels:
  1. Machine-wide accounts + registries  (`~/.config/gendia/gendia.json`)
  2. Per-project repo definitions         (`./gendia.json` in the project root)

Each schema validates its own invariants on construction. Loaders translate
JSON dicts into these objects via `from_dict`; raw dicts never leak past the
loader.

Path-shaped fields (`Project.root`, `RepoSpec.cleanup_globs` items) support
`~` and `${VAR}` expansion at load time, so config files can stay free of
machine-specific paths. Real-world example:

    "root": "${MYORG_ROOT}"
    "root": "~/projects/myorg"
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Self


class ConfigError(ValueError):
    """Raised when a config file is malformed or violates a schema invariant."""


_VAR_PATTERN = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")


def expand(text: str, *, context: str = "config") -> str:
    """Expand `~` and `${VAR}` references in a config string.

    Differences from `os.path.expandvars`:
      - Raises `ConfigError` (not silent passthrough) when a referenced env
        var is unset, with a clear message that names both the variable and
        the calling site (`context`). This catches typos that would otherwise
        produce nonsensical paths like `/Users/foo/${MISSING_VAR}/...`.
    """

    def _resolve(match: re.Match[str]) -> str:
        name = match.group(1)
        value = os.environ.get(name)
        if value is None:
            raise ConfigError(
                f"{context}: environment variable ${{{name}}} is referenced but not set"
            )
        return value

    expanded_vars = _VAR_PATTERN.sub(_resolve, text)
    return os.path.expanduser(expanded_vars)


# --- Accounts ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GitIdentity:
    """Per-account git identity used for commits + signing inside its repos.

    Solves the multi-account leak problem: when you have a personal GitHub,
    a work GitHub org, and a Bitbucket workspace, the global `~/.gitconfig`
    can only spell one identity. This block lets each account declare its
    own, and `gendia identity apply` writes the right `.git/config` into
    every tracked repo so committing under the wrong identity is impossible.

    Fields are all optional; whatever you set is what gets applied. Leaving
    a field unset means "don't touch what's already configured".

    Example covering the GitHub email-privacy gotcha (use the no-reply form
    so commits never expose your real email):

        "git_identity": {
            "name":  "Your Name",
            "email": "<numeric-id>+<username>@users.noreply.github.com",
            "ssh_alias":   "github-personal",
            "signing_key": "~/.ssh/id_ed25519_personal.pub"
        }
    """

    name: str | None = None
    email: str | None = None
    signing_key: str | None = None  # path to .pub key (SSH-signed) or GPG key id
    ssh_alias: str | None = None  # Host entry from ~/.ssh/config
    sign_commits: bool = False
    sign_tags: bool = False

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> Self:
        if not payload:
            return cls()
        return cls(
            name=payload.get("name"),
            email=payload.get("email"),
            signing_key=payload.get("signing_key"),
            ssh_alias=payload.get("ssh_alias"),
            sign_commits=bool(payload.get("sign_commits", False)),
            sign_tags=bool(payload.get("sign_tags", False)),
        )

    def is_empty(self) -> bool:
        return not any((self.name, self.email, self.signing_key, self.ssh_alias))


@dataclass(frozen=True, slots=True)
class SyncPolicy:
    """Per-account include/exclude rules for what counts as in-scope.

    Three modes:

      "explicit"  -> only repos listed in `project.repos` are in scope. The
                     `include`/`exclude` globs are ignored. This is the
                     default and matches the original gendia behaviour.

      "all"       -> every repo returned by the provider's `list_repos()`
                     is in scope (subject to `include_archived` /
                     `include_forks` / `include_private` flags). The
                     `exclude` globs still apply as a deny-list.

      "patterns"  -> only repos whose slug matches one of the `include`
                     globs are in scope, minus any matching `exclude`.

    Glob syntax is shell-style (`fnmatch`): `*` matches any chars except `/`,
    `?` matches one char, `[abc]` matches a class. Patterns are matched
    against the *repo name* (after `<scope>/`), not the full slug.

    The categoriser used by `gendia inventory` reports four states:

      tracked-synced   in scope, in project.repos, last sync ok
      tracked-pending  in scope, in project.repos, never synced or last failed
      tracked-missing  in project.repos, but the remote no longer has it
      discoverable     in scope, on remote, NOT in project.repos
      ignored          on remote but excluded by this policy
    """

    mode: str = "explicit"
    include: tuple[str, ...] = field(default_factory=tuple)
    exclude: tuple[str, ...] = field(default_factory=tuple)
    include_archived: bool = False
    include_forks: bool = False
    include_private: bool = True

    _ALLOWED_MODES = frozenset({"explicit", "all", "patterns"})

    def __post_init__(self) -> None:
        if self.mode not in self._ALLOWED_MODES:
            raise ConfigError(
                f"sync_policy.mode must be one of {sorted(self._ALLOWED_MODES)}, got {self.mode!r}"
            )
        if self.mode == "patterns" and not self.include:
            raise ConfigError("sync_policy.mode='patterns' requires a non-empty `include` list")

    def is_in_scope(  # noqa: PLR0911 — short-circuit each filter in priority order
        self,
        *,
        name: str,
        archived: bool,
        fork: bool,
        private: bool,
    ) -> bool:
        """Return True iff a remote repo should be considered in-scope."""
        if archived and not self.include_archived:
            return False
        if fork and not self.include_forks:
            return False
        if private and not self.include_private:
            return False
        if self.exclude and any(fnmatch(name, pat) for pat in self.exclude):
            return False
        if self.mode == "all":
            return True
        if self.mode == "patterns":
            return any(fnmatch(name, pat) for pat in self.include)
        # explicit -> the inventory step decides via the project.repos list,
        # not via this method. Treat as always-in-scope so it's the project
        # list that governs.
        return True

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> Self:
        if not payload:
            return cls()
        return cls(
            mode=str(payload.get("mode", "explicit")).lower(),
            include=tuple(payload.get("include") or ()),
            exclude=tuple(payload.get("exclude") or ()),
            include_archived=bool(payload.get("include_archived", False)),
            include_forks=bool(payload.get("include_forks", False)),
            include_private=bool(payload.get("include_private", True)),
        )


@dataclass(frozen=True, slots=True)
class Account:
    """A credential-bearing identity on one of the supported VCS platforms.

    Two distinct kinds of credentials apply:

      - `credential_ref`  -> API token used for HTTPS REST calls
        (GitHub / GitLab / Bitbucket REST APIs, plus Packagist update).
      - `ssh_key_path`    -> private key used for git-over-SSH operations
        (push, fetch, clone). When unset, the user's default SSH agent and
        ~/.ssh/config apply, which is the right answer for dev boxes.

    `git_auth` controls which transport git operations use:

      - "ssh"   (default) -> rely on SSH key / agent / ~/.ssh/config
      - "https" -> inject the API token as the password via GIT_ASKPASS
                   (useful in containers without an SSH agent)
      - "auto"  -> ssh when ssh_key_path is set or `~/.ssh` is populated,
                   else https-with-token

    The secrets themselves never live in the config file; only the env-var
    name does. `ssh_key_path` may use `~` and `${VAR}` expansion.
    """

    name: str
    platform: str  # "github" | "gitlab" | "bitbucket"
    credential_ref: str
    org: str | None = None
    workspace: str | None = None  # bitbucket terminology
    group: str | None = None  # gitlab terminology
    username: str | None = None
    host: str | None = None  # for self-hosted gitlab/bitbucket-server
    ssh_key_path: str | None = None
    ssh_known_hosts_strict: bool = True
    git_auth: str = "ssh"
    sync_policy: SyncPolicy = field(default_factory=SyncPolicy)
    git_identity: GitIdentity = field(default_factory=GitIdentity)

    _ALLOWED_PLATFORMS = frozenset({"github", "gitlab", "bitbucket"})
    _ALLOWED_GIT_AUTH = frozenset({"ssh", "https", "auto"})

    def __post_init__(self) -> None:
        if self.platform not in self._ALLOWED_PLATFORMS:
            raise ConfigError(
                f"account {self.name!r}: unsupported platform {self.platform!r}; "
                f"expected one of {sorted(self._ALLOWED_PLATFORMS)}"
            )
        if self.git_auth not in self._ALLOWED_GIT_AUTH:
            raise ConfigError(
                f"account {self.name!r}: git_auth must be one of "
                f"{sorted(self._ALLOWED_GIT_AUTH)}, got {self.git_auth!r}"
            )
        if not self.credential_ref:
            raise ConfigError(f"account {self.name!r}: credential_ref is required")
        if not any((self.org, self.workspace, self.group, self.username)):
            raise ConfigError(
                f"account {self.name!r}: must set one of org / workspace / group / username"
            )

    @property
    def scope(self) -> str:
        """The platform-specific identifier for the owning scope."""
        return self.org or self.workspace or self.group or self.username or ""

    @classmethod
    def from_dict(cls, name: str, payload: dict[str, Any]) -> Self:
        return cls(
            name=name,
            platform=str(payload.get("platform", "")).lower(),
            credential_ref=str(payload.get("credential_ref", "")),
            org=payload.get("org"),
            workspace=payload.get("workspace"),
            group=payload.get("group"),
            username=payload.get("username"),
            host=payload.get("host"),
            ssh_key_path=payload.get("ssh_key_path"),
            ssh_known_hosts_strict=bool(payload.get("ssh_known_hosts_strict", True)),
            git_auth=str(payload.get("git_auth", "ssh")).lower(),
            sync_policy=SyncPolicy.from_dict(payload.get("sync_policy")),
            git_identity=GitIdentity.from_dict(payload.get("git_identity")),
        )


# --- Registries --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Registry:
    """A package registry that mirrors VCS state (Packagist, npm, PyPI)."""

    name: str
    kind: str  # "packagist" | "npm" | "pypi"
    credential_ref: str | None = None
    username: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    _ALLOWED_KINDS = frozenset({"packagist", "npm", "pypi"})

    def __post_init__(self) -> None:
        if self.kind not in self._ALLOWED_KINDS:
            raise ConfigError(
                f"registry {self.name!r}: unsupported kind {self.kind!r}; "
                f"expected one of {sorted(self._ALLOWED_KINDS)}"
            )

    @classmethod
    def from_dict(cls, name: str, payload: dict[str, Any]) -> Self:
        # Allow the dict key (`name`) to imply the kind when not given.
        kind = str(payload.get("kind", name)).lower()
        return cls(
            name=name,
            kind=kind,
            credential_ref=payload.get("credential_ref"),
            username=payload.get("username"),
            extra={
                k: v for k, v in payload.items() if k not in {"kind", "credential_ref", "username"}
            },
        )


# --- Repos -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RepoSpec:
    """One repository under a project."""

    dir: str
    package: str | None = None
    verify: tuple[str, ...] = field(default_factory=tuple)
    cleanup_globs: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.dir:
            raise ConfigError("repo: dir is required")

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Self:
        verify = payload.get("verify") or []
        cleanup = payload.get("cleanup_globs") or []
        return cls(
            dir=str(payload["dir"]),
            package=payload.get("package"),
            verify=tuple(verify),
            cleanup_globs=tuple(cleanup),
        )


# --- Project -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Project:
    """A bundle of repos sharing one account + (optional) one registry."""

    name: str
    account: str
    root: Path
    repos: tuple[RepoSpec, ...]
    registry: str | None = None
    cleanup_globs: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.name:
            raise ConfigError("project: name is required")
        if not self.repos:
            raise ConfigError(f"project {self.name!r}: repos[] cannot be empty")

    def repo(self, name: str) -> RepoSpec:
        """Look up a repo by its directory name. Raises if absent."""
        for r in self.repos:
            if r.dir == name:
                return r
        raise ConfigError(f"project {self.name!r}: no repo named {name!r}")

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Self:
        repos = payload.get("repos") or []
        if not repos:
            raise ConfigError("project: repos[] is required and non-empty")
        name = str(payload["name"])
        return cls(
            name=name,
            account=str(payload["account"]),
            root=Path(expand(str(payload.get("root") or "."), context=f"project {name!r}.root")),
            registry=payload.get("registry"),
            repos=tuple(RepoSpec.from_dict(r) for r in repos),
            cleanup_globs=tuple(payload.get("cleanup_globs") or ()),
        )


# --- Top-level config --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Defaults:
    concurrency: int = 4
    dry_run: bool = False
    log_level: str = "info"
    log_format: str = "human"

    def __post_init__(self) -> None:
        if self.concurrency < 1:
            raise ConfigError("defaults.concurrency must be >= 1")


@dataclass(frozen=True, slots=True)
class GendiaConfig:
    """The fully-merged view exposed to the rest of the program."""

    accounts: dict[str, Account]
    registries: dict[str, Registry]
    project: Project | None
    defaults: Defaults

    def account(self, name: str) -> Account:
        if name not in self.accounts:
            raise ConfigError(f"unknown account: {name!r}")
        return self.accounts[name]

    def registry(self, name: str) -> Registry:
        if name not in self.registries:
            raise ConfigError(f"unknown registry: {name!r}")
        return self.registries[name]
