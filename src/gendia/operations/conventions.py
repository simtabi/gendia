"""`gendia conventions` — repo hygiene + quality-control checker.

A drop-in replacement for `check-repo-conventions.sh`-style scripts that
validate a repository against shared documentation / structure rules.
The original bash version hardcoded everything; this one is JSON-driven
so each project (or each repo within one) can tune the rules without
patching the tool.

Why this lives in gendia: gendia already manages polyrepo workflows
(sync, audit, release). Convention drift is one more axis of fleet
hygiene — `audit` checks the *git* state, `conventions` checks the
*documentation/structure* state.

Surface:

    gendia conventions [PATH] [--rules FILE] [--strict] [--json] [--no-banner]

  - PATH defaults to the current directory (or, when run inside a gendia
    project, every repo in the project).
  - --rules points at a JSON file overriding the built-in defaults.
  - --strict promotes warnings to non-zero exit (rc=1).
  - --json emits a machine-parseable report for CI / log shippers.

Exit codes match `gendia doctor`: 0 = ok, 1 = warnings only, 2 = at
least one error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from gendia.config import load_config
from gendia.config.schema import ConfigError
from gendia.observability.logger import get_logger

_log = get_logger("operations.conventions")

Severity = Literal["ok", "warn", "error"]


# --- data classes -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Finding:
    """One concrete observation against a single repo."""

    severity: Severity
    rule: str
    message: str
    path: str | None = None
    fix: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "severity": self.severity,
            "rule": self.rule,
            "message": self.message,
        }
        if self.path:
            out["path"] = self.path
        if self.fix:
            out["fix"] = self.fix
        return out


@dataclass(frozen=True, slots=True)
class RepoReport:
    """Findings collected for one repo path."""

    repo: str
    findings: tuple[Finding, ...]

    @property
    def has_error(self) -> bool:
        return any(f.severity == "error" for f in self.findings)

    @property
    def has_warn(self) -> bool:
        return any(f.severity == "warn" for f in self.findings)


@dataclass(frozen=True, slots=True)
class Rules:
    """Tuneable conventions config. JSON-loadable; pure data, no I/O."""

    # 1. Required + optional GitHub-special files at repo root.
    github_special_required: tuple[str, ...] = (
        "LICENSE",
        "CONTRIBUTING.md",
        "CODE_OF_CONDUCT.md",
    )
    github_special_optional: tuple[str, ...] = (
        "CODEOWNERS",
        "SECURITY.md",
        "SUPPORT.md",
    )

    # 2. Filename casing for *.md (lowercase kebab-case, with exemptions).
    enforce_md_kebab_case: bool = True
    md_kebab_exempt_names: tuple[str, ...] = (
        "LICENSE",
        "CONTRIBUTING.md",
        "CODE_OF_CONDUCT.md",
        "CODEOWNERS",
        "SECURITY.md",
        "SUPPORT.md",
        "CHANGELOG.md",
        "CLAUDE.md",
        "README.md",
        "ISSUE_TEMPLATE",
        "PULL_REQUEST_TEMPLATE.md",
    )

    # 3. Forbidden glyphs (default: em-dash). Per-glyph exemption lists keep
    #    historical / external content out of scope.
    forbidden_chars: tuple[str, ...] = ("—",)
    forbidden_chars_exempt: tuple[str, ...] = ("CLAUDE.md", "changelog.md", "CHANGELOG.md")

    # 4. README frontmatter expectations (only enforced if a top-level
    #    readme.md / README.md exists).
    require_readme_frontmatter: bool = False
    readme_frontmatter_keys: tuple[str, ...] = ("Owner", "Last Updated")

    # 5. Decorator-emoji ban list. Status emojis (✅ ⏳ 📋 ⛔ 🔴 🟠 🟡 🟢) are
    #    deliberately not on this list.
    decorator_emojis: tuple[str, ...] = ("⭐", "🎯", "💼", "👔", "📢", "✨", "🚀", "🤖", "🎨")
    decorator_emojis_exempt: tuple[str, ...] = ("CLAUDE.md", "changelog.md", "CHANGELOG.md")

    # 6. Spec filename rules.
    spec_dir: str = "specs"
    forbid_date_prefixed_specs: bool = True
    forbid_number_prefixed_specs: bool = True

    # 7. Sub-folder readme.md ban (single tier index per repo).
    forbid_subfolder_readmes: bool = True

    # 8. Stale link patterns (numbered specs, sub-folder readmes).
    forbid_stale_link_patterns: bool = True

    # 9. Shell scripts must start with a shebang and be executable. (New.)
    require_shebang_for_sh: bool = True

    # 10. Trailing-whitespace ban in markdown. (New.)
    forbid_trailing_whitespace_md: bool = False

    # Walk-time exclusions.
    exclude_dirs: tuple[str, ...] = (
        ".git",
        ".claude",
        ".github",
        "node_modules",
        "vendor",
        "engineering",
        "internal",
        "public",
        "confidential",
        ".venv",
        "venv",
        "__pycache__",
        "dist",
        "build",
    )

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> Rules:
        if not raw:
            return cls()
        defaults = cls()

        def _tuple(key: str) -> tuple[str, ...]:
            value = raw.get(key)
            if value is None:
                fallback: tuple[str, ...] = getattr(defaults, key)
                return fallback
            return tuple(str(item) for item in value)

        def _bool(key: str) -> bool:
            return bool(raw.get(key, getattr(defaults, key)))

        def _str(key: str) -> str:
            return str(raw.get(key, getattr(defaults, key)))

        return cls(
            github_special_required=_tuple("github_special_required"),
            github_special_optional=_tuple("github_special_optional"),
            enforce_md_kebab_case=_bool("enforce_md_kebab_case"),
            md_kebab_exempt_names=_tuple("md_kebab_exempt_names"),
            forbidden_chars=_tuple("forbidden_chars"),
            forbidden_chars_exempt=_tuple("forbidden_chars_exempt"),
            require_readme_frontmatter=_bool("require_readme_frontmatter"),
            readme_frontmatter_keys=_tuple("readme_frontmatter_keys"),
            decorator_emojis=_tuple("decorator_emojis"),
            decorator_emojis_exempt=_tuple("decorator_emojis_exempt"),
            spec_dir=_str("spec_dir"),
            forbid_date_prefixed_specs=_bool("forbid_date_prefixed_specs"),
            forbid_number_prefixed_specs=_bool("forbid_number_prefixed_specs"),
            forbid_subfolder_readmes=_bool("forbid_subfolder_readmes"),
            forbid_stale_link_patterns=_bool("forbid_stale_link_patterns"),
            require_shebang_for_sh=_bool("require_shebang_for_sh"),
            forbid_trailing_whitespace_md=_bool("forbid_trailing_whitespace_md"),
            exclude_dirs=_tuple("exclude_dirs"),
        )


# --- file walker ------------------------------------------------------------


_KEBAB_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*\.md$")
_DATE_PREFIX_RE = re.compile(r"^20\d{2}-")
_NUMBER_PREFIX_RE = re.compile(r"^\d{3}-[a-z][a-z0-9-]*\.md$")
_STALE_LINK_RE = re.compile(
    r"\]\([^)]*/\d{3}-[a-z][a-z0-9-]*\.md\)|\]\([a-z][a-z0-9-]*/readme\.md\)",
)

# A markdown file at depth 3+ (subdir/subsubdir/readme.md) is the case
# the "single tier index" rule cares about; depth 1-2 is the tier readme
# itself or one nested layer that happens to live in another package.
_MIN_DEPTH_FOR_SUBFOLDER = 3


def _walk_md_files(root: Path, rules: Rules) -> Iterator[Path]:
    excluded = {Path(d) for d in rules.exclude_dirs}
    for path in root.rglob("*.md"):
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        if any(
            part in {p.name for p in excluded} or Path(part) in excluded for part in rel.parts[:-1]
        ):
            continue
        if any(part.startswith(".") and part not in {".github"} for part in rel.parts[:-1]):
            # Already handled by exclude_dirs for the common ones; this keeps
            # us defensive against other dotted dirs (e.g. .pytest_cache).
            continue
        yield path


def _read_text_safely(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


# --- individual checks ------------------------------------------------------


def _check_github_special(root: Path, rules: Rules) -> Iterator[Finding]:
    for name in rules.github_special_required:
        if (root / name).is_file():
            yield Finding("ok", "github-special", f"required file present: {name}")
        else:
            yield Finding(
                "error",
                "github-special",
                f"missing required GitHub-special file: {name}",
                fix=f"create a {name} at the repo root",
            )
    for name in rules.github_special_optional:
        if (root / name).is_file():
            yield Finding("ok", "github-special", f"optional file present: {name}")


def _check_md_kebab_case(root: Path, rules: Rules) -> Iterator[Finding]:
    if not rules.enforce_md_kebab_case:
        return
    bad: list[Path] = []
    exempt = set(rules.md_kebab_exempt_names)
    for md in _walk_md_files(root, rules):
        rel = md.relative_to(root)
        # CLAUDE.md is exempt anywhere (project-config convention).
        if md.name in exempt or md.name == "CLAUDE.md":
            continue
        # Issue templates, PR templates etc. live under .github/ which is
        # already excluded — but defensively keep this check.
        if any(part == ".github" for part in rel.parts):
            continue
        if not _KEBAB_RE.match(md.name):
            bad.append(rel)

    if bad:
        for rel in bad:
            yield Finding(
                "error",
                "md-kebab-case",
                f"non-lowercase markdown filename: {rel.as_posix()}",
                path=rel.as_posix(),
                fix=f"rename to {rel.parent.as_posix()}/{rel.name.lower()}".lstrip("./"),
            )
    else:
        yield Finding("ok", "md-kebab-case", "all markdown filenames are kebab-case")


def _check_forbidden_chars(root: Path, rules: Rules) -> Iterator[Finding]:
    if not rules.forbidden_chars:
        return
    exempt = set(rules.forbidden_chars_exempt)
    offenders: dict[str, list[str]] = {}
    for md in _walk_md_files(root, rules):
        if md.name in exempt:
            continue
        text = _read_text_safely(md)
        if text is None:
            continue
        for glyph in rules.forbidden_chars:
            if glyph in text:
                offenders.setdefault(glyph, []).append(md.relative_to(root).as_posix())

    if not offenders:
        yield Finding("ok", "forbidden-chars", "no forbidden characters found in current docs")
        return

    for glyph, files in offenders.items():
        codepoint = f"U+{ord(glyph):04X}" if len(glyph) == 1 else f"glyph={glyph!r}"
        for f in files:
            yield Finding(
                "warn",
                "forbidden-chars",
                f"{codepoint} present in {f}",
                path=f,
                fix=f"replace {glyph!r} with the ASCII equivalent",
            )


def _check_readme_frontmatter(root: Path, rules: Rules) -> Iterator[Finding]:
    if not rules.require_readme_frontmatter:
        return
    candidate = next(
        (p for p in (root / "readme.md", root / "README.md") if p.is_file()),
        None,
    )
    if candidate is None:
        return

    text = _read_text_safely(candidate) or ""
    head = "\n".join(text.splitlines()[:20])
    missing = []
    for key in rules.readme_frontmatter_keys:
        # Accept either "**Key**" or "Key:" prefix anywhere in the head.
        pattern = rf"(?i)(\*\*{re.escape(key)}\b|\b{re.escape(key)}:)"
        if not re.search(pattern, head):
            missing.append(key)

    if missing:
        yield Finding(
            "warn",
            "readme-frontmatter",
            f"{candidate.name} missing frontmatter keys: {', '.join(missing)}",
            path=candidate.name,
            fix="add `**Owner:** ...` and `**Last Updated:** ...` near the top of the readme",
        )
    else:
        yield Finding("ok", "readme-frontmatter", f"{candidate.name} frontmatter present")


def _check_decorator_emojis(root: Path, rules: Rules) -> Iterator[Finding]:
    if not rules.decorator_emojis:
        return
    exempt = set(rules.decorator_emojis_exempt)
    pattern = re.compile("|".join(re.escape(e) for e in rules.decorator_emojis))
    offenders: list[str] = []
    for md in _walk_md_files(root, rules):
        if md.name in exempt:
            continue
        text = _read_text_safely(md)
        if text and pattern.search(text):
            offenders.append(md.relative_to(root).as_posix())

    if offenders:
        for f in offenders:
            yield Finding(
                "warn",
                "decorator-emojis",
                f"decorator emoji(s) present in {f}",
                path=f,
                fix="remove ornamental emojis; status emojis (✅ ⏳ 📋 ⛔) are fine",
            )
    else:
        yield Finding("ok", "decorator-emojis", "no decorator emojis found")


def _check_spec_filenames(root: Path, rules: Rules) -> Iterator[Finding]:
    spec_dir = root / rules.spec_dir
    if not spec_dir.is_dir():
        return

    if rules.forbid_date_prefixed_specs:
        bad = [p for p in spec_dir.rglob("*.md") if _DATE_PREFIX_RE.match(p.name)]
        if bad:
            for p in bad:
                yield Finding(
                    "error",
                    "spec-date-prefix",
                    f"date-prefixed spec filename: {p.relative_to(root).as_posix()}",
                    path=p.relative_to(root).as_posix(),
                    fix="move the date into the file's frontmatter; rename without the prefix",
                )
        else:
            yield Finding("ok", "spec-date-prefix", "no date-prefixed spec filenames")

    if rules.forbid_number_prefixed_specs:
        bad = [p for p in spec_dir.iterdir() if p.is_file() and _NUMBER_PREFIX_RE.match(p.name)]
        if bad:
            for p in bad:
                yield Finding(
                    "error",
                    "spec-number-prefix",
                    f"number-prefixed spec filename: {p.relative_to(root).as_posix()}",
                    path=p.relative_to(root).as_posix(),
                    fix="track sequential IDs in the readme index, not the filename",
                )
        else:
            yield Finding("ok", "spec-number-prefix", "no number-prefixed spec filenames")


def _check_subfolder_readmes(root: Path, rules: Rules) -> Iterator[Finding]:
    if not rules.forbid_subfolder_readmes:
        return
    # A path with 3+ parts (subdir/subsubdir/readme.md) is at least two
    # directories deep — the tier readme should live at parts == 1.
    bad: list[str] = []
    for md in _walk_md_files(root, rules):
        rel = md.relative_to(root)
        if md.name.lower() == "readme.md" and len(rel.parts) >= _MIN_DEPTH_FOR_SUBFOLDER:
            bad.append(rel.as_posix())

    if bad:
        for f in bad:
            yield Finding(
                "warn",
                "subfolder-readme",
                f"sub-folder readme.md: {f}",
                path=f,
                fix="fold its content into the tier readme; the index is one file per repo",
            )
    else:
        yield Finding("ok", "subfolder-readme", "no sub-folder readme.md files")


def _check_stale_link_patterns(root: Path, rules: Rules) -> Iterator[Finding]:
    if not rules.forbid_stale_link_patterns:
        return
    offenders: list[str] = []
    for md in _walk_md_files(root, rules):
        if md.name.lower() == "changelog.md":
            continue
        text = _read_text_safely(md)
        if text and _STALE_LINK_RE.search(text):
            offenders.append(md.relative_to(root).as_posix())

    if offenders:
        for f in offenders:
            yield Finding(
                "warn",
                "stale-link-pattern",
                f"links to numbered specs or sub-folder readmes: {f}",
                path=f,
                fix="update links to point at the new flat layout",
            )
    else:
        yield Finding("ok", "stale-link-pattern", "no stale link patterns")


def _check_shell_shebang(root: Path, rules: Rules) -> Iterator[Finding]:
    if not rules.require_shebang_for_sh:
        return
    excluded = {p.name for p in (Path(d) for d in rules.exclude_dirs)}
    bad_shebang: list[str] = []
    bad_mode: list[str] = []
    for sh in root.rglob("*.sh"):
        rel = sh.relative_to(root)
        if any(part in excluded for part in rel.parts[:-1]):
            continue
        first = ""
        try:
            with sh.open("rb") as fh:
                first = fh.readline(256).decode("utf-8", errors="replace").rstrip("\n")
        except OSError:
            continue
        if not first.startswith("#!"):
            bad_shebang.append(rel.as_posix())
        # Executable bit (any-user). 0o111 = at least one x.
        if not (sh.stat().st_mode & 0o111):
            bad_mode.append(rel.as_posix())

    if not bad_shebang and not bad_mode:
        yield Finding("ok", "sh-shebang", "all *.sh files have a shebang and are executable")
        return
    for f in bad_shebang:
        yield Finding(
            "error",
            "sh-shebang",
            f"missing shebang line: {f}",
            path=f,
            fix="add `#!/usr/bin/env bash` (or sh) as the first line",
        )
    for f in bad_mode:
        yield Finding(
            "warn",
            "sh-shebang",
            f"not executable: {f}",
            path=f,
            fix=f"chmod +x {f}",
        )


def _check_trailing_whitespace(root: Path, rules: Rules) -> Iterator[Finding]:
    if not rules.forbid_trailing_whitespace_md:
        return
    offenders: list[tuple[str, int]] = []
    for md in _walk_md_files(root, rules):
        text = _read_text_safely(md)
        if text is None:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if line != line.rstrip():
                offenders.append((md.relative_to(root).as_posix(), lineno))
                break  # one finding per file is enough.

    if not offenders:
        yield Finding("ok", "trailing-whitespace", "no trailing whitespace in markdown")
        return
    for f, lineno in offenders:
        yield Finding(
            "warn",
            "trailing-whitespace",
            f"trailing whitespace at {f}:{lineno}",
            path=f,
            fix="strip trailing whitespace (your editor probably has a setting)",
        )


# --- orchestrator -----------------------------------------------------------


_CHECKS: tuple[Any, ...] = (
    _check_github_special,
    _check_md_kebab_case,
    _check_forbidden_chars,
    _check_readme_frontmatter,
    _check_decorator_emojis,
    _check_spec_filenames,
    _check_subfolder_readmes,
    _check_stale_link_patterns,
    _check_shell_shebang,
    _check_trailing_whitespace,
)


def run_checks(root: Path, rules: Rules) -> tuple[Finding, ...]:
    """Run every enabled check against `root` and return the flat findings tuple."""
    findings: list[Finding] = []
    for check in _CHECKS:
        for finding in check(root, rules):
            findings.append(finding)
    return tuple(findings)


# --- subparser + dispatch ---------------------------------------------------


def add_subparser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subparsers.add_parser(
        "conventions",
        help="Lint repo hygiene: naming, docs, GitHub-special files, ban-list glyphs.",
    )
    p.add_argument(
        "path",
        nargs="?",
        type=Path,
        default=None,
        help="Path to check (default: every repo in the project, or '.' if no project).",
    )
    p.add_argument(
        "--rules",
        type=Path,
        default=None,
        help="Path to a JSON rules file (overrides the built-in defaults).",
    )
    p.add_argument(
        "--strict",
        action="store_true",
        help="Promote warnings to a non-zero exit (rc=1).",
    )
    p.add_argument(
        "--json",
        dest="json_out",
        action="store_true",
        help="Emit findings as JSON instead of the human report.",
    )


def _load_rules(path: Path | None) -> Rules:
    if path is None:
        return Rules()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"failed to read rules file {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"rules file {path} must be a JSON object at the top level")
    return Rules.from_dict(raw)


def _iter_targets(args: argparse.Namespace) -> Iterable[tuple[str, Path]]:
    if args.path is not None:
        yield (args.path.name or str(args.path), args.path.resolve())
        return
    try:
        cfg = load_config(project_path=getattr(args, "config", None))
    except ConfigError:
        cfg = None
    if cfg is None or cfg.project is None:
        yield (".", Path.cwd())
        return
    project = cfg.project
    for repo in project.repos:
        yield (repo.dir, (project.root / repo.dir).resolve())


def _render_human(reports: tuple[RepoReport, ...]) -> None:
    glyph = {"ok": "✓", "warn": "⚠", "error": "✗"}
    for report in reports:
        print(f"== {report.repo} ==")
        if not report.findings:
            print("  (no findings)")
        for finding in report.findings:
            line = f"  {glyph[finding.severity]} [{finding.rule}] {finding.message}"
            print(line)
            if finding.fix:
                print(f"      fix: {finding.fix}")
        print()


def _render_json(reports: tuple[RepoReport, ...]) -> None:
    payload = {
        "reports": [
            {
                "repo": r.repo,
                "findings": [f.to_dict() for f in r.findings],
            }
            for r in reports
        ]
    }
    json.dump(payload, sys.stdout, indent=2)
    sys.stdout.write("\n")


def dispatch(args: argparse.Namespace) -> int:
    try:
        rules = _load_rules(args.rules)
    except ConfigError as exc:
        print(f"conventions: {exc}", file=sys.stderr)
        return 2

    reports: list[RepoReport] = []
    for label, root in _iter_targets(args):
        if not root.is_dir():
            reports.append(
                RepoReport(
                    repo=label,
                    findings=(
                        Finding(
                            "error",
                            "path-missing",
                            f"path does not exist or is not a directory: {root}",
                            fix="run `gendia mirror` first or fix the project root.",
                        ),
                    ),
                )
            )
            continue
        reports.append(RepoReport(repo=label, findings=run_checks(root, rules)))

    if args.json_out:
        _render_json(tuple(reports))
    else:
        _render_human(tuple(reports))

    if any(r.has_error for r in reports):
        return 2
    if args.strict and any(r.has_warn for r in reports):
        return 1
    return 0


# Re-export for tests / programmatic use.
__all__ = ("Finding", "RepoReport", "Rules", "run_checks", "dispatch", "add_subparser")
