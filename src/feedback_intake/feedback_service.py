# PART: feedback-intake v0.2.1 (parts@09b50d1)
"""Feedback write service for an accumulation-ledger.

This module deliberately depends on a small ledger Protocol rather than
importing a concrete ledger package.  A copied template wires its local P1
``LedgerService`` instance in, while feedback-intake owns ownership checks,
idempotency, feedback lineage, and audit events.
"""

from __future__ import annotations

import copy
import threading
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Any, Protocol

from shared.utils.audit_logger import emit_trace_event


class LedgerService(Protocol):
    """P1 surface required by feedback-intake; no concrete package import."""

    def get(self, record_id: str, scope: Mapping[str, Any], state: dict | None = None) -> dict[str, Any] | None: ...

    def write(
        self, scope: Mapping[str, Any], record: Mapping[str, Any], state: dict | None = None
    ) -> dict[str, Any]: ...


class FeedbackReceiptStore(Protocol):
    """Durable idempotency boundary for ``(scope, record_id, feedback_seq)``.

    Production implementations must execute ``create`` in the same unique-key
    transaction/critical section as the receipt write.  This prevents two
    concurrent retries from producing two ledger records.
    """

    def execute_once(
        self,
        *,
        scope: Mapping[str, str],
        record_id: str,
        feedback_seq: str,
        create: Callable[[], dict[str, Any]],
    ) -> tuple[dict[str, Any], bool]: ...


class FeedbackRejectedError(ValueError):
    """A fail-closed feedback rejection with a safe, caller-visible code."""


class InMemoryFeedbackReceiptStore:
    """Test/STG idempotency adapter; mutable receipts live in this backend.

    The lookup, the ``create()`` call, and the store are one critical section
    per key. Without that, two concurrent requests for the same key both see no
    receipt, both run ``create()``, and the ledger takes a duplicate write —
    which makes feedback verification and ledger counts non-deterministic.

    ``create()`` runs while its key's lock is held, so a slow ledger write
    serialises same-key callers. Different keys never block each other.

    A durable backend must provide the same guarantee with a transaction or a
    conditional insert; this adapter is not a substitute for one.
    """

    def __init__(self) -> None:
        self._receipts: dict[tuple[tuple[tuple[str, str], ...], str, str], dict[str, Any]] = {}
        self._locks: dict[tuple[tuple[tuple[str, str], ...], str, str], threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def execute_once(
        self,
        *,
        scope: Mapping[str, str],
        record_id: str,
        feedback_seq: str,
        create: Callable[[], dict[str, Any]],
    ) -> tuple[dict[str, Any], bool]:
        key = (tuple(sorted(scope.items())), record_id, feedback_seq)
        with self._key_lock(key):
            existing = self._receipts.get(key)
            if existing is not None:
                return copy.deepcopy(existing), False
            # create() が失敗したら receipt を残さない。残すと、以後の再試行が
            # created=False で「成功済み」に見え、ledger には何も書かれないまま
            # 永久に回復できなくなる。
            result = create()
            self._receipts[key] = copy.deepcopy(result)
            return copy.deepcopy(result), True

    def _key_lock(self, key) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock


class FeedbackIntakeService:
    """Accept one human feedback decision as a new ledger record.

    ``operator_edited`` is deliberately ``False`` when the source has no AI
    suggestion: no comparison exists, so it must not be counted as an edit.
    """

    def __init__(
        self,
        *,
        ledger: LedgerService,
        receipt_store: FeedbackReceiptStore,
        domain_fields: Mapping[str, str] | None = None,
    ) -> None:
        self._ledger = ledger
        self._receipt_store = receipt_store
        self._domain_fields = MappingProxyType(_validate_domain_fields(domain_fields or {}))

    def submit(
        self,
        *,
        scope: Mapping[str, Any],
        record_id: str,
        feedback_seq: str,
        verdict_code: str,
        rationale: str,
        decided_by: str,
        decided_at: str,
        state: dict | None = None,
    ) -> dict[str, Any]:
        """Record human feedback once, returning the original result on retry."""

        if not _clean_string(record_id) or not _clean_string(feedback_seq):
            return self._reject(scope, record_id, "feedback_request_invalid", state)

        try:
            source = self._ledger.get(record_id, scope, state=state)
        except ValueError:
            return self._reject(scope, record_id, "feedback_scope_invalid", state)
        if source is None:
            # P1's scoped get intentionally makes a missing record and a
            # cross-partition record indistinguishable; neither leaks owner data.
            return self._reject(scope, record_id, "feedback_target_not_owned", state)

        bound_scope = source.get("scope")
        if not isinstance(bound_scope, Mapping) or dict(bound_scope) != dict(scope):
            return self._reject(scope, record_id, "feedback_target_not_owned", state)

        def create() -> dict[str, Any]:
            candidate = _feedback_record(
                source,
                source_record_id=record_id,
                feedback_seq=feedback_seq,
                verdict_code=verdict_code,
                rationale=rationale,
                decided_by=decided_by,
                decided_at=decided_at,
                domain_fields=self._domain_fields,
            )
            try:
                written = self._ledger.write(scope, candidate, state=state)
            except ValueError as exc:
                reason = "feedback_verdict_rejected" if "verdict" in str(exc) else "feedback_record_rejected"
                raise FeedbackRejectedError(reason) from exc
            return {
                "record_id": written["record_id"],
                "source_record_id": record_id,
                "feedback_seq": feedback_seq,
                "status": "accepted",
                "operator_edited": candidate["operator_edited"],
            }

        try:
            result, created = self._receipt_store.execute_once(
                scope={key: value for key, value in dict(scope).items()},
                record_id=record_id,
                feedback_seq=feedback_seq,
                create=create,
            )
        except FeedbackRejectedError as exc:
            return self._reject(scope, record_id, str(exc), state)

        event = "feedback_accepted" if created else "feedback_idempotency_hit"
        self._emit(
            event,
            {
                "scope": dict(scope),
                "record_id": record_id,
                "feedback_seq": feedback_seq,
                "result_record_id": result["record_id"],
            },
            state,
        )
        return result

    @staticmethod
    def _emit(event: str, payload: Mapping[str, Any], state: dict | None) -> None:
        emit_trace_event(event, dict(payload), state or {})

    def _reject(
        self, scope: Mapping[str, Any], record_id: Any, reason: str, state: dict | None
    ) -> None:
        self._emit(
            "feedback_rejected",
            {"scope": dict(scope) if isinstance(scope, Mapping) else {}, "record_id": record_id, "reason": reason},
            state,
        )
        raise FeedbackRejectedError(reason)


_SOURCE_NONCOPY_FIELDS = frozenset(
    {
        "record_id",
        "schema_version",
        "scope",
        "recorded_at",
        "body_safe",
        "rationale_safe",
        "status",
        "superseded_by",
        "supersede_reason",
        "superseded_at",
        "redacted_at",
        "retention_purged_at",
        "dispute_reason",
        "supersedes_record_id",
        "verdict_code",
        "decided_by",
        "decided_at",
        "decision_source",
        "operator_edited",
    }
)


def _feedback_record(
    source: Mapping[str, Any],
    *,
    source_record_id: str,
    feedback_seq: str,
    verdict_code: str,
    rationale: str,
    decided_by: str,
    decided_at: str,
    domain_fields: Mapping[str, str],
) -> dict[str, Any]:
    """Copy structural/domain attributes while never reusing source free text."""

    record = {
        key: copy.deepcopy(value)
        for key, value in source.items()
        if key not in _SOURCE_NONCOPY_FIELDS and not _declared_text_safe_field(key, domain_fields)
    }
    suggested = record.get("ai_suggested_verdict")
    provenance = record.get("provenance")
    record["provenance"] = {
        **(dict(provenance) if isinstance(provenance, Mapping) else {}),
        "feedback_source_record_id": source_record_id,
        "feedback_seq": feedback_seq,
    }
    record.update(
        {
            # A feedback decision is an explicit human correction of the
            # named source. P1 therefore supersedes that record even when
            # both decisions have decision_source="human".
            "supersedes_record_id": source_record_id,
            "verdict_code": verdict_code,
            "rationale": rationale,
            "decided_by": decided_by,
            "decided_at": decided_at,
            "decision_source": "human",
            # No suggestion means no comparison was possible, not an edit.
            "operator_edited": suggested is not None and verdict_code != suggested,
        }
    )
    return record


def _clean_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value == value.strip() and "\n" not in value and "\r" not in value


def _validate_domain_fields(values: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(values, Mapping):
        raise ValueError("FeedbackIntakeService: domain_fields must match LedgerConfig.domain_fields")
    result = {}
    for field, kind in values.items():
        if not isinstance(field, str) or not field.isidentifier() or kind not in {"scalar", "text"}:
            raise ValueError("FeedbackIntakeService: domain_fields must contain identifier scalar/text declarations")
        result[field] = kind
    return result


def _declared_text_safe_field(key: str, domain_fields: Mapping[str, str]) -> bool:
    return key.endswith("_safe") and domain_fields.get(key[: -len("_safe")]) == "text"
