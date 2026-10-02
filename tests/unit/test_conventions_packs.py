"""End-to-end coverage of `gendia conventions --rule-pack` and `--profile`."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from gendia.cli.arguments import build_parser, dispatch
from gendia.operations import conventions as conv_op


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


def _well_formed(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# x\n", encoding="utf-8")
    (tmp_path / "LICENSE").write_text("MIT License\n", encoding="utf-8")
    (tmp_path / "CONTRIBUTING.md").write_text("Hi\n", encoding="utf-8")
    (tmp_path / "CODE_OF_CONDUCT.md").write_text("Hi\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("*.pyc\n", encoding="utf-8")
    (tmp_path / ".gitattributes").write_text("* text=auto\n", encoding="utf-8")
    (tmp_path / ".editorconfig").write_text("root = true\n", encoding="utf-8")


def test_list_profiles_includes_core() -> None:
    rc, out = _run(["conventions", "--list-profiles"])
    assert rc == 0
    assert "core" in out


def test_profile_core_passes_on_well_formed_repo(tmp_path: Path) -> None:
    _well_formed(tmp_path)
    rc, out = _run(["conventions", str(tmp_path), "--profile", "core", "--json"])
    assert rc == 0
    payload = json.loads(out)
    rule_ids = {f["rule"] for r in payload["reports"] for f in r["findings"]}
    # Core's R-rules don't fire on a well-formed repo. Legacy hardcoded
    # findings may still fire (e.g., decorator-emojis), but no R-id should.
    assert not any(rid.startswith("R") for rid in rule_ids)


def test_profile_core_flags_missing_essentials(tmp_path: Path) -> None:
    rc, out = _run(["conventions", str(tmp_path), "--profile", "core", "--json"])
    payload = json.loads(out)
    rule_ids = {f["rule"] for r in payload["reports"] for f in r["findings"]}
    assert "R001" in rule_ids  # README missing
    assert "R004" in rule_ids  # LICENSE missing
    assert "R007" in rule_ids  # .gitignore missing
    assert rc == 2  # at least one error finding


def test_profile_unknown_warns_but_does_not_crash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _well_formed(tmp_path)
    rc, _ = _run(["conventions", str(tmp_path), "--profile", "no-such-pack", "--json"])
    err = capsys.readouterr().err
    assert "no-such-pack" in err
    assert rc in {0, 1, 2}  # legacy rules still ran


def test_rule_pack_path_loads_from_disk(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    pack = tmp_path / "custom.json"
    pack.write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "id": "ORG001",
                        "name": "Repo must have FOO.md",
                        "severity": "error",
                        "check": "file_exists",
                        "args": {"basenames": ["FOO"], "extensions": [".md"], "locations": [""]},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    rc, out = _run(["conventions", str(repo), "--rule-pack", str(pack), "--json"])
    payload = json.loads(out)
    rule_ids = {f["rule"] for r in payload["reports"] for f in r["findings"]}
    assert "ORG001" in rule_ids
    assert rc == 2


def test_profile_and_rule_pack_compose(tmp_path: Path) -> None:
    """Both --profile and --rule-pack contribute findings in the same run."""
    repo = tmp_path / "repo"
    repo.mkdir()
    pack = tmp_path / "custom.json"
    pack.write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "id": "ORG002",
                        "name": "internal-marker",
                        "severity": "error",
                        "check": "file_exists",
                        "args": {
                            "basenames": ["INTERNAL"],
                            "extensions": [".md"],
                            "locations": [""],
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    rc, out = _run(
        [
            "conventions",
            str(repo),
            "--profile",
            "core",
            "--rule-pack",
            str(pack),
            "--json",
        ]
    )
    payload = json.loads(out)
    rule_ids = {f["rule"] for r in payload["reports"] for f in r["findings"]}
    assert "R001" in rule_ids  # core fires
    assert "ORG002" in rule_ids  # custom pack fires
    assert rc == 2


def test_legacy_path_unchanged_when_no_pack_flags(tmp_path: Path) -> None:
    """No --profile and no --rule-pack => identical behaviour to pre-Phase-2."""
    _well_formed(tmp_path)
    rc, out = _run(["conventions", str(tmp_path), "--json"])
    payload = json.loads(out)
    rule_ids = {f["rule"] for r in payload["reports"] for f in r["findings"]}
    assert not any(rid.startswith("R0") for rid in rule_ids)  # no JSON-rule findings
    # rc may be 0/1/2 depending on legacy rules — we only assert the JSON shape.
    assert rc in {0, 1, 2}


def test_fix_flag_applies_bundled_fixes(tmp_path: Path) -> None:
    """`gendia conventions . --no-legacy --profile core --fix` writes the missing files."""
    rc, _ = _run(["conventions", str(tmp_path), "--no-legacy", "--profile", "core", "--fix"])
    assert (tmp_path / "README.md").is_file()
    assert (tmp_path / ".gitignore").is_file()
    assert (tmp_path / ".editorconfig").is_file()
    # R004 (LICENSE) has no fix, so the operation still reports failure overall.
    assert rc == 2


def test_fix_dry_run_writes_nothing(tmp_path: Path) -> None:
    rc, out = _run(
        [
            "conventions",
            str(tmp_path),
            "--no-legacy",
            "--profile",
            "core",
            "--fix",
            "--dry-run",
        ]
    )
    assert not (tmp_path / "README.md").exists()
    assert not (tmp_path / ".gitignore").exists()
    assert "would-fix" in out
    assert rc == 2  # findings still present


def test_fix_json_includes_per_rule_results(tmp_path: Path) -> None:
    """`--fix --json` returns a top-level `fixes` array with per-rule outcomes."""
    rc, out = _run(
        [
            "conventions",
            str(tmp_path),
            "--no-legacy",
            "--profile",
            "core",
            "--fix",
            "--json",
        ]
    )
    payload = json.loads(out)
    assert "fixes" in payload, "JSON output must include a 'fixes' array when --fix is set"
    fixes = payload["fixes"]
    assert len(fixes) >= 3  # R001 + R007 + R010 (R009 doesn't fire on empty dir)
    actions = {f["rule"]: f["action"] for f in fixes}
    assert actions.get("R001") == "applied"
    # Schema check: each entry has the documented shape.
    for entry in fixes:
        assert set(entry.keys()) >= {"repo", "rule", "name", "action", "backup"}
        assert entry["action"] in {"applied", "would-fix", "no-op"}
    # Human stdout suppressed in JSON mode — no `fixed` / `would-fix` lines.
    assert "fixed     [" not in out
    assert "would-fix [" not in out
    assert rc in {0, 1, 2}


def test_fix_dry_run_json_uses_would_fix_action(tmp_path: Path) -> None:
    """`--fix --dry-run --json` reports `would-fix` for every fixable finding."""
    rc, out = _run(
        [
            "conventions",
            str(tmp_path),
            "--no-legacy",
            "--profile",
            "core",
            "--fix",
            "--dry-run",
            "--json",
        ]
    )
    payload = json.loads(out)
    actions = {entry["action"] for entry in payload["fixes"]}
    assert actions == {"would-fix"}, f"expected only would-fix, got {actions}"
    # Files NOT written (dry run).
    assert not (tmp_path / "README.md").exists()
    assert rc == 2  # findings still present


def test_fix_json_omits_fixes_when_apply_fix_off(tmp_path: Path) -> None:
    """Without --fix, the JSON payload has no `fixes` key (back-compat)."""
    rc, out = _run(
        [
            "conventions",
            str(tmp_path),
            "--no-legacy",
            "--profile",
            "core",
            "--json",
        ]
    )
    payload = json.loads(out)
    assert "fixes" not in payload
    assert "reports" in payload
    assert rc == 2  # missing R001/R004/R007 still error


def test_fix_json_backup_flag_per_entry(tmp_path: Path) -> None:
    """When --backup is set and a mutating fix runs, that fix entry has backup=True."""
    (tmp_path / "package.json").write_text('{"name": "demo"}', encoding="utf-8")
    rc, out = _run(
        [
            "conventions",
            str(tmp_path),
            "--no-legacy",
            "--profile",
            "lang-node",
            "--fix",
            "--backup",
            "--json",
        ]
    )
    payload = json.loads(out)
    # JS007 (set_package_manager) is one of the mutating fixes that should run.
    js007 = next((f for f in payload["fixes"] if f["rule"] == "JS007"), None)
    assert js007 is not None
    assert js007["action"] == "applied"
    assert js007["backup"] is True
    assert (tmp_path / "package.json.bak").exists()
    assert rc == 0


def test_fix_backup_writes_bak_for_mutated_files(tmp_path: Path) -> None:
    """--fix --backup writes <name>.bak for the mutating package.json fixes."""
    original = json.dumps({"name": "demo"}, indent=2) + "\n"
    (tmp_path / "package.json").write_text(original, encoding="utf-8")
    rc, out = _run(
        [
            "conventions",
            str(tmp_path),
            "--no-legacy",
            "--profile",
            "lang-node",
            "--fix",
            "--backup",
        ]
    )
    # Backup captures the pre-fix snapshot.
    assert (tmp_path / "package.json.bak").read_text(encoding="utf-8") == original
    # Summary footer mentions .bak.
    assert ".bak backups written" in out
    # Live file is mutated.
    live = json.loads((tmp_path / "package.json").read_text(encoding="utf-8"))
    assert "packageManager" in live
    # lang-node has no error-severity rules and we're not --strict, so rc=0
    # even though JS005 (tsconfig) still warns.
    assert rc == 0


def test_bundled_profiles_constant_lists_all_packs() -> None:
    expected = {
        "core",
        "community",
        "governance",
        "platform-github",
        "platform-gitlab",
        "platform-bitbucket",
    }
    assert expected <= set(conv_op._BUNDLED_PROFILES)


def test_platform_github_profile_flags_missing_essentials(tmp_path: Path) -> None:
    rc, out = _run(
        [
            "conventions",
            str(tmp_path),
            "--profile",
            "platform-github",
            "--json",
        ]
    )
    payload = json.loads(out)
    rule_ids = {f["rule"] for r in payload["reports"] for f in r["findings"]}
    assert "GH001" in rule_ids  # .github/ missing
    assert rc in {0, 1, 2}


def test_multiple_profiles_compose(tmp_path: Path) -> None:
    """--profile is repeatable; rules from both packs fire."""
    rc, out = _run(
        [
            "conventions",
            str(tmp_path),
            "--no-legacy",
            "--profile",
            "core",
            "--profile",
            "lang-python",
            "--json",
        ]
    )
    payload = json.loads(out)
    rule_ids = {f["rule"] for r in payload["reports"] for f in r["findings"]}
    # Core fires R001 / R004 / R007 on empty dir.
    assert {"R001", "R004", "R007"} <= rule_ids
    # lang-python fires PY001 + PY004B on empty dir.
    assert "PY001" in rule_ids
    assert rc in {1, 2}


def test_list_rules_prints_loaded_rules() -> None:
    rc, out = _run(["conventions", "--list-rules", "--profile", "core"])
    assert rc == 0
    # Every core rule id must appear.
    for rid in ("R001", "R004", "R005", "R007", "R009", "R010"):
        assert rid in out


def test_list_rules_without_profile_errors() -> None:
    rc, _ = _run(["conventions", "--list-rules"])
    assert rc == 2


def test_explain_known_rule_prints_details() -> None:
    rc, out = _run(["conventions", "--explain", "R005", "--profile", "core"])
    assert rc == 0
    assert "R005" in out
    assert "SPDX" in out


def test_explain_unknown_rule_errors() -> None:
    rc, _ = _run(["conventions", "--explain", "NOT_A_RULE"])
    assert rc == 2


def test_explain_legacy_rule_falls_back_to_hardcoded_table() -> None:
    """--explain on a legacy rule ID (kebab-case) hits the hardcoded table."""
    rc, out = _run(["conventions", "--explain", "github-special"])
    assert rc == 0
    assert "github-special" in out
    assert "legacy hardcoded rule" in out
    assert "LICENSE" in out  # remediation mentions required defaults


def test_explain_legacy_md_kebab_case() -> None:
    rc, out = _run(["conventions", "--explain", "md-kebab-case"])
    assert rc == 0
    assert "lowercase-kebab-case" in out


def test_explain_legacy_all_eleven_resolve() -> None:
    """Every legacy rule ID emitted by the 10 _check_* functions has an entry."""
    legacy_ids = (
        "github-special",
        "md-kebab-case",
        "forbidden-chars",
        "readme-frontmatter",
        "decorator-emojis",
        "spec-date-prefix",
        "spec-number-prefix",
        "subfolder-readme",
        "stale-link-pattern",
        "sh-shebang",
        "trailing-whitespace",
    )
    for rule_id in legacy_ids:
        rc, _ = _run(["conventions", "--explain", rule_id])
        assert rc == 0, f"--explain {rule_id} should resolve via the legacy table"


def test_no_legacy_runs_only_json_rules(tmp_path: Path) -> None:
    """--no-legacy + --profile core => only R-rules fire, no legacy findings."""
    rc, out = _run(
        [
            "conventions",
            str(tmp_path),
            "--no-legacy",
            "--profile",
            "core",
            "--json",
        ]
    )
    payload = json.loads(out)
    rule_ids = {f["rule"] for r in payload["reports"] for f in r["findings"]}
    # All findings should be from JSON-rule space (R*), none from legacy
    # (which uses kebab-style rule names like "github-special").
    assert rule_ids
    assert all(rid.startswith("R") for rid in rule_ids), f"unexpected legacy ids: {rule_ids}"
    assert rc in {1, 2}  # at least R001 / R004 / R007 fire on empty dir


def test_no_legacy_without_packs_errors(tmp_path: Path) -> None:
    """--no-legacy alone with no JSON rules => nothing to run, exit 2."""
    rc, _ = _run(["conventions", str(tmp_path), "--no-legacy"])
    assert rc == 2


def test_community_profile_flags_missing_security(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# x\n", encoding="utf-8")
    rc, out = _run(
        [
            "conventions",
            str(tmp_path),
            "--profile",
            "community",
            "--json",
        ]
    )
    payload = json.loads(out)
    rule_ids = {f["rule"] for r in payload["reports"] for f in r["findings"]}
    assert {"H001", "H002", "H003"} <= rule_ids
    assert rc in {0, 1, 2}
