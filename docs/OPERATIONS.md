# Operations guide

Quick reference for deploying and operating **cli-mcp-tool**.

## 1. Local dev

```powershell
# From repo root
./scripts/dev.ps1        # server with auto-reload on :8000
cd ui ; npm install ; npm run dev   # admin UI on :5173, proxies to server
```

Settings honoured locally:

| env var             | effect                                                   |
| ------------------- | -------------------------------------------------------- |
| `CONFIG_PATH`       | Path to the config JSON. Defaults to the repo sample.    |
| `ALLOW_ANONYMOUS=1` | Disables auth (dev only).                                |
| `SKIP_AZ_LOGIN=1`   | Skips `az login --identity`; uses your existing session. |
| `MCP_API_KEY`       | Sets a static API key for `/mcp` and `/admin/api`.       |
| `ENTRA_TENANT_ID`   | Tenant used to validate inbound JWTs.                    |
| `ENTRA_AUDIENCE`    | Audience the JWT must target (your app registration).    |

## 2. Build the container

```powershell
docker build -t cli-mcp:dev .
docker run -p 8000:8000 -e ALLOW_ANONYMOUS=1 -e SKIP_AZ_LOGIN=1 cli-mcp:dev
```

## 3. Deploy to Azure

```powershell
az group create -n rg-cli-mcp -l eastus
az deployment group create `
  -g rg-cli-mcp `
  -f infra/main.bicep `
  -p infra/main.parameters.sample.json `
  -p image="<your-registry>/cli-mcp:0.1.0" `
  -p entraAudience="api://<your-app-id>"
```

This provisions:
- Log Analytics + Container Apps Environment
- A Container App with a **system-assigned managed identity**
- **Reader** at the resource group scope (extend per service if you enable
  mutating commands; the role assignments live in `infra/main.bicep`)
- Optional Key Vault holding the MCP API key

## 4. Add a new service / command

1. Add (or POST) entries under `services.<name>.resources.<name>.commands` in
   `config.json`. Mutating commands need `"mutation": true` and require
   explicit `"enabled": true` before they register.
2. Push the file (mounted volume) or call `POST /admin/api/reload`. The MCP
   tool list updates automatically.

## 5. Connect a Foundry agent

Point the agent at `https://<app-fqdn>/mcp` using the Streamable HTTP
transport. Authenticate with either:
- `X-API-Key: <key>` header (the Key Vault-backed value), or
- `Authorization: Bearer <Entra token for ENTRA_AUDIENCE>`.
