# React frontend cutover and rollback

## Current deployment

- React frontend: `/app/`
- Legacy frontend: `/workbench`
- API: same origin under `/workbench` and `/v1/workbench`
- Generated React assets: `frontend_pro/dist/`

The release builder copies `frontend_pro/dist/` when the directory exists. Build the frontend before creating a Windows release:

```powershell
Set-Location frontend_pro
npm install
npm test
npm run build
Set-Location ..
.\venv\Scripts\python.exe scripts\build_release.py <existing release arguments>
```

## Acceptance before cutover

Verify the following at desktop and mobile widths with an authenticated test account:

1. Login, workspace, profile, and tender creation.
2. Tender review, compliance, checklist freeze, bid intake, and classification.
3. Evaluation, human review, results, report generation, and all downloads.
4. Admin usage, audit-chain status, tender activity, and provider calls.
5. Browser back/forward navigation and direct URLs below `/app/`.
6. Keyboard navigation, visible focus, skip link, and reduced-motion behavior.

Do not remove the legacy files or routes as part of acceptance testing.

## Cutover

After parity is approved, change only the installation's external launch URL or reverse-proxy root from `/workbench` to `/app/`. Keep `/workbench` available during the observation period. No API endpoint or stored procurement data changes are required.

## Rollback

Point the launcher or reverse proxy back to `/workbench`. The React frontend and legacy frontend use the same backend records, so rollback does not require a database migration. Preserve logs and record the reason for rollback before attempting another cutover.
