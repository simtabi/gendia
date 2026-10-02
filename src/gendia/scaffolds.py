"""Repo scaffolding from bundled templates.

A scaffold = JSON manifest (variables, files, after-actions) + a directory of
template files. Variables are substituted via Python's `string.Template`
(`${var}` syntax). Templates whose source path ends in `.tmpl` are rendered;
binary-safe files (e.g., `.gitignore`) are copied as-is.

This module is one file by design — schema, loader, renderer, builder, and
post-actions live together because they share state. Adding a new scaffold =
JSON manifest + directory of template files. Adding a new post-action =
register a function in `_ACTION_HANDLERS`.
"""

from __future__ import annotations

import json
import re
import string
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

from gendia.observability.logger import get_logger

_log = get_logger("scaffolds")


# --- schema -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VariableSpec:
    """One template variable's contract."""

    name: str
    description: str = ""
    required: bool = False
    default: str | None = None
    validate: str | None = None  # regex pattern
    choices: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_dict(cls, name: str, payload: dict[str, Any]) -> VariableSpec:
        return cls(
            name=name,
            description=str(payload.get("description", "")),
            required=bool(payload.get("required", False)),
            default=payload.get("default"),
            validate=payload.get("validate"),
            choices=tuple(str(c) for c in (payload.get("choices") or ())),
        )


@dataclass(frozen=True, slots=True)
class FileTemplate:
    """One template-file → destination-path mapping."""

    src: str  # relative path under data/scaffolds/ (may include _common/)
    dest: str  # destination path under target dir; may contain ${vars}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> FileTemplate:
        return cls(src=str(payload["src"]), dest=str(payload["dest"]))


@dataclass(frozen=True, slots=True)
class AfterAction:
    """One post-scaffold side-effect (`git_init`, `license_text`, `audit`)."""

    kind: str
    args: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> AfterAction:
        return cls(
            kind=str(payload["kind"]),
            args={k: v for k, v in payload.items() if k != "kind"},
        )


@dataclass(frozen=True, slots=True)
class ScaffoldTemplate:
    """A scaffold's full manifest."""

    id: str
    name: str
    description: str
    languages: tuple[str, ...] = field(default_factory=tuple)
    tags: tuple[str, ...] = field(default_factory=tuple)
    variables: tuple[VariableSpec, ...] = field(default_factory=tuple)
    files: tuple[FileTemplate, ...] = field(default_factory=tuple)
    after_scaffold: tuple[AfterAction, ...] = field(default_factory=tuple)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ScaffoldTemplate:
        try:
            scaffold_id = str(payload["id"])
        except KeyError as exc:
            raise ValueError(f"scaffold missing required field: {exc.args[0]!r}") from exc

        vars_payload = payload.get("variables") or {}
        variables = tuple(
            VariableSpec.from_dict(name, spec or {}) for name, spec in vars_payload.items()
        )

        return cls(
            id=scaffold_id,
            name=str(payload.get("name", scaffold_id)),
            description=str(payload.get("description", "")),
            languages=tuple(str(x) for x in (payload.get("languages") or ())),
            tags=tuple(str(x) for x in (payload.get("tags") or ())),
            variables=variables,
            files=tuple(
                FileTemplate.from_dict(f) for f in payload.get("files") or () if isinstance(f, dict)
            ),
            after_scaffold=tuple(
                AfterAction.from_dict(a)
                for a in payload.get("after_scaffold") or ()
                if isinstance(a, dict)
            ),
        )


# --- loaders ----------------------------------------------------------------


def list_bundled_scaffolds() -> tuple[str, ...]:
    """Return ids of all bundled scaffold templates (sorted)."""
    try:
        scaffolds_dir = resources.files("gendia").joinpath("data/scaffolds")
        names: list[str] = []
        for entry in scaffolds_dir.iterdir():
            name = entry.name
            if entry.is_file() and name.endswith(".json"):
                names.append(name[: -len(".json")])
        return tuple(sorted(names))
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        return ()


def load_bundled_scaffold(scaffold_id: str) -> ScaffoldTemplate | None:
    """Load `gendia/data/scaffolds/{id}.json`. Returns None on miss."""
    try:
        text = (
            resources.files("gendia")
            .joinpath(f"data/scaffolds/{scaffold_id}.json")
            .read_text(encoding="utf-8")
        )
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        return None
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        _log.warning("scaffold invalid JSON", extra={"id": scaffold_id, "error": str(exc)})
        return None
    if not isinstance(raw, dict):
        return None
    try:
        return ScaffoldTemplate.from_dict(raw)
    except (ValueError, TypeError) as exc:
        _log.warning("scaffold invalid shape", extra={"id": scaffold_id, "error": str(exc)})
        return None


def _read_template_file(src: str) -> str | None:
    """Read a file from the bundled scaffolds directory. None on miss."""
    try:
        return (
            resources.files("gendia").joinpath(f"data/scaffolds/{src}").read_text(encoding="utf-8")
        )
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        return None


# --- variable resolution ----------------------------------------------------


def _builtins(git_identity: dict[str, str] | None) -> dict[str, str]:
    out: dict[str, str] = {
        "now.year": str(datetime.now(UTC).year),
    }
    if git_identity:
        if git_identity.get("name"):
            out["gendia.git_identity.name"] = git_identity["name"]
        if git_identity.get("email"):
            out["gendia.git_identity.email"] = git_identity["email"]
    return out


def resolve_variables(
    spec: ScaffoldTemplate,
    user_values: dict[str, str],
    *,
    git_identity: dict[str, str] | None = None,
) -> dict[str, str]:
    """Combine spec defaults + user-provided values into a final mapping.

    Resolution order (per variable, first wins):
      1. user-supplied value
      2. default (with ${var} / ${builtin} substitution against already-resolved + builtins)
      3. empty string (when not required)

    Raises ValueError when a required variable is missing or fails validation.
    """
    builtins = _builtins(git_identity)
    resolved: dict[str, str] = {}

    for var in spec.variables:
        if var.name in user_values:
            value = user_values[var.name]
        elif var.default is not None:
            ctx = {**builtins, **resolved}
            value = _DottedTemplate(var.default).safe_substitute(ctx)
        elif var.required:
            raise ValueError(f"required variable missing: {var.name}")
        else:
            value = ""

        if var.choices and value not in var.choices:
            raise ValueError(f"variable {var.name}={value!r} not in choices {list(var.choices)}")
        if var.validate and not re.match(var.validate, value):
            raise ValueError(
                f"variable {var.name}={value!r} fails validation pattern {var.validate!r}"
            )
        resolved[var.name] = value

    return {**builtins, **resolved}


class _DottedTemplate(string.Template):
    """Like `string.Template` but accepts `${foo.bar}` and `${foo.bar.baz}`.

    The standard pattern only allows `[_a-z][_a-z0-9]*`. We extend it so the
    JSON manifests can use the doc-friendly names `${now.year}` and
    `${gendia.git_identity.name}` directly without renaming.
    """

    idpattern = r"(?a:[_a-z][_a-z0-9]*(?:\.[_a-z][_a-z0-9]*)*)"


def render_text(text: str, vars_: dict[str, str]) -> str:
    """Apply ${var} substitution. Unknown vars are left as-is (`safe_substitute`).

    Dotted names are supported (e.g. `${now.year}`, `${gendia.git_identity.name}`).
    Use `render_text_strict()` when an unsubstituted `${...}` should be an error
    (e.g., destination paths).
    """
    return _DottedTemplate(text).safe_substitute(vars_)


_UNRESOLVED_VAR_RE = re.compile(r"\$\{[_a-z][_a-z0-9.]*\}", flags=re.IGNORECASE)


def render_text_strict(text: str, vars_: dict[str, str]) -> str:
    """Same as `render_text` but raises if any `${var}` survives the substitution.

    Used for destination-path rendering, where leaving a literal `${var}` in
    the path silently creates a directory called `${var}` — almost certainly
    not what the rule-pack author meant.
    """
    rendered = render_text(text, vars_)
    leftover = _UNRESOLVED_VAR_RE.findall(rendered)
    if leftover:
        raise ValueError(f"unresolved template variables in {text!r}: {leftover}")
    return rendered


# --- builder ----------------------------------------------------------------


@dataclass
class FileOp:
    """One file-write outcome from a scaffold render."""

    kind: str  # "create" | "skip-existing" | "would-create" | "would-skip" | "missing-source"
    dest: Path
    bytes_written: int = 0


def apply(  # noqa: PLR0913 — every flag here is independently meaningful
    spec: ScaffoldTemplate,
    target: Path,
    user_values: dict[str, str],
    *,
    dry_run: bool = False,
    merge: bool = False,
    force: bool = False,
    git_identity: dict[str, str] | None = None,
) -> list[FileOp]:
    """Render every file in `spec.files` into `target`.

    Modes:
      * default — refuse to overwrite existing files; report `skip-existing`.
      * `merge=True` — same as default; explicit name for the use case (retrofit).
      * `force=True` — overwrite existing files.
      * `dry_run=True` — report the plan without writing.

    After files are written (in non-dry-run mode), `spec.after_scaffold`
    actions run (`git_init`, `license_text`, `audit`).
    """
    target = target.expanduser().resolve()
    resolved = resolve_variables(spec, user_values, git_identity=git_identity)
    ops: list[FileOp] = []

    if not dry_run:
        target.mkdir(parents=True, exist_ok=True)

    for ft in spec.files:
        # Strict substitution for dest paths: an unsubstituted `${var}` is
        # almost certainly a manifest bug, not a creative choice.
        try:
            dest_rel = render_text_strict(ft.dest, resolved)
        except ValueError as exc:
            _log.warning(
                "scaffold dest has unresolved variables",
                extra={"src": ft.src, "dest": ft.dest, "error": str(exc)},
            )
            ops.append(FileOp(kind="unresolved-var", dest=target / ft.dest))
            continue

        dest = target / dest_rel

        # Path-traversal guard #1: refuse symlinked dests. If a pre-existing
        # `target/README.md` is a symlink pointing outside `target`,
        # writing through it would clobber files the user didn't intend.
        if dest.is_symlink():
            _log.warning(
                "scaffold dest is a pre-existing symlink; refusing to follow",
                extra={"src": ft.src, "dest": str(dest)},
            )
            ops.append(FileOp(kind="symlink-refused", dest=dest))
            continue

        # Path-traversal guard #2: a malicious or buggy template manifest
        # could produce `dest` outside the target tree (e.g. via
        # `../../etc/passwd` or an absolute path). Refuse to write outside.
        try:
            dest_abs = dest.resolve() if dest.exists() else (target / dest_rel).resolve()
        except OSError:
            _log.warning("scaffold dest unresolvable", extra={"src": ft.src, "dest": str(dest)})
            ops.append(FileOp(kind="path-escape", dest=dest))
            continue
        if not dest_abs.is_relative_to(target):
            _log.warning(
                "scaffold dest escapes target; refusing to write",
                extra={"src": ft.src, "dest": str(dest_abs)},
            )
            ops.append(FileOp(kind="path-escape", dest=dest))
            continue

        content = _read_template_file(ft.src)
        if content is None:
            _log.warning("template source missing", extra={"src": ft.src})
            ops.append(FileOp(kind="missing-source", dest=dest))
            continue

        if ft.src.endswith(".tmpl"):
            content = render_text(content, resolved)

        already_exists = dest.exists()
        if already_exists and not force:
            # `merge` is informational — it's the default behaviour. We
            # report a distinct kind so callers can choose to treat
            # skip-existing as warn (default) or ok (merge mode).
            kind = "would-skip" if dry_run else "skip-existing"
            ops.append(FileOp(kind=kind, dest=dest))
            continue

        if dry_run:
            ops.append(
                FileOp(
                    kind="would-create",
                    dest=dest,
                    bytes_written=len(content.encode("utf-8")),
                )
            )
            continue

        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        ops.append(
            FileOp(
                kind="overwrite" if already_exists else "create",
                dest=dest,
                bytes_written=len(content.encode("utf-8")),
            )
        )

    if not dry_run:
        run_after_actions(spec, target, resolved, ops=ops)

    return ops


# --- post-actions -----------------------------------------------------------


def run_after_actions(
    spec: ScaffoldTemplate,
    target: Path,
    resolved: dict[str, str],
    *,
    ops: list[FileOp] | None = None,
) -> None:
    """Execute every `after_scaffold` action in order. Failures are logged.

    When `ops` is provided, each handler appends a synthetic `FileOp` with a
    `post:<kind>` prefix so the caller can render a unified report.
    """
    sink: list[FileOp] = ops if ops is not None else []
    for action in spec.after_scaffold:
        handler = _ACTION_HANDLERS.get(action.kind)
        if handler is None:
            _log.warning("unknown after-action kind", extra={"kind": action.kind})
            sink.append(FileOp(kind=f"post:{action.kind}-unknown", dest=target))
            continue
        try:
            handler(target, action.args, resolved, sink)
        except Exception as exc:  # noqa: BLE001 — surface as a log, not a raise
            _log.warning(
                "after-action failed",
                extra={"kind": action.kind, "error": str(exc)},
            )
            sink.append(FileOp(kind=f"post:{action.kind}-failed", dest=target))


def _action_git_init(
    target: Path,
    args: dict[str, Any],
    resolved: dict[str, str],
    ops: list[FileOp],
) -> None:
    if (target / ".git").exists():
        ops.append(FileOp(kind="post:git-init-skipped", dest=target / ".git"))
        return
    branch = render_text(str(args.get("default_branch", "main")), resolved)
    subprocess.run(
        ["git", "init", "-q", "-b", branch],
        cwd=target,
        check=True,
        timeout=10.0,
        capture_output=True,
    )
    ops.append(FileOp(kind=f"post:git-init({branch})", dest=target / ".git"))


def _action_license_text(
    target: Path,
    args: dict[str, Any],
    resolved: dict[str, str],
    ops: list[FileOp],
) -> None:
    spdx = render_text(str(args.get("spdx", "MIT")), resolved)
    dest = target / render_text(str(args.get("dest", "LICENSE")), resolved)
    if dest.exists():
        ops.append(FileOp(kind="post:license-skipped", dest=dest))
        return
    template = _LICENSE_TEXTS.get(spdx)
    if template is None:
        _log.info("no bundled license text for SPDX id", extra={"spdx": spdx})
        ops.append(FileOp(kind=f"post:license-unknown({spdx})", dest=dest))
        return
    holder = render_text(str(args.get("holder", "")), resolved)
    year = render_text(str(args.get("year", "")), resolved)
    body = template.format(year=year or "YYYY", holder=holder or "Copyright Holder")
    dest.write_text(body, encoding="utf-8")
    ops.append(
        FileOp(
            kind=f"post:license({spdx})",
            dest=dest,
            bytes_written=len(body.encode("utf-8")),
        )
    )


def _action_audit(
    target: Path,
    args: dict[str, Any],
    resolved: dict[str, str],
    ops: list[FileOp],
) -> None:
    """Run `gendia.standards` rules and append a one-line summary."""
    from gendia import standards  # noqa: PLC0415 — lazy to avoid import cycle in cold-start

    profile = render_text(str(args.get("profile", "core")), resolved)
    rules = standards.load_bundled_pack(profile)
    if not rules:
        return
    findings = standards.run_rules(target, rules)
    errors = sum(1 for f in findings if f.severity == "error")
    warns = sum(1 for f in findings if f.severity == "warn")
    summary = "clean" if not findings else f"{errors}E/{warns}W"
    ops.append(FileOp(kind=f"post:audit({profile})={summary}", dest=target))


_ACTION_HANDLERS: dict[
    str,
    Callable[[Path, dict[str, Any], dict[str, str], list[FileOp]], None],
] = {
    "git_init": _action_git_init,
    "license_text": _action_license_text,
    "audit": _action_audit,
}


# Small built-in license corpus for the most common SPDX ids. Keeps the
# scaffold path self-contained — no network fetch required at scaffold time.
_LICENSE_TEXTS: dict[str, str] = {
    "MIT": (
        "MIT License\n\n"
        "Copyright (c) {year} {holder}\n\n"
        "Permission is hereby granted, free of charge, to any person obtaining a copy\n"
        'of this software and associated documentation files (the "Software"), to deal\n'
        "in the Software without restriction, including without limitation the rights\n"
        "to use, copy, modify, merge, publish, distribute, sublicense, and/or sell\n"
        "copies of the Software, and to permit persons to whom the Software is\n"
        "furnished to do so, subject to the following conditions:\n\n"
        "The above copyright notice and this permission notice shall be included in all\n"
        "copies or substantial portions of the Software.\n\n"
        'THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR\n'
        "IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,\n"
        "FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE\n"
        "AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER\n"
        "LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,\n"
        "OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE\n"
        "SOFTWARE.\n"
    ),
    "Apache-2.0": (
        "                                 Apache License\n"
        "                           Version 2.0, January 2004\n"
        "                        http://www.apache.org/licenses/\n\n"
        "Copyright {year} {holder}\n\n"
        'Licensed under the Apache License, Version 2.0 (the "License");\n'
        "you may not use this file except in compliance with the License.\n"
        "You may obtain a copy of the License at\n\n"
        "    http://www.apache.org/licenses/LICENSE-2.0\n\n"
        "Unless required by applicable law or agreed to in writing, software\n"
        'distributed under the License is distributed on an "AS IS" BASIS,\n'
        "WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.\n"
        "See the License for the specific language governing permissions and\n"
        "limitations under the License.\n"
    ),
    "BSD-3-Clause": (
        "BSD 3-Clause License\n\n"
        "Copyright (c) {year}, {holder}\n"
        "All rights reserved.\n\n"
        "Redistribution and use in source and binary forms, with or without\n"
        "modification, are permitted provided that the following conditions are met:\n\n"
        "1. Redistributions of source code must retain the above copyright notice,\n"
        "   this list of conditions and the following disclaimer.\n\n"
        "2. Redistributions in binary form must reproduce the above copyright notice,\n"
        "   this list of conditions and the following disclaimer in the documentation\n"
        "   and/or other materials provided with the distribution.\n\n"
        "3. Neither the name of the copyright holder nor the names of its contributors\n"
        "   may be used to endorse or promote products derived from this software\n"
        "   without specific prior written permission.\n\n"
        'THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"\n'
        "AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE\n"
        "IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE\n"
        "DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE\n"
        "FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL\n"
        "DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR\n"
        "SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER\n"
        "CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,\n"
        "OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE\n"
        "OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.\n"
    ),
    "BSD-2-Clause": (
        "BSD 2-Clause License\n\n"
        "Copyright (c) {year}, {holder}\n"
        "All rights reserved.\n\n"
        "Redistribution and use in source and binary forms, with or without\n"
        "modification, are permitted provided that the following conditions are met:\n\n"
        "1. Redistributions of source code must retain the above copyright notice,\n"
        "   this list of conditions and the following disclaimer.\n\n"
        "2. Redistributions in binary form must reproduce the above copyright notice,\n"
        "   this list of conditions and the following disclaimer in the documentation\n"
        "   and/or other materials provided with the distribution.\n\n"
        'THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"\n'
        "AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE\n"
        "IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE\n"
        "DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE\n"
        "FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL\n"
        "DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR\n"
        "SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER\n"
        "CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,\n"
        "OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE\n"
        "OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.\n"
    ),
    "ISC": (
        "ISC License\n\n"
        "Copyright (c) {year} {holder}\n\n"
        "Permission to use, copy, modify, and/or distribute this software for any\n"
        "purpose with or without fee is hereby granted, provided that the above\n"
        "copyright notice and this permission notice appear in all copies.\n\n"
        'THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH\n'
        "REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY\n"
        "AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT,\n"
        "INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM\n"
        "LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR\n"
        "OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR\n"
        "PERFORMANCE OF THIS SOFTWARE.\n"
    ),
    "Unlicense": (
        "This is free and unencumbered software released into the public domain.\n\n"
        "Anyone is free to copy, modify, publish, use, compile, sell, or distribute\n"
        "this software, either in source code form or as a compiled binary, for any\n"
        "purpose, commercial or non-commercial, and by any means.\n\n"
        "In jurisdictions that recognize copyright laws, the author or authors of this\n"
        "software dedicate any and all copyright interest in the software to the\n"
        "public domain. We make this dedication for the benefit of the public at large\n"
        "and to the detriment of our heirs and successors. We intend this dedication\n"
        "to be an overt act of relinquishment in perpetuity of all present and future\n"
        "rights to this software under copyright law.\n\n"
        'THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR\n'
        "IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,\n"
        "FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE\n"
        "AUTHORS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN\n"
        "ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION\n"
        "WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.\n\n"
        "For more information, please refer to <https://unlicense.org>\n"
    ),
    "GPL-3.0-or-later": (
        "                    GNU GENERAL PUBLIC LICENSE\n"
        "                       Version 3, 29 June 2007\n\n"
        " Copyright (C) {year} {holder}\n\n"
        " Everyone is permitted to copy and distribute verbatim copies\n"
        " of this license document, but changing it is not allowed.\n\n"
        "This program is free software: you can redistribute it and/or modify\n"
        "it under the terms of the GNU General Public License as published by\n"
        "the Free Software Foundation, either version 3 of the License, or\n"
        "(at your option) any later version.\n\n"
        "This program is distributed in the hope that it will be useful,\n"
        "but WITHOUT ANY WARRANTY; without even the implied warranty of\n"
        "MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the\n"
        "GNU General Public License for more details.\n\n"
        "You should have received a copy of the GNU General Public License\n"
        "along with this program. If not, see <https://www.gnu.org/licenses/>.\n\n"
        "The full license text is available at:\n"
        "  https://www.gnu.org/licenses/gpl-3.0.txt\n"
    ),
    "LGPL-3.0-or-later": (
        "                   GNU LESSER GENERAL PUBLIC LICENSE\n"
        "                       Version 3, 29 June 2007\n\n"
        " Copyright (C) {year} {holder}\n\n"
        " Everyone is permitted to copy and distribute verbatim copies\n"
        " of this license document, but changing it is not allowed.\n\n"
        "This library is free software: you can redistribute it and/or modify\n"
        "it under the terms of the GNU Lesser General Public License as published\n"
        "by the Free Software Foundation, either version 3 of the License, or\n"
        "(at your option) any later version.\n\n"
        "This library is distributed in the hope that it will be useful,\n"
        "but WITHOUT ANY WARRANTY; without even the implied warranty of\n"
        "MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the\n"
        "GNU Lesser General Public License for more details.\n\n"
        "You should have received a copy of the GNU Lesser General Public License\n"
        "along with this library. If not, see <https://www.gnu.org/licenses/>.\n\n"
        "The full license text is available at:\n"
        "  https://www.gnu.org/licenses/lgpl-3.0.txt\n"
    ),
    "MPL-2.0": (
        "Mozilla Public License Version 2.0\n"
        "==================================\n\n"
        "Copyright (c) {year} {holder}\n\n"
        "This Source Code Form is subject to the terms of the Mozilla Public\n"
        "License, v. 2.0. If a copy of the MPL was not distributed with this\n"
        "file, You can obtain one at https://mozilla.org/MPL/2.0/.\n\n"
        "The full license text is available at:\n"
        "  https://www.mozilla.org/media/MPL/2.0/index.txt\n"
    ),
    "MIT-0": (
        "MIT No Attribution\n\n"
        "Copyright (c) {year} {holder}\n\n"
        "Permission is hereby granted, free of charge, to any person obtaining a copy\n"
        'of this software and associated documentation files (the "Software"), to deal\n'
        "in the Software without restriction, including without limitation the rights\n"
        "to use, copy, modify, merge, publish, distribute, sublicense, and/or sell\n"
        "copies of the Software, and to permit persons to whom the Software is\n"
        "furnished to do so.\n\n"
        'THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR\n'
        "IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,\n"
        "FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE\n"
        "AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER\n"
        "LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,\n"
        "OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE\n"
        "SOFTWARE.\n"
    ),
    "Zlib": (
        "zlib License\n\n"
        "Copyright (c) {year} {holder}\n\n"
        "This software is provided 'as-is', without any express or implied warranty.\n"
        "In no event will the authors be held liable for any damages arising from the\n"
        "use of this software.\n\n"
        "Permission is granted to anyone to use this software for any purpose,\n"
        "including commercial applications, and to alter it and redistribute it freely,\n"
        "subject to the following restrictions:\n\n"
        "1. The origin of this software must not be misrepresented; you must not claim\n"
        "   that you wrote the original software. If you use this software in a product,\n"
        "   an acknowledgment in the product documentation would be appreciated but is\n"
        "   not required.\n\n"
        "2. Altered source versions must be plainly marked as such, and must not be\n"
        "   misrepresented as being the original software.\n\n"
        "3. This notice may not be removed or altered from any source distribution.\n"
    ),
    "AGPL-3.0-or-later": (
        "                    GNU AFFERO GENERAL PUBLIC LICENSE\n"
        "                       Version 3, 19 November 2007\n\n"
        " Copyright (C) {year} {holder}\n\n"
        " Everyone is permitted to copy and distribute verbatim copies\n"
        " of this license document, but changing it is not allowed.\n\n"
        "This program is free software: you can redistribute it and/or modify\n"
        "it under the terms of the GNU Affero General Public License as published\n"
        "by the Free Software Foundation, either version 3 of the License, or\n"
        "(at your option) any later version.\n\n"
        "This program is distributed in the hope that it will be useful,\n"
        "but WITHOUT ANY WARRANTY; without even the implied warranty of\n"
        "MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the\n"
        "GNU Affero General Public License for more details.\n\n"
        "You should have received a copy of the GNU Affero General Public License\n"
        "along with this program. If not, see <https://www.gnu.org/licenses/>.\n\n"
        "If you provide network access to this program's interface, you must offer\n"
        "users the source code corresponding to the modified version they're\n"
        "interacting with — see Section 13 of the AGPL for details.\n\n"
        "The full license text is available at:\n"
        "  https://www.gnu.org/licenses/agpl-3.0.txt\n"
    ),
    "EUPL-1.2": (
        "                       EUROPEAN UNION PUBLIC LICENCE v. 1.2\n"
        "                          EUPL © the European Union {year}\n\n"
        "Copyright (c) {year} {holder}\n\n"
        "Licensed under the EUPL, Version 1.2 or — as soon as approved by the\n"
        'European Commission — subsequent versions of the EUPL (the "Licence").\n\n'
        "You may not use this work except in compliance with the Licence.\n"
        "You may obtain a copy of the Licence at:\n\n"
        "    https://joinup.ec.europa.eu/software/page/eupl\n\n"
        "Unless required by applicable law or agreed to in writing, software\n"
        'distributed under the Licence is distributed on an "AS IS" BASIS,\n'
        "WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.\n"
        "See the Licence for the specific language governing permissions and\n"
        "limitations under the Licence.\n\n"
        "The full licence text is available at the URL above.\n"
    ),
    "CC-BY-4.0": (
        "Creative Commons Attribution 4.0 International (CC BY 4.0)\n\n"
        "Copyright (c) {year} {holder}\n\n"
        "This work is licensed under the Creative Commons Attribution 4.0\n"
        "International License. You are free to share and adapt the material for\n"
        "any purpose, even commercially, under the following terms:\n\n"
        "  - Attribution — You must give appropriate credit, provide a link to\n"
        "    the license, and indicate if changes were made.\n\n"
        "No additional restrictions — You may not apply legal terms or\n"
        "technological measures that legally restrict others from doing anything\n"
        "the license permits.\n\n"
        "The full licence text is available at:\n"
        "  https://creativecommons.org/licenses/by/4.0/legalcode\n"
    ),
    "BSL-1.1": (
        "Business Source License 1.1\n\n"
        "Licensor:             {holder}\n"
        "Licensed Work:        The Work delivered by Licensor under this Licence.\n"
        "Additional Use Grant: _TODO — describe any additional production use granted._\n"
        "Change Date:          _TODO — four years after the first public release._\n"
        "Change License:       Apache-2.0\n\n"
        "Copyright (c) {year} {holder}\n\n"
        "The Business Source License (this 'Licence') is not an Open Source\n"
        "license. However, the Licensed Work will become open-source software\n"
        "under the Change License identified above on the Change Date.\n\n"
        "Until the Change Date, the Licensee may copy, modify, distribute, and\n"
        "use the Licensed Work for any purpose other than a Competing Use, as\n"
        "described in the Additional Use Grant.\n\n"
        "The full Licence text is available at:\n"
        "  https://mariadb.com/bsl11/\n\n"
        "For the avoidance of doubt: Section 4 of the Licence is the binding\n"
        "definition of 'Competing Use'.\n"
    ),
    "OFL-1.1": (
        "SIL Open Font License, Version 1.1\n\n"
        "Copyright (c) {year} {holder}\n\n"
        "This Font Software is licensed under the SIL Open Font License, Version\n"
        "1.1. This license is copied below, and is also available with a FAQ at:\n"
        "  https://openfontlicense.org\n\n"
        "PREAMBLE\n\n"
        "The goals of the Open Font License (OFL) are to stimulate worldwide\n"
        "development of collaborative font projects, to support the font creation\n"
        "efforts of academic and linguistic communities, and to provide a free\n"
        "and open framework in which fonts may be shared and improved in\n"
        "partnership with others.\n\n"
        "PERMISSION & CONDITIONS\n\n"
        "Permission is hereby granted, free of charge, to any person obtaining a\n"
        "copy of the Font Software, to use, study, copy, merge, embed, modify,\n"
        "redistribute, and sell modified and unmodified copies of the Font\n"
        "Software, subject to the conditions stated in the full Licence text.\n\n"
        "The Font Software may not be sold by itself. Modified versions may not\n"
        "use the Reserved Font Name(s) (if any) without written permission.\n\n"
        "The full Licence text is available at the URL above.\n"
    ),
    "Artistic-2.0": (
        "The Artistic License 2.0\n\n"
        "Copyright (c) {year} {holder}\n\n"
        "Everyone is permitted to copy and distribute verbatim copies of this\n"
        "license document, but changing it is not allowed.\n\n"
        "PREAMBLE\n\n"
        "This license establishes the terms under which a given free software\n"
        "Package may be copied, modified, distributed, and/or redistributed. The\n"
        "intent is that the Copyright Holder maintains some artistic control over\n"
        "the development of that Package while still keeping the Package\n"
        "available as open source and free software.\n\n"
        "You are always permitted to make arrangements wholly outside of this\n"
        "license directly with the Copyright Holder of a given Package.\n\n"
        "The full Licence text is available at:\n"
        "  https://www.perlfoundation.org/artistic-license-20.html\n"
    ),
}


# --- helper ----------------------------------------------------------------


def parse_var_assignments(assignments: Iterable[str]) -> dict[str, str]:
    """Parse `KEY=VALUE` strings (e.g., from CLI `--var` flags) into a dict."""
    out: dict[str, str] = {}
    for raw in assignments:
        if "=" not in raw:
            raise ValueError(f"--var entry must be KEY=VALUE, got {raw!r}")
        key, _, value = raw.partition("=")
        out[key.strip()] = value
    return out


__all__ = [
    "AfterAction",
    "FileOp",
    "FileTemplate",
    "ScaffoldTemplate",
    "VariableSpec",
    "apply",
    "list_bundled_scaffolds",
    "load_bundled_scaffold",
    "parse_var_assignments",
    "render_text",
    "resolve_variables",
    "run_after_actions",
]
