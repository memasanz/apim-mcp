"""Entry point for the MCP-only Container App."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .az_runner import AzRunner
from .blob_store import maybe_create_blob_sync, remote_watch_loop, stop_task
from .config import ConfigStore
from .identity import ensure_logged_in
from .mcp_server import build_mcp_server
from .settings import Settings

log = logging.getLogger(__name__)


def _bootstrap_from_blob(config_path: Path):
    """If CONFIG_BLOB_URL is set, ensure the local config file is hydrated."""
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
    runner = AzRunner(default_timeout=float(store.config.defaults.timeoutSeconds))
    mcp = build_mcp_server(store, runner)
    mcp_asgi = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ANN202
        await ensure_logged_in(runner, required=settings.az_login_required)
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
        # Run the MCP sub-app's own lifespan so its session manager's task
        # group is initialized. Without this, every POST to /mcp returns
        # 500 "Task group is not initialized. Make sure to use run()."
        async with mcp_asgi.router.lifespan_context(mcp_asgi):
            try:
                yield
            finally:
                await stop_task(watch_task)
                await store.stop_watch()

    app = FastAPI(title="cli-mcp-server", version="0.4.1", lifespan=lifespan)

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok", "role": "mcp"})

    # Mount the FastMCP sub-app at root so its built-in /mcp route is the
    # canonical public URL (rather than /mcp/mcp). FastAPI's own routes
    # (e.g., /healthz) are registered before the mount so they take priority.
    app.mount("/", mcp_asgi)

    app.state.store = store
    app.state.runner = runner
    app.state.mcp = mcp
    return app


app = create_app()


def run() -> None:
    """Console-script entry: ``cli-mcp-server``."""
    import uvicorn

    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    uvicorn.run(
        "app.main_mcp:app",
        host=os.getenv("HOST", "0.0.0.0"),  # noqa: S104
        port=int(os.getenv("PORT", "8000")),
        reload=bool(os.getenv("DEV_RELOAD")),
    )


if __name__ == "__main__":  # pragma: no cover
    run()
