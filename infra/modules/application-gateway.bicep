param name string
param location string
param subnetId string
param privateIp string
param storefrontHost string
param merchantHost string
param storefrontBackend string
param merchantBackend string
param backendPort int = 443
@allowed(['Http', 'Https'])
param backendProtocol string = 'Https'
param vaultName string
param certificateSecretUri string
param workspaceId string

var gatewayId = resourceId('Microsoft.Network/applicationGateways', name)
var roles = ['shopping', 'merchant']
resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${name}-tls'
  location: location
}
resource vault 'Microsoft.KeyVault/vaults@2023-07-01' existing = { name: vaultName }
resource certificateSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' existing = {
  parent: vault
  name: split(certificateSecretUri, '/')[4]
}
resource certAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: certificateSecret
  name: guid(certificateSecret.id, identity.id, 'tls')
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
  }
}
resource publicPolicy 'Microsoft.Network/ApplicationGatewayWebApplicationFirewallPolicies@2024-05-01' = {
  name: '${name}-public'
  location: location
  properties: {
    policySettings: { state: 'Enabled', mode: 'Prevention', requestBodyCheck: true }
    managedRules: { managedRuleSets: [{ ruleSetType: 'OWASP', ruleSetVersion: '3.2' }] }
    customRules: [{
      name: 'NeverPublicMerchant'
      priority: 1
      ruleType: 'MatchRule'
      action: 'Block'
      matchConditions: [{
        matchVariables: [{ variableName: 'RequestUri' }]
        operator: 'Contains'
        matchValues: ['/merchant']
        transforms: ['UrlDecode', 'Lowercase']
      }]
    }]
  }
}
resource privatePolicy 'Microsoft.Network/ApplicationGatewayWebApplicationFirewallPolicies@2024-05-01' = {
  name: '${name}-private'
  location: location
  properties: {
    policySettings: { state: 'Enabled', mode: 'Prevention', requestBodyCheck: true }
    managedRules: { managedRuleSets: [{ ruleSetType: 'OWASP', ruleSetVersion: '3.2' }] }
  }
}
resource pip 'Microsoft.Network/publicIPAddresses@2024-05-01' = {
  name: '${name}-public'
  location: location
  sku: { name: 'Standard' }
  properties: { publicIPAllocationMethod: 'Static' }
}
resource gateway 'Microsoft.Network/applicationGateways@2024-05-01' = {
  name: name
  location: location
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${identity.id}': {} } }
  properties: {
    sku: { name: 'WAF_v2', tier: 'WAF_v2' }
    autoscaleConfiguration: { minCapacity: 2, maxCapacity: 10 }
    enableHttp2: true
    sslPolicy: { policyType: 'Predefined', policyName: 'AppGwSslPolicy20220101S' }
    firewallPolicy: { id: privatePolicy.id }
    gatewayIPConfigurations: [{ name: 'subnet', properties: { subnet: { id: subnetId } } }]
    frontendIPConfigurations: [
      { name: 'public', properties: { publicIPAddress: { id: pip.id } } }
      {
        name: 'private'
        properties: { subnet: { id: subnetId }, privateIPAllocationMethod: 'Static', privateIPAddress: privateIp }
      }
    ]
    frontendPorts: [
      { name: 'shopping', properties: { port: 443 } }
      { name: 'merchant', properties: { port: 8443 } }
    ]
    sslCertificates: [{ name: 'tls', properties: { keyVaultSecretId: certificateSecretUri } }]
    backendAddressPools: [for role in roles: {
      name: role
      properties: { backendAddresses: [{ fqdn: role == 'shopping' ? storefrontBackend : merchantBackend }] }
    }]
    backendHttpSettingsCollection: [for role in roles: {
      name: role
      properties: {
        port: backendPort
        protocol: backendProtocol
        cookieBasedAffinity: 'Disabled'
        requestTimeout: 230
        connectionDraining: { enabled: true, drainTimeoutInSec: 240 }
        pickHostNameFromBackendAddress: backendProtocol == 'Https'
        hostName: backendProtocol == 'Http' ? (role == 'shopping' ? storefrontHost : merchantHost) : null
        probe: { id: '${gatewayId}/probes/${role}' }
      }
    }]
    probes: [for role in roles: {
      name: role
      properties: {
        protocol: backendProtocol
        path: '/health/ready'
        pickHostNameFromBackendHttpSettings: true
        interval: 30
        timeout: 10
        unhealthyThreshold: 3
        match: { statusCodes: ['200-299'] }
      }
    }]
    httpListeners: [for role in roles: {
      name: role
      properties: {
        frontendIPConfiguration: { id: '${gatewayId}/frontendIPConfigurations/${role == 'shopping' ? 'public' : 'private'}' }
        frontendPort: { id: '${gatewayId}/frontendPorts/${role}' }
        protocol: 'Https'
        hostName: role == 'shopping' ? storefrontHost : merchantHost
        requireServerNameIndication: true
        sslCertificate: { id: '${gatewayId}/sslCertificates/tls' }
        firewallPolicy: { id: role == 'shopping' ? publicPolicy.id : privatePolicy.id }
      }
    }]
    requestRoutingRules: [for (role, i) in roles: {
      name: role
      properties: {
        ruleType: 'Basic'
        priority: 100 + i
        httpListener: { id: '${gatewayId}/httpListeners/${role}' }
        backendAddressPool: { id: '${gatewayId}/backendAddressPools/${role}' }
        backendHttpSettings: { id: '${gatewayId}/backendHttpSettingsCollection/${role}' }
      }
    }]
  }
  dependsOn: [certAccess]
}
output id string = gateway.id
resource metrics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'metrics'
  scope: gateway
  properties: { workspaceId: workspaceId, metrics: [{ category: 'AllMetrics', enabled: true }] }
}
output publicIp string = pip.properties.ipAddress
