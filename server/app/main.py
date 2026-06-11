"""FastAPI entry point — wires MCP server, admin API, and static UI."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .admin_api import build_admin_router
from .az_runner import AzRunner
from .config import ConfigStore
from .identity import ensure_logged_in
from .mcp_server import build_mcp_server
from .settings import Settings

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.load()
    store = ConfigStore(settings.config_path)
    runner = AzRunner(default_timeout=float(store.config.defaults.timeoutSeconds))
    mcp = build_mcp_server(store, runner)
    mcp_asgi = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ANN202
        await ensure_logged_in(runner, required=settings.az_login_required)
        if settings.enable_config_watch:
            await store.start_watch()
        # Run the MCP sub-app's own lifespan so its session manager's task
        # group is initialized. Without this, every POST to /mcp returns
        # 500 "Task group is not initialized. Make sure to use run()."
        async with mcp_asgi.router.lifespan_context(mcp_asgi):
            try:
                yield
            finally:
                await store.stop_watch()

    app = FastAPI(title="cli-mcp-server", version="0.1.0", lifespan=lifespan)

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    app.include_router(build_admin_router(store))

    static_dir = settings.static_dir or (Path(__file__).resolve().parent / "static")
    if static_dir.exists():
        app.mount("/admin", StaticFiles(directory=static_dir, html=True), name="admin-ui")

    # Mount the FastMCP sub-app at root LAST so its built-in /mcp route is the
    # canonical public URL (rather than /mcp/mcp). FastAPI's own routes and
    # earlier mounts (registered above) take priority over this catch-all.
    app.mount("/", mcp_asgi)

    app.state.store = store
    app.state.runner = runner
    app.state.mcp = mcp
    return app


app = create_app()


def run() -> None:
    """Console-script entry: `cli-mcp-server`."""
    import uvicorn

    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    uvicorn.run(
        "app.main:app",
        host=os.getenv("HOST", "0.0.0.0"),  # noqa: S104 - container binds 0.0.0.0
        port=int(os.getenv("PORT", "8000")),
        reload=bool(os.getenv("DEV_RELOAD")),
    )


if __name__ == "__main__":  # pragma: no cover
    run()
