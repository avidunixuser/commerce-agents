param location string
@minLength(5)
@maxLength(50)
param name string
param runId string
param generation string
param sourceSha string
param subnetId string
param nsgId string
param builderIdentityId string
param sshPublicKey string
@description('Reviewed exact Canonical ubuntu-24_04-lts/server Gen2 version; never latest.')
param imageVersion string
param adminUsername string = 'buildoperator'

var tags = {
  managedBy: 'commerce-private-build'
  commerceRun: runId
  commerceGeneration: generation
  commerceSource: sourceSha
}
resource nic 'Microsoft.Network/networkInterfaces@2024-05-01' = {
  name: '${name}-nic'
  location: location
  tags: tags
  properties: {
    enableAcceleratedNetworking: true
    enableIPForwarding: false
    networkSecurityGroup: { id: nsgId }
    ipConfigurations: [{
      name: 'private'
      properties: {
        privateIPAllocationMethod: 'Dynamic'
        subnet: { id: subnetId }
      }
    }]
  }
}
resource vm 'Microsoft.Compute/virtualMachines@2024-11-01' = {
  name: name
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${builderIdentityId}': {} }
  }
  properties: {
    hardwareProfile: { vmSize: 'Standard_D4ds_v5' }
    storageProfile: {
      imageReference: {
        publisher: 'Canonical'
        offer: 'ubuntu-24_04-lts'
        sku: 'server'
        version: imageVersion
      }
      osDisk: {
        name: '${name}-os'
        osType: 'Linux'
        createOption: 'FromImage'
        diskSizeGB: 128
        caching: 'ReadOnly'
        deleteOption: 'Delete'
        managedDisk: { storageAccountType: 'Standard_LRS' }
        diffDiskSettings: { option: 'Local', placement: 'ResourceDisk' }
      }
      dataDisks: []
    }
    osProfile: {
      computerName: name
      adminUsername: adminUsername
      allowExtensionOperations: true
      linuxConfiguration: {
        disablePasswordAuthentication: true
        provisionVMAgent: true
        ssh: {
          publicKeys: [{
            path: '/home/${adminUsername}/.ssh/authorized_keys'
            keyData: sshPublicKey
          }]
        }
      }
    }
    securityProfile: {
      securityType: 'TrustedLaunch'
      uefiSettings: { secureBootEnabled: true, vTpmEnabled: true }
    }
    networkProfile: {
      networkInterfaces: [{
        id: nic.id
        properties: { primary: true, deleteOption: 'Detach' }
      }]
    }
    diagnosticsProfile: { bootDiagnostics: { enabled: false } }
  }
}
output vmId string = vm.id
output nicId string = nic.id
