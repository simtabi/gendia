"""`gendia conventions` rule engine.

Each test builds a tmp_path fixture that mirrors the layout the check
under inspection cares about, runs the single check (or the full
orchestrator), and inspects the resulting Findings.
"""

from __future__ import annotations

import json
from pathlib import Path

from gendia.operations.conventions import (
    Finding,
    Rules,
    _check_decorator_emojis,
    _check_forbidden_chars,
    _check_github_special,
    _check_md_kebab_case,
    _check_readme_frontmatter,
    _check_shell_shebang,
    _check_spec_filenames,
    _check_stale_link_patterns,
    _check_subfolder_readmes,
    _check_trailing_whitespace,
    run_checks,
)


def _severities(findings: tuple[Finding, ...] | list[Finding]) -> set[str]:
    return {f.severity for f in findings}


def _by_rule(findings: tuple[Finding, ...] | list[Finding], rule: str) -> list[Finding]:
    return [f for f in findings if f.rule == rule]


# --- Rules.from_dict -------------------------------------------------------


def test_rules_from_dict_overrides_only_specified_keys() -> None:
    rules = Rules.from_dict(
        {
            "github_special_required": ["LICENSE"],
            "forbid_trailing_whitespace_md": True,
        }
    )
    assert rules.github_special_required == ("LICENSE",)
    assert rules.forbid_trailing_whitespace_md is True
    # Untouched keys keep defaults.
    assert "CHANGELOG.md" in rules.md_kebab_exempt_names


def test_rules_from_dict_handles_none_and_empty() -> None:
    assert Rules.from_dict(None) == Rules()
    assert Rules.from_dict({}) == Rules()


# --- github-special --------------------------------------------------------


def test_github_special_missing_required_files_errors(tmp_path: Path) -> None:
    findings = list(_check_github_special(tmp_path, Rules()))
    assert any(f.severity == "error" and "LICENSE" in f.message for f in findings)
    assert any(f.severity == "error" and "CONTRIBUTING.md" in f.message for f in findings)


def test_github_special_present_files_pass(tmp_path: Path) -> None:
    for name in ("LICENSE", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md"):
        (tmp_path / name).write_text("x", encoding="utf-8")

    findings = list(_check_github_special(tmp_path, Rules()))
    assert _severities(findings) == {"ok"}


# --- md-kebab-case ---------------------------------------------------------


def test_md_kebab_case_flags_uppercase_filenames(tmp_path: Path) -> None:
    (tmp_path / "Foo-Bar.md").write_text("x", encoding="utf-8")
    (tmp_path / "good-name.md").write_text("x", encoding="utf-8")

    findings = list(_check_md_kebab_case(tmp_path, Rules()))
    bad = [f for f in findings if f.severity == "error"]
    assert any("Foo-Bar.md" in f.message for f in bad)
    assert not any("good-name.md" in f.message for f in bad)


def test_md_kebab_case_exempts_special_filenames(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("x", encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text("x", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("x", encoding="utf-8")

    findings = list(_check_md_kebab_case(tmp_path, Rules()))
    # Only the catch-all "ok" finding should be present.
    assert all(f.severity == "ok" for f in findings)


# --- forbidden-chars -------------------------------------------------------


def test_forbidden_chars_warns_on_em_dash(tmp_path: Path) -> None:
    (tmp_path / "doc.md").write_text("hello — world", encoding="utf-8")
    findings = list(_check_forbidden_chars(tmp_path, Rules()))
    assert any(f.severity == "warn" and "U+2014" in f.message for f in findings)


def test_forbidden_chars_skips_exempt_files(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("hello — world", encoding="utf-8")
    (tmp_path / "ok.md").write_text("plain ascii only", encoding="utf-8")
    findings = list(_check_forbidden_chars(tmp_path, Rules()))
    assert all(f.severity == "ok" for f in findings)


# --- readme frontmatter ----------------------------------------------------


def test_readme_frontmatter_only_runs_when_enabled(tmp_path: Path) -> None:
    (tmp_path / "readme.md").write_text("# Title\nno frontmatter here", encoding="utf-8")
    rules_off = Rules()  # default: require_readme_frontmatter=False
    rules_on = Rules.from_dict({"require_readme_frontmatter": True})

    assert list(_check_readme_frontmatter(tmp_path, rules_off)) == []
    findings = list(_check_readme_frontmatter(tmp_path, rules_on))
    assert any(f.severity == "warn" and "Owner" in f.message for f in findings)


def test_readme_frontmatter_passes_when_keys_present(tmp_path: Path) -> None:
    (tmp_path / "readme.md").write_text(
        "# Title\n**Owner:** Alice\n**Last Updated:** 2026-05-08\n",
        encoding="utf-8",
    )
    findings = list(
        _check_readme_frontmatter(tmp_path, Rules.from_dict({"require_readme_frontmatter": True}))
    )
    assert _severities(findings) == {"ok"}


# --- decorator-emojis ------------------------------------------------------


def test_decorator_emojis_warns(tmp_path: Path) -> None:
    (tmp_path / "doc.md").write_text("Look 🚀 a rocket", encoding="utf-8")
    findings = list(_check_decorator_emojis(tmp_path, Rules()))
    assert any(f.severity == "warn" for f in findings)


def test_decorator_emojis_allow_status_emojis(tmp_path: Path) -> None:
    (tmp_path / "doc.md").write_text("Done ✅", encoding="utf-8")
    findings = list(_check_decorator_emojis(tmp_path, Rules()))
    assert _severities(findings) == {"ok"}


# --- spec filenames --------------------------------------------------------


def test_spec_date_prefix_errors(tmp_path: Path) -> None:
    specs = tmp_path / "specs"
    specs.mkdir()
    (specs / "2026-05-08-thing.md").write_text("x", encoding="utf-8")
    findings = list(_check_spec_filenames(tmp_path, Rules()))
    assert any(f.rule == "spec-date-prefix" and f.severity == "error" for f in findings)


def test_spec_number_prefix_errors(tmp_path: Path) -> None:
    specs = tmp_path / "specs"
    specs.mkdir()
    (specs / "001-thing.md").write_text("x", encoding="utf-8")
    findings = list(_check_spec_filenames(tmp_path, Rules()))
    assert any(f.rule == "spec-number-prefix" and f.severity == "error" for f in findings)


def test_spec_dir_absent_skips_check(tmp_path: Path) -> None:
    findings = list(_check_spec_filenames(tmp_path, Rules()))
    assert findings == []


# --- subfolder readmes -----------------------------------------------------


def test_subfolder_readme_warns(tmp_path: Path) -> None:
    (tmp_path / "subdir").mkdir()
    (tmp_path / "subdir" / "subsubdir").mkdir()
    (tmp_path / "subdir" / "subsubdir" / "readme.md").write_text("x", encoding="utf-8")
    findings = list(_check_subfolder_readmes(tmp_path, Rules()))
    assert any(f.severity == "warn" and "subsubdir/readme.md" in f.message for f in findings)


# --- stale link patterns ---------------------------------------------------


def test_stale_link_pattern_flags_numbered_spec_link(tmp_path: Path) -> None:
    (tmp_path / "doc.md").write_text(
        "[link](../specs/001-old.md)\n",
        encoding="utf-8",
    )
    findings = list(_check_stale_link_patterns(tmp_path, Rules()))
    assert any(f.severity == "warn" for f in findings)


def test_stale_link_pattern_flags_subfolder_readme_link(tmp_path: Path) -> None:
    (tmp_path / "doc.md").write_text("[link](legacy/readme.md)\n", encoding="utf-8")
    findings = list(_check_stale_link_patterns(tmp_path, Rules()))
    assert any(f.severity == "warn" for f in findings)


# --- shell shebang ---------------------------------------------------------


def test_shell_shebang_missing_errors(tmp_path: Path) -> None:
    f = tmp_path / "script.sh"
    f.write_text("echo hi\n", encoding="utf-8")
    f.chmod(0o755)
    findings = list(_check_shell_shebang(tmp_path, Rules()))
    assert any(f.severity == "error" and "missing shebang" in f.message for f in findings)


def test_shell_shebang_not_executable_warns(tmp_path: Path) -> None:
    f = tmp_path / "script.sh"
    f.write_text("#!/usr/bin/env bash\necho hi\n", encoding="utf-8")
    f.chmod(0o644)
    findings = list(_check_shell_shebang(tmp_path, Rules()))
    assert any(f.severity == "warn" and "not executable" in f.message for f in findings)


def test_shell_shebang_clean_passes(tmp_path: Path) -> None:
    f = tmp_path / "script.sh"
    f.write_text("#!/usr/bin/env bash\necho hi\n", encoding="utf-8")
    f.chmod(0o755)
    findings = list(_check_shell_shebang(tmp_path, Rules()))
    assert _severities(findings) == {"ok"}


# --- trailing whitespace ---------------------------------------------------


def test_trailing_whitespace_warns_when_enabled(tmp_path: Path) -> None:
    (tmp_path / "doc.md").write_text("hello   \nworld\n", encoding="utf-8")
    rules = Rules.from_dict({"forbid_trailing_whitespace_md": True})
    findings = list(_check_trailing_whitespace(tmp_path, rules))
    assert any(f.severity == "warn" and "doc.md:1" in f.message for f in findings)


def test_trailing_whitespace_off_by_default(tmp_path: Path) -> None:
    (tmp_path / "doc.md").write_text("hello   \nworld\n", encoding="utf-8")
    findings = list(_check_trailing_whitespace(tmp_path, Rules()))
    assert findings == []


# --- orchestrator end-to-end ----------------------------------------------


def test_run_checks_aggregates_clean_repo(tmp_path: Path) -> None:
    """A repo that follows all conventions ends with no errors and no warns."""
    for name in ("LICENSE", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    (tmp_path / "readme.md").write_text("# Title\n", encoding="utf-8")

    findings = run_checks(tmp_path, Rules())
    assert not any(f.severity == "error" for f in findings)
    assert not any(f.severity == "warn" for f in findings)


def test_run_checks_aggregates_dirty_repo(tmp_path: Path) -> None:
    """A repo with multiple violations surfaces them all in one pass."""
    # Missing required GitHub-special files (3x error).
    (tmp_path / "Bad_Name.md").write_text("uses an em — dash\n", encoding="utf-8")
    f = tmp_path / "broken.sh"
    f.write_text("echo hi\n", encoding="utf-8")
    f.chmod(0o644)

    findings = run_checks(tmp_path, Rules())
    assert any(f.rule == "github-special" and f.severity == "error" for f in findings)
    assert any(f.rule == "md-kebab-case" and f.severity == "error" for f in findings)
    assert any(f.rule == "forbidden-chars" and f.severity == "warn" for f in findings)
    assert any(f.rule == "sh-shebang" for f in findings)


# --- json round-trip -------------------------------------------------------


def test_finding_to_dict_round_trips() -> None:
    f = Finding("warn", "rule-x", "message", path="p.md", fix="do thing")
    payload = f.to_dict()
    assert json.loads(json.dumps(payload)) == payload
    assert payload["severity"] == "warn"
    assert payload["path"] == "p.md"
