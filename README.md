# cli-mcp-tool

A custom **Model Context Protocol (MCP) server** that exposes the Azure CLI
(`az`) as a curated, configurable set of tools so a Foundry agent (or any MCP
client) can manage Azure services that the built-in **Azure MCP Server** does
not cover (APIM, Front Door, Application Gateway, Static Web Apps, …).

## Architecture

```
┌──────────────────── Azure Container App ─────────────────────┐
│  FastAPI process                                             │
│   ├─ /mcp        → MCP server (Streamable HTTP transport)    │
│   ├─ /admin/api  → Config CRUD (read/write config.json)      │
│   └─ /admin      → React SPA (Vite/TS)                       │
│                                                              │
│  Managed Identity → az CLI → Azure control plane             │
│  Mounted volume   → config.json (enabled services/commands)  │
└──────────────────────────────────────────────────────────────┘
```

- **Language/runtime:** Python 3.12, FastAPI, official `mcp` SDK.
- **Container base:** `mcr.microsoft.com/azure-cli` (so `az` is preinstalled).
- **Auth to Azure:** system-assigned Managed Identity (`az login --identity`).
- **Auth to clients:** Entra ID (Container Apps Easy Auth) for the admin UI;
  Entra token or API key for the `/mcp` endpoint.
- **Config:** JSON file mounted into the container; React UI edits it via
  `/admin/api` and triggers a hot reload.

## Repository layout

```
cli-mcp-tool/
├─ server/        # Python FastAPI + MCP server
├─ ui/            # React + Vite + TypeScript admin UI
├─ infra/         # Bicep modules for Container App + MI + RBAC
├─ config/        # Sample configs
├─ scripts/       # Dev helpers
└─ README.md
```

## Development

See `scripts/dev.ps1` for local dev.  Requires Python 3.12, Node 20, and the
Azure CLI logged in (`az login`) when running outside a Container App.

For full ops/deployment details see [`docs/OPERATIONS.md`](docs/OPERATIONS.md).

## Status

Initial implementation complete: Python MCP server, admin REST API, React
admin UI, Dockerfile, and Bicep for Azure Container Apps.

- **Deploy:** see [`docs/DEPLOY.md`](docs/DEPLOY.md) for the end-to-end walkthrough.
- **Operate:** see [`docs/OPERATIONS.md`](docs/OPERATIONS.md) for local dev and day-2 ops.
