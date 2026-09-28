# FIN-C2-161 — AMLDispositionMemoAgent

> **Category**: Cat 2 (domain workflow)
> **Industry**: FIN

## Overview

AMLDispositionMemoAgent helps a human anti-money-laundering investigator prepare the internal
disposition memo that follows an alert. From the alert type, the evidence references the
investigator has collected, and their own notes, it assembles five sections — established facts,
evidence sources, unknowns, countervailing factors, and a draft narrative — together with an
evidence map that ties every claim back to a cited source.

What the agent will not do is the decision itself. There is no suspicious / not-suspicious field
in the output schema, no filing-required flag, and no account action: under Japan's Act on
Prevention of Transfer of Criminal Proceeds those judgements belong to a person, and the schema is
built so the agent cannot express one even by accident.

Three design choices carry most of the weight:

- **Non-conclusiveness is structural, not lexical.** There is no forbidden-word denylist —
  natural-language assertions are trivially paraphrased, so a denylist buys false assurance and
  maintenance cost. Instead the verdict field simply does not exist, every undeclared output key is
  rejected, each claim must carry a source reference, and the response always states that a human
  is the decision-maker.
- **Prior cases are recalled as questions, not as answers.** The agent can surface earlier
  dispositions in the same institution as investigation perspectives; the earlier verdicts and the
  identity of who decided them are excluded from what reaches the model, so a past outcome cannot
  propagate into a new memo as an implied conclusion.
- **The raw request body never enters graph state.** The HTTP adapter scrubs the envelope and
  vaults the free text in a short-lived, scope- and session-bound payload store, handing the graph
  a reference. What survives into state and any checkpoint is the reference plus a small allowlist
  of safe keys.

Output always requires human review, and says so in the payload it returns.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Integration

Four HTTP endpoints, with authorization split by direction:

| Endpoint | Purpose | Who may call it |
|---|---|---|
| `POST /invoke` | Draft a disposition memo for one case | Any verified caller within the request's scope |
| `POST /ingest` | Load prior disposition records into the recall ledger | Verified middleware identity holding a role from `roles.ingest` |
| `POST /feedback` | Record a reviewer's correction against an earlier record | Verified middleware identity holding a role from `roles.feedback` |
| `GET /health` | Liveness | Unauthenticated |

Provide:

- an AgentCore runtime;
- a `BaseLLM` implementation injected through the graph configuration. No provider client is
  constructed by the graph itself; the Marketplace entry adapter in `cli.py` builds an Azure OpenAI
  client lazily from deployment secrets. Without one, `/invoke` still answers — see
  *Behaviour without the platform*;
- authentication middleware that stamps a verified trust level, a `caller_id`, the caller's roles,
  and the two scope axes `institution_id` / `unit_id`. None of these are ever read from the request
  body, and `decided_by` on the feedback path is stamped from the verified identity rather than
  accepted from the caller;
- durable storage for the ledger and the payload store. Both ship as in-memory seams
  (`src/services/`), which is what keeps the test suite hermetic — a production deployment
  replaces them.

Runtime parameters live in `config/config.yaml` (defaults in parentheses):

| Key | Meaning |
|---|---|
| `scope_keys` (`["institution_id"]`) | Partition key for the recall ledger. Start-up refuses any other value |
| `recall_top_k` (`5`) | Number of prior records recalled as perspectives |
| `required_evidence_by_alert_type` | Evidence kinds expected per alert type; a missing kind becomes an entry in `unknowns` |
| `evidence_max_age_days` (`null`) | Age past which cited evidence is treated as stale; `null` disables the check |
| `payload_ttl_seconds` (`900`) | Lifetime of the vaulted raw request body |
| `max_free_text_chars` (`12000`) | Maximum accepted investigator note length |
| `require_evidence_link_for_all_claims` (`true`) | Whether every claim must carry a source reference |
| `roles.ingest` / `roles.feedback` | Role names authorised for each write endpoint |

`wire_services()` validates these at start-up: a wrong `scope_keys`, a non-positive
`payload_ttl_seconds` or `recall_top_k`, or a `max_free_text_chars` below five stops the process
with a `RuntimeError` rather than falling back to a default.

The template does not ingest file bodies, does not connect to an external database or vector store
on its own, does not prepare or submit a regulatory filing, and does not restrict accounts.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent is designed to run on the platform; deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Supplied by the platform's own package registry at build time. It is not published on public PyPI, so `pip install -e .` alone does not fetch it. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The standalone server in `src/api/server.py` starts without the platform — it wires the in-memory
services and compiles the graph at start-up, so a malformed runtime config fails immediately, and
`/health` then answers `{"status": "ok"}`. What it will not do is fake an identity or a model. A
request with no verified caller is rejected before any node runs (**401** with a wrong bearer
token, **403** with no verified identity or scope), and a `/invoke` that reaches the graph with no
injected model returns `status: success` carrying an explicitly degraded body: `memo_draft` empty,
`requires_human_review: true`, `review_reasons: ["llm_unavailable"]`, and a `degradation_reason`
naming the missing client. The deterministic sections — evidence sources, unknowns derived from
`required_evidence_by_alert_type`, disclaimer, generation metadata — are still produced, so the
response is honest about what is missing instead of silently shrinking. The test suite runs without
a platform connection.

## Input

`POST /invoke` takes a JSON body; unknown fields are rejected (**422**), as is a non-ISO-8601
`as_of_date`:

| Field | Meaning |
|---|---|
| `case_ref` | Case identifier (`^[A-Za-z][A-Za-z0-9_-]{0,127}$`) |
| `alert_type` | `cash`, `sanctions`, `transaction_monitoring`, `kyc`, … — keys of `required_evidence_by_alert_type` |
| `evidence_refs[]` | `source_id`, `kind`, and optional `amount` / `date` / `transaction_id` / `account_last4` / `label` |
| `free_text` | The investigator's own notes |
| `as_of_date` | Optional ISO-8601 reference date; defaults to today, fixed once at the adapter boundary |
| `session_id` | Optional; generated when absent |

Callers must be authenticated. With authentication middleware in front, its verified trust level,
`caller_id`, roles and scope attributes are used. Without middleware, setting `INVOKE_AUTH_TOKEN`
on the server environment enables a bearer token that promotes the caller to `VERIFIED_EXTERNAL`
for **read only** — `/ingest` and `/feedback` always require a verified middleware identity, so the
token path cannot write. On the token path the scope axes come from `STG_DEFAULT_INSTITUTION_ID`
and `STG_DEFAULT_UNIT_ID`; a missing or malformed value is a 403, never a silent default.

```bash
curl -X POST http://127.0.0.1:8000/invoke \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${INVOKE_AUTH_TOKEN}" \
  -d '{
    "case_ref": "CASE-1",
    "alert_type": "cash",
    "evidence_refs": [{"source_id": "TX1", "kind": "transaction_record"}],
    "free_text": "Repeated cash deposits just below the reporting threshold.",
    "session_id": "example-session"
  }'
```

## Output

The standard AgentCore envelope is preserved: `output`, `status`, `trace_id`, `correlation_id`,
`node_history`. `output` is a closed schema — an undeclared key is rejected rather than passed
through:

| Field | Content |
|---|---|
| `case_ref`, `mode`, `gate_kind` | Echoed case identity and the stage that produced the body |
| `facts[]` | Facts assembled from the supplied references and notes |
| `evidence_sources[]` | Cited sources, each with `source_id` and `kind` |
| `unknowns[]` | What could not be established — including evidence kinds required for the alert type but not supplied |
| `countervailing_factors[]` | Considerations that argue against the apparent reading |
| `memo_draft` | The narrative draft |
| `evidence_map[]` | Claim-to-source mapping used to check that every claim is grounded |
| `requires_human_review`, `review_reasons[]` | Always-on review requirement and why it was raised |
| `degradation_reason` | Present when the run was degraded (for example, no model configured) |
| `disclaimer` | Fixed statement that the memo is an AI draft and the decision remains with the investigator |
| `generation_metadata` | `generated_at`, `template_id`, `template_version`, `source_ids[]` |

There is deliberately no verdict, no filing-requirement field and no recommended action.

## Security and Limitations

Read and write paths are separated at the adapter: `/ingest` and `/feedback` require a verified
middleware principal *and* a role drawn from the configured `roles` block, checked against verified
request state rather than anything in the body; a rejection is a 403 accompanied by an
`authorization_denied` audit event. The raw request body is scrubbed and vaulted before the graph
sees it and deleted in a `finally` block after the invocation, so unreviewed free text does not
linger in state. Recalled prior records reach the model without their verdicts or decision-makers.

Known limits, recorded rather than hidden:

- the closed output schema constrains **key names**, not the wording inside a permitted free-text
  field such as `memo_draft`. Prose can still read more assertively than intended; that is why
  human review is mandatory and stated in the response, and why a denylist was rejected rather
  than added;
- PII detection is the framework's standard detector only. Bare Japanese personal names and names
  outside its dictionary are not detected — the payload-vault design, which keeps raw text out of
  state and checkpoints, is the primary defence rather than masking;
- the ledger, payload store and feedback receipt store are in-memory seams; nothing survives a
  restart until a deployment supplies durable implementations;
- scope is partitioned on `institution_id` alone. A second axis (`unit_id`) is carried and enforced
  at the adapter, but the ledger partition itself is single-axis, which is a deliberate, documented
  trade-off — see `docs/02_design.md`;
- file-body ingestion and direct external database or vector-store connections are out of scope for
  this release.

## Release

- Version: 1.0.0
- Category: fixed multi-step domain workflow (Cat 2)
- Generation mode: `llm` (narrative drafting only; evidence checks and gap detection are deterministic)
- HITL: not enabled — review happens outside the graph, through `/feedback`
- Persistence: none — no checkpointer is configured
- External API clients: deployment-injected; none bundled

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection, a model, a database or network access; the model and the
stores are injected fakes. Running the agent itself needs the platform, an injected `BaseLLM`, and
durable stores.

## Documentation

`docs/02_design.md` carries the node flow, the payload-isolation design, the output allowlist, the
recall and feedback contracts and the security design. `docs/03_test_spec.md` records the test
specification and its results.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and test specifications
```

See `docs/02_design.md` for the design and `docs/03_test_spec.md` for the test specification.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Replace the knowledge sources and sample data with your own.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
