"""SSH-config awareness layer.

Three responsibilities:
  - Parse `~/.ssh/config` (Host / Match / Include) into typed `SSHHost` records.
  - Detect which forge a host belongs to, driven by a JSON registry the user
    can override per project.
  - Audit key permissions and strength (used by `gendia doctor`).
  - Synthesize gendia accounts from detected SSH hosts (used by
    `gendia setup --from-ssh` and `gendia ssh bootstrap`).
"""

from gendia.ssh.bootstrap import AccountStub, propose_accounts
from gendia.ssh.config import SSHConfig, SSHHost, SSHHostList, parse_ssh_config
from gendia.ssh.forges import ForgeRegistry, builtin_registry, load_registry
from gendia.ssh.inspector import KeyAudit, audit_keys

__all__ = [
    "AccountStub",
    "ForgeRegistry",
    "KeyAudit",
    "SSHConfig",
    "SSHHost",
    "SSHHostList",
    "audit_keys",
    "builtin_registry",
    "load_registry",
    "parse_ssh_config",
    "propose_accounts",
]
