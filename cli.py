"""AGENTIC STAR Marketplace entry adapter for FIN-C2-161."""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Iterator, Mapping
from dataclasses import replace
from datetime import date as CalendarDate
from pathlib import Path
from typing import Any, cast
from uuid import uuid4
from urllib.parse import urlsplit

from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.base import SecretProvider
from framework.secrets.context import current_secrets

try:
    from framework.utils.config_loader import load_agent_config
except ImportError:  # AgentCore 1.0.0 / 1.0.1 compatibility
    from framework.utils.config_loader import load_config

    def load_agent_config(agent_dir: Path) -> dict[str, Any]:
        config_path = agent_dir / "config" / "config.yaml"
        return load_config(str(config_path)) if config_path.exists() else {}


try:
    from shared.bootstrap import run_agent_marketplace
except ModuleNotFoundError as exc:  # AgentCore 1.0.0 / 1.0.1 has no Marketplace extra
    if exc.name != "shared.bootstrap":
        raise
    _BOOTSTRAP_IMPORT_ERROR: ModuleNotFoundError | None = exc

    def run_agent_marketplace(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise RuntimeError(
            "The installed AgentCore wheel does not provide Marketplace bootstrap support"
        ) from _BOOTSTRAP_IMPORT_ERROR
else:
    _BOOTSTRAP_IMPORT_ERROR = None


try:
    from shared.secrets.env_provider import EnvProvider
except ModuleNotFoundError as exc:  # AgentCore 1.0.0 / 1.0.1 compatibility
    if exc.name != "shared.secrets.env_provider":
        raise

    class EnvProvider(SecretProvider):  # type: ignore[no-redef]
        """Minimal process-environment provider for pre-1.0.2 wheels."""

        def __init__(self, namespace: str, agent_name: str) -> None:
            self._namespace = namespace
            self._agent_name = agent_name

        def get(self, key: str, default: str | None = None) -> str | None:
            return os.environ.get(key, default)


from shared.services.llm.base_llm import BaseLLM

from src.entry_adapter import SCOPE_KEYS, STG_DEFAULT_SCOPE_ENVS
from src.feedback_intake import InMemoryFeedbackReceiptStore
from src.graph.graph import AMLDispositionMemoAgent
from src.llm_draft_stage.draft_node import parse_json_object
from src.ingest_row_pipeline.checkpoint_scrubber import (
    CheckpointScrubPolicy,
    scrub_checkpoint_envelope,
)
from src.services import build_ledger_service, build_payload_store

_NAMESPACE = "fin"
_AGENT_NAME = "FIN-C2-161"
_PROVIDER_AGENT_NAME = "fin_c2_161"
_MARKETPLACE_SESSION_ID = "a-star-marketplace"
_DEFAULT_ALERT_TYPE = "transaction_monitoring"
# Marketplace-only: ask Azure OpenAI to constrain the completion to one JSON
# object *before* generation. The registered model (gpt-5.4-mini) otherwise
# emits a structurally broken object about 1-4% of the time (a stray ``]``
# after the long ``memo_draft`` string), which the shared llm-draft-stage part
# correctly fails closed on. JSON mode removes that class at the source; it is
# not a repair of model output. Azure rejects the request unless the prompt
# mentions "json" (see ``_build_prompt`` and the static test that pins it).
_MARKETPLACE_RESPONSE_FORMAT: dict[str, str] = {"type": "json_object"}
_log = logging.getLogger(__name__)
_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,63}$")

_AZURE_OPENAI_SECRET_KEYS = (
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_DEPLOYMENT",
)
_SCOPE_ENV_KEYS = tuple(STG_DEFAULT_SCOPE_ENVS[key] for key in SCOPE_KEYS)
_REQUIRED_SECRET_KEYS = (*_AZURE_OPENAI_SECRET_KEYS, *_SCOPE_ENV_KEYS)
_ALLOWED_SECRET_KEYS = frozenset(_REQUIRED_SECRET_KEYS)
_MARKETPLACE_INPUT_KEYS = frozenset(
    {"mode", "case_ref", "alert_type", "evidence_refs", "free_text", "as_of_date", "session_id"}
)


class _AllowlistedEnvironmentProvider(EnvProvider):  # type: ignore[misc]
    """Expose only this agent's declared Marketplace environment values."""

    def __init__(self) -> None:
        super().__init__(namespace=_NAMESPACE, agent_name=_PROVIDER_AGENT_NAME)

    def get(self, key: str, default: str | None = None) -> str | None:
        if key not in _ALLOWED_SECRET_KEYS:
            return default
        value = super().get(key)
        return value if value else default


def _validate_scope_declaration() -> None:
    """Reject malformed part adaptation before any graph is constructed."""

    if not isinstance(SCOPE_KEYS, tuple):
        raise RuntimeError("Marketplace SCOPE_KEYS must be a tuple")
    if not isinstance(STG_DEFAULT_SCOPE_ENVS, Mapping) or set(STG_DEFAULT_SCOPE_ENVS) != set(SCOPE_KEYS):
        raise RuntimeError("Marketplace STG_DEFAULT_SCOPE_ENVS keys must exactly match SCOPE_KEYS")
    if len(set(SCOPE_KEYS)) != len(SCOPE_KEYS):
        raise RuntimeError("Marketplace SCOPE_KEYS must not contain duplicates")
    for key in SCOPE_KEYS:
        if not isinstance(key, str) or not key or key != key.strip() or not key.isidentifier():
            raise RuntimeError("Marketplace SCOPE_KEYS contains an invalid key")
        env_name = STG_DEFAULT_SCOPE_ENVS[key]
        if not isinstance(env_name, str) or not env_name or any(character.isspace() for character in env_name):
            raise RuntimeError("Marketplace STG_DEFAULT_SCOPE_ENVS contains an invalid environment name")


def _scope_from_environment(provider: SecretProvider) -> dict[str, str]:
    """Resolve every declared scope value without trimming or defaulting it."""

    _validate_scope_declaration()
    scope: dict[str, str] = {}
    for key in SCOPE_KEYS:
        env_name = STG_DEFAULT_SCOPE_ENVS[key]
        value = provider.require(env_name)
        if not value or any(character.isspace() for character in value) or _IDENTIFIER.fullmatch(value) is None:
            raise ValueError(
                f"Marketplace scope environment variable {env_name!r} "
                "must be a clean non-empty identifier without whitespace"
            )
        scope[key] = value
    return scope


def _require_clean_secret(provider: SecretProvider, key: str) -> str:
    value = provider.require(key)
    if not value.strip() or value != value.strip() or "\n" in value or "\r" in value:
        raise ValueError(f"Marketplace secret {key!r} must be a clean non-empty string")
    return value


def _require_azure_endpoint(provider: SecretProvider) -> str:
    """Reject ``AZURE_OPENAI_ENDPOINT`` values that are not a bare Azure resource endpoint.

    ``AzureOpenAIClient`` appends its own ``/openai/v1/`` path, so anything
    beyond a scheme and host silently produces a broken request URL, and the
    failure surfaces inside ``complete()``. A node that swallows that exception
    degrades quietly while the invocation still reports ``status: success``,
    which is why this has to be a startup check. The value is reported here,
    never repaired.
    """

    endpoint = _require_clean_secret(provider, "AZURE_OPENAI_ENDPOINT")
    parts = urlsplit(endpoint)
    try:
        port = parts.port
    except ValueError:
        port = -1
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or port == -1
        or parts.path.strip("/")
        or parts.query
        or parts.fragment
        or any(character.isspace() for character in endpoint)
    ):
        raise ValueError(
            "Marketplace environment variable 'AZURE_OPENAI_ENDPOINT' must be a bare Azure "
            "resource endpoint -- scheme and host only, with no path (such as "
            "'/openai'), query or fragment"
        )
    return endpoint


class _AzureMarketplaceLLM(BaseLLM):  # type: ignore[misc]
    """Create an Azure client per operation and retain no connection values."""

    def __init__(self, *, tools: tuple[Any, ...] = ()) -> None:
        if tools:
            self._tools = tools

    def _new_client(self) -> BaseLLM:
        provider = current_secrets()
        values = {key: _require_clean_secret(provider, key) for key in _AZURE_OPENAI_SECRET_KEYS}

        # Azure support is an optional SDK extra. This is deliberately the only
        # invocation-time import; framework, security, and graph imports above
        # stay eager for the Marketplace runner's asyncio.to_thread path.
        from shared.services.llm.azure_openai_client import AzureOpenAIClient

        client: BaseLLM = AzureOpenAIClient(
            {
                "api_key": values["AZURE_OPENAI_API_KEY"],
                "azure_endpoint": values["AZURE_OPENAI_ENDPOINT"],
                "azure_deployment": values["AZURE_OPENAI_DEPLOYMENT"],
                "response_format": dict(_MARKETPLACE_RESPONSE_FORMAT),
            }
        )
        tools = getattr(self, "_tools", ())
        return client.bind_tools(list(tools)) if tools else client

    def complete(self, messages: list[Any]) -> dict[str, Any]:
        result = cast(dict[str, Any], self._new_client().complete(messages))
        _observe_json_output(result)
        return result

    def stream(self, messages: list[Any]) -> Iterator[str]:
        yield from self._new_client().stream(messages)

    def bind_tools(self, tools: list[Any]) -> "_AzureMarketplaceLLM":
        return type(self)(tools=tuple(tools))


def _observe_json_output(result: Mapping[str, Any]) -> None:
    """Log shape-only facts when the shared draft parser would reject the completion.

    The Marketplace runner drops ``error_log`` from its failure event, so this
    WARNING line in the Pod log is the only place a parse failure can be seen.
    It applies exactly the part's acceptance rule (``parse_json_object``) and
    carries the size, the token count and the rejection reason -- never the
    model text itself. Observation must never change the outcome, so every
    exception is swallowed here.
    """

    try:
        content = result.get("content")
        usage = result.get("usage")
        tokens = usage.get("completion_tokens") if isinstance(usage, Mapping) else None
        try:
            parse_json_object(content)
        except ValueError as exc:
            _log.warning(
                "marketplace llm output rejected by the draft parser: chars=%s completion_tokens=%s reason=%s: %s",
                len(content) if isinstance(content, str) else None,
                tokens,
                type(exc).__name__,
                str(exc)[:80],
            )
    except Exception as exc:  # noqa: BLE001 - observation only
        _log.warning("marketplace llm output could not be observed: %s", type(exc).__name__)


def _marketplace_payload(user_input: str) -> tuple[dict[str, Any], str]:
    """Normalize one string into the read-only invoke payload contract."""

    if not isinstance(user_input, str):
        raise TypeError("Marketplace input must be one plain string")
    try:
        decoded = json.loads(user_input)
    except json.JSONDecodeError:
        decoded = None

    if decoded is None or not isinstance(decoded, dict):
        return (
            {
                "alert_type": _DEFAULT_ALERT_TYPE,
                "evidence_refs": [],
                "free_text": user_input,
            },
            f"marketplace-{uuid4().hex}",
        )

    undeclared = set(decoded) - _MARKETPLACE_INPUT_KEYS
    if undeclared:
        raise ValueError(f"Marketplace input contains undeclared keys: {sorted(undeclared)}")
    if decoded.get("mode", "invoke") != "invoke":
        raise ValueError("Marketplace write modes are not available")

    case_ref = decoded.get("case_ref") or f"marketplace-{uuid4().hex}"
    if not isinstance(case_ref, str) or _IDENTIFIER.fullmatch(case_ref) is None:
        raise ValueError("Marketplace case_ref must be a clean identifier")
    payload = {
        "alert_type": decoded.get("alert_type", _DEFAULT_ALERT_TYPE),
        "evidence_refs": decoded.get("evidence_refs", []),
        "free_text": decoded.get("free_text", ""),
    }
    return payload, case_ref


def _evidence_control_metadata(payload: dict[str, Any]) -> list[dict[str, str]]:
    """Project only non-body source controls into the checkpoint-safe envelope."""

    evidence_refs = payload.get("evidence_refs")
    if not isinstance(evidence_refs, list):
        return []
    projected: list[dict[str, str]] = []
    for item in evidence_refs:
        if not isinstance(item, dict):
            continue
        source_id = item.get("source_id")
        kind = item.get("kind")
        if (
            isinstance(source_id, str)
            and _IDENTIFIER.fullmatch(source_id) is not None
            and isinstance(kind, str)
            and kind.isascii()
            and len(kind) <= 40
        ):
            projected.append({"source_id": source_id, "kind": kind})
    return projected


def _as_of_date(user_input: str) -> str:
    """Resolve an optional JSON input date once at the adapter boundary."""

    try:
        decoded = json.loads(user_input)
    except json.JSONDecodeError:
        decoded = None
    candidate = decoded.get("as_of_date") if isinstance(decoded, dict) else None
    if candidate is None:
        return CalendarDate.today().isoformat()
    if not isinstance(candidate, str):
        raise ValueError("Marketplace as_of_date must be an ISO-8601 date")
    try:
        return CalendarDate.fromisoformat(candidate).isoformat()
    except ValueError:
        raise ValueError("Marketplace as_of_date must be an ISO-8601 date") from None


def _marketplace_config(config: dict[str, Any] | None, scope: dict[str, str]) -> dict[str, Any]:
    source = dict(config or {})
    ttl = source.get("payload_ttl_seconds", 900)
    if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl <= 0:
        raise ValueError("payload_ttl_seconds must be a positive integer")
    scope_keys = source.get("scope_keys", ["institution_id"])
    if scope_keys != ["institution_id"]:
        raise ValueError("FIN-C2-161 requires scope_keys=['institution_id']")
    return {
        **source,
        "llm": _AzureMarketplaceLLM(),
        "payload_store": source.get("payload_store") or build_payload_store(ttl_seconds=ttl),
        "ledger_service": source.get("ledger_service") or build_ledger_service(),
        "feedback_receipt_store": source.get("feedback_receipt_store") or InMemoryFeedbackReceiptStore(),
        "trusted_scope": {"institution_id": scope["institution_id"]},
        "request_session_id": _MARKETPLACE_SESSION_ID,
        "memory_enabled": False,
        "hitl": {"enabled": False},
    }


class MarketplaceGraph(AMLDispositionMemoAgent):
    """Marketplace-only read path, scope, secret, and Azure boundary."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        # The runner compiles before provision_secrets(). Scope and the
        # credential-free request dependencies must therefore fail closed here.
        scope = _scope_from_environment(_AllowlistedEnvironmentProvider())
        super().__init__(config=_marketplace_config(config, scope))

    def invoke(
        self,
        user_input: str,
        session_id: str = "",
        ctx: InvocationContext | None = None,
        input_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Runner conversation history is untrusted text outside this AG's S-2
        # scan. Rebuild adapter-owned context; do not merge any runner key.
        del input_context, session_id
        scope = _scope_from_environment(_AllowlistedEnvironmentProvider())
        payload, case_ref = _marketplace_payload(user_input)
        as_of_date = _as_of_date(user_input)
        payload_store = self.config["payload_store"]
        raw_envelope = {
            "mode": "invoke",
            "case_ref": case_ref,
            **scope,
            "evidence_refs": _evidence_control_metadata(payload),
            "payload": {"raw": json.dumps(payload, ensure_ascii=False)},
        }
        safe_input = scrub_checkpoint_envelope(
            json.dumps(raw_envelope, ensure_ascii=False),
            payload_store,
            session_id=_MARKETPLACE_SESSION_ID,
            trusted_scope={"institution_id": scope["institution_id"]},
            policy=CheckpointScrubPolicy(
                safe_keys=frozenset({"mode", "case_ref", "unit_id"}),
                scope_keys=frozenset({"institution_id"}),
                enum_value_keys={"mode": frozenset({"invoke"})},
                safe_subtree_keys=frozenset({"evidence_refs"}),
            ),
        )
        payload_ref = json.loads(safe_input)["payload_ref"]
        marketplace_ctx = (
            replace(
                ctx,
                session_id=_MARKETPLACE_SESSION_ID,
                caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
                hitl_allowed=False,
            )
            if ctx is not None
            else InvocationContext(
                session_id=_MARKETPLACE_SESSION_ID,
                caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
                hitl_allowed=False,
            )
        )
        try:
            result = cast(
                dict[str, Any],
                super().invoke(
                    safe_input,
                    session_id=_MARKETPLACE_SESSION_ID,
                    ctx=marketplace_ctx,
                    input_context={"scope": scope, "as_of_date": as_of_date},
                ),
            )
            if result.get("status") == AgentStatus.AWAITING_HUMAN.value:
                return {
                    "output": None,
                    "status": AgentStatus.ERROR.value,
                    "error_log": ["MarketplaceGraph: awaiting_human_not_supported"],
                }
            return result
        finally:
            payload_store.delete(
                payload_ref,
                scope={"institution_id": scope["institution_id"]},
                session_id=_MARKETPLACE_SESSION_ID,
            )

    def provision_secrets(self, provider: SecretProvider) -> None:
        # Ignore the runner's broader/dotenv-capable provider. Missing Azure or
        # scope values must make startup fail before the first invocation.
        del provider
        environment_provider = _AllowlistedEnvironmentProvider()
        for key in _AZURE_OPENAI_SECRET_KEYS:
            _require_clean_secret(environment_provider, key)
        _scope_from_environment(environment_provider)
        _require_azure_endpoint(environment_provider)
        super().provision_secrets(environment_provider)


def main() -> None:
    run_agent_marketplace(
        MarketplaceGraph,
        agent_name=_AGENT_NAME,
        namespace=_NAMESPACE,
        config=load_agent_config(Path(__file__).resolve().parent),
    )


if __name__ == "__main__":
    main()
