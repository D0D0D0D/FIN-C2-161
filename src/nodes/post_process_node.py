"""Closed output envelope and S-3 controls for FIN-C2-161."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, ClassVar

from framework.errors import SecurityViolationError
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.security.credential_detector import detect_credentials
from shared.security.pii_detector import detect_pii
from shared.utils.audit_logger import emit_trace_event
from src.output_envelope import OutputEnvelopeNode

# Keep in step with pyproject.toml / config/agent.yaml (pinned by a unit test).
TEMPLATE_VERSION = "1.0.0"


DISCLAIMER = "本ドラフトは AI が生成した下書きであり、最終判断は担当者が行います。"
_INVOKE_KEYS = frozenset(
    {
        "mode", "gate_kind", "case_ref", "facts", "evidence_sources", "unknowns", "countervailing_factors",
        "memo_draft", "evidence_map", "requires_human_review", "review_reasons", "degradation_reason",
        "disclaimer", "generation_metadata",
    }
)
_INGEST_KEYS = frozenset({"mode", "gate_kind", "ingest_summary", "degradation_reason"})
_FEEDBACK_KEYS = frozenset({"mode", "gate_kind", "feedback_summary", "degradation_reason"})
_SOURCE_KINDS = frozenset(
    {"transaction_record", "kyc_document", "inquiry_response", "attachment_reference", "prior_perspective"}
)


class PostProcessNode(OutputEnvelopeNode):
    """Template adaptation of output-envelope v0.1.2."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL
    required_fields_by_mode: ClassVar[dict[str, tuple[str, ...]]] = {
        "invoke": tuple(_INVOKE_KEYS),
        "ingest": tuple(_INGEST_KEYS),
        "feedback": tuple(_FEEDBACK_KEYS),
    }
    gate_kind_by_mode: ClassVar[dict[str, str]] = {"invoke": "draft", "ingest": "ingest", "feedback": "feedback"}
    max_field_chars: ClassVar[int | None] = None

    def execute(self, state: dict) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("stage_skipped_upstream_error", {"stage": "post_process"}, state)
            return super().execute(state)
        if state.get("mode") == "invoke" and not _state_evidence_map_is_consistent(state):
            emit_trace_event("output_rejected", {"reason": "state_evidence_inconsistent"}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": ["PostProcessNode: state_evidence_inconsistent"]}
        emit_trace_event("output_envelope_requested", {"mode": state.get("mode", "")}, state)
        return super().execute(state)

    def format_invoke(self, state: dict) -> dict[str, Any]:
        source_ids = sorted(
            {
                source_id
                for claim in state.get("evidence_map", [])
                if isinstance(claim, dict)
                for source_id in claim.get("source_ids", [])
                if isinstance(source_id, str)
            }
        )
        return {
            "case_ref": state.get("case_ref", ""),
            "facts": state.get("facts", []),
            "evidence_sources": state.get("evidence_sources", []),
            "unknowns": state.get("unknowns", []),
            "countervailing_factors": state.get("countervailing_factors", []),
            "memo_draft": state.get("memo_draft", ""),
            "evidence_map": state.get("evidence_map", []),
            "requires_human_review": bool(state.get("requires_human_review", False)),
            "review_reasons": state.get("review_reasons", []),
            "degradation_reason": state.get("degradation_reason", ""),
            "disclaimer": DISCLAIMER,
            "generation_metadata": {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "template_id": "FIN-C2-161",
                "template_version": TEMPLATE_VERSION,
                "source_ids": source_ids,
            },
        }

    def format_ingest(self, state: dict) -> dict[str, Any]:
        return {"ingest_summary": state.get("ingest_summary", {}), "degradation_reason": state.get("degradation_reason", "")}

    def format_feedback(self, state: dict) -> dict[str, Any]:
        return {"feedback_summary": state.get("feedback_summary", {}), "degradation_reason": state.get("degradation_reason", "")}

    def _fold_optional_state_fields(self, state: dict, formatted: dict[str, Any]) -> None:
        # The per-mode formatters always declare degradation_reason; do not fold
        # generic framework fields into this closed contract.
        formatted["degradation_reason"] = state.get("degradation_reason", formatted.get("degradation_reason", ""))

    def _extra_security_gate_output(self, result: dict) -> dict:
        result = super()._extra_security_gate_output(result)
        formatted = result["formatted_output"]
        if formatted.get("gate_kind") == "error":
            return result
        expected = {"invoke": _INVOKE_KEYS, "ingest": _INGEST_KEYS, "feedback": _FEEDBACK_KEYS}.get(formatted.get("mode"))
        if expected is None or set(formatted) != expected:
            emit_trace_event("output_rejected", {"reason": "closed_allowlist"}, result)
            raise SecurityViolationError("S-3 closed allowlist rejected output")
        if formatted["mode"] == "invoke":
            if not _invoke_shape_is_safe(formatted):
                emit_trace_event("output_rejected", {"reason": "invoke_schema"}, result)
                raise SecurityViolationError("S-3 invoke schema rejected output")
            evidence_ids = {item["source_id"] for item in formatted["evidence_sources"]}
            mapped_ids = {sid for claim in formatted["evidence_map"] for sid in claim["source_ids"]}
            if not mapped_ids <= evidence_ids:
                emit_trace_event("output_rejected", {"reason": "envelope_reference"}, result)
                raise SecurityViolationError("S-3 evidence_map references an undeclared output source")
        return result

    def _sanitize_client_echoes(self, formatted: dict[str, Any]) -> dict[str, Any]:
        return _mask_free_text(formatted)


def _state_evidence_map_is_consistent(state: dict) -> bool:
    sources = {item.get("source_id") for item in state.get("evidence_sources", []) if isinstance(item, dict)}
    return all(
        isinstance(claim, dict) and isinstance(claim.get("source_ids"), list) and set(claim["source_ids"]) <= sources
        for claim in state.get("evidence_map", [])
    )


def _invoke_shape_is_safe(value: dict[str, Any]) -> bool:
    if not isinstance(value["case_ref"], str) or not isinstance(value["memo_draft"], str):
        return False
    if not all(isinstance(value[key], list) and all(isinstance(item, str) for item in value[key]) for key in ("facts", "unknowns", "countervailing_factors", "review_reasons")):
        return False
    if not isinstance(value["requires_human_review"], bool) or value["disclaimer"] != DISCLAIMER:
        return False
    metadata = value["generation_metadata"]
    if not isinstance(metadata, dict) or set(metadata) != {"generated_at", "template_id", "template_version", "source_ids"}:
        return False
    if not isinstance(metadata["source_ids"], list) or not all(isinstance(item, str) for item in metadata["source_ids"]):
        return False
    for source in value["evidence_sources"]:
        if not isinstance(source, dict) or set(source) != {"source_id", "kind", "label"}:
            return False
        if not isinstance(source["source_id"], str) or source["kind"] not in _SOURCE_KINDS or not isinstance(source["label"], str):
            return False
    for claim in value["evidence_map"]:
        if not isinstance(claim, dict) or set(claim) != {"claim_id", "claim", "source_ids"}:
            return False
        if not isinstance(claim["claim_id"], str) or not isinstance(claim["claim"], str) or not isinstance(claim["source_ids"], list):
            return False
        if not all(isinstance(item, str) for item in claim["source_ids"]):
            return False
    return True


def _mask_free_text(value: Any, path: tuple[str, ...] = ()) -> Any:
    if isinstance(value, dict):
        if any(detect_pii(str(key)) or detect_credentials(str(key)) for key in value):
            raise SecurityViolationError("S-3 unsafe key name")
        return {key: _mask_free_text(item, path + (key,)) for key, item in value.items()}
    if isinstance(value, list):
        return [_mask_free_text(item, path + ("*",)) for item in value]
    if not isinstance(value, str):
        return value
    findings = [*detect_pii(value), *detect_credentials(value)]
    if not findings:
        return value
    if path and path[-1] in {"source_id", "claim_id", "case_ref", "generated_at", "template_id", "template_version"}:
        raise SecurityViolationError("S-3 unsafe identifier")
    masked = value
    for finding in sorted(findings, key=lambda item: item["start"], reverse=True):
        masked = masked[: finding["start"]] + "[MASKED]" + masked[finding["end"] :]
    return masked
