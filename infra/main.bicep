// EKIP Azure infrastructure (Phase 3 Batch 4).
//
// NOT YET APPLIED to any real subscription -- the identity available in
// this environment (mehul.agarwal@navikenz.com, "Dev subscription") has no
// Contributor/Owner role anywhere on it (confirmed via a real, denied
// `az group create` attempt in Batch 3.5). This template is prepared,
// parameterized, and locally syntax-validated (`az bicep build main.bicep`)
// so that once appropriate permissions exist, deployment is a single
// `az deployment group create` command away -- not a codebase-restructuring
// exercise.
//
// Deploy at RESOURCE GROUP scope (the resource group itself is assumed to
// already exist -- creating one requires subscription-level rights this
// identity doesn't have either, and matches how this org's other resource
// groups are already organized, one per project/person):
//
//   az deployment group create \
//     --resource-group <your-resource-group> \
//     --template-file infra/main.bicep \
//     --parameters infra/main.parameters.example.json
//
// No subscription ID, tenant ID, or resource group name is hardcoded
// anywhere in this file -- every one of those is a parameter or is implicit
// in the deployment scope/command above.

targetScope = 'resourceGroup'

@description('Short, unique name segment used to derive every resource name below (e.g. "ekip-dev", "ekip-prod").')
@minLength(3)
@maxLength(24)
param namePrefix string

@description('Azure region for every resource.')
param location string = resourceGroup().location

@description('PostgreSQL Flexible Server administrator login (password supplied separately, never committed).')
param postgresAdminLogin string = 'ekip_admin'

@secure()
@description('PostgreSQL Flexible Server administrator password. Pass via --parameters postgresAdminPassword=$POSTGRES_ADMIN_PASSWORD at deploy time -- never store this in a committed parameters file.')
param postgresAdminPassword string

@description('Password for the ekip_app application role (NOSUPERUSER/NOBYPASSRLS -- see migration b8f3d6a1c4e7). MUST be identical to EKIP_APP_ROLE_PASSWORD passed to the migrate job below, which creates this role with this password. Pass via --parameters ekipAppPassword=$EKIP_APP_PASSWORD -- never store this in a committed parameters file, and never reuse postgresAdminPassword\'s value here (least privilege means a distinct credential per role, not just a distinct role name).')
@secure()
param ekipAppPassword string

@description('Container image reference for the backend/worker (same image, different command -- see repo-root Dockerfile).')
param backendImage string

@description('Container image reference for the frontend (see frontend/Dockerfile).')
param frontendImage string

@description('OpenAI API key, supplied at deploy time, never committed. Stored directly in Key Vault by this template, not passed as a container app secret env var in plaintext.')
@secure()
param openAiApiKey string

@description('Environment tag applied to every resource for cost/ownership tracking.')
@allowed(['dev', 'staging', 'production'])
param environmentTag string = 'dev'

var tags = {
  project: 'ekip'
  environment: environmentTag
}

// --- Managed identity ------------------------------------------------------
// One user-assigned identity shared by backend + worker container apps --
// both need the exact same Key Vault Crypto User access (they run the same
// application code, just a different entrypoint), so a shared identity
// is the least-privilege-equivalent choice with less to provision than two
// separate ones.
resource appIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${namePrefix}-identity'
  location: location
  tags: tags
}

// --- Key Vault ---------------------------------------------------------------
// RBAC authorization mode (not legacy access policies) -- lets the identity
// below be granted exactly "Key Vault Crypto User" (get/wrapKey/unwrapKey),
// never Owner/Contributor/Manage-Access-Policies. Purge protection ON here
// (unlike the disposable test vault from Batch 3.5): this is meant to be a
// real, persistent production vault, where accidental/malicious deletion
// must not be immediately unrecoverable-into-permanent.
resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: '${namePrefix}-kv'
  location: location
  tags: tags
  properties: {
    sku: {
      family: 'A'
      name: 'standard'
    }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
    enableSoftDelete: true
    enablePurgeProtection: true
    networkAcls: {
      defaultAction: 'Deny'
      bypass: 'AzureServices'
    }
  }
}

resource connectorSecretsKey 'Microsoft.KeyVault/vaults/keys@2023-07-01' = {
  parent: keyVault
  name: 'connector-secrets-kek'
  properties: {
    kty: 'RSA'
    keySize: 2048
    keyOps: ['wrapKey', 'unwrapKey']
  }
}

@description('Built-in "Key Vault Crypto User" role -- get/wrapKey/unwrapKey only, matching Batch 3\'s documented least-privilege requirement. Never Owner, never Contributor, never Manage Access Policies.')
var keyVaultCryptoUserRoleId = 'e147488a-f6f5-4113-8e2d-b22465e65bf6'

resource cryptoUserRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, appIdentity.id, keyVaultCryptoUserRoleId)
  scope: keyVault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultCryptoUserRoleId)
    principalId: appIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

@description('Built-in "Key Vault Secrets User" role -- get-secret only. Distinct from Crypto User above: that role lets the app wrap/unwrap with `connectorSecretsKey` itself (the KEK for envelope-encrypted connector/SSO credentials, `app.shared.security.kms`\'s Azure provider); this one is what Container Apps\' own keyVaultUrl secret reference below needs to read `openAiSecret`\'s plaintext value at container-start time -- a different operation on a different object type, so a separate, equally minimal role.')
var keyVaultSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'

resource secretsUserRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, appIdentity.id, keyVaultSecretsUserRoleId)
  scope: keyVault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsUserRoleId)
    principalId: appIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// The OpenAI API key is a secret, but not one `app.shared.security.kms`
// wraps/unwraps -- it's read directly by the running container as an env
// var sourced from this vault via a Container Apps secretRef, never
// stored in a Bicep parameters file or container image.
resource openAiSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'openai-api-key'
  properties: {
    value: openAiApiKey
  }
}

// --- PostgreSQL --------------------------------------------------------------
// Flexible Server, not Single Server (Microsoft's own recommended SKU family
// for new deployments). Public network access disabled by default -- a real
// production deployment should reach this only via VNet integration/private
// endpoint, parameterized here as `publicNetworkAccessEnabled` rather than
// hardcoded, since a dev environment may reasonably need public access with
// firewall rules while production should not.
@description('Whether this server is reachable over the public internet at all (with firewall rules) -- false requires VNet integration, out of scope for this template\'s first pass.')
param postgresPublicNetworkAccessEnabled bool = false

resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2023-06-01-preview' = {
  name: '${namePrefix}-pg'
  location: location
  tags: tags
  sku: {
    name: 'Standard_B2s'
    tier: 'Burstable'
  }
  properties: {
    version: '16'
    administratorLogin: postgresAdminLogin
    administratorLoginPassword: postgresAdminPassword
    storage: {
      storageSizeGB: 32
    }
    backup: {
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
    network: {
      publicNetworkAccess: postgresPublicNetworkAccessEnabled ? 'Enabled' : 'Disabled'
    }
  }
}

resource postgresDatabase 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2023-06-01-preview' = {
  parent: postgres
  name: 'ekip'
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
}

// pgvector must still be allowlisted via the server's own
// azure.extensions configuration parameter before `CREATE EXTENSION vector`
// (which the app's own migrations already run) succeeds -- Azure Database
// for PostgreSQL Flexible Server requires an extension to be both installed
// AND explicitly allowlisted at the server level.
resource pgvectorAllowlist 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2023-06-01-preview' = {
  parent: postgres
  name: 'azure.extensions'
  properties: {
    value: 'VECTOR'
    source: 'user-override'
  }
}

// --- Redis ---------------------------------------------------------------
// TLS-only (minimumTlsVersion + enableNonSslPort: false); production tier
// (Standard, not Basic) for the SLA a real deployment's job queue needs --
// Phase 2/3 observed real Redis Cloud instability that this codebase's
// worker already tolerates (bounded reconnect, not infinite retry), but a
// managed Azure tier with an actual SLA is still the right production
// choice over accepting known third-party instability indefinitely.
resource redis 'Microsoft.Cache/redis@2023-08-01' = {
  name: '${namePrefix}-redis'
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'Standard'
      family: 'C'
      capacity: 1
    }
    enableNonSslPort: false
    minimumTlsVersion: '1.2'
  }
}

// --- Container Apps ------------------------------------------------------
resource containerAppEnv 'Microsoft.App/managedEnvironments@2023-05-01' = {
  name: '${namePrefix}-env'
  location: location
  tags: tags
}

// MIGRATION DATABASE vs RUNTIME DATABASE (Phase 8 closure -- distinct
// credentials for distinct trust levels, not just distinct names):
//   - The migration identity (`postgresAdminLogin`/`postgresAdminPassword`,
//     Postgres's own server-admin account) can create roles, alter schema,
//     and run every migration in `app/database/migrations/versions/`
//     (including `b8f3d6a1c4e7`, which needs `CREATE ROLE`/`GRANT` --
//     privileges `ekip_app` itself deliberately never has). It is used
//     ONLY by `migrateJob` below, which runs once per deploy and exits --
//     never by `backendApp`/`workerApp`/`frontendApp`.
//   - The runtime identity (`ekip_app`, provisioned by that same migration,
//     `NOSUPERUSER`/`NOBYPASSRLS`) is what `backendApp`/`workerApp` connect
//     as for the entire lifetime of the deployment -- ordinary DML only,
//     RLS enforced on every query. `ekipAppPassword` must be the exact
//     value `migrateJob` sets when it creates/updates the role (passed to
//     both via the same parameter), or the runtime containers simply fail
//     to authenticate -- loud, not a silent RLS gap.
// This split is the actual fix for the `EKIP_TENANT_ISOLATION_SECURITY_
// REVIEW.md` recommendation #2 finding that every environment observed so
// far put one single, superuser-equivalent credential in both places.
var migrationDatabaseUrl = 'postgresql+asyncpg://${postgresAdminLogin}:${postgresAdminPassword}@${postgres.properties.fullyQualifiedDomainName}/ekip?ssl=require'
var runtimeDatabaseUrl = 'postgresql+asyncpg://ekip_app:${ekipAppPassword}@${postgres.properties.fullyQualifiedDomainName}/ekip?ssl=require'

// Three distinct kinds of Container Apps `secrets` entry, all resolved by
// the platform before the container starts (never written to the image,
// never visible in `az containerapp show`'s plain env var list, and
// redacted in the portal/CLI):
//   - `keyVaultUrl` -- the platform itself fetches the current value from
//     Key Vault at start (and on every restart), authenticating as
//     `identity` (`secretsUserRoleAssignment` above is what makes this
//     call succeed) -- used for `openAiSecret`, which already lives in
//     Key Vault for the KMS/envelope-encryption code path to reason about
//     consistently, not duplicated into a second, Bicep-only secret.
//   - a plain computed `value` -- for the two connection strings above and
//     for Redis, none of which have another Key Vault presence to point
//     at; Bicep interpolates the credential directly into the full
//     connection string at deployment time, since Container Apps secrets
//     don't support interpolating a secretRef into a larger string -- the
//     assembled value itself becomes the secret.
// `runtimeSecrets` is what `backendApp`/`workerApp` get -- notice
// `migration-database-url` is deliberately NOT in this list, only in
// `migrationJobSecrets` below, so the admin credential never reaches a
// long-lived, internet-facing container.
var runtimeSecrets = [
  {
    name: 'openai-api-key'
    keyVaultUrl: openAiSecret.properties.secretUri
    identity: appIdentity.id
  }
  {
    name: 'database-url'
    value: runtimeDatabaseUrl
  }
  {
    // `redis.listKeys()` -- Azure Cache for Redis authenticates by access
    // key, not by the deploying identity; there is no Key Vault presence
    // for this one either (unlike `openai-api-key` above), so it's a
    // Bicep-computed secret the same way `database-url` is. `redis-py`/
    // arq's `RedisSettings.from_dsn` (`app/ingestion/workers/main.py`,
    // `app/agents/workers/main.py`, `app/api/main.py`) parses the standard
    // `redis://[:password]@host:port` shape -- an empty username before
    // the colon is exactly how a key-only (no separate username) Azure
    // Cache for Redis credential is expressed in a connection URL.
    name: 'redis-url'
    value: 'rediss://:${redis.listKeys().primaryKey}@${redis.name}.redis.cache.windows.net:6380'
  }
]

var sharedEnvVars = [
  {
    name: 'ENVIRONMENT'
    value: environmentTag == 'production' ? 'production' : 'development'
  }
  {
    name: 'KMS_PROVIDER'
    value: 'azure'
  }
  {
    name: 'AZURE_KEY_VAULT_URL'
    value: keyVault.properties.vaultUri
  }
  {
    name: 'AZURE_KEY_VAULT_KEY_NAME'
    value: connectorSecretsKey.name
  }
  {
    name: 'DATABASE_URL'
    secretRef: 'database-url'
  }
  {
    name: 'OPENAI_API_KEY'
    secretRef: 'openai-api-key'
  }
  {
    name: 'REDIS_URL'
    secretRef: 'redis-url'
  }
  {
    name: 'CORS_ALLOWED_ORIGINS'
    value: 'https://${namePrefix}-frontend.${containerAppEnv.properties.defaultDomain}'
  }
]

// One-shot migration job -- `scripts/deploy.sh`'s `azure_deploy` step 5/9
// already calls `az containerapp job start --name "${NAME_PREFIX}-migrate"`
// expecting this to exist; it did not, until this fix (Phase 8 closure --
// every prior "syntax-validated" pass compiled the template without ever
// noticing the deploy script referenced a resource the template never
// provisioned, since `az bicep build` only checks the file in front of it).
// Runs once per deploy, before `backendApp`/`workerApp` start (deploy.sh's
// own ordering) -- `Microsoft.App/jobs` with `triggerType: Manual` is
// Container Apps' actual "run to completion, don't keep serving traffic"
// primitive, matching `docker-compose.yml`'s `migrate` service semantics
// (`restart: "no"`) rather than reusing `containerApps` (a long-running,
// auto-restarting primitive that isn't right for a step meant to run once
// and exit). Uses the MIGRATION identity/credential, never `runtimeSecrets`
// -- this is the one place in the whole template `postgresAdminPassword`
// is used for a database connection at all.
resource migrateJob 'Microsoft.App/jobs@2023-05-01' = {
  name: '${namePrefix}-migrate'
  location: location
  tags: tags
  properties: {
    environmentId: containerAppEnv.id
    configuration: {
      triggerType: 'Manual'
      replicaTimeout: 600
      replicaRetryLimit: 0
      manualTriggerConfig: {
        parallelism: 1
        replicaCompletionCount: 1
      }
      secrets: [
        {
          name: 'migration-database-url'
          value: migrationDatabaseUrl
        }
        {
          name: 'ekip-app-role-password'
          value: ekipAppPassword
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'migrate'
          image: backendImage
          command: ['python', '-m', 'alembic', 'upgrade', 'head']
          env: [
            {
              name: 'DATABASE_URL'
              secretRef: 'migration-database-url'
            }
            // Read by migration b8f3d6a1c4e7 -- see that migration's own
            // "PASSWORD HANDLING" section for why this can't be a bind
            // parameter or a hardcoded value.
            {
              name: 'EKIP_APP_ROLE_PASSWORD'
              secretRef: 'ekip-app-role-password'
            }
          ]
        }
      ]
    }
  }
}

resource backendApp 'Microsoft.App/containerApps@2023-05-01' = {
  name: '${namePrefix}-backend'
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${appIdentity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: containerAppEnv.id
    configuration: {
      ingress: {
        external: true
        targetPort: 8000
      }
      secrets: runtimeSecrets
    }
    template: {
      containers: [
        {
          name: 'backend'
          image: backendImage
          env: sharedEnvVars
          probes: [
            {
              type: 'Liveness'
              httpGet: { path: '/health', port: 8000 }
              periodSeconds: 30
            }
            {
              type: 'Readiness'
              httpGet: { path: '/ready', port: 8000 }
              periodSeconds: 15
            }
          ]
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 3
      }
    }
  }
}

resource workerApp 'Microsoft.App/containerApps@2023-05-01' = {
  name: '${namePrefix}-worker'
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${appIdentity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: containerAppEnv.id
    configuration: {
      // No ingress at all -- the worker never serves HTTP traffic
      // (ENGINEERING_DECISIONS.md #002: a separate arq process, not a
      // second copy of the API).
      ingress: null
      secrets: runtimeSecrets
    }
    template: {
      containers: [
        {
          name: 'worker'
          image: backendImage
          command: ['arq', 'app.ingestion.workers.main.WorkerSettings']
          env: sharedEnvVars
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
    }
  }
}

resource frontendApp 'Microsoft.App/containerApps@2023-05-01' = {
  name: '${namePrefix}-frontend'
  location: location
  tags: tags
  properties: {
    managedEnvironmentId: containerAppEnv.id
    configuration: {
      ingress: {
        external: true
        // The frontend image's nginx now listens on $PORT (default 8080,
        // frontend/nginx.conf.template) so it fits any platform that injects
        // one; Container Apps does not, so it gets the image default.
        targetPort: 8080
      }
    }
    template: {
      containers: [
        {
          name: 'frontend'
          image: frontendImage
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 3
      }
    }
  }
}

output keyVaultUri string = keyVault.properties.vaultUri
output postgresFqdn string = postgres.properties.fullyQualifiedDomainName
output backendUrl string = 'https://${backendApp.properties.configuration.ingress.fqdn}'
output frontendUrl string = 'https://${frontendApp.properties.configuration.ingress.fqdn}'
output appIdentityPrincipalId string = appIdentity.properties.principalId
