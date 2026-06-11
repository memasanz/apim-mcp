"""Entry point for the UI Container App."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .admin_api import build_admin_router
from .blob_store import maybe_create_blob_sync, remote_watch_loop, stop_task
from .config import ConfigStore
from .settings import Settings

log = logging.getLogger(__name__)


def _bootstrap_from_blob(config_path: Path):
    blob = maybe_create_blob_sync()
    if blob is None:
        return None
    if blob.exists():
        blob.download_to(config_path)
    elif not config_path.exists():
        raise RuntimeError(
            f"CONFIG_BLOB_URL is set but blob is missing and no local seed at {config_path}"
        )
    return blob


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.load()
    blob = _bootstrap_from_blob(settings.config_path)
    store = ConfigStore(settings.config_path, blob=blob)

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ANN202
        watch_task = None
        if settings.enable_config_watch:
            await store.start_watch()
            if blob is not None:
                import asyncio as _a

                watch_task = _a.create_task(
                    remote_watch_loop(
                        blob,
                        settings.config_path,
                        after_download=store.reload,
                    ),
                    name="blob-watch",
                )
        try:
            yield
        finally:
            await stop_task(watch_task)
            await store.stop_watch()

    app = FastAPI(title="cli-mcp-ui", version="0.3.0", lifespan=lifespan)

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok", "role": "ui"})

    app.include_router(build_admin_router(store))

    static_dir = settings.static_dir or (Path(__file__).resolve().parent / "static")
    if static_dir.exists():
        app.mount("/admin", StaticFiles(directory=static_dir, html=True), name="admin-ui")
    else:
        log.warning("Static UI directory %s missing — /admin/ will 404", static_dir)

    app.state.store = store
    return app


app = create_app()


def run() -> None:
    """Console-script entry: ``cli-mcp-ui``."""
    import uvicorn

    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    uvicorn.run(
        "app.main_ui:app",
        host=os.getenv("HOST", "0.0.0.0"),  # noqa: S104
        port=int(os.getenv("PORT", "8000")),
        reload=bool(os.getenv("DEV_RELOAD")),
    )


if __name__ == "__main__":  # pragma: no cover
    run()
