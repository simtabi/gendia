"""`gendia config` — manage the `.env` file from the CLI.

Subcommands:
    list                 # all keys, masked previews
    get <KEY>            # one value (masked unless --raw)
    set <KEY> [<VALUE>]  # if VALUE is omitted, prompt with getpass (no echo)
    unset <KEY>          # remove key (preserves comments + order)
    edit                 # open in $EDITOR
    path                 # print which env file is in use
    doctor               # validate (mode 0600, no duplicates, etc.)

Every `set` triggers a `.bak` next to the target file before the write,
so a typo can be reverted with `mv .env.bak .env`.
"""

from __future__ import annotations

import argparse
import getpass
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from gendia.auth.env_file import EnvFile, EnvFileError, secure_create_dir
from gendia.observability.logger import get_logger
from gendia.util.paths import config_home, env_file_path

_VerbHandler = Callable[[], int]

_log = get_logger("cli.config")

_LOOSE_MODE_THRESHOLD = 0o640
_GROUP_READABLE_MODE = 0o640
_MASK_MIN_LEN = 8


def add_subparser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register `gendia config` and its verbs."""
    p = subparsers.add_parser(
        "config",
        help="Manage gendia's `.env` file (list, get, set, unset, edit, path, doctor).",
    )
    p.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="Override the env-file path (default: $GENDIA_ENV_FILE or ~/.config/gendia/.env)",
    )

    sub = p.add_subparsers(dest="config_verb", required=True)

    sub.add_parser("list", help="Print all keys with masked previews.")
    sub.add_parser("path", help="Print the active env-file path.")
    sub.add_parser("doctor", help="Validate the env file (mode, duplicates, parsability).")

    p_get = sub.add_parser("get", help="Print one value (masked unless --raw).")
    p_get.add_argument("key")
    p_get.add_argument("--raw", action="store_true", help="Print the raw value, no masking.")

    p_set = sub.add_parser("set", help="Set a key. Prompts securely when value omitted.")
    p_set.add_argument("key")
    p_set.add_argument("value", nargs="?", default=None)
    p_set.add_argument(
        "--from-file",
        type=Path,
        default=None,
        help="Read the value from the given file (trim trailing newline).",
    )

    p_unset = sub.add_parser("unset", help="Remove a key.")
    p_unset.add_argument("key")

    p_edit = sub.add_parser("edit", help="Open the env file in $EDITOR.")
    p_edit.add_argument("--editor", default=None, help="Override $EDITOR for this run.")


def dispatch(args: argparse.Namespace) -> int:
    target = args.env_file or env_file_path()
    handlers: dict[str, _VerbHandler] = {
        "path": lambda: _cmd_path(target),
        "list": lambda: _cmd_list(target),
        "doctor": lambda: _cmd_doctor(target),
        "get": lambda: _cmd_get(target, args.key, raw=args.raw),
        "set": lambda: _cmd_set(target, args.key, value=args.value, from_file=args.from_file),
        "unset": lambda: _cmd_unset(target, args.key),
        "edit": lambda: _cmd_edit(target, editor_override=args.editor),
    }
    handler = handlers.get(args.config_verb)
    if handler is None:
        raise ValueError(f"unknown config verb: {args.config_verb}")
    return handler()


# --- per-verb implementations -----------------------------------------------


def _cmd_path(target: Path) -> int:
    print(target)
    return 0


def _cmd_list(target: Path) -> int:
    env = EnvFile.load(target)
    keys = env.keys()
    if not keys:
        print(f"(empty: {target})")
        return 0
    width = max(len(k) for k in keys)
    for key in keys:
        value = env.get(key) or ""
        print(f"  {key:<{width}}  {_mask(value)}")
    return 0


def _cmd_doctor(target: Path) -> int:
    findings: list[str] = []

    if not target.is_file():
        print(f"  ✗ no env file at {target}", file=sys.stderr)
        return 1

    mode = target.stat().st_mode & 0o777
    if mode > _LOOSE_MODE_THRESHOLD:
        findings.append(f"loose permissions ({mode:o}); run `chmod 600 {target}`")
    elif mode == _GROUP_READABLE_MODE:
        findings.append("mode 640 OK for groups, but 600 is tighter")

    env = EnvFile.load(target)
    seen: dict[str, int] = {}
    for raw in target.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip().removeprefix("export ").strip()
        seen[key] = seen.get(key, 0) + 1
    for key, count in seen.items():
        if count > 1:
            findings.append(f"duplicate key {key!r} appears {count} times (last one wins)")

    keys = env.keys()
    print(f"env file: {target}")
    print(f"  keys: {len(keys)}")
    print(f"  mode: {mode:o}")
    if findings:
        print("findings:")
        for f in findings:
            print(f"  ⚠ {f}")
        return 1
    print("  ✓ healthy")
    return 0


def _cmd_get(target: Path, key: str, *, raw: bool) -> int:
    env = EnvFile.load(target)
    value = env.get(key)
    if value is None:
        print(f"(unset: {key})", file=sys.stderr)
        return 1
    print(value if raw else _mask(value))
    return 0


def _cmd_set(target: Path, key: str, *, value: str | None, from_file: Path | None) -> int:
    if value is not None and from_file is not None:
        print("error: pass either VALUE or --from-file, not both", file=sys.stderr)
        return 64

    if from_file is not None:
        try:
            value = from_file.read_text(encoding="utf-8").rstrip("\n")
        except OSError as exc:
            print(f"error reading {from_file}: {exc}", file=sys.stderr)
            return 1
    elif value is None:
        if not sys.stdin.isatty():
            print("error: VALUE not given and stdin is not a tty for prompting", file=sys.stderr)
            return 64
        value = getpass.getpass(f"{key}: ")
        if value == "":
            print("error: empty value; aborting", file=sys.stderr)
            return 1

    secure_create_dir(target.parent)
    secure_create_dir(config_home())
    env = EnvFile.load(target)
    env.backup()
    env.set(key, value)
    try:
        env.save()
    except EnvFileError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"set {key}  ({_mask(value)})")
    return 0


def _cmd_unset(target: Path, key: str) -> int:
    env = EnvFile.load(target)
    if key not in env:
        print(f"(already unset: {key})")
        return 0
    env.backup()
    env.unset(key)
    try:
        env.save()
    except EnvFileError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"unset {key}")
    return 0


def _cmd_edit(target: Path, *, editor_override: str | None) -> int:
    editor = editor_override or os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
    secure_create_dir(target.parent)
    if not target.is_file():
        target.touch(mode=0o600)

    backup = EnvFile.load(target).backup()
    try:
        subprocess.run([editor, str(target)], check=True)
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"error: editor {editor!r} failed: {exc}", file=sys.stderr)
        return 1

    # Re-tighten mode if the editor created a new file with looser perms.
    target.chmod(0o600)
    if backup is not None:
        print(f"backup: {backup}")
    return 0


# --- helpers -----------------------------------------------------------------


def _mask(value: str) -> str:
    """Mask a secret-looking value: first 4 + last 2 chars, with length tag."""
    if len(value) <= _MASK_MIN_LEN:
        return f"<set, len={len(value)}>"
    return f"{value[:4]}...{value[-2:]} (len={len(value)})"
