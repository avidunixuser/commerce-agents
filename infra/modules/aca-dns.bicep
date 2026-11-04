param domain string
param staticIp string
param vnetId string
resource dns 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: domain
  location: 'global'
}
resource link 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: dns
  name: 'aca'
  location: 'global'
  properties: { registrationEnabled: false, virtualNetwork: { id: vnetId } }
}
resource wildcard 'Microsoft.Network/privateDnsZones/A@2024-06-01' = {
  parent: dns
  name: '*'
  properties: { ttl: 60, aRecords: [{ ipv4Address: staticIp }] }
}
