"""Config loader: file precedence + env overrides."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gendia.config import load_config
from gendia.config.schema import ConfigError


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_machine_only(tmp_path: Path) -> None:
    machine = tmp_path / "machine.json"
    write_json(
        machine,
        {
            "accounts": {"a": {"platform": "github", "org": "o", "credential_ref": "X"}},
        },
    )
    cfg = load_config(machine_path=machine, project_path=tmp_path / "missing.json")
    assert "a" in cfg.accounts
    assert cfg.project is None


def test_project_overrides_machine(tmp_path: Path) -> None:
    machine = tmp_path / "m.json"
    project = tmp_path / "p.json"
    write_json(
        machine,
        {
            "accounts": {"a": {"platform": "github", "org": "o", "credential_ref": "X"}},
            "defaults": {"concurrency": 8},
        },
    )
    write_json(
        project,
        {
            "name": "demo",
            "account": "a",
            "root": str(tmp_path),
            "repos": [{"dir": "core"}],
        },
    )
    cfg = load_config(machine_path=machine, project_path=project)
    assert cfg.project is not None
    assert cfg.project.name == "demo"
    assert cfg.defaults.concurrency == 8


def test_unknown_account_fails(tmp_path: Path) -> None:
    project = tmp_path / "p.json"
    write_json(
        project,
        {
            "name": "x",
            "account": "missing",
            "repos": [{"dir": "r"}],
            "root": str(tmp_path),
        },
    )
    with pytest.raises(ConfigError, match="unknown account"):
        load_config(project_path=project, machine_path=tmp_path / "m.json")
