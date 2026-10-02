"""SSH config parser.

Reads `~/.ssh/config` (or any path passed in) into a list of `SSHHost`
records. Handles:

  - `Host alias [alias2 ...]` blocks (multi-host lines)
  - `Match` blocks (treated like `Host` for our purposes — we capture the
    settings but do not evaluate the conditional)
  - `Include` directives (recursively follows globs; loops are guarded
    against by tracking visited paths)
  - Comments preceding a Host block (we capture the immediately-prior
    `# ...` line as `comment`, used by the forge registry's hint matcher)
  - Continuation across whitespace; values may be quoted

Stdlib only. Tolerant of malformed lines (skipped, never raised).
"""

from __future__ import annotations

import contextlib
import glob as _glob
import re
import shlex
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

# Keys we care about. Anything else is captured into `extras` for callers
# that need it (the inspector reads `IdentitiesOnly`, etc.).
SSH_DEFAULT_PORT = 22  # exported for callers that decide whether to emit `-p`

_KNOWN_KEYS = {
    "hostname",
    "user",
    "port",
    "identityfile",
    "identitiesonly",
    "proxyjump",
    "proxycommand",
    "preferredauthentications",
    "stricthostkeychecking",
}


@dataclass(frozen=True, slots=True)
class SSHHost:
    """A normalized view of one Host (or Match) block."""

    alias: str
    hostname: str  # falls back to `alias` if HostName not set
    user: str | None = None
    port: int | None = None
    identity_files: tuple[str, ...] = field(default_factory=tuple)
    proxy_jump: str | None = None
    comment: str = ""  # the `# ...` line immediately preceding the Host line
    source_path: str = ""  # which file this block came from
    extras: dict[str, str] = field(default_factory=dict)

    @property
    def primary_identity_file(self) -> str | None:
        return self.identity_files[0] if self.identity_files else None

    @property
    def effective_user(self) -> str:
        return self.user or "git"

    @property
    def effective_port(self) -> int:
        return self.port or SSH_DEFAULT_PORT

    def to_dict(self) -> dict[str, object]:
        return {
            "alias": self.alias,
            "hostname": self.hostname,
            "user": self.user,
            "port": self.port,
            "identity_files": list(self.identity_files),
            "proxy_jump": self.proxy_jump,
            "comment": self.comment,
            "source_path": self.source_path,
            "extras": dict(self.extras),
        }


class SSHHostList(tuple[SSHHost, ...]):
    """A tuple subclass with chainable filter helpers.

    Returned by `SSHConfig.hosts()`. Lets you write:

        SSHConfig.load().hosts().exclude_wildcards().with_identity_file()
    """

    def filter(self, **criteria: str) -> SSHHostList:  # noqa: A003 — tuple-like API
        def matches(host: SSHHost) -> bool:
            for key, value in criteria.items():
                actual = getattr(host, key, None)
                if str(actual) != str(value):
                    return False
            return True

        return SSHHostList(h for h in self if matches(h))

    def exclude_wildcards(self) -> SSHHostList:
        return SSHHostList(h for h in self if "*" not in h.alias and "?" not in h.alias)

    def with_identity_file(self) -> SSHHostList:
        return SSHHostList(h for h in self if h.identity_files)

    def by_alias(self, alias: str) -> SSHHost | None:
        for host in self:
            if host.alias == alias:
                return host
        return None


@dataclass
class SSHConfig:
    """A loaded SSH config (possibly assembled from several Include'd files)."""

    _hosts: list[SSHHost] = field(default_factory=list)
    _sources: list[Path] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path | None = None) -> SSHConfig:
        """Load the user's SSH config. Missing file => empty config (no raise)."""
        cfg = cls()
        target = path or default_config_path()
        if target.is_file():
            cfg._load_file(target, visited=set())
        return cfg

    def hosts(self) -> SSHHostList:
        return SSHHostList(self._hosts)

    def sources(self) -> tuple[Path, ...]:
        return tuple(self._sources)

    # --- internals ----------------------------------------------------------

    def _load_file(self, path: Path, *, visited: set[Path]) -> None:
        try:
            resolved = path.resolve()
        except OSError:
            return
        if resolved in visited:
            return
        visited.add(resolved)
        self._sources.append(resolved)

        try:
            text = resolved.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return

        for block in _iter_blocks(text):
            if block.directive == "include":
                for include in _expand_includes(block.value, base_dir=resolved.parent):
                    self._load_file(include, visited=visited)
                continue

            for alias in block.aliases:
                host = _build_host(
                    alias=alias,
                    block=block,
                    source_path=str(resolved),
                )
                self._hosts.append(host)


# --- block parser -----------------------------------------------------------


@dataclass(slots=True)
class _Block:
    directive: str  # "host" | "match" | "include"
    aliases: tuple[str, ...]
    value: str
    pairs: list[tuple[str, str]]
    comment: str


def _iter_blocks(text: str) -> Iterator[_Block]:
    """Yield Host / Match / Include blocks from a raw SSH config file."""
    last_comment = ""
    current: _Block | None = None

    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            last_comment = ""
            continue
        if stripped.startswith("#"):
            last_comment = stripped.lstrip("# ").strip()
            continue

        try:
            tokens = shlex.split(stripped, comments=True, posix=True)
        except ValueError:
            # malformed quoting; skip the line entirely
            continue
        if not tokens:
            continue

        key = tokens[0].lower()
        rest = tokens[1:]

        if key in {"host", "match"}:
            if current is not None:
                yield current
            current = _Block(
                directive=key,
                aliases=tuple(rest) if key == "host" else (" ".join(rest),),
                value=" ".join(rest),
                pairs=[],
                comment=last_comment,
            )
            last_comment = ""
            continue

        if key == "include":
            yield _Block(
                directive="include",
                aliases=(),
                value=" ".join(rest),
                pairs=[],
                comment=last_comment,
            )
            last_comment = ""
            continue

        if current is None:
            continue
        # Inside a Host/Match block: collect key=value pairs.
        if len(rest) >= 1:
            current.pairs.append((key, " ".join(rest)))

    if current is not None:
        yield current


def _build_host(*, alias: str, block: _Block, source_path: str) -> SSHHost:
    user: str | None = None
    hostname: str | None = None
    port: int | None = None
    proxy_jump: str | None = None
    identity_files: list[str] = []
    extras: dict[str, str] = {}

    for key, value in block.pairs:
        if key == "hostname":
            hostname = value
        elif key == "user":
            user = value
        elif key == "port":
            with contextlib.suppress(ValueError):
                port = int(value)
        elif key == "identityfile":
            identity_files.append(value)
        elif key == "proxyjump":
            proxy_jump = value
        elif key in _KNOWN_KEYS:
            extras[key] = value
        else:
            extras[key] = value

    return SSHHost(
        alias=alias,
        hostname=hostname or alias,
        user=user,
        port=port,
        identity_files=tuple(identity_files),
        proxy_jump=proxy_jump,
        comment=block.comment,
        source_path=source_path,
        extras=extras,
    )


# --- include resolution -----------------------------------------------------


_INCLUDE_TOKEN_RE = re.compile(r"\s+")


def _expand_includes(value: str, *, base_dir: Path) -> Iterable[Path]:
    """Expand an `Include` value into concrete file paths.

    OpenSSH treats unqualified paths as relative to `~/.ssh/`, but absolute
    or `~`-prefixed paths win. Shell globs (`*`, `?`, `[...]`) are expanded.
    """
    # Multiple paths can appear on one Include line.
    for token in _INCLUDE_TOKEN_RE.split(value.strip()):
        if not token:
            continue
        candidate = Path(token).expanduser()
        if not candidate.is_absolute():
            ssh_dir = Path.home() / ".ssh"
            candidate = (ssh_dir if base_dir.name == ".ssh" else base_dir) / candidate
        # glob.glob honours character classes + ranges; fall back to the
        # literal path if the pattern matches nothing.
        matches = sorted(_glob.glob(str(candidate)))  # noqa: PTH207 — pathlib has no glob.glob equivalent
        if matches:
            for match in matches:
                yield Path(match)
        elif candidate.exists():
            yield candidate


def default_config_path() -> Path:
    """Return the platform-default SSH config path."""
    return Path.home() / ".ssh" / "config"


def parse_ssh_config(path: Path | None = None) -> SSHConfig:
    """Convenience wrapper for `SSHConfig.load(path)`."""
    return SSHConfig.load(path)
