# Deploying cli-mcp-tool to Azure

End-to-end walkthrough. Plan ~30 minutes the first time.

## Prerequisites

Install / verify:
- Azure CLI ≥ 2.60 — `az version`
- Docker Desktop (or any OCI builder) — `docker version`
- `az login` against the tenant you'll deploy to
- Owner or User Access Administrator on the target subscription (you'll
  create role assignments)

Pick these values up front and keep them handy:

```powershell
$Sub      = "7ee2b43a-eaea-4259-be7b-c8c220bfbcf9"
$Rg       = "rg-cli-mcp"
$Location = "eastus2"
$AppName  = "climcp"                # short, 2-12 chars, lowercase
$Acr      = "climcpacr$(Get-Random -Maximum 9999)"   # must be globally unique
$Image    = "$Acr.azurecr.io/cli-mcp:0.1.0"
```

```powershell
az account set --subscription $Sub
az group create -n $Rg -l $Location
```

---

## Step 1 — Create an Entra app registration (for inbound auth)

This is the audience the MCP server validates JWTs against. Foundry agents
(or any caller) will request a token for it.

```powershell
$App = az ad app create --display-name "cli-mcp-tool" --sign-in-audience AzureADMyOrg | ConvertFrom-Json
$AppId    = $App.appId
$Audience = "api://$AppId"

# Expose an API + add an "Admin" app role so we can grant write access to the UI
az ad app update --id $AppId --identifier-uris $Audience

$adminRole = @{
  allowedMemberTypes = @("User","Application")
  description        = "Manage cli-mcp-tool configuration"
  displayName        = "Admin"
  isEnabled          = $true
  value              = "Admin"
  id                 = [guid]::NewGuid().ToString()
} | ConvertTo-Json -Compress
az ad app update --id $AppId --app-roles "[$adminRole]"

# Service principal in your tenant (needed before you can assign roles)
az ad sp create --id $AppId | Out-Null
```

Assign yourself (and anyone else who should use the UI) the **Admin** app
role: Entra portal → Enterprise applications → cli-mcp-tool → Users and
groups → Add user → pick **Admin**.

---

## Step 2 — Create an Azure Container Registry and build the image

```powershell
az acr create -g $Rg -n $Acr --sku Basic --admin-enabled false
az acr login -n $Acr

# From the repo root:
docker build -t $Image .
docker push $Image
```

(Or skip Docker locally and let ACR build it: `az acr build -r $Acr -t cli-mcp:0.1.0 .`)

---

## Step 3 — Pick an API key (optional but recommended)

Used by headless callers like the Foundry agent. Stored in Key Vault by the
Bicep template.

```powershell
$ApiKey = [Convert]::ToBase64String([guid]::NewGuid().ToByteArray()) + [Convert]::ToBase64String([guid]::NewGuid().ToByteArray())
$ApiKey | Set-Clipboard      # save somewhere safe!
```

---

## Step 4 — Deploy the infra (Container App + Log Analytics + KV + RBAC)

```powershell
az deployment group create `
  -g $Rg `
  -f infra/main.bicep `
  -p appName=$AppName `
  -p image=$Image `
  -p entraAudience=$Audience `
  -p defaultSubscriptionId=$Sub `
  -p apiKey=$ApiKey
```

Capture outputs:

```powershell
$Out = az deployment group show -g $Rg -n main --query properties.outputs | ConvertFrom-Json
$AppUrl      = $Out.appUrl.value
$PrincipalId = $Out.principalId.value
$AppUrl
```

The Container App's managed identity already has **Reader** at the resource
group scope (from the Bicep). If you enabled any **mutating** APIM commands,
grant it the matching write role on just the APIM resource(s), e.g.:

```powershell
$apimId = az apim show -g <apim-rg> -n <apim-name> --query id -o tsv
az role assignment create `
  --assignee-object-id $PrincipalId `
  --assignee-principal-type ServicePrincipal `
  --role "API Management Service Contributor" `
  --scope $apimId
```

---

## Step 5 — Smoke test

```powershell
# Health
curl "$AppUrl/healthz"

# Admin API with the API key
curl -H "X-API-Key: $ApiKey" "$AppUrl/admin/api/config"

# MCP endpoint — initialize handshake
curl -X POST "$AppUrl/mcp/" `
  -H "X-API-Key: $ApiKey" `
  -H "Content-Type: application/json" `
  -H "Accept: application/json, text/event-stream" `
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}'
```

Open `$AppUrl/admin/` in a browser. You'll be prompted to sign in (Easy Auth
+ Entra). With the **Admin** role you'll see the service/resource/command
toggle UI populated from `config.sample.json`.

---

## Step 6 — Wire up a Foundry agent

In the Foundry agent definition, add an **MCP tool** with:

- **URL:** `https://<app-fqdn>/mcp/`
- **Transport:** Streamable HTTP
- **Header:** `X-API-Key: <ApiKey>` *(or `Authorization: Bearer <token for $Audience>` if you'd rather use Entra)*

The agent will see one tool per enabled command (e.g. `apim_api_list`,
`apim_service_show`, …). Toggling commands in the admin UI hot-reloads the
tool list on the server side.

---

## Step 7 — Add more services later

1. Open the admin UI → it's a JSON-backed config editor.
2. Or `PUT /admin/api/config` with the updated JSON.
3. The server reloads in-process; no redeploy needed.

To support a brand-new service (Front Door, App Gateway, Static Web Apps,
etc.) just add a `services.<name>` block with the `azCommand` arrays you
want exposed. Mutating commands stay disabled until you flip the toggle.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `az login --identity` fails in container logs | Managed identity not enabled, or `AZURE_CLIENT_ID` set when using system MI. Remove `AZURE_CLIENT_ID` or set `SKIP_AZ_LOGIN=1` temporarily. |
| `401 invalid token` on `/admin/api` | JWT audience ≠ `$Audience`. Confirm `ENTRA_AUDIENCE` env var and the token's `aud` claim. |
| `403 admin role required` | User wasn't granted the **Admin** app role on the Enterprise app. |
| `az exited with code 3` from a tool | The MI lacks RBAC for that resource — assign the appropriate built-in role at the narrowest scope. |
| Admin UI is blank | The static build wasn't copied into `server/app/static`. Re-run `npm run build`, rebuild the image. |

## Useful log queries (Log Analytics)

```kusto
ContainerAppConsoleLogs_CL
| where ContainerAppName_s startswith "climcp"
| order by TimeGenerated desc
| project TimeGenerated, Log_s
```
