"""The prompt contract and runtime projection share one declaration."""

from __future__ import annotations

import json

import pytest
from framework.errors import SecurityViolationError

from src.nodes.domain_workflow_nodes import _build_prompt, _markers, _project_draft
from src.services.payload_store import build_payload_store


def _value_from_contract(field: dict) -> object:
    if "enum" in field:
        return field["enum"][0]
    kind = field["type"]
    if kind == "str":
        return "contract-value"
    if kind == "bool":
        return False
    if kind == "list":
        if "items" in field:
            return [_object_from_contract(field["items"])]
        return ["contract-value"] if field.get("item_type") == "str" else []
    raise AssertionError(f"unsupported test contract type: {kind}")


def _object_from_contract(fields: dict) -> dict:
    return {
        name: _value_from_contract(field)
        for name, field in fields.items()
        if field["required"]
    }


def test_prompt_declares_a_machine_constructible_response_that_runtime_accepts():
    store = build_payload_store(ttl_seconds=60)
    ref = store.put(
        json.dumps(
            {
                "__extra__": {
                    "payload.raw": json.dumps(
                        {"free_text": "取引記録を確認した。", "alert_type": "cash", "evidence_refs": []},
                        ensure_ascii=False,
                    )
                }
            },
            ensure_ascii=False,
        ),
        scope={"institution_id": "bank-a"},
        session_id="contract-session",
    )
    prompt = json.loads(
        _build_prompt(
            {
                "scope": {"institution_id": "bank-a", "unit_id": "aml-a"},
                "session_id": "contract-session",
                "payload_ref": ref,
                "case_ref": "case-a",
                "evidence_check": {
                    "evidence_present": [
                        {
                            "source_id": "source-alpha",
                            "kind": "transaction_record",
                            "amount": "１,５００円",
                            "date": "2026-08-12",
                            "transaction_id": "取引ID: TX-ABC",
                        }
                    ],
                    "evidence_gap": [],
                },
                "recalled_perspectives": [],
            },
            store,
        )
    )
    contract = prompt["response_contract"]
    response = _object_from_contract(contract["fields"])
    constraints = contract["marker_constraints"]

    assert _project_draft(response) == response
    assert "validated after normalization" in constraints["instruction"]
    assert constraints["marker_types"] == ["amount", "date", "transaction"]
    allowed = constraints["allowed_values_by_source"]["source-alpha"]
    assert "1500" in allowed["amount"]
    assert allowed["date"] == ["2026-08-12"]
    assert allowed["transaction"] == ["tx-abc"]
    readable = constraints["allowed_marker_forms_by_source"]["source-alpha"]
    assert {
        item["normalized_value"]: item["source_forms"]
        for item in readable["amount"]
    }["1500"] == ["１,５００円"]
    assert {
        item["normalized_value"]: item["source_forms"]
        for item in readable["date"]
    }["2026-08-12"] == ["2026-08-12"]
    assert {
        item["normalized_value"]: item["source_forms"]
        for item in readable["transaction"]
    }["tx-abc"] == ["取引ID: TX-ABC"]

    field_constraints = constraints["field_constraints"]
    assert field_constraints["evidence_map[].claim"]["rule"] == "subset_of_cited_source_markers"
    assert field_constraints["facts[]"]["rule"] == "subset_of_evidence_map_claim_markers"
    assert field_constraints["memo_draft"]["rule"] == "subset_of_evidence_map_claim_markers"

    # Control: the contract must cover every marker kind enforced by the validator,
    # rather than a hand-maintained subset that can drift from _markers().
    extracted_kinds = set(_markers("１,５００円 2026-08-12 取引ID: TX-ABC"))
    assert set(constraints["marker_types"]) == extracted_kinds
    assert all(set(values) == extracted_kinds for values in constraints["allowed_values_by_source"].values())
    assert all(set(values) == extracted_kinds for values in constraints["allowed_marker_forms_by_source"].values())


@pytest.mark.parametrize(
    "malformed",
    [
        {"memo_draft": {"raw": "SENSITIVE_MARKER"}},
        {"facts": [{"raw": "SENSITIVE_MARKER"}]},
        {"undeclared_sensitive_marker": "SENSITIVE_MARKER"},
    ],
)
def test_typed_projection_rejects_nested_or_undeclared_llm_values_without_echo(malformed):
    valid = {
        "facts": [],
        "evidence_sources": [],
        "unknowns": [],
        "countervailing_factors": [],
        "memo_draft": "",
        "requires_human_review": True,
        "review_reasons": [],
        "evidence_map": [],
    }
    valid.update(malformed)

    with pytest.raises(SecurityViolationError) as caught:
        _project_draft(valid)

    assert "SENSITIVE_MARKER" not in str(caught.value)
