# PART: accumulation-ledger v0.1.1 (parts@86cf1c6)
"""Text and scope guards shared by the accumulation ledger.

The ledger deliberately derives the model-visible value from the raw value on
every write.  Callers cannot provide ``*_audit`` or ``*_safe`` values because
that would make the separation bypassable when this part is copied into a
template.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from shared.security.pii_detector import detect_pii


_INSTRUCTION_PATTERN = re.compile(
    r"ignore\s+(?:all\s+)?previous|system\s+prompt|developer\s+message|"
    r"以後の指示|前の指示を無視|システムプロンプト",
    re.IGNORECASE,
)


class LedgerValidationError(ValueError):
    """Raised for invalid ledger input; callers must surface the rejection."""


@dataclass(frozen=True)
class SanitizedText:
    raw: str
    audit: str
    safe: str


def validate_scope_keys(scope_keys: Sequence[str]) -> tuple[str, ...]:
    """Validate the same declarative scope-key shape as scoped-retrieval."""

    if isinstance(scope_keys, (str, bytes)) or not isinstance(scope_keys, Sequence):
        raise ValueError("LedgerService: scope_keys must be a sequence of key names, not a bare string")
    keys = tuple(scope_keys)
    if not keys:
        raise ValueError("LedgerService: scope_keys must declare at least one KB partition key")
    if len(set(keys)) != len(keys):
        raise ValueError(f"LedgerService: scope_keys contains duplicates: {list(keys)}")
    for key in keys:
        if not isinstance(key, str) or not key or key != key.strip() or not key.isidentifier():
            raise ValueError("LedgerService: scope_keys entries must be non-empty identifier strings")
    return keys


def require_scope(scope: Mapping[str, Any] | None, scope_keys: Sequence[str]) -> dict[str, str]:
    """Bind one request to exactly the template-declared KB partition."""

    if not isinstance(scope, Mapping) or set(scope) != set(scope_keys):
        raise LedgerValidationError("ledger_scope_missing_or_invalid")
    if any(not valid_scope_value(scope[key]) for key in scope_keys):
        raise LedgerValidationError("ledger_scope_missing_or_invalid")
    return {key: scope[key] for key in scope_keys}


def valid_scope_value(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and value == value.strip()
        and "\n" not in value
        and "\r" not in value
    )


def sanitize_text(value: str | None, *, field_name: str, max_chars: int) -> SanitizedText | None:
    """Create ``raw/audit/safe`` values without allowing unsafe safe text.

    PII is replaced with ``[MASKED]`` in the audit and model-visible copies.
    Inputs beyond the declared bound and instruction-like content are rejected
    explicitly; neither is silently dropped or truncated.
    """

    if value is None:
        return None
    if not isinstance(value, str):
        raise LedgerValidationError(f"{field_name}_must_be_string")
    # Keep the order explicit: detect/mask PII, apply the declared bound, then
    # normalize and reject instruction-shaped model text.
    audit = _mask_pii(value)
    if len(audit) > max_chars:
        raise LedgerValidationError(f"{field_name}_too_long")
    safe = unicodedata.normalize("NFKC", audit)
    if len(safe) > max_chars:
        raise LedgerValidationError(f"{field_name}_too_long_after_normalization")
    if _INSTRUCTION_PATTERN.search(safe):
        raise LedgerValidationError(f"{field_name}_instruction_detected")

    # Normalization can make a detector pattern visible that did not exist in
    # the source representation.  Re-mask before the value becomes model
    # visible, making the no-PII-safe-text property independent of input form.
    safe = _mask_pii(safe)
    return SanitizedText(raw=value, audit=audit, safe=safe)


def sanitize_retrieval_text(value: Any, *, max_chars: int) -> str:
    """Validate the structured-only text used by a retrieval index.

    Unlike body/rationale, retrieval text has no raw storage column.  PII or
    instruction-like content is therefore rejected rather than copied into an
    index projection.
    """

    if not isinstance(value, str) or not value.strip():
        raise LedgerValidationError("retrieval_text_required")
    if len(value) > max_chars:
        raise LedgerValidationError("retrieval_text_too_long")
    normalized = unicodedata.normalize("NFKC", value)
    if len(normalized) > max_chars:
        raise LedgerValidationError("retrieval_text_too_long_after_normalization")
    if _INSTRUCTION_PATTERN.search(normalized):
        raise LedgerValidationError("retrieval_text_instruction_detected")
    if _pii_findings(normalized):
        raise LedgerValidationError("retrieval_text_pii_detected")
    return normalized


def _mask_pii(value: str) -> str:
    masked = value
    findings = _pii_findings(value)
    valid_findings: list[tuple[int, int]] = []
    for finding in findings:
        if not isinstance(finding, Mapping):
            continue
        start, end = finding.get("start"), finding.get("end")
        if isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(value):
            valid_findings.append((start, end))
    for start, end in sorted(valid_findings, reverse=True):
        masked = masked[:start] + "[MASKED]" + masked[end:]
    return masked


def _pii_findings(value: str) -> list[dict[str, Any]]:
    findings = detect_pii(value)
    return list(findings or [])
