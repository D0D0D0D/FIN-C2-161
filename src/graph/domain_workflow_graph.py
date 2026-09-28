"""Inner Cat2 workflow for AML disposition memo drafting."""

from __future__ import annotations

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_status import AgentStatus
from src.feedback_intake import FeedbackIntakeService
from src.nodes.domain_workflow_nodes import (
    DispositionFeedbackNode,
    DispositionIngestNode,
    DispositionMemoDraftNode,
    EnvelopeRestoreNode,
    EvidenceCompletenessNode,
    EvidenceLinkVerifyNode,
    SimilarPerspectiveRecallNode,
)
from src.schemas.state import AMLDispositionState


class AMLDispositionWorkflowGraph(BaseGraph):
    """Mode-routed inner workflow; all paths consume a restored safe envelope."""

    @property
    def name(self) -> str:
        return "aml_disposition_workflow"

    @property
    def state_schema(self) -> type:
        return AMLDispositionState

    def _validate_config(self) -> None:
        required = (
            "ledger_service",
            "payload_store",
            "recall_top_k",
            "max_free_text_chars",
            "trusted_scope",
            "request_session_id",
            "feedback_receipt_store",
        )
        missing = [key for key in required if self.config.get(key) is None]
        if missing:
            raise ValueError(f"AMLDispositionWorkflowGraph: missing config {missing}")
        if not isinstance(self.config["recall_top_k"], int) or self.config["recall_top_k"] < 1:
            raise ValueError("AMLDispositionWorkflowGraph: recall_top_k must be a positive integer")
        if not isinstance(self.config["max_free_text_chars"], int) or self.config["max_free_text_chars"] < 5:
            raise ValueError("AMLDispositionWorkflowGraph: max_free_text_chars must be at least five")

    def register_nodes(self) -> None:
        ledger = self.config["ledger_service"]
        payload_store = self.config["payload_store"]
        feedback_service = FeedbackIntakeService(
            ledger=ledger,
            receipt_store=self.config["feedback_receipt_store"],
            domain_fields={"unit_id": "scalar", "case_ref": "scalar"},
        )
        self._nodes = {
            "restore": EnvelopeRestoreNode(),
            "evidence_check": EvidenceCompletenessNode(
                payload_store=payload_store,
                required_evidence_by_alert_type=self.config.get("required_evidence_by_alert_type"),
                evidence_max_age_days=self.config.get("evidence_max_age_days"),
            ),
            "perspective_recall": SimilarPerspectiveRecallNode(
                ledger_service=ledger,
                payload_store=payload_store,
                top_k=self.config["recall_top_k"],
                max_free_text_chars=self.config["max_free_text_chars"],
            ),
            "memo_draft": DispositionMemoDraftNode(
                config={"llm": self.config.get("llm"), "max_free_text_chars": self.config["max_free_text_chars"]},
                payload_store=payload_store,
            ),
            "evidence_verify": EvidenceLinkVerifyNode(),
            "ingest": DispositionIngestNode(
                payload_store=payload_store,
                ledger_service=ledger,
                trusted_scope=self.config["trusted_scope"],
                session_id=self.config["request_session_id"],
                max_free_text_chars=self.config["max_free_text_chars"],
            ),
            "feedback": DispositionFeedbackNode(service=feedback_service, payload_store=payload_store),
        }

    def add_edges(self) -> None:
        self._sg.add_edge(START, "restore")
        self._sg.add_conditional_edges("restore", self._route_mode)
        self._sg.add_conditional_edges("evidence_check", self._after_evidence_check)
        self._sg.add_conditional_edges("perspective_recall", self._after_recall)
        self._sg.add_conditional_edges("memo_draft", self._after_draft)
        self._sg.add_edge("evidence_verify", END)
        self._sg.add_edge("ingest", END)
        self._sg.add_edge("feedback", END)

    def route(self, state: AMLDispositionState) -> str:
        return self._route_mode(state)

    @staticmethod
    def _route_mode(state: AMLDispositionState) -> str:
        if state.get("status") == AgentStatus.ERROR.value:
            return END
        return {"invoke": "evidence_check", "ingest": "ingest", "feedback": "feedback"}.get(state.get("mode"), END)

    @staticmethod
    def _after_evidence_check(state: AMLDispositionState) -> str:
        return END if state.get("status") == AgentStatus.ERROR.value else "perspective_recall"

    @staticmethod
    def _after_recall(state: AMLDispositionState) -> str:
        return END if state.get("status") == AgentStatus.ERROR.value else "memo_draft"

    @staticmethod
    def _after_draft(state: AMLDispositionState) -> str:
        return END if state.get("status") == AgentStatus.ERROR.value else "evidence_verify"

    def get_output(self, state: AMLDispositionState) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            return {"status": AgentStatus.ERROR.value, "error_log": state.get("error_log", [])}
        mode = state.get("mode")
        if mode == "ingest":
            return {"mode": mode, "ingest_summary": state.get("ingest_summary", {}), "status": state.get("status")}
        if mode == "feedback":
            return {"mode": mode, "feedback_summary": state.get("feedback_summary", {}), "status": state.get("status")}
        sections = state.get("memo_sections", {})
        return {
            "mode": "invoke",
            "case_ref": state.get("case_ref", ""),
            "facts": sections.get("facts", []),
            "evidence_sources": sections.get("evidence_sources", []),
            "unknowns": sections.get("unknowns", []),
            "countervailing_factors": sections.get("countervailing_factors", []),
            "memo_draft": sections.get("memo_draft", ""),
            "evidence_map": state.get("evidence_map", []),
            "requires_human_review": bool(state.get("requires_human_review", False)),
            "review_reasons": state.get("review_reasons", []),
            "degradation_reason": state.get("degradation_reason", ""),
            "status": state.get("status", AgentStatus.SUCCESS.value),
        }
