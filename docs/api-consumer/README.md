# Procurement Pipeline API v1

Consumer integration guide for tender understanding, bidder-document classification, bid evaluation, evidence previews, approvals, and reports.

Contract files:

- `openapi.json` is the machine-readable source of truth for paths, parameters, request bodies, response bodies, and schemas.
- `Procurement_Pipeline_API.postman_collection.json` is an importable Postman collection.
- `latest_tender_understanding_fixed_v1.json` is a portable 31-field fixed-schema response example with placeholder identifiers and URLs.

## 1. Base URL and authentication

Use the deployment URL supplied by the operator. Local default:

```text
http://127.0.0.1:8000
```

Every documented request, including PDF and page-image requests, requires one of:

```http
X-API-Key: <token>
```

or:

```http
Authorization: Bearer <token-or-JWT>
```

The server selects API-token or JWT/JWKS authentication through its environment. Consumers do not send usernames and passwords to these API routes. Browser login is a separate local-workbench feature.

Never put a token in a query string, URL, source repository, Postman export, screenshot, or log.

## 2. The actual workflow

The three main APIs are asynchronous. A successful start returns HTTP `202` and a `job_id`; it does not mean processing is complete.

```text
Upload tender
  -> poll understanding job
  -> review/correct field ledger
  -> create and review contract draft
  -> approver freezes contract
  -> upload one bidder package
  -> poll classification job
  -> review/correct document classification
  -> approver approves classification
  -> start evaluation
  -> poll evaluation job
  -> read results and generate/download reports
```

The approval calls are mandatory workflow gates, not optional frontend behavior:

- Bid classification returns `409` until the tender version is approved and its contract is frozen.
- Bid evaluation returns `409` until both the tender contract and selected bidder classification are approved.
- Approved tender findings and approved bidder classification are immutable.

Repeat classification and evaluation independently for every bidder. A project can hold multiple bidders, addressed by `vendor_id`.

## 3. Common request rules

### Idempotency

Send a unique stable header on every start request:

```http
Idempotency-Key: <your-system>:<operation>:<stable-business-id>:v1
```

Reusing a key with the same logical request returns the original job. Reusing it for a different request returns `409`.

The server hashes the logical identifiers, uploaded file bytes, and relevant
request metadata. Reusing a key with changed content returns `409`.

### Upload format

Tender and bidder starts use `multipart/form-data`. Do not manually set the multipart boundary; let the HTTP library create it.

Supported package/document extensions are:

```text
.zip .pdf .doc .docx .xls .xlsx .csv .txt .md .json .xml .html .htm
```

Uploads are subject to server-configured size, extracted-file-count, extracted-size, ZIP-entry-size, ZIP-compression-ratio, path-traversal, encryption, content-type, and page-count controls. The exact deployment limits are operational configuration and may be stricter than this contract.

### Identifiers

- `project_id`, `tender_id`, `tender_version_id`, `bid_id`, `evaluation_id`, and `job_id` are opaque strings. Store them exactly as returned.
- `vendor_id` is the normalized bidder key returned by classification. Do not derive it in client code from `vendor_name`.
- `project_key` is supplied by the caller, normalized by the server, and must be unique within the authenticated tenant.

### Timestamps

Review timestamps use ISO 8601 UTC:

```text
2026-07-28T12:00:00Z
```

## 4. Main API 1 - Tender understanding

### Start

```http
POST /v1/pipeline/tenders/understand
Content-Type: multipart/form-data
```

Form fields:

| Field | Type | Required | Meaning |
|---|---|---:|---|
| `file` | binary | yes | Tender file or package. |
| `project_key` | string | no | Caller-owned unique key. A unique key is generated when omitted. |
| `title` | string | no | Human-readable project title. |
| `schema_json` | JSON encoded as text | no | Custom `rfp-field-schema.v1`. The configured default schema is used when omitted. |

Minimal cURL:

```bash
curl -X POST "$BASE_URL/v1/pipeline/tenders/understand" \
  -H "X-API-Key: $API_TOKEN" \
  -H "Idempotency-Key: erp:understanding:TENDER-2026-001:v1" \
  -F "file=@Tender.zip" \
  -F "project_key=tender-2026-001" \
  -F "title=Tender 2026-001"
```

Custom schema shape:

```json
{
  "schema_version": "rfp-field-schema.v1",
  "schema_id": "customer-tender-v1",
  "sections": [
    {
      "key": "identity",
      "label": "Tender identity",
      "fields": [
        {
          "key": "tender_number",
          "label": "Tender number",
          "description": "Authoritative tender or bid number"
        }
      ]
    }
  ]
}
```

Every section must have a non-empty `key` and at least one field. Field keys must be non-empty and unique across the schema. Invalid JSON returns `400`; schema validation failures return `400` or `422`; schema text above 2 MB returns `413`.

### Accepted response

```json
{
  "api_version": "procurement-pipeline.v1",
  "job_id": "7b170f93-768d-4f95-9272-9c7ace5eb0c1",
  "status": "queued",
  "job_url": "http://127.0.0.1:8000/v1/pipeline/jobs/7b170f93-768d-4f95-9272-9c7ace5eb0c1",
  "project_id": "85bf1cf5-39f9-4b74-b8e2-32c7fab16603",
  "tender_id": "b991f3ae-e206-433d-88d3-98df0fcb3713",
  "tender_version_id": "f5f2381b-d869-4358-8346-bd05ac18b18c"
}
```

### Completed result

The job returns `result_type: "tender_understanding"` and:

- `schema_version`: `procurement-understanding.v1`.
- `project_id`, `tender_id`, `tender_version_id`.
- `review_status`: `review_required` or `approved`.
- `field_schema`: schema ID, field count, section count, and section labels.
- `ledger.ledger_status`: `complete` or `needs_review`.
- `ledger.editable`: whether the consumer should present review controls.
- `ledger.tender_summary.text`, `.source`, and `.key_facts`.
- `ledger.entries[]`: one reviewed-field record per schema field.
- `documents[]`: source files with authenticated content and page-preview URLs.
- `links`: exact ledger, contract-draft, and contract-freeze URLs.

Each `ledger.entries[]` item contains:

| Field | Type | Meaning |
|---|---|---|
| `key` | string | Stable field key from the submitted/default schema. |
| `label` | string | Display label. |
| `requirement_level` | string | Current requirement classification. |
| `status` | string | Extraction/review status such as `found`, `not_found`, `not_applicable`, `unresolved`, `ambiguous`, or `need_more_evidence`. |
| `value` | JSON value or null | Extracted value; preserve its JSON type. |
| `conditions` | JSON array | Conditions attached to the value. |
| `exceptions` | JSON array | Exceptions or exclusions. |
| `required_documents` | JSON array | Documents expected from a bidder. |
| `evidence` | array | Exact tender provenance. |
| `verified` | boolean | Whether the current value passed the review/grounding state represented by the record. |
| `review_source` | string | Usually `ai_extracted` or `human_corrected`. |
| `blocking_grounding_issues` | string array | Reasons that prevent clean approval. |
| `notes` | string | Additional extraction/review note. |

Each evidence item contains source/document/page identifiers, a quote, and—when resolvable—`document_url`, `pdf_url`, `page_image_url`, and `page_url`.

## 5. Tender review and approval

Read the public ledger:

```http
GET /v1/workbench/projects/{project_id}/ledger
```

Correct one field:

```http
POST /v1/workbench/projects/{project_id}/ledger/corrections
Content-Type: application/json
```

```json
{
  "field_key": "tender_number",
  "status": "found",
  "value": "BHEL/PROC/2026/001",
  "conditions": [],
  "exceptions": [],
  "required_documents": [],
  "reason": "Confirmed against the signed tender notice on page 1.",
  "reviewed_by": "Reviewer name",
  "reviewed_at": "2026-07-28T12:00:00Z"
}
```

Allowed correction statuses are `found`, `not_found`, `not_applicable`, and `unresolved`.

Create the contract draft:

```http
POST /v1/workbench/projects/{project_id}/contract/draft
```

Read it without mutation:

```http
GET /v1/workbench/projects/{project_id}/contract/draft
```

Freeze exactly the reviewed draft returned by the server:

```http
POST /v1/workbench/projects/{project_id}/contract/freeze
Content-Type: application/json
```

```json
{
  "draft": {
    "draft_id": "",
    "criteria": [
      {
        "criterion_id": "FIELD-turnover",
        "title": "Minimum average annual turnover",
        "family": "financial",
        "rule": {
          "rule_family": "financial",
          "requirement": "Minimum average annual turnover",
          "mandatory": true,
          "failure_effect": "disqualify",
          "pass_rule": "all_required_evidence_must_pass",
          "required_documents": ["Audited financial statements"]
        },
        "rfp_source_refs": [
          {
            "document_id": "source-1",
            "page": 42,
            "quote": "Minimum average annual turnover ..."
          }
        ],
        "missing_fields": [],
        "needs_human_input": false,
        "depends_on": [],
        "notes": ""
      }
    ],
    "unautomated_requirements": [],
    "planning_notes": []
  },
  "reviewed_by": "Approver name",
  "reviewed_at": "2026-07-28T12:05:00Z",
  "continue_with_limitations": false
}
```

If any criterion has `missing_fields`, freezing returns `409` unless an authorized approver deliberately sets `continue_with_limitations: true`. In that case unresolved criteria are recorded and excluded from the frozen evaluation contract.

## 6. Main API 2 - Bid classification

### Preconditions

The project must exist and its tender contract must be frozen.

### Start

```http
POST /v1/pipeline/bids/classify
Content-Type: multipart/form-data
```

| Field | Type | Required | Meaning |
|---|---|---:|---|
| `file` | binary | yes | One bidder package. |
| `project_id` | string | yes | Project returned by tender understanding. |
| `vendor_name` | string | yes | Human-readable bidder name. |

```bash
curl -X POST "$BASE_URL/v1/pipeline/bids/classify" \
  -H "X-API-Key: $API_TOKEN" \
  -H "Idempotency-Key: erp:classification:TENDER-2026-001:bidder-a:v1" \
  -F "file=@Bidder-A.zip" \
  -F "project_id=$PROJECT_ID" \
  -F "vendor_name=Bidder A Private Limited"
```

Store the returned `vendor_id`. It is required for classification review and evaluation.

### Completed result

The job returns `result_type: "bid_classification"` and:

- `schema_version`: `procurement-classification.v1`.
- `project_id`, `bid_id`, `vendor_id`, `vendor_name`.
- `review_status`: `review_required` or `approved`.
- `document_types[]`: document types present in the proposal.
- `documents[]`: physical source documents with page counts and authenticated preview URLs.
- `logical_documents[]`: AI-proposed or human-reviewed document partitions.
- `physical_routes[]`: route for each physical document, including commercial quarantine.
- `links`: exact read, correction, and approval URLs.

Each logical document contains:

```json
{
  "logical_document_id": "logical-1-source-1",
  "title": "Udyam Registration Certificate",
  "document_type": "mse_udyam_or_startup_exemption",
  "source_document_id": "source-1",
  "start_page": 1,
  "end_page": 2,
  "content_summary": "Udyam registration issued to the bidder.",
  "key_facts": ["Registration number", "Enterprise name"],
  "confidence": 0.94
}
```

Read classification:

```http
GET /v1/workbench/projects/{project_id}/bids/{vendor_id}/classification
```

Correct classification:

```http
POST /v1/workbench/projects/{project_id}/bids/{vendor_id}/classification/correct
```

```json
{
  "logical_documents": [
    {
      "logical_document_id": "logical-1-source-1",
      "title": "Udyam Registration Certificate",
      "document_type": "mse_udyam_or_startup_exemption",
      "source_document_id": "source-1",
      "start_page": 1,
      "end_page": 2,
      "content_summary": "Udyam registration issued to the bidder.",
      "key_facts": ["Registration number", "Enterprise name"],
      "confidence": 0.94
    }
  ],
  "physical_routes": [
    {
      "document_id": "source-1",
      "broad_route": "company_eligibility",
      "reason": "Contains bidder registration evidence."
    }
  ],
  "reason": "Reviewed page boundaries and document role.",
  "reviewed_by": "Reviewer name",
  "reviewed_at": "2026-07-28T12:15:00Z"
}
```

Allowed broad routes are `company_eligibility`, `bid_security_exemption`, `technical_capability_past_work`, `financial_qualification`, `declarations_tender_forms`, `commercial_price_restricted`, `reference_document`, and `needs_review`. A `needs_review` route requires a reason. Page ranges must stay within the source document.

Approve classification:

```http
POST /v1/workbench/projects/{project_id}/bids/{vendor_id}/classification/approve
```

```json
{
  "reviewed_by": "Approver name",
  "reviewed_at": "2026-07-28T12:20:00Z",
  "reason": "Bidder document classification reviewed and approved."
}
```

## 7. Main API 3 - Bid evaluation

### Preconditions

- Tender version is approved.
- Tender contract has frozen criteria.
- Selected bidder classification is approved.

### Start

```http
POST /v1/pipeline/bids/evaluate
Content-Type: application/json
```

```json
{
  "project_id": "85bf1cf5-39f9-4b74-b8e2-32c7fab16603",
  "vendor_id": "bidder-a-private-limited"
}
```

### Completed result

The job returns `result_type: "bid_evaluation"` and:

- Project, bid, vendor, and evaluation identifiers.
- `evaluation`, validated as `procurement-evaluation.v3`.
- Links to the multi-bid results and report endpoints.

The evaluation object contains:

| Field | Type | Meaning |
|---|---|---|
| `bid_id` | string | Evaluated bid. |
| `final_decision` | string | Machine evaluation decision. |
| `recommendation` | string | Recommended next handling step. |
| `qualifies` | boolean | Machine qualification outcome under the frozen criteria. |
| `summary` | string | Bid-level explanation. |
| `next_action` | string | Recommended workflow action. |
| `tender` | object | Tender summary and criteria. |
| `vendor` | object | Bidder name, identifiers, submitted documents, and missing documents. |
| `criteria_results[]` | array | Per-criterion decision and evidence-document names. |
| `missing_documents[]` | array | Missing proof and impact. |
| `manual_review` | object | Whether manual review is required and why. |
| `financial_opening` | object | Whether price opening is allowed and why. |
| `validation` | object | Schema, mandatory-fail, and internal-audit validation flags. |

Criterion status is one of `passed`, `failed`, `missing`, or `needs_manual_review`.

The public evaluation is evidence-backed machine output for human decision-making. Final qualification, rejection, shortlisting, clarification, and price opening remain authorized committee actions.

### Optional BHEL evaluation view

The existing fixed evaluation response is unchanged. When
`BID_EVALUATION_PROFILE=bhel_purchase_materials`, consumers that need the
BHEL Noting-derived view may make this additional read:

```http
GET /v1/pipeline/fixed-schema/projects/{project_id}/bids/{vendor_id}/evaluation/bhel
```

The response uses `schema_version: "bhel-bid-evaluation.v1"` and contains the
frozen-submission policy, source-grounded BHEL checks, bidder evidence,
provenance, internal controls kept separate from bidder qualification, and a
bidder summary. Results are one of `met`, `not_met`,
`insufficient_evidence`, `needs_review`, `not_applicable`, or
`not_evaluated`. A `409 BHEL_EVALUATION_NOT_READY` means evaluation has not
completed successfully. A `409 BHEL_PROFILE_NOT_ENABLED` means the optional
profile was not enabled for that run.

## 8. Polling contract

```http
GET /v1/pipeline/jobs/{job_id}
```

Possible `status` values:

- `queued`: accepted and waiting for a worker.
- `running`: worker is processing.
- `complete`: terminal success; `result_type` and `result` are present.
- `failed`: terminal failure; `error_code` and `error` describe the failure.

Pending:

```json
{
  "api_version": "procurement-pipeline.v1",
  "job_id": "7b170f93-768d-4f95-9272-9c7ace5eb0c1",
  "stage": "understanding",
  "status": "running",
  "result_type": null,
  "error_code": null,
  "error": null,
  "result": null
}
```

Failed:

```json
{
  "api_version": "procurement-pipeline.v1",
  "job_id": "7b170f93-768d-4f95-9272-9c7ace5eb0c1",
  "stage": "understanding",
  "status": "failed",
  "result_type": null,
  "error_code": "provider_rate_limited",
  "error": "Configured AI provider rate limit was exceeded.",
  "result": null
}
```

Recommended polling:

1. Poll immediately once, then every 2 seconds.
2. Add small random jitter when many clients poll.
3. Stop only on `complete`, `failed`, or the caller's own timeout.
4. Treat a caller timeout as unknown state, not job failure; keep the `job_id` and resume polling later.
5. After a network error or process restart, poll the same `job_id`. Do not create a replacement job unless the server returns `404`.

There is no public job-cancellation endpoint in v1. Do not imply cancellation in a client UI.

## 9. Evidence, PDF, and page previews

Use only URLs returned in a result or its `links`; do not construct storage paths.

Tender sources:

```http
GET /v1/workbench/projects/{project_id}/documents
GET /v1/workbench/projects/{project_id}/sources/{source_id}/content
GET /v1/workbench/projects/{project_id}/sources/{source_id}/pages/{page_number}.png
```

Bidder sources:

```http
GET /v1/workbench/projects/{project_id}/bids/{vendor_id}/review
GET /v1/workbench/projects/{project_id}/bids/{vendor_id}/sources/{source_id}/content
GET /v1/workbench/projects/{project_id}/bids/{vendor_id}/sources/{source_id}/pages/{page_number}.png
```

Original stored PDFs:

```http
GET /v1/workbench/documents/{document_id}/content
GET /v1/workbench/documents/{document_id}/pages/{page_number}.png
```

Rules:

- Send authentication headers on these requests.
- Content responses are inline file responses, not JSON.
- Page numbering is 1-based.
- Replace `{page}` in a returned page-image template with a positive integer.
- A non-PDF original returns `415` for page rendering; converted preview sources expose their own PDF/page URLs.
- A missing page/source returns `404`.
- Browsers cannot attach API-key headers to a plain `<img src>` or `<iframe src>` reliably. Fetch as an authenticated Blob and create an object URL, or use the authenticated local workbench session.

## 10. Multi-bid results and reports

Read current project results:

```http
GET /v1/workbench/projects/{project_id}/results
```

The response is keyed by `vendor_id`, allowing one frontend to compare any number of bidders without conflating their evaluations.

Generate the report snapshot after required evaluations complete:

```http
POST /v1/workbench/projects/{project_id}/report/generate
```

Read/download:

```http
GET /v1/workbench/projects/{project_id}/report
GET /v1/workbench/projects/{project_id}/report/markdown
GET /v1/workbench/projects/{project_id}/report/excel
```

The JSON report is the integration format. Markdown and Excel are downloadable presentation formats. Before every bidder is evaluated, the JSON report returns HTTP `200` with `complete: false` and lists `missing_vendors`; clients should keep polling or show the incomplete state.

### BHEL evaluation extension

When the operator enables the BHEL profile, read one bidder's noting-aligned result with:

```http
GET /v1/pipeline/fixed-schema/projects/{project_id}/bids/{vendor_id}/evaluation/bhel
```

Read the all-bidder criterion matrix with:

```http
GET /v1/workbench/projects/{project_id}/results/bhel
```

The first response uses `bhel-bid-evaluation.v1`; the comparative response uses
`bhel-bid-comparative.v1`. Tender-derived checks retain exact tender-source
references and bidder evidence. A frozen criterion with missing tender
provenance remains visible as `needs_review`; it is never silently dropped or
reported as grounded. Pending and failed bidders remain visible in the
comparative response as `not_evaluated`.

For the BHEL profile, classification approval freezes the submitted bidder
package. Later document uploads and clarification mutations return HTTP `409`
with stable codes `BHEL_SUBMISSION_FROZEN` and
`BHEL_CLARIFICATIONS_DISABLED`. A human criterion decision is append-only: the
effective BHEL result changes, while `ai_result` preserves the original machine
decision.

## 11. Error contract

All documented JSON errors use:

```json
{
  "error": {
    "code": "422",
    "message": "request validation failed",
    "details": {
      "errors": [
        {
          "field": "vendor_name",
          "message": "Field required",
          "type": "missing"
        }
      ]
    }
  }
}
```

| HTTP | Meaning | Consumer action |
|---:|---|---|
| `400` | Malformed JSON/schema, invalid key, or invalid request semantics. | Fix the request; do not retry unchanged. |
| `401` | Missing/invalid token. | Obtain or correct credentials. |
| `403` | Authenticated but role is insufficient. | Use an authorized identity; do not retry automatically. |
| `404` | Project, bidder, job, source, document, or page not found in the tenant. | Verify stored identifiers and tenant. |
| `409` | Workflow gate, immutable state, duplicate key/entity, or idempotency conflict. | Read `message`/`details`, complete the required review step, or choose a new key. |
| `413` | Upload or schema exceeds a configured limit. | Reduce/split input according to operator policy. |
| `415` | Unsupported file/media operation. | Convert to a supported format or use a returned preview source. |
| `422` | Structurally valid transport with invalid fields, page ranges, or processable content. | Correct the fields/content; do not blindly retry. |
| `429` | Rate limit. | Back off with jitter; retry using the same idempotency key. |
| `500` | Unexpected server failure. | Record the server `error_id` if supplied and contact the operator. |
| `503` | Required configuration/provider/database/storage unavailable. | Retry only after the dependency/configuration is restored. |

Never branch on the human `message`. Branch on HTTP status and stable `error.code`; show `message` to an operator and retain `details` for diagnostics.

## 12. Retry and timeout policy

- Retry `429`, transient network failures, and temporary `503` with exponential backoff and jitter.
- Do not retry `400`, `401`, `403`, `404`, `409`, `413`, `415`, or `422` unchanged.
- Use the same `Idempotency-Key` for retries of the same start request.
- Do not retry a failed job by inventing a new key until its `error_code` has been assessed.
- Set upload/request timeouts independently from the asynchronous job timeout. The start call should return quickly; the worker can take longer.

## 13. Integration acceptance checklist

- Import `openapi.json` into the chosen generator or API tool.
- Configure base URL and credentials outside source code.
- Persist every returned ID and idempotency key.
- Treat all three start calls as asynchronous.
- Implement all four job states and terminal-failure display.
- Present tender ledger evidence and authenticated previews.
- Require explicit tender correction/draft/freeze approval.
- Present bidder classification, summaries, routes, and previews.
- Require explicit classification correction/approval.
- Start evaluation only after both gates pass.
- Keep every bidder under its own `vendor_id`.
- Display final decision, pass/fail/review counts, per-criterion explanation, missing documents, manual-review reasons, and financial-opening control.
- Keep evidence links authenticated.
- Implement stable error parsing and safe retry policy.
- Do not expose price/commercial documents before authorized opening.
- Do not add a cancel control; v1 has no cancellation API.
- Verify against the supplied contract smoke test before frontend handoff.

## 14. Compatibility promise

`procurement-pipeline.v1` is the public envelope. Stage result schemas are independently versioned:

- `procurement-understanding.v1`
- `procurement-classification.v1`
- `procurement-pipeline-evaluation.v1`, containing `procurement-evaluation.v3`

Consumers should reject an unsupported major schema version, ignore additive unknown fields, and never depend on undocumented internal workbench state. Breaking field removal/type changes require a new major public route or schema version.
