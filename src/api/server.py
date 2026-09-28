"""HTTP adapter: trusted scope/RBAC, payload isolation, and graph invocation."""

from __future__ import annotations

import json
from datetime import date as CalendarDate
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from framework.schemas.invocation_context import InvocationContext
from shared.utils.audit_logger import emit_trace_event
from src.entry_adapter import Operation, authenticate
from src.feedback_intake import InMemoryFeedbackReceiptStore
from src.graph.graph import AMLDispositionMemoAgent
from src.ingest_row_pipeline.checkpoint_scrubber import CheckpointScrubPolicy, scrub_checkpoint_envelope
from src.services import build_ledger_service, build_payload_store


_RUNTIME_CONFIG_PATH = Path(__file__).parents[2] / "config" / "config.yaml"
_SAFE_ID = r"^[A-Za-z][A-Za-z0-9_-]{0,127}$"

app = FastAPI(title="AMLDispositionMemoAgent")
agent: AMLDispositionMemoAgent | None = None


@app.middleware("http")
async def _verified_principal_seam(request: Request, call_next):
    """Optional platform-auth seam; never reads identity from client input."""
    stamper = getattr(request.app.state, "verified_principal", None)
    if callable(stamper):
        stamped = stamper(request)
        if isinstance(stamped, dict):
            for key in ("trust_level", "caller_id", "institution_id", "unit_id", "roles", "principal"):
                if key in stamped:
                    setattr(request.state, key, stamped[key])
    return await call_next(request)


class EvidenceReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(pattern=_SAFE_ID)
    kind: str
    amount: str | int | float | None = None
    date: str | None = None
    transaction_id: str | None = None
    account_last4: str | None = Field(default=None, pattern=r"^\d{4}$")
    label: str | None = None


class InvokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_ref: str = Field(pattern=_SAFE_ID)
    alert_type: str
    evidence_refs: list[EvidenceReference] = Field(default_factory=list)
    free_text: str = ""
    as_of_date: str | None = None
    session_id: str = ""


class IngestRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_ref: str = Field(pattern=_SAFE_ID)
    unit_id: str = Field(pattern=_SAFE_ID)
    alert_type: str
    occurred_at: str
    verdict_code: str
    decided_by: str
    decided_at: str
    body: str | None = None
    rationale: str | None = None
    checked_questions: list[str] = Field(default_factory=list)
    evidence_categories: list[str] = Field(default_factory=list)
    countervailing_notes: list[str] = Field(default_factory=list)
    quality_flags: list[str] = Field(default_factory=list)


class IngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    records: list[IngestRecord]
    session_id: str = ""


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_ref: str = Field(pattern=_SAFE_ID)
    record_id: str = Field(pattern=_SAFE_ID)
    feedback_seq: str = Field(pattern=_SAFE_ID)
    verdict_code: str
    rationale: str
    decided_at: str
    session_id: str = ""


def _load_runtime_config() -> dict[str, Any]:
    config = yaml.safe_load(_RUNTIME_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    if not isinstance(config, dict):
        raise RuntimeError("config/config.yaml must be an object")
    return dict(config)


def wire_services(*, runtime_config: dict[str, Any] | None = None) -> AMLDispositionMemoAgent:
    """Build STG-safe in-memory services and compile the graph at startup."""
    global agent
    config = dict(runtime_config or _load_runtime_config())
    scope_keys = config.get("scope_keys")
    if scope_keys != ["institution_id"]:
        raise RuntimeError("FIN-C2-161 requires scope_keys=['institution_id']")
    ttl = config.get("payload_ttl_seconds")
    if not isinstance(ttl, int) or ttl <= 0:
        raise RuntimeError("payload_ttl_seconds must be a positive integer")
    recall_top_k = config.get("recall_top_k")
    if not isinstance(recall_top_k, int) or recall_top_k < 1:
        raise RuntimeError("recall_top_k must be a positive integer")
    max_free_text_chars = config.get("max_free_text_chars")
    if not isinstance(max_free_text_chars, int) or max_free_text_chars < 5:
        raise RuntimeError("max_free_text_chars must be at least five")
    payload_store = config.get("payload_store") or build_payload_store(ttl_seconds=ttl)
    ledger_service = config.get("ledger_service") or build_ledger_service()
    feedback_receipt_store = config.get("feedback_receipt_store") or InMemoryFeedbackReceiptStore()
    config.update(
        {
            "payload_store": payload_store,
            "ledger_service": ledger_service,
            "feedback_receipt_store": feedback_receipt_store,
        }
    )
    agent = AMLDispositionMemoAgent(config=config)
    agent.compile()
    app.state.runtime_config = config
    app.state.payload_store = payload_store
    app.state.ledger_service = ledger_service
    app.state.feedback_receipt_store = feedback_receipt_store
    app.state.agent = agent
    return agent


@app.on_event("startup")
def _startup() -> None:
    wire_services(runtime_config=getattr(app.state, "runtime_config_override", None))


def _session_id(value: str) -> str:
    return value if isinstance(value, str) and value.strip() == value and value else str(uuid4())


def _resolve_as_of_date(value: str | None) -> str:
    """Fix the Stage 1 reference date once, at the HTTP adapter boundary."""
    if value is None:
        return CalendarDate.today().isoformat()
    if not isinstance(value, str):
        raise HTTPException(status_code=422, detail="as_of_date must be an ISO-8601 date.")
    try:
        return CalendarDate.fromisoformat(value).isoformat()
    except ValueError:
        raise HTTPException(status_code=422, detail="as_of_date must be an ISO-8601 date.") from None


def _principal_roles(request: Request) -> set[str] | None:
    """Read roles only from verified request.state, never caller input."""
    principal = getattr(request.state, "principal", None)
    candidate = None
    if isinstance(principal, dict):
        candidate = principal.get("roles")
    elif principal is not None:
        candidate = getattr(principal, "roles", None)
    if candidate is None:
        candidate = getattr(request.state, "roles", None)
    if not isinstance(candidate, (list, tuple, set)) or not all(isinstance(role, str) for role in candidate):
        return None
    return set(candidate)


def _require_mode_role(request: Request, mode: str, config: dict[str, Any]) -> None:
    if mode == "invoke":
        return
    declared = config.get("roles", {}).get(mode) if isinstance(config.get("roles"), dict) else None
    roles = _principal_roles(request)
    caller_id = getattr(request.state, "caller_id", "")
    if not isinstance(declared, list) or not declared or roles is None or not roles.intersection(declared):
        emit_trace_event(
            "authorization_denied",
            {"mode": mode, "required_roles": list(declared) if isinstance(declared, list) else [], "caller_id": caller_id},
            {},
        )
        raise HTTPException(status_code=403, detail="Caller is not authorized for this write mode.")


def _safe_evidence_refs(records: list[EvidenceReference]) -> list[dict[str, str]]:
    return [{"source_id": item.source_id, "kind": item.kind} for item in records]


def _stash_payload(
    *,
    mode: str,
    case_ref: str,
    payload: dict[str, Any],
    evidence_refs: list[dict[str, str]],
    scope: dict[str, str],
    session_id: str,
) -> tuple[str, str]:
    policy = CheckpointScrubPolicy(
        safe_keys=frozenset({"mode", "case_ref", "unit_id"}),
        scope_keys=frozenset({"institution_id"}),
        enum_value_keys={"mode": frozenset({"ingest", "invoke", "feedback"})},
        safe_subtree_keys=frozenset({"evidence_refs"}),
    )
    raw_envelope = {
        "mode": mode,
        "case_ref": case_ref,
        "institution_id": scope["institution_id"],
        "unit_id": scope["unit_id"],
        "evidence_refs": evidence_refs,
        # ingest-row-pipeline v0.4 consumes the scrubber's native `records`
        # bundle through PayloadStore.resolve(..., consume=True). Other modes
        # keep their arbitrary payload as one vaulted JSON string.
        "payload": payload if mode == "ingest" else {"raw": json.dumps(payload, ensure_ascii=False, default=str)},
    }
    safe_input = scrub_checkpoint_envelope(
        json.dumps(raw_envelope, ensure_ascii=False, default=str),
        app.state.payload_store,
        session_id=session_id,
        trusted_scope={"institution_id": scope["institution_id"]},
        policy=policy,
    )
    safe_envelope = json.loads(safe_input)
    payload_ref = safe_envelope.get("payload_ref")
    if not isinstance(payload_ref, str):
        raise RuntimeError("checkpoint scrubber did not issue a payload reference")
    return safe_input, payload_ref


def _invoke_graph(
    request: Request,
    *,
    safe_input: str,
    session_id: str,
    scope: dict[str, str],
    trust: Any,
    as_of_date: str | None = None,
) -> dict:
    # ingest-row-pipeline v0.3+ requires trusted scope/session at construction
    # time. Build the graph after authentication so no shared node can read
    # caller-controlled scope from State or retain request context across calls.
    request_config = {
        **app.state.runtime_config,
        "trusted_scope": {"institution_id": scope["institution_id"]},
        "request_session_id": session_id,
        "feedback_receipt_store": app.state.feedback_receipt_store,
    }
    current_agent = AMLDispositionMemoAgent(config=request_config)
    current_agent.compile()
    ctx = InvocationContext(
        session_id=session_id,
        caller_trust_level=trust,
        caller_id=getattr(request.state, "caller_id", ""),
    )
    # Heterogeneous by design: "scope" is a mapping, "as_of_date" is a string.
    input_context: dict[str, Any] = {"scope": scope}
    if as_of_date is not None:
        input_context["as_of_date"] = as_of_date
    return current_agent.invoke(safe_input, ctx=ctx, input_context=input_context)


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request):
    auth = authenticate(request, operation=Operation.READ_INVOKE)
    scope = {"institution_id": auth.scope["institution_id"], "unit_id": auth.scope["unit_id"]}
    session_id = _session_id(req.session_id)
    as_of_date = _resolve_as_of_date(req.as_of_date)
    payload = {
        "alert_type": req.alert_type,
        "evidence_refs": [item.model_dump(exclude_none=True) for item in req.evidence_refs],
        "free_text": req.free_text,
    }
    safe_input, payload_ref = _stash_payload(
        mode="invoke", case_ref=req.case_ref, payload=payload, evidence_refs=_safe_evidence_refs(req.evidence_refs), scope=scope, session_id=session_id
    )
    try:
        return _invoke_graph(
            request, safe_input=safe_input, session_id=session_id, scope=scope, trust=auth.trust, as_of_date=as_of_date
        )
    finally:
        app.state.payload_store.delete(
            payload_ref, scope={"institution_id": scope["institution_id"]}, session_id=session_id
        )


@app.post("/ingest")
async def ingest(req: IngestRequest, request: Request):
    auth = authenticate(request, operation=Operation.WRITE_INGEST)
    scope = {"institution_id": auth.scope["institution_id"], "unit_id": auth.scope["unit_id"]}
    _require_mode_role(request, "ingest", app.state.runtime_config)
    session_id = _session_id(req.session_id)
    records = [item.model_dump(exclude_none=True) for item in req.records]
    safe_input, payload_ref = _stash_payload(
        mode="ingest", case_ref="", payload={"records": records}, evidence_refs=[], scope=scope, session_id=session_id
    )
    try:
        return _invoke_graph(request, safe_input=safe_input, session_id=session_id, scope=scope, trust=auth.trust)
    finally:
        app.state.payload_store.delete(
            payload_ref, scope={"institution_id": scope["institution_id"]}, session_id=session_id
        )


@app.post("/feedback")
async def feedback(req: FeedbackRequest, request: Request):
    auth = authenticate(request, operation=Operation.WRITE_FEEDBACK)
    scope = {"institution_id": auth.scope["institution_id"], "unit_id": auth.scope["unit_id"]}
    _require_mode_role(request, "feedback", app.state.runtime_config)
    caller_id = auth.principal["caller_id"]
    if not isinstance(caller_id, str) or not caller_id:
        raise HTTPException(status_code=403, detail="Authenticated caller identity is required.")
    session_id = _session_id(req.session_id)
    # decided_by is deliberately stamped here from verified middleware state.
    payload = {
        "record_id": req.record_id,
        "feedback_seq": req.feedback_seq,
        "verdict_code": req.verdict_code,
        "rationale": req.rationale,
        "decided_by": caller_id,
        "decided_at": req.decided_at,
    }
    safe_input, payload_ref = _stash_payload(
        mode="feedback", case_ref=req.case_ref, payload=payload, evidence_refs=[], scope=scope, session_id=session_id
    )
    try:
        return _invoke_graph(request, safe_input=safe_input, session_id=session_id, scope=scope, trust=auth.trust)
    finally:
        app.state.payload_store.delete(
            payload_ref, scope={"institution_id": scope["institution_id"]}, session_id=session_id
        )


@app.get("/health")
def health():
    return {"status": "ok", "agent": "AMLDispositionMemoAgent"}
