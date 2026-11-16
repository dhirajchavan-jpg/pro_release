# Local acceptance

Use this procedure before copying the package to the server. It does not submit
a paid inference request until step 8.

## Isolated installation

1. Keep the currently running service unchanged. Use the new ZIP only.
2. Verify the SHA256 value beside the ZIP:

   ```powershell
   Get-FileHash -Algorithm SHA256 'C:\path\Procurement_Evaluation_Windows_20260831.zip'
   Get-Content 'C:\path\Procurement_Evaluation_Windows_20260831.zip.sha256'
   ```

3. Extract to `C:\Procurement-Acceptance` and run `Verify_Package.bat` before
   changing any file.
4. Run `Setup.bat` without editing `.env`. Expected outcomes:

   - package verification passes;
   - provider and login credentials are retained;
   - the first free PostgreSQL port at or above 55432 is selected, leaving any
     existing database untouched;
   - `PACKAGE_LOCAL_ENV_PENDING` becomes `0`;
   - application imports and the database connection pass.

5. After setup, open `.env` in Notepad and change only:

   ```text
   API_PORT=8010
   PUBLIC_BASE_URL=http://127.0.0.1:8010
   ```

   Do not paste or display any credential value. A later `Setup.bat` run still
   verifies all packaged application files while allowing this configured `.env`.

6. Run `Start_All.bat`. Then open:

   - `http://127.0.0.1:8010/health` — expect `ok`;
   - `http://127.0.0.1:8010/ready` — every required check must pass;
   - `http://127.0.0.1:8010/workbench` — use the same configured local login.

7. Before paid testing, verify controls in `.env` without displaying secrets:

   ```text
   MAX_UPLOAD_MB=50
   MAX_BIDDERS_PER_TENDER=10
   WORKER_CONCURRENCY=1
   MAX_RUNNING_JOBS=1
   MAX_QUEUED_OPERATIONS=11
   PROVIDER_USAGE_REQUIRED=1
   AI_BUDGET_LIMIT_INR=500
   ```

8. Run one representative tender and its bids through the UI. Do not run
   duplicate analyses merely to test navigation. Cached OCR, embeddings, and
   structured model outputs are reused when their source and configuration
   fingerprints match.

## Evidence to check after the run

While logged in, open:

- `http://127.0.0.1:8010/v1/workbench/usage` — confirm the budget, calls, tokens,
  pages, retries, provider time, and INR/USD cost;
- `http://127.0.0.1:8010/v1/workbench/audit/verify` — confirm `valid` is `true`;
- `Logs.bat` — watch application, worker, migration, supervisor, and PostgreSQL
  logs without exposing documents or credentials.

The files are under `runtime\logs`:

- `api.out.log` and `api.err.log` — route, actor/tenant, status, request ID,
  timing, and safe errors;
- `worker.out.log` and `worker.err.log` — operation/stage progress, retry, and
  provider failures by job ID;
- `migrate.log` — database migration history;
- `supervisor.log` — process start, restart, and recovery;
- `postgres.log` — local database lifecycle.

The authoritative database records are:

- `audit_events` — hash-chained user actions and distinct workflow-step visits;
- `provider_calls` — provider, model, stage, duration, tokens/pages, retries,
  pricing snapshot, estimated cost, and budget blocks, without prompts/content;
- `operations` — queue, attempt, progress, completion, and failure state;
- tender/bid versions, documents, and object metadata — original and corrected
  upload lineage with digests.

## Failure checks

- Open a second browser and log in: the first browser session must become
  invalid.
- Upload a file over 50 MB: it must be rejected before processing.
- Add more than ten bidders: the eleventh must be rejected without orphan data.
- Lower `AI_BUDGET_LIMIT_INR` below current spend, restart, and start a paid
  operation only if a deliberate budget-block test is authorized. It must fail
  before provider egress and add a `budget_blocked` provider-call record.
- Do not expect the application budget to resize prompts or model output. It
  blocks only new calls after recorded spend reaches the cap; in-flight calls
  finish, and unpriced calls remain explicitly marked as unpriced.
- Stop with `Stop_All.bat`; the unrelated service on port 8001 must remain up.

After acceptance, run `Stop_All.bat`. Copy the untouched verified ZIP to the
server, choose its approved port/network settings, run `Setup.bat`, and then run
`Deploy.bat`.
