from __future__ import annotations

from pathlib import Path

import pytest
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from llm_draft_stage import DraftStageHooks, LlmDraftStageNode, parse_json_object
from llm_draft_stage.draft_node import AmbiguousJsonOutput


class FakeLLM:
    def __init__(self, content=None, exc=None):
        self.content = content
        self.exc = exc
        self.calls = []

    def complete(self, messages):
        self.calls.append(messages)
        if self.exc:
            raise self.exc
        return {"content": self.content}


def hooks():
    def build_prompt(state):
        return f"draft from {state.get('topic')}"

    def fallback(state):
        return {"body": f"fallback {state.get('topic')}", "cited_source_ids": ["src_1"]}

    def evidence(_state, draft):
        cited = list(draft.get("cited_source_ids", []))
        return [{"claim_id": "C-01", "claim_text": draft.get("body", ""), "source_ids": cited}], cited

    return DraftStageHooks(build_prompt=build_prompt, build_fallback=fallback, build_evidence=evidence)


def test_call_path_uses_injected_llm_from_config_and_outputs_evidence_contract():
    llm = FakeLLM('{"body":"draft","cited_source_ids":["src_1"]}')
    node = LlmDraftStageNode(config={"llm": llm}, hooks=hooks())

    result = node({"topic": "case", "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value})

    assert result["status"] == AgentStatus.SUCCESS.value
    assert result["draft"]["body"] == "draft"
    assert result["cited_source_ids"] == ["src_1"]
    assert result["evidence_map"][0]["source_ids"] == ["src_1"]
    assert llm.calls[0][0]["content"] == "draft from case"


def test_llm_absent_returns_deterministic_fallback_with_degradation():
    node = LlmDraftStageNode(config={}, hooks=hooks())

    result = node.execute({"topic": "case"})

    assert result["status"] == AgentStatus.SUCCESS.value
    assert result["draft"]["body"] == "fallback case"
    assert "LLM not configured" in result["degradation_reason"]


def test_llm_parse_failure_is_fail_closed_and_does_not_emit_raw_text():
    node = LlmDraftStageNode(config={"llm": FakeLLM("not json raw draft")}, hooks=hooks())

    result = node.execute({"topic": "case"})

    assert result["status"] == AgentStatus.ERROR.value
    assert "parse failed" in result["error_log"][0]
    assert "not json raw draft" not in str(result)


def test_parse_json_object_accepts_fenced_json_and_rejects_array():
    assert parse_json_object('```json\n{"x":1}\n```') == {"x": 1}

    try:
        parse_json_object("[1,2]")
    except ValueError as exc:
        assert "not an object" in str(exc)
    else:
        raise AssertionError("array must be rejected")


def test_parse_json_object_accepts_duplicated_gpt54mini_fixture() -> None:
    fixture = Path(__file__).parent / "fixtures" / "gpt54mini_duplicated_decision.txt"

    parsed = parse_json_object(fixture.read_text(encoding="utf-8"))

    assert len(parsed["tool_calls"]) == 3


def test_parse_json_object_rejects_distinct_objects_as_ambiguous() -> None:
    with pytest.raises(AmbiguousJsonOutput) as exc_info:
        parse_json_object('{"decision":"first"}\n{"decision":"second"}')

    assert isinstance(exc_info.value, ValueError)


def test_parse_json_object_ignores_braced_prose_after_json() -> None:
    assert parse_json_object('{"decision":"keep"}\nNotes include {x}.') == {
        "decision": "keep"
    }


def test_parse_json_object_rejects_candidate_after_unclosed_broken_brace() -> None:
    """Reject inner fragments so a malformed outer object cannot become a draft."""
    with pytest.raises(ValueError):
        parse_json_object('broken { prose, then {"decision":"keep"}')


def test_parse_json_object_rejects_nested_fragments_in_malformed_fixture() -> None:
    fixture = Path(__file__).parent / "fixtures" / "gpt54mini_malformed_outer_object.txt"

    with pytest.raises(ValueError) as exc_info:
        parse_json_object(fixture.read_text(encoding="utf-8"))

    assert not isinstance(exc_info.value, AmbiguousJsonOutput)


def test_parse_json_object_rejects_single_nested_fragment_in_malformed_outer() -> None:
    output = '{"broken":],"evidence_map":[{"claim_id":"c1"}]}'

    with pytest.raises(ValueError):
        parse_json_object(output)


def test_parse_json_object_accepts_valid_object_after_closed_invalid_object() -> None:
    assert parse_json_object('{x}\n{"a":1}') == {"a": 1}


def test_parse_json_object_ignores_braces_inside_json_string() -> None:
    assert parse_json_object('{"memo":"a {b} c"}') == {"memo": "a {b} c"}


def test_parse_json_object_accepts_duplicated_object_inside_fence() -> None:
    output = '```json\n{"decision":"keep"}\n{"decision":"keep"}\n```'

    assert parse_json_object(output) == {"decision": "keep"}


@pytest.mark.parametrize("output", ['{"value":NaN}', "[1,2]", "", "{}"])
def test_parse_json_object_rejects_outputs_without_nonempty_object(output: str) -> None:
    with pytest.raises(ValueError):
        parse_json_object(output)


def test_parse_json_object_stops_after_32_decode_attempts() -> None:
    output = " ".join("{x}" for _ in range(40)) + ' {"decision":"too late"}'

    with pytest.raises(ValueError, match="no JSON object found"):
        parse_json_object(output)


def test_validate_draft_return_value_replaces_parsed_draft() -> None:
    llm = FakeLLM('{"body":"original","cited_source_ids":[]}')
    base_hooks = hooks()
    validating_hooks = DraftStageHooks(
        build_prompt=base_hooks.build_prompt,
        build_fallback=base_hooks.build_fallback,
        build_evidence=base_hooks.build_evidence,
        validate_draft=lambda draft: {**draft, "body": "validated"},
    )
    node = LlmDraftStageNode(config={"llm": llm}, hooks=validating_hooks)

    result = node.execute({"topic": "case"})

    assert result["status"] == AgentStatus.SUCCESS.value
    assert result["draft"]["body"] == "validated"
    assert result["evidence_map"][0]["claim_text"] == "validated"
