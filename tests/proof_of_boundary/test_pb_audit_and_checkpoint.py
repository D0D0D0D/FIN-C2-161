"""PB-1/PB-2/PB-5: domain audit bindings and deserialized checkpoint safety."""

from __future__ import annotations

import json

from framework.schemas.agent_status import AgentStatus
from src.ingest_row_pipeline.checkpoint_scrubber import CheckpointScrubPolicy, scrub_checkpoint_envelope
from src.nodes import domain_workflow_nodes as domain
from src.nodes import post_process_node, pre_process_node


class _Store:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def put(self, value: str, *, scope: dict[str, str], session_id: str) -> str:
        ref = f"ref-{len(self.values)}"
        self.values[ref] = value
        return ref


def test_pb1_domain_event_bindings_and_real_event_pairs(monkeypatch):
    expected = {
        pre_process_node: "input_validated",
        domain: "evidence_check_completed",
        post_process_node: "output_envelope_requested",
    }
    for module in expected:
        assert hasattr(module, "emit_trace_event"), f"{module.__name__} does not bind audit emitter"
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(pre_process_node, "emit_trace_event", lambda event, _payload, _state: events.append((pre_process_node.__name__, event)))
    pre_process_node.PreProcessNode().execute(
        {
            "user_input": json.dumps({"mode": "invoke", "case_ref": "case-a", "institution_id": "bank-a", "unit_id": "aml-a", "payload_ref": "abcdefghijklmnopabcdefghijklmnop", "evidence_refs": [], "payload": {"raw": "[scrubbed:payload.raw]"}}),
            "input_context": {"scope": {"institution_id": "bank-a", "unit_id": "aml-a"}},
        }
    )
    monkeypatch.setattr(domain, "emit_trace_event", lambda event, _payload, _state: events.append((domain.__name__, event)))
    domain.EvidenceCompletenessNode(payload_store=type("S", (), {"resolve": lambda *_a, **_k: '{"__extra__":{"payload.raw":"{\\"alert_type\\":\\"cash\\",\\"evidence_refs\\":[]}"}}'})()).execute(
        {"scope": {"institution_id": "bank-a", "unit_id": "aml-a"}, "session_id": "s", "payload_ref": "ref"}
    )
    monkeypatch.setattr(post_process_node, "emit_trace_event", lambda event, _payload, _state: events.append((post_process_node.__name__, event)))
    post_process_node.PostProcessNode().execute({"mode": "ingest", "ingest_summary": {}, "status": AgentStatus.SUCCESS.value})
    assert set(events) >= {(pre_process_node.__name__, "input_validated"), (domain.__name__, "evidence_check_completed"), (post_process_node.__name__, "output_envelope_requested")}


def test_pb2_checkpoint_scrubber_is_structural_and_mutation_sensitive():
    store = _Store()
    policy = CheckpointScrubPolicy(
        safe_keys=frozenset({"mode", "case_ref"}),
        scope_keys=frozenset({"institution_id"}),
        enum_value_keys={"mode": frozenset({"invoke"})},
    )
    safe = json.loads(
        scrub_checkpoint_envelope(
            json.dumps({"mode": "invoke", "case_ref": "case-a", "payload": {"free_text": "090-1111-2222"}}),
            store,
            session_id="session-a",
            trusted_scope={"institution_id": "bank-a"},
            policy=policy,
        )
    )
    # Inspect parsed JSON trees, never repr()/blob text.  The deliberately
    # injected phone value has moved to the vault and cannot reach checkpoint.
    assert safe["payload"]["free_text"] == "[scrubbed:payload.free_text]"
    assert "090-1111-2222" not in json.dumps(safe)
    vaulted = json.loads(store.values[safe["payload_ref"]])
    assert vaulted["__extra__"]["payload.free_text"] == "090-1111-2222"
