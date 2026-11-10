targetScope = 'resourceGroup'

@maxLength(12)
param name string
param location string = resourceGroup().location
param privateEnvironmentName string
param pullIdentityName string
param registryName string
param cosmosName string
param vaultName string
param databaseName string = 'commerce'
param runtimeImage string
@secure()
param oidcClientSecret string
@allowed([true])
param temporarySetupPermissionsApproved bool

resource environment 'Microsoft.App/managedEnvironments@2025-01-01' existing = { name: privateEnvironmentName }
resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = { name: '${name}-setup' }
resource pull 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = { name: pullIdentityName }
resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = { name: registryName }
resource cosmos 'Microsoft.DocumentDB/databaseAccounts@2024-11-15' existing = { name: cosmosName }
resource vault 'Microsoft.KeyVault/vaults@2023-07-01' existing = { name: vaultName }
resource writer 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: vault
  name: guid(vault.id, identity.id, 'demo-setup-secrets')
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7')
  }
}
resource setupLock 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-11-15' = {
  parent: cosmos
  name: guid(cosmos.id, identity.id, name, 'demo-setup-lock')
  properties: {
    principalId: identity.properties.principalId
    roleDefinitionId: '${cosmos.id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002'
    scope: '${cosmos.id}/dbs/${databaseName}/colls/${name}-runtime'
  }
}
resource job 'Microsoft.App/jobs@2025-01-01' = {
  name: '${name}-setup'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {}, '${pull.id}': {} }
  }
  properties: {
    environmentId: environment.id
    workloadProfileName: 'Consumption'
    configuration: {
      triggerType: 'Manual'
      replicaTimeout: 300
      replicaRetryLimit: 0
      manualTriggerConfig: { parallelism: 1, replicaCompletionCount: 1 }
      registries: [{ server: registry.properties.loginServer, identity: pull.id }]
      secrets: [{ name: 'oidc-client-secret', value: oidcClientSecret }]
    }
    template: {
      containers: [{
        name: 'setup'
        image: runtimeImage
        command: ['python', '-m', 'commerce_host.demo_setup']
        resources: { cpu: json('0.5'), memory: '1Gi' }
        env: [
          { name: 'COMMERCE_MODE', value: temporarySetupPermissionsApproved ? 'fixture' : 'disabled' }
          { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
          { name: 'COMMERCE_DEMO_VAULT_URI', value: vault.properties.vaultUri }
          { name: 'COMMERCE_DEMO_SECRET_PREFIX', value: name }
          { name: 'COMMERCE_OIDC_CLIENT_SECRET', secretRef: 'oidc-client-secret' }
          { name: 'COMMERCE_COSMOS_ENDPOINT', value: cosmos.properties.documentEndpoint }
          { name: 'COMMERCE_COSMOS_DATABASE', value: databaseName }
          { name: 'COMMERCE_COSMOS_CONTAINER', value: '${name}-runtime' }
        ]
      }]
    }
  }
  dependsOn: [writer, setupLock]
}
output setupJobName string = job.name
output temporaryVaultAssignmentId string = writer.id
output temporaryCosmosAssignmentId string = setupLock.id
