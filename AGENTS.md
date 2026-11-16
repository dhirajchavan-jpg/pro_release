# Repository Guidelines

## Project Structure & Module Organization

The Python 3.12 backend lives in `procurement/`. Keep API entry points and workers at the package root, domain and persistence code in `procurement/domain/` and `procurement/platform/`, tender extraction in `procurement/understanding/`, and evaluation logic in `procurement/evaluation/`. Browser assets are under `frontend/`; reusable ES modules belong in `frontend/modules/`. Database revisions live in `alembic/versions/`, configuration and policy data in `config/`, operational scripts in `scripts/`, and user or API documentation in `docs/`. Treat `runtime/`, `.env`, virtual environments, generated reports, and bundled executables as local or release artifacts, not source.

## Build, Test, and Development Commands

Run commands from the repository root in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\procurement-verify-config
.\.venv\Scripts\procurement-api       # API on 127.0.0.1:8000
.\.venv\Scripts\procurement-worker    # run in a second terminal
node --test frontend/modules/*.test.mjs
.\.venv\Scripts\python scripts\build_release.py
```

For the packaged Windows workflow, use `Setup.bat`, `Start_All.bat`, `Self_Check.bat`, and `Stop_All.bat`. Do not hand-edit generated release manifests.

## Coding Style & Naming Conventions

Use four spaces, type hints, `snake_case` for Python functions/modules, and `PascalCase` for classes. Keep imports grouped as standard library, third-party, then local. JavaScript uses two spaces, semicolons, double quotes, `camelCase`, and kebab-case module filenames. Prefer small domain-focused modules and preserve existing FastAPI/Pydantic patterns. No formatter is configured, so match nearby code and keep diffs focused.

## Testing Guidelines

Name JavaScript tests `*.test.mjs` beside their modules and use `node:assert/strict`. Add Python tests under `tests/` as `test_<feature>.py`; keep unit tests deterministic and isolate PostgreSQL, object-storage, and AI-provider integration tests. Run relevant tests plus `Self_Check.bat` before release changes. Never use production credentials or tender data in fixtures.

## Commit & Pull Request Guidelines

The short history has no stable convention; use concise imperative subjects such as `Add bidder validation retry`. Keep commits single-purpose. Pull requests should explain behavior and operational impact, list verification commands, link the issue, and include screenshots for `frontend/` changes. Call out migrations, environment-variable additions, policy changes, and rollback steps explicitly.

## Security & Configuration

Copy `.env.example` to `.env`; never commit secrets, tokens, customer documents, or files from `runtime/`. Validate schema changes with Alembic and update configuration examples and relevant docs whenever a setting changes.
