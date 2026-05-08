# Changelog

All notable changes to `gendia` follow [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) and [Semantic Versioning](https://semver.org/).

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
