# Windows installation and operations

Target: 64-bit Windows. The package includes a private Python 3.12 runtime,
offline Python wheels, PostgreSQL 16, and a signed LibreOffice headless
converter for legacy `.doc` and `.xls` files. PostgreSQL 15 or newer can
instead be supplied by the client.

Keep at least 4 GB free on the installation drive before the first setup run.

## Install

1. Extract the package to a permanent local path. When that path is long,
   setup automatically places the Python dependency runtime under
   `%USERPROFILE%\.procurement-evaluation` and records the absolute path in `.env`.
2. Treat this ZIP as a restricted secret-bearing artifact. It contains the
   approved provider configuration and local login hash in `.env`; do not put it
   in source control, public Drive folders, email, or chat.
3. If needed, edit only `API_PORT`, `PUBLIC_BASE_URL`, and the approved network
   settings in `.env`. The lifetime AI budget cap is the value configured in `AI_BUDGET_LIMIT_INR`.
   The cap stops new paid calls after recorded spend reaches it; it does not
   reduce model context, output tokens, retries, or accuracy settings. Keep a
   provider-side hard spending limit for charges already in flight.
4. Run `Setup.bat`. It verifies the package, prepares Python, LibreOffice, and a
   new machine-local PostgreSQL database while retaining provider and login
   credentials. Secret values are never printed.
5. Run `Start_All.bat` for local acceptance, or `Deploy.bat` for the controlled
   server installation. It validates configuration and provider health,
   applies database migrations, starts the API and durable worker, enables
   restart recovery, and opens live logs.
6. Open the `PUBLIC_BASE_URL` followed by `/workbench`.

For an externally managed PostgreSQL 15+ server, set
`LOCAL_POSTGRES_ENABLED=0` and set `DATABASE_URL` to the client-issued account.
The bundled database listens only on `127.0.0.1:55432`, uses SCRAM passwords,
and stores data under `runtime\postgres`.

## Operate

- `Start_All.bat`: start the database, migrate, and start API and worker.
- `Stop_All.bat`: stop API, worker, and the bundled database.
- `Restart_All.bat`: controlled restart through the supervisor.
- `Logs.bat`: live API, worker, migration, supervisor, and database logs.
- `Self_Check.bat`: parse and dry-run verification; starts no service.
- `Verify_Package.bat`: verify every shipped file against the SHA256
  manifest before first use.
- `Disable_Autostart.bat`: maintenance shutdown; `Enable_Autostart.bat` restores
  reboot startup and crash recovery.

The readiness endpoint is `http://127.0.0.1:<API_PORT>/ready`. Runtime logs are under
`runtime\logs`; prior process logs are preserved under `runtime\logs\archive`
after every controlled restart. A failed provider, malformed model response,
database outage, exhausted budget, or exhausted queue returns a bounded job
failure with a stable error identifier;
the worker does not silently change providers.

## Single-operator hosted mode

This package accepts one configured local username and has no registration
route. A successful login invalidates the prior browser session. Direct API-token
authentication is disabled. `WORKER_CONCURRENCY=1` is enforced both at startup
and when work is claimed. `MAX_QUEUED_OPERATIONS=11` permits one running operation
and ten pending operations; later submissions receive `QUEUE_CAPACITY_EXCEEDED`.
Read-only navigation and status polling remain available while work runs.

Uploads remain in the configured server object store, separated by tenant,
digest, and document ID. PostgreSQL links each object to its tender or bid and
stores a hash-chained audit event containing the filename, type, size, digest,
actor, and request ID. API access logs record actor, tenant, route, status, and
duration. Distinct workflow-screen visits and every state-changing action enter
the same tenant audit chain. Logs never contain request bodies, provider keys,
prompts, or uploaded file contents. Browser API/document responses are marked
`private, no-store`.

## Security and exposure

The default API bind is local-only. For approved network access, terminate TLS
at the client's reverse proxy, set the externally visible `PUBLIC_BASE_URL`, set
`AUTH_COOKIE_SECURE=1`, configure JWT/JWKS or retain the local API-token mode,
and enforce the India-only inference and storage controls in client
infrastructure. Do not expose the bundled PostgreSQL port.

Configured AI, OCR, embedding, reranker, and verifier services are explicit
data-egress boundaries. Tender text or page images sent to those services no
longer remain server-only; use only approved India-hosted endpoints and provider
data-handling terms. A browser user who is permitted to view a document can
still deliberately download or capture it; server controls cannot prevent that.

The installation ACL is restricted to the installation user, SYSTEM, and local
Administrators. This private build contains `.env` and its credentials by
explicit deployment decision. It contains no tender files, bidder files,
database data, runtime logs, or prior outputs. Security reports,
the CycloneDX SBOM, dependency inventory, PostgreSQL licenses, and package
manifest are under `SECURITY`.

## Backup and update

Before an update, stop new work and wait for active jobs to finish. Back up the
PostgreSQL database with the bundled `pg_dump.exe`, copy `.env` through the
secret-management process, and back up the configured object-storage root plus
`runtime\logs`. Verify the new package, preserve those assets, run `Setup.bat`, then
run `Restart_All.bat`. Database rollback is performed by restoring the matching
backup; do not copy an older application over a migrated database.

The operating and rollback procedures are in
`docs\operations\OPERATIONS_RUNBOOK.md`.
The isolated pre-deployment procedure is in
`docs\operations\LOCAL_ACCEPTANCE.md`.
