"""Pydantic models + loader for the runtime config file.

The config file describes which Azure services, resources, and `az` CLI
commands are exposed as MCP tools. Read-only commands are enabled by
default; mutating commands must be explicitly enabled.

Hot-reload: callers can subscribe to changes via ``ConfigStore.subscribe``;
``ConfigStore.start_watch()`` polls the file's mtime in a background task.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from threading import RLock
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

PiiRisk = Literal["none", "low", "medium", "high"]

log = logging.getLogger(__name__)

# Identifiers must be CLI-token safe — used both as keys and as part of tool names.
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,40}$")
NameStr = Annotated[str, Field(pattern=_NAME_RE.pattern)]


class CommandSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    mutation: bool = False
    azCommand: list[str] = Field(min_length=1)
    description: str | None = None
    timeoutSeconds: int | None = Field(default=None, ge=1, le=600)
    # Documentation/governance hints surfaced in the admin UI. None of these
    # are enforced at runtime — they exist so operators and reviewers can see
    # at a glance what RBAC role the executing identity needs and whether a
    # command can return PII if anyone has misconfigured logging.
    rbacRoles: list[str] | None = None
    piiRisk: PiiRisk | None = None
    piiNotes: str | None = None
    # Parameter names (e.g. "--analytics-query") whose VALUES are allowed to
    # contain shell metacharacters that the runner would normally reject.
    # NUL, newlines and length limits are still enforced. Use this only for
    # parameters that legitimately need them, like KQL queries.
    permissiveParams: list[str] | None = None

    @field_validator("azCommand")
    @classmethod
    def _validate_az_tokens(cls, value: list[str]) -> list[str]:
        for tok in value:
            if not tok or any(c.isspace() for c in tok):
                raise ValueError(f"azCommand tokens must be non-empty and whitespace-free: {tok!r}")
            if not re.fullmatch(r"[A-Za-z0-9._-]+", tok):
                raise ValueError(f"azCommand token contains unsafe characters: {tok!r}")
        return value


class ResourceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    displayName: str | None = None
    description: str | None = None
    commands: dict[NameStr, CommandSpec]


class ServiceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    displayName: str | None = None
    description: str | None = None
    resources: dict[NameStr, ResourceSpec]


class DefaultsSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeoutSeconds: int = Field(default=60, ge=1, le=600)
    subscriptionId: str | None = None


class AppConfig(BaseModel):
    # Allow `$schema` for editor support, but reject everything else.
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_url: str | None = Field(default=None, alias="$schema")
    version: int = 1
    defaults: DefaultsSpec = Field(default_factory=DefaultsSpec)
    services: dict[NameStr, ServiceSpec] = Field(default_factory=dict)

    def iter_enabled_commands(
        self,
    ) -> list[tuple[str, str, str, CommandSpec]]:
        """Yields ``(service, resource, command, spec)`` for each enabled command.

        Mutating commands that are not explicitly enabled are skipped.
        """
        out: list[tuple[str, str, str, CommandSpec]] = []
        for svc_name, svc in self.services.items():
            for res_name, res in svc.resources.items():
                for cmd_name, cmd in res.commands.items():
                    if not cmd.enabled:
                        continue
                    out.append((svc_name, res_name, cmd_name, cmd))
        return out


# ---------------------------------------------------------------------------
# Loader / store
# ---------------------------------------------------------------------------


class ConfigError(RuntimeError):
    """Raised when the config file cannot be parsed or validated."""


def load_config(path: Path) -> AppConfig:
    """Read and validate a config file from disk."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"unable to read config at {path}: {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config at {path} is not valid JSON: {exc}") from exc

    try:
        return AppConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"config at {path} failed validation:\n{exc}") from exc


def write_config(path: Path, cfg: AppConfig) -> None:
    """Atomically write the config back to disk."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(cfg.model_dump_json(indent=2, exclude_none=True), encoding="utf-8")
    os.replace(tmp, path)


Listener = Callable[[AppConfig], Awaitable[None]]


class ConfigStore:
    """Holds the active config and notifies listeners on change."""

    def __init__(self, path: Path, blob: object | None = None) -> None:
        self.path = path
        self._blob = blob  # Optional BlobSync; avoids hard import dependency.
        self._lock = RLock()
        self._config = load_config(path)
        self._mtime = path.stat().st_mtime
        self._listeners: list[Listener] = []
        self._watch_task: asyncio.Task[None] | None = None

    @property
    def config(self) -> AppConfig:
        with self._lock:
            return self._config

    def subscribe(self, listener: Listener) -> None:
        self._listeners.append(listener)

    async def replace(self, cfg: AppConfig) -> None:
        with self._lock:
            write_config(self.path, cfg)
            self._config = cfg
            self._mtime = self.path.stat().st_mtime
        if self._blob is not None:
            try:
                await asyncio.to_thread(self._blob.upload_from, self.path)
            except Exception:  # noqa: BLE001
                log.exception("failed to upload config to blob; local copy is current")
        await self._notify(cfg)

    async def reload(self) -> AppConfig:
        cfg = load_config(self.path)
        with self._lock:
            self._config = cfg
            self._mtime = self.path.stat().st_mtime
        await self._notify(cfg)
        return cfg

    async def _notify(self, cfg: AppConfig) -> None:
        for listener in list(self._listeners):
            try:
                await listener(cfg)
            except Exception:  # noqa: BLE001
                log.exception("config listener raised; continuing")

    async def start_watch(self, interval: float = 2.0) -> None:
        if self._watch_task is not None:
            return

        async def _watch() -> None:
            while True:
                try:
                    await asyncio.sleep(interval)
                    mtime = self.path.stat().st_mtime
                    if mtime != self._mtime:
                        log.info("config file changed on disk; reloading")
                        await self.reload()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    log.exception("error in config watcher")

        self._watch_task = asyncio.create_task(_watch(), name="config-watcher")

    async def stop_watch(self) -> None:
        if self._watch_task is not None:
            self._watch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._watch_task
            self._watch_task = None
