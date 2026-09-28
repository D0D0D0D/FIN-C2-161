from __future__ import annotations

import pytest

from accumulation_ledger import InMemoryLedgerBackend, LedgerConfig, LedgerService
import feedback_intake.feedback_service as feedback_module
from feedback_intake import FeedbackIntakeService, FeedbackRejectedError, InMemoryFeedbackReceiptStore


SCOPE = {"company_id": "co-1", "case_id": "case-1"}
OTHER_SCOPE = {"company_id": "co-1", "case_id": "case-2"}


class Clock:
    def __init__(self):
        self._index = 0

    def __call__(self):
        value = f"2026-07-30T00:00:{self._index:02d}+00:00"
        self._index += 1
        return value


def make_ledger(*, backend=None):
    return LedgerService(
        LedgerConfig(
            scope_keys=("company_id", "case_id"),
            record_kinds=frozenset({"finding"}),
            classification_codes={"severity": frozenset({"low", "high"})},
            verdict_codes=frozenset({"confirmed", "dismissed"}),
            conflict_key_fields=("finding_key",),
            domain_fields={"finding_key": "scalar"},
            record_id_prefix="finding",
        ),
        backend=backend,
        clock=Clock(),
    )


def make_ledger_with_text_domain(*, backend=None):
    return LedgerService(
        LedgerConfig(
            scope_keys=("company_id", "case_id"),
            record_kinds=frozenset({"finding"}),
            classification_codes={"severity": frozenset({"low", "high"})},
            verdict_codes=frozenset({"confirmed", "dismissed"}),
            conflict_key_fields=("finding_key",),
            domain_fields={"finding_key": "scalar", "operator_note": "text"},
            record_id_prefix="finding",
        ),
        backend=backend,
        clock=Clock(),
    )


def make_intake_service(ledger):
    return FeedbackIntakeService(
        ledger=ledger,
        receipt_store=InMemoryFeedbackReceiptStore(),
        domain_fields={"finding_key": "scalar"},
    )


def source_record(**overrides):
    record = {
        "record_kind": "finding",
        "classification": {"severity": "high"},
        "finding_key": "guardrail-zone-a",
        "occurred_at": "2026-07-29T09:00:00+00:00",
        "valid_to": None,
        "body": "点検で手すりの固定不足を確認した",
        "retrieval_text": "guardrail zone-a severity-high",
        "verdict_code": "confirmed",
        "rationale": "AI の一次判定",
        "decided_by": "inspection-ai",
        "decided_at": "2026-07-29T09:05:00+00:00",
        "decision_source": "ai",
        "standard_ref": None,
        "standard_version": None,
        "standard_hash": None,
        "provenance": {"doc_ref": "inspection-1", "span_ref": "row-4"},
        "ai_suggested_verdict": "confirmed",
    }
    record.update(overrides)
    return record


def submit(service, source_id, *, scope=SCOPE, feedback_seq="seq-1", verdict_code="dismissed", **overrides):
    state = overrides.pop("state", None)
    value = {
        "record_id": source_id,
        "feedback_seq": feedback_seq,
        "verdict_code": verdict_code,
        "rationale": "現場責任者が写真を再確認した",
        "decided_by": "safety-operator",
        "decided_at": "2026-07-30T08:00:00+00:00",
    }
    value.update(overrides)
    return service.submit(scope=scope, state=state, **value)


def test_human_feedback_writes_a_new_ledger_record_with_three_value_rationale():
    backend = InMemoryLedgerBackend()
    ledger = make_ledger(backend=backend)
    source = ledger.write(SCOPE, source_record())
    service = make_intake_service(ledger)

    result = submit(service, source["record_id"])

    assert result["source_record_id"] == source["record_id"]
    assert result["status"] == "accepted"
    assert result["operator_edited"] is True
    assert result["record_id"] != source["record_id"]
    assert ledger.get(source["record_id"], SCOPE)["status"] == "superseded"
    stored = backend.get(result["record_id"], scope=SCOPE)
    assert stored["decision_source"] == "human"
    assert stored["rationale_raw"] == "現場責任者が写真を再確認した"
    assert stored["rationale_audit"] == "現場責任者が写真を再確認した"
    assert stored["rationale_safe"] == "現場責任者が写真を再確認した"
    assert stored["provenance"]["feedback_source_record_id"] == source["record_id"]
    assert stored["provenance"]["feedback_seq"] == "seq-1"


def test_human_feedback_explicitly_corrects_a_prior_human_record_instead_of_disputing_both():
    backend = InMemoryLedgerBackend()
    ledger = make_ledger(backend=backend)
    source = ledger.write(SCOPE, source_record(decision_source="human", ai_suggested_verdict=None))
    service = make_intake_service(ledger)

    result = submit(service, source["record_id"])

    assert ledger.get(source["record_id"], SCOPE)["status"] == "superseded"
    assert ledger.get(result["record_id"], SCOPE)["status"] == "active"
    assert [row["record_id"] for row in ledger.search(SCOPE, statuses=("active",))] == [result["record_id"]]
    assert backend.get(result["record_id"], scope=SCOPE)["supersedes_record_id"] == source["record_id"]


def test_feedback_without_ai_suggestion_sets_operator_edited_false_explicitly():
    ledger = make_ledger()
    source = ledger.write(SCOPE, source_record(ai_suggested_verdict=None))
    service = make_intake_service(ledger)

    result = submit(service, source["record_id"])

    assert result["operator_edited"] is False


def test_feedback_rationale_uses_ledger_three_value_pii_separation():
    backend = InMemoryLedgerBackend()
    ledger = make_ledger(backend=backend)
    source = ledger.write(SCOPE, source_record())
    service = make_intake_service(ledger)

    result = submit(service, source["record_id"], rationale="連絡先 090-1234-5678 を確認")

    stored = backend.get(result["record_id"], scope=SCOPE)
    assert stored["rationale_raw"] == "連絡先 090-1234-5678 を確認"
    assert stored["rationale_audit"] == "連絡先 [MASKED] を確認"
    assert stored["rationale_safe"] == "連絡先 [MASKED] を確認"


def test_feedback_copies_only_declared_scalar_domain_fields_not_source_text():
    backend = InMemoryLedgerBackend()
    ledger = make_ledger_with_text_domain(backend=backend)
    source = ledger.write(SCOPE, source_record(operator_note="source-only free text"))
    service = FeedbackIntakeService(
        ledger=ledger,
        receipt_store=InMemoryFeedbackReceiptStore(),
        domain_fields={"finding_key": "scalar", "operator_note": "text"},
    )

    result = submit(service, source["record_id"])

    stored = backend.get(result["record_id"], scope=SCOPE)
    assert stored["finding_key"] == "guardrail-zone-a"
    assert stored["operator_note_raw"] is None
    assert stored["operator_note_safe"] is None


def test_out_of_range_verdict_is_rejected_without_a_new_record_and_audited(monkeypatch):
    events = []
    monkeypatch.setattr(feedback_module, "emit_trace_event", lambda event, payload, state: events.append((event, payload, state)))
    backend = InMemoryLedgerBackend()
    ledger = make_ledger(backend=backend)
    source = ledger.write(SCOPE, source_record())
    service = make_intake_service(ledger)

    with pytest.raises(FeedbackRejectedError, match="feedback_verdict_rejected"):
        submit(service, source["record_id"], verdict_code="maybe", state={"correlation_id": "c-1"})

    assert len(backend.query(scope=SCOPE)) == 1
    assert events[-1] == (
        "feedback_rejected",
        {"scope": SCOPE, "record_id": source["record_id"], "reason": "feedback_verdict_rejected"},
        {"correlation_id": "c-1"},
    )


def test_other_scope_cannot_update_a_record_and_rejection_does_not_echo_owner_scope(monkeypatch):
    events = []
    monkeypatch.setattr(feedback_module, "emit_trace_event", lambda event, payload, state: events.append((event, payload, state)))
    ledger = make_ledger()
    source = ledger.write(SCOPE, source_record())
    service = make_intake_service(ledger)

    with pytest.raises(FeedbackRejectedError, match="feedback_target_not_owned"):
        submit(service, source["record_id"], scope=OTHER_SCOPE)

    assert events[-1][0] == "feedback_rejected"
    assert events[-1][1] == {"scope": OTHER_SCOPE, "record_id": source["record_id"], "reason": "feedback_target_not_owned"}
    assert SCOPE != OTHER_SCOPE


def test_same_source_and_feedback_seq_replay_returns_first_result_without_second_write_and_audits_hit(monkeypatch):
    events = []
    monkeypatch.setattr(feedback_module, "emit_trace_event", lambda event, payload, state: events.append((event, payload, state)))
    backend = InMemoryLedgerBackend()
    ledger = make_ledger(backend=backend)
    source = ledger.write(SCOPE, source_record())
    service = make_intake_service(ledger)
    state = {"correlation_id": "c-2"}

    first = submit(service, source["record_id"], state=state)
    replay = submit(
        service,
        source["record_id"],
        verdict_code="confirmed",
        rationale="replayed payload must not create a different result",
        state=state,
    )

    assert replay == first
    assert len(backend.query(scope=SCOPE)) == 2
    assert events[-1] == (
        "feedback_idempotency_hit",
        {"scope": SCOPE, "record_id": source["record_id"], "feedback_seq": "seq-1", "result_record_id": first["record_id"]},
        state,
    )


def test_acceptance_audit_event_propagates_state(monkeypatch):
    events = []
    state = {"correlation_id": "c-3"}
    monkeypatch.setattr(feedback_module, "emit_trace_event", lambda event, payload, trace_state: events.append((event, payload, trace_state)))
    ledger = make_ledger()
    source = ledger.write(SCOPE, source_record())
    service = make_intake_service(ledger)

    result = submit(service, source["record_id"], state=state)

    assert events[-1] == (
        "feedback_accepted",
        {"scope": SCOPE, "record_id": source["record_id"], "feedback_seq": "seq-1", "result_record_id": result["record_id"]},
        state,
    )
