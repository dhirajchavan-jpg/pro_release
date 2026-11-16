# Repository audit guide

This document is the shortest path through the production code. Detailed API
payloads live in `API_GUIDE.md`, `PIPELINE_API.md`, and the fixed consumer
contract. Windows installation and recovery procedures live in
`../INSTALL_WINDOWS.md`.

The interactive architecture map is `.codex/codebase-flow.html`; its reviewed,
source-grounded model is `.codex/codebase-flow-model.json`. The map is
self-contained and needs no server or network connection.

## Runtime components

| Component | Entry point | Responsibility |
| --- | --- | --- |
| HTTP API | `procurement.launch:run_api` | FastAPI, authentication, tenant isolation, request validation, stable errors, UI assets, and queue submission |
| Durable worker | `procurement.worker:run_forever` | Claims PostgreSQL operations, runs understanding/compliance/intake/evaluation, checkpoints progress, and records bounded failures |
| PostgreSQL | `procurement/domain/models.py` and Alembic | Authoritative tenants, users, tenders, versions, documents, bids, evaluations, reviews, operations, idempotency keys, clarifications, and audit events |
| Object storage | `procurement/platform/object_store.py` | Uploaded and generated binary objects; filesystem for one local server or S3-compatible storage through environment configuration |
| Browser workbench | `frontend/` and `procurement/workbench_pages.py` | Human review across tender, compliance, checklist, bids, evaluation, results, and reports |
| Supervisor | `scripts/windows/` | Verified setup, migrations, start/stop/restart, crash recovery, PID ownership, and live logs |

The API runs as one process. Long work never runs in a request handler; it is
queued in PostgreSQL and processed by the durable worker. Production preflight
enforces the configured single-operator queue limits.

## Code ownership

| Area | Files |
| --- | --- |
| Application assembly and compatibility exports | `app.py` |
| HTTP middleware, auth, errors, rate limits | `procurement/platform/api_runtime.py`, `auth.py`, `errors.py`, `http.py` |
| Secure archive/document ingestion | `procurement/platform/ingestion.py` |
| Database, queue, idempotency, audit chain | `procurement/platform/database.py`, `operations.py`, `procurement/domain/models.py` |
| Tender extraction and field understanding | `procurement/understanding/native.py`, `ocr_pipeline.py`, `tender_understanding.py`, `field_understanding_*.py` |
| Canonical tender schema and amendments | `base_rfp.py`, `procurement/understanding/base_rfp_models.py`, `base_rfp_normalization.py`, `base_rfp_amendments.py` |
| Package amendment precedence | `procurement/understanding/field_amendments.py`, `field_amendments_parsing.py`, `field_amendments_compiler.py`, `field_amendments_state.py` |
| OCR provider adapters | `procurement/understanding/ocr_providers.py` |
| Compliance and policy application | `procurement/compliance/` |
| Evidence retrieval | `procurement/evaluation/semantic_retrieval.py`, `evidence_pipeline_packets.py`, `evidence_pipeline_routing.py`, `evidence_pipeline_specialists.py`, `evidence_scout.py` |
| Bid evaluation and deterministic controls | `procurement/evaluation/pipeline.py`, `assessment.py`, `validation.py`, `decision_ledger.py` |
| Workbench APIs | `procurement/workbench/` |
| Normalized API | `procurement/api.py` |
| Fixed consumer contract | `procurement/fixed_pipeline_api.py` |
| Durable orchestration | `procurement/worker.py` |
| Provider SDK boundary | `ai_provider.py` |
| Database migrations | `alembic/versions/` |
| Verification suite | `tests/` |

`app.py` remains the compatibility boundary for the proven extraction flow.
Business helpers have been moved behind the ownership boundaries above while
their existing names remain available to callers and tests.

The remaining long modules are intentional, cohesive state machines rather
than composition roots: `field_understanding_field.py` resolves one field,
`field_retrieval_search.py` performs progressive field retrieval,
`field_amendments_compiler.py` compiles package amendment state,
`classification.py` owns tender/bid classification, and
`evaluation/search.py` owns criterion search planning. Splitting these solely
by line count would scatter shared state without creating a new owner.

## End-to-end flow

1. **Authenticate and scope.** Middleware establishes the actor and tenant.
   Every workbench entity lookup includes the tenant identifier.
2. **Accept the tender package.** The ingestion boundary validates size, media
   type, archive paths, compression limits, nesting, and document format before
   storing the original bytes.
3. **Build the source ledger.** Native text, blocks, tables, page numbers, and
   coordinates are retained. Weak or scanned pages alone are routed to the
   configured OCR capability and cached by content digest.
4. **Resolve document precedence.** The understanding layer identifies the
   base tender, corrigenda, pre-bid replies, and amendments without discarding
   superseded evidence.
5. **Compile tender facts.** Locally retrieved candidates are resolved into the
   configured field schema with source references, confidence, and validation.
   Unresolved or conflicting material remains reviewable; it is not guessed.
6. **Audit compliance and freeze checks.** Policy findings and the bidder
   evaluation checklist are reviewable before the contract is frozen. Later
   evaluation reads the frozen version.
7. **Accept and classify bids.** Each bidder package is stored separately.
   Nested folders and combined PDFs are represented as logical documents;
   commercial material remains quarantined until authorized.
8. **Retrieve evidence per criterion.** Classification, lexical search,
   embeddings, reranking, document summaries, and bounded progressive search
   produce evidence packets. Visual verification is requested only when page
   appearance is material or text evidence is insufficient.
9. **Evaluate deterministically.** Provider output is schema-validated, then
   local validators enforce evidence binding, mandatory failures, review
   blockers, policy treatment, and commercial-opening controls.
10. **Review and report.** Human decisions, clarifications, results, Markdown,
    and Excel reports retain tender and bidder provenance. Reports are generated
    explicitly; downloads are read-only.

## Trust and data boundaries

- The private Windows handover archive intentionally includes the approved `.env`; it must be
  transferred and stored as a secret-bearing artifact and must never be committed.
- PostgreSQL is authoritative; local JSON files are not the production state
  store.
- Binary objects are addressed through the object-store interface and linked
  to database records by digest and identifier.
- Provider, OCR, embedding, reranker, and verifier endpoints are explicit
  egress boundaries. The selected URL, model, key, timeout, and concurrency are
  environment-owned. There is no automatic provider or model fallback.
- Production must assert India-region inference and enforce it independently
  with network and provider controls.
- Browser and document responses use private/no-store caching. Logs exclude
  request bodies, uploaded contents, prompts, and secret values.
- Price documents stay quarantined until the recorded authorization allows
  opening.

## Integrity and recovery controls

- Idempotency keys bind one request hash to one operation; reuse with different
  input is rejected.
- Operations use leases, attempts, deadlines, retry scheduling, and restart
  recovery in PostgreSQL.
- One bad page can be recorded as degraded without silently discarding the
  remaining package; terminal provider and validation failures are explicit.
- Review and workflow mutations append tenant-scoped hash-chained audit events
  with actor, reason, before state, after state, prior hash, and event hash.
- Controlled restarts archive prior logs and validate PID ownership before
  stopping a process.

## API surfaces

- `/v1/workbench/*`: first-party browser workflow.
- `/v1/pipeline/fixed-schema/*`: frozen consumer integration contract.
- `/v1/*`: normalized tender, bid, evaluation, operation, and report resources.
- `/health` and `/ready`: liveness and dependency readiness.
- Legacy `/evaluate` and `/jobs/{id}` routes are disabled automatically in
  production.

Do not change request/response schemas or browser routes without updating their
executable contract tests and obtaining explicit compatibility approval.

## Build and verification

Run the complete offline product gate:

```powershell
.\.venv\Scripts\python.exe scripts\run_tests.py
```

Run the security, dependency, license, parse, secret, and SBOM gate:

```powershell
.\Security_Gate.bat SECURITY
```

Builds are allowlist-based through `scripts/build_release.py`. The private
Windows package excludes source-control metadata, tests, runtime state, logs,
uploads, and prior outputs. It includes the approved `.env`, a SHA256
manifest, CycloneDX SBOM, dependency/license inventory, security reports,
signed Python and LibreOffice installers, offline wheels, and PostgreSQL.

Before deployment, verify `PACKAGE_MANIFEST.sha256`, configure the approved
endpoints in `.env`, run setup and preflight, then use the controlled launcher.
