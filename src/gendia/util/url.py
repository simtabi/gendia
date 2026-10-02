"""Git URL parsing + SSH/HTTPS conversion.

Three URL shapes we recognize:

  1. SCP-shorthand SSH      `git@github.com:owner/repo.git`
  2. ssh:// canonical SSH   `ssh://git@host:2222/owner/repo.git`
  3. HTTPS                  `https://host/owner/repo.git`

The conversion functions are pure; pass in an `SSHConfig` if you need
alias resolution (the SSH host alias may differ from the canonical
hostname). `insteadOf` rules from `~/.gitconfig` can be honored via
`load_insteadof_rules()`.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from gendia.ssh.config import SSH_DEFAULT_PORT, SSHConfig

_SCP_RE = re.compile(r"^(?P<user>[^@]+)@(?P<host>[^:/]+):(?P<path>[^:].*)$")
_SSH_RE = re.compile(
    r"^ssh://(?:(?P<user>[^@]+)@)?(?P<host>[^:/]+)(?::(?P<port>\d+))?/(?P<path>.+)$"
)
_HTTPS_RE = re.compile(r"^https?://(?:(?P<user>[^@]+)@)?(?P<host>[^/]+)/(?P<path>.+)$")


@dataclass(frozen=True, slots=True)
class GitURL:
    """A parsed git URL."""

    scheme: str  # "ssh" | "https"
    user: str  # always populated; defaults to "git" for SSH, "" for HTTPS
    host: str
    port: int | None
    path: str  # everything after host[:port]/, no leading slash, no trailing .git

    @property
    def slug(self) -> str:
        """`owner/repo` form, regardless of scheme."""
        return self.path

    def as_ssh(self) -> str:
        """Render as the SCP-shorthand SSH form (or ssh:// when port != default)."""
        if self.port and self.port != SSH_DEFAULT_PORT:
            return f"ssh://{self.user or 'git'}@{self.host}:{self.port}/{self.path}.git"
        return f"{self.user or 'git'}@{self.host}:{self.path}.git"

    def as_https(self) -> str:
        return f"https://{self.host}/{self.path}.git"


# --- parsing ---------------------------------------------------------------


def parse(url: str) -> GitURL:
    """Parse a git URL into a `GitURL`. Raises ValueError on malformed input."""
    url = url.strip()

    # ssh:// canonical form
    match = _SSH_RE.match(url)
    if match:
        return GitURL(
            scheme="ssh",
            user=match.group("user") or "git",
            host=match.group("host"),
            port=int(match.group("port")) if match.group("port") else None,
            path=_strip_git_suffix(match.group("path")),
        )

    # https
    match = _HTTPS_RE.match(url)
    if match:
        return GitURL(
            scheme="https",
            user=match.group("user") or "",
            host=match.group("host"),
            port=None,
            path=_strip_git_suffix(match.group("path")),
        )

    # SCP shorthand. NOTE: this regex is last because `ssh://...` would
    # otherwise also match the `[^@]+@...` prefix.
    match = _SCP_RE.match(url)
    if match and "/" not in match.group("user"):  # avoid swallowing `https://...`
        return GitURL(
            scheme="ssh",
            user=match.group("user"),
            host=match.group("host"),
            port=None,
            path=_strip_git_suffix(match.group("path")),
        )

    raise ValueError(f"unrecognized git URL: {url!r}")


# --- conversion -------------------------------------------------------------


def to_ssh(
    url: str,
    *,
    ssh_config: SSHConfig | None = None,
    insteadof: dict[str, str] | None = None,
) -> str:
    """Return the SSH form of `url`. No-op if already SSH.

    `ssh_config` is consulted for alias resolution: if the resulting SSH
    host has a matching `Host` entry, the alias is used (preserves the
    user's existing setup).
    """
    parsed = parse(_apply_insteadof(url, insteadof))
    if parsed.scheme == "ssh":
        return parsed.as_ssh()
    new_host = _resolve_alias(parsed.host, ssh_config)
    return GitURL(
        scheme="ssh",
        user="git",
        host=new_host,
        port=None,
        path=parsed.path,
    ).as_ssh()


def to_https(
    url: str,
    *,
    ssh_config: SSHConfig | None = None,
    insteadof: dict[str, str] | None = None,
) -> str:
    """Return the HTTPS form of `url`. No-op if already HTTPS.

    `ssh_config` resolves SSH aliases (e.g. `github-personal`) back to the
    canonical hostname (`github.com`). Without it we keep the alias and the
    URL won't work over HTTPS — caller's responsibility to pass the config
    when alias remapping is expected.
    """
    parsed = parse(_apply_insteadof(url, insteadof))
    if parsed.scheme == "https":
        return parsed.as_https()
    canonical = _canonical_host(parsed.host, ssh_config)
    return GitURL(
        scheme="https",
        user="",
        host=canonical,
        port=None,
        path=parsed.path,
    ).as_https()


# --- insteadOf rules --------------------------------------------------------


def load_insteadof_rules(*, gitconfig_path: Path | None = None) -> dict[str, str]:
    """Read `url.<base>.insteadOf` from git config. Returns {pattern: replacement}.

    Uses `git config --get-regexp` so it honours system / global / local
    layers exactly as git would. Returns {} if git isn't available.
    """
    args = ["git"]
    if gitconfig_path is not None:
        args += ["-c", f"include.path={gitconfig_path}"]
    args += ["config", "--global", "--get-regexp", r"^url\..*\.insteadof$"]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, check=False, timeout=5.0)
    except (OSError, subprocess.SubprocessError):
        return {}
    if proc.returncode != 0:
        return {}

    rules: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        # `url.<replacement>.insteadof <pattern>`
        prefix, _, pattern = line.partition(" ")
        if not prefix.startswith("url.") or not prefix.endswith(".insteadof"):
            continue
        replacement = prefix[len("url.") : -len(".insteadof")]
        if pattern:
            rules[pattern] = replacement
    return rules


def _apply_insteadof(url: str, rules: dict[str, str] | None) -> str:
    if not rules:
        return url
    # Longest pattern wins (git's behaviour).
    for pattern in sorted(rules, key=len, reverse=True):
        if url.startswith(pattern):
            return rules[pattern] + url[len(pattern) :]
    return url


# --- helpers ----------------------------------------------------------------


def _strip_git_suffix(path: str) -> str:
    return path[:-4] if path.endswith(".git") else path


def _resolve_alias(canonical_host: str, ssh_config: SSHConfig | None) -> str:
    """If an SSH `Host` entry maps to `canonical_host`, prefer the alias."""
    if ssh_config is None:
        return canonical_host
    for host in ssh_config.hosts():
        if host.hostname == canonical_host and host.alias != canonical_host:
            return host.alias
    return canonical_host


def _canonical_host(possibly_alias: str, ssh_config: SSHConfig | None) -> str:
    """If `possibly_alias` is an SSH alias, return its `HostName`; else passthrough."""
    if ssh_config is None:
        return possibly_alias
    match = ssh_config.hosts().by_alias(possibly_alias)
    if match is not None and match.hostname:
        return match.hostname
    return possibly_alias


def convert_many(
    urls: Iterable[str],
    *,
    target: str,
    ssh_config: SSHConfig | None = None,
    insteadof: dict[str, str] | None = None,
) -> list[tuple[str, str]]:
    """Convert each URL to `target` ("ssh" or "https"); return (input, output) pairs."""
    out: list[tuple[str, str]] = []
    for raw in urls:
        url = raw.strip()
        if not url:
            continue
        if target == "ssh":
            out.append((url, to_ssh(url, ssh_config=ssh_config, insteadof=insteadof)))
        elif target == "https":
            out.append((url, to_https(url, ssh_config=ssh_config, insteadof=insteadof)))
        else:
            raise ValueError(f"unknown target: {target!r}")
    return out
