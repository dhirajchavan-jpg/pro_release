# Frontend migration plan

The goal is visual and behavioral parity with `frontend/`, delivered in reviewable phases. Each phase should be accepted before the next one starts.

## Phase 1 — Foundation (implemented)

- Vite, React, and Tailwind CSS application shell.
- Environment-driven backend URL and timeout.
- Shared fetch client with cookies, uploads, timeouts, and normalized API errors.
- Environment-driven FastAPI CORS allowlist.
- Initial design tokens taken from the current workbench theme.

Acceptance: install, build, and launch succeed; the configured API URL appears on the foundation screen.

## Phase 2 — Account and tender workspace (implemented)

- Login, registration, profile, logout, and session restoration.
- Tender list, create/open/edit actions, loading states, and errors.
- Shared header, footer, buttons, inputs, messages, cards, and responsive layout.

Acceptance: pixel-level comparison at desktop and mobile widths; existing account/project APIs work.

## Phase 3 — Tender setup and review (implemented)

- Tender upload and analysis setup.
- Field schema configuration, job polling, ledger, source viewer, corrections, contract draft, and freeze.
- Workflow navigation and route restoration.

Acceptance: a tender can move from upload through frozen contract with parity to the legacy UI.

## Phase 4 — Bid intake and classification (implemented)

- Bidder upload/replacement/removal and intake status.
- Classification proposal, manual correction, source evidence, and approval.

Acceptance: multi-bidder workflows and all classification states match the legacy implementation.

## Phase 5 — Compliance, evaluation, and results (implemented)

- Compliance execution, policy controls, findings, and resolution.
- Evaluation execution, job progress, review actions, and results views.

Acceptance: failure, retry, locked, empty, and completed states are all covered.

## Phase 6 — Reports, admin, and cutover (implemented; cutover not activated)

- Report generation/download and admin usage/audit dashboard.
- Accessibility, responsive, performance, and end-to-end regression checks.
- Production build serving strategy and removal of legacy routes only after approval.

Acceptance: full parity is signed off and rollback/cutover instructions are documented.

The React build is available at `/app/`. The legacy `/workbench` routes remain unchanged until visual parity is accepted and the cutover is explicitly approved.
