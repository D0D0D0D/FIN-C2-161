"""FIN-C2-161 wiring for the copied accumulation-ledger part."""

from __future__ import annotations

from collections.abc import Callable

from src.accumulation_ledger import InMemoryLedgerBackend, LedgerBackend, LedgerConfig, LedgerService


DISPOSITION_VERDICTS = frozenset({"reviewed", "escalated", "closed"})
ALERT_TYPES = frozenset({"cash", "sanctions", "transaction_monitoring", "kyc"})


def build_ledger_service(
    *,
    clock: Callable[[], str] | None = None,
    backend: LedgerBackend | None = None,
) -> LedgerService:
    """Create the scoped, in-memory ledger used by STG and test injection seams."""

    return LedgerService(
        LedgerConfig(
            scope_keys=("institution_id",),
            record_kinds=frozenset({"disposition_record"}),
            classification_codes={"alert_type": ALERT_TYPES},
            verdict_codes=DISPOSITION_VERDICTS,
            domain_fields={"unit_id": "scalar", "case_ref": "scalar"},
            conflict_key_fields=("case_ref",),
            supersede_mode="auto",
            record_id_prefix="amlmemo",
        ),
        backend=backend or InMemoryLedgerBackend(),
        clock=clock,
    )
