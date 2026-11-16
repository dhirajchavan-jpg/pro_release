# Procurement Pipeline API — Frontend Integration Contract

Contract version: `1.0`  
Base URL (local default): `http://127.0.0.1:8000`  
Interactive schema: `GET /openapi.json` (authenticated)  
Primary integration surface: three start routes and one shared polling route

## 1. Workflow

```text
Tender file + field schema
        |
        v
POST /v1/pipeline/tenders/understand ----> job_id
        |                                      |
        |<---- GET /v1/pipeline/jobs/{job_id} -+
        |
        v
Human reviews tender findings and freezes the contract
        |
        v
Bidder file + project_id
        |
        v
POST /v1/pipeline/bids/classify ---------> job_id
        |                                      |
        |<---- GET /v1/pipeline/jobs/{job_id} -+
        |
        v
Human reviews and approves bidder classification
        |
        v
POST /v1/pipeline/bids/evaluate ---------> job_id
        |                                      |
        |<---- GET /v1/pipeline/jobs/{job_id} -+
        |
        v
Criterion-level result and overall decision
```

Understanding, classification, and evaluation are separate asynchronous jobs.
The backend does not silently approve AI output.

## 2. Browser integration

The production-safe default is same-origin access. A browser frontend should
serve behind the same host or use its own server-side proxy:

```text
Browser -> frontend server/reverse proxy -> Procurement API
```

Do not embed `PROCUREMENT_API_TOKEN` in downloadable browser JavaScript. Use
the frontend's server session or JWT authentication. Wildcard CORS is not
enabled.

## 3. Authentication and common headers

API-token mode:

```http
X-API-Key: <PROCUREMENT_API_TOKEN>
```

JWT mode:

```http
Authorization: Bearer <JWT>
```

Recommended headers for every start request:

```http
Idempotency-Key: <unique-business-operation-key>
X-Request-ID: <frontend-generated-correlation-id>
```

Rules:

- Maximum `Idempotency-Key` length: 255 characters.
- Reuse the same idempotency key only when retrying the same operation.
- Reusing a key for different inputs returns `409`.
- The response contains `X-Request-ID`; record it with frontend error logs.
- API-token and JWT modes are selected by backend environment configuration.

## 4. Shared TypeScript contracts

```ts
export type StartStatus = "queued" | "running" | "complete" | "failed";
export type JobStatus = "queued" | "running" | "complete" | "failed";
export type JobStage = "understanding" | "intake" | "evaluation";

export interface PipelineAccepted {
  job_id: string;
  status: StartStatus;
  job_url: string;
  project_id: string;
  tender_id?: string;
  tender_version_id?: string;
  bid_id?: string;
  vendor_id?: string;
  evaluation_id?: string;
}

export interface PipelineJob<T> {
  job_id: string;
  stage: JobStage;
  status: JobStatus;
  error_code: string | null;
  error: string | null;
  result: T | null;
}

export interface ApiError {
  error: {
    code: string;
    message: string;
    details: Record<string, unknown>;
  };
}
```

`complete` can appear on a start response when an idempotent retry refers to
an already completed job.

## 5. API 1 — Tender understanding

### Request

```http
POST /v1/pipeline/tenders/understand
Content-Type: multipart/form-data
```

| Multipart field | Type | Required | Validation and meaning |
|---|---|---:|---|
| `file` | binary | yes | One tender document or package |
| `schema_json` | JSON string | no | `rfp-field-schema.v1`; configured default when omitted |
| `project_key` | string | no | Caller reference, normalized to lowercase hyphenated form; maximum 120 characters after normalization |
| `title` | string | no | Human-readable project title |

Accepted top-level file extensions:

```text
.zip .pdf .doc .docx .xls .xlsx .csv .txt
```

The configured trial defaults allow a 50 MB upload, 500 PDF pages, a 50 MB ZIP
entry, and a maximum ZIP compression ratio of 200. These limits are backend
environment settings and may differ by deployment.

Security behavior:

- Path traversal and unsafe ZIP entries are rejected.
- Encrypted PDFs are rejected.
- File signatures and Office containers are validated.
- Empty, binary-looking text, malformed PDF/ZIP/Office files, and unsupported
  extensions are rejected.

### Field-schema format

This endpoint accepts the application's reviewed field schema, not arbitrary
JSON Schema draft-07/OpenAPI schema.

```json
{
  "schema_version": "rfp-field-schema.v1",
  "schema_id": "client-tender-schema-v1",
  "prompt": "Optional instruction applied across fields.",
  "sections": [
    {
      "key": "identity",
      "label": "Tender identity",
      "fields": [
        {
          "key": "tender_number",
          "label": "Tender number",
          "description": "Published tender, RFP, bid, or NIT number.",
          "value_type": "string",
          "prompt": "Optional field-specific instruction.",
          "retrieval_guidance": {
            "aliases": ["tender number", "RFP number", "NIT number"],
            "anchor_phrases": ["notice inviting tender"],
            "likely_sections": ["Cover page", "Tender data sheet"]
          }
        }
      ]
    }
  ]
}
```

Required schema rules:

- `schema_version` must equal `rfp-field-schema.v1`.
- `sections` must be a non-empty array.
- Every section requires a non-empty `key` and non-empty `fields`.
- Every field requires a unique, non-empty `key`.
- Maximum serialized schema size: 2 MB.

### Example

```powershell
curl.exe -X POST "$BASE_URL/v1/pipeline/tenders/understand" `
  -H "X-API-Key: $API_TOKEN" `
  -H "Idempotency-Key: tender-2026-001-v1" `
  -H "X-Request-ID: frontend-tender-2026-001" `
  -F "file=@C:\input\tender.zip" `
  -F "schema_json=<C:\input\field-schema.json" `
  -F "project_key=tender-2026-001" `
  -F "title=Core banking procurement"
```

### 202 response

```json
{
  "job_id": "4d0ca806-871e-46d4-b60c-14ecc3e8470b",
  "status": "queued",
  "job_url": "http://127.0.0.1:8000/v1/pipeline/jobs/4d0ca806-871e-46d4-b60c-14ecc3e8470b",
  "project_id": "51f5dc1d-871d-4e43-999d-e1f1f4f1d29d",
  "tender_id": "e3fbe533-5955-4b86-b5b5-c9d3f7e8f8ca",
  "tender_version_id": "f5f55000-923b-451b-9450-77f3f9e52020"
}
```

Store `project_id`; classification and evaluation use it.

### Completed result

`GET job_url` returns `PipelineJob<TenderUnderstandingResult>`. Important
result sections are:

```ts
export interface TenderUnderstandingResult {
  schema_version: "procurement-understanding.v1";
  project_id: string;
  tender_id: string;
  tender_version_id: string;
  review_status: "review_required" | "approved";
  field_schema: {
    schema_id: string;
    field_count: number;
    section_count: number;
    sections: string[];
  };
  ledger: {
    ledger_status: "complete" | "needs_review";
    editable: boolean;
    tender_summary: {
      text: string;
      source: string;
      key_facts: Array<{ label: string; value: string }>;
    };
    entries: PipelineLedgerEntry[];
    warnings: unknown[];
  };
  documents: PipelineDocument[];
  links: PipelineLinks;
}
```

The pipeline result is intentionally curated. Internal extraction artifacts
are not part of this frozen consumer contract.

## 6. API 2 — Bid classification

### Preconditions

- `project_id` exists.
- Tender understanding is complete.
- The reviewed tender contract is frozen and its version is approved.
- The bidder does not already exist in the project.

### Request

```http
POST /v1/pipeline/bids/classify
Content-Type: multipart/form-data
```

| Multipart field | Type | Required | Validation and meaning |
|---|---|---:|---|
| `file` | binary | yes | One bidder package; same secure file types and upload limits as tender ingestion |
| `project_id` | UUID string | yes | Returned by tender understanding |
| `vendor_name` | string | yes | Display name; normalized `vendor_id` is returned |

```powershell
curl.exe -X POST "$BASE_URL/v1/pipeline/bids/classify" `
  -H "X-API-Key: $API_TOKEN" `
  -H "Idempotency-Key: tender-2026-001-acme-classification-v1" `
  -F "file=@C:\input\acme-bid.zip" `
  -F "project_id=$PROJECT_ID" `
  -F "vendor_name=Acme Limited"
```

### 202 response

```json
{
  "job_id": "a96c1300-c019-439f-93ba-4091e68d6145",
  "status": "queued",
  "job_url": "http://127.0.0.1:8000/v1/pipeline/jobs/a96c1300-c019-439f-93ba-4091e68d6145",
  "project_id": "10044675-09c9-4f0b-9476-99f8fcea54c0",
  "bid_id": "4663bfd0-9033-42a2-9c02-1d154dfc5e31",
  "vendor_id": "acme-limited"
}
```

### Completed result

```ts
export interface BidClassificationResult {
  schema_version: "procurement-classification.v1";
  project_id: string;
  bid_id: string;
  vendor_id: string;
  vendor_name: string;
  review_status: "review_required" | "approved";
  document_types: string[];
  documents: PipelineDocument[];
  logical_documents: ClassifiedDocument[];
  physical_routes: PhysicalRoute[];
  links: PipelineLinks;
}

export interface ClassifiedDocument {
  logical_document_id: string;
  title: string;
  document_type: string;
  source_document_id: string;
  start_page: number;
  end_page: number;
  content_summary: string;
  key_facts: unknown[];
  confidence: number;
}

export interface PhysicalRoute {
  document_id: string;
  broad_route:
    | "company_eligibility"
    | "bid_security_exemption"
    | "technical_capability_past_work"
    | "financial_qualification"
    | "declarations_tender_forms"
    | "commercial_price_restricted"
    | "reference_document"
    | "needs_review";
  reason: string;
}
```

`review_status: "review_required"` means AI classification completed but human
approval is still required. Do not label it approved merely because the job is
complete.

## 7. API 3 — Bid evaluation

### Preconditions

- The tender contract is frozen.
- The selected bidder classification is approved.
- `vendor_id` is the normalized value returned by classification, not the
  original display name.

### Request

```http
POST /v1/pipeline/bids/evaluate
Content-Type: application/json
```

```ts
export interface BidEvaluationRequest {
  project_id: string;
  vendor_id: string;
}
```

```json
{
  "project_id": "10044675-09c9-4f0b-9476-99f8fcea54c0",
  "vendor_id": "acme-limited"
}
```

```powershell
curl.exe -X POST "$BASE_URL/v1/pipeline/bids/evaluate" `
  -H "X-API-Key: $API_TOKEN" `
  -H "Idempotency-Key: tender-2026-001-acme-evaluation-v1" `
  -H "Content-Type: application/json" `
  -d '{"project_id":"10044675-09c9-4f0b-9476-99f8fcea54c0","vendor_id":"acme-limited"}'
```

### 202 response

```json
{
  "job_id": "d616f573-1799-4d6d-bcc6-1c1f848011e4",
  "status": "complete",
  "job_url": "http://127.0.0.1:8000/v1/pipeline/jobs/d616f573-1799-4d6d-bcc6-1c1f848011e4",
  "project_id": "10044675-09c9-4f0b-9476-99f8fcea54c0",
  "bid_id": "b878b847-25f7-4df3-bcce-8ac063d173fb",
  "vendor_id": "acme-limited",
  "evaluation_id": "75a4787e-41e0-45eb-8563-bb600b73bf0b"
}
```

An already completed evaluation is returned instead of being recomputed.

### Completed result

```ts
export interface BidEvaluationResult {
  schema_version: "procurement-pipeline-evaluation.v1";
  project_id: string;
  bid_id: string;
  vendor_id: string;
  evaluation_id: string;
  evaluation: PublicEvaluationResult;
  links: PipelineLinks;
}

export interface PublicEvaluationResult {
  schema_version: "procurement-evaluation.v3";
  final_decision: string;
  recommendation: string;
  qualifies: boolean;
  summary: string;
  next_action: string;
  tender: {
    title: string;
    tender_number: string;
    buyer: string;
    procurement_type: string;
    bid_system: string;
    scope: string;
    summary: string;
    mandatory_documents: string[];
    criteria: TenderCriterion[];
  };
  vendor: {
    name: string;
    identifiers: Record<string, string>;
    submitted_documents: string[];
    missing_documents: string[];
  };
  criteria_results: CriterionResult[];
  missing_documents: MissingDocument[];
  manual_review: {
    required: boolean;
    reasons: string[];
  };
  financial_opening: {
    allowed: boolean;
    reason: string;
  };
  validation: {
    schema_valid: boolean;
    strict_mandatory_fail_applied: boolean;
    internal_audit_available: boolean;
  };
}

export interface TenderCriterion {
  no: number;
  category: string;
  requirement: string;
  mandatory: boolean;
  pass_condition: string;
  required_documents: string[];
}

export interface CriterionResult {
  no: number;
  category: string;
  requirement: string;
  status: "passed" | "failed" | "missing" | "needs_manual_review";
  result: string;
  why: string;
  required_documents: string[];
  evidence_documents: string[];
  missing_documents: string[];
}

export interface MissingDocument {
  document_name: string;
  required_for: string;
  impact: string;
}
```

The criterion-level and overall evaluation fields are inside `result.evaluation`;
the outer result carries stable project, bidder, evaluation, and navigation
identifiers.

Frontend presentation should show:

- overall decision, qualification status, and pass/fail/review counts;
- each criterion's requirement, decision, explanation, required proof, bidder
  evidence, and missing evidence;
- manual-review reasons separately from definitive failures;
- financial-opening authorization separately from technical qualification.

## 8. API 4 — Poll any job

```http
GET /v1/pipeline/jobs/{job_id}
```

```powershell
curl.exe "$BASE_URL/v1/pipeline/jobs/$JOB_ID" `
  -H "X-API-Key: $API_TOKEN"
```

Examples:

```json
{
  "job_id": "JOB_UUID",
  "stage": "intake",
  "status": "running",
  "error_code": null,
  "error": null,
  "result": null
}
```

```json
{
  "job_id": "JOB_UUID",
  "stage": "intake",
  "status": "failed",
  "error_code": "intake_failed",
  "error": "frontend-safe failure detail",
  "result": null
}
```

Polling policy:

- Start polling after receiving `202`.
- Poll every 2–5 seconds.
- Stop on `complete` or `failed`.
- Apply exponential backoff only for network errors, `429`, or `503`.
- Respect `Retry-After` when present.
- Do not resubmit a queued/running operation; continue polling its `job_id`.
- After a page reload, resume from persisted `job_id`; jobs are stored in
   PostgreSQL and recover after worker restart.

Suggested frontend pseudocode:

```ts
async function waitForJob<T>(jobUrl: string): Promise<T> {
  for (;;) {
    const response = await apiFetch(jobUrl);
    const job = (await response.json()) as PipelineJob<T>;
    if (job.status === "complete") return job.result as T;
    if (job.status === "failed") {
      throw new Error(`${job.error_code ?? "job_failed"}: ${job.error ?? "Job failed"}`);
    }
    await new Promise(resolve => setTimeout(resolve, 3000));
  }
}
```

Use an `AbortController` when leaving the page, but retain the `job_id` so
polling can resume later.

## 9. Error contract and frontend behavior

All synchronous errors use:

```json
{
  "error": {
    "code": "409",
    "message": "approve bidder classification before evaluation",
    "details": {}
  }
}
```

Validation example:

```json
{
  "error": {
    "code": "422",
    "message": "request validation failed",
    "details": {
      "errors": [
        {
          "field": "body.project_id",
          "message": "Field required",
          "type": "missing"
        }
      ]
    }
  }
}
```

| HTTP | Meaning | Frontend action |
|---:|---|---|
| `400` | Invalid filename, schema, empty upload, or malformed input | Show the message beside the affected form; do not retry unchanged |
| `401` | Missing/expired/invalid credential | Clear session and return to login |
| `403` | Authenticated but role is insufficient | Show access denied; do not retry |
| `404` | Project, bidder, or job does not exist for this tenant | Remove stale local reference or return to project list |
| `409` | Workflow precondition, duplicate entity, immutable approval, or idempotency conflict | Show `message`; direct user to required review/approval stage |
| `413` | Upload, ZIP member, schema, or PDF page limit exceeded | Ask user to reduce/split the package |
| `415` | Unsupported or malformed file/container | Show accepted file types |
| `422` | Request field validation failed | Map `details.errors[].field` to form fields |
| `429` | Rate limit exceeded | Respect `Retry-After`; retry automatically |
| `500` | Unexpected server error | Show generic failure and retain `X-Request-ID`/error ID for support |
| `503` | Provider/configuration/storage/database temporarily unavailable | Keep state and offer retry |

Network timeout is not proof that a start request failed. Retry with the same
`Idempotency-Key`; the backend returns the original job.

## 10. Human-review and approval dependencies

These are governance routes already used by the workbench. They are not extra
AI pipelines, but a frontend implementing the full review journey needs them.

Tender review:

```text
GET  /v1/workbench/projects/{project_id}/ledger
POST /v1/workbench/projects/{project_id}/ledger/corrections
GET  /v1/workbench/projects/{project_id}/contract/draft
POST /v1/workbench/projects/{project_id}/contract/draft
POST /v1/workbench/projects/{project_id}/contract/freeze
```

Freeze body:

```json
{
  "draft": {},
  "reviewed_by": "Reviewer name",
  "reviewed_at": "2026-07-28T14:30:00Z",
  "continue_with_limitations": false
}
```

Bid classification review:

```text
GET  /v1/workbench/projects/{project_id}/bids/{vendor_id}/classification
POST /v1/workbench/projects/{project_id}/bids/{vendor_id}/classification/correct
POST /v1/workbench/projects/{project_id}/bids/{vendor_id}/classification/approve
```

Approval body:

```json
{
  "reviewed_by": "Reviewer name",
  "reviewed_at": "2026-07-28T14:30:00Z",
  "reason": "Bidder document classification approved."
}
```

Approved tender contracts and bidder classifications are immutable.

## 11. Frontend state model

Persist at least:

```ts
interface ProcurementWorkflowState {
  project_id?: string;
  tender_version_id?: string;
  tender_job_id?: string;
  vendor_id?: string;
  bid_id?: string;
  classification_job_id?: string;
  evaluation_id?: string;
  evaluation_job_id?: string;
}
```

Recommended UI states:

```text
idle
uploading
queued
processing
review_required
approved
complete
failed
```

Never infer `failed` from a browser refresh or lost in-memory state. Recover
the persisted job ID and poll the backend.

## 12. Verified live test record

Test date: 2026-07-28  
Backend: local Windows service, PostgreSQL worker queue  
Paid-provider minimization: one-field tender schema and small existing text
fixtures

| Contract | Test | Result |
|---|---|---|
| Tender understanding start | Real multipart upload through live API | `202`, queued |
| Tender understanding worker | Real ingestion and provider pipeline | `complete`, full understanding returned |
| Bid classification start | Real multipart upload through live API | `202`, queued |
| Bid classification worker | Real intake/classification pipeline | `complete`, proposal and summaries returned |
| Bid evaluation start | Real approved bidder through live API | `202` |
| Bid evaluation result | Real previously completed evaluation | `complete`, criterion-level result returned |
| Shared polling | Tested for all three job stages | Passed |
| Idempotent retry | Repeated tender and classification uploads with same keys | Original job IDs returned; no duplicate jobs |
| Authentication | Missing credential | Stable `401` |
| Invalid field schema | Invalid schema upload | Stable `400` |
| Unsupported file | `.exe` upload | Stable `415` |
| Missing project | Unknown UUID | Stable `404` |
| Approval gate | Evaluation before classification approval | Stable `409` |
| Request validation | Missing JSON fields | Stable `422` envelope |
| Restart/readiness | API and worker restart | `/health=ok`, `/ready=ready` |

No reranker or verifier endpoint is exposed. Provider/model selection remains
a backend environment concern and does not alter this frontend contract.
