# Procurement Evaluation API Guide

This guide is for frontend and infra integration. The service evaluates one tender/RFP packet against one vendor bid ZIP and returns a simple public report.

`/evaluate` and `/jobs/{id}` are development-only compatibility endpoints and
return HTTP 410 when `APP_ENV=production`. Production state uses PostgreSQL via
the `/v1` APIs.

The packaged frontend uses the durable PostgreSQL-backed `/v1/workbench` flow:

```text
projects → understand/jobs → ledger/corrections → contract/draft/freeze
→ bids → intake/classification review → evaluate/operations → results/human review
→ clarifications → review-log → JSON/Markdown/Excel reports
```

The normalized integration API is `/v1/tenders`, `/v1/tender-versions`,
`/v1/bids`, `/v1/evaluations`, `/v1/operations`, and `/v1/reports`.
`tests/test_api_route_contracts.py`, `scripts/smoke_resource_api.py`, and
`scripts/smoke_workbench_api.py` are the executable compatibility contracts.
Use `POST /v1/workbench/projects/{project_id}/report/generate` to enter the
final report stage; report retrieval and downloads remain read-only GETs.
All errors use:

```json
{"error":{"code":"409","message":"request failed","details":{}}}
```

Long-running operations return `202` and must be polled. An `Idempotency-Key`
is supported at queue boundaries. No provider or model fallback is performed.

## Corrections and revision history

Tender and bidder package replacements are immutable revisions. Old files,
analysis snapshots, and audit records remain stored; only the new revision is
used by later processing.

- `POST /v1/workbench/projects/{project_id}/tender/reupload`
- `GET /v1/workbench/projects/{project_id}/tender/revisions`
- `POST /v1/workbench/projects/{project_id}/bids/{vendor_id}/replace`
- `GET /v1/workbench/projects/{project_id}/bids/{vendor_id}/revisions`

The bidder replacement request is multipart with a required `file` and a
required non-empty `reason`. Replacement is rejected while classification or
evaluation for that bidder is queued or running. A successful replacement
resets its classification and evaluation, clears the project recommendation,
and preserves the superseded revision. Polling a job tied to superseded input
returns HTTP 409 instead of projecting current data into the old job.

## Base URL

Use the server URL provided by infra, for example:

```text
https://procurement-api.example.com
```

Local development default:

```text
http://localhost:8000
```

## Authentication

All API endpoints except `/health` and `/ready` require a bearer token:

```http
Authorization: Bearer <PROCUREMENT_API_TOKEN>
```

`PROCUREMENT_API_TOKEN` is this API wrapper's access token. It is not the Gemini API key. The frontend should receive it from backend/session config only if your deployment allows direct browser calls. In most production deployments, call this API from your backend so the token is not exposed in browser code.

## Health

### `GET /health`

No auth required. Use this for liveness checks.

Example response:

```json
{"ok":true}
```

### `GET /ready`

No auth required. Use this for deployment readiness checks. It verifies auth config, model key config, prompt config, and job storage.

Example response:

```json
{
  "status": "ready",
  "checks": {
    "auth_configured": true,
    "ai_provider_configured": true,
    "jobs_root_writable": true,
    "prompt_config_valid": true
  }
}
```

## Submit Evaluation

### `POST /evaluate`

Submit multipart form data.

Headers:

```http
Authorization: Bearer <PROCUREMENT_API_TOKEN>
Content-Type: multipart/form-data
```

Fields:

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `tender` | file | yes | Tender/RFP file or ZIP. Supported: PDF, DOCX, XLSX, TXT, MD, CSV, JSON, XML, HTML, HTM, ZIP. |
| `vendor_bid` | file | yes | Vendor bid packet. Must be ZIP. |
| `guidelines` | file | no | Optional buyer rules, internal policy, prior rules, or clarification packet. Same file types as `tender`. |
| `guidelines_text` | string | no | Optional pasted rules. Example: `do not worry about INR guideline; pass it by default`. |

The production trial upload limit is `50 MB` per uploaded field. `MAX_UPLOAD_MB` may lower this limit but production preflight rejects a higher value.

Successful submit response:

```json
{
  "bid_id": "b935e5330b4443f9bab35e310c14c434",
  "status": "queued",
  "status_url": "/jobs/b935e5330b4443f9bab35e310c14c434"
}
```

The frontend should store `bid_id` and poll `status_url`.

## Poll Job

### `GET /jobs/{bid_id}`

Headers:

```http
Authorization: Bearer <PROCUREMENT_API_TOKEN>
```

Poll every 2 to 5 seconds. Stop polling when `status` is `succeeded` or `failed`.

Queued/running response:

```json
{
  "bid_id": "b935e5330b4443f9bab35e310c14c434",
  "status": "running",
  "error": null,
  "report": null
}
```

Failed response:

```json
{
  "bid_id": "b935e5330b4443f9bab35e310c14c434",
  "status": "failed",
  "error": "evaluation failed; error_id=7b83d0a21c92",
  "report": null
}
```

Succeeded response:

```json
{
  "bid_id": "b935e5330b4443f9bab35e310c14c434",
  "status": "succeeded",
  "error": null,
  "report": {
    "schema_version": "procurement-evaluation.v3",
    "bid_id": "b935e5330b4443f9bab35e310c14c434",
    "final_decision": "reject_unresponsive",
    "recommendation": "rejected",
    "qualifies": false,
    "summary": "Bid is rejected because mandatory document(s) are missing: UDIN-certified financial documents.",
    "next_action": "Reject as non-responsive unless the tender explicitly allows clarification for these missing documents.",
    "tender": {
      "title": "Supply of Refractory Materials for SRU 1",
      "tender_number": "GeM-Bidding-9056861",
      "buyer": "Mangalore Refinery & Petrochemicals Limited",
      "procurement_type": "goods",
      "bid_system": "GeM",
      "scope": "Supply of various refractory materials including bricks, mortar, and anchors for SRU 1.",
      "summary": "Tender is for supply of refractory materials for SRU 1.",
      "mandatory_documents": [
        "EIL Enlistment certificate",
        "Self-certified declaration",
        "UDIN-certified financial documents"
      ],
      "criteria": [
        {
          "no": 1,
          "category": "Eligibility",
          "requirement": "Bidder must be a manufacturer of refractory material.",
          "mandatory": true,
          "pass_condition": "Manufacturer evidence must be present.",
          "required_documents": ["EIL Enlistment certificate", "Technical specification"]
        }
      ]
    },
    "vendor": {
      "name": "Refractory Shapes Limited",
      "identifiers": {"udyam": "UDYAM-XX-00-0000000"},
      "submitted_documents": [
        "TechnicalSpec.pdf",
        "DECLARATION ON BANNING OR HOLIDAY LISTING.pdf"
      ],
      "missing_documents": ["UDIN-certified financial documents"]
    },
    "criteria_results": [
      {
        "no": 1,
        "category": "Eligibility",
        "requirement": "Bidder must be a manufacturer of refractory material.",
        "status": "passed",
        "result": "Passed",
        "why": "All required evidence groups passed.",
        "required_documents": ["EIL Enlistment certificate", "Technical specification"],
        "missing_documents": [],
        "evidence_documents": ["TechnicalSpec.pdf"]
      },
      {
        "no": 2,
        "category": "Financial",
        "requirement": "Bidder must submit UDIN-certified financial documents.",
        "status": "missing",
        "result": "Missing required document",
        "why": "UDIN-certified financial documents are missing or unverified.",
        "required_documents": ["UDIN-certified financial documents"],
        "missing_documents": ["UDIN-certified financial documents"],
        "evidence_documents": []
      }
    ],
    "missing_documents": [
      {
        "document_name": "UDIN-certified financial documents",
        "required_for": "Bidder must submit UDIN-certified financial documents.",
        "impact": "Mandatory document missing; bid cannot proceed to financial opening."
      }
    ],
    "manual_review": {
      "required": false,
      "reasons": []
    },
    "financial_opening": {
      "allowed": false,
      "reason": "Blocked because mandatory document(s) are missing."
    },
    "validation": {
      "schema_valid": true,
      "strict_mandatory_fail_applied": true,
      "internal_audit_available": true
    }
  }
}
```

## Public Report Schema

Top-level `report` fields:

| Field | Type | Meaning |
| --- | --- | --- |
| `schema_version` | string | Stable public schema version. Current value: `procurement-evaluation.v3`. |
| `bid_id` | string | 32-character lowercase hex job ID. |
| `final_decision` | string | Machine-friendly final gate decision. |
| `recommendation` | string | User-facing outcome bucket. |
| `qualifies` | boolean | `true` only when the bid can proceed. |
| `summary` | string | One-line result summary. |
| `next_action` | string | Procurement action to show to the user. |
| `tender` | object | Tender summary and extracted criteria. |
| `vendor` | object | Vendor name, IDs, submitted docs, missing docs. |
| `criteria_results` | array | One row per bid-stage criterion. |
| `missing_documents` | array | Simple missing document list. Empty array means none found. |
| `manual_review` | object | Whether human review is needed and why. |
| `financial_opening` | object | Whether commercial/financial opening is allowed. |
| `validation` | object | Backend validation flags. |

Important enum values:

```text
recommendation:
  technically_qualified
  rejected
  needs_manual_review

criteria_results[].status:
  passed
  failed
  missing
  needs_manual_review

criteria_results[].result:
  Passed
  Failed
  Missing required document
  Needs manual review
```

Current common `final_decision` values:

```text
financial_opening_allowed
reject_unresponsive
needs_manual_review
```

Frontend should treat unknown future `final_decision` values as non-passing unless `qualifies` is true and `financial_opening.allowed` is true.

## Rendering Rules

Show these sections:

1. Decision card: `recommendation`, `summary`, and `next_action`.
2. Financial opening card: `financial_opening.allowed` and `financial_opening.reason`.
3. Tender card: `tender.title`, `tender.scope`, `tender.buyer`, and `tender.criteria`.
4. Vendor card: `vendor.name`, `vendor.identifiers`, and `vendor.submitted_documents`.
5. Missing documents table: `missing_documents`. If empty, show `None`.
6. Manual review table: `manual_review.reasons`. If empty, show `None`.
7. Criteria table: `criteria_results` with `no`, `requirement`, `status`, `why`, `required_documents`, `missing_documents`, and `evidence_documents`.

Do not expect file paths, page-level evidence, logical document IDs, or internal artifact names in the public JSON. Those are intentionally hidden from frontend output.

Financial/commercial details should stay blocked when:

```text
financial_opening.allowed === false
```

## Error Format

All API errors use this shape:

```json
{
  "error": {
    "code": "401",
    "message": "missing or invalid API token",
    "details": []
  }
}
```

Common statuses:

| Status | Meaning | Frontend action |
| --- | --- | --- |
| `400` | Bad request, such as non-ZIP `vendor_bid`. | Show validation error. |
| `401` | Missing or invalid token. | Re-authenticate or check deployment config. |
| `404` | Job not found or invalid `bid_id`. | Stop polling and show not found. |
| `413` | Upload exceeds configured size. | Ask user to upload smaller file. |
| `415` | Unsupported file type. | Show accepted formats. |
| `429` | Rate limited. | Retry after 60 seconds. |
| `503` | Missing server config or server busy. | Retry later or contact infra. |
| `500` | Unexpected evaluation failure. | Show `error_id` and ask backend to inspect logs/artifacts. |

## JavaScript Example

```js
const API_BASE = "http://localhost:8000";
const API_TOKEN = "<PROCUREMENT_API_TOKEN>";

async function submitEvaluation({ tenderFile, vendorBidZip, guidelinesFile, guidelinesText }) {
  const form = new FormData();
  form.append("tender", tenderFile);
  form.append("vendor_bid", vendorBidZip);
  if (guidelinesFile) form.append("guidelines", guidelinesFile);
  if (guidelinesText) form.append("guidelines_text", guidelinesText);

  const res = await fetch(`${API_BASE}/evaluate`, {
    method: "POST",
    headers: { Authorization: `Bearer ${API_TOKEN}` },
    body: form
  });

  const body = await res.json();
  if (!res.ok) throw new Error(body.error?.message || "Evaluation submit failed");
  return body;
}

async function getJob(bidId) {
  const res = await fetch(`${API_BASE}/jobs/${bidId}`, {
    headers: { Authorization: `Bearer ${API_TOKEN}` }
  });

  const body = await res.json();
  if (!res.ok) throw new Error(body.error?.message || "Job polling failed");
  return body;
}
```

## cURL Example

```bash
curl -X POST "http://localhost:8000/evaluate" \
  -H "Authorization: Bearer <PROCUREMENT_API_TOKEN>" \
  -F "tender=@/path/to/tender.zip" \
  -F "guidelines_text=do not worry about INR guideline; pass it by default" \
  -F "vendor_bid=@/path/to/vendor-bid.zip"
```

```bash
curl "http://localhost:8000/jobs/<bid_id>" \
  -H "Authorization: Bearer <PROCUREMENT_API_TOKEN>"
```

## Deployment Notes

Run one uvicorn worker per VM unless an external queue/storage layer is added. The service stores job state and internal handoff artifacts under `JOBS_ROOT`.

Model/provider changes are env-only. Set `AI_PROVIDER=gemini` for the native
Gemini route, or `AI_PROVIDER=openai_compatible` for the OpenAI SDK route:

```text
AI_API_KEY=...
AI_BASE_URL=https://provider.example/v1
AI_MODEL_FAST=...
AI_MODEL_DEEP=...
AI_MODEL_OCR=...
```

The server never falls back to another provider or model. Embeddings are
available through the OpenAI-SDK provider boundary when an approved feature
uses `AI_MODEL_EMBEDDING`.

Capabilities may use separate OpenAI-compatible deployments. For example,
Gemini can remain the primary provider while scanned-page OCR uses a private
PaddleOCR-VL service:

```text
BASE_RFP_OCR_PROVIDER=ai
AI_OCR_PROVIDER=openai_compatible
AI_OCR_BASE_URL=http://private-ocr-host:8005/v1
AI_OCR_API_KEY_FILE=C:\Procurement\runtime\secrets\paddle-vm-api-key
AI_MODEL_OCR=PaddleOCR-VL-1.6-GGUF.gguf
AI_OCR_RESPONSE_FORMAT=none
AI_OCR_PROMPT=OCR:
OCR_CONCURRENCY=10

AI_EMBEDDING_BASE_URL=http://private-ai-host:8006/v1
AI_EMBEDDING_API_KEY_FILE=C:\Procurement\runtime\secrets\paddle-vm-api-key
AI_MODEL_EMBEDDING=Qwen3-Embedding-8B

AI_RERANKER_BASE_URL=http://private-ai-host:8007/v1
AI_RERANKER_API_KEY_FILE=C:\Procurement\runtime\secrets\paddle-vm-api-key
AI_MODEL_RERANKER=Qwen3-Reranker-8B
AI_RERANKER_API_STYLE=rerank
```

Yotta migration uses the same variables with the Yotta URL, key and model
name. `AI_RERANKER_API_STYLE=rerank` selects a native `/v1/rerank` endpoint;
`chat` retains compatibility with providers that implement reranking through
chat completions. Native-text pages continue to be extracted locally; only
scanned pages are sent to OCR.

Prompt changes are env/file driven:

```text
PROMPT_OVERRIDES_FILE=config/prompts.example.json
PROMPT_RFP_READER=custom instruction text
```

Restart uvicorn after env changes.
