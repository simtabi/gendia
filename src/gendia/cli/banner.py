"""Application banner shown on top-level invocation and `--help`.

Behaviour:
  - Writes to stderr (so it never pollutes machine-readable stdout).
  - Suppressed when:
      * stderr is not a TTY (CI, log files), unless `force=True`
      * `--log-format=json` is in effect
      * `GENDIA_NO_BANNER=1` is set in the environment
      * `--no-banner` is on the parsed args namespace
  - Uses ANSI color when the destination supports it; falls back to plain
    text otherwise (no `colorama` dependency).
"""

from __future__ import annotations

import os
import sys
from typing import TextIO

from gendia import __version__

_DESCRIPTION = "CI/CD Packagist + npm + PyPI dev-workflow handler for polyrepos."
_AUTHOR = "Simtabi LLC"
_LICENSE = "MIT License"
_URL = "https://github.com/simtabi/gendia"

_INNER_WIDTH = 70  # characters inside the box, between `│ ` and ` │`


def _supports_color(stream: TextIO) -> bool:
    if not hasattr(stream, "isatty") or not stream.isatty():
        return False
    if os.environ.get("NO_COLOR"):
        return False
    return os.environ.get("TERM") != "dumb"


def should_show(*, args: object | None = None, stream: TextIO | None = None) -> bool:
    """Decide whether the banner should be printed in the current context."""
    stream = stream or sys.stderr
    if os.environ.get("GENDIA_NO_BANNER"):
        return False
    if args is not None and getattr(args, "no_banner", False):
        return False
    if args is not None and getattr(args, "log_format", None) == "json":
        return False
    return hasattr(stream, "isatty") and stream.isatty()


def _line(content: str, *, color: str = "", reset: str = "", box: str = "") -> str:
    """Build one box-row of fixed `_INNER_WIDTH`."""
    pad = max(0, _INNER_WIDTH - len(content))
    return f"{box}│{reset} {color}{content}{reset}{' ' * pad} {box}│{reset}"


def render(*, color: bool | None = None) -> str:
    """Build the banner string. Set `color=True/False` to override autodetection."""
    if color is None:
        color = _supports_color(sys.stderr)

    box = "\033[2;37m" if color else ""
    name = "\033[1;36m" if color else ""
    version = "\033[0;33m" if color else ""
    desc = "\033[0;37m" if color else ""
    meta = "\033[2;37m" if color else ""
    url = "\033[0;34m" if color else ""
    reset = "\033[0m" if color else ""

    horizontal = "─" * (_INNER_WIDTH + 2)
    name_text = "gendia v" + __version__

    lines = [
        f"{box}┌{horizontal}┐{reset}",
        # Name + version, colored together by writing the segment manually.
        (
            f"{box}│{reset} "
            f"{name}gendia{reset} {version}v{__version__}{reset}"
            f"{' ' * max(0, _INNER_WIDTH - len(name_text))} "
            f"{box}│{reset}"
        ),
        _line(_DESCRIPTION, color=desc, reset=reset, box=box),
        _line(f"© {_AUTHOR} — {_LICENSE}", color=meta, reset=reset, box=box),
        _line(_URL, color=url, reset=reset, box=box),
        f"{box}└{horizontal}┘{reset}",
    ]
    return "\n".join(lines) + "\n"


def emit(stream: TextIO | None = None, *, force: bool = False) -> None:
    """Write the banner to `stream` (default stderr) when appropriate."""
    stream = stream or sys.stderr
    if not force and not should_show(stream=stream):
        return
    stream.write(render())
    stream.flush()
