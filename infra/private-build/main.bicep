targetScope = 'subscription'

param location string
param buildResourceGroup string
param foundationResourceGroup string
param name string
param vnetName string
param firewallPolicyName string
param registryName string
param repository string
@description('Exact immutable repo prefix returned by the GitHub OIDC settings API.')
param oidcSubjectPrefix string
param dnsProxyAddress string
param privateEndpointCidr string

resource buildGroup 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: buildResourceGroup
  location: location
  tags: { managedBy: 'commerce-private-build' }
}

module control './control.bicep' = {
  name: '${name}-control'
  scope: buildGroup
  params: {
    name: name
    location: location
    repository: repository
    oidcSubjectPrefix: oidcSubjectPrefix
    dnsProxyAddress: dnsProxyAddress
    privateEndpointCidr: privateEndpointCidr
  }
}

module network './network.bicep' = {
  name: '${name}-build-access'
  scope: resourceGroup(foundationResourceGroup)
  params: {
    name: name
    vnetName: vnetName
    firewallPolicyName: firewallPolicyName
    registryName: registryName
    controllerPrincipalId: control.outputs.controllerPrincipalId
    builderPrincipalId: control.outputs.builderPrincipalId
  }
}

output controllerClientId string = control.outputs.controllerClientId
output builderClientId string = control.outputs.builderClientId
output builderIdentityId string = control.outputs.builderIdentityId
output nsgId string = control.outputs.nsgId
output subnetId string = network.outputs.subnetId
output oidcSubject string = control.outputs.oidcSubject
