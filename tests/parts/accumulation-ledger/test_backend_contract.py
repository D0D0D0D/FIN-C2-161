"""Conformance suite every production LedgerBackend implementation must pass."""

from __future__ import annotations

import pytest

from accumulation_ledger import InMemoryLedgerBackend


SCOPE_A = {"company_id": "co-a", "case_id": "case-a"}
SCOPE_B = {"company_id": "co-a", "case_id": "case-b"}


@pytest.fixture(params=[InMemoryLedgerBackend], ids=["in_memory"])
def backend(request):
    return request.param()


def record(record_id, scope, *, recorded_at="2026-07-01T00:00:00+00:00"):
    return {
        "record_id": record_id,
        "scope": scope,
        "recorded_at": recorded_at,
        "classification": {"kind": "observation"},
        "body_raw": "raw body",
        "rationale_raw": "raw rationale",
        "verdict_code": "confirmed",
    }


def test_insert_get_and_scope_isolation(backend):
    backend.insert(record("ledger_abc", SCOPE_A))

    fetched = backend.get("ledger_abc", scope=SCOPE_A)
    assert fetched is not None
    assert fetched["record_id"] == "ledger_abc"
    assert backend.get("ledger_abc", scope=SCOPE_B) is None


def test_records_are_copied_at_backend_boundary(backend):
    original = record("ledger_copy", SCOPE_A)
    backend.insert(original)
    original["body_raw"] = "tampered caller value"

    fetched = backend.get("ledger_copy", scope=SCOPE_A)
    assert fetched["body_raw"] == "raw body"
    fetched["body_raw"] = "tampered returned value"
    assert backend.get("ledger_copy", scope=SCOPE_A)["body_raw"] == "raw body"


def test_update_and_query_are_scoped_and_deterministic(backend):
    backend.insert(record("ledger_z", SCOPE_A))
    backend.insert(record("ledger_a", SCOPE_A))
    backend.insert(record("ledger_b", SCOPE_B))

    assert backend.update("ledger_a", scope=SCOPE_B, changes={"verdict_code": "declined"}) is None
    assert backend.update("ledger_a", scope=SCOPE_A, changes={"verdict_code": "declined"})["verdict_code"] == "declined"
    assert [item["record_id"] for item in backend.query(scope=SCOPE_A)] == ["ledger_a", "ledger_z"]


def test_purge_raw_removes_only_raw_columns_in_the_requested_scope(backend):
    backend.insert(record("ledger_old", SCOPE_A, recorded_at="2026-01-01T00:00:00+00:00"))
    backend.insert(record("ledger_other", SCOPE_B, recorded_at="2026-01-01T00:00:00+00:00"))

    assert backend.purge_raw(scope=SCOPE_A, cutoff="2026-02-01T00:00:00+00:00") == ["ledger_old"]
    old = backend.get("ledger_old", scope=SCOPE_A)
    assert old["body_raw"] is None
    assert old["rationale_raw"] is None
    assert old["classification"] == {"kind": "observation"}
    assert old["verdict_code"] == "confirmed"
    assert backend.get("ledger_other", scope=SCOPE_B)["body_raw"] == "raw body"


def test_purge_raw_compares_iso_timestamps_as_instants_not_lexicographic_strings(backend):
    backend.insert(record("ledger_offset", SCOPE_A, recorded_at="2026-07-29T10:00:00+09:00"))

    assert backend.purge_raw(scope=SCOPE_A, cutoff="2026-07-29T05:00:00+00:00") == ["ledger_offset"]
    assert backend.get("ledger_offset", scope=SCOPE_A)["body_raw"] is None
