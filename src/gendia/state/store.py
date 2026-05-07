"""Persistent sync-state storage.

A tiny append-mostly JSON store at `$XDG_CACHE_HOME/gendia/sync-state.json`
keeps track of: when each package was last synced or released, what tags
were pushed, what the outcome was. Read by `gendia inventory` to categorise
the fleet and by `gendia status` to enrich the per-repo line.

Atomic writes (write → fsync → rename) so the file is never seen in a
half-written state. Safe to delete: gendia recreates it on next sync.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

from gendia.util.paths import cache_home

_FILE_NAME = "sync-state.json"


@dataclass(frozen=True, slots=True)
class SyncRecord:
    """One package's most-recent sync outcome."""

    package: str  # e.g. "myorg/core"
    account: str  # account name from config
    last_action: str  # "sync" | "release"
    last_status: str  # "ok" | "failed" | "dirty" | "skipped"
    last_at: str  # ISO 8601 UTC timestamp
    last_tag: str | None = None  # tag pushed during this op (if any)
    transport: str | None = None  # "ssh" | "https" | "default"
    error: str | None = None
    extra: dict[str, object] = field(default_factory=dict)

    @classmethod
    def now(
        cls,
        *,
        package: str,
        account: str,
        action: str,
        status: str,
        tag: str | None = None,
        transport: str | None = None,
        error: str | None = None,
        extra: dict[str, object] | None = None,
    ) -> Self:
        return cls(
            package=package,
            account=account,
            last_action=action,
            last_status=status,
            last_at=datetime.now(tz=UTC).isoformat(),
            last_tag=tag,
            transport=transport,
            error=error,
            extra=dict(extra or {}),
        )

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> Self:
        extra = raw.get("extra") or {}
        if not isinstance(extra, dict):
            extra = {}
        return cls(
            package=str(raw["package"]),
            account=str(raw.get("account", "")),
            last_action=str(raw.get("last_action", "sync")),
            last_status=str(raw.get("last_status", "ok")),
            last_at=str(raw.get("last_at", "")),
            last_tag=(str(raw["last_tag"]) if raw.get("last_tag") else None),
            transport=(str(raw["transport"]) if raw.get("transport") else None),
            error=(str(raw["error"]) if raw.get("error") else None),
            extra=dict(extra),
        )

    def age_seconds(self, *, now: datetime | None = None) -> float | None:
        """Seconds since last_at, or None if the timestamp can't be parsed."""
        if not self.last_at:
            return None
        try:
            then = datetime.fromisoformat(self.last_at)
        except ValueError:
            return None
        ref = now or datetime.now(tz=UTC)
        return (ref - then).total_seconds()


class SyncStateStore:
    """Load / mutate / persist the JSON state file."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or (cache_home() / _FILE_NAME)
        self._records: dict[str, SyncRecord] = {}
        self._loaded = False

    @property
    def path(self) -> Path:
        return self._path

    # --- read ---------------------------------------------------------------

    def load(self) -> Self:
        if self._loaded:
            return self
        self._loaded = True
        if not self._path.is_file():
            return self
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self
        for entry in raw.get("packages", []):
            try:
                record = SyncRecord.from_dict(entry)
            except (KeyError, TypeError, ValueError):
                continue
            self._records[record.package] = record
        return self

    def get(self, package: str) -> SyncRecord | None:
        self.load()
        return self._records.get(package)

    def all(self) -> dict[str, SyncRecord]:
        self.load()
        return dict(self._records)

    # --- write --------------------------------------------------------------

    def upsert(self, record: SyncRecord) -> None:
        self.load()
        self._records[record.package] = record

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "saved_at": datetime.now(tz=UTC).isoformat(),
            "packages": [asdict(r) for r in self._records.values()],
        }
        rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"

        tmp_dir = str(self._path.parent)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=tmp_dir,
            delete=False,
            prefix=".sync-state.tmp-",
        ) as tmp:
            tmp_path = Path(tmp.name)
            tmp.write(rendered)
            tmp.flush()
            os.fsync(tmp.fileno())
        try:
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, self._path)
        except OSError:
            tmp_path.unlink(missing_ok=True)
            raise
