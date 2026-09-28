# PART: accumulation-ledger v0.1.1 (parts@86cf1c6)
"""Persistence seam for accumulation-ledger.

Real databases implement :class:`LedgerBackend`; the in-memory implementation
is deliberately small and is covered by the package's conformance suite.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
from typing import Any, Protocol


class LedgerBackend(Protocol):
    """Minimum database contract: insert, scoped read/update/query and purge.

    ``purge_raw`` must compare ISO-8601 timestamps as timezone-aware datetime
    instants, never by their lexical string representation.
    """

    def insert(self, record: Mapping[str, Any]) -> str: ...

    def get(self, record_id: str, *, scope: Mapping[str, str]) -> dict[str, Any] | None: ...

    def update(
        self, record_id: str, *, scope: Mapping[str, str], changes: Mapping[str, Any]
    ) -> dict[str, Any] | None: ...

    def query(self, *, scope: Mapping[str, str]) -> list[dict[str, Any]]: ...

    def purge_raw(self, *, scope: Mapping[str, str], cutoff: str) -> list[str]: ...


class InMemoryLedgerBackend:
    """Scoped record store for tests, local development, and conformance checks."""

    def __init__(self) -> None:
        self._records: dict[str, dict[str, Any]] = {}

    def insert(self, record: Mapping[str, Any]) -> str:
        record_id = record.get("record_id")
        if not isinstance(record_id, str) or not record_id:
            raise ValueError("ledger_backend_record_id_required")
        if record_id in self._records:
            raise ValueError("ledger_backend_record_id_conflict")
        self._records[record_id] = deepcopy(dict(record))
        return record_id

    def get(self, record_id: str, *, scope: Mapping[str, str]) -> dict[str, Any] | None:
        record = self._records.get(record_id)
        if record is None or not _scope_matches(record, scope):
            return None
        return deepcopy(record)

    def update(
        self, record_id: str, *, scope: Mapping[str, str], changes: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        record = self._records.get(record_id)
        if record is None or not _scope_matches(record, scope):
            return None
        record.update(deepcopy(dict(changes)))
        return deepcopy(record)

    def query(self, *, scope: Mapping[str, str]) -> list[dict[str, Any]]:
        rows = [deepcopy(record) for record in self._records.values() if _scope_matches(record, scope)]
        return sorted(rows, key=lambda record: str(record["record_id"]))

    def purge_raw(self, *, scope: Mapping[str, str], cutoff: str) -> list[str]:
        purged: list[str] = []
        cutoff_at = _parse_iso_instant(cutoff)
        for record_id, record in self._records.items():
            recorded_at = record.get("recorded_at")
            if (
                not _scope_matches(record, scope)
                or not isinstance(recorded_at, str)
                or _parse_iso_instant(recorded_at) >= cutoff_at
            ):
                continue
            raw_keys = [key for key in record if key.endswith("_raw")]
            if not any(record.get(key) is not None for key in raw_keys):
                continue
            for key in raw_keys:
                record[key] = None
            purged.append(record_id)
        return sorted(purged)


def _scope_matches(record: Mapping[str, Any], scope: Mapping[str, str]) -> bool:
    stored = record.get("scope")
    return isinstance(stored, Mapping) and dict(stored) == dict(scope)


def _parse_iso_instant(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("ledger_backend_timestamp_invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("ledger_backend_timestamp_timezone_required")
    return parsed
