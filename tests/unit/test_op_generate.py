"""Smoke tests for `gendia generate`."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from gendia.cli.arguments import build_parser, dispatch


@pytest.fixture(autouse=True)
def _no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GENDIA_ENV_FILE", "/dev/null")


def _ssh_config(tmp_path: Path) -> Path:
    p = tmp_path / "ssh_config"
    p.write_text(
        "Host github.com\n"
        "    HostName github.com\n"
        "    User git\n"
        "    IdentityFile ~/.ssh/keys/personal/id_ed25519\n"
        "Host gitlab.example.com\n"
        "    HostName gitlab.example.com\n"
        "    User git\n",
        encoding="utf-8",
    )
    return p


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


def test_generate_text_includes_detected_hosts(tmp_path: Path) -> None:
    rc, out = _run(
        [
            "generate",
            "--ssh-config",
            str(_ssh_config(tmp_path)),
            "--no-discover",
            "--format",
            "text",
        ]
    )
    assert rc == 0
    assert "github.com" in out
    assert "gitlab.example.com" in out
    assert "Common workflows" in out.lower() or "COMMON WORKFLOWS" in out


def test_generate_json_shape(tmp_path: Path) -> None:
    rc, out = _run(
        [
            "generate",
            "--ssh-config",
            str(_ssh_config(tmp_path)),
            "--no-discover",
            "--format",
            "json",
        ]
    )
    assert rc == 0
    # The "wrote..." status line is absent because we didn't pass --output;
    # JSON goes to stdout directly. Strip a trailing newline before parsing.
    payload = json.loads(out.strip())
    assert "hosts" in payload
    aliases = sorted(h["alias"] for h in payload["hosts"])
    assert aliases == ["github.com", "gitlab.example.com"]


def test_generate_script_has_shebang(tmp_path: Path) -> None:
    rc, out = _run(
        [
            "generate",
            "--ssh-config",
            str(_ssh_config(tmp_path)),
            "--no-discover",
            "--format",
            "script",
        ]
    )
    assert rc == 0
    assert out.splitlines()[0] == "#!/usr/bin/env bash"
    assert "set -e" in out


def test_generate_writes_output_file(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    rc, out = _run(
        [
            "generate",
            "--ssh-config",
            str(_ssh_config(tmp_path)),
            "--no-discover",
            "--format",
            "text",
            "--output",
            str(target),
        ]
    )
    assert rc == 0
    assert target.is_file()
    assert "github.com" in target.read_text(encoding="utf-8")
