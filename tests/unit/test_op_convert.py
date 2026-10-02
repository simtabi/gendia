"""End-to-end coverage of `gendia convert`."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

from gendia.cli.arguments import build_parser, dispatch


@pytest.fixture(autouse=True)
def _no_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GENDIA_ENV_FILE", "/dev/null")


def _run(argv: list[str], stdin: str = "") -> tuple[int, str]:
    parser = build_parser()
    args = parser.parse_args(argv)
    captured = io.StringIO()
    real_out = sys.stdout
    real_in = sys.stdin
    sys.stdout = captured
    if stdin:
        sys.stdin = io.StringIO(stdin)
    try:
        rc = dispatch(args)
    finally:
        sys.stdout = real_out
        sys.stdin = real_in
    return rc, captured.getvalue()


_FLAGS = ["--no-insteadof", "--no-ssh-config"]


def test_convert_to_ssh(tmp_path: Path) -> None:
    rc, out = _run(["convert", "https://github.com/foo/bar.git", "--to", "ssh", *_FLAGS])
    assert rc == 0
    assert "git@github.com:foo/bar.git" in out.splitlines()[0]


def test_convert_to_https() -> None:
    rc, out = _run(["convert", "git@github.com:foo/bar.git", "--to", "https", *_FLAGS])
    assert rc == 0
    assert "https://github.com/foo/bar.git" in out.splitlines()[0]


def test_convert_json_output() -> None:
    rc, out = _run(["convert", "git@github.com:foo/bar.git", "--to", "https", "--json", *_FLAGS])
    assert rc == 0
    assert '"target": "https"' in out
    assert "https://github.com/foo/bar.git" in out
