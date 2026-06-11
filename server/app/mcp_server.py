"""Generate MCP tools from the active config and expose them via the MCP SDK.

We use the ``FastMCP`` server with the Streamable HTTP transport so the
server can be mounted as a FastAPI sub-application at ``/mcp``.

Each enabled ``(service, resource, command)`` triple becomes one MCP tool:

    name:        <service>_<resource>_<command>   (dashes → underscores)
    description: human + az command preview
    args:        {
      parameters: dict[str, str|number|bool|list]  # forwarded to `az`
      subscription: optional override
    }
    returns:     parsed JSON when az emits JSON, else raw stdout
"""

from __future__ import annotations

import logging
import re
from typing import Any

from mcp.server.fastmcp import FastMCP

from .az_runner import AzExecutionError, AzRunner, AzValidationError
from .config import AppConfig, CommandSpec, ConfigStore

log = logging.getLogger(__name__)

_NAME_NORM = re.compile(r"[^a-zA-Z0-9_]+")


def _tool_name(service: str, resource: str, command: str) -> str:
    raw = f"{service}_{resource}_{command}"
    return _NAME_NORM.sub("_", raw).lower()


def _build_description(svc: str, res: str, cmd: str, spec: CommandSpec) -> str:
    preview = "az " + " ".join(spec.azCommand)
    parts = [f"Run `{preview}`."]
    if spec.description:
        parts.append(spec.description)
    if spec.mutation:
        parts.append("⚠️ This is a MUTATING command.")
    parts.append(f"(service={svc}, resource={res}, command={cmd})")
    return " ".join(parts)


def build_mcp_server(store: ConfigStore, runner: AzRunner) -> FastMCP:
    """Create a FastMCP server whose tool list reflects the current config.

    A subscription on the store rebuilds the tool registry whenever the
    config changes.
    """
    from mcp.server.transport_security import TransportSecuritySettings

    mcp = FastMCP(
        "cli-mcp-server",
        # We sit behind a public Container Apps FQDN with auth enforced by
        # the API-key middleware. Disable FastMCP's DNS-rebinding protection
        # which would otherwise reject the public Host header.
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        ),
    )
    # Track tool names we've registered so we can detect what to remove.
    registered: set[str] = set()

    def _register_all(cfg: AppConfig) -> None:
        # FastMCP exposes a private ``_tool_manager`` we can use for dynamic
        # re-registration. If the SDK shape changes, this is the one place
        # to adapt.
        tm = mcp._tool_manager  # noqa: SLF001
        # Remove old tools
        for name in list(registered):
            tm._tools.pop(name, None)  # noqa: SLF001
        registered.clear()

        for svc, res, cmd, spec in cfg.iter_enabled_commands():
            name = _tool_name(svc, res, cmd)
            description = _build_description(svc, res, cmd, spec)

            def _make(
                az_cmd: list[str],
                cmd_timeout: int | None,
                permissive: set[str] | None,
            ):
                async def _impl(
                    parameters: dict[str, Any] | None = None,
                    subscription: str | None = None,
                ) -> dict[str, Any]:
                    try:
                        result = await runner.run(
                            az_cmd,
                            parameters or {},
                            timeout=float(cmd_timeout) if cmd_timeout else None,
                            subscription=subscription,
                            permissive_params=permissive,
                        )
                    except AzValidationError as exc:
                        return {"ok": False, "error": "validation", "message": str(exc)}
                    except AzExecutionError as exc:
                        return {
                            "ok": False,
                            "error": "execution",
                            "returncode": exc.returncode,
                            "stderr": exc.stderr[-4000:],
                            "stdout": exc.stdout[-4000:],
                        }
                    return {
                        "ok": True,
                        "result": result.json if result.json is not None else result.stdout,
                    }

                return _impl

            impl = _make(
                list(spec.azCommand),
                spec.timeoutSeconds,
                set(spec.permissiveParams) if spec.permissiveParams else None,
            )
            impl.__name__ = name
            impl.__doc__ = description
            mcp.tool(name=name, description=description)(impl)
            registered.add(name)

        log.info("registered %d MCP tools from config", len(registered))

    # Initial registration
    _register_all(store.config)

    async def _on_change(cfg: AppConfig) -> None:
        _register_all(cfg)

    store.subscribe(_on_change)
    return mcp
