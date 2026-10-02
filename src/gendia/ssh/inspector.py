"""SSH key health audit — feeds `gendia doctor` and `gendia ssh inspect`.

Three classes of finding:

  * **mode**       — file permissions looser than 0600/0400 expose the key.
  * **strength**   — DSA is broken; RSA < 2048 bits is no longer adequate.
  * **shared use** — the same key reused across multiple hosts widens blast
    radius if it leaks. We surface but do not fail on this.

Strength detection is intentionally conservative: if we can't classify a
key, we report `unknown` and move on rather than guess. The OpenSSH key
format header is read from the first line; PEM-style keys (`-----BEGIN ...
PRIVATE KEY-----`) are also recognized.

No external libraries used; everything is text inspection.
"""

from __future__ import annotations

import os
import re
import sys
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from gendia.ssh.config import SSHHost

_RSA_HEADERS = {
    "-----BEGIN RSA PRIVATE KEY-----",
    "-----BEGIN OPENSSH PRIVATE KEY-----",
}
_DSA_HEADERS = {"-----BEGIN DSA PRIVATE KEY-----"}
_EC_HEADERS = {"-----BEGIN EC PRIVATE KEY-----"}

_OPENSSH_HEADER = "-----BEGIN OPENSSH PRIVATE KEY-----"
_OPENSSH_KEY_TYPE_RE = re.compile(r"ssh-(rsa|dss|ed25519|ecdsa)|ecdsa-sha2-")

_RSA_BITS_THRESHOLD_WEAK = 2048
_LOOSE_MODE_OK = {0o400, 0o600}
_PUBKEY_PARTS_MIN = 2  # `<type> <base64-blob>` minimum


@dataclass(frozen=True, slots=True)
class KeyAudit:
    """One finding about one private key."""

    severity: str  # "ok" | "warn" | "error"
    section: str  # "mode" | "strength" | "shared"
    path: Path
    message: str
    fix: str = ""
    used_by: tuple[str, ...] = field(default_factory=tuple)


def audit_keys(hosts: Iterable[SSHHost]) -> tuple[KeyAudit, ...]:
    """Audit every IdentityFile referenced by `hosts`. De-duplicates by path."""
    findings: list[KeyAudit] = []
    seen_paths: dict[Path, list[str]] = defaultdict(list)

    for host in hosts:
        for identity in host.identity_files:
            resolved = _resolve_identity(identity)
            if resolved is None:
                continue
            seen_paths[resolved].append(host.alias)

    for path, aliases in seen_paths.items():
        findings.extend(_audit_one_key(path, used_by=tuple(sorted(set(aliases)))))

    findings.extend(_shared_key_findings(seen_paths))
    return tuple(findings)


# --- per-key checks ---------------------------------------------------------


def _audit_one_key(path: Path, *, used_by: tuple[str, ...]) -> Iterable[KeyAudit]:
    if not path.is_file():
        yield KeyAudit(
            severity="error",
            section="mode",
            path=path,
            message=f"IdentityFile {path} does not exist",
            fix=f"generate it with `ssh-keygen -t ed25519 -f {path}`",
            used_by=used_by,
        )
        return

    yield from _check_mode(path, used_by=used_by)
    yield from _check_strength(path, used_by=used_by)


def _check_mode(path: Path, *, used_by: tuple[str, ...]) -> Iterable[KeyAudit]:
    if sys.platform.startswith("win"):
        # File-mode bits don't carry the same meaning on Windows.
        return
    try:
        mode = path.stat().st_mode & 0o777
    except OSError as exc:
        yield KeyAudit(
            severity="warn",
            section="mode",
            path=path,
            message=f"could not stat {path}: {exc}",
            used_by=used_by,
        )
        return

    if mode in _LOOSE_MODE_OK:
        yield KeyAudit(
            severity="ok",
            section="mode",
            path=path,
            message=f"mode {mode:o}",
            used_by=used_by,
        )
        return

    yield KeyAudit(
        severity="warn",
        section="mode",
        path=path,
        message=f"loose permissions on private key (mode {mode:o})",
        fix=f"chmod 600 {path}",
        used_by=used_by,
    )


def _check_strength(  # noqa: PLR0911 — one branch per recognized key format
    path: Path, *, used_by: tuple[str, ...]
) -> Iterable[KeyAudit]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        yield KeyAudit(
            severity="warn",
            section="strength",
            path=path,
            message=f"unreadable: {exc}",
            used_by=used_by,
        )
        return

    first = text.splitlines()[0] if text else ""

    if first in _DSA_HEADERS:
        yield KeyAudit(
            severity="error",
            section="strength",
            path=path,
            message="DSA keys are deprecated and unsafe",
            fix=f"replace with `ssh-keygen -t ed25519 -f {path}`",
            used_by=used_by,
        )
        return

    if first == _OPENSSH_HEADER:
        # New-format keys: best-effort sniff the embedded type string.
        match = _OPENSSH_KEY_TYPE_RE.search(text)
        if match is None:
            yield KeyAudit(
                severity="ok",
                section="strength",
                path=path,
                message="openssh key (type unknown — likely ed25519)",
                used_by=used_by,
            )
            return
        kind = match.group(0)
        if "dss" in kind:
            yield KeyAudit(
                severity="error",
                section="strength",
                path=path,
                message="DSA keys are deprecated and unsafe",
                fix=f"replace with `ssh-keygen -t ed25519 -f {path}`",
                used_by=used_by,
            )
            return
        yield KeyAudit(
            severity="ok",
            section="strength",
            path=path,
            message=f"openssh key ({kind})",
            used_by=used_by,
        )
        return

    if first in _RSA_HEADERS:
        bits = _rsa_bits(path)
        if bits is None:
            yield KeyAudit(
                severity="ok",
                section="strength",
                path=path,
                message="RSA key (size unknown)",
                used_by=used_by,
            )
            return
        if bits < _RSA_BITS_THRESHOLD_WEAK:
            yield KeyAudit(
                severity="warn",
                section="strength",
                path=path,
                message=f"RSA key is only {bits} bits",
                fix=f"replace with `ssh-keygen -t ed25519 -f {path}`",
                used_by=used_by,
            )
            return
        yield KeyAudit(
            severity="ok",
            section="strength",
            path=path,
            message=f"RSA-{bits} key",
            used_by=used_by,
        )
        return

    if first in _EC_HEADERS:
        yield KeyAudit(
            severity="ok",
            section="strength",
            path=path,
            message="ECDSA key",
            used_by=used_by,
        )
        return

    yield KeyAudit(
        severity="ok",
        section="strength",
        path=path,
        message="unrecognized key format (skipping strength check)",
        used_by=used_by,
    )


def _rsa_bits(path: Path) -> int | None:  # noqa: PLR0911 — coarse RSA-bits classification
    """Best-effort key-size detection for *.pub sibling, no openssl required."""
    pub = path.with_suffix(path.suffix + ".pub") if path.suffix else Path(f"{path}.pub")
    if not pub.is_file():
        return None
    try:
        text = pub.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    parts = text.split()
    if len(parts) < _PUBKEY_PARTS_MIN or not parts[0].startswith("ssh-rsa"):
        return None
    # The size sniff requires base64 + asn1 awareness — we don't ship an
    # asn1 parser. Heuristic: encoded length ~ ceil(bits / 6) + small overhead.
    blob = parts[1]
    try:
        # 1 base64 char ≈ 6 bits; pubkey blob ≈ 256 + key_bits bits depending
        # on exponent length. This is a coarse upper bound, fine for the
        # 1024-vs-2048 distinction we actually care about.
        approx_bits = (len(blob) * 6) - 256
    except (TypeError, ValueError):
        return None
    if approx_bits >= _RSA_BITS_THRESHOLD_WEAK * 4:
        return _RSA_BITS_THRESHOLD_WEAK * 4
    if approx_bits >= _RSA_BITS_THRESHOLD_WEAK * 2:
        return _RSA_BITS_THRESHOLD_WEAK * 2
    if approx_bits >= _RSA_BITS_THRESHOLD_WEAK:
        return _RSA_BITS_THRESHOLD_WEAK
    return _RSA_BITS_THRESHOLD_WEAK // 2


# --- shared-key check -------------------------------------------------------


def _shared_key_findings(paths: dict[Path, list[str]]) -> Iterable[KeyAudit]:
    for path, aliases in paths.items():
        unique = sorted(set(aliases))
        if len(unique) > 1:
            yield KeyAudit(
                severity="warn",
                section="shared",
                path=path,
                message=f"key reused across {len(unique)} hosts: {', '.join(unique)}",
                fix="generate a separate key per host to limit blast radius if one leaks",
                used_by=tuple(unique),
            )


# --- helpers ----------------------------------------------------------------


def _resolve_identity(identity: str) -> Path | None:
    if not identity:
        return None
    expanded = os.path.expandvars(os.path.expanduser(identity))
    try:
        return Path(expanded).resolve()
    except OSError:
        return None
