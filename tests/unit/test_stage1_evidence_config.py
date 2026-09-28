"""Stage 1 customer-configurable evidence and deterministic freshness tests."""

from __future__ import annotations

import json
from datetime import date as CalendarDate

from fastapi.testclient import TestClient
from framework.schemas.trust_level import TrustLevel

from src.api import server
from src.feedback_intake import InMemoryFeedbackReceiptStore
from src.graph.domain_workflow_graph import AMLDispositionWorkflowGraph
from src.services.ledger_service import build_ledger_service
from src.services.payload_store import build_payload_store


def _stage1(payload: dict, *, required: dict | None = None, max_age: int | None = None, as_of: str = "2026-01-01") -> dict:
    store = build_payload_store(ttl_seconds=60)
    session_id = "stage1-session"
    ref = store.put(
        json.dumps({"__extra__": {"payload.raw": json.dumps(payload, ensure_ascii=False)}}),
        scope={"institution_id": "bank-a"},
        session_id=session_id,
    )
    workflow = AMLDispositionWorkflowGraph(
        {
            "ledger_service": build_ledger_service(),
            "payload_store": store,
            "recall_top_k": 1,
            "max_free_text_chars": 12000,
            "required_evidence_by_alert_type": required,
            "evidence_max_age_days": max_age,
            "trusted_scope": {"institution_id": "bank-a"},
            "request_session_id": session_id,
            "feedback_receipt_store": InMemoryFeedbackReceiptStore(),
        }
    )
    workflow.register_nodes()
    return workflow._nodes["evidence_check"].execute(
        {
            "scope": {"institution_id": "bank-a", "unit_id": "aml-a"},
            "session_id": session_id,
            "payload_ref": ref,
            "as_of_date": as_of,
        }
    )


def _full_requirements() -> dict[str, list[str]]:
    return {
        "cash": ["transaction_record", "kyc_document"],
        "sanctions": ["transaction_record", "inquiry_response"],
        "transaction_monitoring": ["transaction_record"],
        "kyc": ["kyc_document"],
    }


def test_required_evidence_config_changes_stage1_gap_content():
    payload = {"alert_type": "kyc", "evidence_refs": [{"source_id": "kyc-a", "kind": "kyc_document"}]}
    default_result = _stage1(payload)
    customized = _full_requirements()
    customized["kyc"] = ["kyc_document", "inquiry_response"]
    custom_result = _stage1(payload, required=customized)
    assert default_result["evidence_check"]["evidence_gap"] == []
    assert custom_result["evidence_check"]["evidence_gap"] == [
        {"reason": "required_evidence_kind_missing", "kind": "inquiry_response"}
    ]


def test_evidence_max_age_config_changes_stale_result_without_removing_present():
    payload = {
        "alert_type": "transaction_monitoring",
        "evidence_refs": [{"source_id": "txn-a", "kind": "transaction_record", "date": "２０２５年１月１日"}],
    }
    stale = _stage1(payload, max_age=30)
    fresh_under_policy = _stage1(payload, max_age=3650)
    assert stale["evidence_check"]["evidence_present"] == [
        {"source_id": "txn-a", "kind": "transaction_record", "date": "２０２５年１月１日"}
    ]
    assert stale["evidence_check"]["evidence_gap"] == [
        {"reason": "evidence_stale", "source_id": "txn-a", "kind": "transaction_record", "age_days": 365}
    ]
    assert fresh_under_policy["evidence_check"]["evidence_gap"] == []


def test_unset_max_age_disables_all_freshness_checks():
    payload = {
        "alert_type": "transaction_monitoring",
        "evidence_refs": [{"source_id": "txn-a", "kind": "transaction_record", "date": "2020-01-01"}],
    }
    result = _stage1(payload, max_age=None)
    assert result["evidence_check"]["evidence_gap"] == []


def test_missing_evidence_date_is_unverifiable_when_freshness_enabled():
    payload = {"alert_type": "transaction_monitoring", "evidence_refs": [{"source_id": "txn-a", "kind": "transaction_record"}]}
    result = _stage1(payload, max_age=30)
    assert result["evidence_check"]["evidence_gap"] == [
        {"reason": "evidence_date_unverifiable", "source_id": "txn-a", "kind": "transaction_record"}
    ]


def test_adapter_fixes_explicit_and_omitted_as_of_date_once(monkeypatch):
    captured: list[str] = []
    server.app.state.verify_scope = lambda _request: {"institution_id": "bank-a", "unit_id": "aml-a"}
    server.app.state.verified_principal = lambda _request: {
        "trust_level": TrustLevel.VERIFIED_EXTERNAL,
        "caller_id": "caller-a",
        "unit_id": "aml-a",
        "roles": [],
    }

    def fake_invoke(_request, **kwargs):
        captured.append(kwargs["as_of_date"])
        return {"status": "success", "output": {}}

    monkeypatch.setattr(server, "_invoke_graph", fake_invoke)
    request = {"case_ref": "case-a", "alert_type": "cash", "evidence_refs": [], "free_text": "記録を確認した。"}
    with TestClient(server.app, raise_server_exceptions=False) as client:
        explicit = client.post("/invoke", json={**request, "as_of_date": "2020-01-02"})
        omitted = client.post("/invoke", json=request)
    assert explicit.status_code == omitted.status_code == 200
    assert captured[0] == "2020-01-02"
    assert CalendarDate.fromisoformat(captured[1]).isoformat() == captured[1]
