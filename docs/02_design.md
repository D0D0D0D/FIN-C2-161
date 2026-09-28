# Template Design Specification — FIN-C2-161 AMLDispositionMemoAgent

> 対象: 銀行・証券の AML アナリストが、アラート発生後の調査フェーズで作成する
> **内部の調査所見ドラフト（disposition memo）**を支援する蓄積型エージェント。
> 本書は FIN-C2-161 の実装済み設計を記録する。

## Position in AgentCore Architecture

- **Agent Class**: `AMLDispositionMemoAgent`（`src/graph/graph.py`）
- **L1 Base**: `AgentBaseGraph`（L1 直継承。`AutonomousBaseGraph` は使用しない）
- **Base pattern**: `DocGenerationAgent`（構造化された文書成果物の生成）
- **Three-Layer Separation**:
  - State: flat TypedDict（`AgentState` 継承。Pydantic 不可 — msgpack 非互換）
  - Node: L1 継承（Template Method: `execute(self, state) -> dict` のみ override）
  - Graph: composition（`register_nodes()` で 3 スロットを埋める。`add_edges()` は override しない）

固定 5 ノード backbone（`initialize` / `finalize` は framework が自動注入）のうち、テンプレートは
**pre_process / main / post_process の 3 スロット**を実装する。

## Architecture Overview

### Node Configuration

| Node | 責務 | 入力 State | 出力 State | 継承 / 備考 |
|------|------|-----------|-----------|------------|
| initialize | — | — | — | `InitializeNode`（framework 既定） |
| pre_process | モード判定・入力 schema 検証・**スコープ解決 fail-closed**・payload_ref の出所検証・案件参照 ID 化 | `user_input`, `input_context` | `mode`, `scope`, `case_ref`, `payload_ref`, `validated_input`（構造化メタのみ） | `FunctionNode` / `VERIFIED_EXTERNAL` |
| main | 内側 domain workflow subgraph（3 モード分岐）の実行 | 上記 | モードごとのフィールド（下表） | `GraphNode` / `VERIFIED_EXTERNAL`（既定 `ANONYMOUS` を明示上書き） |
| post_process | S-3 closed allowlist 検査・claim→source 照合・PII 再走査・`formatted_output` への畳み込み | main の出力 | `formatted_output`, `status` | `FunctionNode` / `VERIFIED_EXTERNAL` |
| finalize | — | — | — | `FinalizeNode`（framework 既定） |

### Data Flow

```
START → initialize → pre_process → main → {route} → post_process → finalize → END
                                            ↓ (retry, max 3)
                                          pre_process
```

`route()` は main の後に `state["status"]` を読む framework 既定の動作をそのまま使う（override しない）。

### AGENTIC STAR Marketplace entry adapter

リポジトリ直下の `cli.py` は `AMLDispositionMemoAgent` を継承した `MarketplaceGraph` を公開する。
Marketplace runner の `conversation_history` はこの AG の S-2 対象外なので、受領した
`input_context` はマージせず破棄し、起動環境から解決した `institution_id` / `unit_id` と、adapter
境界で一度確定した `as_of_date` だけで組み直す。scope 宣言は `src/entry_adapter` の
`SCOPE_KEYS` / `STG_DEFAULT_SCOPE_ENVS` を source of truth とし、宣言の不整合、未設定、空文字、
空白を含む値は Graph 構築時（compile より前）に fail closed する。`provision_secrets()` でも同じ
scope と Azure OpenAI の必須値を再検証する。

Marketplace 入力は文字列 1 本である。JSON object なら `/invoke` 相当の `case_ref` / `alert_type` /
`evidence_refs` / `free_text` / `as_of_date` を読み、通常文字列なら `transaction_monitoring` の
`free_text` として扱う。いずれも adapter が `mode="invoke"` の checkpoint-safe envelope と
payload reference を新規発行する。`ingest` / `feedback` を含む非 invoke mode は Graph に渡さない。
HITL は無効化し、万一 `awaiting_human` が返った場合も成功として返さない。

LLM 注入は `_AzureMarketplaceLLM` のみに限定する。Azure の API key / endpoint / deployment は
各 `complete()` / `stream()` 呼び出し時に secret context から引き直し、adapter や LLM instance に
接続値や具象 client を保持しない。任意 extra である `AzureOpenAIClient` だけをメソッド内 import とし、
framework・security・Graph 関連 import は module load 時に完了させる。

**Marketplace 経路だけ Azure の JSON モード**（`response_format={"type":"json_object"}`）を client に渡す。
登録モデル（gpt-5.4-mini）は `memo_draft` の長文字列の直後に余分な `]` を閉じる構造的に壊れた JSON を
1〜4% の頻度で返し、共通部品 `llm-draft-stage` はこれを設計どおり fail-closed にする。JSON モードは
生成前に構文を制約するものであって、出力の修復ではない。Azure はプロンプトに "json" が無いと 400 を返すため、
`_build_prompt` の `instruction` に「Respond with exactly one JSON object…」を固定し、静的テストで縛る。
HTTP/STG 経路（`src/api/server.py`）は LLM を注入しないので挙動は変わらない（`src/` に `response_format` は存在しない）。
`_AzureMarketplaceLLM.complete()` は共通部品の `parse_json_object` と同じ受理規則で応答を検査し、部品が拒否する応答だけ
文字数・completion_tokens・拒否理由（例外種別）を WARNING で残す（本文は残さない・観測が結果を変えることはない）。runner は失敗イベントから `error_log` を落とすため、Pod ログの
この行が parse 失敗を観測できる唯一の窓になる。

### 内側 domain workflow subgraph（main スロット）

`main` は `DomainWorkflowGraphNode`（`GraphNode`）で、内側に `AMLDispositionWorkflowGraph` を持つ。
内側グラフは `mode` で 3 経路に分岐する。

```
                       ┌── mode=ingest ──→ [DispositionIngestNode] ──────────────────┐
[mode_router] ─────────┼── mode=invoke ──→ [Stage1]→[Stage2]→[Stage3]→[Stage4] ──────┼→ END
                       └── mode=feedback ─→ [DispositionFeedbackNode] ───────────────┘
                                  （いずれかで status="error" → END へパススルー）
```

| Stage | ノード | 種別 | 責務 |
|---|---|---|---|
| Stage 1 | `EvidenceCompletenessNode` | **決定論（LLM 不使用）** | `required_evidence_by_alert_type` の必須資料・種別・必須項目を config 定義に照らして検証。`evidence_max_age_days` が未設定/null なら期限検査は無効、設定時は adapter で一度確定した `as_of_date` と比較し、古い資料を `evidence_stale`、日付欠落/不正を `evidence_date_unverifiable` として gap に追加する。資料が存在する事実は別なので stale 資料も `evidence_present[]` から外さない。**十分／不十分の意味的結論は出さない**。出力 = `evidence_present[]` / `evidence_gap[]` |
| Stage 2 | `SimilarPerspectiveRecallNode` | 検索（LLM 不使用） | alert type・証跡種別・検査済み案件テキストから request-scoped な業務 query を作り、同一 `institution_id` 内の過去 disposition を検索する。backend は query を順位付けに実使用し、保存レコード由来 scope を hit に投影する。**部品の public projection にさらにテンプレ側の射影をかけて**調査観点だけを取り出す（後述「Stage 2 の追加射影」）。出力 = `recalled_perspectives[]`（source_id・確認した質問・証跡カテゴリ・非結論的な反証事情・品質/provenance・unit_id） |
| Stage 3 | `DispositionMemoDraftNode` | **LLM** | Stage 1/2 の出力と当該案件の証跡を入力に、5 区分の memo ドラフトを schema 出力。LLM を使う唯一のノード |
| Stage 4 | `EvidenceLinkVerifyNode` | 決定論 | 全 claim に根拠リンクを要求し、**ID 実在照合で終わらせず**「claim に書かれた数値・日付・取引 ID が入力と一致するか」まで機械照合する（後述「provenance の照合契約」）。claim と引用元の不一致は fail-closed、本文から claim が欠落した場合は `requires_human_review: true` |

| モード | ノード | 責務 |
|---|---|---|
| ingest | `DispositionIngestNode` | 過去 disposition 記録を 品質ゲート → サニタイズ → 正規化 → 台帳書込。**監査用原記録**と **retrieval-safe projection** を分離保存。出力 = `ingest_summary`（`written_ids[]` / `rejected[]`） |
| feedback | `DispositionFeedbackNode` | 人間が確定・編集した memo を、明示承認フラグと監査イベント付きで台帳へ反映。**自動学習はしない**（`approved_by` 無しの反映は fail-closed で拒否） |

### State Definition

`src/schemas/state.py` — `AgentState` を継承した flat TypedDict（`AMLDispositionState`）。

| Field | Type | Purpose | 必須 |
|-------|------|---------|------|
| `mode` | `str` | `"ingest"` / `"invoke"` / `"feedback"` | ✅ |
| `scope` | `dict[str, str]` | 認証済みスコープ。パーティションキーは `institution_id` の 1 本（`unit_id` は絞り込み軸として同梱するがパーティションには使わない）。**クライアント自己申告値は入らない** | ✅ |
| `case_ref` | `str` | 案件参照 ID（英字とハイフンのみ）。案件本文は含まない | invoke / feedback |
| `payload_ref` | `str` | 生本文（取引明細・KYC・照会回答・自由記述）の out-of-state 参照トークン | ✅ |
| `as_of_date` | `str` | adapter 境界で一度確定した Stage 1 期限判定の基準日（ISO 日付）。ノード内で現在日時を参照しない | invoke |
| `evidence_check` | `dict` | Stage 1 の結果（`evidence_present[]` / `evidence_gap[]`） | invoke |
| `recalled_perspectives` | `list[dict]` | Stage 2 の想起結果（retrieval-safe projection のみ） | invoke |
| `memo_sections` | `dict` | 5 区分（`facts` / `evidence_sources` / `unknowns` / `countervailing_factors` / `memo_draft`） | invoke |
| `evidence_map` | `list[dict]` | `{claim_id, claim, source_ids[]}` | invoke |
| `requires_human_review` | `bool` | 根拠不整合・縮退時に true | invoke |
| `review_reasons` | `list[str]` | 人間レビューが必要な理由（機械が生成する固定コード） | invoke |
| `ingest_summary` | `dict` | `{written_ids[], rejected[]}` | ingest |
| `feedback_summary` | `dict` | `{updated_ids[], rejected[]}` | feedback |
| `degradation_reason` | `str` | 縮退理由（LLM 不在・KB 空・検索失敗）。`"+"` 結合 | 任意 |

**State Constraints（必須）:**
- flat TypedDict のみ（primitives + JSON-serializable 型）
- JWT / API キー / credential を State に置かない（checkpoint DB 漏洩）
- `InvocationContext` を State に置かない（`InvocationContext.from_state(state)` で取得）
- **生の取引明細・KYC・調査自由記述・memo 本文を State に置かない** — すべて `payload_ref` 経由。
  特に `validated_input` は framework の S-2 が `[MASKED]` に**破壊的置換**するフィールドなので
  （SDK スパイク #4 実測）、構造化メタと参照 ID 以外を入れない。

### 出力契約（closed allowlist）

`formatted_output` は**モードごとに宣言されたキー集合と完全一致**する。未宣言キーは post_process で reject。

```
invoke:   {mode, gate_kind, case_ref, facts, evidence_sources, unknowns,
           countervailing_factors, memo_draft, evidence_map,
           requires_human_review, review_reasons, degradation_reason,
           disclaimer, generation_metadata}
ingest:   {mode, gate_kind, ingest_summary, degradation_reason}
feedback: {mode, gate_kind, feedback_summary, degradation_reason}
```

**`disclaimer` / `generation_metadata`（責任分界の表示・ユーザー判断 2026-07-30）**

この memo は調査記録として保存され、金融庁検査や内部監査で参照されうる。「AG が下書きした」
という事実自体が監査上の情報になるため、invoke の出力に次を必ず載せる:

- `disclaimer` — **コード側の固定文字列定数**。「本ドラフトは AI が生成した下書きであり、
  最終判断は担当者が行う」旨。**LLM に書かせない**（事実主張を LLM に書かせない原則の適用）
- `generation_metadata` — `{generated_at, template_id, template_version, source_ids[]}`。
  使用した source ID の一覧を含めることで、数年後の監査で「何を根拠にした下書きか」を辿れる

`disclaimer` は本文（`memo_draft`）とは**別フィールド**に置く。人間が完成版を作るときに
本文へ転記するかどうかは運用側が決められる（本文に直接埋め込むと完成版から外しにくい）。

**スキーマに持たないフィールド（構造的に生成不能）:**
`final_disposition` / `str_required` / `suspicious` / `filing_payload` / `submit_to_filing_system` /
`account_restriction`、およびこれらに相当する任意のキー。allowlist 方式のため、別名にしても
未宣言キーとして reject される。

## セキュリティ設計（S-1 〜 S-5）

### S-1 Trust Gate

全ノードで `required_trust_level` を**明示宣言**する。有効値は `ANONYMOUS` / `VERIFIED_EXTERNAL` /
`INTERNAL` の 3 つのみ。

| ノード | trust level | 理由 |
|---|---|---|
| pre_process / main / post_process / 内側全ノード | `VERIFIED_EXTERNAL` | 顧客テナントのデータを読み書きするため、認証済み呼び出し元のみ |

`GraphNode` の既定は `ANONYMOUS`（SDK スパイク #8 実測）なので、明示上書きしないと内側ノードの
S-1 と不整合になる（SubcontractorQuotationDisputeHistory で実バグ検出済み）。

### S-2 Input Gate

- framework 既定: `user_input` / `validated_input` / `llm_response` に対する `detect_pii` 自動マスク
- ドメイン拡張は `_extra_security_gate_input(state)` に実装する（`_security_gate_input()` は `@final`）:
  1. **スコープ fail-closed** — `scope` に宣言済みキーが過不足なく揃っていなければ `SecurityViolationError`。
     省略可能なスコープ引数を作らない
  2. **mode allowlist** — `ingest` / `invoke` / `feedback` 以外は reject
  3. **payload_ref の出所検証** — クライアント由来の `payload_ref` は**無条件破棄**し、
     入口 adapter がサーバ発行した内部トークンのみを受け付ける（SafetyPatrolRecordAccumulationWeatherMatchingAdvisoryAgent 裏口 #2 と同型の穴を封鎖）
  4. **入力 schema 検証** — 未宣言キーを含む入力は reject

**reject の方法**: `SecurityViolationError` を raise する（`framework.errors`）。実機で確認済み
（2026-07-30・wheel 1.0.0）: S-2 で raise すると `status="error"` となり、**後続の `main.execute()` は
実行されず**、`invoke()` の `output` は `None` になる。`node_history` には通過記録だけが残る。
framework の必須テストケース（「SecurityViolationError fires」）とも一致する。

> `GraphNode` の S-2/S-3 は ADR-017 の意図的 no-op（上流と内側ノードのゲートが適用済み）。
> ドメインゲートの実体は pre_process / post_process の `FunctionNode` 側に置く。

### S-3 Output Gate — **closed allowlist 一本（denylist は実装しない）**

検査は**二段構え**にする。`_extra_security_gate_output(result)` は `state` を受け取らない
（SDK スパイク #7 実測）ため、**State を参照する照合をこのフックに書くことはできない**。
ノードの instance 変数に registry を持たせる回避は State safety 違反（node instance は全実行で共有される）なので採らない。

| 段 | 実施場所 | 検査内容 |
|---|---|---|
| **① State 参照が要る照合** | Stage 4（内側）+ `PostProcessNode.execute(state)` | 後述「provenance の照合契約」の全項目。捏造 ID・未登録 ID・数値不一致は fail-closed または `requires_human_review` に降格 |
| **② envelope 内で閉じる検査** | `_extra_security_gate_output(result)` | 下記 1〜3。`mode` は `result` 側に宣言して分岐する |

#### provenance の照合契約 — **ID 実在確認で終わらせない**

`source_id` が実在することは、**claim の中身が入力に忠実であることを何も担保しない**
（LLM が入力の金額や取引 ID を書き換えても、ID 照合だけでは発火しない）。このため Stage 4 は、
source ID の実在性だけでなく claim→source の対応、数値・日付・取引 ID の照合を行う。
具体的には以下すべてを実装する:

| # | 照合 | 内容 |
|---|---|---|
| ① | **ID 実在** | `evidence_map[].source_ids` が Stage 1 の `evidence_present[].source_id` / Stage 2 の `recalled_perspectives[].source_id` に実在する |
| ② | **数値・属性照合** | `_markers()` が claim から抽出する **金額を含む数値（`amount`）/ 日付（`date`）/ 取引 ID（`transaction`）**が、参照先 source のメタデータと**値として一致**する。不一致は fail-closed（LLM の書き換えをここで捕まえる）。Stage 3 の応答契約には、この検証実体から marker 種別と source ごとの利用可能値を生成し、引用元にない固有値を claim・facts・memo 本文へ書かないよう明示する |
| ③ | **双方向** | claims→入力（健全性）だけでなく **本文→claims（網羅性）** も見る。`memo_draft` / `facts[]` に数値・日付・取引 ID が現れるのに対応する claim が `evidence_map` に無ければ `requires_human_review`。②だけだと「無関係な claim を 1 件入れて数値は散文にだけ書く」で全回避される |
| ④ | **抽出の正規化** | 抽出器は **NFKC + 漢数字変換 + 日本語単位・助詞の同義語**を前段に通す。`一千万円` / `１,０００万円` / `10,000,000 円` / `1000万` を同一視できないと素通りする。数値の直後に単位・助詞が隣接するケース（`999999円` `1000万円を`）で抽出が空集合になり「照合対象ゼロ＝常に PASS」になる罠を避ける |
| ⑤ | **`kind` は allowlist** | `evidence_sources[].kind` は `transaction_record` / `kyc_document` / `inquiry_response` / `attachment_reference` / `prior_perspective` の有限集合。未知 `kind` を「空ではない」と主張させない |
| ⑥ | **PB ケース** | 「**実在するが当該 claim とは無関係な `source_id` を引用**」ケースを proof-of-boundary に必ず入れる。①だけを実装すると通ってしまう経路であり、②③が効いていることの証明になる |

**受入テストは両方向で書く**: ①正当な memo が通ること ②各回避パターン（数値書換・漢数字・単位隣接・
無関係 source 引用・散文だけに数値）が落ちること。片方向だけだと over-rejection を見逃す。

`_extra_security_gate_output(result)` が行うこと:

1. **出力キーの closed allowlist** — 上記「出力契約」のキー集合と完全一致。top-level だけでなく
   **ネストした dict / list 内も再帰的に**検査する（framework 既定の credential scan は
   top-level string しか見ない）。未宣言キーは reject
2. **envelope 内の参照整合** — `evidence_map[].source_ids` ⊆ `evidence_sources[].source_id`。
   出力の中だけで閉じるため State 不要。①と合わせて「実在する source から来た ID だけが
   claim に紐づく」ことを二重に固定する
3. **PII / credential の再走査と処置** — **LLM が書く自由文フィールドを全列挙**して同一ガードを
   再帰適用する。主フィールド 1 本だけに掛けると脇の自由文が必ず漏れる（同種テンプレートで実証済みの裏口）:

   ```
   _FREE_TEXT_PATHS = (("memo_draft",), ("facts","*"), ("unknowns","*"),
                       ("countervailing_factors","*"), ("evidence_sources","*","label"),
                       ("evidence_map","*","claim"), ("review_reasons","*"))
   ```
   `*` は list 展開。上記に加えネストした全 string を走査する。
   **処置は次のとおり明示する**: 検出箇所は `[MASKED]` に置換して出力を継続し、構造上安全に
   置換できない場合（キー名や ID に混入している等）は fail-closed で reject する。
   **検出値そのものは監査ログ・エラーメッセージに出さない**（`type` と件数のみ記録）

> **禁止語 denylist は書かない。** 自然言語の断定表現は言い換えで原理的に閉じず、
> 「守っているように見えて守らない」false assurance と保守負担だけが残る
> （Logistics Dangerous-Goods Document Consistency Agent / Financial Regulatory Consultation Response Evidence Brief Agent で確定した方針）。非結論性は**構造**で担保する:
> ①最終判定フィールドがスキーマに存在しない ②未宣言キー全 reject ③claim→source 必須
> ④人間が最終判断者であることを出力に明示。
>
> **この allowlist が保証しないこと（正直に書く）**: allowlist が制限するのは**キー名**であり、
> 許可された自由文フィールド（`memo_draft` 等）の**文面**に断定的な表現が現れることは
> 構造では止まらない。ここを止めにいくと禁止語 denylist の whack-a-mole に戻る（3 つの姉妹テンプレートが
> 同じ罠を踏み、いずれも撤去した）。**自由文の最終担保は人間のレビュー**であり、その前提を
> 出力（`requires_human_review` と「本文書はドラフトであり最終判断ではない」旨の固定文言）と
> 運用ガイドの双方に明記する。

### S-4 Audit Logging

各ノードの `execute()` 内で `emit_trace_event()`（`shared.utils.audit_logger`）を**最低 1 回**呼ぶ。
`node_start` / `node_complete` / `node_error` は framework が `__call__()` で自動発火するため
**重複させない**。ドメインイベント: `evidence_check_completed` / `perspectives_recalled` /
`memo_drafted` / `evidence_verified` / `disposition_ingested` / `feedback_applied`。

**成功経路だけに置かない。** 以下の非成功経路にもドメインイベントを出す（監査上、AG が
「何をしなかったか」の方が重要になる）:

| 経路 | イベント |
|---|---|
| モード別 RBAC の拒否（書込モードに権限なし） | `authorization_denied`（mode / 要求ロール名 / caller_id のみ・**payload の値は載せない**） |
| S-2 reject（スコープ不足・未知 mode・schema 不備） | `input_rejected`（理由コードのみ・値は載せない） |
| 上流 `status="error"` の短絡パススルー | `stage_skipped_upstream_error` |
| `requires_human_review` への降格 | `demoted_to_human_review`（`review_reasons` の固定コード） |
| 縮退（LLM 不在・KB 空・検索失敗） | `degraded`（`degradation_reason`） |
| S-3 reject | `output_rejected`（違反種別のみ） |
| provenance 不一致（数値書換・無関係 source） | `provenance_mismatch`（種別と件数のみ） |

> **PB-1 の注意**: framework が自動発火する `node_start` / `node_complete` を見る PB テストは、
> ドメインイベントを全削除しても緑になる（実証済みの空証明）。PB-1 は
> **各 node module の `emit_trace_event` binding を patch してドメインイベントの発火を実測**する。

`feedback` モードの書込は `approved_by` / `decided_at` / `decision_source` を含む監査イベントを必ず伴う。

### S-5 / secrets

- **本テンプレート自身は secret を解決しない。** LLM クライアントと台帳 / KB backend は
  `config` 注入シームで外から渡され、認証情報は注入側が保持する。したがって
  `requires.secrets: []` と「`src/` に `ctx.secrets.require()` を書かない」は**整合している**
- 将来、テンプレート内で直接 backend に接続する構成を採る場合は、
  ①その secret 名を `config/agent.yaml` の `requires.secrets` に宣言し
  ②`ctx.secrets.require("<宣言した名前>")` でのみ読む（`os.environ` 禁止・`ctx` は
  `InvocationContext.from_state(state)`）。**起動時に必ず存在すべき秘密のみ**を宣言する
  （リクエスト時にだけ要る鍵を載せると STG で起動不能になる — InsuranceLargeLossReserveChangeEvidenceReviewAgent 前例）
- 唯一の例外は入口 adapter が検証する caller-auth token `INVOKE_AUTH_TOKEN`。この token は
  ワークフローへ渡さず、受信境界での呼出元認証にのみ用いる
- 届出システムへの書込 credential は**保持しない**（そもそも送信機能を持たない）

### モード別の認可契約（台帳汚染の防止）

**`VERIFIED_EXTERNAL` は「呼び出し元が認証されている」ことしか証明しない。**
後続の AML 調査が参照する証拠ベースを**書き換えてよい権限**は別物であり、
**書込モード（`ingest` / `feedback`）はモードごとに独立した認可を要求する**。

| Mode | 種別 | 要求する権限（config 宣言・既定名） | 未認可時 |
|---|---|---|---|
| `/invoke` | read-only（台帳を読むだけ） | 認証済み + スコープ照合のみ（追加ロール不要） | — |
| `/ingest` | **書込**（過去 disposition の一括投入） | `roles.ingest`（既定 `aml_data_steward`）— 過去記録の移行・投入はデータ管理の権限であり、個別案件の判断確定とは別の職掌 | **403 / fail-closed** |
| `/feedback` | **書込**（個別案件の判断確定） | `roles.feedback`（既定 `aml_reviewer` または `aml_supervisor`）— disposition を確定できる職掌 | **403 / fail-closed** |

`ingest` と `feedback` の権限を分けるのは、**過去記録の一括投入と個別案件の判断確定が実務上
別の職掌**であるため（前者はデータ移行・システム管理、後者は調査の一線・二線）。
同一ロールで運用したい顧客は config で同じ値を宣言できる。

**identity → permission の導出（クライアント入力を一切信用しない）**

- 認可判定に使うのは、verified middleware が `request.state` に設定した**認証済み principal の
  ロール / クレームのみ**（`request.state.caller_id` と同じ出所）
- **リクエスト本文・ヘッダ・クエリで自己申告されたロールは採用しない**（scope と同じ規律）
- ロール名は `config/agent.yaml` の `roles.*` で宣言し、顧客 IdP のロール名に合わせられるようにする。
  **config に宣言が無ければ fail-closed**（既定で開かない）
- middleware が不在 = ロール情報が無い場合も **403**（`VERIFIED_EXTERNAL` の trust level だけでは
  書込モードを通さない）。STG standalone token（`INVOKE_AUTH_TOKEN`）が開けるのは
  **read-only の `/invoke` のみ**で、書込モードは常に middleware 必須

**判定位置（pre-payload / pre-repository-access）**

認可判定は **payload store への預け入れ前・台帳アクセス前**に行う。順序を逆にすると、
未認可 caller の本文が transient store に載り、台帳接続も試行される。

```
request → middleware（認証・scope 解決）→ ★モード別 RBAC 判定★ → payload_store.put()
        → graph.invoke() → ledger アクセス
```

**監査イベント（値を含めない）**

未認可の拒否時は `authorization_denied` を発火する。載せるのは **mode / 要求ロール名 /
caller_id** のみで、**リクエスト本文の値・payload・案件内容は一切載せない**。

**MR-B の受入テスト（必須）**

1. **verified だが未認可**の caller の `/ingest` が 403 になり、**台帳レコードが 1 件も作られず、
   retrieval-safe projection も生成されない**ことを、台帳の実体を数えて assert する
   （403 のステータスコードだけを見て満足しない）
2. 同じことを `/feedback` でも確認する
3. **認可済み**の caller では正常に作成されることを確認（over-rejection の検出。片方向だけの
   テストは fail-closed の行き過ぎを見逃す）
4. `/invoke` は追加ロールなしで通ることを確認（read-only モードに認可を課していないことの確認）
5. **既存の cross-institution 不可視性テストはそのまま維持する**（認可の追加で置き換えない。
   認可と分離は別の防御層）

---

### `/feedback` の認可契約（上記に加えて）

`feedback` は台帳に書き込み、その内容は retrieval-safe projection を通じて**後続案件の想起に影響する**。
「`approved_by` フィールドが存在すること」は承認の証明にならない（リクエスト本文に任意の名前を
書けてしまう）。したがって:

**`feedback-intake` v0.1.0 が部品契約として提供する分（テンプレートで再実装しない）:**

- `decided_by` は verified middleware の `request.state.caller_id` で**必ず上書き注入**する
  （client 自己申告の `decided_by` を監査由来に使わない）— 部品 USAGE に明記された契約
- 対象 `record_id` は **scope 付き `ledger.get()` で所有権を確認**。不一致・不存在はどちらも
  同じ `feedback_target_not_owned` で fail-closed（他区画のレコード存在を漏らさない）
- `verdict_code` は `LedgerConfig.verdict_codes` の有限集合で検証。範囲外は
  `feedback_verdict_rejected` + `feedback_rejected` 監査イベント
- `(scope, record_id, feedback_seq)` を receipt store で一意化し、**再送でも二重 write しない**
- 対象レコードを直接更新せず、**新レコードとして append** する（supersede は ledger が正本）
- raw rationale を State に載せず、`feedback_payload_ref` で payload-store から解決する

**テンプレート側で追加する分:**

- `VERIFIED_EXTERNAL` であることは「AML disposition を確定してよい」を意味しない。
  **`roles.feedback` の RBAC 検査**（上表）を入口で行い、権限のない caller の `/feedback` は 403
  （部品はスコープ所有権までは見るが、**業務ロールの認可は持たない**ため、ここはテンプレート側の責務）
- STG の standalone token（`INVOKE_AUTH_TOKEN`）が開けるのは **read-only の `/invoke` のみ**。
  `/ingest` `/feedback` は認証 middleware を必須とする（Financial Regulatory Consultation Response Evidence Brief Agent のレビュー Medium 指摘で確認した実例）

## データ統治（永続層）

### 二層分離

| 層 | 内容 | 可視範囲 |
|---|---|---|
| **監査用原記録**（DB） | 過去 disposition の原記録（最終判断ラベル・結論を含む自由記述を含む） | 監査・人間のみ。**LLM とモデル可視 payload には渡らない** |
| **retrieval-safe projection**（KB / 検索軸） | 承認済み・source-linked な「確認した質問」「証跡カテゴリ」「明示的に非結論的な反証事情」＋ source ID・品質/provenance | Stage 2 の検索対象・LLM 入力 |

**除外するもの（レビュー High 対応・2026-07-16）:**
過去の `final_disposition` / 届出ラベル、結論を含む自由記述 span、supporting / countervailing の派生要約。
post_process は (a) モデル context に過去 disposition ラベル相当が含まれる場合、(b) memo の claim /
recommendation が今回の最終判定または STR 要否を主張・示唆する構造を持つ場合、出力を
`requires_human_review` に降格するか reject する。

### テナント／スコープ分離

- **パーティションキー（fail-closed 必須）= `institution_id`（金融機関）1 本。**
  `unit_id`（部門・拠点）は**パーティションキーにせず**、`accumulation-ledger` の
  `domain_fields` に `scalar` 宣言した**検索の絞り込み軸**として持つ。
  AML は本部 AML 部門が一元管理する形が実務の主流であり、`unit_id` をパーティションキーにすると
  同一銀行内の横断想起ができなくなるため（ユーザー判断・2026-07-30）。
  部門間の閲覧可否は顧客側の権限設計に委ねる
- `InvocationContext` に `tenant_id` は存在しない（SDK スパイク #1 実測）ため、入口 adapter が認証結果から
  スコープを解決し、`BaseGraph.invoke(..., input_context={...})` で State に渡す。
  **クライアントがヘッダやボディで自己申告したスコープは採用しない**
- 全検索・全書込でスコープ引数を必須にする（省略可能なオーバーロードを作らない）。
  省略可能にした瞬間、呼び忘れが全件検索になる

> **2 段の scope（tenant + org）を両方 fail-closed にする既定からの意図的な逸脱と、その代償**
>
> 姉妹提案のレビュー指摘は「tenant だけだと**同一社内の他組織**へ未公開情報が漏れる」であり、
> 既定は 2 段（tenant + org）を両方 fail-closed にすることである。本テンプレートは
> **意図的にこれを 1 段（`institution_id`）にしている**（D-14・ユーザー判断 2026-07-30）。
>
> - **理由**: AML は本部 AML 部門が一元管理する形が実務の主流で、部門をパーティションキーにすると
>   同一銀行内の横断想起という価値の核が成立しない
> - **代償（正直に書く）**: 同一金融機関内では、**部門をまたいで過去の調査観点が見える**。
>   部門間の閲覧統制は**顧客側の権限設計に委ねる**前提であり、テンプレートは保証しない
> - **緩和**: `unit_id` を `domain_fields` の scalar として持ち、検索時に絞り込める。
>   部門内に閉じたい顧客は絞り込みを必須化する運用を選べる（`docs/07_operation_guide.md` に手順を書く）
>
> 顧客が部門間分離を要件とする場合は、`scope_keys=("institution_id","unit_id")` に戻す config 変更で
> 対応できる（コード変更不要）。この選択肢を運用ガイドに明記する。

**永続層に対する PB テスト（State 非格納 PB では永続層を守れない）**

| PB ケース | 内容 |
|---|---|
| cross-institution 不可視性 | 機関 A のスコープで書いたレコードが、機関 B のスコープの検索結果に現れない |
| PII / credential の非索引化 | `retrieval_text` と Stage 2 の射影後 payload に、seed した PII（電話番号・メール）と credential 様文字列が**含まれない**ことを assert。台帳の `body_safe` はマスク済みでも、**索引テキストに素で入る経路**を別途塞ぐ |
| verdict の非漏洩 | seed した過去 record の `verdict_code` 値が、Stage 2 出力と Stage 3 のプロンプト文字列に現れない（D-17） |
| 検査は**デシリアライズして行う** | checkpoint / 索引の検査を `repr()` やバイト検索で行うと msgpack blob 相手では必ず vacuous に緑になる。復号して構造を辿る。**変異検査**（意図的に PII を混ぜたら赤くなるか）で有効性を確認する |

### 🔴 Stage 2 の追加射影 — 部品の public projection は verdict を落とさない

`accumulation-ledger` の `_public_record()` は `body_raw` / `body_audit` / `rationale_raw` /
`rationale_audit` / `status_history` を落とすが、**`verdict_code` / `ai_suggested_verdict` /
`classification` は残す**（実コードで確認・2026-07-30）。部品は `retrieval_text` への verdict 複製を
正規表現で拒否するところまでは強制するが、構造化フィールドとしての verdict はそのまま返る。

本 AG では過去の `verdict_code` が**まさに「過去の最終判断ラベル」**であり、これを LLM に渡すと
レビュー High（2026-07-16）で指摘された「検索経由での結論の間接漏洩」がそのまま起きる。

→ **Stage 2 は `ledger.search()` の戻り値に対してテンプレ側でもう一段の射影をかける。**
LLM / モデル可視 payload に載せてよいのは以下の allowlist に限る:

```
{source_id, checked_questions[], evidence_categories[], countervailing_notes[],
 quality_flags, recorded_at, unit_id}
```

`verdict_code` / `ai_suggested_verdict` / `classification` / `decision_source` / `decided_by` /
`body_safe` / `rationale_safe` は**この射影で落とす**（監査経路 `export_audit()` からのみ参照可能）。
受入テスト: Stage 2 の出力（および Stage 3 に渡るプロンプト文字列）に、seed した過去 record の
`verdict_code` 値がマーカー文字列として**含まれない**ことを assert する。

### レコードのライフサイクル（`accumulation-ledger` の実契約に合わせる）

- 記録の訂正は**可逆な supersede**（物理削除しない）。`supersede_mode="auto"`
- **人間の明示訂正**は `write(supersedes_record_id=<旧 record_id>)` — 旧を
  `supersede_reason="explicit_correction"` で supersede し、新は `active`（非対称）
- **明示訂正でない human 同士の verdict 不一致**は、どちらも選ばず**双方 `disputed`**（対称）
- **基準・様式の版不一致**は**旧 record だけ** `disputed` にし、新は `active` のまま（非対称）
- `search()` の既定は `active` + `disputed` を返し、`status` を保持する。
  **`disputed` を検索既定から外さない**（外すと矛盾が見えなくなる）
- `redact()` / `purge_expired_raw()` が消すのは `*_raw` のみ。classification / verdict /
  provenance / status は消さない
- 保持期間・削除・訂正の最終権限は顧客の統治契約に従う（テンプレートは config で保持期間を持つ）

## checkpoint スクラバ — **allowlist で書く**

State は LangGraph の checkpoint として DB に永続化される。機微キーを列挙して退避する
**denylist は必ず漏れる**（呼び出し側が送りうるキーを列挙し切ることはできない。
`{"payload": {"metadata": {"free_text": "<PII>"}}}` のように列挙外の階層に入れられる）。

**正**: checkpoint に載せてよいキーだけを `_CHECKPOINT_SAFE_KEYS` として列挙し、
**それ以外の文字列値は中身を問わず transient store（payload-store）へ退避**する。再帰で全ノードを走査。
非 JSON も raw をそのまま返さず `__unparsable__` envelope で退避する。

| 論点 | 本テンプレートの方針 |
|---|---|
| 載せてよいキーの基準 | 「Stage 1 / route が構造化フィールドとして読む」**かつ**「人が文章を書き込めない型」— `mode` / `case_ref` / `payload_ref` / `institution_id` / `unit_id` / 日付 / 列挙値 / 件数。**迷ったら足さない** |
| 新しいキーが増えたら | 自動的に「退避される側」になる = **fail-safe**（denylist は逆に fail-open） |
| 列挙値キー（`mode` 等） | **値ベース allowlist**。`mode: "invoke"` は通し、想定外の文字列は退避 |
| リスト・サブツリー | `evidence_present[]` の要素のように Stage が読む構造化リストは明示 allowlist（allowlist 化で必要な要素を消して Stage が壊れる回帰が実際に出ている） |

## MR-B のテスト要件（CI が構造的に検証しない領域）

以下は `pytest tests/` と 14 ゲートが**構造的に検証しない**ため、MR-B で明示的にテストを置く。

| # | 要件 | 理由 |
|---|---|---|
| T-1 | **`startup()` を実行する統合テストを 1 本以上**（`with TestClient(app):`）。あわせて **`config/agent.yaml` の manifest 値が実際に service / store / ledger に届いているか**を assert する | `run-tests` は `pytest tests/` を回すだけで **`src/api/server.py` の startup hook を一度も起動しない**。TrustLevel の小文字リテラル・manifest 未ロード・secret 必須化といった**本番起動 blocker が全ゲート緑をすり抜ける**（3 件同時検出の実例あり） |
| T-2 | **config が効くことの動的テスト（X-1）**。`recall_top_k` / `payload_ttl_seconds` / `roles.*` / `scope_keys` に加え、`required_evidence_by_alert_type` を実際に変えて gap が変わること、`evidence_max_age_days` を 30/3650/null に変えて stale/unverifiable 判定が変わることを assert する。adapter の明示/省略 `as_of_date` と固定過去日で決定論的な期限判定も確認する | 「config に宣言があるのにコードが module 定数を読む」＝**config が飾り**になる逆パターンが 6 件検出されている。static grep「config を参照している」では不十分で、**値を変えて挙動が変わる**ことを見る |
| T-3 | **fail-closed テストには provisioned 対照実験を添える**。`pytest.raises` は具体型で受け（広い捕捉を書かない）、同じ入力・同じ caller で**権限/スコープを与えた対照実験が成功する**ことを併記する | `pytest.raises(SomeError)` は **state の必須キー不足による `KeyError` でも緑になる**（境界を何も検証していない状態で緑にした実例あり）。仕上げは**変異検査**（production 側のガードを外すとそのテストが赤くなるか） |
| T-4 | **PB-1 は `(発火モジュール, イベント名)` のペアで検証**する。`EXPECTED_EVENT_SOURCES` に載せたモジュールが `emit_trace_event` を束縛していない場合も assert で落とす | framework が自動発火する `node_start` / `node_complete` を見る PB は、**ドメインイベントを全削除しても緑**になる。イベント名の集合だけを見ると、別ノードが同名イベントを出す「肩代わり」が成立する |
| T-5 | **vendored part の import 分離 PB を 2 本立てで**置く。①`src/` 全モジュールを AST スキャンして vendored パッケージの**トップレベル絶対 import** を検出 ②`repo/src` を `sys.path` から外したサブプロセスで `import src.graph.graph` を実行 | 部品の絶対 self-import は `pip install -e .` の shim に隠れて **CI もローカル pytest も全緑ですり抜ける**。本質的リスクは import 失敗より**二重アイデンティティ**（同一部品が 2 モジュールとしてロードされ `except` が他方を捕まえない潜在 fail-open） |
| T-6 | **数値抽出器は「日本語文中での実際の書かれ方」でテスト**する。単位隣接・助詞隣接・文末ピリオド・全角・桁区切りの **5 形を必ず入れる** | 正規表現で `\w` 境界を要求すると Python の `\w` は日本語にもマッチするため、`999999円` `1000万円を` が**抽出されず空集合**になり `issubset()` が常に True で素通りする（実 invoke で架空金額の残存を確認した実例）。テストを空白区切りの数値で書くと欠陥を隠す |

## 適用部品一覧（parts）

MR-B で `parts/tools/apply_part.py` により写経適用する。部品がカバーする骨格を再発明しない。

> **`accumulation-ledger` と `feedback-intake` は本テンプレートが初適用**（2026-07-29 作成・
> `usage_ledger.json` の適用実績ゼロを確認）。初適用ゆえ、部品の想定と実ドメインのズレが
> MR-B で出る可能性がある。**ズレを見つけたらテンプレ側で塗らず上流 `parts/` を直す**
> （テンプレ側で回避すると欠陥が他テンプレへ複製され続ける）。写経忠実性は
> `parts/tools/verify_part.py` で機械照合する（module 定数の値変更を誤検出する既知の癖があるため、
> 🔴 が出たら完全 diff を取って登録済み adaptation_point かを裁定する）。

| part-id | version | 適用先 | adaptation_points の埋め方 |
|---|---|---|---|
| `cat2-graph-skeleton` | 0.1.4 | `src/nodes/main_node.py` | `subgraph_factory` = `AMLDispositionWorkflowGraph` ／ `_parent_config` = service 群・request-scoped trusted scope/session・共有 feedback receipt store ／ `mode_specs` = 3 モードの `gate_kind` と昇格フィールド ／ `propagate_hitl=False`（HITL 非採用）／ `error_strategy="propagate"` |
| `output-envelope` | 0.1.2 | `src/nodes/post_process_node.py` | モードごとの宣言キー集合（上記「出力契約」）・`degradation_reason` の畳み込み・モードスコープ返却 |
| `evidence-gate` | 0.3.0 | 内側 Stage 3/4 / post_process | `FindingField` から prompt 応答契約を生成し、draft/source/claim を `project_typed_finding()` で型付き射影。続いて claim→source registry の実在・属性照合 |
| `scoped-retrieval` | 0.3.0 | 内側 Stage 2 | `scope_keys=("institution_id",)`・backend が業務 query を実使用・保存レコード由来 scope の全 hit への投影・**テンプレ側追加射影**・`top_k`・空振り縮退 |
| `llm-draft-stage` | 0.3.0 | 内側 Stage 3 | `config["llm"]` 注入シーム。LLM 未設定だけ deterministic fallback。provider/auth/config/runtime/parse 障害は安定 `error_code` で fail-closed、例外文は State/trace/応答へ載せない |
| `ingest-row-pipeline` | 0.4.0 | 内側 ingest | 認証後に request-scoped graph/node を構築し、trusted scope/session と実 `PayloadStore` を明示注入。品質ゲート → サニタイズ → 正規化 → 書込 → `ingest_summary`。既存は行単位書込であり batch 原子性の後退なし |
| `accumulation-ledger` | 0.1.1 | `src/services/ledger_service.py` | `scope_keys=("institution_id",)` ／ `record_kinds={"disposition_record"}` ／ `verdict_codes` = 人間が確定した disposition の有限集合（**AG は書かない**・feedback 経路のみ）／ `classification_codes` = alert_type ／ `domain_fields={"unit_id":"scalar","case_ref":"scalar"}` ／ `conflict_key_fields=("case_ref",)` ／ `supersede_mode="auto"` ／ `record_id_prefix` は ASCII 英字 |
| `feedback-intake` | 0.2.0 | 内側 feedback | `scope_keys=("institution_id",)` ／ `payload_ref_key="feedback_payload_ref"` ／ request graph 間で共有する receipt store の key 単位 lock で並行冪等化 ／ adapter は `caller_id` で `decided_by` を上書き |
| `payload-store` | 0.4.0 | `src/services/payload_store.py` + 入口 adapter | 生本文の TTL 付き out-of-state 隔離。ref は**英字とハイフンのみ**で発行（数字連続は S-2 の PII 検知に `[MASKED]` 破壊される既知事故） |
| `entry-adapter` | 0.4.0 | `src/api/server.py` | `SCOPE_KEYS=("institution_id","unit_id")`（**両方が verified caller identity**。ただし台帳のパーティションキーは `institution_id` のみ＝D-14）・`authenticate(..., operation=...)` で `/invoke`=`READ_INVOKE`、`/ingest`=`WRITE_INGEST`、`/feedback`=`WRITE_FEEDBACK` を副作用から宣言。standalone token/STG default は read-only、write は middleware principal + mode RBAC 必須 |

`cli.py` は Marketplace 固有の composition root であり、上表の部品実装は変更しない。Stage 1 の
`EvidenceCompletenessNode`（必須種別・鮮度の決定論的判定）→ Stage 3 の LLM 草案 → Stage 4 の
決定論的 provenance 検証という順序も変更しない。そのため adapter は証跡の十分／不十分を推論せず、
入力を既存の安全な envelope に変換して同じ Graph へ渡すだけである。

## Framework Utilization

### Shared Components Used

| 用途 | 使用するもの | 備考 |
|---|---|---|
| ノード基底 | `framework.nodes.FunctionNode` / `framework.nodes.graph_node.GraphNode` | S-1/S-2/S-3 を自動で受け取る |
| PII / credential 検出 | `shared.security.pii_detector.detect_pii` / `shared.security.credential_detector.detect_credentials` | **自前の正規表現を書かない**（framework が備える検出器を再実装しない）。`framework.security` は wheel に存在しない（SDK スパイクで実測） |
| 監査 | `shared.utils.audit_logger.emit_trace_event` | — |
| エラー | `framework.errors.SecurityViolationError` | S-2 の fail-closed |
| Context | `framework.schemas.invocation_context.InvocationContext.from_state(state)` | 直接構築しない（直接構築すると secrets provider が繋がらない） |
| LLM | `config["llm"]` 注入シーム（`shared.services.llm.base_llm.BaseLLM` 準拠） | 具象クライアント（Anthropic/OpenAI/Bedrock）は langchain 依存で本環境 import 不可。モデル ID を `src/` に書かない |
| DB / KB | `shared.services.database.base_db` / `vector_store.base_vector_store` の**基底のみ** | 具象（mysql/postgres/pgvector/pinecone）は `NotImplementedError` スタブ（SDK スパイク #10 実測）。テンプレートは in-memory backend を注入シームとして実装し、実接続手順を運用ガイドに明記 |

- S-2: `_extra_security_gate_input()` — 上記「S-2 Input Gate」の 4 項目
- S-3: `_extra_security_gate_output()` — 上記「S-3 Output Gate」の 4 項目
- S-4: `emit_trace_event()` — 各 `execute()` に最低 1 回のドメインイベント
- `_security_gate_input()` / `_security_gate_output()` は `@final`。override しない（クラス定義時に `TypeError`）

### Composition Pattern

- **Pattern**: GraphNode（inner subgraph）
- **Composition target**: `AMLDispositionWorkflowGraph`（同一 repo 内の内側グラフ。他テンプレートは import しない）
- **Error propagation strategy**: `propagate`（内側 error は外側へ伝播。部分結果を返さない）

```python
def register_nodes(self) -> None:
    super().register_nodes()                       # initialize + finalize を注入
    self._nodes["pre_process"]  = PreProcessNode()
    self._nodes["main"]         = DomainWorkflowGraphNode(config=self.config)
    self._nodes["post_process"] = PostProcessNode()
# add_edges() は override しない（framework 既定の 5 ノード backbone を使う）
```

#### `get_output()` の override — **必須（未検証出力の漏洩を塞ぐ）**

framework 既定の `AgentBaseGraph.get_output()` は
`state.get("formatted_output") or state.get("result")` を返す。**post_process の S-3 が拒否すると
`formatted_output` が State に反映されないまま、main が残した `result` が `output` として返却される。**

実機で再現済み（2026-07-30・wheel 1.0.0）: S-3 フックで拒否した invoke が
`status="error"` かつ `output={"final_disposition": "UNVALIDATED_LEAK"}` を返した。
`status` を見ない呼び出し元には、**S-3 を通っていない中間データがそのまま渡る**。

対策（両方を実装する）:

1. `AMLDispositionMemoAgent.get_output(state)` を override し、
   **`status == AgentStatus.SUCCESS.value` かつ検証済み `formatted_output` が存在するときだけ**
   `output` を載せる。それ以外は `output: None` +（あれば）`error_log` のみを返す
2. `DomainWorkflowGraphNode.merge_output()` は **`result` キーを一切書かない**。
   内側の生の戻り値を親 State へ素通ししない（スキーマ境界の責務）。
   親 State へ昇格するのは `mode_specs` に宣言した中間フィールドのみ

受入テスト: S-3 拒否時に `output` が `None` であること（上記の再現ケースを回帰テスト化する）。

#### `GraphNode` の入出力契約 — スコープは自動伝播しない

SDK 1.0.0 の `GraphNode.execute()` が内側グラフへ渡すのは **`extract_input()` の戻り文字列・session・
`InvocationContext`** だけである。**親 State の `input_context` / `scope` / `mode` / `payload_ref` は
自動では渡らない**（`InvocationContext` に `tenant_id` 等が無いことは SDK スパイク #1 で確認済み）。
これを設計で明示しないと、内側の検索・書込が無スコープに落ちるか、内側が独自にスコープを
自己申告する経路を作ってしまう（cross-tenant 参照の入口）。

| メソッド | 契約 |
|---|---|
| `extract_input(state)` | `{"mode", "scope": {"institution_id","unit_id"}, "case_ref", "payload_ref", "as_of_date", "evidence_refs"}` を **JSON 直列化した文字列**を返す。`as_of_date` は adapter が一度確定した ISO 日付である。**生本文は載せない**（payload_ref のみ）。値は英字・数字・ハイフンに正規化する（内側の S-2 が `user_input` に PII マスクを掛けるため、電話番号様の数字列を含めない） |
| `get_subgraph()` | 内側グラフを **lazy 生成してキャッシュ**する（`self._subgraph is None` のときだけ生成）。毎回新規生成しない |
| `merge_output(state, sub_result)` | `mode_specs` に宣言したフィールドだけを親 State へ明示マッピングする。**実行されたモードのフィールドのみ**返す（None 埋めしない）。`result` は書かない。`node_history` は返さない（reducer 管理） |
| `required_trust_level` | `VERIFIED_EXTERNAL`（既定 `ANONYMOUS` を明示上書き） |
| `error_strategy` | `propagate` |
| `propagate_hitl` | `False`（HITL 非採用） |

内側グラフは受け取った JSON から scope を復元し、**全 read / write に両スコープキーを必須引数で渡す**。
スコープ引数を省略できるオーバーロードを作らない。

受入テスト（cross-partition）: 機関 A のスコープで書いたレコードが、機関 B のスコープの `/invoke` の
Stage 2 検索結果に**現れない**こと。`unit_id` は D-14 のとおりパーティションではなく、同一機関内の横断想起を許す。

#### 入力チャネル

- **証跡ファイルの本体は受け取らない。** 入力は顧客システム上の**証跡参照 ID + メタデータ**
  （種別・日付・タイトル）と、担当者の**自由記述テキスト**に限る。パーサを持たない形式には
  攻撃面が無い（PDF action・CSV formula・画像 metadata の対策が不要になる）
- 自由記述テキストには**サイズ上限**（config）を設け、超過は reject
- 🔴 **索引・LLM に入るテキストそのものに品質ゲートをかける。** ファイル取込経路を塞いでも、
  **OCR 崩れテキストを自由記述欄に貼り付けられれば同じ事故**が起きる（経路の禁止だけでは足りない）。
  次を決定論的に検査し、閾値超過は `rejected[]` に理由コード付きで返す（黙って索引しない）:
  - U+FFFD（replacement character）比率
  - 制御文字（C0/C1、`\t\n` 以外）比率
  - 可読文字比率（日本語・英数字・一般記号の占める割合）
  - 同一文字の異常な連続（OCR のノイズ列）
  - 最小長（数文字の断片は調査記録として意味を持たない）
- payload store から取り出した本文を LLM に渡す**直前**に、PII 走査と
  **prompt-injection パターンの検出**を行う（顧客システム由来の照会回答テキストは、
  作成者が AML アナリストとは限らない外部由来コンテンツとして扱う）。
  検出時は当該箇所を無害化し `degradation_reason` に記録する
- 生本文を `BaseGraph.invoke()` の `user_input` に載せない。HTTP リクエストの本文は
  入口 adapter が payload store に預け、`user_input` には参照 ID と構造化メタのみを渡す

## Import Isolation Confirmation

- [x] Level 0（`from agenticstar` / `import agenticstar`）を import しない
- [x] import 先は `framework/` と `shared/` のみ（`agents/base/` は不要）
- [x] 他テンプレート（`agents/domain/` の兄弟）を import しない
- [x] 写経部品の import identity は `src.<part>` に統一する（top-level import は editable install に依存し、
      非 editable 経路で `ModuleNotFoundError` になる — 姉妹テンプレートでも修正済みの既知欠陥）

## config/agent.yaml 主要キー

```yaml
agent:
  id: "FIN-C2-161"
  name: "AMLDispositionMemoAgent"
  category: "Cat 2"
  industry: "FIN"
  base_type: "AgentBaseGraph"      # L1 クラス名。概念パターンは DocGenerationAgent
  module: "src.graph.graph"
  class: "AMLDispositionMemoAgent"
  config:
    max_retry: 3
    timeout_seconds: 60
    memory_enabled: false
    hitl:
      enabled: false          # HITL 非採用（docs/01_proposal.md の HITL Gate と一致）
    scope_keys: ["institution_id"]   # unit_id はパーティションキーではなく絞り込み軸（D-14）
    roles:                           # モード別 RBAC（宣言が無ければ fail-closed）
      ingest: ["aml_data_steward"]
      feedback: ["aml_reviewer", "aml_supervisor"]
    recall_top_k: 5
    required_evidence_by_alert_type:
      cash: ["transaction_record", "kyc_document"]
      sanctions: ["transaction_record", "inquiry_response"]
      transaction_monitoring: ["transaction_record"]
      kyc: ["kyc_document"]
    evidence_max_age_days: null  # AML 実務で決める。未設定/null なら期限検査なし
    payload_ttl_seconds: 900
    require_evidence_link_for_all_claims: true
  required_trust_level: "VERIFIED_EXTERNAL"
  requires:
    extras: []
    secrets: []               # 起動時必須の秘密なし（LLM/DB はリクエスト時の注入シーム経由）
```

> 閾値・runtime パラメータは `config/config.yaml` に置き、`config/agent.yaml` には登録用の
> メタデータだけを置く。

## Design Decision Record

| # | 論点 | Option A | Option B | 決定 | 根拠 |
|---|---|---|---|---|---|
| D-1 | L1 base type | `AgentBaseGraph` | `AutonomousBaseGraph` | **A** | 固定パイプライン。エージェントが次アクションを自己決定する think→act ループではない |
| D-2 | HITL の要否 | 採用（D6 interrupt） | 非採用 | **B** | グラフ内 suspend の必然性 4 問（AG が可否判定をしない／検証失敗は fail-closed で正常終了できる／人間の応答が数時間〜数日／`/feedback` で非同期に人間関与を担保）。詳細は docs/01_proposal.md の HITL Gate |
| D-3 | S-3 の方式 | closed allowlist のみ | allowlist + 禁止語 denylist | **A** | 自然言語の断定表現は言い換えで閉じない。最終判定フィールドのスキーマ排除が主防御（Logistics Dangerous-Goods Document Consistency Agent / Financial Regulatory Consultation Response Evidence Brief Agent 裁定） |
| D-4 | 過去 disposition の扱い | 原記録をそのまま検索 | 監査用原記録 + retrieval-safe projection の二層 | **B** | レビュー High 指摘（2026-07-16）「検索経由での結論の間接漏洩」対応。過去の not-suspicious を免罪符にしない |
| D-5 | 機微本文の置き場所 | State に格納 | payload_ref で out-of-state 化 | **B** | 取引明細・KYC・自由記述が checkpoint DB に平文で残るのは重大事故。`validated_input` は S-2 が `[MASKED]` に破壊書換するため構造的にも不適（SDK スパイク #4） |
| D-6 | スコープの受け渡し | `ctx.tenant_id` | `input_context`（認証済み値のみ） | **B** | `InvocationContext` に `tenant_id` が存在しないことを実機確認（SDK スパイク #1）。クライアント自己申告は cross-tenant 参照の入口 |
| D-7 | 証跡チェックの実装 | LLM に判定させる | Stage 1 の決定論 + config | **B** | 「資料があるか」に LLM は不要。LLM の価値は自由記述の当てはめに限定し、単体テスト可能な範囲を最大化する |
| D-8 | 最終判断の出力 | `final_disposition` を返す | スキーマに持たない | **B** | 犯収法第 8 条の届出判断・口座制限は人間の責任。下流の届出書叙述化テンプレートとの工程分界も同時に固定する |
| D-9 | DB / KB の実接続 | 具象クライアントを使う | 基底 + in-memory backend の注入シーム | **B** | 具象が SDK でスタブ（`NotImplementedError`）のため。実配線手順は `docs/07_operation_guide.md` に明記 |
| D-10 | S-3 拒否時の出力 | framework 既定の `get_output()` に任せる | `get_output()` を override して success 時のみ出力 | **B** | 既定は `formatted_output or result` を返すため、S-3 拒否時に未検証の `result` が漏れる。**実機で再現**（2026-07-30・レビュー Critical 指摘）。回帰テスト化する |
| D-11 | 自由文の断定抑止 | 構造化 claim + 決定論テンプレート描画（LLM に文字列を書かせない） | LLM が散文を書き、根拠 ID 検証 + 人間レビューで担保 | **B** | レビューでは A が要求されたが、2026-07-15 に A を過剰設計と判断し CTDClinicalSummaryAgent で巻き戻し済み。既定は穏当設計。**allowlist はキー名しか制限しない**という限界は S-3 節と運用ガイドに明記する |
| D-12 | 証跡の受け取り方 | ファイル本体をアップロード | 証跡参照 ID + メタデータのみ | **B** | パーサを持たない形式には攻撃面が無い（PDF action / CSV formula / 画像 metadata の対策が不要）。ファイル取込が要件化したら別途 `ingest-file-sanitizer` 部品を適用する |
| D-13 | runtime パラメータの置き場所 | `config/config.yaml` | `config/agent.yaml` の `config:` | **A** | 登録メタデータと実行時パラメータを分離し、用途ごとの設定責務を明確にする |
| D-14 | 想起スコープの粒度 | `institution_id` + `unit_id` を両方パーティションキーに | `institution_id` のみパーティション・`unit_id` は絞り込み軸 | **B** | ユーザー判断（2026-07-30）。AML は本部 AML 部門が一元管理する形が実務の主流で、部門をパーティションにすると同一銀行内の横断想起ができない。部門間の閲覧可否は顧客側の権限設計に委ねる |
| D-15 | 過去記録に顧客識別子を持たせるか | 仮名化した顧客キーを projection に含める | 含めない | **B** | ユーザー判断（2026-07-30）。①同一顧客の過去アラートは顧客の AML システムが既に保持 ②顧客キーを入れると「あの顧客は前回問題なし」という**結論の間接継承**に最も近い経路を自分で作ることになる。「仮名化しても個人データ」（ClaimConsistencyCheckAgent 判例）も踏まえる |
| D-16 | AG 生成物であることの表示 | メタデータのみ | 固定文言（`disclaimer`）+ `generation_metadata` を出力に必ず付与 | **B** | ユーザー判断（2026-07-30）。監査証跡としての追跡可能性を体裁より優先する。`disclaimer` は本文と別フィールドに置き、完成版へ転記するかは運用側が決められる形にする |
| D-17 | Stage 2 が部品の public projection をそのまま LLM に渡すか | そのまま渡す | テンプレ側でもう一段の allowlist 射影をかける | **B** | `accumulation-ledger._public_record()` は body/rationale の raw・audit を落とすが **`verdict_code` / `ai_suggested_verdict` / `classification` は残す**（実コードで確認）。本 AG ではこれが「過去の最終判断ラベル」であり、渡すとレビュー High 指摘（結論の間接漏洩）が再発する |
| D-18 | 書込モードの認可 | `VERIFIED_EXTERNAL` + スコープ照合で足りるとする | **モードごとに独立した RBAC**（`ingest` = データ管理職掌 / `feedback` = 判断確定職掌） | **B** | レビュー High 指摘（2026-07-30）。`feedback` にだけ RBAC を書き、同じく台帳へ書き込む `ingest` を trust level だけで開けていたのは非対称。`VERIFIED_EXTERNAL` は認証の証明であって、後続調査が読む証拠ベースを書き換える権限ではない。判定は payload store 預け入れ前・台帳アクセス前 |
| D-19 | 必須証跡表と期限の扱い | module 定数と期限なし | **config 化 + 期限チェック実装** | **B** | 表は顧客ごとに違うためコード変更なしに customize できる必要がある。期限の既定値は AML 実務の知識が要るのでテンプレートでは決めず、未設定なら無効。基準日は adapter 境界で一回確定しグラフ内で現在日時を参照しない（ユーザー裁定 2026-07-30）。 |

| D-20 | STG で `deploy-stg` を PASS させるか | 403 のまま `stage:stg` 留置 | `entry-adapter` の既定 STG フォールバックを使って PASS を取る | **B** | `authenticate(request, operation=READ_INVOKE)` の token-gated STG fallback を read-only smoke にだけ使用する。write operation は v0.4.0 policy により standalone token/STG default では常に 403。`unit_id` も verified identity として `SCOPE_KEYS` に含めるが、台帳 partition は D-14 のとおり `institution_id` のみ |

### ユーザビリティ観点で修正した点

1. **入力の最小化** — 当初案では案件の全項目を構造化 JSON で要求していたが、AML アナリストが自然に
   持っているのは「アラート ID + 添付証跡 + 自由記述の調査メモ」である。必須入力を `case_ref` /
   `alert_type` / 証跡リスト / 自由記述の 4 つに絞り、残りは任意項目とした。不足項目は Stage 1 が
   `evidence_gap` として返す（エラーで弾かない）。
2. **出力をそのまま次のアクションに使える形に** — 5 区分をフラットな文字列ではなく区分ごとの配列で
   返し、各 claim に `claim_id` を振って `evidence_map` から逆引きできるようにした。レビュー担当者が
   「この主張の根拠は何か」を 1 ホップで辿れる。
3. **KB が空でも価値を返す** — 導入初期は過去 disposition が蓄積されていない。Stage 2 が 0 件でも
   `status="success"` + `degradation_reason="no_prior_records"` として、Stage 1 と当該案件の証跡だけで
   ドラフトを生成する（縮退動作）。「過去事例なし」を明示して人間に渡す。
4. **エラー時に何が返るか** — LLM 未注入だけは deterministic fallback（Stage 1/2 の構造化結果のみを
   5 区分に配置し `memo_draft` は空）で `requires_human_review: true`。注入済み provider の auth/config/
   timeout/runtime/parse 障害は `status=error` + 安定 `error_code` で fail-closed とし、例外文は返さない。
   fallback では過去案件の `countervailing_notes` を現在案件の `countervailing_factors` へ昇格させず、
   「過去案件の調査観点として今回も確認する」という確認事項として `unknowns` に明示的に残す。
   これにより過去観点を捨てず、現在案件で確認済みの反証事情との混同だけを防ぐ。

## 既知の限界（正直に記録する）

- **S-3 は法令遵守を確立しない** — 製品安全の緩和策である。断定的な言い回しの完全な遮断は構造
  （スキーマからの排除）で担保し、自由文の表現までは保証しない。最終判断は人間が行う
- **PII 検知は FW 標準 `detect_pii` のみ** — 日本語の裸名・辞書外の姓は検出されない。日本語補完
  サニタイザは方針として実装しない（責務境界は framework 側）。本テンプレートは
  payload_ref 隔離で生本文を State / checkpoint から遠ざける防御を主とする
- **DB / KB は in-memory 注入シーム** — 実 DB / ベクタ DB への接続は顧客環境で配線する
- **証跡ファイルの取込は非対応** — 証跡は参照 ID + メタデータで受け取る。PDF / Excel / 画像の
  本文を読ませたい要件が出た場合は、`ingest-file-sanitizer` 部品（マジックバイト判定・未知形式
  reject・索引テキスト品質ゲート）を適用する別 MR が必要
- **prompt-injection 対策は検出・無害化ベース**であり完全ではない。顧客システム由来の照会回答
  テキストは外部由来コンテンツとして扱うが、未知パターンの混入は残余リスクとして人間レビューに残る
- **顧客データの export 可否・最小データセットは導入環境ごとに決定する必要がある。**
  PoC 開始前に顧客 AML / IT / セキュリティ責任者との統治契約が必要
