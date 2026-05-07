"""`gendia inventory` — fleet-wide package inventory grouped by account.

For each configured account this op:
  1. Calls the provider's `list_repos()` to enumerate everything on the
     remote (read-only API call; cached for the duration of one run).
  2. Cross-references with the project's `gendia.json` to flag what's
     tracked locally.
  3. Cross-references with `~/.cache/gendia/sync-state.json` to surface
     last-sync time and outcome per package.

The result is a multi-axis classification:

    ✓ tracked + recently synced
    ⏱ tracked + never synced (or stale)
    ⚠ tracked but missing on remote (renamed / deleted upstream)
    ◌ on remote but untracked (candidate for adoption)

Output is grouped by account (org / workspace / user), with counts in the
section header. JSON output mirrors the structure for machine consumers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from gendia.config.schema import Account, RepoSpec
from gendia.observability.logger import get_logger
from gendia.operations.base import Operation, OperationContext, OperationResult, RepoResult
from gendia.providers.base import GitProvider, ProviderError, RepoInfo
from gendia.state import SyncRecord, SyncStateStore

_log = get_logger("ops.inventory")


# --- data shapes ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _PackageEntry:
    slug: str  # 'owner/repo' identifier
    category: str  # 'tracked-synced' | 'tracked-pending' | 'tracked-missing' | 'untracked'
    remote: RepoInfo | None  # None when the slug is local-only
    spec: RepoSpec | None  # None when the slug is remote-only
    record: SyncRecord | None  # None when never synced

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "category": self.category,
            "private": self.remote.private if self.remote else None,
            "default_branch": self.remote.default_branch if self.remote else None,
            "tracked": self.spec is not None,
            "last_synced_at": self.record.last_at if self.record else None,
            "last_status": self.record.last_status if self.record else None,
            "last_tag": self.record.last_tag if self.record else None,
        }


@dataclass
class _AccountSection:
    account: Account
    entries: list[_PackageEntry] = field(default_factory=list)
    error: str | None = None

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in self.entries:
            out[e.category] = out.get(e.category, 0) + 1
        return out


# --- operation --------------------------------------------------------------


class InventoryOperation(Operation):
    """Run once across all accounts; fan-out is per-account, not per-repo."""

    name = "inventory"

    def __init__(self, ctx: OperationContext, *, account_filter: str | None = None) -> None:
        super().__init__(ctx)
        self._account_filter = account_filter
        self._state = SyncStateStore().load()

    def run(self) -> OperationResult:  # override; not per-repo
        result = OperationResult(name=self.name, dry_run=self._ctx.dry_run)
        sections = self._collect_sections()

        # Embed each section as a RepoResult so the standard JSON output works.
        for section in sections:
            counts = section.counts()
            summary = ", ".join(f"{cat}={n}" for cat, n in sorted(counts.items())) or "no repos"
            result.repos.append(
                RepoResult(
                    dir=section.account.name,
                    ok=section.error is None,
                    summary=summary,
                    detail={
                        "account": {
                            "name": section.account.name,
                            "platform": section.account.platform,
                            "scope": section.account.scope,
                        },
                        "counts": counts,
                        "entries": [e.to_dict() for e in section.entries],
                        "error": section.error,
                    },
                    error=section.error,
                )
            )

        self._render_human(sections)
        return result

    def _apply_to_repo(self, repo: RepoSpec) -> RepoResult:  # pragma: no cover
        raise NotImplementedError("inventory.run() does its own fan-out across accounts")

    # --- collection ---------------------------------------------------------

    def _collect_sections(self) -> list[_AccountSection]:
        accounts = list(self._ctx.config.accounts.values())
        if self._account_filter:
            accounts = [a for a in accounts if a.name == self._account_filter]

        # Project-tracked slugs, indexed by package slug for lookup.
        tracked_by_slug: dict[str, RepoSpec] = {}
        if self._ctx.project:
            for spec in self._ctx.project.repos:
                if spec.package:
                    tracked_by_slug[spec.package] = spec

        sections: list[_AccountSection] = []
        for account in accounts:
            section = _AccountSection(account=account)
            try:
                provider = self._build_provider(account)
                remotes = provider.list_repos()
            except (ProviderError, RuntimeError) as exc:
                section.error = str(exc)
                sections.append(section)
                continue

            section.entries = self._classify(account, remotes, tracked_by_slug)
            sections.append(section)
        return sections

    def _build_provider(self, account: Account) -> GitProvider:
        # Re-use the resolver from the active context; it already has the
        # right credential backends wired up.
        from gendia.auth import (  # noqa: PLC0415 — local import keeps cli/ off the cold-start path
            CredentialResolver,
            DotEnvCredentialStore,
        )
        from gendia.providers import build_provider  # noqa: PLC0415
        from gendia.util.paths import env_file_path  # noqa: PLC0415

        # If the active context's provider matches the requested account,
        # reuse it (it already has the right token in memory).
        if self._ctx.provider.account.name == account.name:
            return self._ctx.provider
        # Otherwise build a fresh provider with an env-only resolver. Doctor
        # output will surface any missing credentials.
        resolver = CredentialResolver(DotEnvCredentialStore(env_file=env_file_path()))
        return build_provider(account, resolver)

    def _classify(
        self,
        account: Account,
        remotes: list[RepoInfo],
        tracked_by_slug: dict[str, RepoSpec],
    ) -> list[_PackageEntry]:
        entries: list[_PackageEntry] = []
        remote_by_slug = {r.slug: r for r in remotes}

        # Account-scoped tracked slugs only — a package belongs to whichever
        # account's scope matches its slug owner.
        account_scope = account.scope
        scoped_tracked = {
            slug: spec
            for slug, spec in tracked_by_slug.items()
            if slug.startswith(f"{account_scope}/")
        }

        for slug, remote in sorted(remote_by_slug.items()):
            if not slug.startswith(f"{account_scope}/"):
                continue
            spec = scoped_tracked.get(slug)
            record = self._state.get(slug)
            category = self._categorise(spec=spec, record=record, remote=remote)
            entries.append(
                _PackageEntry(
                    slug=slug,
                    category=category,
                    remote=remote,
                    spec=spec,
                    record=record,
                )
            )

        # Tracked-but-missing-on-remote: in scoped_tracked but not in remotes.
        for slug, spec in sorted(scoped_tracked.items()):
            if slug in remote_by_slug:
                continue
            record = self._state.get(slug)
            entries.append(
                _PackageEntry(
                    slug=slug,
                    category="tracked-missing",
                    remote=None,
                    spec=spec,
                    record=record,
                )
            )

        return entries

    @staticmethod
    def _categorise(
        *,
        spec: RepoSpec | None,
        record: SyncRecord | None,
        remote: RepoInfo | None,
    ) -> str:
        if spec is None and remote is not None:
            return "untracked"
        if spec is not None and remote is None:
            return "tracked-missing"
        if record is None or record.last_status != "ok":
            return "tracked-pending"
        return "tracked-synced"

    # --- presentation -------------------------------------------------------

    def _render_human(self, sections: list[_AccountSection]) -> None:
        glyph = {
            "tracked-synced": "✓",
            "tracked-pending": "⏱",
            "tracked-missing": "⚠",
            "untracked": "◌",
        }
        for section in sections:
            counts = section.counts()
            count_str = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no repos"
            header = (
                f"== {section.account.name} "
                f"({section.account.platform}: {section.account.scope}) — {count_str} =="
            )
            print(header)
            if section.error:
                print(f"  ✗ error: {section.error}")
                print()
                continue
            if not section.entries:
                print("  (no repos in this scope)")
                print()
                continue
            for entry in section.entries:
                ago = _humanise_age(entry.record)
                tag = (
                    f" [{entry.record.last_tag}]" if entry.record and entry.record.last_tag else ""
                )
                print(f"  {glyph[entry.category]} {entry.slug:<35} {entry.category:<16}{tag} {ago}")
            print()


def _humanise_age(record: SyncRecord | None) -> str:
    if record is None:
        return ""
    seconds = record.age_seconds(now=datetime.now(tz=UTC))
    if seconds is None:
        return ""
    if seconds < _MINUTE:
        return "just now"
    if seconds < _HOUR:
        return f"{int(seconds / _MINUTE)}m ago"
    if seconds < _DAY:
        return f"{int(seconds / _HOUR)}h ago"
    return f"{int(seconds / _DAY)}d ago"


_MINUTE = 60
_HOUR = 3600
_DAY = 86400
