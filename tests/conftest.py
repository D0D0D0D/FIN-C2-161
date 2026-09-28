"""Fixtures consumed by ``work/scripts/mrb_live_llm_check.py``."""

from __future__ import annotations

import json

import pytest

from src.feedback_intake import InMemoryFeedbackReceiptStore
from src.graph.graph import AMLDispositionMemoAgent
from src.ingest_row_pipeline.checkpoint_scrubber import (
    CheckpointScrubPolicy,
    scrub_checkpoint_envelope,
)
from src.services import build_ledger_service, build_payload_store


_LIVE_SCOPE = {"institution_id": "bank-live", "unit_id": "aml-investigations"}
_LIVE_PARTITION = {"institution_id": _LIVE_SCOPE["institution_id"]}
_LIVE_SESSION_ID = "live-1"
_LIVE_CASE_REF = "case-live"


def _prior_disposition() -> dict:
    """A human precedent relevant to the current high-value cash alert."""

    return {
        "record_kind": "disposition_record",
        "classification": {"alert_type": "cash"},
        "occurred_at": "2026-05-10T00:00:00+00:00",
        "verdict_code": "reviewed",
        "decided_by": "aml-review-team",
        "decided_at": "2026-05-12T00:00:00+00:00",
        "decision_source": "human",
        "retrieval_text": (
            "高額現金入金 資金移動 取引目的 実質的支配者 KYC 更新確認"
        ),
        "case_ref": "case-prior-cash",
        "unit_id": "aml-review",
        "provenance": {
            "checked_questions": [
                "高額現金入金の資金源と直後の資金移動の取引目的は整合するか",
                "実質的支配者と最新 KYC 資料を確認したか",
            ],
            "evidence_categories": ["transaction_record", "kyc_document"],
            "countervailing_notes": [
                "請求書だけでなく入出金の相手方と資金源資料を突合する",
            ],
            "quality_flags": ["human_reviewed", "source_linked"],
        },
        "body": "過去案件では高額現金入金後の資金移動と KYC 更新状況を確認した。",
        "rationale": "裁定結果ではなく再利用可能な調査観点として蓄積する。",
    }


def _current_case_payload() -> dict:
    """Cash activity with a material KYC gap and a prior-case analogue."""

    return {
        "alert_type": "cash",
        "evidence_refs": [
            {
                "source_id": "txn-cash-aug",
                "kind": "transaction_record",
                "amount": "9800000円",
                "date": "2026-08-11",
                "transaction_id": "TXN-20260811-A",
            },
            {
                "source_id": "inquiry-purpose-aug",
                "kind": "inquiry_response",
                "date": "2026-08-12",
            },
        ],
        "free_text": (
            "設立直後の法人で9800000円の現金入金後、同日中に複数口へ資金移動した。"
            "取引目的の回答は設備購入だが請求書の金額と一致せず、"
            "実質的支配者を確認する最新 KYC 資料は提出されていない。"
        ),
    }


@pytest.fixture
def _live_runtime(monkeypatch):
    ledger = build_ledger_service()
    ledger.write(_LIVE_PARTITION, _prior_disposition())
    payload_store = build_payload_store(ttl_seconds=900)
    payload = _current_case_payload()
    policy = CheckpointScrubPolicy(
        safe_keys=frozenset({"mode", "case_ref", "unit_id"}),
        scope_keys=frozenset({"institution_id"}),
        enum_value_keys={"mode": frozenset({"ingest", "invoke", "feedback"})},
        safe_subtree_keys=frozenset({"evidence_refs"}),
    )
    safe_input = scrub_checkpoint_envelope(
        json.dumps(
            {
                "mode": "invoke",
                "case_ref": _LIVE_CASE_REF,
                **_LIVE_SCOPE,
                "evidence_refs": [
                    {"source_id": item["source_id"], "kind": item["kind"]}
                    for item in payload["evidence_refs"]
                ],
                "payload": {"raw": json.dumps(payload, ensure_ascii=False)},
            },
            ensure_ascii=False,
        ),
        payload_store,
        session_id=_LIVE_SESSION_ID,
        trusted_scope=_LIVE_PARTITION,
        policy=policy,
    )

    original_invoke = AMLDispositionMemoAgent.invoke

    def invoke_with_verified_scope(self, user_input, *args, **kwargs):
        # The generic checker constructs the graph directly, so mirror the
        # request-derived input_context that src/api/server.py supplies.
        context = kwargs.get("input_context")
        kwargs["input_context"] = {
            **(context if isinstance(context, dict) else {}),
            "scope": dict(_LIVE_SCOPE),
            "as_of_date": "2026-08-13",
        }
        ctx = kwargs.get("ctx")
        if ctx is not None:
            ctx.hitl_allowed = False
        return original_invoke(self, user_input, *args, **kwargs)

    monkeypatch.setattr(AMLDispositionMemoAgent, "invoke", invoke_with_verified_scope)
    return {
        "payload": json.loads(safe_input),
        "config": {
            "payload_store": payload_store,
            "ledger_service": ledger,
            "feedback_receipt_store": InMemoryFeedbackReceiptStore(),
            "trusted_scope": dict(_LIVE_PARTITION),
            "request_session_id": _LIVE_SESSION_ID,
            "recall_top_k": 5,
            "max_free_text_chars": 12000,
            "required_evidence_by_alert_type": {
                "cash": ["transaction_record", "kyc_document"],
                "sanctions": ["transaction_record", "inquiry_response"],
                "transaction_monitoring": ["transaction_record"],
                "kyc": ["kyc_document"],
            },
            "evidence_max_age_days": None,
            "memory_enabled": False,
            "hitl": {"enabled": False},
        },
    }


@pytest.fixture
def agent_config(_live_runtime) -> dict:
    """Request-scoped graph config; the checker injects ``config['llm']``."""

    return _live_runtime["config"]


@pytest.fixture
def case_payload(_live_runtime) -> dict:
    """Server-minted scrubbed envelope resolving to the AML business case."""

    return _live_runtime["payload"]
