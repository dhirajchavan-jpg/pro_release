"""Console entry point for the production API process."""

from __future__ import annotations

from procurement.platform.env import env_bool, env_int, env_text, load_local_env
from procurement.platform.log_safety import uvicorn_log_config


def run_api() -> None:
    load_local_env()
    import uvicorn

    uvicorn.run(
        "app:app",
        host=env_text("API_HOST", "127.0.0.1"),
        port=env_int("API_PORT", 8000, minimum=1, maximum=65535),
        workers=1,
        log_level=env_text("LOG_LEVEL", "INFO").lower(),
        access_log=env_bool("API_ACCESS_LOG", True),
        log_config=uvicorn_log_config(),
    )
