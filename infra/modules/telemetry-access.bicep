param insightsName string
param principalIds array
resource insights 'Microsoft.Insights/components@2020-02-02' existing = { name: insightsName }
resource metrics 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for principalId in principalIds: {
  scope: insights
  name: guid(insights.id, principalId, 'telemetry')
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '3913510d-42f4-4e42-8a64-420c390055eb')
  }
}]
