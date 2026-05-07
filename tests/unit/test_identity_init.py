"""`gendia identity init` wizard.

Covers the per-account initialiser the user invokes when wiring git
identity onto a fresh machine. The wizard has three scope flags
(`--global`, `--project`, `--path`) plus a default (no flags → project),
and a `--dry-run` mode that must not touch anything.

Each test sandboxes HOME via monkeypatch so global writes are isolated
from the runner's real `~/.gitconfig`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gendia.cli.commands.identity import _cmd_init
from gendia.config.schema import (
    Account,
    Defaults,
    GendiaConfig,
    GitIdentity,
    Project,
    RepoSpec,
)

# --- fixtures ----------------------------------------------------------------


def _account(
    name: str = "myorg",
    *,
    identity: GitIdentity | None = None,
) -> Account:
    return Account(
        name=name,
        platform="github",
        credential_ref=f"GITHUB_TOKEN_{name.upper()}",
        org=name,
        git_identity=identity or GitIdentity(),
    )


def _config(
    *,
    accounts: dict[str, Account] | None = None,
    project: Project | None = None,
) -> GendiaConfig:
    return GendiaConfig(
        accounts=accounts or {},
        registries={},
        project=project,
        defaults=Defaults(),
    )


def _project(root: Path, repos: tuple[RepoSpec, ...]) -> Project:
    return Project(name="myorg", account="myorg", root=root, repos=repos)


def _init_git_repo(path: Path) -> None:
    """Make `path` a real git repo so per-repo .git/config writes can hit it."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)


@pytest.fixture
def sandbox_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect Path.home() so any ~/.gitconfig / ~/.config writes land here."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    # `git config --global` consults $HOME → already redirected by setenv above.
    return home


@pytest.fixture
def identity() -> GitIdentity:
    return GitIdentity(
        name="Tester",
        email="19682005+tester@users.noreply.github.com",
    )


# --- error paths -------------------------------------------------------------


def test_init_unknown_account_returns_2(
    sandbox_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _config(accounts={"myorg": _account(identity=GitIdentity(name="X", email="x@example"))})
    rc = _cmd_init(
        cfg,
        account_name="ghost",
        global_=False,
        project=False,
        paths=(),
        dry_run=True,
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "unknown account 'ghost'" in err
    assert "known: myorg" in err


def test_init_account_with_empty_identity_returns_2(
    sandbox_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _config(accounts={"myorg": _account()})  # default GitIdentity is empty
    rc = _cmd_init(
        cfg,
        account_name="myorg",
        global_=False,
        project=False,
        paths=(),
        dry_run=True,
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "no git_identity block" in err


# --- dry-run paths -----------------------------------------------------------


def test_init_dry_run_writes_nothing(
    sandbox_home: Path,
    identity: GitIdentity,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _config(accounts={"myorg": _account(identity=identity)})
    rc = _cmd_init(
        cfg,
        account_name="myorg",
        global_=True,
        project=False,
        paths=(str(sandbox_home / "projects"),),
        dry_run=True,
    )
    assert rc == 0

    out = capsys.readouterr().out
    assert "would write" in out
    assert "would set" in out
    assert "would add" in out
    assert "DRY-RUN" in out

    # Nothing was actually created on disk.
    assert not (sandbox_home / ".gitconfig").exists()
    assert not (sandbox_home / ".config" / "gendia").exists()


def test_init_default_scope_is_project(
    sandbox_home: Path,
    identity: GitIdentity,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No --global / --project / --path means: act as if --project were set."""
    project_root = tmp_path / "proj"
    repo_path = project_root / "core"
    _init_git_repo(repo_path)

    cfg = _config(
        accounts={"myorg": _account(identity=identity)},
        project=_project(project_root, (RepoSpec(dir="core", package="myorg/core"),)),
    )
    rc = _cmd_init(
        cfg,
        account_name="myorg",
        global_=False,
        project=False,  # default-default: should switch on
        paths=(),
        dry_run=True,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "would apply per-repo .git/config to 1 repo(s)" in out


# --- real writes (sandboxed via $HOME) --------------------------------------


def test_init_global_writes_user_name_and_email_into_gitconfig(
    sandbox_home: Path,
    identity: GitIdentity,
) -> None:
    cfg = _config(accounts={"myorg": _account(identity=identity)})
    rc = _cmd_init(
        cfg,
        account_name="myorg",
        global_=True,
        project=False,
        paths=(),
        dry_run=False,
    )
    assert rc == 0

    name = subprocess.run(
        ["git", "config", "--global", "--get", "user.name"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    email = subprocess.run(
        ["git", "config", "--global", "--get", "user.email"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert name == "Tester"
    assert email == "19682005+tester@users.noreply.github.com"


def test_init_writes_per_account_snippet_to_xdg_dir(
    sandbox_home: Path,
    identity: GitIdentity,
) -> None:
    cfg = _config(accounts={"myorg": _account(identity=identity)})
    rc = _cmd_init(
        cfg,
        account_name="myorg",
        global_=False,
        project=False,
        paths=(),
        dry_run=False,
    )
    assert rc == 0
    snippet = sandbox_home / ".config" / "gendia" / "identity-myorg.gitconfig"
    assert snippet.is_file()
    content = snippet.read_text(encoding="utf-8")
    assert "name = Tester" in content
    assert "email = 19682005+tester@users.noreply.github.com" in content
    assert (snippet.stat().st_mode & 0o777) == 0o600


def test_init_path_adds_includeif_to_gitconfig(
    sandbox_home: Path,
    identity: GitIdentity,
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "myorg-projects"
    project_dir.mkdir()

    cfg = _config(accounts={"myorg": _account(identity=identity)})
    rc = _cmd_init(
        cfg,
        account_name="myorg",
        global_=False,
        project=False,
        paths=(str(project_dir),),
        dry_run=False,
    )
    assert rc == 0

    # Check the includeIf landed in ~/.gitconfig with the trailing slash on gitdir.
    result = subprocess.run(
        ["git", "config", "--global", "--list"],
        check=True,
        capture_output=True,
        text=True,
    )
    listing = result.stdout
    expected_key = f"includeif.gitdir:{project_dir.resolve()}/.path".lower()
    assert any(line.lower().startswith(expected_key) for line in listing.splitlines()), listing


def test_init_project_writes_per_repo_git_config(
    sandbox_home: Path,
    identity: GitIdentity,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "proj"
    repo_path = project_root / "core"
    _init_git_repo(repo_path)

    cfg = _config(
        accounts={"myorg": _account(identity=identity)},
        project=_project(project_root, (RepoSpec(dir="core", package="myorg/core"),)),
    )
    rc = _cmd_init(
        cfg,
        account_name="myorg",
        global_=False,
        project=True,
        paths=(),
        dry_run=False,
    )
    assert rc == 0

    name = subprocess.run(
        ["git", "config", "--get", "user.name"],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    email = subprocess.run(
        ["git", "config", "--get", "user.email"],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert name == "Tester"
    assert email == "19682005+tester@users.noreply.github.com"


def test_init_project_skips_repo_without_dot_git(
    sandbox_home: Path,
    identity: GitIdentity,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A repo declared in gendia.json that hasn't been cloned yet is skipped, not erroring."""
    project_root = tmp_path / "proj"
    (project_root / "core").mkdir(parents=True)  # exists but no .git

    cfg = _config(
        accounts={"myorg": _account(identity=identity)},
        project=_project(project_root, (RepoSpec(dir="core", package="myorg/core"),)),
    )
    rc = _cmd_init(
        cfg,
        account_name="myorg",
        global_=False,
        project=True,
        paths=(),
        dry_run=False,
    )
    assert rc == 0
    assert "applied per-repo .git/config to 0 repo(s)" in capsys.readouterr().out
