"""Load + merge gendia config files.

Order of precedence (later wins for overlapping keys, but accounts and
registries are *unioned* rather than replaced):
  1. Machine-wide:  `$XDG_CONFIG_HOME/gendia/gendia.json`
  2. Project-local: `./gendia.json` or `./.gendia.json`
  3. Environment: `GENDIA_*` overrides

Project-local files MUST contain a `project` block. Machine-wide files
typically contain `accounts` + `registries` + `defaults` only, but may include
a `project` block for one-off setups.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from gendia.config.schema import (
    Account,
    ConfigError,
    Defaults,
    GendiaConfig,
    Project,
    Registry,
)
from gendia.util.paths import config_home, project_config_paths


def _read_json(path: Path) -> dict[str, Any]:
    try:
        loaded: Any = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path}: invalid JSON: {exc}") from exc

    if not isinstance(loaded, dict):
        raise ConfigError(f"{path}: top-level must be a JSON object, got {type(loaded).__name__}")
    return loaded


def _merge_configs(machine: dict[str, Any], project: dict[str, Any]) -> dict[str, Any]:
    """Merge machine-wide and project-local config.

    The two dict-of-dicts blocks (`accounts`, `registries`) are *unioned*:
    a project file can add new entries, override individual ones, but does
    NOT wipe out the entire machine-wide block by declaring its own. Other
    top-level keys follow simple last-wins semantics.
    """
    out: dict[str, Any] = dict(machine)

    for key, value in project.items():
        if key in {"accounts", "registries"} and isinstance(value, dict):
            base = dict(out.get(key) or {})
            base.update(value)
            out[key] = base
        else:
            out[key] = value

    return out


def load_config(
    *,
    project_path: Path | None = None,
    machine_path: Path | None = None,
    cwd: Path | None = None,
) -> GendiaConfig:
    """Load and validate config.

    Explicit `project_path` and `machine_path` override the search heuristics;
    pass them in tests for reproducibility.
    """
    cwd = cwd or Path.cwd()
    machine_path = machine_path or (config_home() / "gendia.json")
    machine_raw = _read_json(machine_path)

    if project_path is not None:
        project_raw = _read_json(project_path)
        project_root_default = project_path.parent
    else:
        project_raw = {}
        project_root_default = cwd
        for candidate in project_config_paths(cwd):
            if candidate == machine_path:
                continue
            if candidate.is_file():
                project_raw = _read_json(candidate)
                project_root_default = candidate.parent
                break

    merged = _merge_configs(machine_raw, project_raw)

    # --- accounts ---
    accounts_raw = merged.get("accounts") or {}
    accounts = {name: Account.from_dict(name, payload) for name, payload in accounts_raw.items()}

    # --- registries ---
    registries_raw = merged.get("registries") or {}
    registries = {
        name: Registry.from_dict(name, payload) for name, payload in registries_raw.items()
    }

    # --- defaults ---
    defaults_raw = merged.get("defaults") or {}
    defaults = Defaults(
        concurrency=int(defaults_raw.get("concurrency", 4)),
        dry_run=bool(defaults_raw.get("dry_run", False)),
        log_level=str(defaults_raw.get("log_level", "info")),
        log_format=str(defaults_raw.get("log_format", "human")),
    )
    # Environment overrides
    if env_level := os.environ.get("GENDIA_LOG_LEVEL"):
        defaults = Defaults(
            concurrency=defaults.concurrency,
            dry_run=defaults.dry_run,
            log_level=env_level,
            log_format=defaults.log_format,
        )

    # --- project ---
    project: Project | None = None
    project_payload = merged.get("project") or (
        # Allow flat layout: top-level keys describe the project directly.
        merged if "name" in merged and "repos" in merged else None
    )
    if project_payload is not None:
        if "root" not in project_payload:
            project_payload = {**project_payload, "root": str(project_root_default)}
        project = Project.from_dict(project_payload)
        # Validate that the named account/registry exist.
        if project.account not in accounts:
            raise ConfigError(
                f"project {project.name!r} references unknown account {project.account!r}"
            )
        if project.registry and project.registry not in registries:
            raise ConfigError(
                f"project {project.name!r} references unknown registry {project.registry!r}"
            )

    return GendiaConfig(
        accounts=accounts,
        registries=registries,
        project=project,
        defaults=defaults,
    )
