"""PB-6: every concrete node follows the framework gate order."""

import importlib
import inspect
import json
import pkgutil

from framework.schemas.trust_level import TrustLevel
from src.feedback_intake import FeedbackIntakeService, InMemoryFeedbackReceiptStore
from src.nodes.domain_workflow_nodes import (
    DispositionFeedbackNode,
    DispositionIngestNode,
    DispositionMemoDraftNode,
    EvidenceCompletenessNode,
    SimilarPerspectiveRecallNode,
)
from src.nodes.main_node import DomainWorkflowGraphNode
from src.nodes.pre_process_node import PreProcessNode
from src.services.ledger_service import build_ledger_service
from src.services.payload_store import build_payload_store
from typing import ClassVar
from framework.nodes.base_node import BaseNode


def _discover_node_classes() -> list[type]:
    """Collect every concrete local BaseNode via the scaffold's package walk."""
    from framework.nodes.base_node import BaseNode

    pkg = importlib.import_module("src.nodes")
    discovered: list[type] = []
    for _, modname, _ in pkgutil.walk_packages(pkg.__path__, prefix="src.nodes."):
        module = importlib.import_module(modname)
        for attr in vars(module).values():
            if (
                isinstance(attr, type)
                and issubclass(attr, BaseNode)
                and attr is not BaseNode
                and attr.__module__ == modname
                and not inspect.isabstract(attr)
                and attr not in discovered
            ):
                discovered.append(attr)
    return discovered


def _vault(store, payload: dict, session_id: str) -> str:
    return store.put(
        json.dumps({"__extra__": {"payload.raw": json.dumps(payload)}}),
        scope={"institution_id": "bank-a"},
        session_id=session_id,
    )


def _node_factory(node_cls: type):
    """Supply only immutable injected services required by concrete nodes."""
    ledger = build_ledger_service()
    store = build_payload_store(ttl_seconds=60)
    if node_cls is EvidenceCompletenessNode:
        return node_cls(payload_store=store), store
    if node_cls is SimilarPerspectiveRecallNode:
        return (
            node_cls(
                ledger_service=ledger,
                payload_store=store,
                top_k=1,
                max_free_text_chars=12000,
            ),
            store,
        )
    if node_cls is DispositionMemoDraftNode:
        return node_cls(config={"llm": None, "max_free_text_chars": 12000}, payload_store=store), store
    if node_cls is DispositionFeedbackNode:
        service = FeedbackIntakeService(
            ledger=ledger,
            receipt_store=InMemoryFeedbackReceiptStore(),
            domain_fields={"unit_id": "scalar", "case_ref": "scalar"},
        )
        return node_cls(service=service, payload_store=store), store
    if node_cls is DispositionIngestNode:
        return (
            node_cls(
                payload_store=store,
                ledger_service=ledger,
                trusted_scope={"institution_id": "bank-a"},
                session_id="pb6-session",
                max_free_text_chars=12000,
            ),
            store,
        )
    if node_cls is DomainWorkflowGraphNode:
        node = node_cls(
            config={
                "ledger_service": ledger,
                "payload_store": store,
                "recall_top_k": 1,
                "max_free_text_chars": 12000,
                "trusted_scope": {"institution_id": "bank-a"},
                "request_session_id": "pb6-session",
                "feedback_receipt_store": InMemoryFeedbackReceiptStore(),
            }
        )
        # PB-6 exercises gate order rather than checkpoint/resume behavior.
        # The template has HITL and memory disabled, so no inner saver is
        # required for this isolated call path.
        node._use_inner_checkpointer = False
        return node, store
    return node_cls(), store


def _state_for(node_cls: type, store) -> dict:
    session_id = "pb6-session"
    payload_ref = _vault(
        store,
        {"alert_type": "cash", "evidence_refs": [], "free_text": "調査記録を確認した。", "records": []},
        session_id,
    )
    inner_envelope = json.dumps(
        {
            "mode": "invoke",
            "scope": {"institution_id": "bank-a", "unit_id": "aml-a"},
            "case_ref": "case-a",
            "payload_ref": payload_ref,
            "evidence_refs": [],
        }
    )
    outer_envelope = json.dumps(
        {
            "mode": "invoke",
            "case_ref": "case-a",
            "institution_id": "bank-a",
            "unit_id": "aml-a",
            "payload_ref": payload_ref,
            "evidence_refs": [],
            "payload": {"raw": "[scrubbed:payload.raw]"},
        }
    )
    state = {
        "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
        "correlation_id": "pb6",
        "trace_id": "trace-pb6",
        "thread_id": "thread-pb6",
        "session_id": session_id,
        "execution_time": {},
        "status": "pending",
        "mode": "invoke",
        "scope": {"institution_id": "bank-a", "unit_id": "aml-a"},
        "institution_id": "bank-a",
        "unit_id": "aml-a",
        "case_ref": "case-a",
        "payload_ref": payload_ref,
        "feedback_payload_ref": payload_ref,
        "input_context": {"scope": {"institution_id": "bank-a", "unit_id": "aml-a"}},
        "user_input": inner_envelope,
        "evidence_check": {"evidence_present": [], "evidence_gap": []},
        "recalled_perspectives": [],
        "memo_sections": {"facts": [], "evidence_sources": [], "unknowns": [], "countervailing_factors": [], "memo_draft": ""},
        "evidence_map": [],
        "requires_human_review": False,
        "review_reasons": [],
        "ingest_summary": {},
    }
    if node_cls is PreProcessNode:
        state["user_input"] = outer_envelope
    if node_cls.__name__ == "PostProcessNode":
        state.update({"mode": "ingest", "ingest_summary": {"written_ids": [], "rejected": []}})
    return state


# ── PB-6 S-1 negative boundary（2026-08-17 正本追従で移植）──────────────
# 正本 tests/proof_of_boundary/test_pb_invoke_order.py より。テンプレのノード構成に
# 依存しない常設フィクスチャで「不足 trust では execute() に到達しない」ことを証明する
# （TC-08 の要求。既存の全ノード走査テストは触っていない）。


class _PrivilegedTrustGateFixture(BaseNode):
    """Always-present privileged node used to prove the S-1 negative boundary."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def _security_gate_input(self, state):
        return state

    def execute(self, state):
        return {"status": "success"}

    def _security_gate_output(self, result):
        return result


def _trust_predecessor(required: TrustLevel) -> TrustLevel:
    """Return a lower valid trust level; fail loudly if the framework adds one."""
    predecessors = {
        TrustLevel.VERIFIED_EXTERNAL: TrustLevel.ANONYMOUS,
        TrustLevel.INTERNAL: TrustLevel.VERIFIED_EXTERNAL,
    }
    try:
        return predecessors[required]
    except KeyError as exc:
        raise AssertionError(f"no lower trust level defined for {required!r}") from exc


def test_pb6_dynamic_scan_checks_every_concrete_node(monkeypatch):
    """Future local nodes are automatically included by ``walk_packages``."""
    import framework.nodes.base_node as base_node_module

    node_classes = _discover_node_classes()
    assert node_classes
    failures: list[str] = []
    for node_cls in node_classes:
        node, store = _node_factory(node_cls)
        state = _state_for(node_cls, store)
        order: list[str] = []
        with monkeypatch.context() as scoped:
            scoped.setattr(
                base_node_module,
                "emit_trace_event",
                lambda event, payload, _state, _order=order, _name=node_cls.__name__: (
                    _order.append(f"event:{event}") if payload.get("node") == _name else None
                ),
            )
            for method_name, label in (
                ("_security_gate_input", "security_gate_input"),
                ("execute", "execute"),
                ("_security_gate_output", "security_gate_output"),
            ):
                original = getattr(node_cls, method_name)

                def spy(self, arg, _order=order, _label=label, _original=original):
                    _order.append(_label)
                    return _original(self, arg)

                scoped.setattr(node_cls, method_name, spy)
            node(state)
        expected = [
            "event:node_start",
            "security_gate_input",
            "execute",
            "security_gate_output",
            "event:node_complete",
        ]
        if order != expected:
            failures.append(f"{node_cls.__module__}.{node_cls.__name__}: expected {expected}, got {order}")
    assert not failures, "\n".join(failures)


def test_pb6_pre_process_runs_s1_s2_execute_s3_in_order(monkeypatch):
    """Use a valid concrete node and distinguish framework, not domain, events."""
    import framework.nodes.base_node as base_node_module

    node = PreProcessNode()
    order: list[str] = []
    monkeypatch.setattr(
        base_node_module,
        "emit_trace_event",
        lambda event, _payload, _state: order.append(f"event:{event}"),
    )
    for name, label in (("_security_gate_input", "s2"), ("execute", "execute"), ("_security_gate_output", "s3")):
        original = getattr(node, name)

        def spy(arg, *, _original=original, _label=label):
            order.append(_label)
            return _original(arg)

        monkeypatch.setattr(node, name, spy)

    node(
        {
            "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
            "correlation_id": "pb6",
            "input_context": {"scope": {"institution_id": "bank-a", "unit_id": "aml-a"}},
            "user_input": json.dumps(
                {
                    "mode": "invoke",
                    "case_ref": "case-a",
                    "institution_id": "bank-a",
                    "unit_id": "aml-a",
                    "payload_ref": "abcdefghijklmnopabcdefghijklmnop",
                    "evidence_refs": [],
                    "payload": {"raw": "[scrubbed:payload.raw]"},
                }
            ),
            "execution_time": {},
        }
    )
    assert order == ["event:node_start", "s2", "execute", "s3", "event:node_complete"]


def test_s1_denial_refuses_execution_before_execute(monkeypatch):
    """TC-08: an always-present privileged node proves the negative S-1 path."""
    import framework.nodes.base_node as base_node_module

    events: list[str] = []
    execute_calls: list[object] = []
    monkeypatch.setattr(
        base_node_module,
        "emit_trace_event",
        lambda event_type, _payload, _state: events.append(event_type),
    )
    original_execute = _PrivilegedTrustGateFixture.execute

    def spy_execute(self, state):
        execute_calls.append(state)
        return original_execute(self, state)

    monkeypatch.setattr(_PrivilegedTrustGateFixture, "execute", spy_execute)
    result = _PrivilegedTrustGateFixture()(
        {
            "caller_trust_level": _trust_predecessor(
                _PrivilegedTrustGateFixture.required_trust_level
            ).value,
            "correlation_id": "tc08-s1-denial",
        }
    )

    assert result["status"] == "error"
    assert "S-1 trust gate denied" in result["error_log"][0]
    assert events == ["s1_denied"]
    assert not execute_calls
