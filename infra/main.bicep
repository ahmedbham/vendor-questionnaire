@description('Short unique prefix for resource names')
param prefix string
param location string = resourceGroup().location
@secure()
param sessionSecret string
@secure()
@description('Output of app.security.hash_password for the admin password')
param adminPasswordHash string
@secure()
@description('Optional base32 TOTP secret for admin MFA')
param adminTotpSecret string = ''

var suffix = uniqueString(resourceGroup().id, prefix)
var containers = ['vendors', 'invites', 'otps', 'responses', 'settings', 'audit']
var roleKvSecretsUser = '4633458b-17de-408a-b874-0445c86b69e6'
var roleContributor = 'b24988ac-6180-42a0-ab88-20f7382dd24c'
var cosmosDataContributor = '00000000-0000-0000-0000-000000000002'

resource plan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: '${prefix}-plan'
  location: location
  kind: 'linux'
  sku: { name: 'B1' }
  properties: { reserved: true }
}

resource logs 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: '${prefix}-logs'
  location: location
  properties: { sku: { name: 'PerGB2018' } }
}

resource ai 'Microsoft.Insights/components@2020-02-02' = {
  name: '${prefix}-ai'
  location: location
  kind: 'web'
  properties: { Application_Type: 'web', WorkspaceResourceId: logs.id }
}

resource kv 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: '${prefix}kv${suffix}'
  location: location
  properties: {
    sku: { family: 'A', name: 'standard' }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
    enableSoftDelete: true
    publicNetworkAccess: 'Disabled'
  }
}

resource secSession 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: kv
  name: 'session-secret'
  properties: { value: sessionSecret }
}
resource secAdmin 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: kv
  name: 'admin-password-hash'
  properties: { value: adminPasswordHash }
}
resource secTotp 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (!empty(adminTotpSecret)) {
  parent: kv
  name: 'admin-totp-secret'
  properties: { value: adminTotpSecret }
}

resource cosmos 'Microsoft.DocumentDB/databaseAccounts@2024-05-15' = {
  name: '${prefix}-cosmos-${suffix}'
  location: location
  properties: {
    databaseAccountOfferType: 'Standard'
    locations: [{ locationName: location, failoverPriority: 0 }]
    capabilities: [{ name: 'EnableServerless' }]
    disableLocalAuth: true
    publicNetworkAccess: 'Disabled'
  }
}

resource db 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-05-15' = {
  parent: cosmos
  name: 'questionnaire'
  properties: { resource: { id: 'questionnaire' } }
}

resource cont 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = [for c in containers: {
  parent: db
  name: c
  properties: {
    resource: { id: c, partitionKey: { paths: ['/vendor_id'], kind: 'Hash' } }
  }
}]

resource emailSvc 'Microsoft.Communication/emailServices@2023-04-01' = {
  name: '${prefix}-email-${suffix}'
  location: 'global'
  properties: { dataLocation: 'United States' }
}
resource emailDomain 'Microsoft.Communication/emailServices/domains@2023-04-01' = {
  parent: emailSvc
  name: 'AzureManagedDomain'
  location: 'global'
  properties: { domainManagement: 'AzureManaged' }
}
resource acs 'Microsoft.Communication/communicationServices@2023-04-01' = {
  name: '${prefix}-acs-${suffix}'
  location: 'global'
  properties: { dataLocation: 'United States', linkedDomains: [emailDomain.id] }
}

// Cosmos DB and Key Vault are reachable only through private endpoints (public access disabled).
resource vnet 'Microsoft.Network/virtualNetworks@2023-11-01' = {
  name: '${prefix}-vnet'
  location: location
  properties: {
    addressSpace: { addressPrefixes: ['10.40.0.0/16'] }
    subnets: [
      {
        name: 'app'
        properties: {
          addressPrefix: '10.40.0.0/24'
          delegations: [{ name: 'web', properties: { serviceName: 'Microsoft.Web/serverFarms' } }]
        }
      }
      { name: 'pe', properties: { addressPrefix: '10.40.1.0/24' } }
    ]
  }
}

var peSubnetId = '${vnet.id}/subnets/pe'
var privateLinks = [
  { key: 'cosmos', zone: 'privatelink.documents.azure.com', targetId: cosmos.id, groupId: 'Sql' }
  { key: 'kv', zone: 'privatelink.vaultcore.azure.net', targetId: kv.id, groupId: 'vault' }
]

resource zones 'Microsoft.Network/privateDnsZones@2020-06-01' = [for l in privateLinks: {
  name: l.zone
  location: 'global'
}]

resource zoneLinks 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = [for (l, i) in privateLinks: {
  parent: zones[i]
  name: '${prefix}-link'
  location: 'global'
  properties: { virtualNetwork: { id: vnet.id }, registrationEnabled: false }
}]

resource pes 'Microsoft.Network/privateEndpoints@2023-11-01' = [for l in privateLinks: {
  name: '${prefix}-pe-${l.key}'
  location: location
  properties: {
    subnet: { id: peSubnetId }
    privateLinkServiceConnections: [{
      name: l.key
      properties: { privateLinkServiceId: l.targetId, groupIds: [l.groupId] }
    }]
  }
}]

resource peDns 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2023-11-01' = [for (l, i) in privateLinks: {
  parent: pes[i]
  name: 'default'
  properties: { privateDnsZoneConfigs: [{ name: l.key, properties: { privateDnsZoneId: zones[i].id } }] }
}]

resource app 'Microsoft.Web/sites@2023-12-01' = {
  name: '${prefix}-app-${suffix}'
  location: location
  identity: { type: 'SystemAssigned' }
  dependsOn: [peDns]
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    virtualNetworkSubnetId: '${vnet.id}/subnets/app'
    vnetRouteAllEnabled: true
    siteConfig: {
      linuxFxVersion: 'PYTHON|3.12'
      appCommandLine: 'gunicorn -k uvicorn.workers.UvicornWorker -w 2 -b 0.0.0.0:8000 app.main:app'
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      appSettings: [
        { name: 'SCM_DO_BUILD_DURING_DEPLOYMENT', value: 'true' }
        { name: 'WEBSITES_PORT', value: '8000' }
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: ai.properties.ConnectionString }
        { name: 'HTTPS_ONLY', value: 'true' }
        { name: 'COSMOS_ENDPOINT', value: cosmos.properties.documentEndpoint }
        { name: 'ACS_ENDPOINT', value: 'https://${acs.properties.hostName}' }
        { name: 'MAIL_SENDER', value: 'DoNotReply@${emailDomain.properties.mailFromSenderDomain}' }
        { name: 'SESSION_SECRET', value: '@Microsoft.KeyVault(SecretUri=${secSession.properties.secretUri})' }
        { name: 'ADMIN_PASSWORD_HASH', value: '@Microsoft.KeyVault(SecretUri=${secAdmin.properties.secretUri})' }
        { name: 'ADMIN_TOTP_SECRET', value: empty(adminTotpSecret) ? '' : '@Microsoft.KeyVault(SecretUri=${secTotp.properties.secretUri})' }
      ]
    }
  }
}

resource kvRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: kv
  name: guid(kv.id, app.id, roleKvSecretsUser)
  properties: {
    principalId: app.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleKvSecretsUser)
  }
}
resource acsRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: acs
  name: guid(acs.id, app.id, roleContributor)
  properties: {
    principalId: app.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleContributor)
  }
}
resource cosmosRole 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-05-15' = {
  parent: cosmos
  name: guid(cosmos.id, app.id, cosmosDataContributor)
  properties: {
    principalId: app.identity.principalId
    roleDefinitionId: '${cosmos.id}/sqlRoleDefinitions/${cosmosDataContributor}'
    scope: cosmos.id
  }
}

output appUrl string = 'https://${app.properties.defaultHostName}'
