// Provisions two Container Apps that share a config file:
//
//   * climcp-mcp   — MCP server, no Easy Auth, uses its own MI for Azure RBAC.
//                    Foundry / agents authenticate via API key (or Entra JWT).
//   * climcp-ui    — React admin SPA + admin REST API, fronted by Container
//                    Apps Easy Auth (browser sign-in, requires Admin role).
//
// Both apps mount the same Azure Files SMB share at /etc/cli-mcp where the
// runtime config lives. The MCP app hot-reloads on file change; the UI writes
// updates through the admin REST API.

targetScope = 'resourceGroup'

@description('Short app name (lowercase, 2-12 chars).')
@minLength(2)
@maxLength(12)
param appName string = 'climcp'

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('Container image for the MCP server, e.g. myacr.azurecr.io/cli-mcp-server:0.2.0')
param mcpImage string

@description('Container image for the admin UI, e.g. myacr.azurecr.io/cli-mcp-ui:0.2.0')
param uiImage string

@description('Name of the Azure Container Registry that hosts both images (same RG).')
param acrName string

@description('Entra tenant ID used to validate JWTs / Easy Auth.')
param tenantId string = subscription().tenantId

@description('Entra app (client) ID for the cli-mcp-tool app registration.')
param appRegClientId string

@description('Audience expected in inbound JWTs (typically api://<appRegClientId>).')
param entraAudience string = 'api://${appRegClientId}'

@description('Client secret of the Entra app registration. Required for Easy Auth code flow.')
@secure()
param aadClientSecret string

@description('Optional API key value injected as MCP_API_KEY into the MCP app.')
@secure()
param apiKey string = ''

@description('Optional subscription id the MCP server should default to.')
param defaultSubscriptionId string = ''

@description('Tags applied to all resources.')
param tags object = {
  app: 'cli-mcp-tool'
}

var resourcePrefix = toLower(appName)
var uniq = uniqueString(resourceGroup().id, appName)
var blobContainerName = 'config'
var blobName = 'config.json'

// ---------- Log Analytics ----------
resource law 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${resourcePrefix}-law-${uniq}'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

// ---------- Storage account + Blob container (OAuth-only, no shared key) ----------
resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: take('${resourcePrefix}st${uniq}', 24)
  location: location
  tags: tags
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    allowSharedKeyAccess: false
    minimumTlsVersion: 'TLS1_2'
    publicNetworkAccess: 'Enabled'
    defaultToOAuthAuthentication: true
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource configContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: blobContainerName
  properties: {
    publicAccess: 'None'
  }
}

// ---------- Container Apps Environment ----------
resource cae 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${resourcePrefix}-env-${uniq}'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: law.properties.customerId
        sharedKey: law.listKeys().primarySharedKey
      }
    }
  }
}

// ---------- Shared user-assigned managed identity (for ACR pull) ----------
resource uami 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${resourcePrefix}-id-${uniq}'
  location: location
  tags: tags
}

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: acrName
}

var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'
resource acrPullAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, uami.id, 'acrpull')
  scope: acr
  properties: {
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      acrPullRoleId
    )
  }
}

// Storage Blob Data Contributor on the shared storage account, scoped to the
// UAMI so both containers can read/write config.json without shared keys.
var blobDataContributorRoleId = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
resource blobRoleUami 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, uami.id, 'blob-data-contributor')
  scope: storage
  properties: {
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      blobDataContributorRoleId
    )
  }
}

// ---------- MCP Container App ----------
resource appMcp 'Microsoft.App/containerApps@2024-03-01' = {
  name: '${resourcePrefix}-mcp-${uniq}'
  location: location
  tags: tags
  dependsOn: [ acrPullAssignment, blobRoleUami, configContainer ]
  identity: {
    type: 'SystemAssigned, UserAssigned'
    userAssignedIdentities: {
      '${uami.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: cae.id
    configuration: {
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
      }
      registries: [
        {
          server: acr.properties.loginServer
          identity: uami.id
        }
      ]
      secrets: empty(apiKey) ? [] : [
        {
          name: 'mcp-api-key'
          value: apiKey
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'cli-mcp'
          image: mcpImage
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: union(
            [
              { name: 'ENTRA_TENANT_ID', value: tenantId }
              { name: 'ENTRA_AUDIENCE',  value: entraAudience }
              { name: 'AZURE_SUBSCRIPTION_ID', value: defaultSubscriptionId }
              { name: 'CONFIG_PATH', value: '/var/lib/cli-mcp/config.json' }
              { name: 'CONFIG_BLOB_URL', value: '${storage.properties.primaryEndpoints.blob}${blobContainerName}/${blobName}' }
              { name: 'AZURE_CLIENT_ID', value: uami.properties.clientId }
            ],
            empty(apiKey) ? [] : [ { name: 'MCP_API_KEY', secretRef: 'mcp-api-key' } ]
          )
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 3
      }
    }
  }
}

// Reader at RG scope for the MCP system MI so `az` calls succeed by default.
var readerRoleId = 'acdd72a7-3385-48ef-bd42-f606fba81ae7'
resource readerAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, appMcp.id, 'reader')
  scope: resourceGroup()
  properties: {
    principalId: appMcp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      readerRoleId
    )
  }
}

// ---------- UI Container App ----------
resource appUi 'Microsoft.App/containerApps@2024-03-01' = {
  name: '${resourcePrefix}-ui-${uniq}'
  location: location
  tags: tags
  dependsOn: [ acrPullAssignment, blobRoleUami, configContainer ]
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${uami.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: cae.id
    configuration: {
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
      }
      registries: [
        {
          server: acr.properties.loginServer
          identity: uami.id
        }
      ]
      secrets: [
        {
          name: 'aad-client-secret'
          value: aadClientSecret
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'cli-mcp-ui'
          image: uiImage
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
          env: [
            { name: 'CONFIG_PATH', value: '/var/lib/cli-mcp/config.json' }
            { name: 'CONFIG_BLOB_URL', value: '${storage.properties.primaryEndpoints.blob}${blobContainerName}/${blobName}' }
            { name: 'AZURE_CLIENT_ID', value: uami.properties.clientId }
            { name: 'ENTRA_TENANT_ID', value: tenantId }
            { name: 'ENTRA_AUDIENCE',  value: entraAudience }
          ]
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 2
      }
    }
  }
}

// ---------- Easy Auth (Microsoft) on the UI app ----------
resource uiAuth 'Microsoft.App/containerApps/authConfigs@2024-03-01' = {
  parent: appUi
  name: 'current'
  properties: {
    platform: {
      enabled: true
    }
    globalValidation: {
      unauthenticatedClientAction: 'RedirectToLoginPage'
      redirectToProvider: 'azureactivedirectory'
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          openIdIssuer: 'https://sts.windows.net/${tenantId}/v2.0'
          clientId: appRegClientId
          clientSecretSettingName: 'aad-client-secret'
        }
        validation: {
          allowedAudiences: [
            entraAudience
            'api://${appRegClientId}'
          ]
        }
      }
    }
    login: {
      tokenStore: {
        enabled: false
      }
    }
  }
}

output mcpUrl string = 'https://${appMcp.properties.configuration.ingress.fqdn}'
output uiUrl string = 'https://${appUi.properties.configuration.ingress.fqdn}'
output mcpPrincipalId string = appMcp.identity.principalId
output uamiPrincipalId string = uami.properties.principalId
output storageAccountName string = storage.name
output blobContainerName string = blobContainerName
output configBlobUrl string = '${storage.properties.primaryEndpoints.blob}${blobContainerName}/${blobName}'
