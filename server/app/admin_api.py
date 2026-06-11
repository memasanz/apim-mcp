"""Admin REST API for reading/writing the runtime config."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ValidationError

from .auth import Principal, require_easy_auth_admin
from .config import AppConfig, ConfigStore

log = logging.getLogger(__name__)


class ReloadResponse(BaseModel):
    ok: bool
    services: int
    enabled_commands: int


def build_admin_router(store: ConfigStore) -> APIRouter:
    router = APIRouter(prefix="/admin/api", tags=["admin"])

    @router.get("/config", response_model=AppConfig)
    async def get_config(_: Principal = Depends(require_easy_auth_admin)) -> AppConfig:
        return store.config

    @router.put("/config", response_model=AppConfig)
    async def put_config(
        body: dict[str, Any],
        _: Principal = Depends(require_easy_auth_admin),
    ) -> AppConfig:
        try:
            cfg = AppConfig.model_validate(body)
        except ValidationError as exc:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, detail=exc.errors()
            ) from exc
        await store.replace(cfg)
        return cfg

    @router.post("/reload", response_model=ReloadResponse)
    async def reload(_: Principal = Depends(require_easy_auth_admin)) -> ReloadResponse:
        cfg = await store.reload()
        return ReloadResponse(
            ok=True,
            services=len(cfg.services),
            enabled_commands=len(cfg.iter_enabled_commands()),
        )

    return router
