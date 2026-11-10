targetScope = 'resourceGroup'

@minLength(3)
@maxLength(12)
param name string
param openAIName string
param insightsName string
param vaultName string
param modelName string
param modelVersion string
param enableModel bool = false
param secretsReady bool = false
@allowed(['Standard'])
param modelSku string = 'Standard'
@minValue(1)
@maxValue(100)
param modelCapacity int = 100

@sealed()
type DemoPrincipals = {
  shoppingApi: string
  merchantApi: string
  shoppingGateway: string
  merchantGateway: string
}

@description('Literal principal IDs from a fresh, tenant/resource-bound managed-identity inventory.')
param principalIds DemoPrincipals

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
module modelAccess '../modules/model-access.bicep' = {
  name: '${name}-model-access'
  params: {
    accountName: openAIName
    principalIds: [principalIds.shoppingApi, principalIds.merchantApi]
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd')
  }
}
module telemetryAccess '../modules/telemetry-access.bicep' = {
  name: '${name}-telemetry-access'
  params: {
    insightsName: insightsName
    principalIds: [principalIds.shoppingApi, principalIds.merchantApi]
  }
}
var gateways = [
  { role: 'shopping', principalId: principalIds.shoppingGateway }
  { role: 'merchant', principalId: principalIds.merchantGateway }
]
module secretAccess '../modules/secret-access.bicep' = [for gateway in gateways: if (secretsReady) {
  name: '${name}-${gateway.role}-gateway-secret-access'
  params: {
    vaultName: vaultName
    principalId: gateway.principalId
    identityResourceId: resourceId('Microsoft.ManagedIdentity/userAssignedIdentities', '${name}-${gateway.role}-gateway')
    secretNames: ['${name}-oidc-client-secret', '${name}-${gateway.role}-signing-key', '${name}-${gateway.role}-encryption-keys']
  }
}]
output modelDeploymentName string = '${name}-mini'
