"""Synthesize gendia accounts from detected SSH config entries.

`gendia setup --from-ssh` uses this. So does `gendia ssh bootstrap`. Given
an SSH config, we walk every Host that the forge registry recognizes and
produce an `AccountStub` per detected forge — a JSON-serializable
proposal the user can review before it lands in their `gendia.json`.

We intentionally do not write `gendia.json` from inside this module; the
caller decides whether to print, prompt, merge, or write.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from gendia.ssh.config import SSH_DEFAULT_PORT, SSHConfig, SSHHost
from gendia.ssh.forges import ForgeRegistry, load_registry


@dataclass(frozen=True, slots=True)
class AccountStub:
    """A proposed gendia account synthesized from an SSH host."""

    name: str  # the label, e.g. "personal" / "myorg"
    platform: str  # gendia provider platform, e.g. "github" / "gitea"
    registry_id: str  # registry-side id, e.g. "github-enterprise"
    host: str | None  # SSH alias / hostname
    credential_ref: str  # e.g. "GIT_TOKEN_PERSONAL"
    ssh_key_path: str | None
    git_auth: str = "ssh"
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_account_payload(self) -> dict[str, Any]:
        """Render as the dict shape `Account.from_dict` accepts."""
        payload: dict[str, Any] = {
            "platform": self.platform,
            "credential_ref": self.credential_ref,
            "git_auth": self.git_auth,
        }
        # Pick the right scope key for the platform.
        scope_key = _scope_key_for(self.platform)
        payload[scope_key] = self.name
        if self.host:
            payload["host"] = self.host
        if self.ssh_key_path:
            payload["ssh_key_path"] = self.ssh_key_path
        return payload

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def propose_accounts(
    *,
    config: SSHConfig | None = None,
    registry: ForgeRegistry | None = None,
    forges_file: Path | None = None,
) -> tuple[AccountStub, ...]:
    """Inspect SSH config + forge registry and return proposed account stubs.

    Skips wildcards (`Host *`) and any host the registry can't classify.
    """
    cfg = config or SSHConfig.load()
    reg = registry or load_registry(forges_file)

    stubs: list[AccountStub] = []
    seen_labels: set[str] = set()
    for host in cfg.hosts().exclude_wildcards():
        registry_id = reg.detect(
            hostname=host.hostname,
            alias=host.alias,
            comment=host.comment,
            user=host.user or "",
        )
        if registry_id is None:
            continue
        provider_platform = reg.provider_platform(registry_id)
        if provider_platform is None:
            # Recognized but not implemented as a gendia provider; skip with note.
            continue

        label = reg.derive_label(
            hostname=host.hostname,
            identity_file=host.primary_identity_file,
        )
        if label in seen_labels:
            label = f"{label}-{host.alias}".replace(".", "-")
        seen_labels.add(label)

        notes = []
        if registry_id != provider_platform:
            notes.append(f"registry id {registry_id!r} maps to provider {provider_platform!r}")
        if host.user and host.user != "git":
            notes.append(f"SSH User={host.user}")
        if host.port and host.port != SSH_DEFAULT_PORT:
            notes.append(f"SSH Port={host.port}")

        stubs.append(
            AccountStub(
                name=label,
                platform=provider_platform,
                registry_id=registry_id,
                host=_maybe_host(provider_platform, host),
                credential_ref=f"GIT_TOKEN_{label.upper()}",
                ssh_key_path=host.primary_identity_file,
                git_auth="ssh" if host.identity_files else "auto",
                notes=tuple(notes),
            )
        )

    return tuple(stubs)


def render_proposed_config(stubs: Iterable[AccountStub]) -> str:
    """Render the stubs as a `gendia.json`-shaped JSON document."""
    payload = {
        "accounts": {stub.name: stub.to_account_payload() for stub in stubs},
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


# --- helpers ----------------------------------------------------------------


def _scope_key_for(platform: str) -> str:
    if platform == "bitbucket":
        return "workspace"
    if platform == "gitlab":
        return "group"
    if platform == "azure":
        return "org"
    return "org"


def _maybe_host(platform: str, host: SSHHost) -> str | None:
    """Whether to set `Account.host` depends on the platform.

    GitHub.com / GitLab.com / Bitbucket.org don't need it. Self-hosted does.
    """
    public_hostnames = {"github.com", "gitlab.com", "bitbucket.org"}
    if host.hostname in public_hostnames:
        return None
    return host.hostname
