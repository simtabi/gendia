"""Forge registry — map an SSH host to a known git platform.

The registry is JSON-driven so the catalogue of supported forges is data,
not code. Adding a self-hosted GitLab variant or a fork of Gitea costs an
edit to `forges.json`, not a code change.

Detection signals, in priority order:

  1. Exact hostname match  (e.g. `github.com` => `github`)
  2. Hostname substring    (e.g. `gitlab.example.com` => `gitlab`)
  3. SSH config comment    (e.g. `# Work GitHub Enterprise`)
  4. SSH `User` field hint (e.g. `User = git` on a non-public host)

The bundled registry lives at `gendia/data/forges.json` and is loaded via
importlib.resources so it works after `pip install` and during local dev.
A user-supplied override path (set via `gendia.json`'s `forges_file` key)
replaces the bundled registry entirely.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from gendia.observability.logger import get_logger

_log = get_logger("ssh.forges")


# --- registry ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ForgeRegistry:
    """Loaded forge catalogue. Pure data; no I/O after construction."""

    known_forges_exact: dict[str, str] = field(default_factory=dict)
    known_forges_patterns: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    public_forges: frozenset[str] = field(default_factory=frozenset)
    comment_keywords: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    tlds: frozenset[str] = field(default_factory=frozenset)

    # Map registry-platform-id -> gendia provider platform string.
    # Keys we don't know are dropped silently when projecting onto an
    # `Account.platform`; callers can still see the registry id.
    _PROVIDER_ALIASES = {
        "github": "github",
        "github-enterprise": "github",
        "gitlab": "gitlab",
        "bitbucket": "bitbucket",
        "bitbucket-server": "bitbucket-server",
        "gitea": "gitea",
        "forgejo": "gitea",
        "codeberg": "gitea",
        "azure-devops": "azure",
    }

    def detect(
        self,
        *,
        hostname: str,
        alias: str = "",
        comment: str = "",
        user: str = "",
    ) -> str | None:
        """Return the registry's id (e.g. `github-enterprise`) or None."""
        # 1. Exact match wins.
        if hostname in self.known_forges_exact:
            return self.known_forges_exact[hostname]
        if alias and alias in self.known_forges_exact:
            return self.known_forges_exact[alias]

        # 2. Hostname substring patterns.
        haystack = hostname.lower()
        for pattern, platform in self.known_forges_patterns:
            if pattern in haystack:
                return platform

        # 3. Comment hints (highest signal among the heuristics).
        comment_platform = self._detect_from_comment(comment)
        if comment_platform is not None:
            return comment_platform

        # 4. `User git` heuristic — assume custom self-hosted git forge.
        if user and "git" in user.lower():
            return "custom"

        return None

    def provider_platform(self, registry_id: str | None) -> str | None:
        """Project a registry id onto a gendia `Account.platform` value."""
        if registry_id is None:
            return None
        return self._PROVIDER_ALIASES.get(registry_id)

    def derive_label(
        self,
        *,
        hostname: str,
        identity_file: str | None = None,
    ) -> str:
        """Pick a short label for an account (used as account name + token suffix).

        Public forges (github.com, gitlab.com, ...) get the parent dir of the
        IdentityFile because the hostname is generic. Self-hosted forges get
        the most-significant chunk of the hostname (excluding TLDs).
        """
        if hostname in self.public_forges and identity_file:
            label = _label_from_identity_file(identity_file)
            if label:
                return label

        return self._label_from_hostname(hostname)

    def _label_from_hostname(self, hostname: str) -> str:
        parts = hostname.lower().split(".")
        meaningful = [p for p in parts if p and p not in self.tlds]
        if meaningful:
            return meaningful[-1]
        return parts[0] if parts else hostname

    def _detect_from_comment(self, comment: str) -> str | None:
        if not comment:
            return None
        lower = comment.lower()
        # Iteration order matters: more specific keywords come first in the
        # registry (e.g. "github enterprise" before "github").
        for keyword, platform in self.comment_keywords:
            if keyword in lower:
                return platform
        return None


# --- loaders ----------------------------------------------------------------


_PAIR_LEN = 2  # JSON pairs in known_forges_patterns / comment_keywords are [string, string]

_FALLBACK_RAW: dict[str, Any] = {
    "known_forges_exact": {
        "github.com": "github",
        "gitlab.com": "gitlab",
        "bitbucket.org": "bitbucket",
        "codeberg.org": "codeberg",
        "gitea.com": "gitea",
        "ssh.dev.azure.com": "azure-devops",
        "vs-ssh.visualstudio.com": "azure-devops",
    },
    "known_forges_patterns": [
        ["github", "github-enterprise"],
        ["gitlab", "gitlab"],
        ["forgejo", "forgejo"],
        ["gitea", "gitea"],
        ["bitbucket", "bitbucket-server"],
        ["gogs", "gitea"],
    ],
    "public_forges": [
        "github.com",
        "gitlab.com",
        "bitbucket.org",
        "codeberg.org",
        "gitea.com",
    ],
    "comment_keywords": [
        ["github enterprise", "github-enterprise"],
        ["github", "github"],
        ["gitlab", "gitlab"],
        ["bitbucket server", "bitbucket-server"],
        ["bitbucket", "bitbucket"],
        ["forgejo", "forgejo"],
        ["codeberg", "codeberg"],
        ["gitea", "gitea"],
        ["azure devops", "azure-devops"],
        ["gogs", "gitea"],
    ],
    "tlds": ["com", "org", "net", "edu", "io", "dev", "co", "uk", "us", "ca", "de", "fr"],
}


def builtin_registry() -> ForgeRegistry:
    """Return the fallback registry shipped in code (used when JSON load fails)."""
    return _build_registry(_FALLBACK_RAW)


def load_registry(path: Path | str | None = None) -> ForgeRegistry:
    """Load the bundled `data/forges.json`, or a user-supplied override.

    `path` wins when provided. The bundled file is read via
    importlib.resources so it works after `pip install` without on-disk paths.
    """
    if path is not None:
        return _load_from_disk(Path(path))

    try:
        text = resources.files("gendia").joinpath("data/forges.json").read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError, OSError) as exc:
        _log.warning("bundled forges.json unreadable", extra={"error": str(exc)})
        return builtin_registry()

    return _parse_registry_text(text, source="bundled gendia/data/forges.json")


def _load_from_disk(path: Path) -> ForgeRegistry:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        _log.warning("forges_file unreadable; using bundled registry", extra={"error": str(exc)})
        return load_registry()
    return _parse_registry_text(text, source=str(path))


def _parse_registry_text(text: str, *, source: str) -> ForgeRegistry:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        _log.warning(
            "forges registry has invalid JSON",
            extra={"source": source, "error": str(exc)},
        )
        return builtin_registry()
    if not isinstance(raw, dict):
        _log.warning("forges registry root must be a JSON object", extra={"source": source})
        return builtin_registry()
    return _build_registry(raw)


def _build_registry(raw: dict[str, Any]) -> ForgeRegistry:
    exact = raw.get("known_forges_exact") or {}
    patterns = raw.get("known_forges_patterns") or []
    public = raw.get("public_forges") or []
    keywords = raw.get("comment_keywords") or []
    tlds = raw.get("tlds") or []

    return ForgeRegistry(
        known_forges_exact={
            str(k): str(v) for k, v in exact.items() if isinstance(k, str) and isinstance(v, str)
        },
        known_forges_patterns=tuple(
            (str(item[0]), str(item[1]))
            for item in patterns
            if isinstance(item, list) and len(item) == _PAIR_LEN
        ),
        public_forges=frozenset(str(x) for x in public if isinstance(x, str)),
        comment_keywords=tuple(
            (str(item[0]).lower(), str(item[1]))
            for item in keywords
            if isinstance(item, list) and len(item) == _PAIR_LEN
        ),
        tlds=frozenset(str(x) for x in tlds if isinstance(x, str)),
    )


# --- helpers ----------------------------------------------------------------


_LABEL_BLOCKLIST = {"ssh", ".ssh", "keys", "sandbox", ""}


def _label_from_identity_file(identity_file: str) -> str | None:
    try:
        path = Path(identity_file).expanduser()
    except (RuntimeError, ValueError):
        return None
    parent = path.parent.name
    if parent and parent not in _LABEL_BLOCKLIST:
        return parent.lower()
    grandparent = path.parent.parent.name if path.parent.parent != path.parent else ""
    if grandparent and grandparent not in _LABEL_BLOCKLIST:
        return grandparent.lower()
    return None
