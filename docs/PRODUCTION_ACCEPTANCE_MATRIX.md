# Production acceptance matrix

This matrix is the release contract and preserves the gaps observed at the
start of hardening. Current closure evidence is recorded in
`operations/RELEASE_ACCEPTANCE.md`; a historical baseline gap below is not a
statement about the accepted release.

## Fixed trial constraints

- One configured login and one active authenticated session.
- One operation may execute at a time; queued work must not execute concurrently.
- One tender package up to 50 MB.
- Up to 10 bidders per tender, each package up to 50 MB.
- Data is retained indefinitely during the trial.
- No automated backup and no application TLS are included in this trial.
- Outbound access is allowed only for explicitly configured model capabilities.
- Ports, paths, credentials, and capability endpoints are environment-owned.

## Acceptance tests

| Area | Required behavior | Baseline evidence | Baseline gap | Release check |
| --- | --- | --- | --- | --- |
| Login | Valid credentials create the only active browser session | PostgreSQL stores one active session hash per local tenant | Browser integration and restart test pending | Second login invalidates the first; logout invalidates the current session |
| Authentication | Private API and document routes reject missing, expired, or replaced sessions | Shared HTTP guard exists | No complete route contract | Automated route matrix plus browser expiry recovery |
| Tenant scope | A principal can never read or mutate another tenant's projects, files, jobs, results, or reports | Most queries include tenant scope | No adversarial cross-tenant suite | Cross-tenant UUID substitution returns 404/403 everywhere |
| Queue | Only one operation runs at once | Worker concurrency, global claim lock, and durable running-state check exist | PostgreSQL multi-process acceptance test pending | Multiple submissions queue safely; exactly one is running |
| Upload size | Tender and bidder packages stop at 50 MB | Shared streaming upload limit and production preflight enforce 50 MB | Exact-boundary route matrix pending | 50 MB accepted; 50 MB plus one byte rejected with 413 |
| Bidder count | A tender accepts no more than 10 bidders | Shared locked capacity check covers normalized, workbench, and pipeline routes | Concurrent PostgreSQL acceptance test pending | Tenth bidder accepted; eleventh rejected without orphan data |
| Archive safety | Traversal, links, encryption, bombs, malformed files, and unsupported types are rejected | Shared ingestion validator exists | No complete regression suite | Deterministic malicious-container suite |
| Tender revision | Re-upload creates a new version and preserves the prior package and audit lineage | Tender version route exists before freeze | Post-freeze correction policy is undefined | Version lineage, stale-job isolation, and immutable history tests |
| Understanding | Source ledger, OCR, amendments, 31 fields, evidence, and requirement matrix remain aligned | Running pipeline exists | No fixture-level full-flow contract | Offline deterministic tender fixture with exact expected facts |
| Understanding recovery | Restart or retry resumes without presenting stale prior-version output | Version pin and checkpoint exist | Not exercised end to end | Kill/restart tests at extraction and field checkpoints |
| Compliance optionality | Disabled audit never blocks or appears as a required step | UI hides disabled compliance | No backend-to-browser journey | Disabled and enabled browser journeys |
| Compliance | Purchase and Works policies map only applicable clauses with exact source references | Policy engine and atomic tests exist | No complete corpus coverage test | Golden Purchase and Works policy fixtures |
| Compliance review | Resolve, waive, and reopen preserve reasons and audit history | Mutation routes and audit calls exist | No round-trip test | Browser and DB verification for every action |
| Checklist | Draft derives from tender facts and governance rules; human changes are auditable | Draft/freeze routes exist | No full contract test | Add/edit/exclude/freeze and immutable-after-freeze tests |
| Bid revision | Corrected bidder package preserves old bytes and creates a new active revision | None | Current route only adds or destructively deletes pre-approval bids | Upload, replace, stale classification, and re-evaluation lineage tests |
| Classification | Every physical page belongs to a reviewed logical document; price material remains quarantined | Intake and approval gates exist | No complete fixture or 10-bidder test | Mixed PDF/ZIP fixture with corrected classifications |
| Evidence index | Text, page, lexical, embedding, reranking, OCR, and image evidence stay linked to source pages | Search manifest and semantic index exist | No persistence/reuse contract | Cache/reuse tests plus broken-preview regression |
| Evaluation | Every frozen criterion receives a grounded decision or explicit review state | Deterministic validator and model pipeline exist | Only policy edge cases are tested | Criterion-by-criterion golden result fixture |
| Evaluation policy | Applicable policy clauses are exact, scoped, non-duplicated, and never inserted as canned text | Policy next-action layer exists | No full Purchase/Works result test | Mock findings covering relaxation, clarification, deviation, rejection, and no-clause cases |
| Human review | Reviewer decisions overlay results and remain attributable to the shared operator session | Review table and overlay exist | No round-trip or stale-result test | Pass/fail/review correction and rerun tests |
| Clarifications | Draft/issue/response/close history remains explicit and never silently changes evidence | Clarification model/routes exist | Current trial scope and bidder re-upload interaction are untested | Lifecycle test with linked criterion and replacement bid |
| Results | Counts, filters, details, evidence previews, and policy treatments agree | Results projection exists | Prior regressions show count/filter drift | API invariant tests plus browser click-through |
| Recommendation | Bid comparison and AI suggestion are derived only from current successful evaluations | Award refresh exists | Failure and stale-evaluation behavior is partial | Mixed bidder outcomes and rerun replacement tests |
| Report | JSON, Markdown, and Excel contain the same current reviewed decisions | Explicit report generation exists | No parity test | Cross-format semantic parity check |
| Audit | Every mutation is hash-chained with actor, request ID, reason, before, and after | AuditEvent chain exists | Chain verification and event completeness are absent | Tamper detection and per-route event inventory |
| Usage | Every stage records capability, model, calls, tokens, latency, cache status, and estimated cost | Evaluation stores partial LLM usage | Understanding, compliance, classification, OCR, embedding, and reranking are incomplete | Per-operation usage ledger and project total reconciliation |
| Logging | Operational logs are useful but exclude credentials, request bodies, prompts, and document contents | Redacted request log exists | Worker/provider redaction is not proven | Secret canary and sensitive-content scan |
| Failure handling | Provider timeout, invalid JSON, database outage, disk pressure, and process death produce bounded recoverable states | Retry/lease machinery exists | Scenario coverage is absent | Fault-injection suite and browser recovery states |
| Retention | No automatic deletion occurs; low disk space is visible before upload or processing | Retention command is opt-in | Capacity forecasting and warnings are incomplete | Disk threshold and indefinite-retention checks |
| Packaging | Setup adapts to a short installation path, configured port, available CPU/RAM/disk, and private environment file | Windows scripts and preflight exist | Current source replica excludes bundled dependencies | Clean-machine install, restart, verify, and uninstall-free rollback |
| Security gate | Source, dependency, secret, license, and package checks are reproducible | Prior reports exist | Reports are stale until rebuilt from this replica | Fresh SBOM, dependency audit, secret scan, parser checks, and manifest |

## Current baseline

- Source replica tag: `baseline-20260831`
- Source commit: `1d5f4f948185b3235876d8a40e968a7a9cf9ec3c`
- Existing offline checks: 17 passed
- Existing complete browser journeys: none in the isolated replica
- Paid provider calls authorized for this hardening run: none unless explicitly
  required after deterministic and mock-provider tests are exhausted
- Architecture map: source/model validation passed; automated visual inspection
  of the local HTML artifact is pending because the in-app browser correctly
  blocks direct `file:` navigation. This is not a product acceptance result.
