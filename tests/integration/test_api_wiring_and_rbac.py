"""T-1/T-2 and mode-RBAC integration coverage using only injected seams."""

from __future__ import annotations

from fastapi.testclient import TestClient
from framework.schemas.trust_level import TrustLevel

from src.api import server


def _record() -> dict:
    return {
        "case_ref": "case-history",
        "unit_id": "aml-a",
        "alert_type": "cash",
        "occurred_at": "2026-01-01T00:00:00+00:00",
        "verdict_code": "reviewed",
        "decided_by": "legacy-reviewer",
        "decided_at": "2026-01-02T00:00:00+00:00",
        "body": "過去の調査記録本文です。",
        "checked_questions": ["取引目的は確認済みか"],
        "evidence_categories": ["transaction_record"],
        "countervailing_notes": ["照会回答を確認する"],
    }


def test_startup_wires_manifest_values_and_mode_rbac_precedes_storage():
    roles = {"value": ["unrelated_role"]}
    server.app.state.verify_scope = lambda _request: {"institution_id": "bank-a", "unit_id": "aml-a"}
    server.app.state.verified_principal = lambda _request: {
        "trust_level": TrustLevel.VERIFIED_EXTERNAL,
        "caller_id": "caller-a",
        "unit_id": "aml-a",
        "roles": roles["value"],
    }
    with TestClient(server.app, raise_server_exceptions=False) as client:
        # T-1: startup() ran and manifest values reached real injected services.
        assert server.app.state.agent.config["recall_top_k"] == 5
        assert server.app.state.payload_store._ttl_seconds == 900
        assert server.app.state.ledger_service._config.scope_keys == ("institution_id",)
        assert server.app.state.runtime_config["roles"]["ingest"] == ["aml_data_steward"]

        before = len(server.app.state.ledger_service._backend.query(scope={"institution_id": "bank-a"}))
        denied = client.post("/ingest", json={"records": [_record()], "session_id": "ingest-denied"})
        after = len(server.app.state.ledger_service._backend.query(scope={"institution_id": "bank-a"}))
        assert denied.status_code == 403
        assert before == after == 0  # no ledger record or retrieval projection was created
        assert server.app.state.payload_store._items == {}  # RBAC was before payload_store.put

        # The read-only mode deliberately needs no extra role.
        invoke = client.post(
            "/invoke",
            json={
                "case_ref": "case-new",
                "alert_type": "cash",
                "evidence_refs": [{"source_id": "source-a", "kind": "transaction_record", "amount": "999999円"}],
                "free_text": "取引記録を確認した。",
                "session_id": "invoke-readonly",
            },
        )
        assert invoke.status_code == 200
        assert invoke.json()["status"] == "success"

        roles["value"] = ["aml_data_steward"]
        accepted = client.post("/ingest", json={"records": [_record()], "session_id": "ingest-authorized"})
        assert accepted.status_code == 200
        summary = accepted.json()["output"]["ingest_summary"]
        assert summary["written_count"] == 1
        assert len(server.app.state.ledger_service._backend.query(scope={"institution_id": "bank-a"})) == 1

        record_id = summary["written_ids"][0]
        roles["value"] = ["unrelated_role"]
        denied_feedback = client.post(
            "/feedback",
            json={
                "case_ref": "case-history",
                "record_id": record_id,
                "feedback_seq": "feedback-a",
                "verdict_code": "closed",
                "rationale": "担当者が根拠を確認した。",
                "decided_at": "2026-01-03T00:00:00+00:00",
                "session_id": "feedback-denied",
            },
        )
        assert denied_feedback.status_code == 403
        assert len(server.app.state.ledger_service._backend.query(scope={"institution_id": "bank-a"})) == 1

        roles["value"] = ["aml_reviewer"]
        accepted_feedback = client.post(
            "/feedback",
            json={
                "case_ref": "case-history",
                "record_id": record_id,
                "feedback_seq": "feedback-a",
                "verdict_code": "closed",
                "rationale": "担当者が根拠を確認した。",
                "decided_at": "2026-01-03T00:00:00+00:00",
                "session_id": "feedback-authorized",
            },
        )
        assert accepted_feedback.status_code == 200
        assert accepted_feedback.json()["output"]["feedback_summary"]["updated_ids"]


def test_manifest_runtime_values_change_live_service_and_authorization_behavior():
    config = server._load_runtime_config()
    config["payload_ttl_seconds"] = 17
    config["recall_top_k"] = 1
    config["roles"] = {"ingest": ["custom_steward"], "feedback": ["custom_reviewer"]}
    server.app.state.runtime_config_override = config
    roles = {"value": ["aml_data_steward"]}
    server.app.state.verify_scope = lambda _request: {"institution_id": "bank-config", "unit_id": "aml-a"}
    server.app.state.verified_principal = lambda _request: {
        "trust_level": TrustLevel.VERIFIED_EXTERNAL,
        "caller_id": "caller-config",
        "unit_id": "aml-config",
        "roles": roles["value"],
    }
    try:
        with TestClient(server.app, raise_server_exceptions=False) as client:
            assert server.app.state.payload_store._ttl_seconds == 17
            assert server.app.state.agent.config["recall_top_k"] == 1
            denied = client.post("/ingest", json={"records": [_record()], "session_id": "config-denied"})
            assert denied.status_code == 403
            roles["value"] = ["custom_steward"]
            accepted = client.post("/ingest", json={"records": [_record()], "session_id": "config-accepted"})
            assert accepted.status_code == 200
            assert accepted.json()["output"]["ingest_summary"]["written_count"] == 1
    finally:
        server.app.state.runtime_config_override = None
