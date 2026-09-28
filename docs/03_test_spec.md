# FIN-C2-161 Test Specification

## Scope

Tests use injected ledger, payload-store, and LLM seams and never contact an
external service. R-1/R-2 additionally use an in-process real SQLite database
to prove query filtering plus stored-record scope projection.

## Framework compliance

| ID | Coverage | Test location | Result |
|---|---|---|---|
| TC-01 | Flat `AMLDispositionState`; forbidden decision fields absent | `tests/unit/test_domain_security.py` | PASS |
| TC-02 | Invalid S-2 input raises `SecurityViolationError` | `tests/unit/test_domain_security.py` | PASS |
| TC-03 | State has no credential-like fields | `tests/proof_of_boundary/test_state_safety.py` | PASS |
| TC-04 | `InvocationContext` is created only at the server adapter; nodes use State | AST/manual contract review plus `tests/proof_of_boundary/test_import_isolation.py` | PASS |
| TC-05 | No duplicate framework lifecycle events in domain `execute()` | `tests/proof_of_boundary/test_pb_audit_and_checkpoint.py` | PASS |
| TC-06 | Final S-2 gate cannot be overridden | `tests/unit/test_framework_compliance_tc06_tc07.py` | PASS |
| TC-07 | Final S-3 gate cannot be overridden | `tests/unit/test_framework_compliance_tc06_tc07.py` | PASS |
| TC-08 | Every domain node declares `VERIFIED_EXTERNAL` | `tests/unit/test_domain_security.py` | PASS |

## Boundary proofs

| ID | Boundary and assertion | Test location | Result |
|---|---|---|---|
| PB-1 | `(module, event)` pairs are patched and observed for input validation, evidence check, and output-envelope request | `tests/proof_of_boundary/test_pb_audit_and_checkpoint.py` | PASS |
| PB-2 | Safe checkpoint JSON is structurally deserialized; injected phone data is vaulted, not checkpointed | `tests/proof_of_boundary/test_pb_audit_and_checkpoint.py` | PASS |
| PB-3 | LLM is a constructor/config injection seam; fake success, failure, and no-LLM paths are exercised | `tests/parts/llm-draft-stage/test_draft_node.py` | PASS |
| PB-4 | AST rejects Level-0 imports and top-level vendored imports; subprocess imports `src.graph.graph` without `repo/src` path | `tests/proof_of_boundary/test_import_isolation.py` | PASS |
| PB-5 | Checkpoint scrubber uses an allowlist and mutation-sensitive vault assertion | `tests/proof_of_boundary/test_pb_audit_and_checkpoint.py`, `tests/parts/ingest-row-pipeline/test_checkpoint_scrubber.py` | PASS |
| PB-6 | `pkgutil.walk_packages` dynamically collects every concrete local node; each call order is `node_start → S-2 → execute → S-3 → node_complete` | `tests/proof_of_boundary/test_pb_invoke_order.py` | PASS |
| PB-7 | Auto-waived: `agent.config.hitl.enabled` is `false`; the exact-name boundary test remains and auto-skips | `tests/proof_of_boundary/test_pb7_hitl_interrupt_propagation.py` | SKIPPED (expected) |

## Domain and security regression tests

| ID | Scenario | Expected result | Test location |
|---|---|---|---|
| D-01 | LLM unavailable | Deterministic five-section draft, empty memo body, human review required | `tests/unit/test_domain_security.py` |
| D-02 | Stage 2 prior record has verdict marker | Marker is absent from recall projection and Stage 3 prompt | `tests/unit/test_domain_security.py` |
| D-03 | Cross-institution ledger boundary | Other institution cannot read or update scoped record | `tests/parts/accumulation-ledger/test_ledger.py` |
| D-04 | Numeric provenance normalization | Unit-adjacent, particle-adjacent, sentence-period, full-width, comma, and compound `千`/`万`/`億` forms compare correctly on both claim and source paths | `tests/unit/test_domain_security.py` |
| D-05 | Existing but unrelated source ID | Attribute mismatch fails closed | `tests/unit/test_domain_security.py` |
| D-06 | Numeric fact exists only in prose | Output is demoted to `requires_human_review` | `tests/unit/test_domain_security.py` |
| D-07 | S-3 rejects unvalidated output | Outer `get_output()` returns `output: null` unless `success` and `formatted_output` exist | `tests/unit/test_main_node.py` and graph regression suite |
| D-08 | `/ingest` and `/feedback` RBAC | Unauthorized callers receive 403 before payload/ledger access; authorized controls write | `tests/integration/test_api_wiring_and_rbac.py` |
| D-09 | Manifest wiring | `TestClient` startup propagates TTL, recall top-k, roles, and scope config into live services | `tests/integration/test_api_wiring_and_rbac.py` |
| D-10 | Payload quality gate | Length, replacement character, control character, readability, and repeated-character rejection codes | `src/nodes/domain_workflow_nodes.py` unit/integration paths |
| D-11 | Stage 1 evidence config and freshness | Customer requirement table changes gaps; 30/3650/null age policy, missing date, and adapter-fixed explicit/omitted `as_of_date` are deterministic | `tests/unit/test_stage1_evidence_config.py` |
| D-12 | LLM response contract | Prompt contract is generated from `FindingField` declarations; a response mechanically built from the prompt passes; undeclared/nested wrong types fail without value echo | `tests/unit/test_prompt_response_contract.py` |
| D-13 | Operation-aware auth | Standalone token can read but cannot ingest/feedback; authorization occurs before payload/ledger mutation | `tests/integration/test_batch3_security_composition.py` |
| D-14 | Direct-ID scope ownership | A record ID from institution A cannot be fed back from institution B and does not reveal or mutate the target | `tests/integration/test_batch3_security_composition.py` |
| D-15 | Retrieval production wiring | HTTP invoke supplies business text to query-aware retrieval; different records produce different ranked recall | `tests/integration/test_batch3_security_composition.py` |
| R-1/R-2 | Real DB scope projection | SQLite の保存 scope を `WHERE institution_id=?` に投影し、部品単体に加えて本番 HTTP→graph→retrieval composition でも同一区画 1 hit・他区画非混入を確認する | `tests/integration/test_sqlite_scope_projection.py` |
| L-5 | LLM exception non-exposure | Provider exception marker is absent from HTTP output/state; stable `llm_call_failed` remains | `tests/integration/test_batch3_security_composition.py`, `tests/parts/llm-draft-stage/test_error_boundary.py` |
| I-7/I-8 | Request-scoped ingest wiring | Real `PayloadStore` + constructor-injected trusted scope/session; state cannot replace trusted context | `tests/integration/test_batch3_security_composition.py`, `tests/parts/ingest-row-pipeline/test_payload_resolver_integration.py` |

## Execution result

Run with:

```sh
source .venv/bin/activate
pytest tests/ -q --tb=line
pytest tests/proof_of_boundary/ -q --tb=line
```

MR-B final execution (2026-07-30): `pytest tests/ -q --tb=line` returned
**311 passed, 2 skipped**; `pytest tests/proof_of_boundary/ -q --tb=line`
returned **8 passed, 2 skipped**. The two skips are the required non-HITL
PB-7 boundary cases.

Batch 3 execution (2026-08-13): `pytest -q --ignore=scripts/test_stg_invoke_evidence.py`
returned **564 passed, 2 skipped**.
The repository-root run returned **565 passed, 2 skipped, 11 setup errors**;
all 11 errors are the unchanged STG-evidence tests failing to bind
their loopback HTTP server in the restricted workspace. `ruff check src tests`
and `verify_part.py` both passed, with zero SUSPECT files.

## 実 LLM 1 回転検証

モックだけで固めた実装は実 LLM を繋いだ瞬間に動かないことがあるため、注入シームへ実 LLM クライアントを刺して 1 回 invoke し、**中身のある出力が出ること**を確認している（`gitproject-dev-flow.md` §14-1-bis）。

| 項目 | 内容 |
|---|---|
| 実施日 | 2026-08-18 |
| 使用 LLM | ローカル CLI（Azure OpenAI `gpt-5.5`）を `BaseLLM` 実装として注入 |
| 注入シーム | `config[llm]` |
| 実 LLM 呼び出し | 1 回 |
| 出力 | ['case_ref', 'facts', 'evidence_sources', 'unknowns', 'countervailing_factors', 'memo_draft', 'evidence_map', 'review_reasons', 'disclaimer', 'generation_metadata', 'mode', 'gate_kind'] が非空。縮退理由なし |

本番の LLM は `AnthropicClient` 等で、これは検証用シーム。テンプレの `src/` は変更していない（一時テストを生成→実行→削除する方式）。CI には入れていない（ローカル CLI と鍵が要るため）。

## llm-draft-stage 0.3.0 の回帰テスト（LLM 出力の複数 JSON）

AGENTIC STAR の登録モデルが draft JSON を改行区切りで 2 回連結して返す、または JSON の後ろに `{` `}` を含む散文を付ける応答で、
Stage の parse が `Extra data` になり fail-closed していた。部品 `llm-draft-stage` を 0.3.0 に更新し、複数候補を走査して
一意な非空 object だけを受理する（相異なる複数は `AmbiguousJsonOutput` で fail-closed・`{}` / NaN / 配列は不受理）。
対応テスト: `tests/parts/llm-draft-stage/test_draft_node.py`（実物 fixture `fixtures/gpt54mini_duplicated_decision.txt` を含む）。

## Marketplace 経路の JSON モード（0.1.2）

0.3.1 適用後も登録モデルが `"memo_draft":"…"],` の形（配列列の直後の長文字列の後に余分な `]`）を返す回が残り、
ローカル実測 69/70（fail-closed 1）・実機 1/2 error だった。Marketplace 専用 seam `cli.py::_AzureMarketplaceLLM` に
`response_format={"type":"json_object"}` を追加し、`instruction` に "JSON" を含む 1 文を固定した。

| ID | 観点 | 内容 | テスト |
|---|---|---|---|
| M-1 | JSON モードは Marketplace seam のみ | `_new_client()` の config に `response_format` が入り、`src/` には存在しない | `tests/unit/test_marketplace_adapter.py::test_azure_client_is_recreated_with_fresh_secrets_for_every_call` / `::test_json_mode_is_confined_to_the_marketplace_seam` |
| M-2 | プロンプトに "json" | `_build_prompt` の `instruction` に json を含む（Azure 400 の再発防止） | `::test_prompt_mentions_json_so_azure_json_mode_is_accepted` |
| M-3 | 観測は部品と同じ受理規則・本文を残さない・例外を出さない | 部品 `parse_json_object` が拒否する応答だけ WARNING（文字数・トークン数・拒否理由）。部品が受理する形（BOM・前置き散文・同一 object の重複）は無音。観測側の例外は握って WARNING 1 行 | `::test_marketplace_output_observer_warns_without_echoing_the_model_text` / `::test_marketplace_output_observer_is_silent_when_the_part_accepts` / `::test_marketplace_output_observer_never_raises` |
| M-4 | `generation_metadata.template_version` と release 版の一致 | `post_process_node.TEMPLATE_VERSION` が pyproject / agent.yaml の版と一致（0.1.0 のまま取り残されていた定数を修正） | `::test_generation_metadata_version_matches_the_release_version` |

登録値モデルでの実測（2026-09-17・ローカル・runner 相当ライフサイクル）: JSON モードなし 69/70 success → JSON モードあり **40/40 success**、
key set 1 種・`degradation_reason=no_prior_records` 40/40・生応答長 838〜1274 文字（変更前 ≈1170 と同程度）。
N=40 で 0 失敗は p=1.4% でも 57% の確率で起きるため、構文保証は JSON モードの仕組みに置き、N=40 は内容退行の検査と位置づける。
