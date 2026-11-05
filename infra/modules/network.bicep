param name string
param location string
param privateAccessCidrs array
param applicationFqdns array
param aks bool = false
@description('Existing workload, endpoints and runners subnet NSG IDs to preserve during incremental deployment. Their rules remain externally managed.')
param existingSubnetNsgIds object = {}
var vnetPrefix = '10.40.0.0/16'
var workloadNsgId = existingSubnetNsgIds[?'workload'] ?? ''
var endpointsNsgId = existingSubnetNsgIds[?'endpoints'] ?? ''
var runnersNsgId = existingSubnetNsgIds[?'runners'] ?? ''

var firewallIp = '10.40.0.4'
var merchantIp = '10.40.4.10'

resource policy 'Microsoft.Network/firewallPolicies@2024-05-01' = {
  name: '${name}-egress'
  location: location
  properties: {
    sku: { tier: 'Standard' }
    threatIntelMode: 'Deny'
    dnsSettings: { enableProxy: true }
  }
}
resource rules 'Microsoft.Network/firewallPolicies/ruleCollectionGroups@2024-05-01' = {
  parent: policy
  name: 'platform'
  properties: {
    priority: 100
    ruleCollections: [
      {
        name: 'https-dependencies'
        priority: 100
        ruleCollectionType: 'FirewallPolicyFilterRuleCollection'
        action: { type: 'Allow' }
        rules: concat([
          {
            name: 'approved-https'
            ruleType: 'ApplicationRule'
            sourceAddresses: [vnetPrefix]
            protocols: [{ protocolType: 'Https', port: 443 }]
            targetFqdns: concat([
              replace(replace(environment().authentication.loginEndpoint, 'https://', ''), '/', '')
              replace(replace(environment().resourceManager, 'https://', ''), '/', '')
              'mcr.microsoft.com'
              '*.data.mcr.microsoft.com'
              'packages.microsoft.com'
              'packages.aks.azure.com'
              'acs-mirror.azureedge.net'
              '*.identity.azure.net'
              'login.microsoft.com'
              '*.login.microsoft.com'
              '*.${replace(replace(environment().authentication.loginEndpoint, 'https://', ''), '/', '')}'
              '*.monitor.azure.com'
              '*.ods.opinsights.azure.com'
              '*.oms.opinsights.azure.com'
              '*.applicationinsights.azure.com'
              '*.applicationinsights.microsoft.com'
            ], applicationFqdns)
          }
        ], aks ? [{
            name: 'managed-platform'
            ruleType: 'ApplicationRule'
            sourceAddresses: [vnetPrefix]
            protocols: [{ protocolType: 'Https', port: 443 }]
            fqdnTags: ['AzureKubernetesService']
          }] : [])
      }
      {
        name: 'platform-network'
        priority: 200
        ruleCollectionType: 'FirewallPolicyFilterRuleCollection'
        action: { type: 'Allow' }
        rules: aks ? [
          {
            name: 'aks-control-plane'
            ruleType: 'NetworkRule'
            sourceAddresses: [vnetPrefix]
            destinationAddresses: ['AzureCloud.${location}']
            destinationPorts: ['1194', '9000']
            ipProtocols: ['UDP', 'TCP']
          }
          {
            name: 'time'
            ruleType: 'NetworkRule'
            sourceAddresses: [vnetPrefix]
            destinationFqdns: ['ntp.ubuntu.com']
            destinationPorts: ['123']
            ipProtocols: ['UDP']
          }
        ] : [
          {
            name: 'aca-control-plane'
            ruleType: 'NetworkRule'
            sourceAddresses: [vnetPrefix]
            destinationAddresses: ['AzureActiveDirectory', 'Storage.${location}']
            destinationPorts: ['443']
            ipProtocols: ['TCP']
          }
        ]
      }
    ]
  }
}
resource route 'Microsoft.Network/routeTables@2024-05-01' = {
  name: '${name}-udr'
  location: location
  properties: {
    disableBgpRoutePropagation: false
    routes: [{
      name: 'default-firewall'
      properties: {
        addressPrefix: '0.0.0.0/0'
        nextHopType: 'VirtualAppliance'
        nextHopIpAddress: firewallIp
      }
    }]
  }
}
resource gatewayNsg 'Microsoft.Network/networkSecurityGroups@2024-05-01' = {
  name: '${name}-gateway'
  location: location
  properties: {
    securityRules: [
      {
        name: 'merchant-private-connectivity'
        properties: {
          priority: 100
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefixes: privateAccessCidrs
          sourcePortRange: '*'
          destinationAddressPrefix: merchantIp
          destinationPortRange: '8443'
        }
      }
      {
        name: 'deny-other-merchant'
        properties: {
          priority: 110
          direction: 'Inbound'
          access: 'Deny'
          protocol: '*'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '8443'
        }
      }
      {
        name: 'storefront-https'
        properties: {
          priority: 120
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: 'Internet'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '443'
        }
      }
      {
        name: 'gateway-manager'
        properties: {
          priority: 130
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: 'GatewayManager'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '65200-65535'
        }
      }
      {
        name: 'azure-load-balancer'
        properties: {
          priority: 140
          direction: 'Inbound'
          access: 'Allow'
          protocol: '*'
          sourceAddressPrefix: 'AzureLoadBalancer'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '*'
        }
      }
      {
        name: 'deny-other-inbound'
        properties: {
          priority: 4000
          direction: 'Inbound'
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
resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: '${name}-vnet'
  location: location
  properties: {
    addressSpace: { addressPrefixes: [vnetPrefix] }
    dhcpOptions: { dnsServers: [firewallIp] }
    subnets: [
      { name: 'AzureFirewallSubnet', properties: { addressPrefix: '10.40.0.0/26' } }
      {
        name: 'workload'
        properties: union({
          addressPrefix: aks ? '10.40.8.0/22' : '10.40.2.0/23'
          routeTable: { id: route.id }
          delegations: aks ? [] : [{
            name: 'container-apps'
            properties: { serviceName: 'Microsoft.App/environments' }
          }]
        }, empty(workloadNsgId) ? {} : { networkSecurityGroup: { id: workloadNsgId } })
      }
      {
        name: 'endpoints'
        properties: union({
          addressPrefix: '10.40.5.0/24'
          privateEndpointNetworkPolicies: 'Disabled'
        }, empty(endpointsNsgId) ? {} : { networkSecurityGroup: { id: endpointsNsgId } })
      }
      { name: 'gateway', properties: { addressPrefix: '10.40.4.0/24', networkSecurityGroup: { id: gatewayNsg.id } } }
      {
        name: 'runners'
        properties: union({
          addressPrefix: '10.40.6.0/24'
          routeTable: { id: route.id }
        }, empty(runnersNsgId) ? {} : { networkSecurityGroup: { id: runnersNsgId } })
      }
    ]
  }
}
resource pip 'Microsoft.Network/publicIPAddresses@2024-05-01' = {
  name: '${name}-firewall'
  location: location
  sku: { name: 'Standard' }
  properties: { publicIPAllocationMethod: 'Static' }
}
resource firewall 'Microsoft.Network/azureFirewalls@2024-05-01' = {
  name: '${name}-firewall'
  location: location
  properties: {
    sku: { name: 'AZFW_VNet', tier: 'Standard' }
    firewallPolicy: { id: policy.id }
    ipConfigurations: [{
      name: 'primary'
      properties: {
        subnet: { id: '${vnet.id}/subnets/AzureFirewallSubnet' }
        publicIPAddress: { id: pip.id }
      }
    }]
  }
}
output vnetId string = vnet.id
output workloadSubnetId string = '${vnet.id}/subnets/workload'
output endpointSubnetId string = '${vnet.id}/subnets/endpoints'
output gatewaySubnetId string = '${vnet.id}/subnets/gateway'
output merchantIp string = merchantIp
output firewallId string = firewall.id
output dnsServer string = firewallIp
