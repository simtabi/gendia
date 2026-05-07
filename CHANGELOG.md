# Changelog

All notable changes to `gendia` follow [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) and [Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-05-06

### Added

- Initial release. CI/CD-friendly Packagist (and npm + PyPI) dev-workflow handler for polyrepo ecosystems.
- Three VCS providers: GitHub (primary), GitLab (cloud + self-hosted), Bitbucket Cloud. Each implements list / get / create / set-secret to the extent supported by its API.
- Three package registries: Packagist (webhook-style update notification), npm (push via `npm publish`), PyPI (push via `twine upload`). Split via mix-ins (`WebhookCapable`, `PublishCapable`).
- Eight operations: `status`, `sync`, `release`, `audit`, `cleanup`, `verify`, `mirror`, `init`. All operations honour `--dry-run`, `--only`, `--skip`, `--concurrency`.
- Two-axis credential model. API tokens live in `credential_ref` (env / `.env` file / OS keyring / Docker secrets). SSH keys live in `ssh_key_path` (optional). Per-account `git_auth` chooses between `ssh`, `https` (API token via `GIT_ASKPASS`, never in URL or argv), or `auto`.
- Multi-account / multi-org config. JSON-based, with per-project overlay files. Validated via frozen dataclasses on load.
- Structured logging: human-friendly (with TTY colour) by default, JSON for CI / log shippers.
- Bounded thread-pool concurrency for fan-out operations (status, sync, audit, etc.). Exceptions captured per repo, never halt the fleet.
- Bash entry-point shim (`bin/gendia`) for development use without installing the wheel.
- Makefile with help-driven targets covering install, test, lint, typecheck, every operation, and Docker.
- Multi-stage Dockerfile (~80 MB final), runs as a non-root `gendia` user, suitable for VPS cron deployment.
- 29 unit tests covering schema invariants, config loading + precedence, credential resolution including Docker-secrets convention, the `GitRepo` wrapper against a real tmp repo, and the SSH/HTTPS auth-environment builder.
- Example configs for the Ichava ecosystem and a multi-org / multi-platform setup.

### Requirements

- Python 3.12+ (3.13 supported)
- `git` and `openssh-client` on PATH for git operations
- Optional: `npm` (only when publishing to npm), `twine` (only when publishing to PyPI), `keyring` (for OS-keychain credentials)
