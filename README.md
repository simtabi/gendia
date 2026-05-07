# gendia

A CI/CD-friendly Packagist (and npm + PyPI) dev-workflow handler for polyrepo ecosystems across GitHub orgs, GitLab groups, and Bitbucket workspaces.

`gendia` is what stands between *your local repo state* and *the world seeing it*: it pushes pending commits and tags, fires the Packagist update webhook (or `npm publish` / `twine upload`), bumps changelogs, runs your verify pipeline, and audits the fleet for hygiene drift. One config file, one tool, one CLI verb per intent. Works the same on a developer laptop, on a VPS cron, and inside a Docker-based CI runner.

```text
$ gendia status
status — ok
  ✓ core                   main clean ahead=0 behind=0 latest_tag=v1.0.0
  ✓ browser                main clean ahead=0 behind=0 latest_tag=v1.0.0
  ✓ documentation          main clean ahead=0 behind=0 latest_tag=v1.0.0
  ✓ tabler-icons           main clean ahead=0 behind=0 latest_tag=v1.0.0
  ✓ bundled-icons          main clean ahead=0 behind=0 latest_tag=v1.0.0
  ✓ metronic-icons         main clean ahead=0 behind=0 latest_tag=v1.0.0
```

## Why

Maintaining a polyrepo Composer / npm / Python ecosystem means juggling: per-repo `composer.json` validation, CHANGELOG bumps, semver tagging, `git push --tags`, the Packagist update webhook, occasional Docker-image republishing, sometimes a parallel `npm publish` to a private registry — all wired together with bash scripts that drift out of sync the moment a repo gets renamed.

Existing multi-repo tools (`mr`, `mu-repo`, `gita`) stop at clone / pull / status; none of them know about Packagist webhooks, npm tokens, or CHANGELOG-bump conventions. `gendia` fills that gap. It treats a release as a *workflow* (verify → bump → tag → push → notify) and keeps the dev surface and the CI surface identical — `gendia release myorg/core 1.0.1` does the same thing on your laptop and inside a CI runner.

## Highlights

- **CI/CD-first design**: identical CLI on dev box, VPS cron, and Docker-based runners. JSON log format for structured log shippers; exit code reflects all-ok / failed.
- **Packagist-aware out of the box**: `sync` and `release` know how to fire the Packagist update endpoint when tags get pushed. npm and PyPI included as first-class push registries.
- **Three platforms from day one**: GitHub, GitLab (cloud or self-hosted), Bitbucket Cloud.
- **Two transports**: SSH (with optional explicit private-key path, `IdentitiesOnly=yes` to block agent enumeration) or HTTPS-with-API-token (via `GIT_ASKPASS`, never via URL embed, never in argv).
- **Multi-org / multi-account**: any number of accounts in one config; each repo binds to one account. A leak in one account's credentials can't touch another's repos.
- **Eight first-class operations**: `status`, `sync`, `audit`, `cleanup`, `release`, `verify`, `mirror`, `init`.
- **Dry-run on everything**: `--dry-run` is honoured by every mutating operation.
- **Zero runtime dependencies**: stdlib only. `keyring` is an optional extra for OS-keychain-backed secrets.
- **Docker-native**: slim image (~80 MB), runs as non-root, mounts your `~/.config/gendia` and SSH keys; `docker compose run gendia sync` is the deployable unit.

## Install

```bash
# Recommended: uv (fast, isolated)
uv tool install gendia

# Or pip
pip install gendia

# From source (development)
git clone git@github.com:simtabi/gendia.git
cd gendia
make install-dev
```

## Configure

Two files, both JSON. The machine-wide one declares accounts and registries; the per-project one declares the repos.

**`~/.config/gendia/gendia.json`** (machine-wide):

```json
{
  "accounts": {
    "myorg":     { "platform": "github",    "org": "myorg",        "credential_ref": "GITHUB_TOKEN_MYORG" },
    "personal":   { "platform": "github",    "username": "imanim",   "credential_ref": "GITHUB_TOKEN_PERSONAL" },
    "internal":   { "platform": "gitlab",    "host": "gitlab.simtabi.com", "group": "tools", "credential_ref": "GITLAB_TOKEN_INTERNAL" },
    "simtabi-bb": { "platform": "bitbucket", "workspace": "simtabi", "credential_ref": "BITBUCKET_APP_PASSWORD" }
  },
  "registries": {
    "packagist": { "kind": "packagist", "username": "simtabi", "credential_ref": "PACKAGIST_API_TOKEN" },
    "npm":        { "kind": "npm",       "credential_ref": "NPM_TOKEN" },
    "pypi":       { "kind": "pypi",      "credential_ref": "PYPI_API_TOKEN" }
  },
  "defaults": { "concurrency": 4, "log_format": "human" }
}
```

**`./gendia.json`** (one per project, lives at the project root):

```json
{
  "name": "myorg",
  "account": "myorg",
  "registry": "packagist",
  "root": "~/projects/myorg",
  "repos": [
    { "dir": "core",        "package": "myorg/core",        "verify": ["composer validate", "vendor/bin/pest"] },
    { "dir": "browser",     "package": "myorg/browser",     "verify": ["composer validate", "vendor/bin/pest"] },
    { "dir": "tabler-icons","package": "myorg/tabler-icons","verify": ["composer validate", "vendor/bin/pest"] }
  ],
  "cleanup_globs": ["vendor", "node_modules", ".phpunit.cache", ".DS_Store"]
}
```

**Secrets** never live in the JSON files. One `.env` covers every deployment shape — local dev, VPS, Docker, Kubernetes — by mixing two patterns side by side.

### Where does the `.env` live?

| Use case | Location | How gendia finds it |
|---|---|---|
| Local dev (default) | `~/.config/gendia/.env` | XDG-compliant default; no setup |
| VPS / system-wide | `/etc/gendia/.env` (or anywhere) | `export GENDIA_ENV_FILE=/etc/gendia/.env` |
| Per-project override | any path, e.g. `<project>/.env.gendia` | `GENDIA_ENV_FILE` in that shell / Makefile |
| Docker / Compose | host file bind-mounted at `/config/.env` | image sets `GENDIA_ENV_FILE=/config/.env` |
| Kubernetes | secrets at `/run/secrets/<name>` | use `<KEY>_FILE=/run/secrets/<name>` in any of the above |

**The file in this repo at `examples/.env.example` is a TEMPLATE only — never put real secrets there.** It's tracked by git. Copy it to the runtime location:

```sh
mkdir -p ~/.config/gendia
cp examples/.env.example ~/.config/gendia/.env
chmod 600 ~/.config/gendia/.env
$EDITOR ~/.config/gendia/.env
```

`gendia-env` warns if your `.env` has looser permissions than 0640.

Inside the file:

- **Direct value** for local dev: `GITHUB_TOKEN_MYORG=ghp_...`
- **`<KEY>_FILE`** for Docker / Compose / Kubernetes secret mounts: `GITHUB_TOKEN_MYORG_FILE=/run/secrets/github_token_myorg`. The file's contents become the value of `<KEY>` at runtime — the secret never appears in env vars or process listings. Both forms can coexist; `*_FILE` wins when both are set.

A small companion script ships in `bin/gendia-env` (also baked into the Docker image) that handles all of this for you. It sources the env file, resolves any `*_FILE` references, and runs whatever you exec it with — including `docker compose`, so `${VAR}` interpolation in `docker-compose.yml` Just Works:

```bash
gendia-env docker compose run --rm gendia status        # one-shot
gendia-env --check                                      # show keys + masked previews
gendia-env --docker-args                                # prints --env-file=PATH for docker
eval "$(gendia-env --export)"                           # source into current shell
```

A third optional resolver (when the `keyring` extra is installed): `pip install gendia[keyring]` then `keyring set gendia GITHUB_TOKEN_MYORG`. Skipped automatically when the package isn't installed (e.g., on a VPS).

Process environment variables override `.env` file values. The `--no-keyring` flag forces env-only resolution.

## Auth: SSH vs API token

Two distinct credential types are at play. `gendia` keeps them separate.

- **API token** (`credential_ref` in account config): used for HTTPS REST calls to GitHub / GitLab / Bitbucket APIs and Packagist's update endpoint. Always required.
- **SSH key** (`ssh_key_path` in account config, optional): used for git over SSH. When unset, the user's default SSH agent and `~/.ssh/config` apply — the right answer for dev machines.

Each account can pick its git transport via `git_auth`:

| `git_auth` | Behaviour |
|---|---|
| `"ssh"` (default) | Use SSH agent / `~/.ssh/config` / explicit `ssh_key_path` |
| `"https"` | Use the API token via `GIT_ASKPASS`; ideal for containers without an SSH agent |
| `"auto"` | SSH when a key is available, else HTTPS-with-token |

**Tokens are never embedded in URLs.** When `git_auth=https`, the token is supplied via a temporary `GIT_ASKPASS` script that prints the token to stdin. The token never appears in `argv` (visible to `ps`) or in the remote URL (visible in reflogs).

For a VPS deployment, the typical pattern is `git_auth: "auto"` with an explicit `ssh_key_path: /etc/gendia/keys/deploy_ed25519` — falls through to HTTPS-with-token if the key is missing.

## Commands

```bash
gendia status                                  # one-line summary per repo
gendia sync                                    # push pending commits + tags + notify webhook registry
gendia release <repo> <version>                # bump CHANGELOG, tag, push, notify
gendia audit                                   # hygiene checks
gendia cleanup                                 # rm cleanup_globs across all repos
gendia mirror --to ~/repos                     # clone every repo defined in config
gendia verify                                  # run each repo's verify[] commands
gendia init [--out FILE] [--force]             # scaffold a fresh gendia.json

# Modifiers (work with most commands):
--dry-run                  show what would happen, do nothing
--only repo-a,repo-b       operate on a subset
--skip repo-c
--concurrency 8            override default parallelism
--log-format json          for CI consumption
--no-keyring               skip the OS keyring backend; env / .env only
--config PATH              point at a specific gendia.json
```

## Make targets

```bash
make help              # list targets
make install           # uv tool install (or pip fallback)
make install-dev       # editable install with [dev] extras
make test              # pytest
make lint              # ruff
make typecheck         # mypy
make sync              # gendia sync (extra args via ARGS=...)
make release REPO=core VERSION=1.0.1
make audit / cleanup / verify / mirror / init / status
make docker-build / docker-shell / docker-push REGISTRY=ghcr.io/simtabi
```

## Docker / VPS

`gendia` ships a slim Python image (~80 MB) with the `gendia-env` helper baked in. The image's `ENTRYPOINT` is `gendia-env gendia`, so anything you put in CMD runs after the env file has been sourced and `*_FILE` secrets have been resolved.

```bash
# One-shot from the project root:
make docker-build
docker run --rm \
  --env-file ~/.config/gendia/.env \
  -v ~/.config/gendia:/config:ro \
  -v ~/.ssh:/home/gendia/.ssh:ro \
  -v "$PWD":/work \
  gendia:latest status
```

Or via Compose (recommended):

```bash
docker compose run --rm gendia status
docker compose run --rm gendia sync
docker compose run --rm gendia release myorg/core 1.0.1
```

Or via the Make targets:

```bash
make docker-build
make docker-shell                   # interactive bash inside the container
make docker-run VERB=sync           # one-shot any verb
```

VPS cron (every 30 minutes):

```cron
*/30 * * * * docker run --rm \
    --env-file /home/ops/.config/gendia/.env \
    -v /home/ops/.config/gendia:/config:ro \
    -v /home/ops/.ssh:/home/gendia/.ssh:ro \
    -v /home/ops/repos:/work \
    gendia:latest sync --log-format json \
    >> /var/log/gendia/sync.log 2>&1
```

For Kubernetes, mount each secret at `/run/secrets/<name>` via a Secret volume and set `<KEY>_FILE=/run/secrets/<name>` in the env. See the comments at the top of `examples/.env.example` for a complete recipe.

## Architecture

```
gendia/
├── src/gendia/
│   ├── auth/         credential resolvers: env, .env, Docker secrets, keyring
│   ├── config/       frozen dataclasses + JSON loader
│   ├── git/          GitRepo wrapper, safe subprocess shell, transport-auth env
│   ├── providers/    GitProvider abstract base + GitHub / GitLab / Bitbucket
│   ├── registries/   PackageRegistry hierarchy (Webhook / Publish capable)
│   ├── operations/   Operation abstract base + 8 concrete verbs
│   ├── observability logger.py (human + JSON formatters)
│   └── cli/          argparse setup + dispatcher
├── tests/unit/       schema, loader, auth, git_repo, git_auth, concurrency
├── examples/         myorg.json, multi-org.json, .env.example
├── bin/gendia        bash shim for development use
├── Dockerfile        multi-stage, ~80 MB
├── docker-compose.yml
└── Makefile
```

Each layer depends only on the layer below; SOLID applied across the board. Adding a new VCS = one file in `providers/`. Adding a new operation = one file in `operations/`.

## Contributing

```bash
git clone git@github.com:simtabi/gendia.git
cd gendia
make install-dev
make test
```

Tests must pass before merging. `make lint` and `make typecheck` should produce no errors. Conventional Commit messages preferred (`feat:`, `fix:`, `docs:`, `refactor:`, etc.).

## Security

- Tokens are never written to JSON config or argv.
- SSH transport uses `IdentitiesOnly=yes` to prevent SSH agent enumeration.
- HTTPS transport disables the system credential helper to prevent token caching.
- The Docker image runs as a non-root user.
- Report vulnerabilities to security@simtabi.com — not via public issues.

## License

This project is licensed under the MIT License.

© Simtabi LLC
