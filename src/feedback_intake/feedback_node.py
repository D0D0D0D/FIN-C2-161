# PART: feedback-intake v0.2.1 (parts@09b50d1)
"""AgentCore node that resolves a scoped feedback payload and calls the service."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar, Protocol

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from .feedback_service import FeedbackIntakeService, FeedbackRejectedError


_FEEDBACK_PAYLOAD_KEYS = frozenset(
    {"record_id", "feedback_seq", "verdict_code", "rationale", "decided_by", "decided_at"}
)


class PayloadStore(Protocol):
    def resolve(
        self, ref: str | None, *, scope: Mapping[str, str], session_id: str, consume: bool = False
    ) -> Any: ...


class FeedbackIntakeNode(FunctionNode):
    """Resolve raw feedback inside ``execute`` and return only a safe result."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(
        self,
        *,
        service: FeedbackIntakeService,
        payload_store: PayloadStore,
        scope_keys: Sequence[str],
        payload_ref_key: str = "feedback_payload_ref",
    ) -> None:
        super().__init__()
        self._service = service
        self._payload_store = payload_store
        self._scope_keys = _validate_scope_keys(scope_keys)
        self._payload_ref_key = payload_ref_key

    def execute(self, state: dict) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            return {}
        scope = _scope_from_state(state, self._scope_keys)
        if scope is None:
            return _error("feedback_scope_missing_or_invalid", state=state)
        session_id = state.get("session_id")
        ref = state.get(self._payload_ref_key)
        if not _clean_string(session_id) or not _clean_string(ref):
            return _error("feedback_payload_ref_missing_or_invalid", scope=scope, state=state)
        try:
            raw = self._payload_store.resolve(ref, scope=scope, session_id=session_id, consume=False)
            payload = _parse_payload(raw)
        except Exception:  # noqa: BLE001 - payload store failures are fail-closed without value echo.
            return _error("feedback_payload_unresolvable", scope=scope, state=state)
        if payload is None:
            return _error("feedback_payload_unresolvable", scope=scope, state=state)
        if set(payload) != _FEEDBACK_PAYLOAD_KEYS:
            return _error("feedback_payload_invalid", scope=scope, state=state)
        # S-4: this node's own domain event, and the only one it owns. The split
        # is by stage, not by outcome: the node records what it accepted for
        # processing (an opaque ref, resolved under this scope into a payload of
        # the declared shape); the service records the outcome of that submission
        # (feedback_accepted / feedback_idempotency_hit / feedback_rejected). The
        # same fact is never written twice -- see the submit() call below.
        # Payload is the verified scope and the declared ref key name only: the
        # resolved payload's fields are caller-supplied and unvalidated at this
        # point, so none of them (record_id, rationale, decided_by) are copied here.
        emit_trace_event(
            "feedback_payload_resolved",
            {"scope": dict(scope), "payload_ref_key": self._payload_ref_key},
            state,
        )
        try:
            result = self._service.submit(
                scope=scope,
                state=state,
                **{key: payload[key] for key in _FEEDBACK_PAYLOAD_KEYS},
            )
        except FeedbackRejectedError as exc:
            # The service emitted feedback_rejected with its scoped ownership
            # and validation context; do not duplicate that trace event.
            return _error(str(exc))
        return {"feedback_result": result, "status": AgentStatus.SUCCESS.value}

def _scope_from_state(state: Mapping[str, Any], scope_keys: tuple[str, ...]) -> dict[str, str] | None:
    scope = {key: state.get(key) for key in scope_keys}
    if any(not _clean_string(value) for value in scope.values()):
        return None
    return scope


def _parse_payload(value: Any) -> dict[str, Any] | None:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    return dict(value) if isinstance(value, Mapping) else None


def _validate_scope_keys(scope_keys: Sequence[str]) -> tuple[str, ...]:
    if isinstance(scope_keys, (str, bytes)) or not isinstance(scope_keys, Sequence):
        raise ValueError("FeedbackIntakeNode: scope_keys must be a sequence of key names")
    keys = tuple(scope_keys)
    if not keys or len(keys) != len(set(keys)) or any(not isinstance(key, str) or not key.isidentifier() for key in keys):
        raise ValueError("FeedbackIntakeNode: scope_keys must be unique identifier strings")
    return keys


def _clean_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value == value.strip() and "\n" not in value and "\r" not in value


def _error(
    reason: str,
    *,
    scope: Mapping[str, str] | None = None,
    state: dict | None = None,
) -> dict[str, Any]:
    if state is not None:
        emit_trace_event(
            "feedback_rejected",
            {"scope": dict(scope or {}), "record_id": None, "reason": reason},
            state or {},
        )
    return {"status": AgentStatus.ERROR.value, "error_log": [f"FeedbackIntakeNode: {reason}"]}
