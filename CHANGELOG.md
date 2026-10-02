# Changelog

All notable changes to `gendia` follow [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) and [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added — Standards engine, scaffolds, autofix

- **JSON-driven standards engine** (`src/gendia/standards.py`, ~1280 LOC): a new layer on top of the existing `conventions` verb that lints repos against rule packs declared in JSON. New CLI surface: `gendia conventions --list-profiles`, `--list-rules --profile <name>`, `--explain <RULE_ID>`, `--no-legacy` (skip the 10 hardcoded checks), `--profile <name>` (repeatable). **16 bundled rule packs / 71 rules / 7 check kinds** — `file_exists`, `file_absent`, `regex_in_file`, `directory_exists`, `license_spdx`, `glob_exists`, `one_of_files`. Plugin syntax: a rule's `check:` field may be `module:function` to load org-internal checks.
- **`gendia conventions --fix`** (with `--dry-run`): **21 fix kinds** spanning the universal essentials (README / .gitignore / .editorconfig / .gitattributes), community health (CONTRIBUTING / CODE_OF_CONDUCT / SECURITY / CHANGELOG), GitHub workflows (CODEOWNERS / PR template / Dependabot / CodeQL / Scorecard), OpenSSF Security Insights, and **5 JSON-mutating fixes for `package.json`** (`name`, `version`, `license`, `engines.node`, `packageManager`). All fixes are idempotent; mutating fixes use atomic writes via tmpfile + `os.replace` and refuse symlinked targets. `set_package_license` auto-detects SPDX from an existing LICENSE file (`SPDX-License-Identifier:` line wins; falls back to a 17-marker body-text table).
- **`gendia init --scaffold`** (`src/gendia/scaffolds.py`, ~860 LOC): renders bundled scaffold templates with `${var}` substitution + post-actions (`git_init`, `license_text`, `audit`). **13 bundled scaffolds**: `bare`, `python-uv`, `python-poetry`, `rust`, `go`, `node-pnpm`, `node-bun`, `php-composer`, `ruby-gem`, `dotnet`, `docs-mkdocs`, `monorepo-pnpm-turbo`, `java-gradle`. **17 bundled license texts** for the `license_text` post-action. Security guards: path-traversal refusal (`is_relative_to`), symlink refusal, unresolved-variable detection.
- **`bin/gendia-menu`** + `make menu`: interactive numbered menu wrapping all 18 verbs with drill-down by category (Inspect / Release & Sync / Standards / Scaffold / Config). `--list` for non-interactive option dump. Shipped inside the Docker image so `make docker-exec` lands in a working toolkit shell.
- **Makefile extensions**: `make check` runs the full CI gate (lint + format-check + typecheck + tests), `make menu`, `make docker-up/down/ps/logs/exec/host-shell` for compose lifecycle, plus the existing `docker-build/run/push/shell`. `PYTHON` auto-detects `python3.12+` so it works on hosts where `python3` resolves to 3.10.
- **`.dockerignore`**: trims the build context (excludes `.venv/`, caches, `tests/`, `docs/`, `examples/`, build artefacts) so `docker build` is fast and tidy.
- **205 new unit tests** for the standards engine + scaffolds + autofix (running total: 389 across all changes in this Unreleased section). Coverage includes schema, every check kind, every fix kind (round-trip clears findings), every bundled rule pack, every bundled scaffold, every license text (via parametrize), security guards, JSON-mutating idempotency / malformed-JSON / missing-file / symlink-refusal, SPDX detection from LICENSE files, slugify edge cases.
- **Docs**: `docs/repo-standards-and-audit.md` (v3.0 driver doc, ~3000 lines, ships the integration plan as Section 18) and `docs/status.md` (canonical phase tracker — 10 audit passes catalogued).

### Added — SSH-config awareness layer

- **SSH-config awareness layer** (`src/gendia/ssh/`): parser (`SSHConfig`, `SSHHost`, `SSHHostList`) handles `Host` / `Match` / `Include` directives, multi-host lines, and per-host `IdentityFile` / `Port` / `User` / `ProxyJump`. JSON-driven forge registry (`ForgeRegistry`) with detection signals (exact hostname, substring pattern, comment hint, `User git` heuristic). Key-health auditor (`audit_keys`) flags loose permissions, DSA / weak-RSA, and shared-key reuse. Account synthesizer (`propose_accounts`) projects detected hosts onto gendia account stubs.
- **Bundled forge registry** (`src/gendia/data/forges.json`) shipped via `package_data`. Override per project with the new top-level `forges_file` key in `gendia.json`. See `examples/forges.json`.
- **Three new providers**: `gitea` (covers Gitea / Forgejo / Codeberg with one wire format), `azure` (Azure DevOps; PAT over Basic-auth), and `bitbucket-server` (Atlassian Data Center, distinct from Bitbucket Cloud). GitHub Enterprise now honoured via `Account.host` (`/api/v3` REST root, host-aware clone URLs).
- **Three new operations**: `gendia generate` (emit copy-paste git commands per detected SSH host in `text` / `script` / `json` / `markdown`; `--execute` runs the bash form), `gendia scan` (filesystem walk classifying every git repo as `clean` / `dirty` / `orphan` / `https-candidate`), and `gendia convert` (flip URLs between SSH and HTTPS, alias-aware via SSH config and `~/.gitconfig` `insteadOf` rules).
- **`gendia ssh` command group**: `ssh list`, `ssh test`, `ssh inspect`, `ssh bootstrap [--write]`. `ssh bootstrap` synthesizes account stubs from `~/.ssh/config` and merges them into `~/.config/gendia/gendia.json` on demand.
- **`gendia setup --from-ssh`**: same synthesis as `ssh bootstrap`, wired into the first-run wizard.
- **`gendia doctor`** now includes `audit_keys` findings (key permissions, weak-key types, shared-key reuse) under a new `ssh keys` section.
- **`Account.manual_repos`** schema field for repos the provider's API can't (or shouldn't) discover. JSON shape: `[{"host": "alias", "org": "scope", "repos": ["repo1", ...]}]`. Picked up by `generate` and reflected in the proposed config from `ssh bootstrap`.
- **Util helpers**: `gendia.util.url` (parse / convert SSH ↔ HTTPS, alias + `insteadOf` aware) and `gendia.util.repo_scan` (filesystem scanner returning `ScannedRepo` with classification helpers).
- **51 new unit tests for the SSH layer** (running total: 184 + 205 = 389 across all changes in this Unreleased section) covering SSH config parsing, forge detection, URL conversion, the filesystem scanner, the new providers, GitHub Enterprise host honouring, and end-to-end coverage of `generate` / `scan` / `convert`.
- **Examples**: `examples/forges.json` (forge registry overlay), `examples/ssh-bootstrap.json` (config showing `forges_file`, `manual_repos`, and the new providers).

### Notes

- Migrated from the standalone `git-helpers` script. The single-file `generate_git_commands.py` was decomposed across the new layers (parser, registry, providers, operations) with no behavioural regressions; the `git-commands.txt` artefact it produced is now reachable via `gendia generate --output git-commands.txt`. `git-helpers` is removed as a separate project; gendia is the supported replacement.

## [0.1.0] - 2026-05-06

### Added

- Initial release. CI/CD-friendly Packagist (and npm + PyPI) dev-workflow handler for polyrepo ecosystems.
- Three VCS providers: GitHub (primary), GitLab (cloud + self-hosted), Bitbucket Cloud. Each implements list / get / create / set-secret to the extent supported by its API.
- Three package registries: Packagist (webhook-style update notification), npm (push via `npm publish`), PyPI (push via `twine upload`). Split via mix-ins (`WebhookCapable`, `PublishCapable`).
- Nine fleet operations: `status`, `sync`, `inventory`, `release`, `audit`, `cleanup`, `verify`, `mirror`, `init`. All honour `--dry-run`, `--only`, `--skip`, `--concurrency`.
- Four workstation sidecar verbs: `config` (`list/get/set/unset/edit/path/doctor` for the `.env`), `setup` (interactive first-run wizard with `local/vps/project/docker/k8s/ci` shapes and runtime auto-detection), `doctor` (preflight covering env file, git/ssh binaries, ssh-agent, credentials), and `identity` (`list/check/apply/setup/init` for per-account git `user.name` / `user.email` / SSH alias / signing key, scoped global / project / specific path).
- Repo-hygiene linter: `conventions` (`gendia conventions [PATH] [--rules FILE] [--strict] [--json]`). Ten built-in checks (GitHub-special files, markdown kebab-case naming, banned glyphs like em-dash, decorator emojis, spec filename conventions, sub-folder readmes, stale link patterns, shell-script shebangs + executable bit, optional readme frontmatter, optional trailing-whitespace ban). Fully JSON-driven; every rule is opt-in/out per project via `examples/conventions.json`. Exit codes match `doctor` (0/1/2).
- Two-axis credential model. API tokens live in `credential_ref` (env / `.env` file / OS keyring / Docker secrets via the `<KEY>_FILE` convention). SSH keys live in `ssh_key_path` (optional). Per-account `git_auth` chooses between `ssh`, `https` (API token via `GIT_ASKPASS`, never in URL or argv), or `auto`.
- Multi-account / multi-org config. JSON-based, with per-project overlay files. Validated via frozen dataclasses on load.
- Per-account `sync_policy` with three modes (`explicit`, `all`, `patterns`) and `fnmatch` glob include / exclude rules so a leak in one account's credentials cannot push to another's repos.
- Per-account `git_identity` block (`name`, `email`, `ssh_alias`, `signing_key`, `sign_commits`, `sign_tags`) wired through `gendia identity` so commits never bleed identities between accounts.
- Sync-state store at `~/.cache/gendia/sync-state.json` (atomic writes) feeding `gendia inventory` cross-referenced against each provider's `list_repos()`.
- Structured logging: human-friendly (with TTY colour) by default, JSON for CI / log shippers.
- TTY-aware boxed banner on `--help` and bare invocation; suppressed for non-TTY, JSON output, and `--no-banner`.
- Bounded thread-pool concurrency for fan-out operations (status, sync, audit, etc.). Exceptions captured per repo, never halt the fleet.
- `bin/gendia-env` companion: sources the env file, resolves `<KEY>_FILE` references, and `exec`s the wrapped command. Baked into the Docker image as the `ENTRYPOINT`.
- Bash entry-point shim (`bin/gendia`) for development use without installing the wheel.
- Makefile with help-driven targets covering install, test, lint, typecheck, every operation, and Docker.
- Multi-stage Dockerfile (~80 MB final), runs as a non-root `gendia` user, suitable for VPS cron deployment.
- 133 unit tests covering schema invariants, config loading + precedence, credential resolution including Docker-secrets convention, the `GitRepo` wrapper against a real tmp repo, the SSH/HTTPS auth-environment builder, sync-policy matching, sync-state persistence, `GitIdentity` round-tripping, the `gendia identity init` wizard across global / project / includeIf scopes, and every individual `conventions` check (per-rule plus end-to-end orchestrator).
- Generic example configs (`single-org.json`, `multi-org.json`, `.env.example`) — placeholder vendor / host names only, no project-specific identifiers.

### Requirements

- Python 3.12+ (3.13 supported)
- `git` and `openssh-client` on PATH for git operations
- Optional: `npm` (only when publishing to npm), `twine` (only when publishing to PyPI), `keyring` (for OS-keychain credentials)
