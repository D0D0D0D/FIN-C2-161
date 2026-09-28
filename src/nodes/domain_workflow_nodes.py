"""Domain nodes composing the FIN-C2-161 Cat2 inner workflow.

Raw caller text is resolved only inside a node from the scoped payload store;
no node returns that text into graph state.
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import date as CalendarDate
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar, Mapping

from framework.errors import SecurityViolationError
from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.security.credential_detector import detect_credentials
from shared.security.pii_detector import detect_pii
from shared.utils.audit_logger import emit_trace_event
from src.evidence_gate import EvidenceGateNode, FindingField, ProvenanceContract, project_typed_finding
from src.feedback_intake import FeedbackIntakeNode, FeedbackIntakeService
from src.ingest_row_pipeline.row_pipeline import IngestRowPipelineNode, RowPipelineHooks
from src.llm_draft_stage import DraftStageHooks, LlmDraftStageNode
from src.scoped_retrieval import ScopedRetrievalNode


_EVIDENCE_KINDS = frozenset(
    {"transaction_record", "kyc_document", "inquiry_response", "attachment_reference", "prior_perspective"}
)
_REQUIRED_BY_ALERT = {
    "cash": ("transaction_record", "kyc_document"),
    "sanctions": ("transaction_record", "inquiry_response"),
    "transaction_monitoring": ("transaction_record",),
    "kyc": ("kyc_document",),
}
_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")
_DATE = re.compile(r"\d{4}(?:[-/]\d{1,2}(?:[-/]\d{1,2})?|年\d{1,2}月\d{1,2}日)")
# A monetary token may contain chained Japanese units. A single optional unit
# is insufficient: ``3千万円`` means 30,000,000, not 3,000.
_NUMBER = re.compile(
    r"\d[\d,]*(?:\.\d+)?(?:\s*[億万千]+(?:\s*\d[\d,]*(?:\.\d+)?)?)*\s*(?:円|JPY)?",
    re.IGNORECASE,
)
_TRANSACTION = re.compile(r"(?:取引\s*ID|取引番号|transaction[_ ]?id)\s*[:#：]?\s*([A-Za-z0-9-]+)", re.IGNORECASE)
_KANJI_DIGITS = str.maketrans({"〇": "0", "零": "0", "一": "1", "二": "2", "三": "3", "四": "4", "五": "5", "六": "6", "七": "7", "八": "8", "九": "9"})

_DRAFT_FIELDS = (
    FindingField("facts", list, required=True, item_type=str),
    FindingField("evidence_sources", list, required=True, item_type=dict),
    FindingField("unknowns", list, required=True, item_type=str),
    FindingField("countervailing_factors", list, required=True, item_type=str),
    FindingField("memo_draft", str, required=True),
    FindingField("requires_human_review", bool, required=True),
    FindingField("review_reasons", list, required=True, item_type=str),
    FindingField("evidence_map", list, required=True, item_type=dict),
)
_SOURCE_FIELDS = (
    FindingField("source_id", str, required=True),
    FindingField("kind", str, required=True, allowed_values=tuple(sorted(_EVIDENCE_KINDS))),
    FindingField("label", str, required=True),
)
_CLAIM_FIELDS = (
    FindingField("claim_id", str, required=True),
    FindingField("claim", str, required=True),
    FindingField("source_ids", list, required=True, item_type=str),
)


class EnvelopeRestoreNode(FunctionNode):
    """Restore only the JSON control envelope passed by the outer GraphNode."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("stage_skipped_upstream_error", {"stage": "restore"}, state)
            return {}
        try:
            envelope = json.loads(state.get("user_input", ""))
            scope = envelope["scope"]
            if (
                set(envelope) != {"mode", "scope", "case_ref", "payload_ref", "as_of_date", "evidence_refs"}
                or envelope["mode"] not in {"ingest", "invoke", "feedback"}
                or not isinstance(scope, dict)
                or set(scope) != {"institution_id", "unit_id"}
            ):
                raise ValueError("inner_envelope_invalid")
        except (TypeError, ValueError, json.JSONDecodeError, KeyError):
            emit_trace_event("input_rejected", {"reason": "inner_envelope_invalid"}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": ["EnvelopeRestoreNode: inner_envelope_invalid"]}
        emit_trace_event("inner_envelope_restored", {"mode": envelope["mode"]}, state)
        result = {
            "mode": envelope["mode"],
            "scope": dict(scope),
            "institution_id": scope["institution_id"],
            "unit_id": scope["unit_id"],
            "case_ref": envelope["case_ref"],
            "payload_ref": envelope["payload_ref"],
            "as_of_date": envelope["as_of_date"],
            "evidence_refs": envelope["evidence_refs"],
            "status": AgentStatus.SUCCESS.value,
        }
        if envelope["mode"] == "feedback":
            result["feedback_payload_ref"] = envelope["payload_ref"]
        return result


class EvidenceCompletenessNode(FunctionNode):
    """Stage 1: deterministic evidence inventory; it makes no disposition."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(
        self,
        *,
        payload_store: Any,
        required_evidence_by_alert_type: Any = None,
        evidence_max_age_days: Any = None,
    ) -> None:
        super().__init__()
        self._payload_store = payload_store
        self._required_by_alert = _required_evidence_config(required_evidence_by_alert_type)
        self._evidence_max_age_days = _evidence_max_age_days(evidence_max_age_days)

    def execute(self, state: dict) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("stage_skipped_upstream_error", {"stage": "evidence_check"}, state)
            return {}
        try:
            payload = _payload_from_state(state, self._payload_store)
        except ValueError as exc:
            emit_trace_event("input_rejected", {"reason": str(exc)}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": [f"EvidenceCompletenessNode: {exc}"]}

        alert_type = payload.get("alert_type") if isinstance(payload.get("alert_type"), str) else "transaction_monitoring"
        present = _evidence_records(payload.get("evidence_refs"))
        required = self._required_by_alert.get(alert_type, self._required_by_alert["transaction_monitoring"])
        kinds = {item["kind"] for item in present}
        gaps = [{"reason": "required_evidence_kind_missing", "kind": kind} for kind in required if kind not in kinds]
        if self._evidence_max_age_days is not None:
            as_of_date = _parse_evidence_date(state.get("as_of_date"))
            if as_of_date is None:
                return {"status": AgentStatus.ERROR.value, "error_log": ["EvidenceCompletenessNode: as_of_date_invalid"]}
            for evidence in present:
                evidence_date = _parse_evidence_date(evidence.get("date"))
                if evidence_date is None:
                    gaps.append(
                        {
                            "reason": "evidence_date_unverifiable",
                            "source_id": evidence["source_id"],
                            "kind": evidence["kind"],
                        }
                    )
                    continue
                age_days = (as_of_date - evidence_date).days
                if age_days > self._evidence_max_age_days:
                    gaps.append(
                        {
                            "reason": "evidence_stale",
                            "source_id": evidence["source_id"],
                            "kind": evidence["kind"],
                            "age_days": age_days,
                        }
                    )
        evidence_check = {"evidence_present": present, "evidence_gap": gaps}
        emit_trace_event(
            "evidence_check_completed", {"present_count": len(present), "gap_count": len(gaps)}, state
        )
        return {"evidence_check": evidence_check, "status": AgentStatus.SUCCESS.value}


class LedgerPerspectiveBackend:
    """Adapt the scoped ledger to the v0.3 retrieval backend contract."""

    def __init__(self, ledger_service: Any) -> None:
        self._ledger_service = ledger_service

    def search(self, *, scope: Mapping[str, str], query: str, top_k: int) -> list[dict[str, Any]]:
        query_terms = _retrieval_terms(query)
        rows = self._ledger_service.search(scope, record_kind="disposition_record")
        ranked: list[tuple[int, dict[str, Any]]] = []
        for row in rows:
            searchable = " ".join(
                [
                    str(row.get("retrieval_text", "")),
                    _flatten_text(row.get("classification", {})),
                    _flatten_text(row.get("provenance", {})),
                ]
            )
            score = len(query_terms & _retrieval_terms(searchable))
            if query_terms and score == 0:
                continue
            stored_scope = row.get("scope") if isinstance(row.get("scope"), dict) else {}
            provenance = row.get("provenance") if isinstance(row.get("provenance"), dict) else {}
            hit = {
                "source_id": row.get("record_id", ""),
                "source_tier": "internal_kb",
                "kind": "prior_perspective",
                "score": score,
                "institution_id": stored_scope.get("institution_id"),
                "checked_questions": _safe_text_list(provenance.get("checked_questions")),
                "evidence_categories": _safe_text_list(provenance.get("evidence_categories")),
                "countervailing_notes": _safe_text_list(provenance.get("countervailing_notes")),
                "quality_flags": _safe_text_list(provenance.get("quality_flags")),
                "recorded_at": row.get("recorded_at", ""),
                "unit_id": row.get("unit_id", ""),
            }
            ranked.append((score, hit))
        ranked.sort(key=lambda item: (-item[0], str(item[1]["source_id"])))
        if not ranked:
            return []
        best_score = ranked[0][0]
        return [hit for score, hit in ranked if score == best_score][:top_k]


class SimilarPerspectiveRecallNode(ScopedRetrievalNode):
    """Stage 2: query-aware scoped recall plus a second safe projection."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(
        self,
        *,
        ledger_service: Any,
        payload_store: Any,
        top_k: int,
        max_free_text_chars: int,
    ) -> None:
        super().__init__(
            scope_keys=("institution_id",),
            backend=LedgerPerspectiveBackend(ledger_service),
            normalize_query=_normalize_retrieval_query,
            top_k=top_k,
        )
        self._payload_store = payload_store
        self._max_free_text_chars = max_free_text_chars

    def execute(self, state: dict) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("stage_skipped_upstream_error", {"stage": "perspective_recall"}, state)
            return {}
        scope = state.get("scope")
        if not isinstance(scope, dict) or not isinstance(scope.get("institution_id"), str):
            return {"status": AgentStatus.ERROR.value, "error_log": ["SimilarPerspectiveRecallNode: scope_missing"]}
        try:
            query = _build_retrieval_query(
                state,
                self._payload_store,
                max_free_text_chars=self._max_free_text_chars,
            )
            retrieved = super().execute(
                {
                    **state,
                    "institution_id": scope["institution_id"],
                    "query": query,
                }
            )
        except (SecurityViolationError, ValueError):
            emit_trace_event("degraded", {"degradation_reason": "retrieval_failed"}, state)
            return {
                "recalled_perspectives": [],
                "status": AgentStatus.SUCCESS.value,
                "degradation_reason": "retrieval_failed",
            }
        if retrieved.get("status") == AgentStatus.ERROR.value:
            return retrieved
        perspectives = [_project_prior_record(row) for row in retrieved.get("retrieved_records", [])]
        degradation = "" if perspectives else "no_prior_records"
        emit_trace_event("perspectives_recalled", {"count": len(perspectives)}, state)
        result: dict[str, Any] = {"recalled_perspectives": perspectives, "status": AgentStatus.SUCCESS.value}
        if degradation:
            result["degradation_reason"] = degradation
            emit_trace_event("degraded", {"degradation_reason": degradation}, state)
        return result


class DispositionMemoDraftNode(LlmDraftStageNode):
    """Stage 3: the only LLM stage, with a deterministic safe fallback."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, config: dict[str, Any], payload_store: Any) -> None:
        self._payload_store_for_hooks = payload_store
        max_chars = config.get("max_free_text_chars", 12000)
        hooks = DraftStageHooks(
            build_prompt=lambda state: _build_prompt(state, payload_store, max_free_text_chars=max_chars),
            build_fallback=_build_fallback,
            build_evidence=_build_evidence,
        )
        super().__init__(config=config, hooks=hooks)

    def execute(self, state: dict) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("stage_skipped_upstream_error", {"stage": "memo_draft"}, state)
            return {}
        result = super().execute(state)
        if result.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("memo_drafted", {"outcome": "rejected"}, state)
            return result
        draft = result.get("draft")
        if not isinstance(draft, dict):
            return {"status": AgentStatus.ERROR.value, "error_log": ["DispositionMemoDraftNode: draft_not_object"]}
        try:
            projected_draft = _project_draft(draft)
        except SecurityViolationError:
            emit_trace_event("memo_drafted", {"outcome": "contract_rejected"}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_code": "llm_output_contract_failed",
                "error_log": ["DispositionMemoDraftNode: llm_output_contract_failed"],
            }
        result = {**result, "draft": projected_draft}
        sections = _memo_sections(projected_draft)
        reasons = list(projected_draft["review_reasons"])
        requires_review = projected_draft["requires_human_review"]
        if result.get("degradation_reason"):
            requires_review = True
            reasons = sorted(set([*reasons, "llm_unavailable"]))
            emit_trace_event("degraded", {"degradation_reason": result["degradation_reason"]}, state)
        emit_trace_event("memo_drafted", {"requires_human_review": requires_review}, state)
        return {
            **result,
            "memo_sections": sections,
            "requires_human_review": requires_review,
            "review_reasons": reasons,
        }


class EvidenceLinkVerifyNode(EvidenceGateNode):
    """Stage 4: validate source identity, attributes, and free-text coverage."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self) -> None:
        super().__init__(
            contract=ProvenanceContract(
                gate_kind="draft",
                cited_ids_key="cited_source_ids",
                retrieved_records_key="retrieved_records",
                evidence_map_key="evidence_map",
                record_id_keys=("source_id",),
                claim_source_keys=("source_ids",),
                passthrough_keys=("memo_sections", "requires_human_review", "review_reasons", "degradation_reason"),
            )
        )

    def execute(self, state: dict) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("stage_skipped_upstream_error", {"stage": "evidence_verify"}, state)
            return {}
        sections = state.get("memo_sections")
        evidence_map = state.get("evidence_map")
        if not isinstance(sections, dict) or not isinstance(evidence_map, list):
            return {"status": AgentStatus.ERROR.value, "error_log": ["EvidenceLinkVerifyNode: evidence_contract_missing"]}
        registry = [
            *state.get("evidence_check", {}).get("evidence_present", []),
            *state.get("recalled_perspectives", []),
        ]
        if not _valid_output_source_kinds(sections):
            emit_trace_event("provenance_mismatch", {"kind": "source_kind", "count": 1}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": ["EvidenceLinkVerifyNode: source_kind_invalid"]}
        mismatch = _provenance_mismatch(evidence_map, registry)
        if mismatch:
            emit_trace_event("provenance_mismatch", {"kind": mismatch, "count": 1}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": [f"EvidenceLinkVerifyNode: {mismatch}"]}
        review_reasons = list(state.get("review_reasons", []))
        uncovered = _uncited_markers(sections, evidence_map)
        requires_review = bool(state.get("requires_human_review", False))
        if uncovered:
            requires_review = True
            review_reasons = sorted(set([*review_reasons, "uncited_free_text_marker"]))
            emit_trace_event("demoted_to_human_review", {"review_reasons": ["uncited_free_text_marker"]}, state)
        cited = sorted({sid for claim in evidence_map if isinstance(claim, dict) for sid in claim.get("source_ids", []) if isinstance(sid, str)})
        result: dict[str, Any] = {
            "gate_kind": "draft",
            "cited_source_ids": cited,
            "retrieved_records": registry,
            "evidence_map": evidence_map,
            "memo_sections": sections,
            "requires_human_review": requires_review,
            "review_reasons": review_reasons,
            "status": AgentStatus.SUCCESS.value,
        }
        if state.get("degradation_reason"):
            result["degradation_reason"] = state["degradation_reason"]
        emit_trace_event("evidence_verified", {"claim_count": len(evidence_map)}, state)
        return result


class _DecodedPayloadStore:
    """Adapter for feedback-intake: decode a scrubber vault without State writes."""

    def __init__(self, store: Any) -> None:
        self._store = store

    def resolve(self, ref: str | None, *, scope: Mapping[str, str], session_id: str, consume: bool = False) -> Any:
        bundle = self._store.resolve(ref, scope=scope, session_id=session_id, consume=consume)
        return _decode_vault_bundle(bundle)


class DispositionFeedbackNode(FeedbackIntakeNode):
    """Feedback part adapter returning the domain's closed feedback summary."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, service: FeedbackIntakeService, payload_store: Any) -> None:
        super().__init__(
            service=service,
            payload_store=_DecodedPayloadStore(payload_store),
            scope_keys=("institution_id",),
            payload_ref_key="feedback_payload_ref",
        )

    def execute(self, state: dict) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("stage_skipped_upstream_error", {"stage": "feedback"}, state)
            return {}
        result = super().execute(state)
        if result.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("feedback_applied", {"outcome": "rejected"}, state)
            return result
        feedback = result.get("feedback_result", {})
        summary = {"updated_ids": [feedback["record_id"]], "rejected": []}
        emit_trace_event("feedback_applied", {"updated_count": 1}, state)
        return {"feedback_summary": summary, "status": AgentStatus.SUCCESS.value}


class DispositionIngestNode(IngestRowPipelineNode):
    """Ingest part adapter with text quality gates and local ledger write hook."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL
    required_fields: ClassVar[tuple[str, ...]] = (
        "case_ref",
        "alert_type",
        "occurred_at",
        "verdict_code",
        "decided_by",
        "decided_at",
    )
    required_any_fields: ClassVar[tuple[tuple[str, ...], ...]] = (("body", "rationale"),)

    def __init__(
        self,
        *,
        payload_store: Any,
        ledger_service: Any,
        trusted_scope: Mapping[str, str],
        session_id: str,
        max_free_text_chars: int,
    ) -> None:
        self._ledger_service = ledger_service
        self._trusted_write_scope = dict(trusted_scope)
        self._max_free_text_chars = max_free_text_chars
        super().__init__(
            payload_store=payload_store,
            trusted_scope=trusted_scope,
            session_id=session_id,
            hooks=RowPipelineHooks(sanitize=self._sanitize, normalize=self._normalize, write=self._write),
        )

    def execute(self, state: dict) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("stage_skipped_upstream_error", {"stage": "ingest"}, state)
            return {}
        result = super().execute(state)
        summary = result.get("ingest_summary", {})
        emit_trace_event("disposition_ingested", {"written_count": summary.get("written_count", 0)}, state)
        return result

    def _sanitize(self, state: dict, row: dict[str, Any]) -> dict[str, Any]:
        for field in ("body", "rationale"):
            value = row.get(field)
            if value is not None:
                _assert_text_quality(value, max_free_text_chars=self._max_free_text_chars)
        return row

    @staticmethod
    def _normalize(state: dict, row: dict[str, Any]) -> dict[str, Any]:
        text = " ".join(
            str(item)
            for key in ("checked_questions", "evidence_categories", "countervailing_notes")
            for item in (row.get(key) if isinstance(row.get(key), list) else [])
        )
        if not text:
            text = "recorded-evidence-context"
        provenance = row.get("provenance", {})
        if not isinstance(provenance, dict):
            provenance = {}
        provenance = {
            **provenance,
            "checked_questions": _safe_text_list(row.get("checked_questions")),
            "evidence_categories": _safe_text_list(row.get("evidence_categories")),
            "countervailing_notes": _safe_text_list(row.get("countervailing_notes")),
            "quality_flags": _safe_text_list(row.get("quality_flags")),
        }
        return {
            **row,
            "record_kind": "disposition_record",
            "classification": {"alert_type": row["alert_type"]},
            "decision_source": "import",
            "retrieval_text": text,
            "provenance": provenance,
        }

    def _write(self, state: dict, row: dict[str, Any]) -> str:
        stored = self._ledger_service.write(self._trusted_write_scope, row, state=state)
        return stored["record_id"]


def _payload_from_state(state: Mapping[str, Any], payload_store: Any) -> dict[str, Any]:
    scope = state.get("scope")
    if not isinstance(scope, Mapping) or not isinstance(scope.get("institution_id"), str):
        raise ValueError("payload_scope_invalid")
    session_id = state.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("payload_session_invalid")
    try:
        bundle = payload_store.resolve(
            state.get("payload_ref"), scope={"institution_id": scope["institution_id"]}, session_id=session_id
        )
    except Exception:
        raise ValueError("payload_unresolvable") from None
    decoded = _decode_vault_bundle(bundle)
    if not isinstance(decoded, dict):
        raise ValueError("payload_invalid")
    return decoded


def _decode_vault_bundle(bundle: Any) -> Any:
    if isinstance(bundle, str):
        try:
            bundle = json.loads(bundle)
        except json.JSONDecodeError:
            raise ValueError("payload_invalid") from None
    if not isinstance(bundle, dict):
        raise ValueError("payload_invalid")
    extra = bundle.get("__extra__")
    raw = extra.get("payload.raw") if isinstance(extra, dict) else None
    if not isinstance(raw, str):
        raise ValueError("payload_invalid")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError("payload_invalid") from None


def _evidence_records(value: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not isinstance(value, list):
        return records
    for item in value:
        if not isinstance(item, dict) or not _IDENTIFIER.fullmatch(str(item.get("source_id", ""))):
            continue
        kind = item.get("kind")
        if kind not in _EVIDENCE_KINDS - {"prior_perspective"}:
            continue
        record = {key: item[key] for key in ("source_id", "kind", "amount", "date", "transaction_id", "account_last4") if key in item}
        records.append(record)
    return records


def _required_evidence_config(value: Any) -> dict[str, tuple[str, ...]]:
    """Accept a complete customer table or retain the reviewed default."""
    if not isinstance(value, dict) or not value:
        return dict(_REQUIRED_BY_ALERT)
    normalized: dict[str, tuple[str, ...]] = {}
    for alert_type, kinds in value.items():
        if not isinstance(alert_type, str) or not alert_type or not isinstance(kinds, list):
            return dict(_REQUIRED_BY_ALERT)
        if not kinds or any(not isinstance(kind, str) or kind not in _EVIDENCE_KINDS - {"prior_perspective"} for kind in kinds):
            return dict(_REQUIRED_BY_ALERT)
        normalized[alert_type] = tuple(kinds)
    if "transaction_monitoring" not in normalized:
        return dict(_REQUIRED_BY_ALERT)
    return normalized


def _evidence_max_age_days(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _parse_evidence_date(value: Any) -> CalendarDate | None:
    if not isinstance(value, str):
        return None
    normalized = unicodedata.normalize("NFKC", value).strip()
    match = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", normalized)
    if match is None:
        match = re.fullmatch(r"(\d{4})/(\d{1,2})/(\d{1,2})", normalized)
    if match is None:
        match = re.fullmatch(r"(\d{4})年(\d{1,2})月(\d{1,2})日", normalized)
    if match is None:
        return None
    try:
        return CalendarDate(*(int(part) for part in match.groups()))
    except ValueError:
        return None


def _project_prior_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """D-17 projection: no verdict, decision, body, rationale, or customer ID."""
    return {
        "source_id": record.get("source_id", ""),
        "checked_questions": _safe_text_list(record.get("checked_questions")),
        "evidence_categories": _safe_text_list(record.get("evidence_categories")),
        "countervailing_notes": _safe_text_list(record.get("countervailing_notes")),
        "quality_flags": _safe_text_list(record.get("quality_flags")),
        "recorded_at": record.get("recorded_at", ""),
        "unit_id": record.get("unit_id", ""),
    }


def _safe_text_list(value: Any) -> list[str]:
    return [item for item in value if isinstance(item, str) and len(item) <= 160] if isinstance(value, list) else []


def _build_retrieval_query(
    state: Mapping[str, Any],
    payload_store: Any,
    *,
    max_free_text_chars: int,
) -> str:
    """Build a transient business query; never persist raw case text in State."""

    payload = _payload_from_state(state, payload_store)
    free_text = payload.get("free_text", "")
    if not isinstance(free_text, str):
        free_text = ""
    if free_text:
        _assert_text_quality(free_text, max_free_text_chars=max_free_text_chars)
    safe_text, _redacted = _mask_untrusted_text(free_text)
    evidence_check = state.get("evidence_check") if isinstance(state.get("evidence_check"), dict) else {}
    business_terms = [
        str(payload.get("alert_type", "")),
        safe_text,
        *[
            str(item.get("kind", ""))
            for item in evidence_check.get("evidence_present", [])
            if isinstance(item, dict)
        ],
        *[
            str(item.get("kind") or item.get("reason") or "")
            for item in evidence_check.get("evidence_gap", [])
            if isinstance(item, dict)
        ],
    ]
    return " ".join(term for term in business_terms if term)


def _normalize_retrieval_query(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def _retrieval_terms(value: Any) -> set[str]:
    normalized = _normalize_retrieval_query(value)
    ascii_terms = {term for term in re.findall(r"[a-z0-9_]+", normalized) if len(term) >= 2}
    cjk_runs = re.findall(r"[\u3040-\u30ff\u4e00-\u9fff]+", normalized)
    cjk_terms = {
        run[index : index + 2]
        for run in cjk_runs
        for index in range(max(1, len(run) - 1))
        if len(run[index : index + 2]) == 2
    }
    return ascii_terms | cjk_terms


def _project_draft(value: Any) -> dict[str, Any]:
    projected = project_typed_finding(value, _DRAFT_FIELDS)
    projected["evidence_sources"] = [
        project_typed_finding(source, _SOURCE_FIELDS) for source in projected["evidence_sources"]
    ]
    projected["evidence_map"] = [
        project_typed_finding(claim, _CLAIM_FIELDS) for claim in projected["evidence_map"]
    ]
    return projected


def _response_contract(registry: list[dict[str, Any]]) -> dict[str, Any]:
    """Generate the prompt contract from the same declarations as validation."""

    contract = _field_contract(_DRAFT_FIELDS)
    contract["evidence_sources"]["items"] = _field_contract(_SOURCE_FIELDS)
    contract["evidence_map"]["items"] = _field_contract(_CLAIM_FIELDS)
    marker_types = list(_markers("").keys())
    sources_by_id = _registry_by_source_id(registry)
    markers_by_source = {
        source_id: _markers_for_sources([source]) for source_id, source in sources_by_id.items()
    }
    return {
        "type": "object",
        "additional_properties": False,
        "fields": contract,
        "marker_constraints": {
            "instruction": (
                "Marker values are validated after normalization. In each evidence_map claim, every normalized "
                "marker must be listed for one of that claim's source_ids in allowed_values_by_source. "
                "allowed_marker_forms_by_source shows source text forms that normalize to each listed value; copy "
                "one of those source forms or use its normalized_value without changing its meaning. Facts and "
                "memo_draft may only repeat markers already present in a valid evidence_map claim. Omit unsupported "
                "specific values."
            ),
            "marker_types": marker_types,
            "allowed_values_by_source": {
                source_id: {kind: sorted(values) for kind, values in markers.items()}
                for source_id, markers in markers_by_source.items()
            },
            "allowed_marker_forms_by_source": {
                source_id: _marker_forms_for_source(sources_by_id[source_id], markers)
                for source_id, markers in markers_by_source.items()
            },
            "field_constraints": {
                "evidence_map[].claim": {
                    "rule": "subset_of_cited_source_markers",
                    "source_ids_from": "evidence_map[].source_ids",
                    "allowed_values_from": "allowed_values_by_source",
                },
                "facts[]": {
                    "rule": "subset_of_evidence_map_claim_markers",
                    "allowed_values_from": "evidence_map[].claim",
                },
                "memo_draft": {
                    "rule": "subset_of_evidence_map_claim_markers",
                    "allowed_values_from": "evidence_map[].claim",
                },
            },
        },
    }


def _marker_forms_for_source(
    source: Mapping[str, Any],
    allowed_markers: Mapping[str, set[str]],
) -> dict[str, list[dict[str, Any]]]:
    """Map validator-normalized values back to readable source spellings."""

    forms: dict[str, dict[str, set[str]]] = {
        kind: {normalized: set() for normalized in values}
        for kind, values in allowed_markers.items()
    }
    for source_text in _flatten_scalar_text(source):
        for kind, normalized_values in _markers(source_text).items():
            for normalized in normalized_values & allowed_markers[kind]:
                forms[kind][normalized].add(source_text)
    return {
        kind: [
            {
                "normalized_value": normalized,
                "source_forms": sorted(values) if values else [normalized],
            }
            for normalized, values in sorted(normalized_forms.items())
        ]
        for kind, normalized_forms in forms.items()
    }


def _flatten_scalar_text(value: Any) -> list[str]:
    if isinstance(value, Mapping):
        return [text for item in value.values() for text in _flatten_scalar_text(item)]
    if isinstance(value, list):
        return [text for item in value for text in _flatten_scalar_text(item)]
    return [str(value)]


def _field_contract(fields: tuple[FindingField, ...]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for declared in fields:
        entry: dict[str, Any] = {
            "type": _declared_type_name(declared.expected_type),
            "required": declared.required,
        }
        if declared.item_type is not None:
            entry["item_type"] = _declared_type_name(declared.item_type)
        if declared.allowed_values:
            entry["enum"] = list(declared.allowed_values)
        result[declared.name] = entry
    return result


def _declared_type_name(value: type | tuple[type, ...]) -> str:
    declared = value if isinstance(value, tuple) else (value,)
    return " | ".join(item.__name__ for item in declared)


def _build_prompt(state: dict, payload_store: Any, *, max_free_text_chars: int = 12000) -> str:
    payload = _payload_from_state(state, payload_store)
    free_text = payload.get("free_text", "")
    if not isinstance(free_text, str):
        free_text = ""
    _assert_text_quality(free_text, max_free_text_chars=max_free_text_chars) if free_text else None
    safe_text, _ = _mask_untrusted_text(free_text)
    registry = [
        *state.get("evidence_check", {}).get("evidence_present", []),
        *state.get("recalled_perspectives", []),
    ]
    return json.dumps(
        {
            "instruction": (
                "Create an internal draft only. Do not make a suspicious/not-suspicious, STR, filing, or "
                "account-restriction decision. Prior records are investigation perspectives, not conclusions. "
                # "JSON" must stay in the prompt: the Marketplace adapter enables Azure JSON mode,
                # which the API refuses unless the messages mention json.
                "Respond with exactly one JSON object that follows response_contract."
            ),
            "case_ref": state.get("case_ref"),
            "evidence_check": state.get("evidence_check", {}),
            "prior_investigation_perspectives": state.get("recalled_perspectives", []),
            "case_notes": safe_text,
            "response_contract": _response_contract(registry),
        },
        ensure_ascii=False,
        default=str,
    )


def _build_fallback(state: dict) -> dict[str, Any]:
    check = state.get("evidence_check", {})
    evidence = check.get("evidence_present", []) if isinstance(check, dict) else []
    prior_perspective_questions = [
        f"過去案件の調査観点として今回も確認する: {note}"
        for record in state.get("recalled_perspectives", [])
        if isinstance(record, dict)
        for note in record.get("countervailing_notes", [])
        if isinstance(note, str) and note
    ]
    return {
        "facts": [],
        "evidence_sources": [
            {"source_id": item["source_id"], "kind": item["kind"], "label": "provided evidence reference"}
            for item in evidence
            if isinstance(item, dict) and item.get("source_id") and item.get("kind")
        ],
        "unknowns": [
            gap.get("reason", "evidence_gap")
            for gap in check.get("evidence_gap", [])
            if isinstance(gap, dict)
        ]
        + prior_perspective_questions,
        # A prior case is an investigation perspective, not evidence that the
        # same countervailing circumstance exists in the current case.  Keep
        # the perspective as an explicitly framed follow-up question above;
        # do not promote its wording into a current-case factual section.
        "countervailing_factors": [],
        "memo_draft": "",
        "requires_human_review": True,
        "review_reasons": ["llm_unavailable"],
        "evidence_map": [],
    }


def _build_evidence(state: dict, draft: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    projected = _project_draft(draft)
    cleaned = list(projected["evidence_map"])
    cited: set[str] = set()
    for claim in cleaned:
        cited.update(claim["source_ids"])
    return cleaned, sorted(cited)


def _memo_sections(draft: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "facts": list(draft["facts"]),
        "evidence_sources": list(draft["evidence_sources"]),
        "unknowns": list(draft["unknowns"]),
        "countervailing_factors": list(draft["countervailing_factors"]),
        "memo_draft": draft["memo_draft"],
    }


def _valid_output_source_kinds(sections: Mapping[str, Any]) -> bool:
    return all(
        isinstance(item.get("source_id"), str) and item.get("kind") in _EVIDENCE_KINDS
        for item in sections.get("evidence_sources", [])
        if isinstance(item, dict)
    )


def _provenance_mismatch(evidence_map: list[dict[str, Any]], registry: list[dict[str, Any]]) -> str | None:
    by_id = _registry_by_source_id(registry)
    for claim in evidence_map:
        if not isinstance(claim, dict):
            return "claim_invalid"
        ids = claim.get("source_ids")
        if not isinstance(ids, list) or not ids:
            return "claim_source_missing"
        sources = []
        for source_id in ids:
            if source_id not in by_id:
                return "source_unknown"
            sources.append(by_id[source_id])
        claim_text = str(claim.get("claim", ""))
        attributes = _markers(claim_text)
        source_attributes = _markers_for_sources(sources)
        for key, values in attributes.items():
            if not values <= source_attributes[key]:
                return f"{key}_mismatch"
    return None


def _registry_by_source_id(registry: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        record["source_id"]: record
        for record in registry
        if isinstance(record, dict) and isinstance(record.get("source_id"), str)
    }


def _markers_for_sources(sources: list[dict[str, Any]]) -> dict[str, set[str]]:
    """Extract source markers through the same path used by provenance validation."""

    return _markers(" ".join(_flatten_text(source) for source in sources))


def _uncited_markers(sections: Mapping[str, Any], evidence_map: list[dict[str, Any]]) -> bool:
    output_markers = _markers(" ".join([sections.get("memo_draft", ""), *sections.get("facts", [])]))
    claim_markers = _markers(" ".join(str(item.get("claim", "")) for item in evidence_map if isinstance(item, dict) and item.get("source_ids")))
    return any(not values <= claim_markers[key] for key, values in output_markers.items())


def _markers(text: str) -> dict[str, set[str]]:
    normalized = unicodedata.normalize("NFKC", text).translate(_KANJI_DIGITS)
    amounts = {_canonical_amount(match.group(0)) for match in _NUMBER.finditer(normalized)}
    amounts.discard(None)
    dates = {match.group(0).replace("年", "-").replace("月", "-").replace("日", "") for match in _DATE.finditer(normalized)}
    transactions = {match.group(1).casefold() for match in _TRANSACTION.finditer(normalized)}
    return {"amount": amounts, "date": dates, "transaction": transactions}


def _canonical_amount(token: str) -> str | None:
    """Normalize Japanese monetary units into one non-exponential value.

    Each numeric term owns all adjacent scale characters, so ``1千万円`` is
    1×千×万 and ``1億2千万円`` is 1×億 + 2×千×万. Both claim and source
    text reach this function through :func:`_markers`.
    """
    body = unicodedata.normalize("NFKC", token).translate(_KANJI_DIGITS)
    body = re.sub(r"\s+", "", body)
    body = re.sub(r"(?:円|JPY)$", "", body, flags=re.IGNORECASE)
    total = Decimal("0")
    position = 0
    for match in re.finditer(r"(\d[\d,]*(?:\.\d+)?)([億万千]*)", body):
        if match.start() != position:
            return None
        position = match.end()
        try:
            value = Decimal(match.group(1).replace(",", ""))
        except InvalidOperation:
            return None
        multiplier = Decimal("1")
        for unit in match.group(2):
            multiplier *= {"億": Decimal("100000000"), "万": Decimal("10000"), "千": Decimal("1000")}[unit]
        total += value * multiplier
    if position != len(body):
        return None
    if total == total.to_integral_value():
        return str(int(total))
    return format(total.normalize(), "f").rstrip("0").rstrip(".")


def _flatten_text(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(_flatten_text(item) for item in value.values())
    if isinstance(value, list):
        return " ".join(_flatten_text(item) for item in value)
    return str(value)


def _mask_untrusted_text(value: str) -> tuple[str, bool]:
    findings = [*detect_pii(value), *detect_credentials(value)]
    injection = any(marker in unicodedata.normalize("NFKC", value).casefold() for marker in ("ignore previous", "system prompt", "指示を無視"))
    if not findings and not injection:
        return value, False
    masked = value
    for finding in sorted(findings, key=lambda item: item["start"], reverse=True):
        masked = masked[: finding["start"]] + "[MASKED]" + masked[finding["end"] :]
    return ("[UNTRUSTED_CONTENT_REDACTED]" if injection else masked), True


def _assert_text_quality(value: Any, *, max_free_text_chars: int = 12000) -> None:
    if not isinstance(value, str) or len(value) < 5:
        raise ValueError("quality_min_length")
    if len(value) > max_free_text_chars:
        raise ValueError("quality_max_length")
    length = len(value)
    if value.count("\ufffd") / length > 0.01:
        raise ValueError("quality_replacement_character")
    controls = sum(ord(char) < 32 and char not in "\t\n" for char in value)
    if controls / length > 0.01:
        raise ValueError("quality_control_character")
    readable = sum(char.isalnum() or "\u3040" <= char <= "\u30ff" or "\u4e00" <= char <= "\u9fff" or char.isspace() or char in "、。,.:-()[]" for char in value)
    if readable / length < 0.6:
        raise ValueError("quality_unreadable")
    if re.search(r"(.)\1{19,}", value):
        raise ValueError("quality_repeated_character")
