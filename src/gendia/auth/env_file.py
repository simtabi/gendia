"""Read/write `.env`-style files while preserving comments and ordering.

`DotEnvCredentialStore` (auth/env_store.py) is read-only and parses just
enough to resolve secrets at runtime. This module is the *write* side: a
typed editor used by the `gendia config` CLI to mutate the same file in
place without losing comments, blank lines, or key order.

Design choices:
  - Each line in the file becomes a typed item (`Comment`, `Blank`,
    `Assignment`). Round-trips byte-for-byte except for the keys you change.
  - Atomic save: write to a sibling tmp file, fsync, then `os.replace`.
    Refuses to write a file with looser perms than 0600.
  - No external deps; `python-dotenv` is intentionally avoided to keep
    gendia's runtime dependency footprint at zero.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

# `[export ]KEY=VALUE` with optional surrounding whitespace. The value
# capture is greedy on the right; we strip + dequote separately.
_LINE = re.compile(
    r"^(?P<lead>\s*)(?P<export>export\s+)?"
    r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<val>.*?)\s*$"
)

_MIN_QUOTED_LEN = 2  # opening + closing quote


class EnvFileError(RuntimeError):
    """Raised when an env file can't be read, written, or parsed safely."""


# --- typed items -------------------------------------------------------------


@dataclass
class Comment:
    text: str  # raw line including the leading '#'

    def render(self) -> str:
        return self.text


@dataclass
class Blank:
    def render(self) -> str:
        return ""


@dataclass
class Assignment:
    key: str
    value: str
    exported: bool = False
    quoted: str = ""  # '"' | "'" | "" — preserves original quoting

    def render(self) -> str:
        prefix = "export " if self.exported else ""
        if self.quoted:
            # Escape any same-kind quotes inside the value.
            escaped = self.value.replace("\\", "\\\\").replace(self.quoted, f"\\{self.quoted}")
            return f"{prefix}{self.key}={self.quoted}{escaped}{self.quoted}"
        return f"{prefix}{self.key}={self.value}"


Item = Comment | Blank | Assignment


# --- editor ------------------------------------------------------------------


class EnvFile:
    """In-memory model of a `.env`-style file with read/write helpers."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._items: list[Item] = []
        self._index: dict[str, int] = {}  # key -> position in self._items

    # --- factory methods ---------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> EnvFile:
        """Load `path`, building a typed item list. Empty if file is missing."""
        env = cls(path)
        if not path.is_file():
            return env

        for raw in path.read_text(encoding="utf-8").splitlines():
            stripped = raw.strip()
            if not stripped:
                env._items.append(Blank())
                continue
            if stripped.startswith("#"):
                env._items.append(Comment(text=raw))
                continue

            match = _LINE.match(raw)
            if match is None:
                # Unparseable line: keep it as a comment so save() preserves it.
                env._items.append(Comment(text=raw))
                continue

            value, quoted = _strip_quotes(match.group("val"))
            assignment = Assignment(
                key=match.group("key"),
                value=value,
                exported=bool(match.group("export")),
                quoted=quoted,
            )
            env._items.append(assignment)
            env._index[assignment.key] = len(env._items) - 1

        return env

    # --- queries -----------------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    def keys(self) -> list[str]:
        return list(self._index.keys())

    def get(self, key: str) -> str | None:
        idx = self._index.get(key)
        if idx is None:
            return None
        item = self._items[idx]
        return item.value if isinstance(item, Assignment) else None

    def __contains__(self, key: str) -> bool:
        return key in self._index

    # --- mutations ---------------------------------------------------------

    def set(self, key: str, value: str) -> None:
        """Set or update `key` = `value`. Preserves position when updating."""
        if key in self._index:
            idx = self._index[key]
            existing = self._items[idx]
            if isinstance(existing, Assignment):
                existing.value = value
                return
        # New key: append at end with a blank line of separation if the file
        # doesn't already end in blanks.
        if self._items and not isinstance(self._items[-1], Blank):
            self._items.append(Blank())
        self._items.append(Assignment(key=key, value=value, quoted='"'))
        self._index[key] = len(self._items) - 1

    def unset(self, key: str) -> bool:
        """Remove `key` if present. Returns True if anything was removed."""
        idx = self._index.get(key)
        if idx is None:
            return False
        # Replace the assignment with a Blank so positions of other keys
        # don't shift in `self._index`.
        self._items[idx] = Blank()
        del self._index[key]
        return True

    # --- persistence -------------------------------------------------------

    def save(self, *, mode: int = 0o600) -> None:
        """Atomic save with strict mode enforcement.

        Writes to a sibling tmp file, fsyncs, then `os.replace` so the file
        is never seen in a half-written state. Forces mode 0600 on the
        replacement (override via `mode=` for collaborative-team scenarios).
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)

        rendered = "\n".join(item.render() for item in self._items)
        if not rendered.endswith("\n"):
            rendered += "\n"

        # tempfile in the same directory so os.replace is atomic across
        # filesystems.
        tmp_dir = str(self._path.parent)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=tmp_dir,
            delete=False,
            prefix=".env.tmp-",
        ) as tmp:
            tmp_path = Path(tmp.name)
            tmp.write(rendered)
            tmp.flush()
            os.fsync(tmp.fileno())

        try:
            os.chmod(tmp_path, mode)
            os.replace(tmp_path, self._path)
        except OSError as exc:
            # Clean up the temp file on any failure.
            with _SuppressOSError():
                tmp_path.unlink()
            raise EnvFileError(f"failed to write {self._path}: {exc}") from exc

    def backup(self) -> Path | None:
        """Copy the current file alongside as `<name>.bak`. Returns the path."""
        if not self._path.is_file():
            return None
        backup = self._path.with_suffix(self._path.suffix + ".bak")
        shutil.copy2(self._path, backup)
        return backup

    # --- safety ------------------------------------------------------------

    def assert_secure_mode(self, *, max_octal: int = 0o640) -> None:
        """Raise if the file's mode is looser than `max_octal`."""
        if not self._path.is_file():
            return
        mode = self._path.stat().st_mode & 0o777
        if mode > max_octal:
            raise EnvFileError(
                f"{self._path} has loose permissions (mode {mode:o}); "
                f"run `chmod {max_octal:o} {self._path}` first"
            )


# --- helpers -----------------------------------------------------------------


def _strip_quotes(value: str) -> tuple[str, str]:
    """Return (unquoted_value, quote_char_or_empty)."""
    if len(value) >= _MIN_QUOTED_LEN and value[0] == value[-1] and value[0] in {'"', "'"}:
        # Unescape the same kind of quote inside the value.
        unquoted = value[1:-1].replace(f"\\{value[0]}", value[0]).replace("\\\\", "\\")
        return unquoted, value[0]
    return value, ""


class _SuppressOSError:  # noqa: N801 — mimics contextlib.suppress naming intent
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: type[BaseException] | None, *_: object) -> bool:
        return exc_type is not None and issubclass(exc_type, OSError)


def secure_create_dir(path: Path) -> None:
    """Create the parent directory tree with mode 0700 if it doesn't exist.

    Used by `gendia config set` and `gendia setup` to ensure `~/.config/gendia`
    is locked down before any secret lands inside.
    """
    if path.exists():
        return
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)  # 0700
