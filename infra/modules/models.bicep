param name string
param location string
param endpointSubnetId string
param vnetId string
param createOpenAI bool = false
param createClaude bool = false
param existingOpenAIName string = ''
param existingClaudeName string = ''
@description('Operator must verify entitlement, region, deployment SKU/quota, private inference and Entra support before enabling a model.')
@allowed([true])
param availabilityVerified bool
param deployments array = []
param claudeDeployments array = []
@description('Required for new Claude deployments; applying these attestation fields accepts Marketplace terms. No defaults representing a real organization.')
param claudeAttestation object = {}
param principalIds array = []

resource openai 'Microsoft.CognitiveServices/accounts@2025-06-01' = if (createOpenAI) {
  name: '${name}-openai'
  location: location
  kind: 'OpenAI'
  sku: { name: 'S0' }
  properties: {
    customSubDomainName: '${name}-openai'
    publicNetworkAccess: 'Disabled'
    disableLocalAuth: true
    networkAcls: { defaultAction: 'Deny' }
    restrictOutboundNetworkAccess: true
  }
}
@batchSize(1)
resource modelDeployments 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = [for deployment in deployments: if (createOpenAI) {
  parent: openai
  name: deployment.name
  sku: { name: deployment.sku, capacity: deployment.capacity }
  properties: {
    model: { format: 'OpenAI', name: deployment.model, version: deployment.version }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
}]
resource existingOpenAI 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = if (!empty(existingOpenAIName)) { name: existingOpenAIName }
resource claude 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = if (!empty(existingClaudeName)) { name: existingClaudeName }
resource newClaude 'Microsoft.CognitiveServices/accounts@2025-10-01-preview' = if (createClaude) {
  name: '${name}-claude'
  location: location
  kind: 'AIServices'
  sku: { name: 'S0' }
  identity: { type: 'SystemAssigned' }
  properties: {
    customSubDomainName: '${name}-claude'
    allowProjectManagement: true
    publicNetworkAccess: 'Disabled'
    disableLocalAuth: true
    networkAcls: { defaultAction: 'Deny' }
  }
}
resource project 'Microsoft.CognitiveServices/accounts/projects@2025-10-01-preview' = if (createClaude) {
  parent: newClaude
  name: 'commerce'
  location: location
  identity: { type: 'SystemAssigned' }
  properties: {}
}
@batchSize(1)
resource claudeModels 'Microsoft.CognitiveServices/accounts/deployments@2025-10-01-preview' = [for deployment in claudeDeployments: if (createClaude) {
  parent: newClaude
  name: deployment.name
  sku: { name: deployment.sku, capacity: deployment.capacity }
  properties: {
    model: { format: 'Anthropic', name: deployment.model, version: deployment.version }
    // Required by the first-party Azure-Samples/claude Bicep pattern; absent from Bicep 0.46.1's bundled preview types.
    #disable-next-line BCP037
    modelProviderData: claudeAttestation
    versionUpgradeOption: 'NoAutoUpgrade'
  }
  dependsOn: [project]
}]
var accounts = concat(
  createOpenAI && availabilityVerified ? [{ name: openai!.name, id: openai!.id, zone: 'privatelink.openai.azure.com' }] : [],
  !empty(existingOpenAIName) ? [{ name: existingOpenAI.name, id: existingOpenAI.id, zone: 'privatelink.openai.azure.com' }] : [],
  !empty(existingClaudeName) ? [{ name: claude.name, id: claude.id, zone: 'privatelink.services.ai.azure.com' }] : [],
  createClaude ? [{ name: newClaude!.name, id: newClaude!.id, zone: 'privatelink.services.ai.azure.com' }] : []
)
// The documented Cognitive Services User role also lists account keys. Retain
// its documented data actions only, scoped below to the dedicated Claude account.
resource claudeDataRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = if (createClaude || !empty(existingClaudeName)) {
  name: guid(resourceGroup().id, 'commerce-claude-data-plane-v1')
  properties: {
    roleName: 'Commerce Claude Data Plane ${uniqueString(resourceGroup().id)}'
    description: 'Cognitive Services data plane only; assign exclusively to a dedicated Claude account. No account keys or control-plane operations.'
    type: 'CustomRole'
    assignableScopes: [resourceGroup().id]
    permissions: [{
      actions: []
      notActions: []
      dataActions: ['Microsoft.CognitiveServices/*']
      notDataActions: []
    }]
  }
}
module endpoints './private-endpoint.bicep' = [for account in accounts: {
  name: '${account.name}-private'
  params: {
    name: '${account.name}-private'
    location: location
    resourceId: account.id
    groupId: 'account'
    zoneName: account.zone
    subnetId: endpointSubnetId
    vnetId: vnetId
  }
}]
module access './model-access.bicep' = [for account in accounts: {
  name: '${account.name}-access'
  params: {
    accountName: account.name
    principalIds: principalIds
    roleDefinitionId: account.zone == 'privatelink.openai.azure.com' ? subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd') : claudeDataRole!.id
  }
}]
var openAIBase = createOpenAI ? openai!.properties.endpoint : (!empty(existingOpenAIName) ? existingOpenAI!.properties.endpoint : '')
output openAIEndpoint string = empty(openAIBase) ? '' : '${openAIBase}${endsWith(openAIBase, '/') ? '' : '/'}openai/v1/'
output claudeEndpoint string = createClaude ? 'https://${newClaude!.name}.services.ai.azure.com/anthropic/' : (!empty(existingClaudeName) ? 'https://${claude!.name}.services.ai.azure.com/anthropic/' : '')
