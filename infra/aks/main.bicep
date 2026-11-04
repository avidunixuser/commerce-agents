targetScope = 'resourceGroup'

@maxLength(12)
param name string
param location string = resourceGroup().location
param kubernetesVersion string
@minLength(1)
param adminGroupObjectIds array
@minLength(1)
param privateAccessCidrs array
param storefrontHost string
param merchantHost string
param certificateSecretUri string
@description('Private managed Gateway load balancer DNS name, created by approved Helm release before WAF enablement.')
param storefrontGatewayFqdn string
param merchantGatewayFqdn string
param additionalEgressFqdns array = []
@allowed([true])
param modelAvailabilityVerified bool
param createOpenAI bool = false
param createClaude bool = false
param existingOpenAIName string = ''
param existingClaudeName string = ''
param modelDeployments array = []
param claudeDeployments array = []
param claudeAttestation object = {}
param databaseName string = 'commerce'
param runtimeContainer string = 'runtime'
var containerNames = [runtimeContainer, 'identity']
param nodeVmSize string = 'Standard_D4ds_v5'
param zones array = ['1', '2', '3']
param namespace string = 'commerce'
@description('Gateway-only Key Vault secret names, keyed by shopping-gateway or merchant-gateway; match Helm CSI remoteName entries. Empty when using externally managed Kubernetes secrets.')
param gatewaySecretNames object = {}
param enableIngress bool = false
@description('Enable only after installing an approved DCR transform/exclusion for managed Gateway access logs and OIDC callback query strings.')
param enableContainerInsights bool = false

module network '../modules/network.bicep' = {
  name: 'network'
  params: { name: name, location: location, privateAccessCidrs: privateAccessCidrs, applicationFqdns: additionalEgressFqdns, aks: true }
}
module services '../modules/services.bicep' = {
  name: 'services'
  params: {
    name: name
    location: location
    vnetId: network.outputs.vnetId
    endpointSubnetId: network.outputs.endpointSubnetId
    databaseName: databaseName
    containerNames: containerNames
  }
}
module nodeIdentity '../modules/identity.bicep' = {
  name: 'node-identity'
  params: {
    name: '${name}-node'
    location: location
    registryName: services.outputs.registryName
    cosmosName: services.outputs.cosmosName
    vaultName: services.outputs.vaultName
    imagePull: true
  }
}
resource controlIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = { name: '${name}-control', location: location }
resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' existing = { name: '${name}-vnet' }
resource workloadSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' existing = {
  parent: vnet
  name: 'workload'
}
resource networkRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: workloadSubnet
  name: guid(workloadSubnet.id, controlIdentity.id, 'network')
  properties: {
    principalId: controlIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4d97b98b-1d4f-4787-a291-c67834d212e7')
  }
  dependsOn: [network]
}
resource routeTable 'Microsoft.Network/routeTables@2024-05-01' existing = { name: '${name}-udr' }
resource routeTableReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: routeTable
  name: guid(routeTable.id, controlIdentity.id, 'read-routes')
  properties: {
    principalId: controlIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'acdd72a7-3385-48ef-bd42-f606fba81ae7')
  }
  dependsOn: [network]
}
resource node 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = { name: '${name}-node' }
resource managedIdentityOperator 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: node
  name: guid(node.id, controlIdentity.id, 'identity-operator')
  properties: {
    principalId: controlIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'f1a07417-d97a-45cb-824c-7a7467783830')
  }
  dependsOn: [nodeIdentity]
}
resource cluster 'Microsoft.ContainerService/managedClusters@2026-03-01' = {
  name: '${name}-aks'
  location: location
  sku: { name: 'Base', tier: 'Standard' }
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${controlIdentity.id}': {} } }
  properties: {
    kubernetesVersion: kubernetesVersion
    dnsPrefix: '${name}-aks'
    enableRBAC: true
    disableLocalAccounts: true
    aadProfile: { managed: true, enableAzureRBAC: true, adminGroupObjectIDs: adminGroupObjectIds, tenantID: tenant().tenantId }
    apiServerAccessProfile: { enablePrivateCluster: true, enablePrivateClusterPublicFQDN: false, privateDNSZone: 'system' }
    oidcIssuerProfile: { enabled: true }
    securityProfile: { workloadIdentity: { enabled: true } }
    identityProfile: {
      kubeletidentity: {
        resourceId: nodeIdentity.outputs.id
        clientId: nodeIdentity.outputs.clientId
        objectId: nodeIdentity.outputs.principalId
      }
    }
    ingressProfile: {
      gatewayAPI: { installation: 'Standard' }
      webAppRouting: { gatewayAPIImplementations: { appRoutingIstio: { mode: 'Enabled' } } }
    }
    autoUpgradeProfile: { upgradeChannel: 'stable', nodeOSUpgradeChannel: 'NodeImage' }
    networkProfile: {
      networkPlugin: 'azure'
      networkPluginMode: 'overlay'
      networkDataplane: 'cilium'
      networkPolicy: 'cilium'
      outboundType: 'userDefinedRouting'
      loadBalancerSku: 'standard'
      serviceCidr: '10.100.0.0/16'
      dnsServiceIP: '10.100.0.10'
      podCidr: '10.244.0.0/16'
    }
    agentPoolProfiles: [
      {
        name: 'system'
        mode: 'System'
        osType: 'Linux'
        osSKU: 'AzureLinux'
        vmSize: nodeVmSize
        count: 3
        enableAutoScaling: true
        minCount: 3
        maxCount: 5
        availabilityZones: zones
        vnetSubnetID: network.outputs.workloadSubnetId
        nodeTaints: ['CriticalAddonsOnly=true:NoSchedule']
        type: 'VirtualMachineScaleSets'
        upgradeSettings: { maxSurge: '33%', drainTimeoutInMinutes: 30 }
      }
      {
        name: 'workload'
        mode: 'User'
        osType: 'Linux'
        osSKU: 'AzureLinux'
        vmSize: nodeVmSize
        count: 3
        enableAutoScaling: true
        minCount: 3
        maxCount: 12
        availabilityZones: zones
        vnetSubnetID: network.outputs.workloadSubnetId
        type: 'VirtualMachineScaleSets'
        upgradeSettings: { maxSurge: '33%', drainTimeoutInMinutes: 30 }
      }
    ]
    addonProfiles: {
      azurepolicy: { enabled: true }
      azureKeyvaultSecretsProvider: { enabled: true, config: { enableSecretRotation: 'true', rotationPollInterval: '2m' } }
      omsagent: { enabled: enableContainerInsights, config: { logAnalyticsWorkspaceResourceID: services.outputs.logsId, useAADAuth: 'true' } }
    }
  }
  dependsOn: [networkRole, routeTableReader, managedIdentityOperator]
}
resource clusterMetrics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'metrics'
  scope: cluster
  properties: { workspaceId: services.outputs.logsId, metrics: [{ category: 'AllMetrics', enabled: true }] }
}
var workloads = [
  { key: 'shopping-api', containers: [runtimeContainer], secrets: false }
  { key: 'merchant-api', containers: [runtimeContainer], secrets: false }
  { key: 'shopping-worker', containers: [runtimeContainer], secrets: false }
  { key: 'merchant-worker', containers: [runtimeContainer], secrets: false }
  { key: 'shopping-gateway', containers: ['identity'], secrets: true }
  { key: 'merchant-gateway', containers: ['identity'], secrets: true }
  { key: 'shopping-web', containers: [], secrets: false }
  { key: 'merchant-web', containers: [], secrets: false }
]
module identities '../modules/identity.bicep' = [for workload in workloads: {
  name: '${workload.key}-identity'
  params: {
    name: '${name}-${workload.key}'
    location: location
    registryName: services.outputs.registryName
    cosmosName: services.outputs.cosmosName
    vaultName: services.outputs.vaultName
    databaseName: databaseName
    cosmosContainers: workload.containers
    secretNames: workload.secrets ? union(gatewaySecretNames[?workload.key] ?? [], []) : []
    issuer: cluster.properties.oidcIssuerProfile.issuerURL
    subject: 'system:serviceaccount:${namespace}:${workload.key}'
  }
}]
module models '../modules/models.bicep' = {
  name: 'models'
  params: {
    name: name
    location: location
    endpointSubnetId: network.outputs.endpointSubnetId
    vnetId: network.outputs.vnetId
    availabilityVerified: modelAvailabilityVerified
    createOpenAI: createOpenAI
    createClaude: createClaude
    existingOpenAIName: existingOpenAIName
    existingClaudeName: existingClaudeName
    deployments: modelDeployments
    claudeDeployments: claudeDeployments
    claudeAttestation: claudeAttestation
    principalIds: [for i in range(0, 4): identities[i].outputs.principalId]
  }
}
module telemetry '../modules/telemetry-access.bicep' = {
  name: 'telemetry-access'
  params: {
    insightsName: services.outputs.insightsName
    principalIds: [for i in range(0, 6): identities[i].outputs.principalId]
  }
}
module waf '../modules/application-gateway.bicep' = if (enableIngress) {
  name: 'waf'
  params: {
    name: '${name}-waf'
    location: location
    subnetId: network.outputs.gatewaySubnetId
    privateIp: network.outputs.merchantIp
    storefrontHost: storefrontHost
    merchantHost: merchantHost
    storefrontBackend: storefrontGatewayFqdn
    merchantBackend: merchantGatewayFqdn
    backendPort: 80
    backendProtocol: 'Http'
    vaultName: services.outputs.vaultName
    certificateSecretUri: certificateSecretUri
    workspaceId: services.outputs.logsId
  }
}
output clusterName string = cluster.name
output registryServer string = services.outputs.registryServer
output cosmosEndpoint string = services.outputs.cosmosEndpoint
output vaultUri string = services.outputs.vaultUri
output applicationInsightsConnectionString string = services.outputs.insightsConnectionString
output workloadIdentities array = [for (workload, i) in workloads: { name: workload.key, clientId: identities[i].outputs.clientId }]
output storefrontIp string = enableIngress ? waf!.outputs.publicIp : ''
output merchantIp string = network.outputs.merchantIp
output openAIEndpoint string = models.outputs.openAIEndpoint
output claudeEndpoint string = models.outputs.claudeEndpoint
