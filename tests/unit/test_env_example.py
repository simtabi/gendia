"""Sanity-check the shipped `.env.example`.

Catches drift between the sample file and the credential names the rest of
the project (README, examples) refers to. Pure parsing, no execution.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def env_example_text() -> str:
    repo_root = Path(__file__).parents[2]
    return (repo_root / "examples" / ".env.example").read_text(encoding="utf-8")


def _keys_in(text: str) -> set[str]:
    """Return the set of variable names assigned (uncommented) in the file."""
    pattern = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=", re.MULTILINE)
    return set(pattern.findall(text))


def test_env_example_contains_each_platform_credential(env_example_text: str) -> None:
    keys = _keys_in(env_example_text)
    expected_substrings = (
        "GITHUB_TOKEN",
        "GITLAB_TOKEN",
        "BITBUCKET_APP_PASSWORD",
        "PACKAGIST_API_TOKEN",
        "NPM_TOKEN",
        "PYPI_API_TOKEN",
    )
    for substring in expected_substrings:
        matching = [k for k in keys if substring in k and not k.endswith("_FILE")]
        assert matching, f"no key containing {substring!r} in .env.example"


def test_env_example_documents_file_pattern(env_example_text: str) -> None:
    """The file should mention the *_FILE Docker-secrets convention prominently."""
    assert "_FILE" in env_example_text
    assert "/run/secrets/" in env_example_text
    assert "Docker" in env_example_text or "docker" in env_example_text


def test_env_example_warns_about_chmod(env_example_text: str) -> None:
    assert "chmod 600" in env_example_text
