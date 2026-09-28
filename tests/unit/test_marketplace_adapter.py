"""AGENTIC STAR Marketplace adapter boundary tests."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from types import ModuleType

import pytest
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.base import MissingSecret, NullProvider
from framework.secrets.context import bound_secrets

import cli
from src.graph.graph import AMLDispositionMemoAgent


def _set_required_environment(monkeypatch, suffix: str = "one") -> dict[str, str]:
    values = {key: f"test-{suffix}-{key.lower().replace('_', '-')}" for key in cli._REQUIRED_SECRET_KEYS}
    # The startup gate requires a bare Azure resource endpoint, so
    # this value cannot be a placeholder like the other secrets.
    values["AZURE_OPENAI_ENDPOINT"] = f"https://{suffix}.services.ai.azure.example"
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return values


def _invoke_packet(**overrides) -> str:
    packet = {
        "case_ref": "marketplace-case-a",
        "alert_type": "cash",
        "evidence_refs": [
            {
                "source_id": "txn-a",
                "kind": "transaction_record",
                "amount": "1000",
                "date": "2026-08-30",
            }
        ],
        "free_text": "取引記録を確認した。",
        "as_of_date": "2026-08-31",
    }
    packet.update(overrides)
    return json.dumps(packet, ensure_ascii=False)


def test_marketplace_rebuilds_input_context_from_deployment_scope(monkeypatch):
    values = _set_required_environment(monkeypatch)
    captured = {}

    def parent_invoke(self, user_input, session_id="", ctx=None, input_context=None):
        captured.update(
            user_input=json.loads(user_input),
            session_id=session_id,
            ctx=ctx,
            input_context=input_context,
        )
        return {"status": "success", "output": {}}

    monkeypatch.setattr(AMLDispositionMemoAgent, "invoke", parent_invoke)
    original_ctx = InvocationContext(
        caller_id="marketplace-user",
        caller_trust_level=TrustLevel.INTERNAL,
        hitl_allowed=True,
    )
    result = cli.MarketplaceGraph().invoke(
        _invoke_packet(),
        ctx=original_ctx,
        input_context={
            "conversation_history": [{"role": "user", "content": "untrusted-history"}],
            "scope": {"institution_id": "attacker", "unit_id": "attacker"},
        },
    )

    assert result["status"] == "success"
    assert captured["input_context"] == {
        "scope": {
            "institution_id": values["STG_DEFAULT_INSTITUTION_ID"],
            "unit_id": values["STG_DEFAULT_UNIT_ID"],
        },
        "as_of_date": "2026-08-31",
    }
    assert "conversation_history" not in captured["input_context"]
    assert captured["user_input"]["mode"] == "invoke"
    assert captured["user_input"]["institution_id"] == values["STG_DEFAULT_INSTITUTION_ID"]
    assert captured["user_input"]["unit_id"] == values["STG_DEFAULT_UNIT_ID"]
    assert captured["user_input"]["payload"] == {"raw": "[scrubbed:payload.raw]"}
    assert captured["session_id"] == cli._MARKETPLACE_SESSION_ID
    assert captured["ctx"] is not original_ctx
    assert captured["ctx"].caller_trust_level is TrustLevel.VERIFIED_EXTERNAL
    assert captured["ctx"].hitl_allowed is False


def test_scope_empty_or_whitespace_fails_closed_at_startup(monkeypatch):
    _set_required_environment(monkeypatch)
    for invalid in ("", " ", "bank one", "\tbank", "bank\n"):
        monkeypatch.setenv("STG_DEFAULT_INSTITUTION_ID", invalid)
        with pytest.raises((MissingSecret, ValueError), match="STG_DEFAULT_INSTITUTION_ID"):
            cli.MarketplaceGraph()


@pytest.mark.parametrize("missing_key", cli._REQUIRED_SECRET_KEYS)
def test_missing_required_secret_fails_during_startup(monkeypatch, missing_key):
    _set_required_environment(monkeypatch)
    monkeypatch.delenv(missing_key)

    with pytest.raises(MissingSecret, match=missing_key):
        cli.MarketplaceGraph().provision_secrets(NullProvider())


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://example.cognitiveservices.azure.com/openai/responses?api-version=2025-04-01-preview",
        "https://example.services.ai.azure.example/openai",
        "https://example.services.ai.azure.example/v1",
        "https://example.services.ai.azure.example?api-version=2024-10-21",
        "example.services.ai.azure.example",
    ],
    ids=["api-path", "openai-path", "base-path", "query-only", "missing-scheme"],
)
def test_unusable_azure_endpoint_shape_fails_during_startup(monkeypatch, endpoint):
    """AzureOpenAIClient appends its own path, so anything but a bare endpoint breaks.

    The resulting error surfaces inside complete(); a node that swallows it
    degrades silently while the invocation still reports status: success, so the
    adapter has to reject the shape at startup instead.
    """

    _set_required_environment(monkeypatch)
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", endpoint)

    with pytest.raises(ValueError, match="bare"):
        cli.MarketplaceGraph().provision_secrets(NullProvider())


def test_secret_provider_is_allowlisted(monkeypatch):
    values = _set_required_environment(monkeypatch)
    monkeypatch.setenv("UNRELATED_PLATFORM_SECRET", "must-not-be-visible")
    agent = cli.MarketplaceGraph()

    agent.provision_secrets(NullProvider())

    provider = agent._secrets_provider
    assert {key: provider.require(key) for key in cli._REQUIRED_SECRET_KEYS} == values
    assert provider.get("UNRELATED_PLATFORM_SECRET") is None
    assert isinstance(agent.config["llm"], cli._AzureMarketplaceLLM)


def test_azure_client_is_recreated_with_fresh_secrets_for_every_call(monkeypatch):
    configs = []

    class FakeAzureOpenAIClient:
        def __init__(self, config):
            configs.append(dict(config))

        def complete(self, messages):
            return {"content": "{}", "messages": messages}

        def stream(self, messages):
            yield "{}"

        def bind_tools(self, tools):
            return self

    azure_module = ModuleType("shared.services.llm.azure_openai_client")
    azure_module.AzureOpenAIClient = FakeAzureOpenAIClient
    monkeypatch.setitem(sys.modules, azure_module.__name__, azure_module)
    _set_required_environment(monkeypatch, "first")
    provider = cli._AllowlistedEnvironmentProvider()
    llm = cli._AzureMarketplaceLLM()

    assert vars(llm) == {}
    with bound_secrets(provider):
        first = _set_required_environment(monkeypatch, "first")
        llm.complete([{"role": "user", "content": "first"}])
        assert vars(llm) == {}

        second = _set_required_environment(monkeypatch, "second")
        llm.complete([{"role": "user", "content": "second"}])
        assert vars(llm) == {}

    assert configs == [
        {
            "api_key": first["AZURE_OPENAI_API_KEY"],
            "azure_endpoint": first["AZURE_OPENAI_ENDPOINT"],
            "azure_deployment": first["AZURE_OPENAI_DEPLOYMENT"],
            "response_format": {"type": "json_object"},
        },
        {
            "api_key": second["AZURE_OPENAI_API_KEY"],
            "azure_endpoint": second["AZURE_OPENAI_ENDPOINT"],
            "azure_deployment": second["AZURE_OPENAI_DEPLOYMENT"],
            "response_format": {"type": "json_object"},
        },
    ]


def test_json_mode_is_confined_to_the_marketplace_seam():
    """Only cli.py asks Azure for JSON mode; the HTTP/STG path and the shared
    part are untouched, so their behaviour (deterministic fallback when no llm
    is injected) does not change with this MR."""

    src_root = Path(__file__).resolve().parents[2] / "src"
    offenders = [
        path.relative_to(src_root.parent)
        for path in src_root.rglob("*.py")
        if "response_format" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
    assert cli._MARKETPLACE_RESPONSE_FORMAT == {"type": "json_object"}


def test_prompt_mentions_json_so_azure_json_mode_is_accepted():
    """Azure returns 400 for response_format=json_object unless the messages
    contain the word json. Pin it statically so a prompt edit cannot silently
    bring that failure back on the Marketplace path."""

    from src.nodes.domain_workflow_nodes import _build_prompt
    from src.services.payload_store import build_payload_store

    store = build_payload_store(ttl_seconds=60)
    ref = store.put(
        json.dumps({"__extra__": {"payload.raw": json.dumps({"free_text": "", "alert_type": "cash", "evidence_refs": []})}}),
        scope={"institution_id": "bank-a"},
        session_id="json-mode-session",
    )
    prompt = _build_prompt(
        {
            "scope": {"institution_id": "bank-a"},
            "session_id": "json-mode-session",
            "payload_ref": ref,
            "case_ref": "case-a",
            "evidence_check": {},
        },
        store,
    )
    assert "json" in json.loads(prompt)["instruction"].lower()


@pytest.mark.parametrize(
    ("result", "fragment"),
    [
        ({"content": '{"a": "x"], "b": 1}', "usage": {"completion_tokens": 7}}, "reason=ValueError"),
        ({"content": '{"a": 1}\n{"a": 2}'}, "reason=AmbiguousJsonOutput"),
        ({"content": "[1]"}, "reason=ValueError"),
        ({"content": "{}"}, "reason=ValueError"),
        ({"content": '{"x": NaN}'}, "reason=ValueError"),
        ({"content": None}, "reason=ValueError"),
    ],
)
def test_marketplace_output_observer_warns_without_echoing_the_model_text(caplog, result, fragment):
    """The observer applies the shared part's own acceptance rule, so every
    completion the draft stage would reject (and only those) leaves one
    WARNING carrying shape facts, never the model text."""

    caplog.set_level(logging.WARNING, logger=cli.__name__)
    cli._observe_json_output(result)
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "rejected by the draft parser" in message and fragment in message
    assert '"a"' not in message and "[1]" not in message and '"x"' not in message


@pytest.mark.parametrize(
    "content",
    [' {"a": 1} \n', '\ufeff{"a": 1}', '{"a": 1}\n{"a": 1}', 'note\n{"a": 1}'],
)
def test_marketplace_output_observer_is_silent_when_the_part_accepts(caplog, content):
    caplog.set_level(logging.WARNING, logger=cli.__name__)
    cli._observe_json_output({"content": content, "usage": {"completion_tokens": 3}})
    assert caplog.records == []


def test_marketplace_output_observer_never_raises(caplog, monkeypatch):
    caplog.set_level(logging.WARNING, logger=cli.__name__)

    def explode(_content):
        raise RecursionError("too deep")

    monkeypatch.setattr(cli, "parse_json_object", explode)
    cli._observe_json_output({"content": "{" * 10_000})
    assert [r.getMessage() for r in caplog.records] == ["marketplace llm output could not be observed: RecursionError"]


def test_generation_metadata_version_matches_the_release_version():
    from src.nodes.post_process_node import TEMPLATE_VERSION

    pyproject = (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    manifest = (Path(__file__).resolve().parents[2] / "config" / "agent.yaml").read_text(encoding="utf-8")
    assert f'version = "{TEMPLATE_VERSION}"' in pyproject
    assert f'version: "{TEMPLATE_VERSION}"' in manifest
def test_marketplace_write_modes_never_reach_parent_graph(monkeypatch):
    _set_required_environment(monkeypatch)
    invoked = False

    def parent_invoke(*args, **kwargs):
        nonlocal invoked
        invoked = True
        return {"status": "success"}

    monkeypatch.setattr(AMLDispositionMemoAgent, "invoke", parent_invoke)

    with pytest.raises(ValueError, match="write modes are not available"):
        cli.MarketplaceGraph().invoke(_invoke_packet(mode="ingest"))

    assert not invoked


def test_marketplace_treats_awaiting_human_as_failure(monkeypatch):
    _set_required_environment(monkeypatch)

    def parent_invoke(*args, **kwargs):
        return {"status": "awaiting_human", "output": {"unreviewed": "must-not-pass"}}

    monkeypatch.setattr(AMLDispositionMemoAgent, "invoke", parent_invoke)

    result = cli.MarketplaceGraph().invoke(_invoke_packet())

    assert result == {
        "output": None,
        "status": "error",
        "error_log": ["MarketplaceGraph: awaiting_human_not_supported"],
    }


def test_plain_text_is_vaulted_as_read_only_transaction_monitoring_input(monkeypatch):
    _set_required_environment(monkeypatch)
    captured = {}

    def parent_invoke(self, user_input, session_id="", ctx=None, input_context=None):
        safe = json.loads(user_input)
        stored = self.config["payload_store"].resolve(
            safe["payload_ref"],
            scope={"institution_id": safe["institution_id"]},
            session_id=session_id,
        )
        captured.update(safe=safe, stored=json.loads(stored))
        return {"status": "success"}

    monkeypatch.setattr(AMLDispositionMemoAgent, "invoke", parent_invoke)
    cli.MarketplaceGraph().invoke("確認対象のアラート本文")

    raw = json.loads(captured["stored"]["__extra__"]["payload.raw"])
    assert raw == {
        "alert_type": "transaction_monitoring",
        "evidence_refs": [],
        "free_text": "確認対象のアラート本文",
    }
    assert captured["safe"]["mode"] == "invoke"


def test_main_loads_repository_config_for_marketplace_runner(monkeypatch):
    called = {}

    def fake_runner(agent_cls, **kwargs):
        called["agent_cls"] = agent_cls
        called.update(kwargs)

    monkeypatch.setattr(cli, "run_agent_marketplace", fake_runner)
    cli.main()

    assert called["agent_cls"] is cli.MarketplaceGraph
    assert called["agent_name"] == "FIN-C2-161"
    assert called["namespace"] == "fin"
    assert called["config"]["max_retry"] == 3
    assert called["config"]["scope_keys"] == ["institution_id"]
