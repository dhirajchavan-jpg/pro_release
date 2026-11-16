# Procurement Evaluation API

FastAPI service and durable worker for Indian government/PSU bid evaluation.

## Local launch (no Docker)

Use the installed local PostgreSQL 15+ service. Copy `.env.example` to `.env`, then set PostgreSQL, S3-compatible storage, authentication, India-region assertion, and one selected AI provider. Gemini works now; Yotta capability URLs/models can be inserted later without code changes.

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\procurement-api
```

In a second terminal, start the durable worker:

```powershell
.\.venv\Scripts\procurement-worker
```

The API is available at `http://127.0.0.1:8000`. It does not call a model until an evaluation is queued.

### Windows launcher scripts (no Docker)

`Setup.bat` uses Python 3.12, installs the package, preserves an existing `.env`, and tests the configured PostgreSQL URL/server version. The offline Windows package carries a signed private Python 3.12 runtime, so a machine-wide Python 3.14 installation is left untouched. `Start_All.bat` runs production preflight and migrations, starts the API and durable worker as hidden owned processes, and waits for `/ready`. `Stop_All.bat` validates every PID before stopping anything; `Restart_All.bat` combines both. Logs and PID files live under `RUNTIME_DIR`.

## Manual run

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\procurement-verify-config
.\.venv\Scripts\procurement-api
# Start .\.venv\Scripts\procurement-worker in a second terminal.
```

Operational limits:

```powershell
$env:RATE_LIMIT_PER_MINUTE="30"
$env:MAX_UPLOAD_MB="50"
$env:GEMINI_REQUEST_TIMEOUT_SECONDS="60"
$env:GEMINI_RETRY_ATTEMPTS="3"
$env:MAX_RUNNING_JOBS="1"
$env:WORKER_CONCURRENCY="1"
$env:MAX_QUEUED_OPERATIONS="11"
$env:MAX_BIDDERS_PER_TENDER="10"
$env:MAX_JOB_SECONDS="900"
$env:PROMPT_OVERRIDES_FILE="config/prompts.example.json"
$env:PROCUREMENT_DOCUMENT_DICTIONARY_FILE="config/procurement_document_dictionary.json"
$env:JOBS_ROOT="runtime/jobs"
```

`AUTH_MODE=api_token` is the supported single-account local-install mode. The production trial disables direct API-token authentication: successful browser login replaces any earlier session for the configured account. Use `/health` for liveness and `/ready` for live PostgreSQL/object-storage readiness.

## Configuration

### RFP OCR

Native PDF text and tables are extracted locally first. Only weak/scanned pages enter OCR. The Windows package supports hosted Paddle (`paddle_api`) and the configured OpenAI-compatible OCR capability (`ai`). OCR checkpoints are content-addressed and reused. Local Paddle execution (`paddle`) requires a separately validated PaddlePaddle runtime and model cache; it is not part of the offline Windows package.

Provider fallback is disabled. A provider failure is recorded explicitly and is never silently routed to another model.

Provider and model changes are environment-only:

```powershell
$env:AI_PROVIDER="gemini" # or openai_compatible
$env:AI_MODEL_FAST="gemini-3.7-flash"
$env:AI_MODEL_DEEP="gemini-3.7-flash"
```

Restart uvicorn after changing env variables. Prompt override file contents are read at request time, but restart after prompt edits is still the safest operational path.

Prompt changes can be made through `PROMPT_OVERRIDES_FILE`, a JSON object keyed by agent name. Copy `config/prompts.example.json`, edit only the keys you want, and point the env var at that file. For one-off changes, set `PROMPT_<KEY>` directly.

Prompt keys:

```text
ocr_pdf_page
document_classifier
page_classifier
base_rfp_understanding
rfp_reader
criteria_coverage_auditor
requirement_planner
vendor_profile
document_verifier
visual_signature
final_evaluator
```

## AI provider configuration

`AI_PROVIDER=gemini` preserves the existing native Gemini route. Set
`AI_PROVIDER=openai_compatible` to use the official OpenAI Python SDK for an
OpenAI-compatible provider such as a compatible Gemini deployment or Yotta.
Set `AI_API_KEY`, `AI_BASE_URL`, `AI_MODEL_FAST`, `AI_MODEL_DEEP`, and
`AI_MODEL_OCR`; limits and retries use `AI_MAX_OUTPUT_TOKENS`,
`AI_REQUEST_TIMEOUT_SECONDS`, `AI_RETRY_ATTEMPTS`, and `AI_RETRY_BASE_SECONDS`.
Startup/readiness fails if the selected provider is incomplete.
There is no automatic provider or model fallback. Primary LLM, OCR, embeddings,
reranker, and verifier each have ENV URL/model/key settings and use the OpenAI
SDK when `AI_PROVIDER=openai_compatible`.

Set `AI_REQUIRE_INDIA_REGION=1` and `AI_INFERENCE_REGION=india` for the
application-level residency assertion, and enforce the same restriction with
provider account controls and network egress policy.

## Verify

```powershell
.\.venv\Scripts\python -m tests.test_app
.\.venv\Scripts\python -m tests.test_api_route_contracts
.\.venv\Scripts\python scripts\smoke_resource_api.py
.\.venv\Scripts\python scripts\smoke_workbench_api.py --tender <file> --bid "Bidder=<file>"
.\.venv\Scripts\python scripts\run_eval_dataset.py --offline --repeat 2
.\.venv\Scripts\python scripts\offline_api_smoke.py
.\.venv\Scripts\python scripts\offline_api_stress.py --requests 8 --workers 4
```

The legacy filesystem-backed `/evaluate` and `/jobs/{id}` compatibility routes
are disabled automatically when `APP_ENV=production`; production workflows use
the PostgreSQL-backed `/v1` APIs.

The final workflow transition uses
`POST /v1/workbench/projects/{project_id}/report/generate`; report retrieval
and downloads are read-only.

Retention is dry-run by default; schedule the apply command under the approved operations account:

```powershell
.\.venv\Scripts\procurement-retention
.\.venv\Scripts\procurement-retention --apply
```

Run only the generalized base-RFP layer, without a vendor bid:

```powershell
.\.venv\Scripts\python scripts\run_base_rfp.py "C:\path\to\tender-or-packet.zip" --output samples\runs\base_rfp\my-run
```

Stop before semantic extraction and validate only the local physical/structural layer:

```powershell
.\.venv\Scripts\python scripts\run_base_rfp.py "C:\path\to\tender-or-packet.zip" --structure-only --output samples\runs\structure_eval\my-run
.\.venv\Scripts\python scripts\evaluate_structure.py
```

`--structure-only` does not require a Gemini key. It writes `01_file_inventory.json`, the lossless page/block/table/cell ledger in `02_source_units.json`, the OCR audit in `02a_layout_ocr.json`, logical-document boundaries in `02b_logical_documents.json`, base-source selection in `02c_base_selection.json`, and the section graph plus integrity gates in `02d_structure.json`. Use `--ocr-cache <path>` to reuse content-addressed PaddleOCR checkpoints across runs.

Opt into targeted structural adjudication when an API key is available:

```powershell
.\.venv\Scripts\python scripts\run_base_rfp.py "C:\path\to\tender.pdf" --structure-only --adjudicate-structure --structural-review-max-pages 12 --output samples\runs\structure_eval\reviewed
```

The local parser first ranks heading disagreements using layout, sentence context, numbering consistency, and hierarchy blast radius. Only selected single-page PDFs and candidate block IDs are sent to Gemini; page text and tables are not retranscribed. Decisions are content-addressed under `checkpoints/structure`, the original graph is retained in `02d_structure_initial.json`, and the reviewed graph plus audit are written to `02d_structure.json` and `02e_structural_adjudication.json`.

The structural gate accounts for every physical page, including explicit blank pages; verifies source IDs, reading order, section parents, table overlap links, and character carry-through; and fails on weak/unreadable pages or silent loss. PaddleOCR is invoked only for weak pages, corrects dense rotated pages locally, and bypasses orientation on confidently sparse pages.

This writes the physical source-unit ledger, logical-document reconstruction, base-source selection, compact semantic atoms, canonical `base_rfp.v1` record, and legacy compatibility projection. `BASE_RFP_OCR_MAX_PAGES=0` means all weak tender pages are local-OCR candidates. Semantic calls are bounded by `BASE_RFP_CHUNK_MAX_UNITS` and `BASE_RFP_CHUNK_MAX_SOURCE_CHARS`; output is separately capped by `BASE_RFP_ATOM_MAX_OUTPUT_TOKENS` and `BASE_RFP_ATOM_OUTPUT_TOKENS_PER_UNIT`.

The source-unit ledger, not model output, retains every word, table cell, page, and coordinate. Gemini receives that local ledger as input only for semantic annotation. Its response groups simple unit IDs by type and emits detailed atoms only for structured fields, requirements, policies, or cross-label cases; it never retranscribes page text. Cached atoms are the semantic source of truth, so changing the 21-key response projection reuses the cache and makes zero model calls.

`--offline` and `offline_api_smoke.py` use `PROCUREMENT_OFFLINE_LLM=1` to exercise the pipeline without a network model call. Real Gemini evaluation still requires `GEMINI_API_KEY`:

```powershell
$env:GEMINI_API_KEY="your_gemini_key"
.\.venv\Scripts\python scripts\run_eval_dataset.py --repeat 2
```

## API

```http
POST /evaluate
Authorization: Bearer <PROCUREMENT_API_TOKEN>
form-data:
  tender: PDF/DOCX/XLSX/TXT/ZIP. Zip it when the tender has multiple files.
  guidelines: optional PDF/DOCX/XLSX/TXT/ZIP with buyer rules, prior guidelines, or evaluation policy.
  guidelines_text: optional pasted buyer rules/guidelines text.
  vendor_bid: ZIP
```

Response:

```json
{"bid_id":"...","status":"queued","status_url":"/jobs/..."}
```

Poll:

```http
GET /jobs/{bid_id}
Authorization: Bearer <PROCUREMENT_API_TOKEN>
```

Successful jobs return a simple `report` and no local filesystem paths. The public report shows tender summary, vendor summary, criteria, missing mandatory items, manual-review reasons, and financial-opening status. Detailed page units, quotes, full internal report, and extracted artifacts are still written internally under `JOBS_ROOT/{bid_id}/`.

The internal flow is RFP-led and writes numbered handoff files:

```text
01_file_inventory.json
02_extracted_units.json
02_base_rfp_layout_ocr.json
02_base_rfp_selection.json
03_base_rfp_schema.json
03_rfp_understanding.json
03a_tender_packet_warnings.json
04_requirement_matrix.json
05_vendor_logical_documents.json
05a_evidence_scout_log.json
06_criteria_evidence_map.json
06a_evidence_packets.json
06b_specialist_findings.json
07_document_verification.json
08_criteria_findings.json
09_internal_decision_report.json
10_final_public_report.json
11_llm_usage_and_cost.json
```

The server validates the model JSON with Pydantic and then applies strict deterministic checks itself: missing/failed mandatory criteria reject the vendor, unclear mandatory criteria require manual review, every public criterion must have a result, and financial opening stays blocked unless there are no failures or review blockers. Job status is also written to `status.json` and `events.jsonl` in the job artifact folder so a completed bid can still be inspected after a server restart.

Research assumptions come from the DoE procurement manuals, GFR 2017, and GeM GTC sources catalogued in `procurement/domain/catalog.py` and `docs/RESEARCH_AND_TEST_PLAN.md`.

See `docs/RESEARCH_AND_TEST_PLAN.md` for source-backed evaluator rules and sample-document strategy.
See `docs/API_GUIDE.md` for frontend request/response integration, polling, auth, error handling, and deployment notes.
See `docs/AUDIT_GUIDE.md` for runtime ownership, trust boundaries, end-to-end flow, and auditor entry points.
