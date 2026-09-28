"""Batch 3 guards through the real FastAPI -> graph composition."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from framework.schemas.trust_level import TrustLevel

from src.api import server


def _record(case_ref: str, *, question: str, category: str, note: str) -> dict:
    return {
        "case_ref": case_ref,
        "unit_id": "aml-a",
        "alert_type": "cash",
        "occurred_at": "2026-01-01T00:00:00+00:00",
        "verdict_code": "reviewed",
        "decided_by": "legacy-reviewer",
        "decided_at": "2026-01-02T00:00:00+00:00",
        "body": "過去の調査記録本文です。",
        "checked_questions": [question],
        "evidence_categories": [category],
        "countervailing_notes": [note],
    }


def test_readonly_entry_token_cannot_write_and_payload_is_deleted(monkeypatch):
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "mock-entry-token")
    server.app.state.verified_principal = None
    server.app.state.verify_scope = lambda _request: {
        "institution_id": "bank-token",
        "unit_id": "aml-a",
    }

    with TestClient(server.app, raise_server_exceptions=False) as client:
        denied = client.post(
            "/ingest",
            headers={"authorization": "Bearer mock-entry-token"},
            json={"records": [_record("case-token", question="資金移動", category="cash", note="note")]},
        )
        assert denied.status_code == 403
        assert server.app.state.ledger_service._backend.query(scope={"institution_id": "bank-token"}) == []
        assert server.app.state.payload_store._items == {}

        readable = client.post(
            "/invoke",
            headers={"authorization": "Bearer mock-entry-token"},
            json={
                "case_ref": "case-token",
                "alert_type": "cash",
                "evidence_refs": [{"source_id": "txn-a", "kind": "transaction_record"}],
                "free_text": "取引記録を確認した。",
            },
        )
        assert readable.status_code == 200
        assert readable.json()["status"] == "success"
        assert server.app.state.payload_store._items == {}


def test_cross_scope_feedback_id_is_rejected_without_existence_or_write_leak():
    identity = {
        "scope": {"institution_id": "bank-a", "unit_id": "aml-a"},
        "roles": ["aml_data_steward"],
    }
    server.app.state.verify_scope = lambda _request: dict(identity["scope"])
    server.app.state.verified_principal = lambda _request: {
        "trust_level": TrustLevel.VERIFIED_EXTERNAL,
        "caller_id": "reviewer-a",
        "roles": list(identity["roles"]),
    }

    with TestClient(server.app, raise_server_exceptions=False) as client:
        ingested = client.post(
            "/ingest",
            json={"records": [_record("case-a", question="取引目的", category="cash", note="反証A")]},
        )
        record_id = ingested.json()["output"]["ingest_summary"]["written_ids"][0]

        identity["scope"] = {"institution_id": "bank-b", "unit_id": "aml-a"}
        identity["roles"] = ["aml_reviewer"]
        rejected = client.post(
            "/feedback",
            json={
                "case_ref": "case-a",
                "record_id": record_id,
                "feedback_seq": "feedback-a",
                "verdict_code": "closed",
                "rationale": "担当者が根拠を確認した。",
                "decided_at": "2026-01-03T00:00:00+00:00",
            },
        )

        assert rejected.status_code == 200
        assert rejected.json()["status"] == "error"
        assert "feedback_target_not_owned" in str(rejected.json())
        assert len(server.app.state.ledger_service._backend.query(scope={"institution_id": "bank-a"})) == 1
        assert server.app.state.ledger_service._backend.query(scope={"institution_id": "bank-b"}) == []


def test_production_retrieval_uses_business_query_to_rank_prior_records():
    roles = {"value": ["aml_data_steward"]}
    server.app.state.verify_scope = lambda _request: {"institution_id": "bank-query", "unit_id": "aml-a"}
    server.app.state.verified_principal = lambda _request: {
        "trust_level": TrustLevel.VERIFIED_EXTERNAL,
        "caller_id": "caller-query",
        "roles": roles["value"],
    }

    with TestClient(server.app, raise_server_exceptions=False) as client:
        for record in (
            _record(
                "case-cash",
                question="資金移動の取引目的を確認",
                category="transaction_record",
                note="資金移動の反証を確認する",
            ),
            _record(
                "case-kyc",
                question="本人確認書類を更新",
                category="kyc_document",
                note="本人確認の反証を確認する",
            ),
        ):
            response = client.post("/ingest", json={"records": [record]})
            assert response.json()["status"] == "success"

        roles["value"] = []
        invoked = client.post(
            "/invoke",
            json={
                "case_ref": "case-new",
                "alert_type": "cash",
                "evidence_refs": [{"source_id": "txn-a", "kind": "transaction_record"}],
                "free_text": "資金移動の取引目的を確認した。",
            },
        )
        output = invoked.json()["output"]

        assert invoked.json()["status"] == "success"
        assert output["countervailing_factors"] == []
        assert any("資金移動の反証を確認する" in item for item in output["unknowns"])
        assert not any("本人確認の反証を確認する" in item for item in output["unknowns"])


def test_llm_provider_exception_text_never_reaches_http_state_or_output():
    marker = "PROVIDER_SECRET_MARKER"

    class ExplodingLlm:
        def complete(self, _messages):
            raise RuntimeError(marker)

    config = server._load_runtime_config()
    config["llm"] = ExplodingLlm()
    server.app.state.runtime_config_override = config
    server.app.state.verify_scope = lambda _request: {"institution_id": "bank-llm", "unit_id": "aml-a"}
    server.app.state.verified_principal = lambda _request: {
        "trust_level": TrustLevel.VERIFIED_EXTERNAL,
        "caller_id": "caller-llm",
        "roles": [],
    }
    try:
        with TestClient(server.app, raise_server_exceptions=False) as client:
            response = client.post(
                "/invoke",
                json={
                    "case_ref": "case-llm",
                    "alert_type": "cash",
                    "evidence_refs": [{"source_id": "txn-a", "kind": "transaction_record"}],
                    "free_text": "取引記録を確認した。",
                },
            )
        serialized = json.dumps(response.json(), ensure_ascii=False)
        assert response.json()["status"] == "error"
        assert marker not in serialized
        assert "llm_call_failed" in serialized
    finally:
        server.app.state.runtime_config_override = None
