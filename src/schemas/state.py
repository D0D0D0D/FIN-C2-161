"""Flat, checkpoint-safe state for FIN-C2-161."""

from typing import NotRequired

from framework.schemas.agent_state import AgentState


class AMLDispositionState(AgentState):
    mode: str
    scope: dict[str, str]
    case_ref: str
    payload_ref: str
    as_of_date: NotRequired[str]
    institution_id: NotRequired[str]
    unit_id: NotRequired[str]
    feedback_payload_ref: NotRequired[str]
    evidence_check: NotRequired[dict]
    recalled_perspectives: NotRequired[list[dict]]
    memo_sections: NotRequired[dict]
    evidence_map: NotRequired[list[dict]]
    requires_human_review: NotRequired[bool]
    review_reasons: NotRequired[list[str]]
    ingest_summary: NotRequired[dict]
    feedback_summary: NotRequired[dict]
    degradation_reason: NotRequired[str]
    gate_kind: NotRequired[str]
    facts: NotRequired[list[str]]
    evidence_sources: NotRequired[list[dict]]
    unknowns: NotRequired[list[str]]
    countervailing_factors: NotRequired[list[str]]
    memo_draft: NotRequired[str]


# Retain the conventional scaffold name for discovery compatibility.
State = AMLDispositionState
