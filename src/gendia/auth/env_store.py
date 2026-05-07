"""Read credentials from environment variables and `.env`-style files.

Supports two sources, layered:
  1. Process environment (`os.environ`)
  2. A `.env`-format file (default: `~/.config/gendia/.env`, mode 0600)

If `_FILE`-suffixed variables are set (e.g. `PACKAGIST_API_TOKEN_FILE`), the
referenced file is read instead. This is the Docker-secrets convention: a
secret is mounted at `/run/secrets/<name>` and its path lives in the env var.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from gendia.auth.base import CredentialStore

_LINE = re.compile(r"^\s*(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<val>.*?)\s*$")
_MIN_QUOTED_LEN = 2  # opening + closing quote


def _strip_quotes(value: str) -> str:
    if len(value) >= _MIN_QUOTED_LEN and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _parse_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        # Comments + blanks
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        match = _LINE.match(raw)
        if not match:
            continue
        out[match.group("key")] = _strip_quotes(match.group("val"))
    return out


class DotEnvCredentialStore(CredentialStore):
    """Resolve secrets from the process env, a `.env` file, and Docker secrets.

    Docker secrets convention: when an environment variable named `<KEY>_FILE`
    is set, the file at that path holds the secret. This is the standard
    pattern for Docker / Kubernetes / Compose secret mounts.
    """

    def __init__(self, *, env_file: Path | None = None) -> None:
        self._env_file = env_file
        self._file_cache: dict[str, str] | None = None

    @property
    def name(self) -> str:
        return "env"

    def _file_values(self) -> dict[str, str]:
        if self._file_cache is None:
            self._file_cache = _parse_env_file(self._env_file) if self._env_file else {}
        return self._file_cache

    def get(self, key: str) -> str | None:
        # 1. Docker-secrets convention: <KEY>_FILE points at a file holding the value.
        file_var = f"{key}_FILE"
        file_path = os.environ.get(file_var) or self._file_values().get(file_var)
        if file_path:
            try:
                value = Path(file_path).read_text(encoding="utf-8").strip()
            except OSError:
                value = None
            if value:
                return value

        # 2. Process env
        if value := os.environ.get(key):
            return value

        # 3. .env file
        return self._file_values().get(key)
