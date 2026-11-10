param vaultName string
param principalId string
param identityResourceId string
param secretNames array

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' existing = { name: vaultName }
resource secrets 'Microsoft.KeyVault/vaults/secrets@2023-07-01' existing = [for secretName in secretNames: {
  parent: vault
  name: secretName
}]
resource readers 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for (secretName, i) in secretNames: {
  scope: secrets[i]
  name: guid(vault.id, secretName, identityResourceId, 'secrets')
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
  }
}]
