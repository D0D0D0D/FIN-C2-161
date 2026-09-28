# PART: accumulation-ledger v0.1.1 (parts@86cf1c6)
"""Schema ownership and lifecycle operations for accumulated records."""

from __future__ import annotations

import base64
import copy
import math
import re
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from secrets import token_bytes
from types import MappingProxyType
from typing import Any

from shared.utils.audit_logger import emit_trace_event

from .backend import InMemoryLedgerBackend, LedgerBackend
from .sanitize import LedgerValidationError, require_scope, sanitize_retrieval_text, sanitize_text, validate_scope_keys


_DECISION_SOURCES = frozenset({"human", "ai", "import"})
_STATUSES = frozenset({"active", "superseded", "disputed", "expired"})
_SERVICE_OWNED_KEYS = frozenset(
    {
        "record_id",
        "schema_version",
        "scope",
        "recorded_at",
        "status",
        "superseded_by",
        "supersede_reason",
        "superseded_at",
        "redacted_at",
        "retention_purged_at",
        "status_history",
        "dispute_reason",
    }
)


@dataclass(frozen=True)
class LedgerConfig:
    """Template-declared contract for a copied accumulation-ledger part."""

    scope_keys: tuple[str, ...]
    record_kinds: frozenset[str]
    classification_codes: Mapping[str, frozenset[str]]
    verdict_codes: frozenset[str]
    conflict_key_fields: tuple[str, ...]
    domain_fields: Mapping[str, str] = field(default_factory=dict)
    schema_version: str = "1.0"
    supersede_mode: str = "auto"
    record_id_prefix: str = "ledger"
    max_body_chars: int = 4000
    max_rationale_chars: int = 2000
    max_retrieval_chars: int = 512
    max_scalar_chars: int = 64

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope_keys", validate_scope_keys(self.scope_keys))
        object.__setattr__(self, "record_kinds", _finite_strings(self.record_kinds, "record_kinds"))
        object.__setattr__(self, "verdict_codes", _finite_strings(self.verdict_codes, "verdict_codes"))
        object.__setattr__(self, "classification_codes", MappingProxyType(_classification_codes(self.classification_codes)))
        object.__setattr__(self, "domain_fields", MappingProxyType(_domain_fields(self.domain_fields)))
        object.__setattr__(self, "conflict_key_fields", _conflict_key_fields(self.conflict_key_fields))
        if not set(self.conflict_key_fields) <= set(self.domain_fields):
            raise ValueError("LedgerConfig: conflict_key_fields must be declared domain_fields")
        if any(self.domain_fields[field] != "scalar" for field in self.conflict_key_fields):
            raise ValueError("LedgerConfig: conflict_key_fields must use scalar domain_fields only")
        if self.supersede_mode not in {"auto", "review_required"}:
            raise ValueError("LedgerConfig: supersede_mode must be auto or review_required")
        if not self.schema_version or not isinstance(self.schema_version, str):
            raise ValueError("LedgerConfig: schema_version is required")
        if not self.record_id_prefix.isascii() or not self.record_id_prefix.isalpha():
            raise ValueError("LedgerConfig: record_id_prefix must contain ASCII letters only")
        if any(limit <= 0 for limit in (self.max_body_chars, self.max_rationale_chars, self.max_retrieval_chars, self.max_scalar_chars)):
            raise ValueError("LedgerConfig: text limits must be positive")


class LedgerService:
    """Owns ledger schema validation, safe projection, and record lifecycle.

    The service retains only immutable configuration, a backend handle, and a
    clock.  All mutable business data lives in the backend records, so a node
    can safely share one service instance without cross-invocation state.
    """

    def __init__(
        self,
        config: LedgerConfig,
        *,
        backend: LedgerBackend | None = None,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self._config = config
        self._backend: LedgerBackend = backend or InMemoryLedgerBackend()
        self._clock = clock or _utc_now_iso

    def write(
        self, scope: Mapping[str, Any], record: Mapping[str, Any], state: dict | None = None
    ) -> dict[str, Any]:
        """Validate, sanitize, mint, and persist one record.

        ``body``/``body_raw`` and ``rationale``/``rationale_raw`` are input
        aliases.  ``*_audit`` and ``*_safe`` are service-owned and cannot be
        supplied by a template call site.
        """

        bound_scope = self._scope(scope)
        normalized = self._normalize_new_record(bound_scope, record)
        explicit_target = self._explicit_correction_target(normalized, bound_scope)
        self._backend.insert(normalized)
        self._emit("ledger_record_written", {"scope": bound_scope, "record_id": normalized["record_id"]}, state)
        if explicit_target is not None:
            self._set_status(
                explicit_target,
                bound_scope,
                "superseded",
                superseded_by=normalized["record_id"],
                supersede_reason="explicit_correction",
                superseded_at=self._now(),
            )
            self._emit(
                "ledger_explicit_correction_superseded",
                {
                    "scope": bound_scope,
                    "old_id": explicit_target["record_id"],
                    "new_id": normalized["record_id"],
                },
                state,
            )
        # Any other active peer retains the ordinary automatic lifecycle.
        # An explicit correction is authoritative only for its named target.
        self._apply_write_lifecycle(normalized, bound_scope, state)
        stored = self._backend.get(normalized["record_id"], scope=bound_scope)
        return self._public_record(stored or normalized)

    def get(self, record_id: str, scope: Mapping[str, Any], state: dict | None = None) -> dict[str, Any] | None:
        bound_scope = self._scope(scope)
        record = self._backend.get(record_id, scope=bound_scope)
        self._emit("ledger_record_get", {"scope": bound_scope, "record_id": record_id, "found": record is not None}, state)
        return self._public_record(record) if record is not None else None

    def search(
        self,
        scope: Mapping[str, Any],
        *,
        record_kind: str | None = None,
        classification: Mapping[str, str] | None = None,
        statuses: Sequence[str] | None = None,
        include_expired: bool = False,
        state: dict | None = None,
    ) -> list[dict[str, Any]]:
        bound_scope = self._scope(scope)
        selected_statuses = set(statuses or ("active", "disputed"))
        if include_expired:
            selected_statuses.add("expired")
        if not selected_statuses <= _STATUSES:
            raise LedgerValidationError("ledger_status_filter_invalid")
        if record_kind is not None and record_kind not in self._config.record_kinds:
            raise LedgerValidationError("ledger_record_kind_invalid")
        if classification is not None and not isinstance(classification, Mapping):
            raise LedgerValidationError("ledger_classification_filter_invalid")

        rows = []
        for record in self._backend.query(scope=bound_scope):
            if record["status"] not in selected_statuses:
                continue
            if record_kind is not None and record["record_kind"] != record_kind:
                continue
            if classification is not None and any(record["classification"].get(k) != v for k, v in classification.items()):
                continue
            rows.append(self._public_record(record))
        rows.sort(key=lambda row: (row["recorded_at"], row["record_id"]))
        self._emit("ledger_records_searched", {"scope": bound_scope, "result_count": len(rows)}, state)
        return rows

    def resolve_body(self, record_id: str, scope: Mapping[str, Any], state: dict | None = None) -> str | None:
        """Resolve raw body only through an explicit, audited service call."""

        bound_scope = self._scope(scope)
        record = self._backend.get(record_id, scope=bound_scope)
        self._emit("ledger_body_resolved", {"scope": bound_scope, "record_id": record_id, "found": record is not None}, state)
        return record.get("body_raw") if record is not None else None

    def export_audit(
        self, record_id: str, scope: Mapping[str, Any], state: dict | None = None
    ) -> dict[str, Any] | None:
        """Return PII-masked audit text without exposing physically retained raw text."""

        bound_scope = self._scope(scope)
        record = self._backend.get(record_id, scope=bound_scope)
        self._emit("ledger_audit_exported", {"scope": bound_scope, "record_id": record_id, "found": record is not None}, state)
        return self._audit_record(record) if record is not None else None

    def supersede(
        self, old_id: str, new_id: str, reason: str, scope: Mapping[str, Any], state: dict | None = None
    ) -> bool:
        bound_scope = self._scope(scope)
        if old_id == new_id or not _valid_structured_string(reason, self._config.max_scalar_chars):
            raise LedgerValidationError("ledger_supersede_invalid")
        old = self._backend.get(old_id, scope=bound_scope)
        new = self._backend.get(new_id, scope=bound_scope)
        if old is None or new is None:
            self._emit("ledger_record_superseded", {"scope": bound_scope, "old_id": old_id, "new_id": new_id, "ok": False}, state)
            return False
        self._set_status(
            old,
            bound_scope,
            "superseded",
            superseded_by=new_id,
            supersede_reason=reason,
            superseded_at=self._now(),
        )
        self._emit("ledger_record_superseded", {"scope": bound_scope, "old_id": old_id, "new_id": new_id, "ok": True}, state)
        return True

    def un_supersede(self, record_id: str, scope: Mapping[str, Any], state: dict | None = None) -> bool:
        bound_scope = self._scope(scope)
        record = self._backend.get(record_id, scope=bound_scope)
        if record is None or record.get("status") != "superseded":
            self._emit("ledger_record_un_superseded", {"scope": bound_scope, "record_id": record_id, "ok": False}, state)
            return False
        self._set_status(
            record,
            bound_scope,
            "active",
            superseded_by=None,
            supersede_reason=None,
            superseded_at=None,
        )
        self._emit("ledger_record_un_superseded", {"scope": bound_scope, "record_id": record_id, "ok": True}, state)
        return True

    def dispute(self, record_id: str, reason: str, scope: Mapping[str, Any], state: dict | None = None) -> bool:
        bound_scope = self._scope(scope)
        if not _valid_structured_string(reason, self._config.max_scalar_chars):
            raise LedgerValidationError("ledger_dispute_reason_required")
        record = self._backend.get(record_id, scope=bound_scope)
        if record is None:
            self._emit("ledger_record_disputed", {"scope": bound_scope, "record_id": record_id, "ok": False}, state)
            return False
        self._set_status(record, bound_scope, "disputed", dispute_reason=reason)
        self._emit("ledger_record_disputed", {"scope": bound_scope, "record_id": record_id, "ok": True}, state)
        return True

    def expire_due(self, scope: Mapping[str, Any], now: str, state: dict | None = None) -> int:
        bound_scope = self._scope(scope)
        cutoff = _valid_iso(now, "expire_due_now")
        expired = 0
        for record in self._backend.query(scope=bound_scope):
            valid_to = record.get("valid_to")
            if record.get("status") != "active" or valid_to is None or _parse_iso(valid_to) >= _parse_iso(cutoff):
                continue
            self._set_status(record, bound_scope, "expired")
            expired += 1
        self._emit("ledger_records_expired", {"scope": bound_scope, "expired_count": expired, "now": cutoff}, state)
        return expired

    def conflicts(self, scope: Mapping[str, Any], state: dict | None = None) -> list[dict[str, Any]]:
        """Return deterministic unresolved conflict groups, not an LLM judgment."""

        bound_scope = self._scope(scope)
        groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
        for record in self._backend.query(scope=bound_scope):
            if record.get("status") in {"active", "disputed"}:
                groups[self._conflict_key(record)].append(record)
        conflicts = []
        for key, records in groups.items():
            verdicts = sorted({record["verdict_code"] for record in records})
            if len(verdicts) > 1:
                conflicts.append(
                    {
                        "conflict_key": _display_conflict_key(key),
                        "record_ids": sorted(record["record_id"] for record in records),
                        "verdict_codes": verdicts,
                        "records": [
                            {"record_id": record["record_id"], "status": record["status"]}
                            for record in sorted(records, key=lambda record: record["record_id"])
                        ],
                    }
                )
        conflicts.sort(key=lambda item: (tuple(item["record_ids"]), tuple(item["verdict_codes"])))
        self._emit("ledger_conflicts_queried", {"scope": bound_scope, "conflict_count": len(conflicts)}, state)
        return conflicts

    def as_of(
        self,
        scope: Mapping[str, Any],
        timestamp: str,
        *,
        record_kind: str | None = None,
        state: dict | None = None,
    ) -> list[dict[str, Any]]:
        """Reproduce the ledger knowledge available at ``timestamp``."""

        bound_scope = self._scope(scope)
        as_of = _valid_iso(timestamp, "as_of_timestamp")
        if record_kind is not None and record_kind not in self._config.record_kinds:
            raise LedgerValidationError("ledger_record_kind_invalid")
        rows = []
        for record in self._backend.query(scope=bound_scope):
            if _parse_iso(record["recorded_at"]) > _parse_iso(as_of):
                continue
            if record_kind is not None and record["record_kind"] != record_kind:
                continue
            projected = self._public_record(record)
            projected["status"] = _status_at(record, as_of)
            rows.append(projected)
        rows.sort(key=lambda row: (row["recorded_at"], row["record_id"]))
        self._emit("ledger_as_of_queried", {"scope": bound_scope, "as_of": as_of, "result_count": len(rows)}, state)
        return rows

    def redact(self, record_id: str, scope: Mapping[str, Any], state: dict | None = None) -> bool:
        bound_scope = self._scope(scope)
        record = self._backend.get(record_id, scope=bound_scope)
        if record is None:
            self._emit("ledger_record_redacted", {"scope": bound_scope, "record_id": record_id, "ok": False}, state)
            return False
        self._backend.update(
            record_id,
            scope=bound_scope,
            changes={**{field: None for field in self._raw_text_fields()}, "redacted_at": self._now()},
        )
        self._emit("ledger_record_redacted", {"scope": bound_scope, "record_id": record_id, "ok": True}, state)
        return True

    def purge_expired_raw(self, scope: Mapping[str, Any], cutoff: str, state: dict | None = None) -> int:
        bound_scope = self._scope(scope)
        cutoff = _valid_iso(cutoff, "purge_cutoff")
        record_ids = self._backend.purge_raw(scope=bound_scope, cutoff=cutoff)
        purged_at = self._now()
        for record_id in record_ids:
            self._backend.update(record_id, scope=bound_scope, changes={"retention_purged_at": purged_at})
        self._emit("ledger_raw_retention_purged", {"scope": bound_scope, "purged_count": len(record_ids)}, state)
        return len(record_ids)

    def _normalize_new_record(self, scope: dict[str, str], record: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(record, Mapping):
            raise LedgerValidationError("ledger_record_must_be_mapping")
        provided = dict(record)
        forbidden = sorted(_SERVICE_OWNED_KEYS & set(provided))
        if forbidden:
            raise LedgerValidationError(f"ledger_service_owned_fields_forbidden:{','.join(forbidden)}")
        for owned_field in ("body_audit", "body_safe", "rationale_audit", "rationale_safe"):
            if owned_field in provided:
                raise LedgerValidationError(f"ledger_service_owned_field_forbidden:{owned_field}")
        body = _input_text(provided, "body")
        rationale = _input_text(provided, "rationale")
        body_values = sanitize_text(body, field_name="body", max_chars=self._config.max_body_chars)
        rationale_values = sanitize_text(rationale, field_name="rationale", max_chars=self._config.max_rationale_chars)

        record_kind = provided.pop("record_kind", None)
        if record_kind not in self._config.record_kinds:
            raise LedgerValidationError("ledger_record_kind_invalid")
        classification = _validate_classification(provided.pop("classification", None), self._config.classification_codes)
        occurred_at = _valid_iso(provided.pop("occurred_at", None), "occurred_at")
        valid_to_value = provided.pop("valid_to", None)
        valid_to = _valid_iso(valid_to_value, "valid_to") if valid_to_value is not None else None
        if valid_to is not None and _parse_iso(valid_to) < _parse_iso(occurred_at):
            raise LedgerValidationError("valid_to_before_occurred_at")
        verdict_code = provided.pop("verdict_code", None)
        if verdict_code not in self._config.verdict_codes:
            raise LedgerValidationError("ledger_verdict_code_invalid")
        decided_by = _required_structured_string(
            provided.pop("decided_by", None), "decided_by", self._config.max_scalar_chars
        )
        decided_at = _valid_iso(provided.pop("decided_at", None), "decided_at")
        decision_source = provided.pop("decision_source", None)
        if decision_source not in _DECISION_SOURCES:
            raise LedgerValidationError("ledger_decision_source_invalid")
        retrieval_text = sanitize_retrieval_text(
            provided.pop("retrieval_text", None), max_chars=self._config.max_retrieval_chars
        )
        _reject_retrieval_text_content(retrieval_text, verdict_code, body_values, rationale_values)
        provenance = _validate_provenance(provided.pop("provenance", None), self._config.max_body_chars)
        ai_suggested = provided.pop("ai_suggested_verdict", None)
        if ai_suggested is not None and ai_suggested not in self._config.verdict_codes:
            raise LedgerValidationError("ledger_ai_suggested_verdict_invalid")
        operator_edited = provided.pop("operator_edited", False)
        if not isinstance(operator_edited, bool):
            raise LedgerValidationError("ledger_operator_edited_invalid")

        standards = {}
        for standard_field in ("standard_ref", "standard_version", "standard_hash"):
            value = provided.pop(standard_field, None)
            if value is not None and not _valid_structured_string(value, self._config.max_scalar_chars):
                raise LedgerValidationError(f"{standard_field}_invalid")
            standards[standard_field] = value
        supersedes_record_id = provided.pop("supersedes_record_id", None)
        if supersedes_record_id is not None and not _valid_structured_string(
            supersedes_record_id, self._config.max_scalar_chars
        ):
            raise LedgerValidationError("ledger_explicit_correction_target_invalid")
        domain_values = _normalize_domain_fields(
            provided,
            self._config.domain_fields,
            max_text_chars=self._config.max_body_chars,
            max_scalar_chars=self._config.max_scalar_chars,
        )
        for key_field in self._config.conflict_key_fields:
            if domain_values.get(key_field) is None:
                raise LedgerValidationError(f"ledger_conflict_key_field_invalid:{key_field}")

        record_id = _mint_record_id(self._config.record_id_prefix)
        recorded_at = self._now()
        normalized = {
            **standards,
            **domain_values,
            "record_id": record_id,
            "schema_version": self._config.schema_version,
            "record_kind": record_kind,
            "scope": scope,
            "classification": classification,
            "occurred_at": occurred_at,
            "valid_to": valid_to,
            "recorded_at": recorded_at,
            "body_raw": body_values.raw if body_values else None,
            "body_audit": body_values.audit if body_values else None,
            "body_safe": body_values.safe if body_values else None,
            "retrieval_text": retrieval_text,
            "verdict_code": verdict_code,
            "rationale_raw": rationale_values.raw if rationale_values else None,
            "rationale_audit": rationale_values.audit if rationale_values else None,
            "rationale_safe": rationale_values.safe if rationale_values else None,
            "decided_by": decided_by,
            "decided_at": decided_at,
            "decision_source": decision_source,
            "provenance": provenance,
            "supersedes_record_id": supersedes_record_id,
            "status": "active",
            "superseded_by": None,
            "supersede_reason": None,
            "superseded_at": None,
            "ai_suggested_verdict": ai_suggested,
            "operator_edited": operator_edited,
            "redacted_at": None,
            "retention_purged_at": None,
            "status_history": [{"status": "active", "at": recorded_at}],
        }
        return normalized

    def _apply_write_lifecycle(
        self, new_record: Mapping[str, Any], scope: Mapping[str, str], state: dict | None
    ) -> None:
        peers = [
            record
            for record in self._backend.query(scope=scope)
            if record["record_id"] != new_record["record_id"]
            and record.get("status") == "active"
            and self._conflict_key(record) == self._conflict_key(new_record)
        ]
        symmetric_conflicts = [record for record in peers if self._is_symmetric_dispute(record, new_record)]
        if symmetric_conflicts:
            reason = "automatic_lifecycle_ambiguous"
            # Equal ingestion time and conflicting human decisions are symmetric:
            # automation cannot choose either record, so both are retained as
            # searchable disputed evidence.
            for old in symmetric_conflicts:
                self._set_status(old, scope, "disputed", dispute_reason=reason)
            self._set_status(dict(new_record), scope, "disputed", dispute_reason=reason)
            self._emit(
                "ledger_auto_disputed",
                {
                    "scope": dict(scope),
                    "old_ids": sorted(record["record_id"] for record in symmetric_conflicts),
                    "new_id": new_record["record_id"],
                },
                state,
            )
            return
        for old in peers:
            if self._standard_version_changed(old, new_record):
                # A standard update is asymmetric: the old decision needs
                # re-evaluation, while the newly recorded decision remains
                # active and immediately searchable.
                self._set_status(old, scope, "disputed", dispute_reason="standard_version_changed")
                self._emit(
                    "ledger_standard_version_disputed",
                    {"scope": dict(scope), "old_id": old["record_id"], "new_id": new_record["record_id"]},
                    state,
                )
            elif self._config.supersede_mode == "auto":
                self._set_status(
                    old,
                    scope,
                    "superseded",
                    superseded_by=new_record["record_id"],
                    supersede_reason="automatic_newer_record",
                    superseded_at=self._now(),
                )
                self._emit(
                    "ledger_record_superseded",
                    {"scope": dict(scope), "old_id": old["record_id"], "new_id": new_record["record_id"], "automatic": True},
                    state,
                )
            else:
                self._emit(
                    "ledger_supersede_review_required",
                    {"scope": dict(scope), "old_id": old["record_id"], "new_id": new_record["record_id"]},
                    state,
                )

    def _explicit_correction_target(
        self, record: Mapping[str, Any], scope: Mapping[str, str]
    ) -> dict[str, Any] | None:
        """Resolve a named correction target before inserting the new record."""

        target_id = record.get("supersedes_record_id")
        if target_id is None:
            return None
        if not _valid_structured_string(target_id, self._config.max_scalar_chars):
            raise LedgerValidationError("ledger_explicit_correction_target_invalid")
        target = self._backend.get(target_id, scope=scope)
        if target is None or target.get("status") not in {"active", "disputed"}:
            raise LedgerValidationError("ledger_explicit_correction_target_invalid")
        return target

    @staticmethod
    def _is_symmetric_dispute(old: Mapping[str, Any], new: Mapping[str, Any]) -> bool:
        if old["recorded_at"] == new["recorded_at"]:
            return True
        return old["decision_source"] == new["decision_source"] == "human" and old["verdict_code"] != new["verdict_code"]

    @staticmethod
    def _standard_version_changed(old: Mapping[str, Any], new: Mapping[str, Any]) -> bool:
        return old.get("standard_version") != new.get("standard_version") and (
            old.get("standard_version") is not None or new.get("standard_version") is not None
        )

    def _set_status(self, record: Mapping[str, Any], scope: Mapping[str, str], status: str, **changes: Any) -> None:
        if status not in _STATUSES:
            raise LedgerValidationError("ledger_status_invalid")
        history = list(record.get("status_history", []))
        history.append({"status": status, "at": self._now()})
        self._backend.update(record["record_id"], scope=scope, changes={"status": status, "status_history": history, **changes})

    def _conflict_key(self, record: Mapping[str, Any]) -> tuple[Any, ...]:
        return (
            tuple(sorted(dict(record["scope"]).items())),
            tuple(sorted(dict(record["classification"]).items())),
            tuple((field, _freeze(record[field])) for field in self._config.conflict_key_fields),
        )

    def _scope(self, scope: Mapping[str, Any]) -> dict[str, str]:
        return require_scope(scope, self._config.scope_keys)

    def _now(self) -> str:
        return _valid_iso(self._clock(), "ledger_clock")

    def _public_record(self, record: Mapping[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(dict(record))
        result.pop("body_raw", None)
        result.pop("body_audit", None)
        result.pop("rationale_raw", None)
        result.pop("rationale_audit", None)
        for domain_field, kind in self._config.domain_fields.items():
            if kind == "text":
                result.pop(f"{domain_field}_raw", None)
                result.pop(f"{domain_field}_audit", None)
        result.pop("status_history", None)
        return result

    def _audit_record(self, record: Mapping[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(dict(record))
        result.pop("body_raw", None)
        result.pop("rationale_raw", None)
        result.pop("body_safe", None)
        result.pop("rationale_safe", None)
        for domain_field, kind in self._config.domain_fields.items():
            if kind == "text":
                result.pop(f"{domain_field}_raw", None)
                result.pop(f"{domain_field}_safe", None)
        result.pop("status_history", None)
        return result

    def _raw_text_fields(self) -> tuple[str, ...]:
        return ("body_raw", "rationale_raw") + tuple(
            f"{field}_raw" for field, kind in self._config.domain_fields.items() if kind == "text"
        )

    @staticmethod
    def _emit(event: str, payload: Mapping[str, Any], state: dict | None = None) -> None:
        emit_trace_event(event, dict(payload), state or {})


def _finite_strings(values: Any, name: str) -> frozenset[str]:
    if not isinstance(values, (set, frozenset, tuple, list)):
        raise ValueError(f"LedgerConfig: {name} must be a finite sequence of strings")
    result = frozenset(values)
    if not result or any(not _clean_text(value) for value in result):
        raise ValueError(f"LedgerConfig: {name} must contain non-empty clean strings")
    return result


def _classification_codes(values: Any) -> dict[str, frozenset[str]]:
    if not isinstance(values, Mapping):
        raise ValueError("LedgerConfig: classification_codes must be a mapping of finite code sets")
    result = {}
    for key, codes in values.items():
        if not isinstance(key, str) or not key.isidentifier():
            raise ValueError("LedgerConfig: classification code names must be identifiers")
        result[key] = _finite_strings(codes, f"classification_codes[{key}]")
    return result


def _domain_fields(values: Any) -> dict[str, str]:
    if not isinstance(values, Mapping):
        raise ValueError("LedgerConfig: domain_fields must be a mapping of field names to scalar/text")
    result: dict[str, str] = {}
    for domain_field, kind in values.items():
        if not isinstance(domain_field, str) or not domain_field.isidentifier():
            raise ValueError("LedgerConfig: domain field names must be identifier strings")
        if kind not in {"scalar", "text"}:
            raise ValueError("LedgerConfig: domain field kinds must be scalar or text")
        result[domain_field] = kind
    return result


def _conflict_key_fields(values: Any) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise ValueError("LedgerConfig: conflict_key_fields must be a sequence of domain field names")
    fields = tuple(values)
    if len(fields) != len(set(fields)) or any(not isinstance(field, str) or not field.isidentifier() for field in fields):
        raise ValueError("LedgerConfig: conflict_key_fields must be unique identifier strings")
    return fields


def _validate_classification(value: Any, codes: Mapping[str, frozenset[str]]) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != set(codes):
        raise LedgerValidationError("ledger_classification_invalid")
    result = {}
    for key, allowed in codes.items():
        item = value.get(key)
        if item not in allowed:
            raise LedgerValidationError(f"ledger_classification_invalid:{key}")
        result[key] = item
    return result


def _normalize_domain_fields(
    provided: dict[str, Any],
    declarations: Mapping[str, str],
    *,
    max_text_chars: int,
    max_scalar_chars: int,
) -> dict[str, Any]:
    """Process declared extensions and gate every untyped extension value."""

    normalized: dict[str, Any] = {}
    for domain_field, kind in declarations.items():
        if kind == "scalar":
            value = provided.pop(domain_field, None)
            if value is not None and not _valid_structured_scalar(value, max_scalar_chars):
                raise LedgerValidationError(f"ledger_domain_field_invalid:{domain_field}")
            normalized[domain_field] = copy.deepcopy(value)
            continue
        for suffix in ("_raw", "_audit", "_safe"):
            if f"{domain_field}{suffix}" in provided:
                raise LedgerValidationError(f"ledger_domain_field_service_owned:{domain_field}{suffix}")
        value = provided.pop(domain_field, None)
        sanitized = sanitize_text(value, field_name=domain_field, max_chars=max_text_chars)
        normalized[f"{domain_field}_raw"] = sanitized.raw if sanitized else None
        normalized[f"{domain_field}_audit"] = sanitized.audit if sanitized else None
        normalized[f"{domain_field}_safe"] = sanitized.safe if sanitized else None
    for extra_field, value in provided.items():
        normalized[extra_field] = _sanitize_untyped_value(value, field_name=extra_field, max_chars=max_text_chars)
    return normalized


def _validate_provenance(value: Any, max_chars: int) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise LedgerValidationError("ledger_provenance_required")
    return _sanitize_untyped_value(value, field_name="provenance", max_chars=max_chars)


def _sanitize_untyped_value(value: Any, *, field_name: str, max_chars: int) -> Any:
    """Gate every undeclared public value without imposing a closed schema."""

    if isinstance(value, str):
        sanitized = sanitize_text(value, field_name=field_name, max_chars=max_chars)
        return sanitized.safe if sanitized is not None else None
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise LedgerValidationError(f"{field_name}_invalid_scalar")
        return value
    if isinstance(value, Mapping):
        result: dict[Any, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise LedgerValidationError(f"{field_name}_invalid_mapping_key")
            result[key] = _sanitize_untyped_value(item, field_name=field_name, max_chars=max_chars)
        return result
    if isinstance(value, list):
        return [_sanitize_untyped_value(item, field_name=field_name, max_chars=max_chars) for item in value]
    raise LedgerValidationError(f"{field_name}_invalid_value")


def _input_text(record: dict[str, Any], name: str) -> str | None:
    raw_name = f"{name}_raw"
    if name in record and raw_name in record:
        raise LedgerValidationError(f"{name}_provided_twice")
    value = record.pop(name, record.pop(raw_name, None))
    return value


def _required_structured_string(value: Any, field: str, max_chars: int) -> str:
    if not _valid_structured_string(value, max_chars):
        raise LedgerValidationError(f"{field}_required")
    return value


def _clean_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value == value.strip() and "\n" not in value and "\r" not in value


def _valid_structured_string(value: Any, max_chars: int) -> bool:
    return _clean_text(value) and len(value) <= max_chars


def _valid_structured_scalar(value: Any, max_chars: int) -> bool:
    if isinstance(value, str):
        return _valid_structured_string(value, max_chars)
    if isinstance(value, bool) or isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _reject_retrieval_text_content(
    retrieval_text: str,
    verdict_code: str,
    body: Any,
    rationale: Any,
) -> None:
    """Keep conclusions and free-text reasons out of the index field."""

    # Verdict codes are often short (go/ok/ng).  Reject only an independent
    # token, never an incidental substring such as ``cargo`` or ``no_go``.
    verdict_present = bool(
        re.search(rf"(?<![0-9A-Za-z_]){re.escape(verdict_code)}(?![0-9A-Za-z_])", retrieval_text, re.IGNORECASE)
    )
    free_text_present = any(
        value and value.safe.casefold() in retrieval_text.casefold() for value in (body, rationale)
    )
    if verdict_present or free_text_present:
        raise LedgerValidationError("retrieval_text_must_not_include_verdict_or_free_text")


def _valid_iso(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LedgerValidationError(f"{field}_iso_required")
    _parse_iso(value)
    return value


def _parse_iso(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LedgerValidationError("ledger_timestamp_invalid") from exc
    if parsed.tzinfo is None:
        raise LedgerValidationError("ledger_timestamp_timezone_required")
    return parsed


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _mint_record_id(prefix: str) -> str:
    """Mint an ASCII-letter prefix plus RFC 4648 base32, never UUID hex."""

    token = base64.b32encode(token_bytes(16)).decode("ascii").rstrip("=").lower()
    return f"{prefix}_{token}"


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return tuple(sorted((str(key), _freeze(item)) for key, item in value.items()))
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _display_conflict_key(key: tuple[Any, ...]) -> dict[str, dict[str, Any]]:
    scope, classification, fields = key
    return {
        "scope": dict(scope),
        "classification": dict(classification),
        "fields": dict(fields),
    }


def _status_at(record: Mapping[str, Any], timestamp: str) -> str:
    current = "active"
    for item in record.get("status_history", []):
        if isinstance(item, Mapping) and isinstance(item.get("at"), str) and _parse_iso(item["at"]) <= _parse_iso(timestamp):
            current = item.get("status", current)
    return current
