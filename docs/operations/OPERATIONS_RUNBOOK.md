# Operations runbook

This runbook covers the single-account Windows deployment. PostgreSQL is the
authoritative state store. Uploaded and generated files remain in the configured
object store. The API and worker are separate processes and durable work is
queued in PostgreSQL.

## Before installation

- Use 64-bit Windows and a permanent local installation directory.
- Keep at least 4 GB free for first setup. Runtime readiness also enforces
  `MIN_AVAILABLE_MEMORY_MB` and `MIN_FREE_DISK_MB` from `.env`.
- Confirm the packaged provider credentials, URLs, deployment IDs, and model
  names remain approved. This is a restricted secret-bearing artifact.
- Confirm `AI_PRICING_REVISION`, `USD_TO_INR_RATE`, and the provider rates before
  deployment. Calls without enough pricing data remain visible as unpriced;
  they are never assigned an invented cost.
- Decide the API port. Local access needs no proxy. Remote access requires an
  approved HTTPS reverse proxy, `ALLOW_REMOTE_API=1`, a non-loopback
  `API_HOST`, the external `PUBLIC_BASE_URL`, and `AUTH_COOKIE_SECURE=1`.

## Install

1. Verify the companion ZIP SHA256 before extraction.
2. Extract the package and run `Verify_Package.bat`.
3. Edit only the port/network values required for this machine, then run
   `Setup.bat`. Setup installs signed bundled runtimes and creates a new local
   database while preserving the packaged provider and login credentials.
4. Keep these
   enforced trial controls unchanged:

   ```text
   WORKER_CONCURRENCY=1
   MAX_RUNNING_JOBS=1
   MAX_QUEUED_OPERATIONS=11
   MAX_UPLOAD_MB=50
   MAX_BIDDERS_PER_TENDER=10
   RETENTION_DAYS=0
   AUTH_MODE=api_token
   ALLOW_DIRECT_API_TOKEN_AUTH=0
   PROVIDER_USAGE_REQUIRED=1
   AI_BUDGET_LIMIT_INR=<approved lifetime cap>
   ```

5. Run `Start_All.bat` for local acceptance or `Deploy.bat` on the server.
6. Open `/ready`; every check must be true. Rotate the password during
   maintenance by replacing
   `PROCUREMENT_LOCAL_PASSWORD_HASH` and restarting the services.

Setup preserves an existing `.env`. For a long installation path it stores the
Python dependency runtime under `%USERPROFILE%\.procurement-evaluation` and
writes that absolute location to `.env`, so SYSTEM restart recovery uses the
same runtime.

## Operate

- `Start_All.bat` starts PostgreSQL, applies migrations, and starts API and worker.
- `Stop_All.bat` stops only processes proven to belong to this installation.
- `Restart_All.bat` requests a controlled supervisor restart.
- `Logs.bat` follows API, worker, migration, supervisor, and PostgreSQL logs.
- `Self_Check.bat` parses the launcher and performs non-mutating dry runs.
- `/health` proves API liveness. `/ready` additionally checks database, object
  storage, worker heartbeat, provider configuration, free memory, and free disk.

One successful login invalidates the previous browser session. One operation
runs at a time and at most ten more wait. The tenth bidder is accepted; an
eleventh bidder is rejected without creating orphan state. Tender and bidder
uploads are each capped at 50 MB.

Usage and estimated cost are available at `GET /v1/workbench/usage`; add
`project_id` to reconcile one tender and its bids. The response separates calls,
tokens, input units, retries, cache hits, provider time, priced cost, and
unpriced successful calls by stage and capability. Its `budget` object reports
the tenant-wide cap, spend, headroom, blocked calls, FX rate, and pricing
revision even when the rest of the response is filtered to one project. A call
is not made once recorded spend has reached the cap; the rejected call is
recorded as `budget_blocked`. The cap does not reduce model context, output
tokens, retries, or any other accuracy setting. Calls already in flight may
finish and make final spend exceed the application cap, so the provider account
must also have its own hard spending limit.

`GET /v1/workbench/audit/verify` must return `valid: true`. State changes and
distinct workflow-step visits are hash chained in `audit_events`; paid-provider
activity is in `provider_calls`; durable work is in `operations`; tender and bid
replacement lineage is retained in the version tables and object metadata.

## Incident triage

1. Preserve `runtime\logs` and the failing request or operation identifier.
2. Check `/ready`, then `api.err.log`, `worker.err.log`, `supervisor.log`, and
   `postgres.log`. Do not paste document contents or credentials into tickets.
3. Provider failures remain attached to the operation; the application does not
   silently switch provider or model. Correct the approved endpoint and retry
   through the UI.
4. If disk or memory readiness fails, stop new submissions and restore capacity.
   Do not delete `runtime`, PostgreSQL data, or objects as a recovery shortcut.
5. If a process dies, the supervisor retries with bounded backoff. Use
   `Restart_All.bat` only after the underlying dependency is healthy.
6. If an operation reports exhausted AI budget, inspect `/v1/workbench/usage`.
   Raise `AI_BUDGET_LIMIT_INR` only with authorization, restart, and record the
   change. Also set provider-side spend/credit limits because no application can
   stop a charge after its process or database has failed.

## Update and rollback

There is no automated backup in this trial. Before every update, stop new work,
wait for active operations to finish, and take an operator-controlled backup of:

- PostgreSQL with the bundled `pg_dump.exe`;
- `.env` through the approved secret-handling process;
- the configured object-storage root; and
- `runtime\logs`.

Keep the prior signed package and its SHA256 beside that backup. Verify the new
package, stop services, replace application files without deleting `.env` or
`runtime`, run `Setup.bat`, and start normally. If rollback is required after a
database migration, stop services, restore the matching PostgreSQL and object
backup, restore the prior package, verify its manifest, and start it. Never run
older code against a database already migrated by a newer release.

For decommissioning, stop services and disable autostart first. Retained trial
data is not deleted automatically.
