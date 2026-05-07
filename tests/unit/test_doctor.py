"""`gendia doctor` health-check logic.

Exercises each `_check_*` against tmpdir fixtures so we never touch the
host's real config or run real git/ssh commands.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gendia.cli.commands.doctor import (
    Report,
    _check_env_file,
)


def _severities(report: Report) -> set[str]:
    return {f.severity for f in report.findings}


# --- env file ----------------------------------------------------------------


def test_env_file_missing_warns(tmp_path: Path) -> None:
    report = Report()
    _check_env_file(report, tmp_path / "missing.env")
    assert any(f.severity == "warn" and "no env file" in f.message for f in report.findings)


def test_env_file_loose_perms_errors(tmp_path: Path) -> None:
    f = tmp_path / ".env"
    f.write_text("X=1\n", encoding="utf-8")
    f.chmod(0o644)

    report = Report()
    _check_env_file(report, f)
    assert any(f.severity == "error" and "loose permissions" in f.message for f in report.findings)


def test_env_file_clean_ok(tmp_path: Path) -> None:
    f = tmp_path / ".env"
    f.write_text("FOO=1\nBAR=2\n", encoding="utf-8")
    f.chmod(0o600)

    report = Report()
    _check_env_file(report, f)
    severities = _severities(report)
    assert "ok" in severities
    assert "error" not in severities


def test_env_file_duplicates_warn(tmp_path: Path) -> None:
    f = tmp_path / ".env"
    f.write_text("FOO=1\nFOO=2\nBAR=3\n", encoding="utf-8")
    f.chmod(0o600)

    report = Report()
    _check_env_file(report, f)
    duplicate_warnings = [
        finding
        for finding in report.findings
        if finding.severity == "warn" and "duplicate" in finding.message
    ]
    assert duplicate_warnings


# --- exit-code aggregation --------------------------------------------------


def test_report_exit_code_aggregates_to_max_severity() -> None:
    r = Report()
    r.add("ok", "x", "fine")
    assert r.exit_code() == 0

    r.add("warn", "x", "uh-oh")
    assert r.exit_code() == 1

    r.add("error", "x", "broken")
    assert r.exit_code() == 2


def test_report_render_groups_by_section(capsys: pytest.CaptureFixture[str]) -> None:
    r = Report()
    r.add("ok", "alpha", "alpha-fact")
    r.add("warn", "beta", "beta-warn")
    r.add("ok", "alpha", "another alpha")

    r.render()
    out = capsys.readouterr().out
    # Section headers in encounter order.
    assert out.find("== alpha ==") < out.find("== beta ==")
    # Both alpha lines are under the alpha header.
    alpha_block = out.split("== beta ==")[0]
    assert "alpha-fact" in alpha_block
    assert "another alpha" in alpha_block
