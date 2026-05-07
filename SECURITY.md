# Security Policy

## Reporting a vulnerability

Email **security@simtabi.com**. Do not open a public GitHub issue.

Acknowledgement within 48 hours. Patch SLA: 14 days for severity ≤ 3, 30 days for severity 4. Coordinated disclosure with a default 90-day embargo from triage.

## Secrets handling

`gendia` is a tool that wields credentials; getting this right matters.

- **No secrets ever appear in JSON config files.** Only `credential_ref` strings name the env-var / keyring entry that holds the secret.
- **No tokens in argv.** Subprocess auth uses `GIT_ASKPASS` (HTTPS) or `GIT_SSH_COMMAND` (SSH); tokens never land in process arguments visible to `ps`.
- **No tokens in URLs.** HTTPS git auth uses `GIT_ASKPASS` rather than `https://x:token@host/...` URL embedding (which would leak into reflogs).
- **No SSH agent enumeration.** `IdentitiesOnly=yes` is set when an explicit `ssh_key_path` is configured, so a misconfigured agent can't shop other identities at the remote.
- **System credential helper is neutralised** when `gendia` runs HTTPS git auth, so a stale `~/.git-credentials` cannot shadow the request token.
- **Temp ASKPASS scripts** live in `tempfile.mkdtemp()` directories and are cleaned up via context-manager exit; they exist only for the duration of one operation.
- **Docker secrets convention**: any env var `<KEY>_FILE` resolves to the file content at that path, supporting `/run/secrets/...` mounts.

## Scope

In scope:

- The `gendia` Python package and CLI.
- Default configuration (`Dockerfile`, `bin/gendia`, examples).

Out of scope:

- Vulnerabilities in upstream dependencies (`keyring`, the system `git`, `python` itself).
- Misuse: writing tokens into a public repo's JSON config, mode 0644 on `.env`, etc.

## Hall of fame

(none yet — be the first.)
