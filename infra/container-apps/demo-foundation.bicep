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
param registryName string
param cosmosName string
param vaultName string
param databaseName string = 'commerce'

var containerNames = ['${name}-runtime', '${name}-identity']
var workloads = [
  { key: 'shopping-api', containers: [containerNames[0]] }
  { key: 'merchant-api', containers: [containerNames[0]] }
  { key: 'shopping-gateway', containers: [containerNames[1]] }
  { key: 'merchant-gateway', containers: [containerNames[1]] }
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
  }
  dependsOn: [containers]
}]
resource setupIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${name}-setup'
  location: location
}
output shopperEnvironmentName string = shopperEnvironment.name
output shopperDomain string = shopperEnvironment.properties.defaultDomain
output runtimeContainer string = containerNames[0]
output identityContainer string = containerNames[1]
output setupIdentityId string = setupIdentity.id
