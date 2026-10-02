"""Top-level `forges_file` plumbing through loader + GendiaConfig."""

from __future__ import annotations

import json
from pathlib import Path

from gendia.config import load_config


def test_forges_file_default_is_none(tmp_path: Path) -> None:
    cfg = tmp_path / "gendia.json"
    cfg.write_text(
        json.dumps(
            {
                "accounts": {"a": {"platform": "github", "credential_ref": "X", "org": "o"}},
            }
        )
    )
    loaded = load_config(project_path=cfg, machine_path=tmp_path / "missing.json")
    assert loaded.forges_file is None


def test_forges_file_propagates_from_config(tmp_path: Path) -> None:
    cfg = tmp_path / "gendia.json"
    cfg.write_text(
        json.dumps(
            {
                "forges_file": "~/.config/gendia/forges.json",
                "accounts": {"a": {"platform": "github", "credential_ref": "X", "org": "o"}},
            }
        )
    )
    loaded = load_config(project_path=cfg, machine_path=tmp_path / "missing.json")
    assert loaded.forges_file == "~/.config/gendia/forges.json"


def test_forges_file_project_overrides_machine(tmp_path: Path) -> None:
    machine = tmp_path / "machine.json"
    machine.write_text(
        json.dumps(
            {
                "forges_file": "/etc/gendia/forges.json",
                "accounts": {"a": {"platform": "github", "credential_ref": "X", "org": "o"}},
            }
        )
    )
    project = tmp_path / "gendia.json"
    project.write_text(json.dumps({"forges_file": "./local-forges.json"}))
    loaded = load_config(project_path=project, machine_path=machine)
    assert loaded.forges_file == "./local-forges.json"
