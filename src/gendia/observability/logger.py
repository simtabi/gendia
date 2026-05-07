"""Structured logging with two output formats: human (default) and JSON.

JSON format suits VPS / CI / log-shipping pipelines. Human format suits
interactive use. Selection is via `configure_logging(format=...)` or the
`GENDIA_LOG_FORMAT` environment variable.

No external dependencies; built on stdlib `logging`.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime
from typing import Any


class _JsonFormatter(logging.Formatter):
    """Emit each record as a single-line JSON object."""

    _RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
        "message",
        "asctime",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        # Surface any extra={"foo": "bar"} fields the caller passed.
        for key, value in record.__dict__.items():
            if key in self._RESERVED:
                continue
            if key.startswith("_"):
                continue
            try:
                json.dumps(value)
                safe = value
            except (TypeError, ValueError):
                safe = repr(value)
            payload[key] = safe

        return json.dumps(payload, separators=(",", ":"))


class _HumanFormatter(logging.Formatter):
    """Compact `[LEVEL] message` with optional context as `key=value`."""

    _LEVEL_COLORS = {
        "DEBUG": "\033[37m",
        "INFO": "\033[36m",
        "WARNING": "\033[33m",
        "ERROR": "\033[31m",
        "CRITICAL": "\033[1;31m",
    }
    _RESET = "\033[0m"
    _RESERVED = _JsonFormatter._RESERVED

    def __init__(self, *, color: bool) -> None:
        super().__init__()
        self._color = color

    def format(self, record: logging.LogRecord) -> str:
        prefix = f"[{record.levelname}]"
        if self._color and record.levelname in self._LEVEL_COLORS:
            prefix = f"{self._LEVEL_COLORS[record.levelname]}{prefix}{self._RESET}"

        parts = [prefix, record.getMessage()]

        extras = []
        for key, value in record.__dict__.items():
            if key in self._RESERVED or key.startswith("_"):
                continue
            extras.append(f"{key}={value}")
        if extras:
            parts.append(" ".join(extras))

        line = " ".join(parts)
        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        return line


def configure_logging(
    *,
    level: str | int = "INFO",
    output_format: str | None = None,
    stream: Any = None,
) -> None:
    """Set up the root logger. Idempotent (safe to call repeatedly)."""
    output_format = output_format or os.environ.get("GENDIA_LOG_FORMAT") or "human"
    stream = stream or sys.stderr

    handler = logging.StreamHandler(stream=stream)
    if output_format == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        # Color only when we're attached to a TTY.
        color = hasattr(stream, "isatty") and stream.isatty()
        handler.setFormatter(_HumanFormatter(color=color))

    root = logging.getLogger("gendia")
    root.setLevel(level if isinstance(level, int) else level.upper())
    root.handlers.clear()
    root.addHandler(handler)
    root.propagate = False


def get_logger(name: str) -> logging.Logger:
    """Get a child logger under the `gendia` root."""
    return logging.getLogger(f"gendia.{name}" if not name.startswith("gendia") else name)
