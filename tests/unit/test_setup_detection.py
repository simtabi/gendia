"""Auto-detection of existing gendia setups."""

from __future__ import annotations

from pathlib import Path

import pytest

from gendia.cli.commands.setup import _detect


def test_detect_no_setup_returns_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("GENDIA_ENV_FILE", raising=False)
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.chdir(tmp_path)

    detection = _detect()
    assert detection.shape is None
    assert detection.env_file is None


def test_detect_local_at_xdg_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".config" / "gendia").mkdir(parents=True)
    env_file = home / ".config" / "gendia" / ".env"
    env_file.write_text("DEMO=x\n", encoding="utf-8")
    env_file.chmod(0o600)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GENDIA_ENV_FILE", raising=False)
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.chdir(tmp_path)

    detection = _detect()
    assert detection.shape == "local"
    assert detection.env_file == env_file


def test_detect_project_when_env_var_overrides(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_env = tmp_path / "myproj" / ".env.gendia"
    project_env.parent.mkdir()
    project_env.write_text("DEMO=x\n", encoding="utf-8")
    project_env.chmod(0o600)

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GENDIA_ENV_FILE", str(project_env))
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)

    detection = _detect()
    assert detection.shape == "project"
    assert detection.env_file == project_env


def test_detect_k8s_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("GENDIA_ENV_FILE", raising=False)
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    monkeypatch.chdir(tmp_path)

    detection = _detect()
    assert detection.runtime == "k8s"
    # No env file present, so shape stays None even though runtime says k8s.
    assert detection.shape is None


def test_detect_flags_loose_permissions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    (home / ".config" / "gendia").mkdir(parents=True)
    env_file = home / ".config" / "gendia" / ".env"
    env_file.write_text("DEMO=x\n", encoding="utf-8")
    env_file.chmod(0o644)  # too loose

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GENDIA_ENV_FILE", raising=False)
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.chdir(tmp_path)

    detection = _detect()
    assert detection.shape == "local"
    assert any("loose permissions" in note for note in detection.notes)
