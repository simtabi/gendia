"""Scaffolds module — schema, loaders, variable resolution, builder, post-actions."""

from __future__ import annotations

import io
import re
import subprocess
import sys
from pathlib import Path

import pytest

from gendia import scaffolds
from gendia.cli.arguments import build_parser, dispatch
from gendia.scaffolds import (
    ScaffoldTemplate,
    VariableSpec,
    apply,
    list_bundled_scaffolds,
    load_bundled_scaffold,
    parse_var_assignments,
    render_text,
    resolve_variables,
)

# --- schema -----------------------------------------------------------------


def test_variable_spec_from_dict_minimal() -> None:
    v = VariableSpec.from_dict("name", {})
    assert v.name == "name"
    assert v.required is False
    assert v.default is None


def test_scaffold_template_from_dict_requires_id() -> None:
    with pytest.raises(ValueError, match="id"):
        ScaffoldTemplate.from_dict({})


def test_scaffold_template_round_trip() -> None:
    spec = ScaffoldTemplate.from_dict(
        {
            "id": "demo",
            "name": "Demo",
            "description": "x",
            "languages": ["python"],
            "variables": {
                "name": {"required": True},
                "year": {"default": "${now.year}"},
            },
            "files": [{"src": "_common/.gitignore", "dest": ".gitignore"}],
            "after_scaffold": [{"kind": "git_init"}],
        }
    )
    assert spec.id == "demo"
    assert len(spec.variables) == 2
    assert len(spec.files) == 1
    assert spec.after_scaffold[0].kind == "git_init"


# --- loaders ----------------------------------------------------------------


def test_list_bundled_scaffolds() -> None:
    ids = list_bundled_scaffolds()
    assert "bare" in ids
    assert "python-uv" in ids


def test_load_bundled_scaffold_unknown_returns_none() -> None:
    assert load_bundled_scaffold("does-not-exist") is None


def test_bundled_bare_loads() -> None:
    spec = load_bundled_scaffold("bare")
    assert spec is not None
    assert spec.id == "bare"
    assert any(v.name == "name" for v in spec.variables)
    assert any(f.dest == "README.md" for f in spec.files)


# --- variable resolution ---------------------------------------------------


def _spec(**variables: dict[str, object]) -> ScaffoldTemplate:
    return ScaffoldTemplate(
        id="t",
        name="t",
        description="",
        variables=tuple(VariableSpec.from_dict(k, v) for k, v in variables.items()),
    )


def test_resolve_user_value_wins() -> None:
    spec = _spec(name={"required": True})
    out = resolve_variables(spec, {"name": "my-pkg"})
    assert out["name"] == "my-pkg"


def test_resolve_default_substitutes_builtins() -> None:
    spec = _spec(year={"default": "${now.year}"})
    out = resolve_variables(spec, {})
    assert out["year"].isdigit()
    assert int(out["year"]) >= 2025


def test_resolve_default_substitutes_other_vars() -> None:
    spec = _spec(
        name={"required": True},
        module={"default": "${name}"},
    )
    out = resolve_variables(spec, {"name": "my-pkg"})
    assert out["module"] == "my-pkg"


def test_resolve_required_missing_raises() -> None:
    spec = _spec(name={"required": True})
    with pytest.raises(ValueError, match="required variable missing: name"):
        resolve_variables(spec, {})


def test_resolve_choices_enforced() -> None:
    spec = _spec(license={"choices": ["MIT", "Apache-2.0"]})
    with pytest.raises(ValueError, match="not in choices"):
        resolve_variables(spec, {"license": "WTFPL"})


def test_resolve_validate_pattern_enforced() -> None:
    spec = _spec(name={"required": True, "validate": "^[a-z][a-z0-9-]*$"})
    with pytest.raises(ValueError, match="validation"):
        resolve_variables(spec, {"name": "BAD_NAME"})


def test_resolve_git_identity_builtin() -> None:
    spec = _spec(author={"default": "${gendia.git_identity.name}"})
    out = resolve_variables(
        spec,
        {},
        git_identity={"name": "Ada Lovelace", "email": "ada@example.com"},
    )
    assert out["author"] == "Ada Lovelace"


# --- render_text -----------------------------------------------------------


def test_render_text_supports_dotted_names() -> None:
    assert render_text("Year: ${now.year}", {"now.year": "2026"}) == "Year: 2026"


def test_render_text_leaves_unknown_vars_intact() -> None:
    assert render_text("Hello ${unknown}", {}) == "Hello ${unknown}"


# --- builder ---------------------------------------------------------------


def _bare_vars(**overrides: str) -> dict[str, str]:
    base = {
        "name": "demo-pkg",
        "description": "Demo",
        "license": "MIT",
        "author_name": "Test User",
        "author_email": "test@example.com",
    }
    base.update(overrides)
    return base


def test_apply_writes_files(tmp_path: Path) -> None:
    spec = load_bundled_scaffold("bare")
    assert spec is not None
    target = tmp_path / "demo-pkg"
    ops = apply(spec, target, _bare_vars())
    assert (target / "README.md").is_file()
    assert (target / ".gitignore").is_file()
    assert (target / "LICENSE").is_file()  # produced by post-action
    assert any(op.kind == "create" and op.dest.name == "README.md" for op in ops)


def test_apply_dry_run_writes_nothing(tmp_path: Path) -> None:
    spec = load_bundled_scaffold("bare")
    assert spec is not None
    target = tmp_path / "demo-pkg"
    ops = apply(spec, target, _bare_vars(), dry_run=True)
    assert not target.exists()
    assert all(op.kind in {"would-create", "would-skip"} for op in ops)


def test_apply_skips_existing_files_by_default(tmp_path: Path) -> None:
    spec = load_bundled_scaffold("bare")
    assert spec is not None
    target = tmp_path / "demo-pkg"
    target.mkdir()
    (target / "README.md").write_text("# preserved\n", encoding="utf-8")
    apply(spec, target, _bare_vars())
    assert (target / "README.md").read_text(encoding="utf-8") == "# preserved\n"


def test_apply_force_overwrites(tmp_path: Path) -> None:
    spec = load_bundled_scaffold("bare")
    assert spec is not None
    target = tmp_path / "demo-pkg"
    target.mkdir()
    (target / "README.md").write_text("# overwrite-me\n", encoding="utf-8")
    apply(spec, target, _bare_vars(), force=True)
    assert "# demo-pkg" in (target / "README.md").read_text(encoding="utf-8")


def test_template_substitution_in_destination_paths(tmp_path: Path) -> None:
    """python-uv's `src/${module}/__init__.py` lands under the substituted directory."""
    spec = load_bundled_scaffold("python-uv")
    assert spec is not None
    target = tmp_path / "demo-pkg"
    apply(
        spec,
        target,
        {
            "name": "demo-pkg",
            "module": "demo_pkg",
            "description": "Demo",
            "license": "MIT",
            "author_name": "T",
            "author_email": "t@example.com",
        },
    )
    assert (target / "src" / "demo_pkg" / "__init__.py").is_file()


def test_license_post_action_writes_LICENSE(tmp_path: Path) -> None:  # noqa: N802 — LICENSE name
    spec = load_bundled_scaffold("bare")
    assert spec is not None
    target = tmp_path / "demo-pkg"
    apply(spec, target, _bare_vars(license="MIT"))
    text = (target / "LICENSE").read_text(encoding="utf-8")
    assert "MIT License" in text
    assert "Test User" in text
    # The year placeholder is filled in (current year, so just check 4-digit).
    assert re.search(r"Copyright \(c\) \d{4} Test User", text)


def test_git_init_post_action_creates_repo(tmp_path: Path) -> None:
    spec = load_bundled_scaffold("bare")
    assert spec is not None
    target = tmp_path / "demo-pkg"
    apply(spec, target, _bare_vars())
    assert (target / ".git").is_dir()
    # Default branch is `main`
    proc = subprocess.run(
        ["git", "-C", str(target), "branch", "--show-current"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.stdout.strip() in {"main", ""}  # empty when no commits yet


# --- parse_var_assignments -------------------------------------------------


def test_parse_var_assignments_basic() -> None:
    out = parse_var_assignments(["name=foo", "license=MIT"])
    assert out == {"name": "foo", "license": "MIT"}


def test_parse_var_assignments_value_can_contain_equals() -> None:
    out = parse_var_assignments(["expr=a=b"])
    assert out == {"expr": "a=b"}


def test_parse_var_assignments_rejects_malformed() -> None:
    with pytest.raises(ValueError, match="KEY=VALUE"):
        parse_var_assignments(["just-a-key"])


# --- CLI integration -------------------------------------------------------


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


def test_cli_list_scaffolds_includes_bare_and_python_uv() -> None:
    rc, out = _run(["init", "--list-scaffolds"])
    assert rc == 0
    assert "bare" in out
    assert "python-uv" in out


def test_cli_init_scaffold_writes_files(tmp_path: Path) -> None:
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
            "description=A demo",
        ]
    )
    assert rc == 0
    assert (target / "README.md").is_file()
    assert (target / "LICENSE").is_file()


def test_cli_init_scaffold_unknown_id_fails(tmp_path: Path) -> None:
    rc, out = _run(
        [
            "init",
            "--scaffold",
            "does-not-exist",
            "--target",
            str(tmp_path / "x"),
            "--var",
            "name=demo",
            "--var",
            "description=A demo",
        ]
    )
    assert rc == 1
    assert "unknown scaffold" in out


def test_cli_init_scaffold_missing_required_var_fails(tmp_path: Path) -> None:
    rc, out = _run(
        [
            "init",
            "--scaffold",
            "bare",
            "--target",
            str(tmp_path / "x"),
        ]
    )
    assert rc == 1
    assert "required variable" in out


def test_cli_init_default_template_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No --scaffold => writes the legacy gendia.json stub (unchanged behaviour)."""
    monkeypatch.chdir(tmp_path)
    rc, _ = _run(["init"])
    assert rc == 0
    assert (tmp_path / "gendia.json").is_file()


# --- new bundled scaffolds + license texts --------------------------------


def test_bundled_scaffold_list_includes_all_thirteen() -> None:
    ids = list_bundled_scaffolds()
    assert set(ids) >= {
        "bare",
        "python-uv",
        "python-poetry",
        "node-pnpm",
        "node-bun",
        "rust",
        "go",
        "php-composer",
        "ruby-gem",
        "dotnet",
        "docs-mkdocs",
        "monorepo-pnpm-turbo",
        "java-gradle",
    }


def test_java_gradle_scaffold_renders(tmp_path: Path) -> None:
    spec = load_bundled_scaffold("java-gradle")
    assert spec is not None
    target = tmp_path / "hello"
    apply(spec, target, {"name": "hello", "description": "Demo Java app"})
    assert (target / "build.gradle.kts").is_file()
    assert (target / "settings.gradle.kts").is_file()
    assert (target / "src" / "main" / "java" / "org" / "example" / "App.java").is_file()
    assert (target / "src" / "test" / "java" / "org" / "example" / "AppTest.java").is_file()
    build = (target / "build.gradle.kts").read_text(encoding="utf-8")
    assert 'group = "org.example"' in build
    assert "junit-bom:5." in build  # junit_version default present
    settings = (target / "settings.gradle.kts").read_text(encoding="utf-8")
    assert 'rootProject.name = "hello"' in settings


def test_dotnet_scaffold_renders(tmp_path: Path) -> None:
    spec = load_bundled_scaffold("dotnet")
    assert spec is not None
    target = tmp_path / "Demo"
    apply(spec, target, {"name": "Demo", "description": "Demo .NET lib"})
    assert (target / "Demo.sln").is_file()
    assert (target / "src" / "Demo" / "Demo.csproj").is_file()
    assert (target / "tests" / "Demo.Tests" / "Demo.Tests.csproj").is_file()
    assert (target / "global.json").is_file()


def test_docs_mkdocs_scaffold_renders(tmp_path: Path) -> None:
    spec = load_bundled_scaffold("docs-mkdocs")
    assert spec is not None
    target = tmp_path / "demo-docs"
    apply(spec, target, {"name": "demo-docs", "description": "Demo docs site"})
    assert (target / "mkdocs.yml").is_file()
    assert (target / "docs" / "index.md").is_file()
    assert "material" in (target / "mkdocs.yml").read_text(encoding="utf-8")


def test_monorepo_pnpm_turbo_scaffold_renders(tmp_path: Path) -> None:
    spec = load_bundled_scaffold("monorepo-pnpm-turbo")
    assert spec is not None
    target = tmp_path / "demo-mono"
    apply(spec, target, {"name": "demo-mono", "description": "Demo monorepo"})
    assert (target / "pnpm-workspace.yaml").is_file()
    assert (target / "turbo.json").is_file()
    assert (target / "apps" / "web" / "package.json").is_file()
    pkg = (target / "packages" / "core" / "package.json").read_text(encoding="utf-8")
    assert '"name": "@demo-mono/core"' in pkg


def test_rust_scaffold_renders(tmp_path: Path) -> None:
    spec = load_bundled_scaffold("rust")
    assert spec is not None
    target = tmp_path / "demo"
    apply(spec, target, {"name": "demo", "description": "Demo crate"})
    assert (target / "Cargo.toml").is_file()
    assert (target / "src" / "lib.rs").is_file()
    assert (target / "rust-toolchain.toml").is_file()
    assert (target / "LICENSE").is_file()
    cargo = (target / "Cargo.toml").read_text(encoding="utf-8")
    assert 'name = "demo"' in cargo


def test_go_scaffold_renders(tmp_path: Path) -> None:
    spec = load_bundled_scaffold("go")
    assert spec is not None
    target = tmp_path / "demo"
    apply(
        spec,
        target,
        {
            "name": "demo",
            "module_path": "github.com/test/demo",
            "description": "Demo service",
        },
    )
    assert (target / "go.mod").is_file()
    assert (target / "cmd" / "demo" / "main.go").is_file()
    assert (target / "internal" / "app" / "app.go").is_file()
    go_mod = (target / "go.mod").read_text(encoding="utf-8")
    assert "module github.com/test/demo" in go_mod


def test_node_pnpm_scaffold_renders(tmp_path: Path) -> None:
    spec = load_bundled_scaffold("node-pnpm")
    assert spec is not None
    target = tmp_path / "demo"
    apply(spec, target, {"name": "demo", "description": "Demo package"})
    assert (target / "package.json").is_file()
    assert (target / "tsconfig.json").is_file()
    assert (target / ".npmrc").is_file()
    package_json = (target / "package.json").read_text(encoding="utf-8")
    assert '"name": "demo"' in package_json
    assert '"packageManager": "pnpm@' in package_json
    npmrc = (target / ".npmrc").read_text(encoding="utf-8")
    assert "engine-strict=true" in npmrc
    assert "ignore-scripts=true" in npmrc


def test_python_poetry_scaffold_renders(tmp_path: Path) -> None:
    spec = load_bundled_scaffold("python-poetry")
    assert spec is not None
    target = tmp_path / "demo"
    apply(spec, target, {"name": "demo", "module": "demo", "description": "Demo"})
    assert (target / "pyproject.toml").is_file()
    pyproject = (target / "pyproject.toml").read_text(encoding="utf-8")
    assert "[tool.poetry]" in pyproject
    assert (target / "src" / "demo" / "__init__.py").is_file()


def test_node_bun_scaffold_renders(tmp_path: Path) -> None:
    spec = load_bundled_scaffold("node-bun")
    assert spec is not None
    target = tmp_path / "demo"
    apply(spec, target, {"name": "demo", "description": "Demo"})
    package_json = (target / "package.json").read_text(encoding="utf-8")
    assert '"name": "demo"' in package_json
    assert "bun build" in package_json
    assert (target / ".bun-version").read_text(encoding="utf-8").strip()


def test_php_composer_scaffold_renders(tmp_path: Path) -> None:
    spec = load_bundled_scaffold("php-composer")
    assert spec is not None
    target = tmp_path / "demo"
    apply(
        spec,
        target,
        {
            "name": "demo",
            "vendor": "acme",
            "namespace": "Demo",
            "description": "Demo PHP package",
        },
    )
    composer = (target / "composer.json").read_text(encoding="utf-8")
    assert '"name": "acme/demo"' in composer
    assert (target / "src" / "Hello.php").is_file()
    assert (target / "tests" / "HelloTest.php").is_file()


def test_ruby_gem_scaffold_renders(tmp_path: Path) -> None:
    spec = load_bundled_scaffold("ruby-gem")
    assert spec is not None
    target = tmp_path / "demo"
    apply(spec, target, {"name": "demo", "module": "Demo", "description": "Demo gem"})
    gemspec = (target / "demo.gemspec").read_text(encoding="utf-8")
    assert "spec.name        = 'demo'" in gemspec
    assert (target / "lib" / "demo.rb").is_file()
    assert (target / "lib" / "demo" / "version.rb").is_file()
    assert (target / "spec" / "version_spec.rb").is_file()


def test_node_pnpm_index_ts_avoids_double_substitution(tmp_path: Path) -> None:
    """Regression: the index.ts.tmpl used to have `${name}, ${name}` in a
    JS template literal that both got substituted to the package name."""
    spec = load_bundled_scaffold("node-pnpm")
    assert spec is not None
    target = tmp_path / "demo"
    apply(spec, target, {"name": "demo", "description": "Demo"})
    text = (target / "src" / "index.ts").read_text(encoding="utf-8")
    assert "PACKAGE_NAME = 'demo'" in text
    assert "target: string" in text


@pytest.mark.parametrize(
    "spdx",
    [
        "MIT",
        "Apache-2.0",
        "BSD-3-Clause",
        "BSD-2-Clause",
        "ISC",
        "Unlicense",
        "GPL-3.0-or-later",
        "LGPL-3.0-or-later",
        "MPL-2.0",
        "MIT-0",
        "Zlib",
        "AGPL-3.0-or-later",
        "EUPL-1.2",
        "CC-BY-4.0",
        "BSL-1.1",
        "OFL-1.1",
        "Artistic-2.0",
    ],
)
def test_all_bundled_license_texts_produce_license_file(tmp_path: Path, spdx: str) -> None:
    """Every SPDX id in _LICENSE_TEXTS must produce a non-empty LICENSE."""
    spec = scaffolds.ScaffoldTemplate(
        id="bare-test",
        name="bare-test",
        description="x",
        after_scaffold=(
            scaffolds.AfterAction(
                kind="license_text",
                args={"spdx": spdx, "dest": "LICENSE", "holder": "Test", "year": "2026"},
            ),
        ),
    )
    target = tmp_path / spdx.replace("-", "_").replace(".", "_")
    target.mkdir()
    scaffolds.run_after_actions(spec, target, {"year": "2026", "holder": "Test"})
    text = (target / "LICENSE").read_text(encoding="utf-8")
    assert len(text) > 100, f"{spdx} license body is suspiciously short"
    # The Unlicense doesn't embed the copyright holder (public-domain dedication
    # without holder). All other bundled licenses include both year and holder.
    if spdx not in {"Unlicense"}:
        assert "2026" in text, f"{spdx} missing year"
        assert "Test" in text, f"{spdx} missing holder"


# --- module-level smoke ----------------------------------------------------


def test_scaffolds_module_exports() -> None:
    assert callable(scaffolds.apply)
    assert callable(scaffolds.list_bundled_scaffolds)
    assert callable(scaffolds.load_bundled_scaffold)


# --- security: path-traversal guard ---------------------------------------


def test_apply_refuses_dest_outside_target(tmp_path: Path) -> None:
    """A malicious dest like `../../etc/x` must NOT escape the target tree."""
    spec = ScaffoldTemplate(
        id="evil",
        name="evil",
        description="x",
        files=(scaffolds.FileTemplate(src="_common/.gitignore", dest="../escaped"),),
    )
    target = tmp_path / "demo"
    ops = apply(spec, target, {})
    assert any(op.kind == "path-escape" for op in ops)
    # Confirm the escape file was NOT written.
    assert not (tmp_path / "escaped").exists()


def test_apply_allows_nested_dest_inside_target(tmp_path: Path) -> None:
    spec = ScaffoldTemplate(
        id="ok",
        name="ok",
        description="x",
        files=(scaffolds.FileTemplate(src="_common/.gitignore", dest="deep/nested/.gitignore"),),
    )
    target = tmp_path / "demo"
    ops = apply(spec, target, {})
    assert any(op.kind == "create" for op in ops)
    assert (target / "deep" / "nested" / ".gitignore").is_file()


def test_apply_rejects_absolute_dest(tmp_path: Path) -> None:
    """Absolute dest paths must not escape via leading-/."""
    escaped = tmp_path / "outside"
    spec = ScaffoldTemplate(
        id="abs",
        name="abs",
        description="x",
        files=(scaffolds.FileTemplate(src="_common/.gitignore", dest=str(escaped)),),
    )
    target = tmp_path / "demo"
    ops = apply(spec, target, {})
    assert any(op.kind == "path-escape" for op in ops)
    assert not escaped.exists()


def test_apply_rejects_unresolved_var_in_dest(tmp_path: Path) -> None:
    """An undefined ${var} in dest must not produce a literal-named directory."""
    spec = ScaffoldTemplate(
        id="undef",
        name="undef",
        description="x",
        files=(scaffolds.FileTemplate(src="_common/.gitignore", dest="${undefined_var}/file"),),
    )
    target = tmp_path / "demo"
    ops = apply(spec, target, {})
    assert any(op.kind == "unresolved-var" for op in ops)
    # The literal `${undefined_var}` directory must NOT have been created.
    assert not (target / "${undefined_var}").exists()


def test_apply_refuses_to_follow_symlinked_dest(tmp_path: Path) -> None:
    """A pre-existing symlink at dest must not be followed (--force or not)."""
    target = tmp_path / "demo"
    target.mkdir()
    sensitive = tmp_path / "sensitive"
    sensitive.write_text("secret\n", encoding="utf-8")
    (target / "README.md").symlink_to(sensitive)

    spec = scaffolds.load_bundled_scaffold("bare")
    assert spec is not None
    ops = apply(spec, target, _bare_vars(), force=True)
    assert any(op.kind == "symlink-refused" for op in ops)
    assert sensitive.read_text(encoding="utf-8") == "secret\n"


# --- post-action surfacing ------------------------------------------------


def test_apply_surfaces_post_action_results(tmp_path: Path) -> None:
    """git_init / license_text / audit each append a `post:*` op to the result."""
    spec = scaffolds.load_bundled_scaffold("python-uv")
    assert spec is not None
    target = tmp_path / "demo"
    ops = apply(
        spec,
        target,
        {
            "name": "demo",
            "module": "demo",
            "description": "Demo",
            "license": "MIT",
            "author_name": "T",
            "author_email": "t@example.com",
        },
    )
    kinds = [op.kind for op in ops]
    assert any(k.startswith("post:license(MIT)") for k in kinds)
    assert any(k.startswith("post:git-init") for k in kinds)
    assert any(k.startswith("post:audit") for k in kinds)


def test_post_action_results_appear_in_cli_output(tmp_path: Path) -> None:
    target = tmp_path / "demo"
    rc, out = _run(
        [
            "init",
            "--scaffold",
            "python-uv",
            "--target",
            str(target),
            "--var",
            "name=demo",
            "--var",
            "module=demo",
            "--var",
            "description=A demo",
        ]
    )
    assert rc == 0
    # The audit summary line is rendered via the standard repo-result formatter.
    assert "post:audit" in out
    assert "post:license(MIT)" in out


# --- merge mode -----------------------------------------------------------


def test_cli_init_scaffold_merge_skip_is_ok(tmp_path: Path) -> None:
    """In --merge mode, skip-existing files don't make the operation fail."""
    target = tmp_path / "demo"
    target.mkdir()
    (target / "README.md").write_text("# preserved\n", encoding="utf-8")

    rc, _ = _run(
        [
            "init",
            "--scaffold",
            "bare",
            "--target",
            str(target),
            "--merge",
            "--var",
            "name=demo",
            "--var",
            "description=A demo",
        ]
    )
    # Without --merge, skip-existing would mark the op !ok and rc=1.
    # With --merge, it's an explicit "fine, this is a retrofit" signal.
    assert rc == 0
    # README still preserved, other files written.
    assert (target / "README.md").read_text(encoding="utf-8") == "# preserved\n"
    assert (target / ".gitignore").is_file()


def test_cli_init_scaffold_default_skip_is_failure(tmp_path: Path) -> None:
    """Default mode (no --merge): skip-existing is a failure (rc=1)."""
    target = tmp_path / "demo"
    target.mkdir()
    (target / "README.md").write_text("# preserved\n", encoding="utf-8")

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
            "description=A demo",
        ]
    )
    assert rc == 1


# --- target dir surfaced in output ---------------------------------------


def test_cli_init_scaffold_output_shows_target_dir(tmp_path: Path) -> None:
    target = tmp_path / "demo"
    rc, out = _run(
        [
            "init",
            "--scaffold",
            "bare",
            "--target",
            str(target),
            "--var",
            "name=demo",
            "--var",
            "description=A demo",
        ]
    )
    assert rc == 0
    # The resolved target path must appear so users know where the new repo lives.
    assert str(target.resolve()) in out
    assert "scaffold=bare" in out
