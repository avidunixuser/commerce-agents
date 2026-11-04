param name string
param location string
param vnetId string
param endpointSubnetId string
@minValue(1000)
param cosmosMaxThroughput int = 4000
param databaseName string = 'commerce'
param containerNames array = ['runtime', 'identity']

var suffix = uniqueString(resourceGroup().id, name)
resource cosmos 'Microsoft.DocumentDB/databaseAccounts@2024-11-15' = {
  name: '${name}-cosmos-${suffix}'
  location: location
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    disableLocalAuth: true
    publicNetworkAccess: 'Disabled'
    networkAclBypass: 'None'
    minimalTlsVersion: 'Tls12'
    consistencyPolicy: { defaultConsistencyLevel: 'Session' }
    locations: [{ locationName: location, failoverPriority: 0, isZoneRedundant: false }]
    backupPolicy: { type: 'Continuous', continuousModeProperties: { tier: 'Continuous7Days' } }
  }
}
resource database 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-11-15' = {
  parent: cosmos
  name: databaseName
  properties: {
    resource: { id: databaseName }
    options: { autoscaleSettings: { maxThroughput: cosmosMaxThroughput } }
  }
}
resource containers 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-11-15' = [for container in containerNames: {
  parent: database
  name: container
  properties: {
    resource: {
      id: container
      partitionKey: { paths: ['/pk'], kind: 'Hash', version: 2 }
      defaultTtl: -1
      indexingPolicy: {
        indexingMode: 'consistent'
        automatic: true
        includedPaths: [{ path: '/*' }]
        excludedPaths: [{ path: '/"_etag"/?' }]
      }
    }
  }
}]
resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: 'cr${replace(name, '-', '')}${suffix}'
  location: location
  sku: { name: 'Premium' }
  properties: {
    adminUserEnabled: false
    publicNetworkAccess: 'Disabled'
    networkRuleBypassOptions: 'None'
    dataEndpointEnabled: true
    policies: { retentionPolicy: { days: 30, status: 'enabled' } }
  }
}
resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: take('${name}-kv-${suffix}', 24)
  location: location
  properties: {
    tenantId: tenant().tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    enablePurgeProtection: true
    softDeleteRetentionInDays: 90
    publicNetworkAccess: 'Disabled'
    networkAcls: { defaultAction: 'Deny', bypass: 'None' }
  }
}
resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${name}-logs'
  location: location
  properties: { sku: { name: 'PerGB2018' }, retentionInDays: 30 }
}
resource insights 'Microsoft.Insights/components@2020-02-02' = {
  name: '${name}-insights'
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logs.id
    DisableLocalAuth: true
    IngestionMode: 'LogAnalytics'
  }
}
resource cosmosMetrics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'metrics'
  scope: cosmos
  properties: { workspaceId: logs.id, metrics: [{ category: 'AllMetrics', enabled: true }] }
}
resource vaultMetrics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'metrics'
  scope: vault
  properties: { workspaceId: logs.id, metrics: [{ category: 'AllMetrics', enabled: true }] }
}
resource registryMetrics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'metrics'
  scope: registry
  properties: { workspaceId: logs.id, metrics: [{ category: 'AllMetrics', enabled: true }] }
}
var privateServices = [
  { name: 'cosmos', id: cosmos.id, group: 'Sql', zone: 'privatelink.documents.azure.com' }
  { name: 'acr', id: registry.id, group: 'registry', zone: 'privatelink.azurecr.io' }
  { name: 'kv', id: vault.id, group: 'vault', zone: 'privatelink.vaultcore.azure.net' }
]
module endpoints './private-endpoint.bicep' = [for service in privateServices: {
  name: '${name}-${service.name}-private'
  params: {
    name: '${name}-${service.name}'
    location: location
    resourceId: service.id
    groupId: service.group
    zoneName: service.zone
    subnetId: endpointSubnetId
    vnetId: vnetId
  }
}]
output cosmosId string = cosmos.id
output cosmosName string = cosmos.name
output cosmosEndpoint string = cosmos.properties.documentEndpoint
output databaseName string = database.name
output registryId string = registry.id
output registryName string = registry.name
output registryServer string = registry.properties.loginServer
output vaultId string = vault.id
output vaultName string = vault.name
output vaultUri string = vault.properties.vaultUri
output logsId string = logs.id
output logsCustomerId string = logs.properties.customerId
@secure()
output logsKey string = logs.listKeys().primarySharedKey
output insightsConnectionString string = insights.properties.ConnectionString
output insightsId string = insights.id
output insightsName string = insights.name
