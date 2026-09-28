"""TC coverage for the Cat2 main-slot GraphNode adaptation."""

import json

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from src.nodes.main_node import MainNode
from src.graph.graph import AMLDispositionMemoAgent


def test_main_node_declares_verified_trust_and_no_hitl():
    node = MainNode()
    assert node.required_trust_level is TrustLevel.VERIFIED_EXTERNAL
    assert node.propagate_hitl is False
    assert node.error_strategy == "propagate"


def test_extract_input_is_json_control_envelope_without_body():
    node = MainNode()
    envelope = json.loads(
        node.extract_input(
            {
                "mode": "invoke",
                "scope": {"institution_id": "bank-a", "unit_id": "aml-a"},
                "case_ref": "case-a",
                "payload_ref": "abcdefghijklmnopabcdefghijklmnop",
                "evidence_refs": [{"source_id": "src-1", "kind": "transaction_record"}],
            }
        )
    )
    assert envelope["scope"] == {"institution_id": "bank-a", "unit_id": "aml-a"}
    assert "body" not in envelope and "free_text" not in str(envelope)


def test_merge_output_promotes_declared_invoke_fields_but_never_result():
    delta = MainNode().merge_output(
        {"mode": "invoke"},
        {
            "status": AgentStatus.SUCCESS.value,
            "facts": ["fact"],
            "evidence_sources": [],
            "unknowns": [],
            "countervailing_factors": [],
            "memo_draft": "draft",
            "evidence_map": [],
            "requires_human_review": True,
            "review_reasons": ["reason"],
            "result": {"final_disposition": "UNVALIDATED_LEAK"},
        },
    )
    assert delta["status"] == AgentStatus.SUCCESS.value
    assert delta["memo_draft"] == "draft"
    assert "result" not in delta


def test_s3_rejection_never_falls_back_to_unvalidated_result():
    response = AMLDispositionMemoAgent().get_output(
        {
            "status": AgentStatus.ERROR.value,
            "result": {"final_disposition": "UNVALIDATED_LEAK"},
            "error_log": ["S-3 rejected"],
        }
    )
    assert response["output"] is None
    assert "UNVALIDATED_LEAK" not in str(response["output"])
