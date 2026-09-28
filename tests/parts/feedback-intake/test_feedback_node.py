from __future__ import annotations

import json

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from feedback_intake import FeedbackIntakeNode, FeedbackRejectedError
import feedback_intake.feedback_node as node_module
import feedback_intake.feedback_service as service_module


class PayloadStore:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def resolve(self, ref, *, scope, session_id, consume=False):
        self.calls.append({"ref": ref, "scope": dict(scope), "session_id": session_id, "consume": consume})
        return self.payload if ref == "feedback-ref" else None


class Service:
    def __init__(self):
        self.calls = []

    def submit(self, **kwargs):
        self.calls.append(kwargs)
        return {"record_id": "finding_feedback", "source_record_id": kwargs["record_id"], "status": "accepted", "operator_edited": False}


class StrictService:
    def submit(self, *, scope, state, record_id, feedback_seq, verdict_code, rationale, decided_by, decided_at):
        return {"record_id": "finding_feedback", "source_record_id": record_id, "status": "accepted", "operator_edited": False}


def test_node_resolves_scoped_payload_and_never_returns_rationale_raw():
    payload = {
        "record_id": "finding_source",
        "feedback_seq": "seq-1",
        "verdict_code": "dismissed",
        "rationale": "operator private rationale",
        "decided_by": "operator-1",
        "decided_at": "2026-07-30T08:00:00+00:00",
    }
    store = PayloadStore(json.dumps(payload))
    service = Service()
    node = FeedbackIntakeNode(service=service, payload_store=store, scope_keys=("company_id", "case_id"))

    result = node.execute(
        {
            "feedback_payload_ref": "feedback-ref",
            "session_id": "session-1",
            "company_id": "co-1",
            "case_id": "case-1",
        }
    )

    assert result == {
        "feedback_result": {
            "record_id": "finding_feedback",
            "source_record_id": "finding_source",
            "status": "accepted",
            "operator_edited": False,
        },
        "status": AgentStatus.SUCCESS.value,
    }
    assert "operator private rationale" not in str(result)
    assert store.calls == [{"ref": "feedback-ref", "scope": {"company_id": "co-1", "case_id": "case-1"}, "session_id": "session-1", "consume": False}]
    assert service.calls[0]["state"]["feedback_payload_ref"] == "feedback-ref"


def test_node_fails_closed_when_scope_or_payload_ref_is_missing():
    node = FeedbackIntakeNode(service=Service(), payload_store=PayloadStore({}), scope_keys=("company_id", "case_id"))

    result = node.execute({"feedback_payload_ref": "feedback-ref", "session_id": "session-1", "company_id": "co-1"})

    assert result["status"] == AgentStatus.ERROR.value
    assert result["error_log"] == ["FeedbackIntakeNode: feedback_scope_missing_or_invalid"]


def test_node_audits_rejection_before_the_service_can_be_called(monkeypatch):
    events = []
    monkeypatch.setattr(node_module, "emit_trace_event", lambda event, payload, state: events.append((event, payload, state)))
    state = {"feedback_payload_ref": "feedback-ref", "session_id": "session-1", "company_id": "co-1"}
    node = FeedbackIntakeNode(service=Service(), payload_store=PayloadStore({}), scope_keys=("company_id", "case_id"))

    result = node.execute(state)

    assert result["status"] == AgentStatus.ERROR.value
    assert events == [
        ("feedback_rejected", {"scope": {}, "record_id": None, "reason": "feedback_scope_missing_or_invalid"}, state)
    ]


def test_node_emits_payload_resolved_once_before_the_service_records_the_outcome(monkeypatch):
    """Node event = resolution stage; service events = outcome. Never the same fact twice."""
    trace = []
    monkeypatch.setattr(
        node_module,
        "emit_trace_event",
        lambda event, payload, state: trace.append((event, payload, state)),
    )
    payload = {
        "record_id": "finding_source",
        "feedback_seq": "seq-1",
        "verdict_code": "dismissed",
        "rationale": "operator private rationale",
        "decided_by": "operator-1",
        "decided_at": "2026-07-30T08:00:00+00:00",
    }

    class RecordingService(Service):
        def submit(self, **kwargs):
            trace.append(("service.submit", None, None))
            return super().submit(**kwargs)

    node = FeedbackIntakeNode(
        service=RecordingService(),
        payload_store=PayloadStore(json.dumps(payload)),
        scope_keys=("company_id", "case_id"),
    )

    state = {
        "feedback_payload_ref": "feedback-ref",
        "session_id": "session-1",
        "company_id": "co-1",
        "case_id": "case-1",
    }

    result = node.execute(state)

    assert result["status"] == AgentStatus.SUCCESS.value
    assert [event for event, _, _ in trace] == ["feedback_payload_resolved", "service.submit"]
    assert trace[0][1] == {
        "scope": {"company_id": "co-1", "case_id": "case-1"},
        "payload_ref_key": "feedback_payload_ref",
    }
    # The full state must reach the emitter, or the record loses
    # trace_id / correlation_id / session_id and cannot be tied to the invocation.
    assert trace[0][2] is state
    # The resolved payload is caller-supplied and the node does not add another
    # copy of it to the trace; it emits only values it received verified.
    assert "operator private rationale" not in str(trace[0][1])
    assert "finding_source" not in str(trace[0][1])
    assert "operator-1" not in str(trace[0][1])


def test_service_rejection_is_recorded_once_by_the_service_not_again_by_the_node(monkeypatch):
    """TESTING.md B-6 (4): the node must not duplicate the service's feedback_rejected.

    Both modules' emitters are collected into one list, so a second write by the
    node shows up as a second `feedback_rejected` entry here.
    """
    trace = []
    collect = lambda event, payload, state: trace.append(event)  # noqa: E731
    monkeypatch.setattr(node_module, "emit_trace_event", collect)
    monkeypatch.setattr(service_module, "emit_trace_event", collect)

    class RejectingService:
        """Mirrors FeedbackIntakeService._reject: emit from the service, then raise."""

        def submit(self, **kwargs):
            service_module.emit_trace_event(
                "feedback_rejected",
                {"scope": dict(kwargs["scope"]), "record_id": kwargs["record_id"], "reason": "feedback_target_not_owned"},
                kwargs.get("state"),
            )
            raise FeedbackRejectedError("feedback_target_not_owned")

    payload = {
        "record_id": "finding_source",
        "feedback_seq": "seq-1",
        "verdict_code": "dismissed",
        "rationale": "rationale",
        "decided_by": "operator-1",
        "decided_at": "2026-07-30T08:00:00+00:00",
    }
    node = FeedbackIntakeNode(
        service=RejectingService(),
        payload_store=PayloadStore(json.dumps(payload)),
        scope_keys=("company_id", "case_id"),
    )

    result = node.execute(
        {
            "feedback_payload_ref": "feedback-ref",
            "session_id": "session-1",
            "company_id": "co-1",
            "case_id": "case-1",
        }
    )

    assert result["error_log"] == ["FeedbackIntakeNode: feedback_target_not_owned"]
    assert trace == ["feedback_payload_resolved", "feedback_rejected"]
    assert trace.count("feedback_rejected") == 1


def test_call_path_emits_payload_resolved_through_the_security_pipeline(monkeypatch):
    """Same event on the real `node(state)` path, not just a direct execute() call."""
    trace = []
    monkeypatch.setattr(
        node_module, "emit_trace_event", lambda event, payload, state: trace.append(event)
    )
    payload = {
        "record_id": "finding_source",
        "feedback_seq": "seq-1",
        "verdict_code": "dismissed",
        "rationale": "rationale",
        "decided_by": "operator-1",
        "decided_at": "2026-07-30T08:00:00+00:00",
    }
    node = FeedbackIntakeNode(
        service=Service(),
        payload_store=PayloadStore(json.dumps(payload)),
        scope_keys=("company_id", "case_id"),
    )

    result = node(
        {
            "feedback_payload_ref": "feedback-ref",
            "session_id": "session-1",
            "company_id": "co-1",
            "case_id": "case-1",
            "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
        }
    )

    assert result["status"] == AgentStatus.SUCCESS.value
    assert trace == ["feedback_payload_resolved"]


def test_call_path_denied_by_the_trust_gate_emits_no_domain_event(monkeypatch):
    trace = []
    monkeypatch.setattr(
        node_module, "emit_trace_event", lambda event, payload, state: trace.append(event)
    )
    node = FeedbackIntakeNode(
        service=Service(), payload_store=PayloadStore("{}"), scope_keys=("company_id", "case_id")
    )

    result = node(
        {
            "feedback_payload_ref": "feedback-ref",
            "session_id": "session-1",
            "company_id": "co-1",
            "case_id": "case-1",
            "caller_trust_level": TrustLevel.ANONYMOUS.value,
        }
    )

    # S-1 refuses before execute(), so the node's own event must not appear.
    assert result["status"] == AgentStatus.ERROR.value
    assert trace == []


def test_node_does_not_emit_payload_resolved_when_the_payload_never_resolved(monkeypatch):
    trace = []
    monkeypatch.setattr(
        node_module, "emit_trace_event", lambda event, payload, state: trace.append(event)
    )
    node = FeedbackIntakeNode(
        service=Service(), payload_store=PayloadStore(None), scope_keys=("company_id", "case_id")
    )

    result = node.execute(
        {
            "feedback_payload_ref": "unknown-ref",
            "session_id": "session-1",
            "company_id": "co-1",
            "case_id": "case-1",
        }
    )

    assert result["error_log"] == ["FeedbackIntakeNode: feedback_payload_unresolvable"]
    assert trace == ["feedback_rejected"]


def test_node_does_not_emit_payload_resolved_for_a_payload_that_fails_the_allowlist(monkeypatch):
    trace = []
    monkeypatch.setattr(
        node_module, "emit_trace_event", lambda event, payload, state: trace.append(event)
    )
    payload = {
        "record_id": "finding_source",
        "feedback_seq": "seq-1",
        "verdict_code": "dismissed",
        "rationale": "rationale",
        "decided_by": "operator-1",
        "decided_at": "2026-07-30T08:00:00+00:00",
        "scope": {"company_id": "attacker"},
    }
    node = FeedbackIntakeNode(
        service=StrictService(),
        payload_store=PayloadStore(json.dumps(payload)),
        scope_keys=("company_id", "case_id"),
    )

    result = node.execute(
        {
            "feedback_payload_ref": "feedback-ref",
            "session_id": "session-1",
            "company_id": "co-1",
            "case_id": "case-1",
        }
    )

    assert result["error_log"] == ["FeedbackIntakeNode: feedback_payload_invalid"]
    assert trace == ["feedback_rejected"]


def test_node_allowlists_exact_feedback_payload_keys_instead_of_expanding_client_mapping():
    payload = {
        "record_id": "finding_source",
        "feedback_seq": "seq-1",
        "verdict_code": "dismissed",
        "rationale": "rationale",
        "decided_by": "operator-1",
        "decided_at": "2026-07-30T08:00:00+00:00",
        "scope": {"company_id": "attacker"},
    }
    node = FeedbackIntakeNode(
        service=StrictService(),
        payload_store=PayloadStore(json.dumps(payload)),
        scope_keys=("company_id", "case_id"),
    )

    result = node.execute(
        {"feedback_payload_ref": "feedback-ref", "session_id": "session-1", "company_id": "co-1", "case_id": "case-1"}
    )

    assert result["status"] == AgentStatus.ERROR.value
    assert result["error_log"] == ["FeedbackIntakeNode: feedback_payload_invalid"]
