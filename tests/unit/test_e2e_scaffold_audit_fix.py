"""End-to-end pipeline tests: scaffold → audit → fix → re-audit clean.

These tests exercise the full happy path across three subsystems (init,
conventions, fix) and catch regressions where a change in one layer breaks
the pipeline for users running the CLI. Unit tests cover each layer in
isolation; these tests cover the seams.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

from gendia.cli.arguments import build_parser, dispatch


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


def test_e2e_bare_scaffold_then_audit_clean(tmp_path: Path) -> None:
    """The `bare` scaffold's output passes the core audit out of the box.

    Regresses if scaffold templates drift away from what `core` enforces.
    """
    target = tmp_path / "demo"
    rc, _ = _run(
        [
            "init",
            "--scaffold",
            "bare",
            "--target",
            str(target),
            "--var",
            "name=demo",
            "--var",
            "description=Demo",
            "--var",
            "license=MIT",
            "--var",
            "author_name=Test User",
            "--var",
            "author_email=test@example.com",
        ]
    )
    assert rc == 0
    # Now audit it.
    rc, out = _run(["conventions", str(target), "--no-legacy", "--profile", "core", "--json"])
    payload = json.loads(out)
    rule_ids = {f["rule"] for r in payload["reports"] for f in r["findings"]}
    # `bare` writes everything core demands; no R-findings should fire.
    assert not any(rid.startswith("R") for rid in rule_ids), (
        f"bare scaffold should pass core audit, but these rules fired: {rule_ids}"
    )
    assert rc == 0


def test_e2e_empty_dir_audit_then_fix_then_clean(tmp_path: Path) -> None:
    """Audit a fresh empty dir, apply --fix, re-audit, expect the fixable rules cleared."""
    # Phase 1: audit a totally empty dir — R001/R004/R007/R010 fire.
    rc, out = _run(["conventions", str(tmp_path), "--no-legacy", "--profile", "core", "--json"])
    payload = json.loads(out)
    before = {f["rule"] for r in payload["reports"] for f in r["findings"]}
    assert {"R001", "R007", "R010"} <= before  # rules that have fix kinds
    assert rc == 2

    # Phase 2: apply --fix.
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
    applied = {f["rule"] for f in payload["fixes"] if f["action"] == "applied"}
    assert {"R001", "R007", "R010"} <= applied

    # Phase 3: re-audit, expect those rules to no longer fire.
    rc, out = _run(["conventions", str(tmp_path), "--no-legacy", "--profile", "core", "--json"])
    payload = json.loads(out)
    after = {f["rule"] for r in payload["reports"] for f in r["findings"]}
    assert "R001" not in after, "README should now exist"
    assert "R007" not in after, ".gitignore should now exist"
    assert "R010" not in after, ".editorconfig should now exist"
    # R004 (LICENSE) has no fix and must still flag.
    assert "R004" in after
    assert rc == 2  # one error remains


def test_e2e_lang_node_scaffold_then_lang_node_audit_no_mutating_fixes_needed(
    tmp_path: Path,
) -> None:
    """The `node-pnpm` scaffold's package.json is already well-formed.

    A subsequent `gendia conventions . --profile lang-node --fix` should find
    nothing to fix — the JSON-mutating fixes (set_engines_node /
    set_package_manager / set_package_name / set_package_version /
    set_package_license) are all idempotent no-ops on output from this
    scaffold.
    """
    target = tmp_path / "demo-pkg"
    rc, _ = _run(
        [
            "init",
            "--scaffold",
            "node-pnpm",
            "--target",
            str(target),
            "--var",
            "name=demo-pkg",
            "--var",
            "description=Demo",
        ]
    )
    assert rc == 0
    rc, out = _run(
        [
            "conventions",
            str(target),
            "--no-legacy",
            "--profile",
            "lang-node",
            "--fix",
            "--json",
        ]
    )
    payload = json.loads(out)
    # All JS002 + JS004 + JS007 fixes should report no-op (or not appear at all,
    # if the rule didn't even fire).
    mutating_fixes = {
        f["rule"]: f["action"]
        for f in payload["fixes"]
        if f["rule"] in {"JS002A", "JS002B", "JS002C", "JS004", "JS007"}
    }
    for rule_id, action in mutating_fixes.items():
        assert action == "no-op", (
            f"scaffold output should already satisfy {rule_id}, got action={action!r}"
        )
    # No .bak files should be created when every mutating fix is a no-op.
    assert not (target / "package.json.bak").exists()


def test_e2e_full_pack_set_one_pass_fix_then_clean(tmp_path: Path) -> None:
    """Combined --fix across core+community+platform-github+supply-chain writes
    10+ files and then the same audit re-runs cleanly for all those rules.
    """
    # Phase 1: apply all fixes against an empty dir.
    profiles = ["core", "community", "platform-github", "supply-chain"]
    argv = ["conventions", str(tmp_path), "--no-legacy"]
    for p in profiles:
        argv += ["--profile", p]
    argv += ["--fix", "--json"]
    rc, out = _run(argv)
    payload = json.loads(out)
    applied_rule_ids = {f["rule"] for f in payload["fixes"] if f["action"] == "applied"}
    # At least the 8 community/platform/supply-chain fix kinds + R001/R007/R010 from core.
    expected_applied = {
        "R001",
        "R007",
        "R010",
        "H001",
        "H002",
        "H003",
        "H006",
        "GH003",
        "GH004",
        "GH006",
        "SUPPLY005",
        "SUPPLY007",
        "SUPPLY008",
    }
    assert expected_applied <= applied_rule_ids, (
        f"missing applied rules: {expected_applied - applied_rule_ids}"
    )

    # Phase 2: re-audit — those fixable rules should all be cleared.
    rc, out = _run(
        [
            "conventions",
            str(tmp_path),
            "--no-legacy",
            *sum([["--profile", p] for p in profiles], []),
            "--json",
        ]
    )
    payload = json.loads(out)
    after = {f["rule"] for r in payload["reports"] for f in r["findings"]}
    for cleared in expected_applied:
        assert cleared not in after, f"{cleared} should have been cleared by --fix"
