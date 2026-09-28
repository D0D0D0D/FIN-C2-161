# PART: ingest-row-pipeline v0.4.0 (parts@86cf1c6)
"""Generic row ingest pipeline node.

The part owns row orchestration and the `ingest_summary` contract. Domain
templates inject sanitization, normalization, and persistence hooks.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable, ClassVar, Protocol

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event


class PayloadResolver(Protocol):
    def resolve(
        self,
        ref: str | None,
        *,
        scope: Mapping[str, str],
        session_id: str,
        consume: bool = True,
    ) -> Any | None: ...


SanitizeHook = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]
NormalizeHook = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]
WriteHook = Callable[[dict[str, Any], dict[str, Any]], str]
BatchWriteHook = Callable[[dict[str, Any], list[dict[str, Any]]], list[str]]


@dataclass(frozen=True)
class RowPipelineHooks:
    sanitize: SanitizeHook | None = None
    normalize: NormalizeHook | None = None
    write: WriteHook | None = None
    write_batch: BatchWriteHook | None = None


class IngestRowPipelineNode(FunctionNode):
    """Required-fields -> sanitize -> normalize -> write -> ingest_summary."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL
    required_fields: ClassVar[tuple[str, ...]] = ("site_id", "observed_at")
    required_any_fields: ClassVar[tuple[tuple[str, ...], ...]] = (("free_text", "category"),)

    def __init__(
        self,
        *,
        payload_store: PayloadResolver | None = None,
        trusted_scope: Mapping[str, str] | None = None,
        session_id: str | None = None,
        hooks: RowPipelineHooks | None = None,
        allow_unsanitized_writes: bool = False,
        allow_unnormalized_writes: bool = False,
        reject_batch_on_validation_error: bool = False,
    ) -> None:
        super().__init__()
        self._payload_resolver = payload_store
        self._trusted_scope = dict(trusted_scope) if isinstance(trusted_scope, Mapping) else None
        self._session_id = session_id
        self._hooks = hooks or RowPipelineHooks()
        self._allow_unsanitized_writes = allow_unsanitized_writes
        self._allow_unnormalized_writes = allow_unnormalized_writes
        self._reject_batch_on_validation_error = reject_batch_on_validation_error
        self._validate_hook_config()

    def execute(self, state: dict) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            return {}

        rows, resolve_error = self.resolve_rows(state)
        if resolve_error:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [resolve_error],
            }
        prepared: list[tuple[int, dict[str, Any]]] = []
        rejected: list[dict[str, Any]] = []

        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                rejected.append({"index": index, "reasons": ["row_not_object"]})
                continue

            reasons = self.check_required_fields(row, state)
            if reasons:
                rejected.append({"index": index, "reasons": reasons})
                continue

            try:
                sanitized = self.sanitize_row(state, dict(row))
                normalized = self.normalize_row(state, sanitized)
                prepared.append((index, normalized))
            except Exception as exc:  # noqa: BLE001 - validation failures are summarized.
                rejected.append({"index": index, "reasons": [f"write_pipeline_error: {exc}"]})

        written_ids: list[str] = []
        if self._reject_batch_on_validation_error and rejected:
            rejected.extend(
                {
                    "index": index,
                    "reasons": ["batch_aborted_due_to_rejected_rows"],
                }
                for index, _row in prepared
            )
        elif self._hooks.write_batch is not None and prepared:
            try:
                written_ids = self.write_batch(state, [row for _index, row in prepared])
            except Exception as exc:  # noqa: BLE001 - the hook owns rollback.
                rejected.extend(
                    {
                        "index": index,
                        "reasons": [f"batch_write_error: {exc}"],
                    }
                    for index, _row in prepared
                )
        else:
            for index, row in prepared:
                try:
                    written_ids.append(self.write_row(state, row))
                except Exception as exc:  # noqa: BLE001 - per-row failures are summarized.
                    rejected.append(
                        {"index": index, "reasons": [f"write_pipeline_error: {exc}"]}
                    )

        rejected.sort(key=lambda item: item["index"])

        summary = {
            "written_count": len(written_ids),
            "written_ids": written_ids,
            "rejected_count": len(rejected),
            "rejected": rejected,
        }
        emit_trace_event(
            "ingest_row_pipeline_complete",
            {"written_count": len(written_ids), "rejected_count": len(rejected)},
            state,
        )
        return {"ingest_summary": summary, "status": AgentStatus.SUCCESS.value}

    def resolve_rows(self, state: dict) -> tuple[list[Any], str | None]:
        payload_ref = state.get("payload_ref")
        if payload_ref:
            if self._payload_resolver is None:
                return [], "IngestRowPipelineNode: payload_ref unresolvable (payload_store not configured)"
            scope, session_id = self._require_payload_context()
            try:
                raw = self._payload_resolver.resolve(
                    payload_ref,
                    scope=scope,
                    session_id=session_id,
                    consume=True,
                )
            except ValueError:
                return [], "IngestRowPipelineNode: payload_ref unresolvable"
            if raw is None:
                return [], "IngestRowPipelineNode: payload_ref unresolvable"
            parsed = _parse_jsonish(raw)
            if isinstance(parsed, dict) and isinstance(parsed.get("records"), list):
                return parsed["records"], None
            if isinstance(parsed, list):
                return parsed, None
            return [], "IngestRowPipelineNode: payload_ref unresolvable (payload parse failed)"
        rows = state.get("rows")
        return (rows if isinstance(rows, list) else []), None

    def check_required_fields(self, row: dict[str, Any], state: dict) -> list[str]:
        reasons: list[str] = []
        for field in self.required_fields:
            if _is_missing(row.get(field)) and _is_missing(state.get(field)):
                reasons.append(f"missing_{field}")
        for group in self.required_any_fields:
            if not any(not _is_missing(row.get(field)) for field in group):
                reasons.append("missing_" + "_or_".join(group))
        return reasons

    def sanitize_row(self, state: dict, row: dict[str, Any]) -> dict[str, Any]:
        if self._hooks.sanitize is None:
            if not self._allow_unsanitized_writes:
                raise RuntimeError("sanitize hook not configured")
            return row
        return self._hooks.sanitize(state, row)

    def normalize_row(self, state: dict, row: dict[str, Any]) -> dict[str, Any]:
        if self._hooks.normalize is None:
            if not self._allow_unnormalized_writes:
                raise RuntimeError("normalize hook not configured")
            return row
        return self._hooks.normalize(state, row)

    def write_row(self, state: dict, row: dict[str, Any]) -> str:
        if self._hooks.write is None:
            raise RuntimeError("write hook not configured")
        return self._hooks.write(state, row)

    def write_batch(self, state: dict, rows: list[dict[str, Any]]) -> list[str]:
        """Call one template-owned atomic persistence boundary.

        The hook must commit every row or raise without committing any row.
        IDs must correspond one-to-one with ``rows`` in the same order.
        """

        if self._hooks.write_batch is None:
            raise RuntimeError("write_batch hook not configured")
        written_ids = self._hooks.write_batch(state, rows)
        if (
            not isinstance(written_ids, list)
            or len(written_ids) != len(rows)
            or any(not isinstance(item, str) or not item for item in written_ids)
        ):
            raise RuntimeError("write_batch hook returned invalid written_ids")
        return written_ids

    def _extra_security_gate_input(self, state: dict) -> dict:
        return state

    def _validate_hook_config(self) -> None:
        if self._hooks.write is not None and self._hooks.write_batch is not None:
            raise ValueError(
                "IngestRowPipelineNode: write and write_batch hooks are mutually exclusive"
            )
        if self._reject_batch_on_validation_error and self._hooks.write_batch is None:
            raise ValueError(
                "IngestRowPipelineNode: reject_batch_on_validation_error requires a "
                "write_batch hook"
            )
        if self._hooks.write is None and self._hooks.write_batch is None:
            return
        if self._hooks.sanitize is None and not self._allow_unsanitized_writes:
            raise ValueError(
                "IngestRowPipelineNode: write hook configured but sanitize hook is missing. "
                "Set allow_unsanitized_writes=True only for tests."
            )
        if self._hooks.normalize is None and not self._allow_unnormalized_writes:
            raise ValueError(
                "IngestRowPipelineNode: write hook configured but normalize hook is missing. "
                "Set allow_unnormalized_writes=True only for tests."
            )

    def _require_payload_context(self) -> tuple[Mapping[str, str], str]:
        if self._trusted_scope is None or not self._trusted_scope:
            raise ValueError(
                "IngestRowPipelineNode: trusted_scope must be passed explicitly from AuthContext.scope"
            )
        if not _valid_scope_value(self._session_id):
            raise ValueError(
                "IngestRowPipelineNode: session_id must be a clean non-empty string"
            )
        return self._trusted_scope, self._session_id


def _parse_jsonish(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return None


def _is_missing(value: Any) -> bool:
    return value is None or value == ""


def _valid_scope_value(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and value == value.strip()
        and "\n" not in value
        and "\r" not in value
    )
