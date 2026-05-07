"""End-to-end-ish tests for the local-only operations.

Operations exercised against tmpdir fixtures, no network. Provider is a
simple in-test fake that satisfies the GitProvider interface enough for
the operation under test.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from gendia.config.schema import (
    Account,
    Defaults,
    GendiaConfig,
    Project,
    RepoSpec,
)
from gendia.operations import (
    AuditOperation,
    CleanupOperation,
    InitOperation,
    OperationContext,
    StatusOperation,
)
from gendia.providers.base import GitProvider, RepoInfo

# --- helpers -----------------------------------------------------------------


class _FakeProvider(GitProvider):
    """Bare-minimum GitProvider for ops that don't talk to the upstream API."""

    @property
    def platform(self) -> str:
        return "github"

    def clone_url(self, slug: str, *, ssh: bool = True) -> str:
        return f"git@github.com:{slug}.git"

    def web_url(self, slug: str) -> str:
        return f"https://github.com/{slug}"

    def list_repos(self) -> list[RepoInfo]:
        return []

    def get_repo(self, slug: str) -> RepoInfo:  # pragma: no cover
        raise NotImplementedError

    def _auth_header(self) -> str:
        return ""


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def fake_account() -> Account:
    return Account(name="fake", platform="github", credential_ref="X", org="fake")


@pytest.fixture
def fake_provider(fake_account: Account) -> GitProvider:
    return _FakeProvider(account=fake_account, token=None)


@pytest.fixture
def project_root(tmp_path: Path) -> Iterator[Path]:
    """Set up a project root with two real git-init'd repos inside it."""
    for sub in ("alpha", "beta"):
        d = tmp_path / sub
        d.mkdir()
        _git(d, "init", "--initial-branch=main")
        _git(d, "config", "user.email", "test@example.com")
        _git(d, "config", "user.name", "Test")
        (d / "README.md").write_text(f"# {sub}", encoding="utf-8")
        (d / "LICENSE").write_text("MIT", encoding="utf-8")
        _git(d, "add", ".")
        _git(d, "commit", "-m", "init")
    yield tmp_path


@pytest.fixture
def context(
    project_root: Path,
    fake_account: Account,
    fake_provider: GitProvider,
) -> OperationContext:
    project = Project(
        name="test",
        account="fake",
        root=project_root,
        repos=(RepoSpec(dir="alpha"), RepoSpec(dir="beta")),
        cleanup_globs=("dist", ".cache"),
    )
    config = GendiaConfig(
        accounts={"fake": fake_account},
        registries={},
        project=project,
        defaults=Defaults(concurrency=1),
    )
    return OperationContext(
        config=config,
        project=project,
        provider=fake_provider,
        registry=None,
        dry_run=False,
        workers=1,
    )


# --- Status ------------------------------------------------------------------


def test_status_reports_clean_repos(context: OperationContext) -> None:
    result = StatusOperation(context).run()

    assert result.all_ok
    assert len(result.repos) == 2
    for repo in result.repos:
        assert repo.detail["clean"] is True
        assert repo.detail["branch"] == "main"
        assert repo.detail["latest_tag"] == "—"


def test_status_flags_dirty_tree(
    context: OperationContext,
    project_root: Path,
) -> None:
    (project_root / "alpha" / "scratch.txt").write_text("x", encoding="utf-8")

    result = StatusOperation(context).run()

    alpha = next(r for r in result.repos if r.dir == "alpha")
    beta = next(r for r in result.repos if r.dir == "beta")
    assert alpha.detail["clean"] is False
    assert beta.detail["clean"] is True


# --- Audit -------------------------------------------------------------------


def test_audit_clean_when_no_findings(context: OperationContext) -> None:
    result = AuditOperation(context).run()
    assert result.all_ok
    for r in result.repos:
        assert r.detail["findings"] == []


def test_audit_flags_missing_license(
    context: OperationContext,
    project_root: Path,
) -> None:
    (project_root / "alpha" / "LICENSE").unlink()

    result = AuditOperation(context).run()

    alpha = next(r for r in result.repos if r.dir == "alpha")
    assert not alpha.ok
    assert any("LICENSE" in f for f in alpha.detail["findings"])


def test_audit_flags_dirty_tree(
    context: OperationContext,
    project_root: Path,
) -> None:
    (project_root / "alpha" / "scratch.txt").write_text("x", encoding="utf-8")

    result = AuditOperation(context).run()

    alpha = next(r for r in result.repos if r.dir == "alpha")
    assert not alpha.ok
    assert any("uncommitted" in f for f in alpha.detail["findings"])


def test_audit_flags_invalid_composer_json(
    context: OperationContext,
    project_root: Path,
) -> None:
    (project_root / "alpha" / "composer.json").write_text("{not valid json", encoding="utf-8")

    result = AuditOperation(context).run()

    alpha = next(r for r in result.repos if r.dir == "alpha")
    assert any("composer.json invalid" in f for f in alpha.detail["findings"])


# --- Cleanup -----------------------------------------------------------------


def test_cleanup_removes_globs(
    context: OperationContext,
    project_root: Path,
) -> None:
    (project_root / "alpha" / "dist").mkdir()
    (project_root / "alpha" / "dist" / "build.bin").write_text("x", encoding="utf-8")
    (project_root / "alpha" / ".cache").mkdir()
    (project_root / "beta" / "dist").mkdir()

    result = CleanupOperation(context).run()

    assert result.all_ok
    assert not (project_root / "alpha" / "dist").exists()
    assert not (project_root / "alpha" / ".cache").exists()
    assert not (project_root / "beta" / "dist").exists()


def test_cleanup_dry_run_lists_but_does_not_delete(
    context: OperationContext,
    project_root: Path,
) -> None:
    (project_root / "alpha" / "dist").mkdir()

    op = CleanupOperation(context).with_dry_run(True)
    result = op.run()

    assert result.all_ok
    assert (project_root / "alpha" / "dist").exists()  # not deleted
    alpha = next(r for r in result.repos if r.dir == "alpha")
    assert "dist" in alpha.detail["removed"]


def test_cleanup_refuses_absolute_glob(
    context: OperationContext,
    project_root: Path,
) -> None:
    bad_project = Project(
        name="bad",
        account="fake",
        root=project_root,
        repos=(RepoSpec(dir="alpha", cleanup_globs=("/etc/passwd",)),),
    )
    bad_ctx = OperationContext(
        config=context.config,
        project=bad_project,
        provider=context.provider,
        registry=None,
        workers=1,
    )

    result = CleanupOperation(bad_ctx).run()

    alpha = next(r for r in result.repos if r.dir == "alpha")
    assert any("absolute" in s for s in alpha.detail["skipped"])


# --- Init --------------------------------------------------------------------


def test_init_writes_template(
    context: OperationContext,
    tmp_path: Path,
) -> None:
    target = tmp_path / "gendia.json"
    InitOperation(context, target=target).run()

    assert target.is_file()
    parsed = json.loads(target.read_text(encoding="utf-8"))
    assert parsed["name"] == "my-project"
    assert "repos" in parsed


def test_init_refuses_to_overwrite_without_force(
    context: OperationContext,
    tmp_path: Path,
) -> None:
    target = tmp_path / "gendia.json"
    target.write_text('{"existing": true}', encoding="utf-8")

    result = InitOperation(context, target=target, force=False).run()

    assert not result.all_ok
    assert json.loads(target.read_text(encoding="utf-8")) == {"existing": True}


def test_init_overwrites_with_force(
    context: OperationContext,
    tmp_path: Path,
) -> None:
    target = tmp_path / "gendia.json"
    target.write_text('{"existing": true}', encoding="utf-8")

    InitOperation(context, target=target, force=True).run()

    assert json.loads(target.read_text(encoding="utf-8"))["name"] == "my-project"
