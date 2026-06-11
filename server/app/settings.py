"""Application settings loaded from env vars."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    config_path: Annotated[Path, Field(default=Path("/etc/cli-mcp/config.json"))]
    static_dir: Path | None = None
    az_login_required: bool = False
    enable_config_watch: bool = True

    @classmethod
    def load(cls) -> Settings:
        # Sensible local dev default: fall back to repo-relative sample.
        path = os.getenv("CONFIG_PATH")
        if path is None:
            local = (Path(__file__).resolve().parents[2] / "config" / "config.sample.json")
            if local.exists():
                os.environ["CONFIG_PATH"] = str(local)
        return cls()
