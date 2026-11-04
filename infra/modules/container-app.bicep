param name string
param location string
param environmentId string
param workloadIdentityId string
param pullIdentityId string
param registryServer string
param image string
param command array
param env array
param secrets array = []
param exposedToVnet bool = false
param ingressAllowedCidrs array = []
param ingressEnabled bool = true
param port int = 8080
param healthPath string = '/health/ready'
param livePath string = '/health/live'
param minReplicas int = 2
param maxReplicas int = 10
param workloadProfileName string = 'Consumption'

resource app 'Microsoft.App/containerApps@2025-01-01' = {
  name: name
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: union({ '${workloadIdentityId}': {} }, { '${pullIdentityId}': {} })
  }
  properties: {
    managedEnvironmentId: environmentId
    workloadProfileName: workloadProfileName
    configuration: {
      activeRevisionsMode: 'Single'
      secrets: secrets
      registries: [{ server: registryServer, identity: pullIdentityId }]
      ingress: ingressEnabled ? {
        external: exposedToVnet
        targetPort: port
        transport: 'http'
        allowInsecure: false
        ipSecurityRestrictions: map(ingressAllowedCidrs, (cidr, i) => {
          name: 'allowed-${i}'
          action: 'Allow'
          ipAddressRange: cidr
        })
        traffic: [{ latestRevision: true, weight: 100 }]
      } : null
    }
    template: {
      terminationGracePeriodSeconds: 240
      containers: [{
        name: 'app'
        image: image
        command: command
        env: env
        resources: { cpu: json('0.5'), memory: '1Gi' }
        probes: [
          { type: 'Startup', httpGet: { path: livePath, port: port }, initialDelaySeconds: 5, periodSeconds: 5, failureThreshold: 30 }
          { type: 'Liveness', httpGet: { path: livePath, port: port }, periodSeconds: 10, failureThreshold: 3 }
          { type: 'Readiness', httpGet: { path: healthPath, port: port }, periodSeconds: 5, failureThreshold: 3 }
        ]
      }]
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
        rules: ingressEnabled ? [{ name: 'http', http: { metadata: { concurrentRequests: '40' } } }] : []
      }
    }
  }
}
output fqdn string = ingressEnabled ? app.properties.configuration.ingress.fqdn : ''
output id string = app.id
