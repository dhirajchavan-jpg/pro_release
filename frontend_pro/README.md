# Tenderabl React frontend

This directory contains the phased React migration of the existing `frontend/` UI. The legacy UI remains the visual and behavioral source of truth until the final cutover.

## Local development

1. Copy `.env.example` to `.env` and set `VITE_API_BASE_URL`.
2. Add the Vite origin to the backend configuration: `FRONTEND_CORS_ORIGINS=http://127.0.0.1:5173`.
3. Run `npm install` once, then `npm run dev`.
4. Run the backend on the URL configured by `VITE_API_BASE_URL`.

Do not put secrets in a `VITE_*` variable. Vite exposes those values to browser code.

## Migration phases

See [`MIGRATION_PLAN.md`](./MIGRATION_PLAN.md) for phase boundaries and acceptance criteria.

## Production build

Run `npm run build`. Production configuration builds the SPA for `/app/` and uses the same origin for API calls. The FastAPI application serves the generated build at `/app/`, including deep links such as `/app/projects/<project-id>/report`.

The existing `/workbench` UI is deliberately retained as the rollback path. See [`CUTOVER.md`](./CUTOVER.md) before changing the default entry point.
