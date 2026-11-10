param name string
param location string
param registryName string
param cosmosName string
param vaultName string
param cosmosContainers array = []
param databaseName string = 'commerce'
param imagePull bool = false
param secretNames array = []
param issuer string = ''
param subject string = ''

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: name
  location: location
}
resource federation 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = if (!empty(issuer)) {
  parent: identity
  name: 'aks'
  properties: { issuer: issuer, subject: subject, audiences: ['api://AzureADTokenExchange'] }
}
resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = { name: registryName }
resource cosmos 'Microsoft.DocumentDB/databaseAccounts@2024-11-15' existing = { name: cosmosName }
resource pull 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (imagePull) {
  scope: registry
  name: guid(registry.id, identity.id, 'pull')
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  }
}
resource dataRole 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-11-15' = [for container in cosmosContainers: {
  parent: cosmos
  name: guid(cosmos.id, identity.id, databaseName, container)
  properties: {
    principalId: identity.properties.principalId
    roleDefinitionId: '${cosmos.id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002'
    scope: '${cosmos.id}/dbs/${databaseName}/colls/${container}'
  }
}]
module secretAccess './secret-access.bicep' = if (!empty(secretNames)) {
  name: '${name}-secret-access'
  params: {
    vaultName: vaultName
    principalId: identity.properties.principalId
    identityResourceId: identity.id
    secretNames: secretNames
  }
}
output id string = identity.id
output clientId string = identity.properties.clientId
output principalId string = identity.properties.principalId
