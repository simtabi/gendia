"""Standards rule engine — schema, loaders, check kinds, runner."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gendia import standards
from gendia.standards import (
    Rule,
    _detect_spdx_from_license,
    _slugify_npm_name,
    apply_fix,
    load_bundled_pack,
    load_rule_pack,
    run_rules,
)

# --- schema -----------------------------------------------------------------


def test_rule_from_dict_minimum() -> None:
    r = Rule.from_dict({"id": "X1", "check": "file_exists"})
    assert r.id == "X1"
    assert r.name == "X1"  # falls back to id when name absent
    assert r.severity == "warn"  # default normalised from "info"
    assert r.check == "file_exists"
    assert r.args == {}


def test_rule_severity_normalisation() -> None:
    assert Rule.from_dict({"id": "X", "check": "k", "severity": "error"}).severity == "error"
    assert Rule.from_dict({"id": "X", "check": "k", "severity": "warning"}).severity == "warn"
    assert Rule.from_dict({"id": "X", "check": "k", "severity": "info"}).severity == "warn"


def test_rule_from_dict_rejects_missing_required() -> None:
    with pytest.raises(ValueError, match="id"):
        Rule.from_dict({"check": "file_exists"})
    with pytest.raises(ValueError, match="check"):
        Rule.from_dict({"id": "X"})


def test_rule_inherits_default_category() -> None:
    r = Rule.from_dict({"id": "X", "check": "k"}, default_category="essential-files")
    assert r.category == "essential-files"
    r2 = Rule.from_dict({"id": "X", "check": "k", "category": "explicit"}, default_category="x")
    assert r2.category == "explicit"


# --- bundled pack -----------------------------------------------------------


def test_bundled_core_loads() -> None:
    rules = load_bundled_pack("core")
    assert len(rules) >= 6
    ids = {r.id for r in rules}
    assert {"R001", "R004", "R005", "R007", "R009", "R010"} <= ids


def test_bundled_unknown_pack_returns_empty() -> None:
    assert load_bundled_pack("does-not-exist") == ()


def test_pack_invalid_json_returns_empty(tmp_path: Path) -> None:
    p = tmp_path / "broken.json"
    p.write_text("{not json", encoding="utf-8")
    assert load_rule_pack(p) == ()


def test_pack_with_malformed_rule_skips_it(tmp_path: Path) -> None:
    p = tmp_path / "mixed.json"
    p.write_text(
        json.dumps({"rules": [{"id": "GOOD", "check": "file_exists"}, {"oops": True}]}),
        encoding="utf-8",
    )
    rules = load_rule_pack(p)
    assert {r.id for r in rules} == {"GOOD"}


# --- check_file_exists ------------------------------------------------------


def _readme(tmp_path: Path) -> Path:
    p = tmp_path / "README.md"
    p.write_text("# hi\n", encoding="utf-8")
    return p


def test_file_exists_passes_when_present(tmp_path: Path) -> None:
    _readme(tmp_path)
    rules = load_bundled_pack("core")
    findings = run_rules(tmp_path, [r for r in rules if r.id == "R001"])
    assert findings == ()


def test_file_exists_fails_when_absent(tmp_path: Path) -> None:
    rules = load_bundled_pack("core")
    findings = run_rules(tmp_path, [r for r in rules if r.id == "R001"])
    assert len(findings) == 1
    assert findings[0].rule == "R001"
    assert findings[0].severity == "error"


def test_file_exists_searches_alternate_locations(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "README.md").write_text("# hi\n", encoding="utf-8")
    rules = load_bundled_pack("core")
    findings = run_rules(tmp_path, [r for r in rules if r.id == "R001"])
    assert findings == ()  # docs/ is one of the allowed locations


# --- check_regex_in_file ----------------------------------------------------


def test_regex_in_file_passes_when_pattern_matches(tmp_path: Path) -> None:
    (tmp_path / ".gitattributes").write_text("* text=auto\n", encoding="utf-8")
    rules = load_bundled_pack("core")
    findings = run_rules(tmp_path, [r for r in rules if r.id == "R009"])
    assert findings == ()


def test_regex_in_file_fails_when_pattern_missing(tmp_path: Path) -> None:
    (tmp_path / ".gitattributes").write_text("# nothing useful\n", encoding="utf-8")
    rules = load_bundled_pack("core")
    findings = run_rules(tmp_path, [r for r in rules if r.id == "R009"])
    assert len(findings) == 1
    assert findings[0].rule == "R009"


def test_regex_in_file_skips_when_target_absent(tmp_path: Path) -> None:
    """R009 doesn't fire when .gitattributes itself is missing — applies_when gates it."""
    rules = load_bundled_pack("core")
    findings = run_rules(tmp_path, [r for r in rules if r.id == "R009"])
    assert findings == ()


# --- check_license_spdx -----------------------------------------------------


def test_license_spdx_passes_for_recognised_text(tmp_path: Path) -> None:
    (tmp_path / "LICENSE").write_text("MIT License\n\nCopyright …\n", encoding="utf-8")
    rules = load_bundled_pack("core")
    findings = run_rules(tmp_path, [r for r in rules if r.id == "R005"])
    assert findings == ()


def test_license_spdx_flags_unrecognised_text(tmp_path: Path) -> None:
    (tmp_path / "LICENSE").write_text("Custom internal license, do not share.\n", encoding="utf-8")
    rules = load_bundled_pack("core")
    findings = run_rules(tmp_path, [r for r in rules if r.id == "R005"])
    assert len(findings) == 1
    assert findings[0].rule == "R005"
    assert findings[0].severity == "warn"


def test_license_spdx_skipped_when_no_license_file(tmp_path: Path) -> None:
    rules = load_bundled_pack("core")
    findings = run_rules(tmp_path, [r for r in rules if r.id == "R005"])
    assert findings == ()  # applies_when gated it


# --- runner: end-to-end on a "good" repo ------------------------------------


def test_well_formed_repo_passes_all_core_rules(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# hi\n", encoding="utf-8")
    (tmp_path / "LICENSE").write_text("MIT License\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("*.pyc\n", encoding="utf-8")
    (tmp_path / ".gitattributes").write_text("* text=auto\n", encoding="utf-8")
    (tmp_path / ".editorconfig").write_text("root = true\n", encoding="utf-8")
    findings = run_rules(tmp_path, load_bundled_pack("core"))
    assert findings == ()


def test_empty_repo_fails_required_rules(tmp_path: Path) -> None:
    findings = run_rules(tmp_path, load_bundled_pack("core"))
    rule_ids = {f.rule for f in findings}
    # Required (error) rules fire; advisory ones (R005, R009) gate on prerequisites.
    assert "R001" in rule_ids
    assert "R004" in rule_ids
    assert "R007" in rule_ids
    assert all(f.severity in {"error", "warn"} for f in findings)


# --- error handling: unknown check kind, raising check ----------------------


def test_unknown_check_kind_surfaces_as_finding(tmp_path: Path) -> None:
    bogus = Rule(id="X", name="x", severity="warn", check="not_a_real_check")
    findings = run_rules(tmp_path, [bogus])
    assert len(findings) == 1
    assert findings[0].rule == "X"
    assert "unknown check kind" in findings[0].message


def test_check_exception_is_caught(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_root: Path, rule: Rule) -> None:
        raise RuntimeError("kaboom")

    monkeypatch.setitem(standards._CHECK_KINDS, "boom", boom)
    bogus = Rule(id="X", name="x", severity="warn", check="boom")
    findings = run_rules(tmp_path, [bogus])
    assert len(findings) == 1
    assert "kaboom" in findings[0].message


# --- check_directory_exists ------------------------------------------------


def test_directory_exists_passes_when_present(tmp_path: Path) -> None:
    (tmp_path / ".github").mkdir()
    rule = Rule(
        id="X",
        name="x",
        severity="warn",
        check="directory_exists",
        args={"paths": [".github"]},
    )
    assert run_rules(tmp_path, [rule]) == ()


def test_directory_exists_fails_when_absent(tmp_path: Path) -> None:
    rule = Rule(
        id="X",
        name="x",
        severity="warn",
        check="directory_exists",
        args={"paths": [".github", ".gitlab"]},
    )
    findings = run_rules(tmp_path, [rule])
    assert len(findings) == 1
    assert findings[0].rule == "X"


def test_directory_exists_does_not_match_a_file(tmp_path: Path) -> None:
    """A file with the listed name shouldn't satisfy a directory_exists check."""
    (tmp_path / ".github").write_text("not a dir", encoding="utf-8")
    rule = Rule(
        id="X",
        name="x",
        severity="warn",
        check="directory_exists",
        args={"paths": [".github"]},
    )
    findings = run_rules(tmp_path, [rule])
    assert len(findings) == 1


# --- bundled packs ----------------------------------------------------------


def test_all_bundled_packs_load() -> None:
    expected = {
        "core": 6,
        "community": 9,
        "governance": 5,
        "platform-github": 4,
        "platform-gitlab": 1,
        "platform-bitbucket": 1,
        "lang-python": 4,
        "lang-node": 10,
        "lang-rust": 3,
        "lang-go": 4,
        "web-and-api": 5,
        "os-packaging": 4,
        "supply-chain": 4,
        "env-loaders": 4,
        "migrations": 4,
        "task-runners": 2,
    }
    for name, min_count in expected.items():
        rules = load_bundled_pack(name)
        assert len(rules) >= min_count, f"{name}: expected ≥{min_count} rules, got {len(rules)}"


def test_file_absent_passes_when_none_present(tmp_path: Path) -> None:
    rule = Rule(
        id="X",
        name="x",
        severity="error",
        check="file_absent",
        args={"paths": [".env", "secret.pem"]},
    )
    assert run_rules(tmp_path, [rule]) == ()


def test_file_absent_fails_when_any_present(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("SECRET=1\n", encoding="utf-8")
    rule = Rule(
        id="X",
        name="x",
        severity="error",
        check="file_absent",
        args={"paths": [".env", "secret.pem"]},
    )
    findings = run_rules(tmp_path, [rule])
    assert len(findings) == 1
    assert findings[0].rule == "X"
    assert findings[0].severity == "error"
    assert ".env" in findings[0].message


def test_env_loaders_flags_tracked_dotenv(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("SECRET=1\n", encoding="utf-8")
    findings = run_rules(tmp_path, load_bundled_pack("env-loaders"))
    rule_ids = {f.rule for f in findings}
    assert "DENV001" in rule_ids


def test_env_loaders_passes_clean_repo(tmp_path: Path) -> None:
    (tmp_path / ".env.example").write_text("SECRET=\n", encoding="utf-8")
    findings = run_rules(tmp_path, load_bundled_pack("env-loaders"))
    rule_ids = {f.rule for f in findings}
    assert "DENV001" not in rule_ids  # no tracked .env
    assert "DENV002" not in rule_ids  # .env.example exists


def test_lang_python_passes_on_well_formed_python_project(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="x"\nversion="0.1.0"\nrequires-python=">=3.12"\n',
        encoding="utf-8",
    )
    (tmp_path / ".python-version").write_text("3.12\n", encoding="utf-8")
    findings = run_rules(tmp_path, load_bundled_pack("lang-python"))
    assert findings == ()


def test_lang_node_flags_missing_package_manager_field(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        '{"name": "x", "version": "0.1.0", "license": "MIT"}\n',
        encoding="utf-8",
    )
    findings = run_rules(tmp_path, load_bundled_pack("lang-node"))
    rule_ids = {f.rule for f in findings}
    assert "JS007" in rule_ids  # packageManager missing
    assert "JS004" in rule_ids  # engines.node missing
    # JS001/002A/002B/002C should NOT fire — package.json is fine.
    assert "JS001" not in rule_ids
    assert "JS002A" not in rule_ids


def test_lang_node_flags_missing_essentials_when_no_package_json(tmp_path: Path) -> None:
    findings = run_rules(tmp_path, load_bundled_pack("lang-node"))
    rule_ids = {f.rule for f in findings}
    assert "JS001" in rule_ids
    # Regex-based rules silently skip when their target file is missing.
    assert "JS007" not in rule_ids


def test_lang_go_flags_missing_go_directive(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module example.com/x\n", encoding="utf-8")
    findings = run_rules(tmp_path, load_bundled_pack("lang-go"))
    rule_ids = {f.rule for f in findings}
    assert "GO003" in rule_ids
    assert "GO002" in rule_ids  # go.sum missing


# --- check_glob_exists -----------------------------------------------------


def test_glob_exists_passes_when_match_found(tmp_path: Path) -> None:
    (tmp_path / "a.spec").write_text("Name: pkg\n", encoding="utf-8")
    rule = Rule(
        id="X",
        name="x",
        severity="warn",
        check="glob_exists",
        args={"pattern": "*.spec", "base_dir": "", "recursive": False},
    )
    assert run_rules(tmp_path, [rule]) == ()


def test_glob_exists_fails_when_no_match(tmp_path: Path) -> None:
    rule = Rule(
        id="X",
        name="x",
        severity="warn",
        check="glob_exists",
        args={"pattern": "*.spec", "base_dir": "", "recursive": False},
    )
    findings = run_rules(tmp_path, [rule])
    assert len(findings) == 1
    assert findings[0].rule == "X"


def test_glob_exists_recursive_finds_deep_files(tmp_path: Path) -> None:
    deep = tmp_path / "alembic" / "versions"
    deep.mkdir(parents=True)
    (deep / "0001_init.py").write_text("# migration\n", encoding="utf-8")
    rule = Rule(
        id="X",
        name="x",
        severity="warn",
        check="glob_exists",
        args={"pattern": "*.py", "base_dir": "alembic/versions", "recursive": False},
    )
    assert run_rules(tmp_path, [rule]) == ()


def test_glob_exists_skips_when_base_dir_missing(tmp_path: Path) -> None:
    """When base_dir doesn't exist, the rule didn't apply (no finding)."""
    rule = Rule(
        id="X",
        name="x",
        severity="warn",
        check="glob_exists",
        args={"pattern": "*.py", "base_dir": "missing/", "recursive": True},
    )
    assert run_rules(tmp_path, [rule]) == ()


# --- check_one_of_files ----------------------------------------------------


def test_one_of_files_exactly_one_passes(tmp_path: Path) -> None:
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    rule = Rule(
        id="X",
        name="x",
        severity="warn",
        check="one_of_files",
        args={
            "groups": [["package-lock.json"], ["yarn.lock"], ["pnpm-lock.yaml"]],
            "expected": "exactly_one",
        },
    )
    assert run_rules(tmp_path, [rule]) == ()


def test_one_of_files_exactly_one_fails_when_two(tmp_path: Path) -> None:
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    (tmp_path / "yarn.lock").write_text("{}", encoding="utf-8")
    rule = Rule(
        id="X",
        name="x",
        severity="warn",
        check="one_of_files",
        args={
            "groups": [["package-lock.json"], ["yarn.lock"], ["pnpm-lock.yaml"]],
            "expected": "exactly_one",
        },
    )
    findings = run_rules(tmp_path, [rule])
    assert len(findings) == 1
    assert "2 group(s)" in findings[0].message


def test_one_of_files_at_most_one_allows_zero(tmp_path: Path) -> None:
    rule = Rule(
        id="X",
        name="x",
        severity="warn",
        check="one_of_files",
        args={"paths": ["Makefile", "Justfile", "Taskfile.yml"], "expected": "at_most_one"},
    )
    assert run_rules(tmp_path, [rule]) == ()


def test_one_of_files_at_most_one_fails_when_two(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text("\n", encoding="utf-8")
    (tmp_path / "Justfile").write_text("\n", encoding="utf-8")
    rule = Rule(
        id="X",
        name="x",
        severity="warn",
        check="one_of_files",
        args={"paths": ["Makefile", "Justfile", "Taskfile.yml"], "expected": "at_most_one"},
    )
    findings = run_rules(tmp_path, [rule])
    assert len(findings) == 1


def test_one_of_files_at_least_one(tmp_path: Path) -> None:
    rule = Rule(
        id="X",
        name="x",
        severity="warn",
        check="one_of_files",
        args={"paths": ["Makefile", "Justfile"], "expected": "at_least_one"},
    )
    # No runner files yet — should fire.
    assert len(run_rules(tmp_path, [rule])) == 1
    (tmp_path / "Makefile").write_text("\n", encoding="utf-8")
    assert run_rules(tmp_path, [rule]) == ()


# --- new packs: migrations + task-runners ---------------------------------


def test_migrations_pack_detects_alembic(tmp_path: Path) -> None:
    (tmp_path / "alembic.ini").write_text("[alembic]\n", encoding="utf-8")
    (tmp_path / "alembic" / "versions").mkdir(parents=True)
    findings = run_rules(tmp_path, load_bundled_pack("migrations"))
    rule_ids = {f.rule for f in findings}
    # MIG004 fires because alembic.ini exists but no migrations yet.
    assert "MIG004" in rule_ids


def test_task_runners_pack_flags_two_runners(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text("\n", encoding="utf-8")
    (tmp_path / "Justfile").write_text("\n", encoding="utf-8")
    findings = run_rules(tmp_path, load_bundled_pack("task-runners"))
    rule_ids = {f.rule for f in findings}
    assert "TASK001" in rule_ids


def test_task_runners_pack_flags_no_runner(tmp_path: Path) -> None:
    findings = run_rules(tmp_path, load_bundled_pack("task-runners"))
    rule_ids = {f.rule for f in findings}
    assert "TASK002" in rule_ids


# --- lang-node JS003 (lockfile uniqueness) --------------------------------


def test_lang_node_flags_multiple_lockfiles(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"name":"x","version":"1"}', encoding="utf-8")
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    (tmp_path / "yarn.lock").write_text("\n", encoding="utf-8")
    findings = run_rules(tmp_path, load_bundled_pack("lang-node"))
    rule_ids = {f.rule for f in findings}
    assert "JS003" in rule_ids


def test_lang_rust_flags_missing_rustfmt(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text('[package]\nname="x"\n', encoding="utf-8")
    findings = run_rules(tmp_path, load_bundled_pack("lang-rust"))
    rule_ids = {f.rule for f in findings}
    assert "RUST003" in rule_ids
    assert "RUST004" in rule_ids


# --- fix kinds ------------------------------------------------------------


def test_apply_fix_returns_false_without_fix(tmp_path: Path) -> None:
    rule = Rule(id="X", name="x", severity="warn", check="file_exists")
    assert apply_fix(tmp_path, rule) is False


def test_apply_fix_unknown_kind_returns_false(tmp_path: Path) -> None:
    rule = Rule(
        id="X",
        name="x",
        severity="warn",
        check="file_exists",
        fix="does_not_exist",
    )
    assert apply_fix(tmp_path, rule) is False


def test_fix_create_default_readme(tmp_path: Path) -> None:
    rule = Rule(id="X", name="x", severity="warn", check="file_exists", fix="create_default_readme")
    assert apply_fix(tmp_path, rule) is True
    readme = tmp_path / "README.md"
    assert readme.is_file()
    assert tmp_path.name in readme.read_text(encoding="utf-8")


def test_fix_create_default_readme_no_op_if_exists(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# preserved\n", encoding="utf-8")
    rule = Rule(id="X", name="x", severity="warn", check="file_exists", fix="create_default_readme")
    assert apply_fix(tmp_path, rule) is False
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "# preserved\n"


def test_fix_create_default_gitignore(tmp_path: Path) -> None:
    rule = Rule(
        id="X", name="x", severity="warn", check="file_exists", fix="create_default_gitignore"
    )
    assert apply_fix(tmp_path, rule) is True
    text = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert ".DS_Store" in text
    assert ".env" in text


def test_fix_create_default_editorconfig(tmp_path: Path) -> None:
    rule = Rule(
        id="X", name="x", severity="warn", check="file_exists", fix="create_default_editorconfig"
    )
    assert apply_fix(tmp_path, rule) is True
    text = (tmp_path / ".editorconfig").read_text(encoding="utf-8")
    assert "root = true" in text


def test_fix_add_text_auto_creates_when_missing(tmp_path: Path) -> None:
    rule = Rule(
        id="X",
        name="x",
        severity="warn",
        check="regex_in_file",
        fix="add_text_auto_to_gitattributes",
    )
    assert apply_fix(tmp_path, rule) is True
    assert (tmp_path / ".gitattributes").read_text(encoding="utf-8") == "* text=auto\n"


def test_fix_add_text_auto_prepends_to_existing(tmp_path: Path) -> None:
    (tmp_path / ".gitattributes").write_text("*.png binary\n", encoding="utf-8")
    rule = Rule(
        id="X",
        name="x",
        severity="warn",
        check="regex_in_file",
        fix="add_text_auto_to_gitattributes",
    )
    assert apply_fix(tmp_path, rule) is True
    text = (tmp_path / ".gitattributes").read_text(encoding="utf-8")
    assert text.startswith("* text=auto\n")
    assert "*.png binary" in text


def test_fix_add_text_auto_no_op_when_already_present(tmp_path: Path) -> None:
    (tmp_path / ".gitattributes").write_text("* text=auto\n*.png binary\n", encoding="utf-8")
    rule = Rule(
        id="X",
        name="x",
        severity="warn",
        check="regex_in_file",
        fix="add_text_auto_to_gitattributes",
    )
    assert apply_fix(tmp_path, rule) is False


def test_core_rules_with_fixes_idempotent(tmp_path: Path) -> None:
    """After applying every R-rule's fix, re-running should clear R001/R007/R010."""
    rules = [r for r in load_bundled_pack("core") if r.fix]
    for rule in rules:
        apply_fix(tmp_path, rule)
    findings = run_rules(tmp_path, load_bundled_pack("core"))
    rule_ids = {f.rule for f in findings}
    assert "R001" not in rule_ids
    assert "R007" not in rule_ids
    assert "R010" not in rule_ids


def test_module_function_check_resolves(tmp_path: Path) -> None:
    """`module:function` form lets a rule reference an importable callable."""
    rule = Rule(
        id="EXTRA",
        name="extra",
        severity="warn",
        check="gendia.standards:check_file_exists",
        args={"basenames": ["NEVER"], "extensions": [""], "locations": [""]},
    )
    findings = run_rules(tmp_path, [rule])
    assert len(findings) == 1
    assert findings[0].rule == "EXTRA"


# --- new fix kinds: npmrc / dependabot / codeql -------------------------------


def test_fix_append_engine_strict_creates_npmrc_when_missing(tmp_path: Path) -> None:
    rule = Rule(
        id="JS008",
        name="x",
        severity="info",
        check="regex_in_file",
        fix="append_engine_strict_to_npmrc",
    )
    changed = apply_fix(tmp_path, rule)
    assert changed is True
    assert (tmp_path / ".npmrc").read_text(encoding="utf-8") == "engine-strict=true\n"


def test_fix_append_engine_strict_appends_to_existing(tmp_path: Path) -> None:
    (tmp_path / ".npmrc").write_text("ignore-scripts=true\n", encoding="utf-8")
    rule = Rule(
        id="JS008",
        name="x",
        severity="info",
        check="regex_in_file",
        fix="append_engine_strict_to_npmrc",
    )
    assert apply_fix(tmp_path, rule) is True
    body = (tmp_path / ".npmrc").read_text(encoding="utf-8")
    assert "ignore-scripts=true" in body
    assert "engine-strict=true" in body


def test_fix_append_engine_strict_idempotent_when_already_set(tmp_path: Path) -> None:
    (tmp_path / ".npmrc").write_text("engine-strict=true\n", encoding="utf-8")
    rule = Rule(
        id="JS008",
        name="x",
        severity="info",
        check="regex_in_file",
        fix="append_engine_strict_to_npmrc",
    )
    assert apply_fix(tmp_path, rule) is False  # already present
    # Body unchanged
    assert (tmp_path / ".npmrc").read_text(encoding="utf-8") == "engine-strict=true\n"


def test_fix_append_ignore_scripts_creates_npmrc(tmp_path: Path) -> None:
    rule = Rule(
        id="JS009",
        name="x",
        severity="info",
        check="regex_in_file",
        fix="append_ignore_scripts_to_npmrc",
    )
    assert apply_fix(tmp_path, rule) is True
    assert "ignore-scripts=true" in (tmp_path / ".npmrc").read_text(encoding="utf-8")


def test_fix_append_npmrc_refuses_symlink(tmp_path: Path) -> None:
    """Symlinked .npmrc must not be followed (root-relative writes only)."""
    real = tmp_path / "elsewhere.npmrc"
    real.write_text("# elsewhere\n", encoding="utf-8")
    (tmp_path / ".npmrc").symlink_to(real)
    rule = Rule(
        id="JS008",
        name="x",
        severity="info",
        check="regex_in_file",
        fix="append_engine_strict_to_npmrc",
    )
    assert apply_fix(tmp_path, rule) is False
    # Real file not modified
    assert "engine-strict" not in real.read_text(encoding="utf-8")


def test_fix_create_default_dependabot(tmp_path: Path) -> None:
    rule = Rule(
        id="GH003",
        name="x",
        severity="info",
        check="file_exists",
        fix="create_default_dependabot",
    )
    assert apply_fix(tmp_path, rule) is True
    body = (tmp_path / ".github" / "dependabot.yml").read_text(encoding="utf-8")
    assert "version: 2" in body
    assert "github-actions" in body


def test_fix_create_default_dependabot_skips_when_renovate_present(tmp_path: Path) -> None:
    (tmp_path / "renovate.json").write_text("{}", encoding="utf-8")
    rule = Rule(
        id="GH003",
        name="x",
        severity="info",
        check="file_exists",
        fix="create_default_dependabot",
    )
    assert apply_fix(tmp_path, rule) is False
    assert not (tmp_path / ".github" / "dependabot.yml").exists()


def test_fix_create_default_codeql_workflow(tmp_path: Path) -> None:
    rule = Rule(
        id="SUPPLY005",
        name="x",
        severity="info",
        check="file_exists",
        fix="create_default_codeql_workflow",
    )
    assert apply_fix(tmp_path, rule) is True
    body = (tmp_path / ".github" / "workflows" / "codeql.yml").read_text(encoding="utf-8")
    assert "github/codeql-action/init" in body
    assert "security-events: write" in body


def test_fix_create_default_codeql_workflow_idempotent(tmp_path: Path) -> None:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "codeql.yaml").write_text("# existing\n", encoding="utf-8")
    rule = Rule(
        id="SUPPLY005",
        name="x",
        severity="info",
        check="file_exists",
        fix="create_default_codeql_workflow",
    )
    assert apply_fix(tmp_path, rule) is False


# --- RUST002: Cargo.lock for binary crates -----------------------------------


def test_rust002_skips_library_only_crates(tmp_path: Path) -> None:
    """Library-only crates (src/lib.rs without src/main.rs) should not flag."""
    (tmp_path / "Cargo.toml").write_text("[package]\nname = 'demo'\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text("// lib\n", encoding="utf-8")
    findings = run_rules(tmp_path, load_bundled_pack("lang-rust"))
    rule_ids = {f.rule for f in findings if f.severity != "ok"}
    assert "RUST002" not in rule_ids


def test_rust002_flags_binary_crate_without_lockfile(tmp_path: Path) -> None:
    """Binary crates (src/main.rs present) without Cargo.lock should flag."""
    (tmp_path / "Cargo.toml").write_text("[package]\nname = 'demo'\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
    findings = run_rules(tmp_path, load_bundled_pack("lang-rust"))
    failing = {f.rule for f in findings if f.severity != "ok"}
    assert "RUST002" in failing


def test_rust002_passes_when_lockfile_present(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text("[package]\nname = 'demo'\n", encoding="utf-8")
    (tmp_path / "Cargo.lock").write_text("# lock\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
    findings = run_rules(tmp_path, load_bundled_pack("lang-rust"))
    failing = {f.rule for f in findings if f.severity != "ok"}
    assert "RUST002" not in failing


# --- community pack create-only fixes ---------------------------------------


def test_fix_create_default_contributing(tmp_path: Path) -> None:
    rule = Rule(
        id="H001",
        name="x",
        severity="warn",
        check="file_exists",
        fix="create_default_contributing",
    )
    assert apply_fix(tmp_path, rule) is True
    body = (tmp_path / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "Conventional Commits" in body
    # Idempotent
    assert apply_fix(tmp_path, rule) is False


def test_fix_create_default_contributing_skips_when_dotgithub_variant_exists(
    tmp_path: Path,
) -> None:
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "CONTRIBUTING.md").write_text("hi", encoding="utf-8")
    rule = Rule(
        id="H001",
        name="x",
        severity="warn",
        check="file_exists",
        fix="create_default_contributing",
    )
    assert apply_fix(tmp_path, rule) is False
    assert not (tmp_path / "CONTRIBUTING.md").exists()


def test_fix_create_default_code_of_conduct(tmp_path: Path) -> None:
    rule = Rule(
        id="H002",
        name="x",
        severity="warn",
        check="file_exists",
        fix="create_default_code_of_conduct",
    )
    assert apply_fix(tmp_path, rule) is True
    body = (tmp_path / "CODE_OF_CONDUCT.md").read_text(encoding="utf-8")
    assert "Contributor Covenant" in body


def test_fix_create_default_security(tmp_path: Path) -> None:
    rule = Rule(
        id="H003",
        name="x",
        severity="warn",
        check="file_exists",
        fix="create_default_security",
    )
    assert apply_fix(tmp_path, rule) is True
    body = (tmp_path / "SECURITY.md").read_text(encoding="utf-8")
    assert "How to report a vulnerability" in body


def test_fix_create_default_changelog(tmp_path: Path) -> None:
    rule = Rule(
        id="H006",
        name="x",
        severity="warn",
        check="file_exists",
        fix="create_default_changelog",
    )
    assert apply_fix(tmp_path, rule) is True
    body = (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "Keep a Changelog" in body
    assert "## [Unreleased]" in body


def test_fix_create_default_codeowners(tmp_path: Path) -> None:
    rule = Rule(
        id="GH004",
        name="x",
        severity="info",
        check="file_exists",
        fix="create_default_codeowners",
    )
    assert apply_fix(tmp_path, rule) is True
    body = (tmp_path / ".github" / "CODEOWNERS").read_text(encoding="utf-8")
    assert "@TODO-owning-team" in body


def test_fix_create_default_pr_template(tmp_path: Path) -> None:
    rule = Rule(
        id="GH006",
        name="x",
        severity="info",
        check="file_exists",
        fix="create_default_pr_template",
    )
    assert apply_fix(tmp_path, rule) is True
    body = (tmp_path / ".github" / "PULL_REQUEST_TEMPLATE.md").read_text(encoding="utf-8")
    assert "## Summary" in body
    assert "## Test plan" in body


def test_fix_create_default_pr_template_skips_when_lowercase_variant_exists(tmp_path: Path) -> None:
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "pull_request_template.md").write_text("ok", encoding="utf-8")
    rule = Rule(
        id="GH006",
        name="x",
        severity="info",
        check="file_exists",
        fix="create_default_pr_template",
    )
    assert apply_fix(tmp_path, rule) is False


def test_fix_create_default_scorecard_workflow(tmp_path: Path) -> None:
    rule = Rule(
        id="SUPPLY007",
        name="x",
        severity="info",
        check="file_exists",
        fix="create_default_scorecard_workflow",
    )
    assert apply_fix(tmp_path, rule) is True
    body = (tmp_path / ".github" / "workflows" / "scorecard.yml").read_text(encoding="utf-8")
    assert "ossf/scorecard-action" in body


def test_fix_create_default_security_insights(tmp_path: Path) -> None:
    rule = Rule(
        id="SUPPLY008",
        name="x",
        severity="info",
        check="file_exists",
        fix="create_default_security_insights",
    )
    assert apply_fix(tmp_path, rule) is True
    body = (tmp_path / "security-insights.yml").read_text(encoding="utf-8")
    assert "schema-version" in body
    assert "vulnerability-reporting" in body


def test_community_pack_fixes_clear_all_four_findings(tmp_path: Path) -> None:
    """After applying every community-pack `--fix`, H001/H002/H003/H006 stop failing."""
    rules = [r for r in load_bundled_pack("community") if r.fix]
    for rule in rules:
        apply_fix(tmp_path, rule)
    findings = run_rules(tmp_path, load_bundled_pack("community"))
    failing = {f.rule for f in findings if f.severity != "ok"}
    for cleared in ("H001", "H002", "H003", "H006"):
        assert cleared not in failing, f"{cleared} should have been cleared by --fix"


# --- JSON-mutating fixes (JS004 / JS007) ------------------------------------
#
# The package.json mutators are the only fixes that edit an existing
# user-owned file rather than creating one. The tests below cover:
#   1. Happy path — the field is added.
#   2. Idempotency — re-running is a no-op.
#   3. Existing-value respected — never clobber what the user set.
#   4. Other keys preserved — no destruction of script/dep tables.
#   5. Pre-existing `engines` object — only the `node` key gets added.
#   6. Missing / malformed / symlinked package.json — fix returns False.
#   7. The rule's own regex re-matches after the fix (round-trip).


def _write_pkg(root: Path, data: dict[str, object]) -> None:
    (root / "package.json").write_text(
        json.dumps(data, indent=2) + "\n",
        encoding="utf-8",
    )


def test_fix_set_package_manager_happy_path(tmp_path: Path) -> None:
    _write_pkg(tmp_path, {"name": "demo", "version": "0.1.0"})
    rule = Rule(
        id="JS007",
        name="x",
        severity="info",
        check="regex_in_file",
        fix="set_package_manager",
    )
    assert apply_fix(tmp_path, rule) is True
    data = json.loads((tmp_path / "package.json").read_text(encoding="utf-8"))
    assert data["packageManager"].startswith("pnpm@")
    assert data["name"] == "demo"  # original keys preserved
    assert data["version"] == "0.1.0"


def test_fix_set_package_manager_idempotent(tmp_path: Path) -> None:
    _write_pkg(tmp_path, {"name": "demo", "packageManager": "yarn@4.0.0"})
    rule = Rule(
        id="JS007",
        name="x",
        severity="info",
        check="regex_in_file",
        fix="set_package_manager",
    )
    assert apply_fix(tmp_path, rule) is False
    data = json.loads((tmp_path / "package.json").read_text(encoding="utf-8"))
    assert data["packageManager"] == "yarn@4.0.0"  # user choice preserved


def test_fix_set_package_manager_missing_file_returns_false(tmp_path: Path) -> None:
    rule = Rule(
        id="JS007",
        name="x",
        severity="info",
        check="regex_in_file",
        fix="set_package_manager",
    )
    assert apply_fix(tmp_path, rule) is False
    assert not (tmp_path / "package.json").exists()


def test_fix_set_package_manager_malformed_json_returns_false(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{ not valid json", encoding="utf-8")
    rule = Rule(
        id="JS007",
        name="x",
        severity="info",
        check="regex_in_file",
        fix="set_package_manager",
    )
    assert apply_fix(tmp_path, rule) is False
    # The file is untouched by the failed parse.
    assert "{ not valid json" in (tmp_path / "package.json").read_text(encoding="utf-8")


def test_fix_set_package_manager_refuses_symlinked_package_json(tmp_path: Path) -> None:
    real = tmp_path / "actual-pkg.json"
    _json_text = json.dumps({"name": "demo"})
    real.write_text(_json_text, encoding="utf-8")
    (tmp_path / "package.json").symlink_to(real)
    rule = Rule(
        id="JS007",
        name="x",
        severity="info",
        check="regex_in_file",
        fix="set_package_manager",
    )
    assert apply_fix(tmp_path, rule) is False
    # Symlink target is unchanged.
    assert "packageManager" not in real.read_text(encoding="utf-8")


def test_fix_set_engines_node_creates_engines_object(tmp_path: Path) -> None:
    _write_pkg(tmp_path, {"name": "demo"})
    rule = Rule(
        id="JS004",
        name="x",
        severity="info",
        check="regex_in_file",
        fix="set_engines_node",
    )
    assert apply_fix(tmp_path, rule) is True
    data = json.loads((tmp_path / "package.json").read_text(encoding="utf-8"))
    assert data["engines"]["node"].startswith(">=")


def test_fix_set_engines_node_extends_existing_engines(tmp_path: Path) -> None:
    """If engines already exists with other keys (e.g. pnpm), don't clobber it."""
    _write_pkg(
        tmp_path,
        {
            "name": "demo",
            "engines": {"pnpm": ">=9"},
        },
    )
    rule = Rule(
        id="JS004",
        name="x",
        severity="info",
        check="regex_in_file",
        fix="set_engines_node",
    )
    assert apply_fix(tmp_path, rule) is True
    data = json.loads((tmp_path / "package.json").read_text(encoding="utf-8"))
    assert data["engines"]["node"].startswith(">=")
    assert data["engines"]["pnpm"] == ">=9"  # other key preserved


def test_fix_set_engines_node_idempotent(tmp_path: Path) -> None:
    _write_pkg(
        tmp_path,
        {
            "name": "demo",
            "engines": {"node": ">=18"},
        },
    )
    rule = Rule(
        id="JS004",
        name="x",
        severity="info",
        check="regex_in_file",
        fix="set_engines_node",
    )
    assert apply_fix(tmp_path, rule) is False
    data = json.loads((tmp_path / "package.json").read_text(encoding="utf-8"))
    assert data["engines"]["node"] == ">=18"  # user pin preserved


def test_js_mutating_fixes_round_trip_clears_findings(tmp_path: Path) -> None:
    """Apply both mutating fixes, then run the lang-node pack — JS004 + JS007 cleared."""
    _write_pkg(tmp_path, {"name": "demo", "version": "0.1.0"})
    mutators = {"set_engines_node", "set_package_manager"}
    rules = [r for r in load_bundled_pack("lang-node") if r.fix in mutators]
    for rule in rules:
        apply_fix(tmp_path, rule)
    findings = run_rules(tmp_path, load_bundled_pack("lang-node"))
    failing = {f.rule for f in findings if f.severity != "ok"}
    assert "JS004" not in failing
    assert "JS007" not in failing


def test_fix_set_package_manager_preserves_trailing_newline(tmp_path: Path) -> None:
    """If the original ended with \\n, the result should too (POSIX convention)."""
    (tmp_path / "package.json").write_text('{\n  "name": "demo"\n}\n', encoding="utf-8")
    rule = Rule(
        id="JS007",
        name="x",
        severity="info",
        check="regex_in_file",
        fix="set_package_manager",
    )
    apply_fix(tmp_path, rule)
    assert (tmp_path / "package.json").read_text(encoding="utf-8").endswith("\n")


# --- JSON-mutating fixes for JS002A/B/C (name / version / license) ----------


def test_slugify_npm_name_basic() -> None:
    assert _slugify_npm_name("my-app") == "my-app"
    assert _slugify_npm_name("My App") == "my-app"
    assert _slugify_npm_name("Hello World!") == "hello-world"
    assert _slugify_npm_name("  Trim  Me  ") == "trim-me"
    assert _slugify_npm_name("###") == ""


def test_slugify_npm_name_strips_leading_dot_or_underscore() -> None:
    assert _slugify_npm_name("_private") == "private"
    assert _slugify_npm_name(".hidden") == "hidden"


def test_fix_set_package_name_uses_directory_basename(tmp_path: Path) -> None:
    pkg_dir = tmp_path / "Foo Bar"
    pkg_dir.mkdir()
    _write_pkg(pkg_dir, {"version": "0.1.0"})
    rule = Rule(
        id="JS002A",
        name="x",
        severity="warn",
        check="regex_in_file",
        fix="set_package_name",
    )
    assert apply_fix(pkg_dir, rule) is True
    data = json.loads((pkg_dir / "package.json").read_text(encoding="utf-8"))
    assert data["name"] == "foo-bar"
    # name appears before existing keys (npm convention)
    keys = list(data.keys())
    assert keys[0] == "name"


def test_fix_set_package_name_idempotent_when_already_set(tmp_path: Path) -> None:
    _write_pkg(tmp_path, {"name": "demo"})
    rule = Rule(
        id="JS002A",
        name="x",
        severity="warn",
        check="regex_in_file",
        fix="set_package_name",
    )
    assert apply_fix(tmp_path, rule) is False
    assert json.loads((tmp_path / "package.json").read_text())["name"] == "demo"


def test_fix_set_package_version_default(tmp_path: Path) -> None:
    _write_pkg(tmp_path, {"name": "demo"})
    rule = Rule(
        id="JS002B",
        name="x",
        severity="warn",
        check="regex_in_file",
        fix="set_package_version",
    )
    assert apply_fix(tmp_path, rule) is True
    data = json.loads((tmp_path / "package.json").read_text(encoding="utf-8"))
    assert data["version"] == "0.1.0"
    # version inserted right after name
    keys = list(data.keys())
    assert keys[0] == "name"
    assert keys[1] == "version"


def test_fix_set_package_version_idempotent(tmp_path: Path) -> None:
    _write_pkg(tmp_path, {"name": "demo", "version": "1.2.3"})
    rule = Rule(
        id="JS002B",
        name="x",
        severity="warn",
        check="regex_in_file",
        fix="set_package_version",
    )
    assert apply_fix(tmp_path, rule) is False
    assert json.loads((tmp_path / "package.json").read_text())["version"] == "1.2.3"


def test_fix_set_package_license_detects_mit_from_license_file(tmp_path: Path) -> None:
    _write_pkg(tmp_path, {"name": "demo"})
    (tmp_path / "LICENSE").write_text(
        "MIT License\n\nCopyright (c) 2026 Test User\n",
        encoding="utf-8",
    )
    rule = Rule(
        id="JS002C",
        name="x",
        severity="warn",
        check="regex_in_file",
        fix="set_package_license",
    )
    assert apply_fix(tmp_path, rule) is True
    assert json.loads((tmp_path / "package.json").read_text())["license"] == "MIT"


def test_fix_set_package_license_detects_apache_from_license_md(tmp_path: Path) -> None:
    _write_pkg(tmp_path, {"name": "demo"})
    (tmp_path / "LICENSE.md").write_text(
        "Apache License\nVersion 2.0, January 2004\n",
        encoding="utf-8",
    )
    rule = Rule(
        id="JS002C",
        name="x",
        severity="warn",
        check="regex_in_file",
        fix="set_package_license",
    )
    assert apply_fix(tmp_path, rule) is True
    assert json.loads((tmp_path / "package.json").read_text())["license"] == "Apache-2.0"


def test_fix_set_package_license_honours_spdx_identifier_line(tmp_path: Path) -> None:
    """An explicit `SPDX-License-Identifier:` line wins over body-text matching."""
    _write_pkg(tmp_path, {"name": "demo"})
    (tmp_path / "LICENSE").write_text(
        "SPDX-License-Identifier: BSD-3-Clause\n\n(license body...)\n",
        encoding="utf-8",
    )
    rule = Rule(
        id="JS002C",
        name="x",
        severity="warn",
        check="regex_in_file",
        fix="set_package_license",
    )
    assert apply_fix(tmp_path, rule) is True
    assert json.loads((tmp_path / "package.json").read_text())["license"] == "BSD-3-Clause"


def test_fix_set_package_license_no_op_when_no_license_file(tmp_path: Path) -> None:
    """Without a LICENSE file we have no defensible default — skip."""
    _write_pkg(tmp_path, {"name": "demo"})
    rule = Rule(
        id="JS002C",
        name="x",
        severity="warn",
        check="regex_in_file",
        fix="set_package_license",
    )
    assert apply_fix(tmp_path, rule) is False
    assert "license" not in json.loads((tmp_path / "package.json").read_text())


def test_fix_set_package_license_idempotent_when_already_set(tmp_path: Path) -> None:
    _write_pkg(tmp_path, {"name": "demo", "license": "Apache-2.0"})
    (tmp_path / "LICENSE").write_text("MIT License\n", encoding="utf-8")
    rule = Rule(
        id="JS002C",
        name="x",
        severity="warn",
        check="regex_in_file",
        fix="set_package_license",
    )
    assert apply_fix(tmp_path, rule) is False
    # User's manual setting wins over auto-detection.
    assert json.loads((tmp_path / "package.json").read_text())["license"] == "Apache-2.0"


def test_full_js002_round_trip_clears_findings(tmp_path: Path) -> None:
    """name + version + license fixes together clear JS002A/B/C."""
    pkg_dir = tmp_path / "demo-app"
    pkg_dir.mkdir()
    _write_pkg(pkg_dir, {})
    (pkg_dir / "LICENSE").write_text("MIT License\n\nCopyright (c) 2026\n", encoding="utf-8")
    rule_ids = {"set_package_name", "set_package_version", "set_package_license"}
    rules = [r for r in load_bundled_pack("lang-node") if r.fix in rule_ids]
    for rule in rules:
        apply_fix(pkg_dir, rule)
    findings = run_rules(pkg_dir, load_bundled_pack("lang-node"))
    failing = {f.rule for f in findings if f.severity != "ok"}
    for cleared in ("JS002A", "JS002B", "JS002C"):
        assert cleared not in failing, f"{cleared} should be cleared"


def test_detect_spdx_returns_none_when_unknown_license(tmp_path: Path) -> None:
    """A LICENSE file with no recognisable marker returns None — don't guess."""
    (tmp_path / "LICENSE").write_text("Some custom proprietary terms here.\n", encoding="utf-8")
    assert _detect_spdx_from_license(tmp_path) is None


# --- --backup flag ----------------------------------------------------------


def test_backup_creates_bak_for_mutating_fix(tmp_path: Path) -> None:
    """When backup=True, the original package.json is copied to package.json.bak."""
    original = json.dumps({"name": "demo"}, indent=2) + "\n"
    (tmp_path / "package.json").write_text(original, encoding="utf-8")
    rule = Rule(
        id="JS007",
        name="x",
        severity="info",
        check="regex_in_file",
        fix="set_package_manager",
    )
    assert apply_fix(tmp_path, rule, backup=True) is True
    assert (tmp_path / "package.json.bak").read_text(encoding="utf-8") == original
    # Live file is mutated.
    live = json.loads((tmp_path / "package.json").read_text(encoding="utf-8"))
    assert "packageManager" in live


def test_backup_off_by_default(tmp_path: Path) -> None:
    """Without backup=True (the default), no .bak file is written."""
    (tmp_path / "package.json").write_text(json.dumps({"name": "demo"}), encoding="utf-8")
    rule = Rule(
        id="JS007",
        name="x",
        severity="info",
        check="regex_in_file",
        fix="set_package_manager",
    )
    assert apply_fix(tmp_path, rule) is True
    assert not (tmp_path / "package.json.bak").exists()


def test_backup_skipped_when_no_pre_existing_file(tmp_path: Path) -> None:
    """Create-only fixes (file didn't exist) leave no .bak — nothing to back up."""
    rule = Rule(
        id="R001",
        name="x",
        severity="error",
        check="file_exists",
        fix="create_default_readme",
    )
    assert apply_fix(tmp_path, rule, backup=True) is True
    assert (tmp_path / "README.md").exists()
    assert not (tmp_path / "README.md.bak").exists()


def test_backup_captures_original_across_multiple_mutating_fixes(tmp_path: Path) -> None:
    """When 3 mutating fixes run sequentially against package.json, the .bak
    preserves the pre-first-fix state (not the intermediate state after fix #1)."""
    original = json.dumps({"name": "demo"}, indent=2) + "\n"
    (tmp_path / "package.json").write_text(original, encoding="utf-8")
    mutators = {
        "set_package_name",
        "set_package_version",
        "set_package_manager",
        "set_engines_node",
    }
    rules = [r for r in load_bundled_pack("lang-node") if r.fix in mutators]
    for rule in rules:
        apply_fix(tmp_path, rule, backup=True)
    bak = (tmp_path / "package.json.bak").read_text(encoding="utf-8")
    assert bak == original, "backup must preserve the original pre-fix state"


def test_backup_refuses_symlinked_target(tmp_path: Path) -> None:
    """A symlinked package.json triggers the symlink-refusal in _load_package_json,
    which means the fix returns False before _maybe_backup is reached — no .bak."""
    real = tmp_path / "actual.json"
    real.write_text(json.dumps({"name": "demo"}), encoding="utf-8")
    (tmp_path / "package.json").symlink_to(real)
    rule = Rule(
        id="JS007",
        name="x",
        severity="info",
        check="regex_in_file",
        fix="set_package_manager",
    )
    assert apply_fix(tmp_path, rule, backup=True) is False
    assert not (tmp_path / "package.json.bak").exists()


def test_backup_for_append_fix_npmrc(tmp_path: Path) -> None:
    """_append_npmrc_directive honours backup mode too — append fixes count as mutations."""
    (tmp_path / ".npmrc").write_text("# starter\n", encoding="utf-8")
    rule = Rule(
        id="JS008",
        name="x",
        severity="info",
        check="regex_in_file",
        fix="append_engine_strict_to_npmrc",
    )
    assert apply_fix(tmp_path, rule, backup=True) is True
    assert (tmp_path / ".npmrc.bak").read_text(encoding="utf-8") == "# starter\n"


def test_backup_contextvar_does_not_leak_across_calls(tmp_path: Path) -> None:
    """A backup-enabled call must NOT influence a subsequent default-mode call."""
    (tmp_path / "package.json").write_text(json.dumps({"name": "demo"}), encoding="utf-8")
    rule = Rule(
        id="JS007",
        name="x",
        severity="info",
        check="regex_in_file",
        fix="set_package_manager",
    )
    apply_fix(tmp_path, rule, backup=True)
    # Second target dir, default backup=False:
    other = tmp_path / "other"
    other.mkdir()
    (other / "package.json").write_text(json.dumps({"name": "x"}), encoding="utf-8")
    apply_fix(other, rule)  # default backup=False
    assert not (other / "package.json.bak").exists()
