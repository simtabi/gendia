"""XDG-compliant path resolution.

`XDG_CONFIG_HOME` defaults to `~/.config`. We layer a fixed `gendia` subdir.
Same for cache and data dirs. This lets the Docker image set
`XDG_CONFIG_HOME=/config` and have everything follow.
"""

from __future__ import annotations

import os
from pathlib import Path


def config_home() -> Path:
    """Return `$XDG_CONFIG_HOME/gendia` (default: `~/.config/gendia`)."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "gendia"


def cache_home() -> Path:
    """Return `$XDG_CACHE_HOME/gendia` (default: `~/.cache/gendia`)."""
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "gendia"


def data_home() -> Path:
    """Return `$XDG_DATA_HOME/gendia` (default: `~/.local/share/gendia`)."""
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "gendia"


def project_config_paths(cwd: Path | None = None) -> list[Path]:
    """Per-project config search order, most-specific first.

    1. `./gendia.json` in the current working directory
    2. `./.gendia.json` (hidden alternative)
    3. `~/.config/gendia/gendia.json` (machine-wide default)
    """
    cwd = cwd or Path.cwd()
    return [
        cwd / "gendia.json",
        cwd / ".gendia.json",
        config_home() / "gendia.json",
    ]


def env_file_path() -> Path:
    """Default secrets file location (mode 0600 recommended).

    Resolution order:
      1. `$GENDIA_ENV_FILE`  (highest precedence; matches `bin/gendia-env`)
      2. `$XDG_CONFIG_HOME/gendia/.env`  (default: `~/.config/gendia/.env`)
    """
    if override := os.environ.get("GENDIA_ENV_FILE"):
        return Path(override).expanduser()
    return config_home() / ".env"
