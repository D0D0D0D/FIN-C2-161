"""R-1/R-2: real SQLite filtering and stored-scope projection."""

from __future__ import annotations

import sqlite3
import json
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from fastapi.testclient import TestClient
from framework.schemas.trust_level import TrustLevel

from src.api import server
from src.scoped_retrieval import ScopedRetrievalNode
from src.services import build_ledger_service


class SQLiteBackend:
    def __init__(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute(
            "CREATE TABLE perspective (id TEXT PRIMARY KEY, institution_id TEXT NOT NULL, body TEXT NOT NULL)"
        )

    def insert(self, record_id: str, institution_id: str, body: str) -> None:
        self.connection.execute(
            "INSERT INTO perspective(id, institution_id, body) VALUES (?, ?, ?)",
            (record_id, institution_id, body),
        )
        self.connection.commit()

    def search(self, *, scope, query: str, top_k: int):
        rows = self.connection.execute(
            "SELECT id, institution_id, body FROM perspective "
            "WHERE institution_id = ? AND body LIKE ? ORDER BY id LIMIT ?",
            (scope["institution_id"], f"%{query}%", top_k),
        ).fetchall()
        return [
            {"source_id": row[0], "institution_id": row[1], "summary": row[2]}
            for row in rows
        ]


def test_real_sqlite_returns_one_stored_scope_hit_and_hides_other_partition():
    backend = SQLiteBackend()
    backend.insert("same-scope", "bank-a", "needle perspective")
    backend.insert("other-scope", "bank-b", "needle perspective")
    node = ScopedRetrievalNode(
        scope_keys=("institution_id",),
        backend=backend,
        normalize_query=lambda value: str(value),
    )

    result = node(
        {
            "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
            "correlation_id": "sqlite-scope",
            "execution_time": {},
            "institution_id": "bank-a",
            "query": "needle",
        }
    )

    assert [item["source_id"] for item in result["retrieved_records"]] == ["same-scope"]
    assert result["retrieved_records"][0]["metadata"]["institution_id"] == "bank-a"


class SQLiteLedgerBackend:
    """Real SQLite implementation of the production ledger backend seam."""

    def __init__(self) -> None:
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.connection.execute(
            "CREATE TABLE ledger (record_id TEXT PRIMARY KEY, institution_id TEXT NOT NULL, body TEXT NOT NULL)"
        )

    def insert(self, record: Mapping[str, Any]) -> str:
        record_id = str(record["record_id"])
        scope = record.get("scope")
        institution_id = scope.get("institution_id") if isinstance(scope, Mapping) else None
        if not isinstance(institution_id, str):
            raise ValueError("sqlite_scope_required")
        with self.connection:
            self.connection.execute(
                "INSERT INTO ledger(record_id, institution_id, body) VALUES (?, ?, ?)",
                (record_id, institution_id, json.dumps(dict(record), ensure_ascii=False)),
            )
        return record_id

    def get(self, record_id: str, *, scope: Mapping[str, str]) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT body FROM ledger WHERE record_id = ? AND institution_id = ?",
            (record_id, scope["institution_id"]),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def update(
        self,
        record_id: str,
        *,
        scope: Mapping[str, str],
        changes: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        current = self.get(record_id, scope=scope)
        if current is None:
            return None
        current.update(deepcopy(dict(changes)))
        with self.connection:
            self.connection.execute(
                "UPDATE ledger SET body = ? WHERE record_id = ? AND institution_id = ?",
                (json.dumps(current, ensure_ascii=False), record_id, scope["institution_id"]),
            )
        return current

    def query(self, *, scope: Mapping[str, str]) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT body FROM ledger WHERE institution_id = ? ORDER BY record_id",
            (scope["institution_id"],),
        ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def purge_raw(self, *, scope: Mapping[str, str], cutoff: str) -> list[str]:
        return []


def _ingest_record(case_ref: str, note: str) -> dict[str, Any]:
    return {
        "case_ref": case_ref,
        "unit_id": "aml-a",
        "alert_type": "cash",
        "occurred_at": "2026-01-01T00:00:00+00:00",
        "verdict_code": "reviewed",
        "decided_by": "reviewer",
        "decided_at": "2026-01-02T00:00:00+00:00",
        "body": "reviewed case",
        "checked_questions": ["fund transfer purpose"],
        "evidence_categories": ["transaction_record"],
        "countervailing_notes": [note],
    }


def test_real_sqlite_scope_predicate_is_used_by_the_production_http_composition():
    backend = SQLiteLedgerBackend()
    config = server._load_runtime_config()
    config["ledger_service"] = build_ledger_service(backend=backend)
    server.app.state.runtime_config_override = config
    identity = {
        "scope": {"institution_id": "bank-a", "unit_id": "aml-a"},
        "roles": ["aml_data_steward"],
    }
    server.app.state.verify_scope = lambda _request: dict(identity["scope"])
    server.app.state.verified_principal = lambda _request: {
        "trust_level": TrustLevel.VERIFIED_EXTERNAL,
        "caller_id": "sqlite-reviewer",
        "roles": list(identity["roles"]),
    }
    try:
        with TestClient(server.app, raise_server_exceptions=False) as client:
            assert client.post(
                "/ingest", json={"records": [_ingest_record("case-a", "same-scope-note")]}
            ).json()["status"] == "success"
            identity["scope"] = {"institution_id": "bank-b", "unit_id": "aml-a"}
            assert client.post(
                "/ingest", json={"records": [_ingest_record("case-b", "foreign-scope-note")]}
            ).json()["status"] == "success"

            identity["scope"] = {"institution_id": "bank-a", "unit_id": "aml-a"}
            identity["roles"] = []
            invoked = client.post(
                "/invoke",
                json={
                    "case_ref": "case-new",
                    "alert_type": "cash",
                    "evidence_refs": [{"source_id": "txna", "kind": "transaction_record"}],
                    "free_text": "fund transfer purpose",
                },
            )

        assert invoked.json()["status"] == "success"
        output = invoked.json()["output"]
        assert output["countervailing_factors"] == []
        assert any("same-scope-note" in item for item in output["unknowns"])
        assert not any("foreign-scope-note" in item for item in output["unknowns"])
    finally:
        server.app.state.runtime_config_override = None
        backend.connection.close()
