targetScope = 'resourceGroup'

@maxLength(12)
param name string
param location string = resourceGroup().location
@allowed(['retail', 'travel', 'telecom', 'entertainment'])
param vertical string = 'retail'
@description('Full ACR repository@sha256:digest references; identical artifacts are used by AKS.')
param images object
@minLength(1)
param privateAccessCidrs array
param storefrontHost string
param merchantHost string
param certificateSecretUri string
@description('Non-secret production configuration, keyed by shopping-api, merchant-api, shopping-worker, merchant-worker, shopping-gateway, merchant-gateway.')
param runtimeEnvironment object
@description('Per-workload arrays of {name, envName, keyVaultUrl}; values are resolved with workload identity, never ARM parameter secrets.')
@secure()
param secretReferences object = {}
@allowed([true])
param modelAvailabilityVerified bool
param createOpenAI bool = false
param createClaude bool = false
param existingOpenAIName string = ''
param existingClaudeName string = ''
param modelDeployments array = []
param claudeDeployments array = []
param claudeAttestation object = {}
param additionalEgressFqdns array = []
param databaseName string = 'commerce'
param runtimeContainer string = 'runtime'
var containerNames = [runtimeContainer, 'identity']
param enableWorkloads bool = false
param enableIngress bool = false

var workloads = [
  { key: 'shopping-api', role: 'shopping', mode: 'api', containers: [runtimeContainer], secrets: false }
  { key: 'merchant-api', role: 'merchant', mode: 'api', containers: [runtimeContainer], secrets: false }
  { key: 'shopping-worker', role: 'shopping', mode: 'worker', containers: [runtimeContainer], secrets: false }
  { key: 'merchant-worker', role: 'merchant', mode: 'worker', containers: [runtimeContainer], secrets: false }
  { key: 'shopping-gateway', role: 'shopping', mode: 'gateway', containers: ['identity'], secrets: true }
  { key: 'merchant-gateway', role: 'merchant', mode: 'gateway', containers: ['identity'], secrets: true }
]
module network '../modules/network.bicep' = {
  name: 'network'
  params: { name: name, location: location, privateAccessCidrs: privateAccessCidrs, applicationFqdns: additionalEgressFqdns }
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
module pull '../modules/identity.bicep' = {
  name: 'pull-identity'
  params: {
    name: '${name}-pull'
    location: location
    registryName: services.outputs.registryName
    cosmosName: services.outputs.cosmosName
    vaultName: services.outputs.vaultName
    imagePull: true
  }
}
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
    secretNames: enableWorkloads && workload.secrets ? union(map(secretReferences[?workload.key] ?? [], secret => split(secret.keyVaultUrl, '/')[4]), []) : []
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
    principalIds: [for i in range(0, length(workloads)): identities[i].outputs.principalId]
  }
}
resource environment 'Microsoft.App/managedEnvironments@2025-01-01' = {
  name: '${name}-aca'
  location: location
  properties: {
    vnetConfiguration: { internal: true, infrastructureSubnetId: network.outputs.workloadSubnetId }
    workloadProfiles: [{ name: 'Consumption', workloadProfileType: 'Consumption' }]
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: { customerId: services.outputs.logsCustomerId, sharedKey: services.outputs.logsKey }
    }
    zoneRedundant: false
  }
}
module dns '../modules/aca-dns.bicep' = {
  name: 'aca-dns'
  params: {
    domain: environment.properties.defaultDomain
    staticIp: environment.properties.staticIp
    vnetId: network.outputs.vnetId
  }
}
module frontends '../modules/container-app.bicep' = [for role in ['shopping', 'merchant']: if (enableWorkloads) {
  name: '${role}-web'
  params: {
    name: '${name}-${role}-web'
    location: location
    environmentId: environment.id
    workloadIdentityId: pull.outputs.id
    pullIdentityId: pull.outputs.id
    registryServer: services.outputs.registryServer
    image: images['${role}Web']
    command: []
    env: [{ name: 'PORT', value: '3000' }, { name: 'HOSTNAME', value: '0.0.0.0' }, { name: 'NODE_ENV', value: 'production' }]
    port: 3000
    healthPath: '/'
    livePath: '/'
  }
}]
module apps '../modules/container-app.bicep' = [for (workload, i) in workloads: if (enableWorkloads) {
  name: workload.key
  params: {
    name: '${name}-${workload.key}'
    location: location
    environmentId: environment.id
    workloadIdentityId: identities[i].outputs.id
    pullIdentityId: pull.outputs.id
    registryServer: services.outputs.registryServer
    image: workload.mode == 'gateway' ? images.gateway : images.runtime
    command: workload.mode == 'gateway' ? ['python', '-m', 'commerce_gateway'] : ['python', '-m', 'commerce_host', workload.mode]
    ingressEnabled: workload.mode != 'worker'
    exposedToVnet: workload.mode == 'gateway'
    ingressAllowedCidrs: workload.mode == 'gateway' ? ['10.40.4.0/24'] : []
    secrets: [for secret in (secretReferences[?workload.key] ?? []): {
      name: secret.name
      keyVaultUrl: secret.keyVaultUrl
      identity: identities[i].outputs.id
    }]
    env: concat([
      { name: 'COMMERCE_ROLE', value: workload.role }
      { name: 'COMMERCE_VERTICAL', value: vertical }
      { name: 'COMMERCE_ENVIRONMENT', value: 'production' }
      { name: 'COMMERCE_MODE', value: 'production' }
      { name: 'COMMERCE_APP_ROOT', value: '/app' }
      { name: 'AZURE_CLIENT_ID', value: identities[i].outputs.clientId }
      { name: 'COMMERCE_COSMOS_ENDPOINT', value: services.outputs.cosmosEndpoint }
      { name: 'COMMERCE_COSMOS_DATABASE', value: databaseName }
      { name: 'COMMERCE_COSMOS_CONTAINER', value: runtimeContainer }
      { name: 'COMMERCE_AUTH_CONTAINER', value: 'identity' }
      { name: 'COSMOS_CONNECTION_MODE', value: 'gateway' }
      { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: services.outputs.insightsConnectionString }
      { name: 'APPLICATIONINSIGHTS_AUTHENTICATION_STRING', value: 'Authorization=AAD;ClientId=${identities[i].outputs.clientId}' }
      { name: 'OTEL_SERVICE_NAME', value: workload.key }
      { name: 'COMMERCE_STREAM_SEGMENT_SECONDS', value: '210' }
      { name: 'COMMERCE_PRIVATE_BOUNDARY', value: workload.role == 'merchant' ? 'true' : 'false' }
    ], workload.mode == 'gateway' ? [
      { name: 'COMMERCE_API_UPSTREAM', value: 'https://${name}-${workload.role}-api.internal.${environment.properties.defaultDomain}' }
      { name: 'COMMERCE_WEB_UPSTREAM', value: 'https://${name}-${workload.role}-web.internal.${environment.properties.defaultDomain}' }
      { name: 'COMMERCE_PUBLIC_ORIGIN', value: workload.role == 'shopping' ? 'https://${storefrontHost}' : 'https://${merchantHost}:8443' }
    ] : [], map(items(runtimeEnvironment[workload.key]), item => { name: item.key, value: string(item.value) }),
    map(secretReferences[?workload.key] ?? [], secret => { name: secret.envName, secretRef: secret.name }))
  }
  dependsOn: [models]
}]
module waf '../modules/application-gateway.bicep' = if (enableWorkloads && enableIngress) {
  name: 'waf'
  params: {
    name: '${name}-waf'
    location: location
    subnetId: network.outputs.gatewaySubnetId
    privateIp: network.outputs.merchantIp
    storefrontHost: storefrontHost
    merchantHost: merchantHost
    storefrontBackend: apps[4]!.outputs.fqdn
    merchantBackend: apps[5]!.outputs.fqdn
    vaultName: services.outputs.vaultName
    certificateSecretUri: certificateSecretUri
    workspaceId: services.outputs.logsId
  }
}
output registryServer string = services.outputs.registryServer
output cosmosEndpoint string = services.outputs.cosmosEndpoint
output vaultUri string = services.outputs.vaultUri
output environmentDomain string = environment.properties.defaultDomain
output storefrontIp string = enableWorkloads && enableIngress ? waf!.outputs.publicIp : ''
output merchantIp string = network.outputs.merchantIp
output openAIEndpoint string = models.outputs.openAIEndpoint
output claudeEndpoint string = models.outputs.claudeEndpoint
