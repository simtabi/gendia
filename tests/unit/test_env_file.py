"""EnvFile read/write/round-trip behaviour."""

from __future__ import annotations

from pathlib import Path

import pytest

from gendia.auth.env_file import EnvFile, EnvFileError


@pytest.fixture
def populated(tmp_path: Path) -> Path:
    p = tmp_path / ".env"
    p.write_text(
        "# Header comment\n"
        "\n"
        "GITHUB_TOKEN=ghp_xxx\n"
        "# A note about npm\n"
        'NPM_TOKEN="npm_yyy"\n'
        "export PYPI_API_TOKEN=pypi-zzz\n",
        encoding="utf-8",
    )
    p.chmod(0o600)
    return p


def test_load_parses_each_item_kind(populated: Path) -> None:
    env = EnvFile.load(populated)
    kinds = [type(item).__name__ for item in env._items]  # noqa: SLF001 — inspecting internals
    assert kinds == ["Comment", "Blank", "Assignment", "Comment", "Assignment", "Assignment"]


def test_get_returns_unquoted_value(populated: Path) -> None:
    env = EnvFile.load(populated)
    assert env.get("GITHUB_TOKEN") == "ghp_xxx"
    assert env.get("NPM_TOKEN") == "npm_yyy"  # quotes stripped
    assert env.get("PYPI_API_TOKEN") == "pypi-zzz"
    assert env.get("MISSING") is None


def test_set_updates_existing_in_place(populated: Path) -> None:
    env = EnvFile.load(populated)
    env.set("GITHUB_TOKEN", "ghp_new_value")
    env.save()

    saved = populated.read_text(encoding="utf-8")
    assert "GITHUB_TOKEN=ghp_new_value" in saved
    assert "ghp_xxx" not in saved
    # Other lines preserved.
    assert "# Header comment" in saved
    assert "# A note about npm" in saved
    assert 'NPM_TOKEN="npm_yyy"' in saved


def test_set_appends_new_key_with_blank_separator(populated: Path) -> None:
    env = EnvFile.load(populated)
    env.set("NEW_TOKEN", "new_value")
    env.save()

    lines = populated.read_text(encoding="utf-8").splitlines()
    assert any("NEW_TOKEN" in line for line in lines)
    # New key sits at the end.
    assert "NEW_TOKEN" in lines[-1]


def test_unset_removes_without_shifting_other_keys(populated: Path) -> None:
    env = EnvFile.load(populated)
    env.unset("NPM_TOKEN")
    env.save()

    saved = populated.read_text(encoding="utf-8")
    assert "NPM_TOKEN" not in saved
    assert "GITHUB_TOKEN=ghp_xxx" in saved
    assert "PYPI_API_TOKEN=pypi-zzz" in saved
    # The descriptive comment that lived above NPM_TOKEN should still be there.
    assert "# A note about npm" in saved


def test_save_uses_mode_0600_by_default(populated: Path) -> None:
    env = EnvFile.load(populated)
    env.set("GITHUB_TOKEN", "x")
    env.save()
    assert populated.stat().st_mode & 0o777 == 0o600


def test_save_atomic_replaces_existing(populated: Path) -> None:
    """The temp file should not linger and only the final value should be visible."""
    env = EnvFile.load(populated)
    env.set("GITHUB_TOKEN", "atomic_value")
    env.save()

    # No leftover .env.tmp-* sibling.
    siblings = list(populated.parent.iterdir())
    assert all(not s.name.startswith(".env.tmp-") for s in siblings)
    assert env.get("GITHUB_TOKEN") == "atomic_value"


def test_backup_creates_sibling_bak(populated: Path) -> None:
    env = EnvFile.load(populated)
    backup = env.backup()
    assert backup is not None
    assert backup.is_file()
    assert backup.read_text(encoding="utf-8") == populated.read_text(encoding="utf-8")


def test_assert_secure_mode_raises_on_loose_perms(tmp_path: Path) -> None:
    p = tmp_path / ".env"
    p.write_text("X=1", encoding="utf-8")
    p.chmod(0o644)
    env = EnvFile.load(p)
    with pytest.raises(EnvFileError, match="loose permissions"):
        env.assert_secure_mode()


def test_round_trip_preserves_quoting_when_unchanged(populated: Path) -> None:
    env = EnvFile.load(populated)
    env.save()  # no edits; should round-trip
    saved = populated.read_text(encoding="utf-8")
    assert 'NPM_TOKEN="npm_yyy"' in saved
    assert "export PYPI_API_TOKEN=pypi-zzz" in saved
