"""TC-01..08 domain contracts and Stage 2/4 regression coverage."""

from __future__ import annotations

import inspect

import pytest
from framework.errors import SecurityViolationError
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.domain_workflow_nodes import (
    EvidenceLinkVerifyNode,
    SimilarPerspectiveRecallNode,
    _build_prompt,
    _markers,
)
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import AMLDispositionState
from src.services.ledger_service import build_ledger_service
from src.services.payload_store import build_payload_store


def _stage4_state(claim: str, source: dict, *, facts: list[str] | None = None) -> dict:
    return {
        "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
        "correlation_id": "tc-stage4",
        "execution_time": {},
        "memo_sections": {
            "facts": facts or [],
            "evidence_sources": [{"source_id": source["source_id"], "kind": source["kind"], "label": "reference"}],
            "unknowns": [],
            "countervailing_factors": [],
            "memo_draft": "",
        },
        "evidence_map": [{"claim_id": "claim-a", "claim": claim, "source_ids": [source["source_id"]]}],
        "evidence_check": {"evidence_present": [source], "evidence_gap": []},
        "recalled_perspectives": [],
        "review_reasons": [],
        "requires_human_review": False,
    }


@pytest.mark.parametrize(
    "claim",
    [
        "総額は999999円。",       # unit adjacent
        "総額は1000万円を確認した。",  # particle adjacent
        "総額は999999円。",       # sentence period
        "総額は１,０００万円。",    # full-width and separator
        "総額は999,999円。",      # separator
    ],
)
def test_stage4_accepts_normalized_numeric_forms(claim):
    source = {"source_id": "source-a", "kind": "transaction_record", "amount": "10000000円"}
    # The two 999999 forms intentionally use their own exact source amount.
    if "999999" in claim or "999,999" in claim:
        source["amount"] = "999999円"
    result = EvidenceLinkVerifyNode().execute(_stage4_state(claim, source))
    assert result["status"] == AgentStatus.SUCCESS.value


def test_stage4_fails_for_real_but_unrelated_source_and_numeric_rewrite():
    unrelated = {"source_id": "source-b", "kind": "transaction_record", "amount": "1000円"}
    result = EvidenceLinkVerifyNode().execute(_stage4_state("総額は999999円。", unrelated))
    assert result["status"] == AgentStatus.ERROR.value
    assert "amount_mismatch" in result["error_log"][0]


def test_stage4_rejects_a_date_missing_from_the_cited_source():
    source = {
        "source_id": "source-a",
        "kind": "transaction_record",
        "date": "2026-08-12",
        "reference_number": "13",
    }

    result = EvidenceLinkVerifyNode().execute(_stage4_state("記録日は2026-08-13。", source))

    assert result["status"] == AgentStatus.ERROR.value
    assert result["error_log"] == ["EvidenceLinkVerifyNode: date_mismatch"]


_MARKER_PROVENANCE_CASES = (
    pytest.param(
        {"amount": "１,５００円"},
        "金額は1,500円。",
        "金額は9,999円。",
        "amount",
        id="amount",
    ),
    pytest.param(
        {"date": "2026/08/12", "reference_number": "13"},
        "記録日は2026/08/12。",
        "記録日は2026/08/13。",
        "date",
        id="date",
    ),
    pytest.param(
        {"transaction_id": "取引ID: TX-ABC"},
        "取引ID: tx-abcを確認した。",
        "取引ID: TX-OTHERを確認した。",
        "transaction",
        id="transaction",
    ),
)


@pytest.mark.parametrize(("source_marker", "accepted_claim", "rejected_claim", "marker_kind"), _MARKER_PROVENANCE_CASES)
def test_stage4_enforces_every_marker_kind_with_a_passing_control(
    source_marker,
    accepted_claim,
    rejected_claim,
    marker_kind,
):
    source = {"source_id": "source-a", "kind": "transaction_record", **source_marker}

    accepted = EvidenceLinkVerifyNode().execute(_stage4_state(accepted_claim, source))
    rejected = EvidenceLinkVerifyNode().execute(_stage4_state(rejected_claim, source))

    assert accepted["status"] == AgentStatus.SUCCESS.value
    assert rejected["status"] == AgentStatus.ERROR.value
    assert rejected["error_log"] == [f"EvidenceLinkVerifyNode: {marker_kind}_mismatch"]


def test_stage4_marker_cases_cover_every_kind_returned_by_validator():
    exercised = {case.values[-1] for case in _MARKER_PROVENANCE_CASES}
    assert exercised == set(_markers("１,５００円 2026/08/12 取引ID: TX-ABC"))


def test_stage4_accepts_only_marker_values_present_in_the_cited_source():
    source = {
        "source_id": "source-a",
        "kind": "transaction_record",
        "date": "2026-08-12",
        "amount": "1500円",
        "transaction_id": "取引ID: TX-ABC",
    }
    claim = "2026-08-12の金額は1500円、取引ID: TX-ABC。"

    result = EvidenceLinkVerifyNode().execute(_stage4_state(claim, source))

    assert result["status"] == AgentStatus.SUCCESS.value


@pytest.mark.parametrize(
    ("amount_text", "expected"),
    [
        ("1千万円", "10000000"),
        ("一千万円", "10000000"),
        ("3千万円", "30000000"),
        ("1億2千万円", "120000000"),
        ("2億", "200000000"),
        ("1000万円", "10000000"),
        ("1,234,567円", "1234567"),
        ("１２３４５６円", "123456"),
        ("999999円", "999999"),
        ("250000.", "250000"),
    ],
)
def test_complex_japanese_amounts_are_canonical_on_claim_and_source(amount_text, expected):
    """The same parser must normalize source metadata and claim text."""
    assert _markers(amount_text)["amount"] == {expected}
    source = {"source_id": "source-a", "kind": "transaction_record", "amount": amount_text}
    result = EvidenceLinkVerifyNode().execute(_stage4_state(f"総額は{amount_text}", source))
    assert result["status"] == AgentStatus.SUCCESS.value


def test_stage4_demotes_numeric_fact_missing_from_claim_map():
    source = {"source_id": "source-a", "kind": "transaction_record", "amount": "1000円"}
    state = _stage4_state("取引記録を確認した。", source, facts=["総額は1000万円を確認した。"])
    result = EvidenceLinkVerifyNode().execute(state)
    assert result["status"] == AgentStatus.SUCCESS.value
    assert result["requires_human_review"] is True
    assert "uncited_free_text_marker" in result["review_reasons"]


def test_stage2_projection_and_stage3_prompt_never_expose_verdict_marker():
    ledger = build_ledger_service()
    marker = "VERDICT_SECRET_MARKER"
    written = ledger.write(
        {"institution_id": "bank-a"},
        {
            "record_kind": "disposition_record",
            "classification": {"alert_type": "cash"},
            "occurred_at": "2026-01-01T00:00:00+00:00",
            "verdict_code": "reviewed",
            "decided_by": "reviewer-a",
            "decided_at": "2026-01-02T00:00:00+00:00",
            "decision_source": "human",
            "retrieval_text": "調査観点のみ",
            "case_ref": "case-history",
            "unit_id": "aml-a",
            "provenance": {
                "checked_questions": ["取引目的"],
                "evidence_categories": ["transaction_record"],
                "countervailing_notes": ["反証確認"],
                "quality_flags": ["complete"],
                "private_marker": marker,
            },
            "body": "監査本文",
            "rationale": "監査根拠",
            "ai_suggested_verdict": "closed",
        },
    )
    # The marker is deliberately injected into a public verdict-shaped field
    # to prove the template's second allowlist projection is what protects it.
    ledger._backend._records[written["record_id"]]["verdict_code"] = marker
    store = build_payload_store(ttl_seconds=60)
    recall_ref = store.put(
        '{"__extra__":{"payload.raw":"{\\"free_text\\":\\"調査観点を確認した。\\",'
        '\\"alert_type\\":\\"cash\\",\\"evidence_refs\\":[]}"}}',
        scope={"institution_id": "bank-a"},
        session_id="recall-session",
    )
    recalled = SimilarPerspectiveRecallNode(
        ledger_service=ledger,
        payload_store=store,
        top_k=5,
        max_free_text_chars=12000,
    ).execute(
        {
            "scope": {"institution_id": "bank-a", "unit_id": "aml-a"},
            "session_id": "recall-session",
            "payload_ref": recall_ref,
            "evidence_check": {"evidence_present": [], "evidence_gap": []},
            "status": "pending",
        }
    )["recalled_perspectives"]
    assert marker not in str(recalled)

    ref = store.put(
        '{"free_text":"ケース本文","alert_type":"cash","evidence_refs":[]}',
        scope={"institution_id": "bank-a"},
        session_id="session-a",
    )
    # Prompt helper consumes the same vault shape that the production scrubber writes.
    store._items[ref].value = '{"__extra__":{"payload.raw":"{\\"free_text\\":\\"ケース本文\\",\\"alert_type\\":\\"cash\\",\\"evidence_refs\\":[]}"}}'
    prompt = _build_prompt(
        {
            "scope": {"institution_id": "bank-a", "unit_id": "aml-a"},
            "session_id": "session-a",
            "payload_ref": ref,
            "evidence_check": {},
            "recalled_perspectives": recalled,
            "case_ref": "case-new",
        },
        store,
    )
    assert marker not in prompt


def test_state_contract_is_flat_and_preprocess_rejects_invalid_s2_context():
    assert "final_disposition" not in AMLDispositionState.__annotations__
    assert "str_required" not in AMLDispositionState.__annotations__
    with pytest.raises(SecurityViolationError):
        PreProcessNode()._extra_security_gate_input({"input_context": "untrusted"})


def test_all_domain_nodes_explicitly_declare_verified_trust():
    import src.nodes.domain_workflow_nodes as module

    classes = [value for value in vars(module).values() if inspect.isclass(value) and value.__module__ == module.__name__ and hasattr(value, "required_trust_level")]
    assert classes
    assert all(value.required_trust_level is TrustLevel.VERIFIED_EXTERNAL for value in classes)
