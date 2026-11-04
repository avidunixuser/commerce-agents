param accountName string
param principalIds array
param roleDefinitionId string
resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = { name: accountName }
resource access 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for principalId in principalIds: {
  scope: account
  name: guid(account.id, principalId, roleDefinitionId, 'inference')
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: roleDefinitionId
  }
}]
