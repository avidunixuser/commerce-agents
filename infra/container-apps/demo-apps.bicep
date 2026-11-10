targetScope = 'resourceGroup'

@maxLength(12)
param name string
param location string = resourceGroup().location
param privateEnvironmentName string
param pullIdentityName string
param registryName string
param cosmosName string
param vaultName string
param openAIName string
param insightsName string
param databaseName string = 'commerce'
param oidcClientId string
param providerId string
param operatorObjectId string
param privateAccessCidrs array
param images object
@description('Enable only after reading and verifying the existing operator-only auth configuration. Start false.')
param enablePublicIngress bool = false

var roles = ['shopping', 'merchant']
var secretTypes = [
  { suffix: 'oidc-client-secret', envName: 'COMMERCE_OIDC_CLIENT_SECRET' }
  { suffix: 'signing-key', envName: 'COMMERCE_GATEWAY_SIGNING_KEY' }
  { suffix: 'encryption-keys', envName: 'COMMERCE_AUTH_ENCRYPTION_KEYS' }
]
resource shopperEnvironment 'Microsoft.App/managedEnvironments@2025-01-01' existing = { name: '${name}-shopper' }
resource merchantEnvironment 'Microsoft.App/managedEnvironments@2025-01-01' existing = { name: privateEnvironmentName }
resource pull 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = { name: pullIdentityName }
resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = { name: registryName }
resource cosmos 'Microsoft.DocumentDB/databaseAccounts@2024-11-15' existing = { name: cosmosName }
resource vault 'Microsoft.KeyVault/vaults@2023-07-01' existing = { name: vaultName }
resource insights 'Microsoft.Insights/components@2020-02-02' existing = { name: insightsName }
resource apiIdentities 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = [for role in roles: { name: '${name}-${role}-api' }]
resource gatewayIdentities 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = [for role in roles: { name: '${name}-${role}-gateway' }]
var environmentIds = [shopperEnvironment.id, merchantEnvironment.id]
var domains = [shopperEnvironment.properties.defaultDomain, merchantEnvironment.properties.defaultDomain]
var origins = [
  'https://${name}-shopping-gateway.${domains[0]}'
  'https://${name}-merchant-gateway.${domains[1]}'
]
var issuer = 'https://login.microsoftonline.com/${tenant().tenantId}/v2.0'
var jwksUrl = 'https://login.microsoftonline.com/${tenant().tenantId}/discovery/v2.0/keys'

module frontends '../modules/container-app.bicep' = [for (role, i) in roles: {
  name: '${name}-${role}-web'
  params: {
    name: '${name}-${role}-web'
    location: location
    environmentId: environmentIds[i]
    workloadIdentityId: pull.id
    pullIdentityId: pull.id
    registryServer: registry.properties.loginServer
    image: images['${role}Web']
    command: []
    env: [{ name: 'PORT', value: '3000' }, { name: 'HOSTNAME', value: '0.0.0.0' }, { name: 'NODE_ENV', value: 'production' }]
    port: 3000
    healthPath: '/'
    livePath: '/'
    minReplicas: 1
    maxReplicas: 1
  }
}]
module apis '../modules/container-app.bicep' = [for (role, i) in roles: {
  name: '${name}-${role}-api'
  params: {
    name: '${name}-${role}-api'
    location: location
    environmentId: environmentIds[i]
    workloadIdentityId: apiIdentities[i].id
    pullIdentityId: pull.id
    registryServer: registry.properties.loginServer
    image: images.runtime
    command: ['python', '-m', 'commerce_host', 'worker']
    minReplicas: 1
    maxReplicas: 1
    env: concat([
      { name: 'COMMERCE_MODE', value: 'fixture' }
      { name: 'COMMERCE_ROLE', value: role }
      { name: 'COMMERCE_VERTICAL', value: 'retail' }
      { name: 'COMMERCE_APP_ROOT', value: '/app' }
      { name: 'AZURE_CLIENT_ID', value: apiIdentities[i].properties.clientId }
      { name: 'COMMERCE_TELEMETRY', value: 'azure' }
      { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: insights.properties.ConnectionString }
      { name: 'OTEL_SERVICE_NAME', value: '${name}-${role}-api' }
      { name: 'COMMERCE_COSMOS_ENDPOINT', value: cosmos.properties.documentEndpoint }
      { name: 'COMMERCE_COSMOS_DATABASE', value: databaseName }
      { name: 'COMMERCE_COSMOS_CONTAINER', value: '${name}-runtime' }
      { name: 'COMMERCE_AUTH_ISSUER', value: issuer }
      { name: 'COMMERCE_AUTH_AUDIENCE', value: oidcClientId }
      { name: 'COMMERCE_AUTH_JWKS_URL', value: jwksUrl }
      { name: 'COMMERCE_ALLOWED_TENANTS', value: tenant().tenantId }
      { name: 'COMMERCE_PROVIDER', value: 'azure_openai' }
      { name: 'COMMERCE_MODEL', value: '${name}-mini' }
      { name: 'AZURE_OPENAI_ENDPOINT', value: 'https://${openAIName}.openai.azure.com/openai/v1/' }
      { name: 'COMMERCE_PROVIDER_ID', value: providerId }
      { name: 'COMMERCE_BACKEND_FACTORY', value: 'commerce_host.fixtures:build' }
      { name: 'COMMERCE_PORTABLE_TOOLS', value: 'true' }
      { name: 'COMMERCE_STREAM_SECONDS', value: '210' }
    ], role == 'shopping' ? [
      { name: 'COMMERCE_GUEST_ISSUER', value: origins[0] }
      { name: 'COMMERCE_GUEST_JWKS_URL', value: '${origins[0]}/.well-known/jwks.json' }
    ] : [])
  }
}]
module gateways '../modules/container-app.bicep' = [for (role, i) in roles: {
  name: '${name}-${role}-gateway'
  params: {
    name: '${name}-${role}-gateway'
    location: location
    environmentId: environmentIds[i]
    workloadIdentityId: gatewayIdentities[i].id
    pullIdentityId: pull.id
    registryServer: registry.properties.loginServer
    image: images.gateway
    command: ['python', '-m', 'commerce_gateway']
    exposedToVnet: role == 'merchant' || enablePublicIngress
    ingressAllowedCidrs: role == 'merchant' ? privateAccessCidrs : []
    minReplicas: 1
    maxReplicas: 1
    secrets: [for secret in secretTypes: {
      name: secret.suffix
      keyVaultUrl: '${vault.properties.vaultUri}secrets/${name}-${secret.suffix == 'oidc-client-secret' ? secret.suffix : '${role}-${secret.suffix}'}'
      identity: gatewayIdentities[i].id
    }]
    env: concat([
      { name: 'COMMERCE_MODE', value: 'fixture' }
      { name: 'COMMERCE_ROLE', value: role }
      { name: 'AZURE_CLIENT_ID', value: gatewayIdentities[i].properties.clientId }
      { name: 'COMMERCE_PUBLIC_ORIGIN', value: origins[i] }
      { name: 'COMMERCE_GATEWAY_ISSUER', value: origins[i] }
      { name: 'COMMERCE_PRIVATE_BOUNDARY', value: role == 'merchant' ? 'true' : 'false' }
      { name: 'COMMERCE_API_UPSTREAM', value: 'https://${name}-${role}-api.internal.${domains[i]}' }
      { name: 'COMMERCE_WEB_UPSTREAM', value: 'https://${name}-${role}-web.internal.${domains[i]}' }
      { name: 'COMMERCE_OIDC_ISSUER', value: issuer }
      { name: 'COMMERCE_OIDC_CLIENT_ID', value: oidcClientId }
      { name: 'COMMERCE_OIDC_API_AUDIENCE', value: oidcClientId }
      { name: 'COMMERCE_OIDC_SCOPE', value: 'openid profile api://${oidcClientId}/access_as_user' }
      { name: 'COMMERCE_TENANT_ID', value: tenant().tenantId }
      { name: 'COMMERCE_GATEWAY_SIGNING_KID', value: '${name}-${role}-1' }
      { name: 'COMMERCE_COSMOS_ENDPOINT', value: cosmos.properties.documentEndpoint }
      { name: 'COMMERCE_COSMOS_DATABASE', value: databaseName }
      { name: 'COMMERCE_AUTH_CONTAINER', value: '${name}-identity' }
    ], map(secretTypes, secret => { name: secret.envName, secretRef: secret.suffix }))
  }
  dependsOn: [apis, frontends]
}]
resource shopperGateway 'Microsoft.App/containerApps@2025-01-01' existing = { name: '${name}-shopping-gateway' }
resource authentication 'Microsoft.App/containerApps/authConfigs@2025-01-01' = {
  parent: shopperGateway
  name: 'current'
  properties: {
    platform: { enabled: true }
    httpSettings: { requireHttps: true }
    globalValidation: {
      unauthenticatedClientAction: 'RedirectToLoginPage'
      redirectToProvider: 'azureactivedirectory'
      excludedPaths: ['/health/live', '/health/ready', '/.well-known/jwks.json']
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          clientId: oidcClientId
          clientSecretSettingName: 'oidc-client-secret'
          openIdIssuer: issuer
        }
        validation: {
          allowedAudiences: [oidcClientId]
          defaultAuthorizationPolicy: {
            allowedPrincipals: { identities: [operatorObjectId] }
          }
        }
      }
    }
  }
  dependsOn: [gateways]
}
output shopperOrigin string = origins[0]
output merchantPrivateOrigin string = origins[1]
output publicIngressEnabled bool = enablePublicIngress
