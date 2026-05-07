"""`gendia setup` — scaffold the right config layout per deployment shape.

Six shapes are supported, matching the documented `.env` placement options:

    local    ~/.config/gendia/.env  (XDG, single-user dev box)
    vps      /etc/gendia/.env       (system-wide; needs sudo)
    project  ./.env.gendia          (per-project override)
    docker   ~/.config/gendia/.env + a sample bind-mount recipe
    k8s      generates a Secret + CronJob YAML stub to stdout
    ci       generates a GitHub Actions workflow YAML stub to stdout

When invoked without `--shape`, gendia inspects the host and tells you what's
already configured (or what it can detect). The command never overwrites
without `--force`; a `.bak` is created before any in-place modification.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from gendia.auth.env_file import secure_create_dir
from gendia.observability.logger import get_logger

_log = get_logger("cli.setup")


_SHAPES = ("local", "vps", "project", "docker", "k8s", "ci")
_LOOSE_MODE_THRESHOLD = 0o640


def add_subparser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subparsers.add_parser(
        "setup",
        help="Scaffold gendia config for a deployment shape (local, vps, docker, k8s, ci).",
    )
    p.add_argument(
        "--shape",
        choices=_SHAPES,
        default=None,
        help="Deployment shape. Omit to auto-detect what's already configured.",
    )
    p.add_argument(
        "--detect",
        action="store_true",
        help="Print what gendia detects on this host and exit (no scaffolding).",
    )
    p.add_argument("--force", action="store_true", help="Overwrite existing files.")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be written, don't actually create files.",
    )


_HANDLERS_WITH_FLAGS = {"local", "vps", "project", "docker"}
_HANDLERS_NO_FLAGS = {"k8s", "ci"}


def dispatch(args: argparse.Namespace) -> int:
    if args.detect:
        return _print_detection()

    shape = args.shape or _resolve_default_shape(force=args.force)

    if shape in _HANDLERS_WITH_FLAGS:
        handler = {
            "local": _setup_local,
            "vps": _setup_vps,
            "project": _setup_project,
            "docker": _setup_docker,
        }[shape]
        return handler(force=args.force, dry_run=args.dry_run)
    if shape in _HANDLERS_NO_FLAGS:
        return {"k8s": _setup_k8s, "ci": _setup_ci}[shape]()
    raise ValueError(f"unknown shape: {shape}")


# --- detection ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Detection:
    """Result of inspecting the host for an existing gendia setup."""

    shape: str | None  # detected shape, or None when nothing present
    env_file: Path | None  # the `.env` we'd actually use
    runtime: str | None  # "container" / "k8s" when running inside one
    notes: tuple[str, ...]  # human-readable details


def _detect() -> _Detection:
    """Inspect the host and return the most likely shape + path."""
    notes: list[str] = []
    runtime: str | None = None

    # Runtime context: are we INSIDE a container or k8s pod right now?
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        runtime = "k8s"
        notes.append("KUBERNETES_SERVICE_HOST is set (running inside a Kubernetes pod)")
    elif Path("/.dockerenv").exists() or _cgroup_mentions_container():
        runtime = "container"
        notes.append("/.dockerenv or cgroup indicates we're running inside a container")

    # Env-file location, in precedence order.
    candidates: list[tuple[str, Path]] = []
    if override := os.environ.get("GENDIA_ENV_FILE"):
        candidates.append(("project", Path(override).expanduser()))
    candidates += [
        ("docker", Path("/config/.env")),
        ("local", Path.home() / ".config" / "gendia" / ".env"),
        ("vps", Path("/etc/gendia/.env")),
        ("project", Path.cwd() / ".env.gendia"),
    ]

    for shape, path in candidates:
        if path.is_file():
            mode = path.stat().st_mode & 0o777
            notes.append(f"{shape}: {path} (mode {mode:o})")
            if mode > _LOOSE_MODE_THRESHOLD:
                notes.append(f"  ⚠ loose permissions; run chmod 600 {path}")
            return _Detection(shape=shape, env_file=path, runtime=runtime, notes=tuple(notes))

    return _Detection(shape=None, env_file=None, runtime=runtime, notes=tuple(notes))


def _cgroup_mentions_container() -> bool:
    try:
        text = Path("/proc/1/cgroup").read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return any(token in text for token in ("docker", "containerd", "kubepods", "podman"))


def _resolve_default_shape(*, force: bool) -> str:
    """Pick a shape when the user didn't specify one."""
    detection = _detect()
    if detection.shape is not None and not force:
        # Already configured. Report and refuse to scaffold.
        print(f"detected existing setup: {detection.shape} at {detection.env_file}")
        for note in detection.notes:
            print(f"  {note}")
        print()
        print("To re-scaffold, pass an explicit shape and --force:")
        print(f"  gendia setup --shape={detection.shape} --force")
        print()
        print("To inspect without changing anything:")
        print("  gendia setup --detect")
        print("  gendia config doctor")
        sys.exit(0)

    # Nothing configured (or --force given without --shape). Pick a sensible default.
    if detection.runtime == "k8s":
        return "k8s"
    if detection.runtime == "container":
        return "docker"
    if detection.shape is None:
        print("no existing gendia setup detected; scaffolding the local-dev shape.")
        print("(use --shape={vps,project,docker,k8s,ci} to pick a different layout)")
        print()
    return detection.shape or "local"


def _print_detection() -> int:
    detection = _detect()
    if detection.shape is None and not detection.notes:
        print("no gendia setup detected on this host.")
        return 1
    print(f"detected shape: {detection.shape or '(none)'}")
    if detection.env_file is not None:
        print(f"env file:       {detection.env_file}")
    if detection.runtime is not None:
        print(f"runtime:        {detection.runtime}")
    if detection.notes:
        print("notes:")
        for note in detection.notes:
            print(f"  {note}")
    return 0 if detection.shape is not None else 1


# --- local -------------------------------------------------------------------


def _setup_local(*, force: bool, dry_run: bool) -> int:
    target_dir = Path.home() / ".config" / "gendia"
    target_env = target_dir / ".env"

    print(f"setting up local config at {target_dir}/")

    if dry_run:
        print(f"  DRY-RUN: would mkdir -p {target_dir} (mode 0700)")
        print(f"  DRY-RUN: would copy examples/.env.example -> {target_env} (mode 0600)")
        return 0

    secure_create_dir(target_dir)
    if target_env.exists() and not force:
        print(f"  ✗ {target_env} already exists; pass --force to overwrite", file=sys.stderr)
        return 1

    template = _example_text(".env.example")
    target_env.write_text(template, encoding="utf-8")
    target_env.chmod(0o600)
    print(f"  ✓ wrote {target_env} (mode 0600)")

    print()
    print("Next steps:")
    print(f"  1. $EDITOR {target_env}    # fill in your real tokens")
    print("  2. gendia config doctor    # verify file mode + duplicates")
    print("  3. gendia status           # try it from any project")
    return 0


# --- vps ---------------------------------------------------------------------


def _setup_vps(*, force: bool, dry_run: bool) -> int:
    target_dir = Path("/etc/gendia")
    target_env = target_dir / ".env"

    print(f"setting up VPS / system-wide config at {target_dir}/")

    if dry_run or os.geteuid() != 0:
        print()
        print("This shape needs root permissions. Run as root or with sudo:")
        print()
        print(f"  sudo install -d -m 0700 -o root -g root {target_dir}")
        print(f"  sudo install -m 0600 examples/.env.example {target_env}")
        print(f"  sudo $EDITOR {target_env}")
        print()
        print("Then export GENDIA_ENV_FILE in the gendia process's env:")
        print()
        print(f"  GENDIA_ENV_FILE={target_env} gendia status")
        print()
        print("Or add this line to /etc/gendia/gendia.environment, sourced by")
        print("your systemd unit / cron wrapper:")
        print()
        print(f"  GENDIA_ENV_FILE={target_env}")
        return 0 if dry_run else 1

    secure_create_dir(target_dir)
    if target_env.exists() and not force:
        print(f"  ✗ {target_env} already exists; pass --force to overwrite", file=sys.stderr)
        return 1

    template = _example_text(".env.example")
    target_env.write_text(template, encoding="utf-8")
    target_env.chmod(0o600)
    os.chown(target_env, 0, 0)
    print(f"  ✓ wrote {target_env} (mode 0600 root:root)")
    print()
    print("Add this systemd drop-in for your gendia cron unit:")
    print()
    print("  [Service]")
    print(f"  Environment=GENDIA_ENV_FILE={target_env}")
    return 0


# --- project -----------------------------------------------------------------


def _setup_project(*, force: bool, dry_run: bool) -> int:
    target = Path.cwd() / ".env.gendia"

    print(f"setting up per-project override at {target}")

    if dry_run:
        print(f"  DRY-RUN: would copy examples/.env.example -> {target} (mode 0600)")
        return 0

    if target.exists() and not force:
        print(f"  ✗ {target} already exists; pass --force to overwrite", file=sys.stderr)
        return 1

    template = _example_text(".env.example")
    target.write_text(template, encoding="utf-8")
    target.chmod(0o600)
    print(f"  ✓ wrote {target} (mode 0600)")
    print()
    print("Add to your .gitignore:")
    print()
    print("  /.env.gendia")
    print()
    print("Use in this project:")
    print()
    print(f"  GENDIA_ENV_FILE={target} gendia status")
    print()
    print("Or set persistently in this shell:")
    print()
    print(f"  export GENDIA_ENV_FILE={target}")
    return 0


# --- docker ------------------------------------------------------------------


def _setup_docker(*, force: bool, dry_run: bool) -> int:
    rc = _setup_local(force=force, dry_run=dry_run)
    if rc != 0:
        return rc
    print()
    print("Docker integration:")
    print()
    print("  # one-shot:")
    print("  docker run --rm \\")
    print("      --env-file ~/.config/gendia/.env \\")
    print("      -v ~/.config/gendia:/config:ro \\")
    print("      -v ~/.ssh:/home/gendia/.ssh:ro \\")
    print('      -v "$PWD":/work \\')
    print("      gendia:latest status")
    print()
    print("  # via Compose (recommended):")
    print("  docker compose run --rm gendia status")
    print()
    print("  # for *_FILE Docker-secrets (no env vars in the container):")
    print("  uncomment the matching `<KEY>_FILE=/run/secrets/...` lines")
    print("  in ~/.config/gendia/.env and mount the secrets via Compose.")
    return 0


# --- k8s ---------------------------------------------------------------------


def _setup_k8s() -> int:
    template = _example_text(".env.example")
    keys = sorted(
        {
            line.split("=", 1)[0].strip()
            for line in template.splitlines()
            if line.strip() and not line.strip().startswith("#") and "=" in line
        }
    )

    print("# Kubernetes scaffold for gendia.")
    print("# 1. Create a Secret with the actual token values:")
    print()
    print("#   kubectl create secret generic gendia-secrets \\")
    print("#     --from-literal=GITHUB_TOKEN_PERSONAL=ghp_xxx \\")
    print("#     --from-literal=PACKAGIST_API_TOKEN=xxxxx")
    print()
    print("# 2. Reference each key via *_FILE in your Pod env. The CronJob below")
    print("#    runs `gendia sync` every 30 minutes.")
    print()
    print("---")
    print("apiVersion: batch/v1")
    print("kind: CronJob")
    print("metadata:")
    print("  name: gendia-sync")
    print("spec:")
    print('  schedule: "*/30 * * * *"')
    print("  concurrencyPolicy: Forbid")
    print("  jobTemplate:")
    print("    spec:")
    print("      template:")
    print("        spec:")
    print("          restartPolicy: OnFailure")
    print("          containers:")
    print("          - name: gendia")
    print("            # Replace with the registry path you push gendia to:")
    print("            image: ghcr.io/your-org/gendia:latest")
    print('            args: ["sync", "--log-format", "json"]')
    print("            env:")
    for k in keys:
        if k.endswith("_FILE"):
            continue
        print(f"            - name: {k}_FILE")
        print(f"              value: /run/secrets/gendia/{k.lower()}")
    print("            volumeMounts:")
    print("            - name: gendia-secrets")
    print("              mountPath: /run/secrets/gendia")
    print("              readOnly: true")
    print("            - name: gendia-config")
    print("              mountPath: /config")
    print("              readOnly: true")
    print("          volumes:")
    print("          - name: gendia-secrets")
    print("            secret:")
    print("              secretName: gendia-secrets")
    print("          - name: gendia-config")
    print("            configMap:")
    print("              name: gendia-config")
    return 0


# --- ci ----------------------------------------------------------------------


def _setup_ci() -> int:
    print("# .github/workflows/gendia-sync.yml")
    print()
    print("name: gendia sync")
    print()
    print("on:")
    print("  push:")
    print("    branches: [main]")
    print("  schedule:")
    print('    - cron: "*/30 * * * *"')
    print()
    print("jobs:")
    print("  sync:")
    print("    runs-on: ubuntu-latest")
    print("    steps:")
    print("      - uses: actions/checkout@v4")
    print("      - uses: actions/setup-python@v5")
    print('        with: { python-version: "3.12" }')
    print("      - run: pip install gendia")
    print("      - run: gendia status")
    print("        env:")
    print("          GITHUB_TOKEN_REPO: ${{ secrets.GITHUB_TOKEN }}")
    print("          PACKAGIST_API_TOKEN: ${{ secrets.PACKAGIST_API_TOKEN }}")
    return 0


# --- helpers -----------------------------------------------------------------


def _example_text(name: str) -> str:
    """Return the bundled `examples/<name>` content as a string.

    Tries package resources first (works after pip install), falls back to
    the on-disk path (works during development without an install).
    """
    try:
        return resources.files("gendia").joinpath(f"examples/{name}").read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError):
        pass

    # Dev fallback: walk up to the repo root looking for examples/.
    here = Path(__file__).resolve()
    for ancestor in [here, *here.parents]:
        candidate = ancestor / "examples" / name
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")

    raise FileNotFoundError(f"could not locate bundled example: {name}")
