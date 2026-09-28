"""Cat2 main slot configured from the copied graph-node skeleton."""

from __future__ import annotations

import json
import re
from typing import Any, ClassVar

from framework.schemas.agent_state import AgentState
from framework.schemas.trust_level import TrustLevel
from src.cat2_graph_skeleton.graph_node import DomainWorkflowGraphNode as _SkeletonGraphNode
from src.cat2_graph_skeleton.graph_node import ModeSpec
from src.graph.domain_workflow_graph import AMLDispositionWorkflowGraph


_SAFE_VALUE = re.compile(r"[^A-Za-z0-9-]")


class DomainWorkflowGraphNode(_SkeletonGraphNode):
    """FIN-C2-161 adaptation of cat2-graph-skeleton v0.1.4."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL
    error_strategy: ClassVar[str] = "propagate"
    propagate_hitl: ClassVar[bool] = False
    mode_specs: ClassVar[dict[str, ModeSpec]] = {
        "ingest": ModeSpec(
            gate_kind="ingest",
            fields={"ingest_summary": "ingest_summary", "error_log": "error_log"},
        ),
        "feedback": ModeSpec(
            gate_kind="feedback",
            fields={"feedback_summary": "feedback_summary", "error_log": "error_log"},
        ),
        "invoke": ModeSpec(
            gate_kind="draft",
            fields={
                "case_ref": "case_ref",
                "facts": "facts",
                "evidence_sources": "evidence_sources",
                "unknowns": "unknowns",
                "countervailing_factors": "countervailing_factors",
                "memo_draft": "memo_draft",
                "evidence_map": "evidence_map",
                "requires_human_review": "requires_human_review",
                "review_reasons": "review_reasons",
                "error_log": "error_log",
            },
        ),
    }

    def __init__(self, *, config: dict[str, Any] | None = None) -> None:
        super().__init__(config=config, subgraph_factory=AMLDispositionWorkflowGraph)

    def _parent_config(self) -> dict[str, Any]:
        return {
            "llm": self._config.get("llm"),
            "ledger_service": self._config.get("ledger_service"),
            "payload_store": self._config.get("payload_store"),
            "recall_top_k": self._config.get("recall_top_k", 5),
            "max_free_text_chars": self._config.get("max_free_text_chars", 12000),
            "required_evidence_by_alert_type": self._config.get("required_evidence_by_alert_type"),
            "evidence_max_age_days": self._config.get("evidence_max_age_days"),
            "trusted_scope": self._config.get("trusted_scope"),
            "request_session_id": self._config.get("request_session_id"),
            "feedback_receipt_store": self._config.get("feedback_receipt_store"),
            "memory_enabled": False,
            "hitl": {"enabled": False},
        }

    def extract_input(self, state: AgentState) -> str:
        scope = state.get("scope") if isinstance(state.get("scope"), dict) else {}
        envelope = {
            "mode": _safe(state.get("mode", "")),
            "scope": {
                "institution_id": _safe(scope.get("institution_id", "")),
                "unit_id": _safe(scope.get("unit_id", "")),
            },
            "case_ref": _safe(state.get("case_ref", "")),
            "payload_ref": _safe(state.get("payload_ref", "")),
            "as_of_date": _safe(state.get("as_of_date", "")),
            "evidence_refs": [
                {"source_id": _safe(item.get("source_id", "")), "kind": _safe(item.get("kind", ""))}
                for item in state.get("evidence_refs", [])
                if isinstance(item, dict)
            ],
        }
        return json.dumps(envelope, ensure_ascii=False, default=str, sort_keys=True)


# Compatibility name retained for the scaffold's unit-test import path.
MainNode = DomainWorkflowGraphNode


def _safe(value: Any) -> str:
    return _SAFE_VALUE.sub("", str(value))
