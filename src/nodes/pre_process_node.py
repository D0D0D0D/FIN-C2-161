"""Trusted envelope validation for the AML disposition workflow."""

from __future__ import annotations

import json
import re
from typing import Any, ClassVar

from framework.errors import SecurityViolationError
from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.payload_store import is_reference


_MODES = frozenset({"ingest", "invoke", "feedback"})
_ENVELOPE_KEYS = frozenset({"mode", "case_ref", "payload_ref", "institution_id", "unit_id", "evidence_refs", "payload"})
_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,63}$")


class PreProcessNode(FunctionNode):
    """Accept only the server-minted, checkpoint-safe invocation envelope."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("stage_skipped_upstream_error", {"stage": "pre_process"}, state)
            return {}
        try:
            envelope = json.loads(state.get("user_input", ""))
        except (TypeError, json.JSONDecodeError) as exc:
            return self._reject("invalid_envelope", state, exc)
        if not isinstance(envelope, dict):
            return self._reject("invalid_envelope", state)

        scope = state.get("input_context", {}).get("scope")
        if not isinstance(scope, dict) or set(scope) != {"institution_id", "unit_id"}:
            return self._reject("scope_missing", state)
        if not all(_clean_identifier(value) for value in scope.values()):
            return self._reject("scope_invalid", state)
        if set(envelope) - _ENVELOPE_KEYS:
            return self._reject("undeclared_input_key", state)
        expected_payload = {} if envelope.get("mode") == "ingest" else {"raw": "[scrubbed:payload.raw]"}
        if envelope.get("payload") != expected_payload:
            return self._reject("payload_envelope_invalid", state)
        if envelope.get("mode") not in _MODES:
            return self._reject("mode_invalid", state)
        if envelope.get("institution_id") != scope["institution_id"] or envelope.get("unit_id") != scope["unit_id"]:
            return self._reject("scope_mismatch", state)
        payload_ref = envelope.get("payload_ref")
        if not is_reference(payload_ref):
            return self._reject("payload_ref_untrusted", state)
        case_ref = envelope.get("case_ref", "")
        if envelope["mode"] in {"invoke", "feedback"} and not _clean_identifier(case_ref):
            return self._reject("case_ref_invalid", state)

        validated = {
            "mode": envelope["mode"],
            "scope": {"institution_id": scope["institution_id"], "unit_id": scope["unit_id"]},
            "case_ref": case_ref if isinstance(case_ref, str) else "",
            "payload_ref": payload_ref,
            "evidence_refs": _safe_evidence_refs(envelope.get("evidence_refs")),
        }
        as_of_date = state.get("input_context", {}).get("as_of_date")
        if isinstance(as_of_date, str):
            validated["as_of_date"] = as_of_date
        emit_trace_event("input_validated", {"mode": envelope["mode"]}, state)
        return {
            "mode": validated["mode"],
            "scope": validated["scope"],
            "case_ref": validated["case_ref"],
            "payload_ref": validated["payload_ref"],
            **({"as_of_date": validated["as_of_date"]} if "as_of_date" in validated else {}),
            "validated_input": json.dumps(validated, ensure_ascii=False, sort_keys=True),
            "status": AgentStatus.SUCCESS.value,
        }

    def _extra_security_gate_input(self, state: dict) -> dict:
        # The framework's final S-2 hook calls this before execute.  All
        # control-plane failures raise SecurityViolationError so main is never
        # invoked on malformed, unscoped, or client-forged envelopes.
        try:
            envelope = json.loads(state.get("user_input", ""))
            scope = state.get("input_context", {}).get("scope")
            if not isinstance(envelope, dict) or not isinstance(scope, dict):
                raise ValueError("input_context_invalid")
            if set(scope) != {"institution_id", "unit_id"} or not all(_clean_identifier(value) for value in scope.values()):
                raise ValueError("scope_invalid")
            if set(envelope) - _ENVELOPE_KEYS:
                raise ValueError("undeclared_input_key")
            if envelope.get("mode") not in _MODES:
                raise ValueError("mode_invalid")
            if envelope.get("institution_id") != scope["institution_id"] or envelope.get("unit_id") != scope["unit_id"]:
                raise ValueError("scope_mismatch")
            if not is_reference(envelope.get("payload_ref")):
                raise ValueError("payload_ref_untrusted")
            expected_payload = {} if envelope.get("mode") == "ingest" else {"raw": "[scrubbed:payload.raw]"}
            if envelope.get("payload") != expected_payload:
                raise ValueError("payload_envelope_invalid")
            if envelope["mode"] in {"invoke", "feedback"} and not _clean_identifier(envelope.get("case_ref", "")):
                raise ValueError("case_ref_invalid")
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError, KeyError) as exc:
            reason = str(exc) or "invalid_envelope"
            emit_trace_event("input_rejected", {"reason": reason}, state)
            raise SecurityViolationError(reason) from None
        return state

    @staticmethod
    def _reject(reason: str, state: dict, exc: Exception | None = None) -> dict:
        emit_trace_event("input_rejected", {"reason": reason}, state)
        suffix = f": {type(exc).__name__}" if exc is not None else ""
        return {"status": AgentStatus.ERROR.value, "error_log": [f"PreProcessNode: {reason}{suffix}"]}


def _clean_identifier(value: Any) -> bool:
    return isinstance(value, str) and bool(_IDENTIFIER.fullmatch(value))


def _safe_evidence_refs(value: Any) -> list[dict[str, str]]:
    """Keep only non-body evidence control metadata in graph state."""
    if not isinstance(value, list):
        return []
    safe: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        source_id = item.get("source_id")
        kind = item.get("kind")
        if _clean_identifier(source_id) and isinstance(kind, str) and kind.isascii() and len(kind) <= 40:
            safe.append({"source_id": source_id, "kind": kind})
    return safe
