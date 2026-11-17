"""FastAPI application assembly."""

import os

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware

from procurement.api import router as production_router
from procurement.fixed_pipeline_api import router as fixed_pipeline_router
from procurement.health import router as health_router
from procurement.legacy_api import router as legacy_router
from procurement.pipeline_api import router as pipeline_router
from procurement.platform.api_runtime import install as install_api_runtime
from procurement.platform.legacy_jobs import mark_stale_jobs
from procurement.workbench_api import router as workbench_router
from procurement.workbench_pages import router as workbench_pages_router


def startup_housekeeping() -> None:
    if os.getenv("APP_ENV", "development").strip().lower() != "production":
        mark_stale_jobs()


def frontend_cors_origins() -> list[str]:
    """Return the explicit browser origins allowed to call the API."""
    default_origins = "http://127.0.0.1:5173,http://localhost:5173"
    if os.getenv("APP_ENV", "development").strip().lower() == "production":
        default_origins = ""
    configured = os.getenv("FRONTEND_CORS_ORIGINS", default_origins)
    return [origin.strip().rstrip("/") for origin in configured.split(",") if origin.strip()]


def create_app() -> FastAPI:
    app = FastAPI(title="Procurement Evaluation API")
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)
    allowed_origins = frontend_cors_origins()
    if allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=allowed_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    app.on_event("startup")(startup_housekeeping)
    install_api_runtime(app)
    for router in (
        health_router,
        legacy_router,
        production_router,
        fixed_pipeline_router,
        pipeline_router,
        workbench_router,
        workbench_pages_router,
    ):
        app.include_router(router)

    def custom_openapi() -> dict:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version="1.0.0",
            description=(
                "Use **Authentication → Sign in and obtain a bearer token**, then click "
                "**Authorize** and paste the returned JWT `access_token`."
            ),
            routes=app.routes,
        )
        schema.setdefault("components", {}).setdefault("securitySchemes", {})["BearerAuth"] = {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": "A JWT returned by workspace sign-in, registration, or invitation acceptance.",
        }
        app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = custom_openapi
    return app
