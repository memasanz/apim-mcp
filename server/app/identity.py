"""Managed-identity bootstrap for the Azure CLI.

When the container starts we run ``az login --identity`` so that subsequent
``az`` invocations inherit credentials from the Container App's managed
identity. Outside Azure (e.g., local dev), set ``SKIP_AZ_LOGIN=1`` and rely
on the developer's existing ``az login`` session.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from .az_runner import AzExecutionError, AzRunner

log = logging.getLogger(__name__)


@dataclass(slots=True)
class IdentityInfo:
    tenant_id: str
    subscription_id: str
    user_name: str
    user_type: str


class IdentityError(RuntimeError):
    """Raised when managed identity login fails and is required."""


async def login_with_managed_identity(
    runner: AzRunner,
    *,
    client_id: str | None = None,
    subscription_id: str | None = None,
) -> IdentityInfo:
    """Run ``az login --identity`` and return the resulting account info.

    Pass ``client_id`` for a user-assigned managed identity.
    """
    params: dict[str, object] = {}
    if client_id:
        # Newer azure-cli (2.65+) replaced `--username <client-id>` with
        # `--client-id` for managed-identity login. The old flag is rejected
        # outright, so we must use the new spelling.
        params["--client-id"] = client_id

    log.info("logging in with managed identity (client_id=%s)", client_id or "<system>")
    await runner.run(["login", "--identity"], params, output="json", timeout=30)

    if subscription_id:
        await runner.run(["account", "set"], {"--subscription": subscription_id}, timeout=15)

    show = await runner.run(["account", "show"], timeout=15)
    data = show.json or {}
    info = IdentityInfo(
        tenant_id=str(data.get("tenantId", "")),
        subscription_id=str(data.get("id", "")),
        user_name=str(data.get("user", {}).get("name", "")),
        user_type=str(data.get("user", {}).get("type", "")),
    )
    log.info(
        "managed identity active: tenant=%s subscription=%s user=%s (%s)",
        info.tenant_id,
        info.subscription_id,
        info.user_name,
        info.user_type,
    )
    return info


async def ensure_logged_in(
    runner: AzRunner,
    *,
    required: bool | None = None,
    client_id: str | None = None,
    subscription_id: str | None = None,
) -> IdentityInfo | None:
    """Best-effort login wrapper used at app startup.

    Honours ``SKIP_AZ_LOGIN``: when truthy, we only call ``az account show`` to
    confirm an existing session. When ``required`` is true and login fails,
    raises ``IdentityError``.
    """
    skip = os.getenv("SKIP_AZ_LOGIN", "").lower() in ("1", "true", "yes")
    req = required if required is not None else not skip

    try:
        if skip:
            show = await runner.run(["account", "show"], timeout=15)
            data = show.json or {}
            return IdentityInfo(
                tenant_id=str(data.get("tenantId", "")),
                subscription_id=str(data.get("id", "")),
                user_name=str(data.get("user", {}).get("name", "")),
                user_type=str(data.get("user", {}).get("type", "")),
            )
        return await login_with_managed_identity(
            runner,
            client_id=client_id or os.getenv("AZURE_CLIENT_ID"),
            subscription_id=subscription_id or os.getenv("AZURE_SUBSCRIPTION_ID"),
        )
    except AzExecutionError as exc:
        msg = f"az login failed: {exc} (stderr: {exc.stderr.strip()[:500]})"
        if req:
            raise IdentityError(msg) from exc
        log.warning("%s — continuing without verified identity", msg)
        return None
