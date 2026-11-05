param name string
param location string
param repository string
param oidcSubjectPrefix string
param dnsProxyAddress string
param privateEndpointCidr string

var tags = { managedBy: 'commerce-private-build' }
var subject = '${oidcSubjectPrefix}:environment:commerce-build:ref:refs/heads/main:event_name:workflow_dispatch:workflow_ref:${repository}/.github/workflows/private-build.yml@refs/heads/main:runner_environment:github-hosted'

resource controller 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${name}-controller'
  location: location
  tags: tags
}
resource builder 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${name}-builder'
  location: location
  tags: tags
}
resource federation 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = {
  parent: controller
  name: 'github-private-build'
  properties: {
    issuer: 'https://token.actions.githubusercontent.com'
    audiences: ['api://AzureADTokenExchange']
    subject: subject
  }
}
resource lifecycle 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(resourceGroup().id, 'commerce-private-build-lifecycle')
  properties: {
    roleName: '${name}-private-build-lifecycle'
    description: 'Disposable build VMs only; no disks, IAM, identities or application services.'
    type: 'CustomRole'
    assignableScopes: [resourceGroup().id]
    permissions: [{
      actions: [
        'Microsoft.Resources/deployments/*'
        'Microsoft.Resources/subscriptions/resourceGroups/read'
        'Microsoft.Resources/subscriptions/resourceGroups/resources/read'
        'Microsoft.Compute/virtualMachines/read'
        'Microsoft.Compute/virtualMachines/write'
        'Microsoft.Compute/virtualMachines/delete'
        'Microsoft.Compute/virtualMachines/instanceView/read'
        'Microsoft.Compute/virtualMachines/runCommands/read'
        'Microsoft.Compute/virtualMachines/runCommands/write'
        'Microsoft.Compute/virtualMachines/runCommands/delete'
        'Microsoft.Network/networkInterfaces/read'
        'Microsoft.Network/networkInterfaces/write'
        'Microsoft.Network/networkInterfaces/delete'
        'Microsoft.Network/networkInterfaces/join/action'
        'Microsoft.Network/networkSecurityGroups/read'
        'Microsoft.Network/networkSecurityGroups/join/action'
      ]
      notActions: []
      dataActions: []
      notDataActions: []
    }]
  }
}
resource lifecycleGrant 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, controller.id, lifecycle.id)
  properties: {
    principalId: controller.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: lifecycle.id
  }
}
resource builderAttachment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(builder.id, controller.id, 'builder-attachment')
  scope: builder
  properties: {
    principalId: controller.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'f1a07417-d97a-45cb-824c-7a7467783830')
  }
}
resource nsg 'Microsoft.Network/networkSecurityGroups@2024-05-01' = {
  name: '${name}-build-nic'
  location: location
  tags: tags
  properties: {
    securityRules: [
      {
        name: 'deny-inbound'
        properties: {
          priority: 100
          direction: 'Inbound'
          access: 'Deny'
          protocol: '*'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '*'
        }
      }
      {
        name: 'firewall-dns'
        properties: {
          priority: 100
          direction: 'Outbound'
          access: 'Allow'
          protocol: '*'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: dnsProxyAddress
          destinationPortRange: '53'
        }
      }
      {
        name: 'azure-platform'
        properties: {
          priority: 110
          direction: 'Outbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefixes: ['168.63.129.16/32', '169.254.169.254/32']
          destinationPortRanges: ['80', '32526']
        }
      }
      {
        name: 'private-endpoints'
        properties: {
          priority: 120
          direction: 'Outbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: privateEndpointCidr
          destinationPortRange: '443'
        }
      }
      {
        name: 'deny-lateral'
        properties: {
          priority: 130
          direction: 'Outbound'
          access: 'Deny'
          protocol: '*'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefixes: ['10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16']
          destinationPortRange: '*'
        }
      }
      {
        name: 'firewall-routed-https'
        properties: {
          priority: 140
          direction: 'Outbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: 'Internet'
          destinationPortRange: '443'
        }
      }
      {
        name: 'deny-other-outbound'
        properties: {
          priority: 4000
          direction: 'Outbound'
          access: 'Deny'
          protocol: '*'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '*'
        }
      }
    ]
  }
}
output controllerClientId string = controller.properties.clientId
output controllerPrincipalId string = controller.properties.principalId
output builderClientId string = builder.properties.clientId
output builderPrincipalId string = builder.properties.principalId
output builderIdentityId string = builder.id
output nsgId string = nsg.id
output oidcSubject string = subject
