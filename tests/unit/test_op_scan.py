"""End-to-end coverage of `gendia scan`."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from gendia.cli.arguments import build_parser, dispatch


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GENDIA_ENV_FILE", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")


def _git_init(path: Path, *, remote: str | None = None) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    if remote:
        subprocess.run(["git", "remote", "add", "origin", remote], cwd=path, check=True)


def _run(argv: list[str]) -> tuple[int, str]:
    parser = build_parser()
    args = parser.parse_args(argv)
    captured = io.StringIO()
    real_out = sys.stdout
    sys.stdout = captured
    try:
        rc = dispatch(args)
    finally:
        sys.stdout = real_out
    return rc, captured.getvalue()


def test_scan_classifies_repos(tmp_path: Path) -> None:
    _git_init(tmp_path / "clean", remote="git@github.com:x/y.git")
    _git_init(tmp_path / "orphan")
    _git_init(tmp_path / "https-it", remote="https://github.com/x/y.git")

    rc, out = _run(["scan", str(tmp_path), "--json"])
    assert rc == 0
    payload = json.loads(out)
    assert payload["root"] == str(tmp_path.resolve())
    classifications = sorted(r["classification"] for r in payload["repos"])
    assert classifications == ["clean", "https-candidate", "orphan"]
