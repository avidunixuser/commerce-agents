targetScope = 'resourceGroup'

@minLength(3)
@maxLength(12)
param name string
param location string = resourceGroup().location
param vnetName string
param subnetPrefix string
param endpointSubnetPrefix string
param dnsProxyAddress string
param routeTableName string
param workspaceName string
param insightsName string
param registryName string
param cosmosName string
param vaultName string
param openAIName string
param databaseName string = 'commerce'
param enableModel bool = false
@description('Enable in a separately validated stage after the new API identities exist, so assignment IDs are resolvable in what-if.')
param enableApiAccess bool = false
param secretsReady bool = false
param modelName string
param modelVersion string
@allowed(['Standard'])
param modelSku string = 'Standard'
@minValue(1)
@maxValue(100)
param modelCapacity int = 100

var containerNames = ['${name}-runtime', '${name}-identity']
var workloads = [
  { key: 'shopping-api', containers: [containerNames[0]], secrets: [] }
  { key: 'merchant-api', containers: [containerNames[0]], secrets: [] }
  { key: 'shopping-gateway', containers: [containerNames[1]], secrets: ['oidc-client-secret', 'shopping-signing-key', 'shopping-encryption-keys'] }
  { key: 'merchant-gateway', containers: [containerNames[1]], secrets: ['oidc-client-secret', 'merchant-signing-key', 'merchant-encryption-keys'] }
]

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' existing = { name: vnetName }
resource route 'Microsoft.Network/routeTables@2024-05-01' existing = { name: routeTableName }
resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = { name: workspaceName }
resource cosmos 'Microsoft.DocumentDB/databaseAccounts@2024-11-15' existing = { name: cosmosName }
resource database 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-11-15' existing = {
  parent: cosmos
  name: databaseName
}
resource nsg 'Microsoft.Network/networkSecurityGroups@2024-05-01' = {
  name: '${name}-shopper'
  location: location
  properties: {
    securityRules: [
      { name: 'intra-environment-in', properties: { priority: 100, direction: 'Inbound', access: 'Allow', protocol: '*', sourceAddressPrefix: subnetPrefix, sourcePortRange: '*', destinationAddressPrefix: subnetPrefix, destinationPortRange: '*' } }
      { name: 'platform-probes', properties: { priority: 110, direction: 'Inbound', access: 'Allow', protocol: '*', sourceAddressPrefix: 'AzureLoadBalancer', sourcePortRange: '*', destinationAddressPrefix: subnetPrefix, destinationPortRange: '*' } }
      { name: 'deny-other-inbound', properties: { priority: 200, direction: 'Inbound', access: 'Deny', protocol: '*', sourceAddressPrefix: '*', sourcePortRange: '*', destinationAddressPrefix: '*', destinationPortRange: '*' } }
      { name: 'dns-proxy', properties: { priority: 100, direction: 'Outbound', access: 'Allow', protocol: '*', sourceAddressPrefix: subnetPrefix, sourcePortRange: '*', destinationAddressPrefix: dnsProxyAddress, destinationPortRange: '53' } }
      { name: 'private-dependencies', properties: { priority: 110, direction: 'Outbound', access: 'Allow', protocol: 'Tcp', sourceAddressPrefix: subnetPrefix, sourcePortRange: '*', destinationAddressPrefix: endpointSubnetPrefix, destinationPortRange: '443' } }
      { name: 'intra-environment-out', properties: { priority: 120, direction: 'Outbound', access: 'Allow', protocol: '*', sourceAddressPrefix: subnetPrefix, sourcePortRange: '*', destinationAddressPrefix: subnetPrefix, destinationPortRange: '*' } }
      { name: 'deny-other-private', properties: { priority: 130, direction: 'Outbound', access: 'Deny', protocol: '*', sourceAddressPrefix: subnetPrefix, sourcePortRange: '*', destinationAddressPrefixes: ['10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'], destinationPortRange: '*' } }
      { name: 'https-through-existing-firewall', properties: { priority: 140, direction: 'Outbound', access: 'Allow', protocol: 'Tcp', sourceAddressPrefix: subnetPrefix, sourcePortRange: '*', destinationAddressPrefix: 'Internet', destinationPortRange: '443' } }
      { name: 'deny-other-outbound', properties: { priority: 200, direction: 'Outbound', access: 'Deny', protocol: '*', sourceAddressPrefix: '*', sourcePortRange: '*', destinationAddressPrefix: '*', destinationPortRange: '*' } }
    ]
  }
}
resource subnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' = {
  parent: vnet
  name: '${name}-shopper'
  properties: {
    addressPrefix: subnetPrefix
    networkSecurityGroup: { id: nsg.id }
    routeTable: { id: route.id }
    delegations: [{ name: 'container-apps', properties: { serviceName: 'Microsoft.App/environments' } }]
  }
}
resource shopperEnvironment 'Microsoft.App/managedEnvironments@2025-01-01' = {
  name: '${name}-shopper'
  location: location
  properties: {
    vnetConfiguration: { internal: false, infrastructureSubnetId: subnet.id }
    workloadProfiles: [{ name: 'Consumption', workloadProfileType: 'Consumption' }]
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
    zoneRedundant: false
  }
}
resource containers 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-11-15' = [for containerName in containerNames: {
  parent: database
  name: containerName
  properties: {
    resource: {
      id: containerName
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
module identities '../modules/identity.bicep' = [for workload in workloads: {
  name: '${name}-${workload.key}-identity'
  params: {
    name: '${name}-${workload.key}'
    location: location
    registryName: registryName
    cosmosName: cosmosName
    vaultName: vaultName
    databaseName: databaseName
    cosmosContainers: workload.containers
    secretNames: secretsReady ? map(workload.secrets, secret => '${name}-${secret}') : []
  }
  dependsOn: [containers]
}]
resource setupIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${name}-setup'
  location: location
}
resource openai 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = { name: openAIName }
resource deployment 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = if (enableModel) {
  parent: openai
  name: '${name}-mini'
  sku: { name: modelSku, capacity: modelCapacity }
  properties: {
    model: { format: 'OpenAI', name: modelName, version: modelVersion }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
}
module modelAccess '../modules/model-access.bicep' = if (enableApiAccess) {
  name: '${name}-model-access'
  params: {
    accountName: openAIName
    principalIds: [identities[0].outputs.principalId, identities[1].outputs.principalId]
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd')
  }
}
module telemetryAccess '../modules/telemetry-access.bicep' = if (enableApiAccess) {
  name: '${name}-telemetry-access'
  params: {
    insightsName: insightsName
    principalIds: [identities[0].outputs.principalId, identities[1].outputs.principalId]
  }
}
output shopperEnvironmentName string = shopperEnvironment.name
output shopperDomain string = shopperEnvironment.properties.defaultDomain
output modelDeploymentName string = '${name}-mini'
output runtimeContainer string = containerNames[0]
output identityContainer string = containerNames[1]
output setupIdentityId string = setupIdentity.id
