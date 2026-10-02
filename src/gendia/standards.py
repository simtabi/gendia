"""Data-driven repo-standards rule engine.

A "rule pack" is a JSON file that declares rules. Each rule references one of
the bundled check functions (`file_exists`, `regex_in_file`, `license_spdx`,
…) by short name and supplies its parameters. New rules are JSON-only — no
Python required for the common case.

This module is intentionally one file: schema, loader, check kinds, and runner.
The existing `gendia.operations.conventions` module re-exports `Finding` and
`Severity`; we reuse those rather than redefining them so JSON-driven rules
and the legacy hardcoded checks render through the same `RepoReport` and
output formatters.

Adding a new check kind = add a function below + register it in `_CHECK_KINDS`.
Adding a new rule = JSON only, no Python touch.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Iterable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, cast

from gendia.observability.logger import get_logger
from gendia.operations.conventions import Finding, Severity

_log = get_logger("standards")


# --- schema -----------------------------------------------------------------


_VALID_SEVERITIES: frozenset[str] = frozenset({"ok", "info", "warn", "warning", "error"})


def _normalise_severity(raw: str) -> Severity:
    """Project the broader rule-pack vocabulary onto the existing 3-level model.

    The doc treats `info` as a distinct level below `warn`; for v1 we map it
    to `warn` so rule findings flow through the existing RepoReport without
    schema churn. A future change can promote `info` to a first-class level.

    Unknown severities log a warning and fall through to `ok` so the rule
    silently passes — better than crashing during audit. Rule-pack authors
    see the typo in the log.
    """
    lowered = raw.strip().lower()
    if lowered == "error":
        return "error"
    if lowered in {"info", "warn", "warning"}:
        return "warn"
    if lowered != "ok":
        _log.warning("unknown severity in rule pack", extra={"severity": raw})
    return "ok"


@dataclass(frozen=True, slots=True)
class Rule:
    """One JSON-declared rule.

    Frozen so a rule pack can be cached. Fields mirror the v1 schema
    documented at https://simtabi.com/gendia/schemas/standards-v1.json.
    """

    id: str
    name: str
    severity: Severity
    check: str  # name of a bundled check function (or "module:function")
    args: dict[str, Any] = field(default_factory=dict)
    remediation: str = ""
    applies_when: dict[str, Any] | None = None
    category: str = ""
    fix: str | None = None  # name of a bundled fix kind (or "module:function")

    @classmethod
    def from_dict(cls, payload: dict[str, Any], *, default_category: str = "") -> Rule:
        try:
            rule_id = str(payload["id"])
            check = str(payload["check"])
        except KeyError as exc:
            raise ValueError(f"rule missing required field: {exc.args[0]!r}") from exc
        return cls(
            id=rule_id,
            name=str(payload.get("name", rule_id)),
            severity=_normalise_severity(str(payload.get("severity", "info"))),
            check=check,
            args=dict(payload.get("args") or {}),
            remediation=str(payload.get("remediation", "")),
            applies_when=payload.get("applies_when"),
            category=str(payload.get("category", default_category)),
            fix=payload.get("fix"),
        )


# --- loaders ----------------------------------------------------------------


def load_bundled_pack(name: str) -> tuple[Rule, ...]:
    """Load `gendia/data/standards/{name}.json` from package resources."""
    try:
        text = (
            resources.files("gendia")
            .joinpath(f"data/standards/{name}.json")
            .read_text(encoding="utf-8")
        )
    except (FileNotFoundError, ModuleNotFoundError, OSError) as exc:
        _log.warning("bundled pack unreadable", extra={"pack": name, "error": str(exc)})
        return ()
    return _parse_pack(text, source=f"bundled:{name}")


def load_rule_pack(path: Path | str) -> tuple[Rule, ...]:
    """Load a user-provided JSON rule pack from disk."""
    p = Path(path).expanduser()
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        _log.warning("rule pack unreadable", extra={"path": str(p), "error": str(exc)})
        return ()
    return _parse_pack(text, source=str(p))


def _parse_pack(text: str, *, source: str) -> tuple[Rule, ...]:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        _log.warning("rule pack invalid JSON", extra={"source": source, "error": str(exc)})
        return ()
    if not isinstance(raw, dict):
        _log.warning("rule pack root must be a JSON object", extra={"source": source})
        return ()
    category = str(raw.get("category", ""))
    rules_raw = raw.get("rules") or []
    out: list[Rule] = []
    for item in rules_raw:
        if not isinstance(item, dict):
            continue
        try:
            out.append(Rule.from_dict(item, default_category=category))
        except (ValueError, TypeError) as exc:
            _log.warning(
                "rule skipped — invalid shape",
                extra={"source": source, "error": str(exc)},
            )
    return tuple(out)


# --- check kinds ------------------------------------------------------------
#
# Each check function takes (root, rule) and returns a Finding when the check
# fails, or None when it passes. The signature is uniform so they can all
# live in one dispatch dict.


CheckFn = Callable[[Path, "Rule"], "Finding | None"]


def check_file_exists(root: Path, rule: Rule) -> Finding | None:
    """Pass when at least one (basename + extension) is found in any allowed location."""
    basenames = rule.args.get("basenames") or []
    extensions = rule.args.get("extensions") or [""]
    locations = rule.args.get("locations") or [""]

    for loc in locations:
        for base in basenames:
            for ext in extensions:
                candidate = (root / loc / f"{base}{ext}") if loc else (root / f"{base}{ext}")
                if candidate.is_file():
                    return None

    where = ", ".join(loc or "<root>" for loc in locations)
    candidates = ", ".join(f"{b}{e}" for b in basenames for e in extensions)
    return Finding(
        severity=rule.severity,
        rule=rule.id,
        message=f"{rule.name}: none of [{candidates}] found under [{where}]",
        path=None,
        fix=rule.remediation,
    )


def check_regex_in_file(root: Path, rule: Rule) -> Finding | None:
    """Pass when `args.pattern` matches `args.path` content (multi-line, by default)."""
    rel = str(rule.args.get("path", ""))
    pattern = str(rule.args.get("pattern", ""))
    if not rel or not pattern:
        return Finding(
            severity="warn",
            rule=rule.id,
            message=f"{rule.name}: misconfigured rule (path/pattern missing)",
            fix="this is a rule-pack bug; report it to the pack author.",
        )

    target = root / rel
    if not target.is_file():
        # Absence is handled by paired file_exists rules. Don't double-warn.
        return None
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return Finding(
            severity="warn",
            rule=rule.id,
            message=f"{rule.name}: could not read {rel}: {exc}",
            path=rel,
        )

    if re.search(pattern, text, re.MULTILINE):
        return None
    return Finding(
        severity=rule.severity,
        rule=rule.id,
        message=f"{rule.name}: pattern not found in {rel}",
        path=rel,
        fix=rule.remediation,
    )


_LICENSE_BASENAMES: tuple[str, ...] = (
    "LICENSE",
    "LICENSE.md",
    "LICENSE.txt",
    "LICENCE",
    "LICENCE.md",
    "LICENCE.txt",
    "COPYING",
)

# Lower-case substrings that uniquely identify a major OSI license. Conservative:
# we only flag a finding when the file exists AND none of these substrings are
# present in its first 2 KiB. The audit's job is to surface "this isn't a
# recognised license text", not to classify which license it is.
_LICENSE_SIGNATURES: tuple[str, ...] = (
    "mit license",
    "apache license",
    "gnu general public license",
    "gnu lesser general public license",
    "gnu affero general public license",
    "bsd 2-clause",
    "bsd 3-clause",
    "mozilla public license",
    "the unlicense",
    "creative commons",
    "isc license",
    "eclipse public license",
    "boost software license",
    "zlib license",
)


def check_license_spdx(root: Path, rule: Rule) -> Finding | None:
    """Pass when a LICENSE-like file contains a recognised license header."""
    for basename in _LICENSE_BASENAMES:
        target = root / basename
        if not target.is_file():
            continue
        try:
            text = target.read_text(encoding="utf-8", errors="replace")[:2048].lower()
        except OSError:
            return None
        if any(sig in text for sig in _LICENSE_SIGNATURES):
            return None
        return Finding(
            severity=rule.severity,
            rule=rule.id,
            message=f"{rule.name}: {basename} does not match a recognised SPDX license header",
            path=basename,
            fix=rule.remediation,
        )
    return None  # no LICENSE — paired R004 covers absence


def check_file_absent(root: Path, rule: Rule) -> Finding | None:
    """Pass when *none* of the listed paths exists. Inverse of `file_exists`.

    Use case: catch tracked files that should never be in a repo (`.env`,
    `*.pem`, Terraform state files). `args.paths` is a list of relative
    paths to check.
    """
    paths = rule.args.get("paths") or []
    offenders = [str(p) for p in paths if (root / str(p)).is_file()]
    if not offenders:
        return None
    return Finding(
        severity=rule.severity,
        rule=rule.id,
        message=f"{rule.name}: forbidden file(s) present: {', '.join(offenders)}",
        path=offenders[0],
        fix=rule.remediation,
    )


def check_directory_exists(root: Path, rule: Rule) -> Finding | None:
    """Pass when at least one of the listed directories exists under `root`.

    `args.paths` is a list of directory paths relative to the repo root.
    """
    paths = rule.args.get("paths") or []
    for rel in paths:
        if (root / str(rel)).is_dir():
            return None
    candidates = ", ".join(str(p) for p in paths) or "<none>"
    return Finding(
        severity=rule.severity,
        rule=rule.id,
        message=f"{rule.name}: none of [{candidates}] exists as a directory",
        path=None,
        fix=rule.remediation,
    )


def check_glob_exists(root: Path, rule: Rule) -> Finding | None:
    """Pass when at least one file matches `args.pattern` under `args.base_dir`.

    `args.pattern` is a glob (e.g. `*.spec`, `*.proto`, `<timestamp>_*.sql`).
    `args.base_dir` is the relative search root (default: repo root).
    `args.recursive` controls whether `**` semantics apply (default True).
    """
    pattern = str(rule.args.get("pattern", ""))
    base_dir = str(rule.args.get("base_dir", ""))
    recursive = bool(rule.args.get("recursive", True))
    if not pattern:
        return Finding(
            severity="warn",
            rule=rule.id,
            message=f"{rule.name}: misconfigured rule (pattern required)",
        )

    search_root = (root / base_dir) if base_dir else root
    if not search_root.is_dir():
        return None  # base_dir doesn't exist — rule didn't apply
    glob_iter = search_root.rglob(pattern) if recursive else search_root.glob(pattern)
    for _ in glob_iter:
        return None  # at least one match

    where = f"{base_dir}/" if base_dir else "<root>"
    return Finding(
        severity=rule.severity,
        rule=rule.id,
        message=f"{rule.name}: no files match {pattern!r} under {where}",
        path=base_dir or None,
        fix=rule.remediation,
    )


def check_one_of_files(root: Path, rule: Rule) -> Finding | None:
    """Constrain how many of the listed file groups are present.

    `args.groups` is a list of lists; each inner list = alternatives for one
    ecosystem (a group "exists" when ANY of its paths exists). Simpler form:
    `args.paths` (flat list) — each path counts as its own group.

    `args.expected` is one of:
      * `"exactly_one"` (default) — pass iff exactly one group exists
      * `"at_most_one"`           — pass iff zero or one group exists
      * `"at_least_one"`          — pass iff one or more groups exist
    """
    expected = str(rule.args.get("expected", "exactly_one"))

    raw_groups = rule.args.get("groups")
    if raw_groups is None:
        raw_groups = [[p] for p in rule.args.get("paths") or ()]

    if not raw_groups:
        return Finding(
            severity="warn",
            rule=rule.id,
            message=f"{rule.name}: misconfigured rule (groups or paths required)",
        )

    present_groups: list[list[str]] = []
    for group in raw_groups:
        if not isinstance(group, list):
            continue
        if any((root / str(p)).is_file() for p in group):
            present_groups.append([str(p) for p in group])

    count = len(present_groups)
    ok = {
        "exactly_one": count == 1,
        "at_most_one": count <= 1,
        "at_least_one": count >= 1,
    }.get(expected, False)
    if ok:
        return None

    present_summary = ", ".join("|".join(g) for g in present_groups) or "none"
    return Finding(
        severity=rule.severity,
        rule=rule.id,
        message=(
            f"{rule.name}: expected {expected}, found {count} group(s) present: [{present_summary}]"
        ),
        fix=rule.remediation,
    )


_CHECK_KINDS: dict[str, CheckFn] = {
    "file_exists": check_file_exists,
    "file_absent": check_file_absent,
    "regex_in_file": check_regex_in_file,
    "license_spdx": check_license_spdx,
    "directory_exists": check_directory_exists,
    "glob_exists": check_glob_exists,
    "one_of_files": check_one_of_files,
}


def _resolve_check(name: str) -> CheckFn | None:
    """Resolve a check kind. Built-in names lookup first; `module:function` syntax second."""
    if name in _CHECK_KINDS:
        return _CHECK_KINDS[name]
    if ":" in name:
        module_path, _, attr = name.partition(":")
        try:
            module = importlib.import_module(module_path)
        except ImportError as exc:
            _log.warning("check module import failed", extra={"check": name, "error": str(exc)})
            return None
        fn = getattr(module, attr, None)
        if callable(fn):
            # The runtime contract is "callable returning Finding | None"; static
            # checking can't see across importlib so we cast explicitly.
            return cast(CheckFn, fn)
    return None


# --- applies_when -----------------------------------------------------------


def _applies(root: Path, when: dict[str, Any] | None) -> bool:
    """Tiny predicate language for gating rules on cheap repo properties.

    Supported keys:
      * `any_file_exists: [paths]` — at least one of the relative paths exists.
      * `all_files_exist: [paths]` — every path exists.
      * `none_of_files_exist: [paths]` — no path exists.

    Unknown keys are ignored (forward-compatible). An empty / None when block
    means "always applies".
    """
    if not when:
        return True
    any_files = when.get("any_file_exists") or ()
    if any_files and not any((root / str(p)).is_file() for p in any_files):
        return False
    all_files = when.get("all_files_exist") or ()
    if all_files and not all((root / str(p)).is_file() for p in all_files):
        return False
    none_files = when.get("none_of_files_exist") or ()
    return not (none_files and any((root / str(p)).is_file() for p in none_files))


# --- runner -----------------------------------------------------------------


def run_rules(root: Path, rules: Sequence[Rule]) -> tuple[Finding, ...]:
    """Execute every rule against `root`. Returns only the non-passing findings.

    Exceptions inside a check are caught and surfaced as `warn`-severity
    findings — one bad rule must not abort the whole audit.
    """
    findings: list[Finding] = []
    for rule in rules:
        if not _applies(root, rule.applies_when):
            continue

        check_fn = _resolve_check(rule.check)
        if check_fn is None:
            findings.append(
                Finding(
                    severity="warn",
                    rule=rule.id,
                    message=f"{rule.name}: unknown check kind {rule.check!r}",
                    fix="add the check to gendia.standards or correct the rule pack.",
                )
            )
            continue

        try:
            outcome = check_fn(root, rule)
        except Exception as exc:  # noqa: BLE001 — surface as a finding, never raise
            findings.append(
                Finding(
                    severity="warn",
                    rule=rule.id,
                    message=f"{rule.name}: check raised: {exc}",
                    fix=rule.remediation,
                )
            )
            continue

        if outcome is not None:
            findings.append(outcome)
    return tuple(findings)


# --- fix kinds --------------------------------------------------------------
#
# A fix function takes (root, rule) and either creates / patches a file to
# make the rule pass, or returns False when it can't help. Fix kinds are
# referenced by name in the rule pack JSON via the optional `fix` field.


FixFn = Callable[[Path, "Rule"], bool]


def fix_create_default_readme(root: Path, _rule: Rule) -> bool:
    """Create a stub `README.md` at the repo root. No-op when one already exists."""
    target = root / "README.md"
    if target.exists():
        return False
    name = root.resolve().name or "project"
    target.write_text(
        f"# {name}\n\n_TODO: describe this project._\n",
        encoding="utf-8",
    )
    return True


def fix_create_default_gitignore(root: Path, _rule: Rule) -> bool:
    """Create a sensible default `.gitignore` covering common cruft."""
    target = root / ".gitignore"
    if target.exists():
        return False
    target.write_text(
        "# OS\n"
        ".DS_Store\n"
        "Thumbs.db\n"
        "desktop.ini\n\n"
        "# Secrets — never commit\n"
        ".env\n"
        ".env.local\n"
        ".env.*.local\n"
        "*.pem\n"
        "*.key\n\n"
        "# Build outputs\n"
        "build/\n"
        "dist/\n"
        "out/\n"
        "target/\n"
        "*.egg-info/\n"
        "__pycache__/\n"
        "*.pyc\n"
        ".pytest_cache/\n"
        ".mypy_cache/\n"
        ".ruff_cache/\n"
        ".coverage\n"
        "htmlcov/\n\n"
        "# Dependencies\n"
        "node_modules/\n"
        ".venv/\n"
        "venv/\n",
        encoding="utf-8",
    )
    return True


def fix_create_default_editorconfig(root: Path, _rule: Rule) -> bool:
    """Create a sensible default `.editorconfig`."""
    target = root / ".editorconfig"
    if target.exists():
        return False
    target.write_text(
        "root = true\n\n"
        "[*]\n"
        "charset = utf-8\n"
        "end_of_line = lf\n"
        "indent_style = space\n"
        "indent_size = 2\n"
        "insert_final_newline = true\n"
        "trim_trailing_whitespace = true\n\n"
        "[*.{md,markdown}]\n"
        "trim_trailing_whitespace = false\n\n"
        "[Makefile]\n"
        "indent_style = tab\n\n"
        "[*.py]\n"
        "indent_size = 4\n\n"
        "[*.go]\n"
        "indent_style = tab\n",
        encoding="utf-8",
    )
    return True


def fix_add_text_auto_to_gitattributes(root: Path, _rule: Rule) -> bool:
    """Ensure `.gitattributes` contains a `* text=auto` line.

    Creates the file if it's missing. If it exists but lacks the directive,
    prepend the line and leave the rest of the file intact.
    """
    target = root / ".gitattributes"
    if not target.exists():
        target.write_text("* text=auto\n", encoding="utf-8")
        return True
    text = target.read_text(encoding="utf-8")
    if re.search(r"^\*\s+text\s*=\s*auto", text, re.MULTILINE):
        return False  # already present
    _maybe_backup(target)
    new_text = "* text=auto\n" + text
    target.write_text(new_text, encoding="utf-8")
    return True


def _append_npmrc_directive(root: Path, directive: str, pattern: str) -> bool:
    """Append `directive` to `.npmrc` unless a matching line is already present.

    Refuses to touch the file if `.npmrc` is a symlink — fixes are root-relative
    only and we don't want to follow them.
    """
    target = root / ".npmrc"
    if target.is_symlink():
        return False
    if not target.exists():
        target.write_text(directive + "\n", encoding="utf-8")
        return True
    text = target.read_text(encoding="utf-8")
    if re.search(pattern, text, re.MULTILINE):
        return False
    _maybe_backup(target)
    sep = "" if text.endswith("\n") or text == "" else "\n"
    target.write_text(text + sep + directive + "\n", encoding="utf-8")
    return True


def fix_append_engine_strict_to_npmrc(root: Path, _rule: Rule) -> bool:
    """Append `engine-strict=true` to `.npmrc` if not already present (JS008 fix)."""
    return _append_npmrc_directive(
        root,
        "engine-strict=true",
        r"^\s*engine-strict\s*=\s*true",
    )


def fix_append_ignore_scripts_to_npmrc(root: Path, _rule: Rule) -> bool:
    """Append `ignore-scripts=true` to `.npmrc` if not already present (JS009 fix)."""
    return _append_npmrc_directive(
        root,
        "ignore-scripts=true",
        r"^\s*ignore-scripts\s*=\s*true",
    )


def fix_create_default_dependabot(root: Path, _rule: Rule) -> bool:
    """Write a starter `.github/dependabot.yml` (GH003 fix).

    No-op if any dependabot or renovate config already exists in the canonical
    lookup locations — keeps `--fix` idempotent across re-runs.
    """
    existing_locations = (
        root / ".github" / "dependabot.yml",
        root / ".github" / "dependabot.yaml",
        root / "dependabot.yml",
        root / "renovate.json",
        root / "renovate.json5",
        root / ".github" / "renovate.json",
        root / ".github" / "renovate.json5",
    )
    if any(p.exists() for p in existing_locations):
        return False
    target = root / ".github" / "dependabot.yml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "version: 2\n"
        "updates:\n"
        "  - package-ecosystem: github-actions\n"
        "    directory: /\n"
        "    schedule:\n"
        "      interval: weekly\n",
        encoding="utf-8",
    )
    return True


def fix_create_default_codeql_workflow(root: Path, _rule: Rule) -> bool:
    """Write a starter `.github/workflows/codeql.yml` (SUPPLY005 fix).

    No-op if a CodeQL workflow already exists at either common filename.
    """
    workflows = root / ".github" / "workflows"
    candidates = (workflows / "codeql.yml", workflows / "codeql.yaml")
    if any(p.exists() for p in candidates):
        return False
    workflows.mkdir(parents=True, exist_ok=True)
    target = workflows / "codeql.yml"
    target.write_text(
        "name: CodeQL\n"
        "on:\n"
        "  push:\n"
        "    branches: [main]\n"
        "  pull_request:\n"
        "    branches: [main]\n"
        "  schedule:\n"
        '    - cron: "0 6 * * 1"\n'
        "permissions:\n"
        "  contents: read\n"
        "  security-events: write\n"
        "jobs:\n"
        "  analyze:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - uses: github/codeql-action/init@v3\n"
        "      - uses: github/codeql-action/analyze@v3\n",
        encoding="utf-8",
    )
    return True


# --- community-health fix kinds ---------------------------------------------
#
# All of these refuse to overwrite an existing file at any of the rule's
# accepted locations (root / `.github/` / `docs/`), so `--fix` is idempotent
# across re-runs and never clobbers customised content.


def _any_exists(root: Path, paths: Iterable[Path]) -> bool:
    return any((root / p).exists() for p in paths)


def fix_create_default_contributing(root: Path, _rule: Rule) -> bool:
    """Write a minimal `CONTRIBUTING.md` (H001 fix)."""
    if _any_exists(
        root,
        (
            Path("CONTRIBUTING.md"),
            Path("CONTRIBUTING"),
            Path(".github") / "CONTRIBUTING.md",
            Path("docs") / "CONTRIBUTING.md",
        ),
    ):
        return False
    (root / "CONTRIBUTING.md").write_text(
        "# Contributing\n\n"
        "Thanks for your interest in contributing. This document covers the\n"
        "basics — please open an issue if anything is unclear.\n\n"
        "## Filing issues\n\n"
        "Before opening an issue, search existing ones to avoid duplicates.\n"
        "Include: what you tried, what you expected, what happened, and\n"
        "version / OS info.\n\n"
        "## Development setup\n\n"
        "_TODO: document how to bootstrap the dev environment._\n\n"
        "## Pull requests\n\n"
        "- Branch from `main` (`feature/<slug>`, `fix/<slug>`, `chore/<slug>`).\n"
        "- Follow [Conventional Commits](https://www.conventionalcommits.org).\n"
        "- Sign off your commits (`git commit -s`).\n"
        "- Ensure tests pass and add new ones for behaviour changes.\n",
        encoding="utf-8",
    )
    return True


def fix_create_default_code_of_conduct(root: Path, _rule: Rule) -> bool:
    """Write a Contributor-Covenant-style `CODE_OF_CONDUCT.md` (H002 fix)."""
    if _any_exists(
        root,
        (
            Path("CODE_OF_CONDUCT.md"),
            Path("CODE_OF_CONDUCT"),
            Path(".github") / "CODE_OF_CONDUCT.md",
            Path("docs") / "CODE_OF_CONDUCT.md",
        ),
    ):
        return False
    (root / "CODE_OF_CONDUCT.md").write_text(
        "# Code of Conduct\n\n"
        "This project adopts the [Contributor Covenant v2.1]"
        "(https://www.contributor-covenant.org/version/2/1/code_of_conduct/)\n"
        "as its code of conduct.\n\n"
        "## Our pledge\n\n"
        "We pledge to make participation a harassment-free experience for\n"
        "everyone, regardless of age, body size, visible or invisible\n"
        "disability, ethnicity, sex characteristics, gender identity and\n"
        "expression, level of experience, education, socio-economic status,\n"
        "nationality, personal appearance, race, religion, or sexual identity\n"
        "and orientation.\n\n"
        "## Reporting\n\n"
        "Instances of abusive, harassing, or otherwise unacceptable behaviour\n"
        "may be reported privately to the maintainers (see SECURITY.md or the\n"
        "AUTHORS file for contact addresses).\n\n"
        "The full text of the Contributor Covenant is linked above.\n",
        encoding="utf-8",
    )
    return True


def fix_create_default_security(root: Path, _rule: Rule) -> bool:
    """Write a minimal `SECURITY.md` (H003 fix). Includes a 'How to report' section."""
    if _any_exists(
        root,
        (
            Path("SECURITY.md"),
            Path("SECURITY"),
            Path(".github") / "SECURITY.md",
            Path("docs") / "SECURITY.md",
        ),
    ):
        return False
    (root / "SECURITY.md").write_text(
        "# Security policy\n\n"
        "## Supported versions\n\n"
        "_TODO: list which release lines still receive security fixes._\n\n"
        "## How to report a vulnerability\n\n"
        "Please **do not** open a public issue for security reports.\n\n"
        "Instead, contact the maintainers privately by email at\n"
        "`security@example.com` (replace with the project's address). Include:\n\n"
        "- Affected version(s) and a minimal reproduction.\n"
        "- The impact you've observed or estimated.\n"
        "- Whether the issue is being actively exploited.\n\n"
        "We aim to acknowledge reports within 3 business days and to publish a\n"
        "fix or mitigation within 30 days.\n",
        encoding="utf-8",
    )
    return True


def fix_create_default_changelog(root: Path, _rule: Rule) -> bool:
    """Write a Keep-a-Changelog skeleton `CHANGELOG.md` (H006 fix)."""
    target = root / "CHANGELOG.md"
    if target.exists():
        return False
    target.write_text(
        "# Changelog\n\n"
        "All notable changes to this project are documented in this file.\n\n"
        "The format is based on [Keep a Changelog]"
        "(https://keepachangelog.com/en/1.1.0/), and this project adheres to\n"
        "[Semantic Versioning](https://semver.org/spec/v2.0.0.html).\n\n"
        "## [Unreleased]\n\n"
        "### Added\n"
        "- _TODO_\n\n"
        "### Changed\n"
        "- _TODO_\n\n"
        "### Fixed\n"
        "- _TODO_\n",
        encoding="utf-8",
    )
    return True


def fix_create_default_codeowners(root: Path, _rule: Rule) -> bool:
    """Write a starter `.github/CODEOWNERS` (GH004 fix).

    The default catch-all is a TODO placeholder — users must edit it before
    the rule actually does anything useful in PR reviews.
    """
    if _any_exists(
        root,
        (
            Path(".github") / "CODEOWNERS",
            Path("CODEOWNERS"),
            Path("docs") / "CODEOWNERS",
        ),
    ):
        return False
    target = root / ".github" / "CODEOWNERS"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "# Each line is a file pattern followed by one or more owners.\n"
        "# Order matters — the last matching pattern wins.\n"
        "# Documentation: https://docs.github.com/en/repositories/"
        "managing-your-repositories-settings-and-features/customizing-your-repository/about-code-owners\n\n"
        "# Catch-all — TODO: replace with the actual owning team / users.\n"
        "*  @TODO-owning-team\n",
        encoding="utf-8",
    )
    return True


def fix_create_default_pr_template(root: Path, _rule: Rule) -> bool:
    """Write a starter `.github/PULL_REQUEST_TEMPLATE.md` (GH006 fix)."""
    if _any_exists(
        root,
        (
            Path(".github") / "PULL_REQUEST_TEMPLATE.md",
            Path(".github") / "pull_request_template.md",
            Path("PULL_REQUEST_TEMPLATE.md"),
            Path("pull_request_template.md"),
            Path("docs") / "pull_request_template.md",
        ),
    ):
        return False
    target = root / ".github" / "PULL_REQUEST_TEMPLATE.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "## Summary\n\n"
        "<!-- One sentence on what this PR does and why. Link related issues. -->\n\n"
        "## Changes\n\n"
        "- _Bullet the user-visible changes._\n\n"
        "## Test plan\n\n"
        "- [ ] Unit tests added / updated\n"
        "- [ ] Manual smoke run (describe)\n"
        "- [ ] No regressions in adjacent features\n\n"
        "## Notes for reviewers\n\n"
        "<!-- Anything reviewers should look at carefully. -->\n",
        encoding="utf-8",
    )
    return True


def fix_create_default_scorecard_workflow(root: Path, _rule: Rule) -> bool:
    """Write a starter `.github/workflows/scorecard.yml` (SUPPLY007 fix)."""
    workflows = root / ".github" / "workflows"
    candidates = (
        workflows / "scorecard.yml",
        workflows / "scorecard.yaml",
        workflows / "scorecards.yml",
        workflows / "scorecards.yaml",
    )
    if any(p.exists() for p in candidates):
        return False
    workflows.mkdir(parents=True, exist_ok=True)
    (workflows / "scorecard.yml").write_text(
        "name: OpenSSF Scorecard\n"
        "on:\n"
        "  schedule:\n"
        '    - cron: "0 6 * * 1"\n'
        "  push:\n"
        "    branches: [main]\n"
        "permissions: read-all\n"
        "jobs:\n"
        "  analysis:\n"
        "    runs-on: ubuntu-latest\n"
        "    permissions:\n"
        "      security-events: write\n"
        "      id-token: write\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "        with: { persist-credentials: false }\n"
        "      - uses: ossf/scorecard-action@v2.4.0\n"
        "        with:\n"
        "          results_file: results.sarif\n"
        "          results_format: sarif\n"
        "          publish_results: true\n"
        "      - uses: github/codeql-action/upload-sarif@v3\n"
        "        with: { sarif_file: results.sarif }\n",
        encoding="utf-8",
    )
    return True


def fix_create_default_security_insights(root: Path, _rule: Rule) -> bool:
    """Write a minimal `security-insights.yml` (SUPPLY008 fix)."""
    if _any_exists(
        root,
        (
            Path("security-insights.yml"),
            Path("security-insights.yaml"),
            Path(".github") / "security-insights.yml",
            Path(".github") / "security-insights.yaml",
            Path(".gitlab") / "security-insights.yml",
        ),
    ):
        return False
    (root / "security-insights.yml").write_text(
        "# OpenSSF Security Insights schema:\n"
        "#   https://github.com/ossf/security-insights-spec\n"
        "header:\n"
        "  schema-version: 1.0.0\n"
        '  expiration-date: "2099-12-31T00:00:00.000Z"\n'
        '  last-updated: "2026-01-01"\n'
        '  last-reviewed: "2026-01-01"\n'
        "  project-url: https://example.com/TODO\n"
        "  changelog: https://example.com/TODO/blob/main/CHANGELOG.md\n"
        "  license: https://example.com/TODO/blob/main/LICENSE\n"
        "project-lifecycle:\n"
        "  status: active\n"
        "  bug-fixes-only: false\n"
        "  core-maintainers:\n"
        "    - https://github.com/TODO\n"
        "contribution-policy:\n"
        "  accepts-pull-requests: true\n"
        "  accepts-automated-pull-requests: true\n"
        "  contributing-policy: https://example.com/TODO/blob/main/CONTRIBUTING.md\n"
        "vulnerability-reporting:\n"
        "  accepts-vulnerability-reports: true\n"
        "  email-contact: security@example.com\n"
        "  security-policy: https://example.com/TODO/blob/main/SECURITY.md\n",
        encoding="utf-8",
    )
    return True


# --- JSON-mutating fixes ----------------------------------------------------
#
# Fixes that edit existing JSON files (currently only `package.json`). These
# are riskier than create-only fixes because they re-format the file via
# `json.dump`. The helpers below preserve:
#   - 2-space indent (npm / pnpm / bun convention)
#   - Trailing newline (if original had one, or always for newly-created)
#   - Atomic write semantics (write to tmp + `os.replace`) so a crash mid-write
#     can't truncate the user's file.
#
# Idempotency is guaranteed by checking the existing JSON state *before*
# writing — if the field is already present with a non-empty value, the fix
# returns False and the file is untouched.

# Conservative defaults. These are explicit constants so they're easy to tweak
# in one place and discoverable by readers of the fix pack.
_DEFAULT_PACKAGE_MANAGER = "pnpm@9.7.0"
_DEFAULT_ENGINES_NODE = ">=20"
_DEFAULT_PACKAGE_VERSION = "0.1.0"

# Distinctive first-line / title markers for the bundled LICENSE bodies. Used
# by `_detect_spdx_from_license` to populate JS002C's `license` field when a
# LICENSE file already exists. Order matters — longer / more-specific markers
# come first so e.g. "Affero" wins over "General Public".
_LICENSE_BODY_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("AGPL-3.0-or-later", ("GNU AFFERO GENERAL PUBLIC LICENSE",)),
    ("LGPL-3.0-or-later", ("GNU LESSER GENERAL PUBLIC LICENSE", "Version 3")),
    ("GPL-3.0-or-later", ("GNU GENERAL PUBLIC LICENSE", "Version 3")),
    ("MPL-2.0", ("Mozilla Public License", "Version 2.0")),
    ("Apache-2.0", ("Apache License", "Version 2.0")),
    ("BSD-3-Clause", ("BSD 3-Clause",)),
    ("BSD-2-Clause", ("BSD 2-Clause",)),
    ("MIT-0", ("MIT No Attribution",)),
    ("MIT", ("MIT License",)),
    ("ISC", ("ISC License",)),
    ("Unlicense", ("This is free and unencumbered software",)),
    ("Zlib", ("zlib License",)),
    ("EUPL-1.2", ("EUROPEAN UNION PUBLIC LICENCE",)),
    ("CC-BY-4.0", ("Creative Commons Attribution",)),
    ("BSL-1.1", ("Business Source License",)),
    ("OFL-1.1", ("SIL Open Font License",)),
    ("Artistic-2.0", ("Artistic License",)),
)

# npm name rules (simplified): lowercase, ascii, no leading dot/underscore,
# allowed chars are [a-z0-9._~-]. We slugify any non-allowed run to a single
# hyphen and strip leading/trailing hyphens.
_NPM_NAME_NON_ALLOWED = re.compile(r"[^a-z0-9._~-]+")


def _slugify_npm_name(raw: str) -> str:
    """Coerce an arbitrary string into a publishable npm package name.

    Returns an empty string if the input has no usable characters; callers
    should treat that as "skip the fix".
    """
    slug = _NPM_NAME_NON_ALLOWED.sub("-", raw.strip().lower())
    slug = slug.strip("-._~")
    # npm forbids leading dot / underscore even after our cleanup. Strip them.
    while slug and slug[0] in "._":
        slug = slug[1:]
    return slug


def _detect_spdx_from_license(root: Path) -> str | None:
    """Best-effort SPDX detection from a LICENSE file in `root`.

    Recognises:
      - An explicit `SPDX-License-Identifier: X` line anywhere in the file.
      - A distinctive title / phrase from a bundled-license body (first ~30
        lines only, so we don't false-positive on prose mentioning a license
        name in passing).

    Returns None if no LICENSE file is present or no marker is recognised.
    """
    candidates = (
        root / "LICENSE",
        root / "LICENSE.md",
        root / "LICENSE.txt",
        root / "LICENCE",
        root / "LICENCE.md",
        root / "LICENCE.txt",
    )
    target = next((p for p in candidates if p.is_file()), None)
    if target is None:
        return None
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    # Modern explicit marker wins.
    spdx_match = re.search(r"SPDX-License-Identifier:\s*([A-Za-z0-9.\-+]+)", text)
    if spdx_match:
        return spdx_match.group(1)
    # Fall back to body-marker scan over the first 30 lines.
    head = "\n".join(text.splitlines()[:30])
    for spdx, markers in _LICENSE_BODY_MARKERS:
        if all(marker in head for marker in markers):
            return spdx
    return None


# --- backup mode ------------------------------------------------------------
#
# Mutating fixes can opt into copying the existing file to `<name>.bak` before
# overwriting. The flag is propagated through a ContextVar so we don't have to
# thread an explicit parameter through every fix function (most don't care).
# `apply_fix(..., backup=True)` flips the flag for the duration of one rule.

_BACKUP_MODE: ContextVar[bool] = ContextVar("gendia_backup_mode", default=False)


def _maybe_backup(target: Path) -> None:
    """If backup mode is on and `target` is a real existing file, copy to `<target>.bak`.

    Skips silently when:
      - backup mode is off (default),
      - the target is a symlink (we already refuse to follow symlinks in fixes),
      - the target doesn't exist (nothing to back up),
      - a `<target>.bak` is already present (preserve the original pre-fix
        snapshot — successive mutating fixes against the same file must NOT
        overwrite the backup with their intermediate state),
      - the backup write fails (logged; the fix still proceeds — losing a backup
        is better than blocking a valid fix).
    """
    if not _BACKUP_MODE.get():
        return
    if target.is_symlink() or not target.is_file():
        return
    bak = target.with_name(target.name + ".bak")
    if bak.exists():
        return  # Preserve the first-write snapshot across multiple fixes per file.
    try:
        shutil.copy2(target, bak)
    except OSError as exc:
        _log.warning(
            "backup failed",
            extra={"target": str(target), "error": str(exc)},
        )


def _load_package_json(root: Path) -> tuple[dict[str, Any] | None, bool]:
    """Load `package.json` from `root`. Returns (data, had_trailing_newline).

    On any failure (missing, unreadable, malformed JSON, or non-object root)
    returns (None, False). Fixes treat None as "skip — not our problem".
    """
    target = root / "package.json"
    if target.is_symlink() or not target.is_file():
        return None, False
    try:
        text = target.read_text(encoding="utf-8")
        data = json.loads(text)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        _log.warning(
            "package.json could not be parsed; skipping fix",
            extra={"path": str(target), "error": str(exc)},
        )
        return None, False
    if not isinstance(data, dict):
        return None, False
    return data, text.endswith("\n")


def _write_package_json(root: Path, data: dict[str, Any], *, trailing_newline: bool = True) -> None:
    """Atomically write `package.json`. 2-space indent; LF line endings."""
    target = root / "package.json"
    _maybe_backup(target)
    body = json.dumps(data, indent=2, ensure_ascii=False)
    if trailing_newline:
        body += "\n"
    # Write to a tmp file in the same directory, then atomically replace.
    # Same-directory tmp ensures `os.replace` is a single inode rename rather
    # than a cross-filesystem copy (which isn't atomic).
    fd, tmp = tempfile.mkstemp(prefix=".package-", suffix=".json.tmp", dir=str(root))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.replace(tmp, target)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def fix_set_package_manager(root: Path, _rule: Rule) -> bool:
    """Inject a default `packageManager` field into `package.json` (JS007 fix).

    Idempotent: returns False if the field is already present with a non-empty
    string value. Refuses to act if package.json is missing, symlinked, or
    malformed.
    """
    data, had_nl = _load_package_json(root)
    if data is None:
        return False
    existing = data.get("packageManager")
    if isinstance(existing, str) and existing.strip():
        return False  # Already set — don't clobber.
    data["packageManager"] = _DEFAULT_PACKAGE_MANAGER
    _write_package_json(root, data, trailing_newline=had_nl or True)
    return True


def fix_set_engines_node(root: Path, _rule: Rule) -> bool:
    """Inject `engines.node` into `package.json` (JS004 fix).

    Adds an `engines` object if missing, or just the `node` key if `engines`
    exists without it. Never overwrites an existing `engines.node` value.
    """
    data, had_nl = _load_package_json(root)
    if data is None:
        return False
    engines = data.get("engines")
    if not isinstance(engines, dict):
        engines = {}
        data["engines"] = engines
    existing = engines.get("node")
    if isinstance(existing, str) and existing.strip():
        return False  # User already pinned a node range — leave it alone.
    engines["node"] = _DEFAULT_ENGINES_NODE
    _write_package_json(root, data, trailing_newline=had_nl or True)
    return True


def fix_set_package_name(root: Path, _rule: Rule) -> bool:
    """Set `name` in `package.json` to the slugified directory basename (JS002A fix).

    No-op if `name` is already a non-empty string, or if the slugified
    directory name is empty (e.g. running against `/`).
    """
    data, had_nl = _load_package_json(root)
    if data is None:
        return False
    existing = data.get("name")
    if isinstance(existing, str) and existing.strip():
        return False
    slug = _slugify_npm_name(root.resolve().name)
    if not slug:
        return False  # Couldn't synthesise a name; skip rather than write garbage.
    # Insert `name` at the top of the JSON to match npm convention. Python 3.7+
    # dicts preserve insertion order, so rebuild with name first.
    new_data: dict[str, Any] = {"name": slug}
    for k, v in data.items():
        if k != "name":
            new_data[k] = v
    _write_package_json(root, new_data, trailing_newline=had_nl or True)
    return True


def fix_set_package_version(root: Path, _rule: Rule) -> bool:
    """Set `version` in `package.json` to `0.1.0` (JS002B fix).

    Conservative default — semver convention for an unreleased package.
    No-op if `version` is already a non-empty string.
    """
    data, had_nl = _load_package_json(root)
    if data is None:
        return False
    existing = data.get("version")
    if isinstance(existing, str) and existing.strip():
        return False
    # Place `version` right after `name` if `name` exists; else at the top.
    new_data: dict[str, Any] = {}
    inserted = False
    for k, v in data.items():
        new_data[k] = v
        if k == "name" and not inserted:
            new_data["version"] = _DEFAULT_PACKAGE_VERSION
            inserted = True
    if not inserted:
        new_data = {"version": _DEFAULT_PACKAGE_VERSION, **data}
    _write_package_json(root, new_data, trailing_newline=had_nl or True)
    return True


def fix_set_package_license(root: Path, _rule: Rule) -> bool:
    """Set `license` in `package.json` to the SPDX detected from LICENSE (JS002C fix).

    Deliberately a no-op if no LICENSE file is present — picking a license is
    a user decision, not a tool default. Users who want a license auto-applied
    should run a scaffold (which has a `license_text` post-action) first.
    """
    data, had_nl = _load_package_json(root)
    if data is None:
        return False
    existing = data.get("license")
    if isinstance(existing, str) and existing.strip():
        return False
    spdx = _detect_spdx_from_license(root)
    if spdx is None:
        return False
    data["license"] = spdx
    _write_package_json(root, data, trailing_newline=had_nl or True)
    return True


_FIX_KINDS: dict[str, FixFn] = {
    "create_default_readme": fix_create_default_readme,
    "create_default_gitignore": fix_create_default_gitignore,
    "create_default_editorconfig": fix_create_default_editorconfig,
    "add_text_auto_to_gitattributes": fix_add_text_auto_to_gitattributes,
    "append_engine_strict_to_npmrc": fix_append_engine_strict_to_npmrc,
    "append_ignore_scripts_to_npmrc": fix_append_ignore_scripts_to_npmrc,
    "create_default_dependabot": fix_create_default_dependabot,
    "create_default_codeql_workflow": fix_create_default_codeql_workflow,
    "create_default_contributing": fix_create_default_contributing,
    "create_default_code_of_conduct": fix_create_default_code_of_conduct,
    "create_default_security": fix_create_default_security,
    "create_default_changelog": fix_create_default_changelog,
    "create_default_codeowners": fix_create_default_codeowners,
    "create_default_pr_template": fix_create_default_pr_template,
    "create_default_scorecard_workflow": fix_create_default_scorecard_workflow,
    "create_default_security_insights": fix_create_default_security_insights,
    "set_package_manager": fix_set_package_manager,
    "set_engines_node": fix_set_engines_node,
    "set_package_name": fix_set_package_name,
    "set_package_version": fix_set_package_version,
    "set_package_license": fix_set_package_license,
}


def _resolve_fix(name: str) -> FixFn | None:
    """Resolve a fix kind. Built-in lookup first; `module:function` syntax second."""
    if name in _FIX_KINDS:
        return _FIX_KINDS[name]
    if ":" in name:
        module_path, _, attr = name.partition(":")
        try:
            module = importlib.import_module(module_path)
        except ImportError as exc:
            _log.warning("fix module import failed", extra={"fix": name, "error": str(exc)})
            return None
        fn = getattr(module, attr, None)
        if callable(fn):
            return cast(FixFn, fn)
    return None


def apply_fix(root: Path, rule: Rule, *, backup: bool = False) -> bool:
    """Apply a rule's bundled fix, if any. Returns True iff a change was made.

    When `backup=True`, any mutating fix that overwrites an existing file will
    first copy it to `<name>.bak`. Create-only fixes (which never overwrite)
    are unaffected. The flag is scoped to this call via a ContextVar — nested
    `apply_fix` calls or concurrent threads see independent values.
    """
    if rule.fix is None:
        return False
    fix_fn = _resolve_fix(rule.fix)
    if fix_fn is None:
        _log.warning("unknown fix kind", extra={"rule": rule.id, "fix": rule.fix})
        return False
    token = _BACKUP_MODE.set(backup)
    try:
        return bool(fix_fn(root, rule))
    except Exception as exc:  # noqa: BLE001 — surface as a warning, never raise
        _log.warning(
            "fix raised an exception",
            extra={"rule": rule.id, "fix": rule.fix, "error": str(exc)},
        )
        return False
    finally:
        _BACKUP_MODE.reset(token)


__all__ = [
    "Rule",
    "apply_fix",
    "load_bundled_pack",
    "load_rule_pack",
    "run_rules",
]
