# Release acceptance

Acceptance date: 31 August 2026

## Product gates

- All 19 regression modules pass: 99 tests, including isolated PostgreSQL
  concurrency and full-flow acceptance, with no skipped tests.
- Deterministic Purchase and Works journeys complete through tender
  understanding, compliance, frozen checklist, ten bidder uploads,
  classification approval, ten evaluations, results, report, and all eight
  workbench routes.
- The journeys complete 51 durable operations, reject an eleventh bidder, make
  zero provider calls, preserve a valid audit chain, and replace the prior
  browser session on a second login.
- Browser verification covers both policy journeys and all workbench routes.
  Results filtering, bidder switching, completed-step navigation, document
  rendering, and console errors were checked.
- Malformed archives, cross-tenant access, stale revisions, queue limits,
  idempotency, audit tampering, provider failures, database failures, disk
  pressure, and process restart behavior are covered by the regression gates.

## Package gates

- The release is allowlist-built and excludes tests, datasets, samples, uploads,
  database data, runtime logs, and prior output.
- This private deployment build intentionally includes the approved `.env`.
  First setup replaces only machine-local database/runtime values; the manifest
  protects the resulting secret-bearing package in transit.
- Signed Python and LibreOffice installers, offline wheels, PostgreSQL, licenses,
  dependency inventory, CycloneDX SBOM, vulnerability report, secret scan, and
  SHA256 manifest are included.
- Setup supports long extraction paths by using a recorded short Python runtime
  path, and supports an environment-owned API port.
- Clean extraction, package verification, two-phase setup, database migration,
  readiness, controlled restart, controlled stop, and retained database state
  are release-blocking checks.

## Recorded clean-install evidence

- The package was extracted to an 88-character Windows path and verified before
  setup.
- Setup used a fresh external Python runtime and installed from the bundled
  wheelhouse without contacting a package index.
- With PostgreSQL already listening on the default port `55432`, first setup
  selected `55433` and kept the existing server untouched.
- The bundled PostgreSQL 16.13 database initialized, all migrations reached
  `20260831_14`, and every `/ready` check passed.
- API and worker launched from the recorded external runtime. Authenticated UI
  access passed, and a second login invalidated the first session.
- PostgreSQL was launched in an independent hidden process group. It remained
  healthy after the caller console exited, survived a controlled restart, and
  retained its tenant rows and migration version.
- The final security gate reported no known dependency vulnerabilities, no
  embedded secrets, valid Python and PowerShell syntax, and generated the
  CycloneDX SBOM and package/license inventory.

## Deployment prerequisite

The packaged provider credentials and rates must still be approved at the time
of deployment. Package acceptance and provider health checks do not authorize
or make a paid inference call. Set a provider-side spending limit in addition
to the application INR 500 cap.
