param name string
param vnetName string
param firewallPolicyName string
param registryName string
param controllerPrincipalId string
param builderPrincipalId string

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' existing = { name: vnetName }
resource subnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' existing = {
  parent: vnet
  name: 'runners'
}
resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = { name: registryName }
resource policy 'Microsoft.Network/firewallPolicies@2024-05-01' existing = { name: firewallPolicyName }
resource joinRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(resourceGroup().id, 'commerce-private-build-subnet-join')
  properties: {
    roleName: '${name}-private-build-subnet-join'
    description: 'Read/join only; assignment restricted to the existing runners subnet.'
    type: 'CustomRole'
    assignableScopes: [resourceGroup().id]
    permissions: [{
      actions: [
        'Microsoft.Network/virtualNetworks/subnets/read'
        'Microsoft.Network/virtualNetworks/subnets/join/action'
      ]
      notActions: []
      dataActions: []
      notDataActions: []
    }]
  }
}
resource subnetGrant 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(subnet.id, controllerPrincipalId, joinRole.id)
  scope: subnet
  properties: {
    principalId: controllerPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: joinRole.id
  }
}
resource imageGrant 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, builderPrincipalId, 'private-build-push')
  scope: registry
  properties: {
    principalId: builderPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '8311e382-0749-4cb8-b61a-304f252e45ec')
  }
}
resource buildRules 'Microsoft.Network/firewallPolicies/ruleCollectionGroups@2024-05-01' = {
  parent: policy
  name: 'private-build'
  properties: {
    priority: 200
    ruleCollections: [{
      name: 'private-build-https'
      priority: 100
      ruleCollectionType: 'FirewallPolicyFilterRuleCollection'
      action: { type: 'Allow' }
      rules: [{
        name: 'reviewed-build-dependencies'
        ruleType: 'ApplicationRule'
        sourceAddresses: [subnet.properties.addressPrefix]
        protocols: [{ protocolType: 'Https', port: 443 }]
        targetFqdns: [
          replace(replace(environment().resourceManager, 'https://', ''), '/', '')
          replace(replace(environment().authentication.loginEndpoint, 'https://', ''), '/', '')
          'github.com'
          'codeload.github.com'
          'azure.archive.ubuntu.com'
          'archive.ubuntu.com'
          'security.ubuntu.com'
          'packages.microsoft.com'
          'auth.docker.io'
          'registry-1.docker.io'
          'production.cloudfront.docker.com'
          'production.cloudflare.docker.com'
          'docker-images-prod.6aa30f8b08e16409b46e0173d6de2f56.r2.cloudflarestorage.com'
          'pypi.org'
          'files.pythonhosted.org'
          'registry.npmjs.org'
          'ms-feed-25.pkgs.visualstudio.com'
          '*.vsblob.vsassets.io'
        ]
      }]
    }]
  }
}
output subnetId string = subnet.id
