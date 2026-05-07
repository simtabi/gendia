# Contributing to gendia

## Development setup

```bash
git clone git@github.com:simtabi/gendia.git
cd gendia
make install-dev
make test
```

You'll need Python 3.12 or newer, `git`, and `make`.

## Adding a feature

The architecture is layered: pick the right layer.

| Adding | Location | Effort |
|---|---|---|
| A new VCS platform (GitHub Enterprise, Codeberg, Forgejo, …) | `src/gendia/providers/<name>.py` + register in `providers/factory.py` | medium |
| A new package registry (Cargo, RubyGems, …) | `src/gendia/registries/<name>.py` + register in `registries/factory.py` | medium |
| A new operation (e.g. `gendia tag`, `gendia diff`) | `src/gendia/operations/<name>.py` + dispatch in `cli/arguments.py` | small |
| A new credential backend (Vault, AWS Secrets Manager, …) | `src/gendia/auth/<backend>_store.py` + add to `_build_credentials` in `cli/arguments.py` | small |

Each new file should mirror the structure of an existing peer. The abstract base classes (`GitProvider`, `PackageRegistry`, `Operation`, `CredentialStore`) document the contract.

## Tests

Every new public class or function needs a unit test in `tests/unit/`. We aim for behaviour coverage, not line coverage; a test that exercises one realistic scenario beats five that exercise edge cases.

`tests/integration/` is for end-to-end runs that hit a real git tree (in a tmpdir) but no network. Network-touching tests don't belong here; mock or skip them.

## Style

- Conventional Commits (`feat:`, `fix:`, `docs:`, `refactor:`, etc.).
- `make lint` and `make typecheck` produce no errors.
- Docstrings on every public class and function.
- No `from __future__ import annotations` is fine (Python 3.12 has PEP 563-equivalent behaviour by default in many libs); we still keep it for older-tool compatibility.
- One public class per module. Module docstring explains *why* the file exists.
- Prefer composition over inheritance. The few abstract bases we use are deliberate.

## Reviews

PRs need a green CI build and one approval. Reviewers look for:

- Does this respect the layer boundaries?
- Is the public API surface minimal?
- Does it handle the dry-run path?
- Are credentials handled correctly? (no argv embedding, no URL embedding, no logs)
- Are there tests?

## Release

```bash
make release REPO=. VERSION=0.2.0
```

(Requires write access to the upstream repo; not something contributors do.)
