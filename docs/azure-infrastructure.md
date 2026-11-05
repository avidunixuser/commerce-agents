# Private Azure infrastructure

`infra/container-apps/main.bicep` and `infra/aks/main.bicep` are independent
resource-group deployment targets. Neither provisions the other. Both consume
the same runtime, gateway and selected-vertical frontend image digests. A target
creates its own private ACR; promote the same manifest without rebuilding and
verify that the destination digest is identical. A frontend is built for one
role and vertical, not for a model provider.

These are deployment artifacts, not a deployed or capacity-certified service.
No subscription, model entitlement, quota, private inference availability,
network connectivity or production commerce integration is inferred from
successful offline validation.

## Container Apps foundation-only stage

Use `infra/container-apps/foundation.parameters.example.json` when creating only
the private foundation, with application rollout deferred. Populate the real
prefix, approved region and bounded private source CIDRs, and establish the
account/service availability prerequisites before setting
`modelAvailabilityVerified=true`.

```powershell
python infra\scripts\preflight.py --target container-apps --phase foundation --parameters <local-foundation-parameters.json>
```

This explicit mode requires `enableWorkloads=false` and `enableIngress=false`.
Images, runtime environment, secret references, hostnames, certificate URI,
model deployment arrays and Claude marketplace attestation must stay empty.
It creates the internal ACA environment, private shared services, identities
and configured AI accounts/project, **not** Container Apps workloads, WAF,
model deployments, marketplace acceptance or a working commerce application.
The network module creates the firewall's public IP; the firewall is active
and billable even without workloads.

The default preflight phase remains `application`, including all original
authentication, digest-pinned image, model-deployment and approval checks.
Foundation validation cannot authorize application enablement or certify live
inference. Before rollout, supply the real integrations and approved model
attestation, then repeat full application and live deployment validation.
AKS is not supported by this narrow foundation-only preflight mode.

Keep actual deployment parameters and cloud outputs outside version control.
Local `.azure/` environment files are ignored; the reviewed deployment plan is
the only retained file from that directory.

The optional subscription entrypoint `infra/private-build/main.bicep` adds
isolated image-build identities, a NIC NSG and runner-only firewall rules to
an existing foundation without enabling workloads or ingress. Its separate
`vm.bicep` describes a fresh, private ephemeral build host, not a registered
GitHub runner. See [private image-build commissioning and recovery](azure-private-runners.md#protected-private-image-builds)
for exact permissions, OIDC approval, cost and cleanup boundaries. This path
requires separate validation/provisioning approval and does not install AKS.

### Incremental deployment and subscription prerequisites

Before retrying or updating either hosting target, inspect the existing
`workload`, `endpoints` and `runners` subnet NSG associations. Supply their
full resource IDs in the optional `existingSubnetNsgIds` object, keyed by those
subnet names. This preserves externally managed associations without changing
their NSG rules; the gateway NSG remains managed by this deployment. The
default empty object preserves the original fresh-install behavior. Review
what-if and do not remove automatically attached governance controls.

If creating the ordinary Azure-allocated firewall IP fails with
`SubscriptionNotRegisteredForFeature` for
`Microsoft.Network/AllowBringYourOwnPublicIpAddress`, obtain subscription
administrator approval before registering the feature. Wait for `Registered`,
refresh Microsoft.Network provider registration, then repeat validation and
incremental what-if before retrying. The template does not request a custom
IP prefix; changing API versions is not a reliable workaround for a
subscription feature restriction.

## Resource and trust boundaries

| Component | Configuration |
|---|---|
| Network | One segmented `10.40.0.0/16` VNet, private DNS links, workload and runner default routes through Azure Firewall Standard with DNS proxy |
| Cosmos | NoSQL, local authentication disabled, public access disabled, continuous seven-day backup, shared database autoscale, `/pk` containers with per-item TTL enabled |
| Registry | ACR Premium, Private Link, dedicated data endpoint, admin credentials disabled, public access and trusted-service bypass disabled |
| Secrets | RBAC Key Vault, Private Link, public access disabled, purge protection and 90-day soft deletion |
| Models | New or existing OpenAI/Foundry Claude accounts, Private Link, explicit deployments and availability gate; no public fallback |
| Observability | Log Analytics, workspace-backed Application Insights with local auth disabled, ACA environment logs, diagnostic metrics, gated AKS Container Insights and resource-scoped telemetry publisher identities |
| Edge | Application Gateway WAF_v2, prevention mode, TLS 1.2+, independent public and private listeners |

The database contains `runtime` and `identity`. APIs and workers share only the
`runtime` container SQL Data Contributor grant. That includes tenant-qualified
session, memory, jobs, journal and audit aggregates; it is not one tenant-wide
partition. The gateway has a grant only to the encrypted auth `identity`
container. Cosmos transactional batches remain limited to one container and one
partition. No Service Bus, Durable Task Scheduler, PostgreSQL, Redis, AI Search
or unused asset storage is provisioned.

`runtimeContainer` can rename the runtime container; `identity` is reserved for
gateway auth. Do not select `identity` as the runtime container. Resource groups
are the isolation unit between independent installations. Application tenancy
and principal binding remain mandatory even with container-scoped RBAC.

Every API, worker and gateway has its own managed identity. AKS frontends also
have dedicated service accounts and federated identities with no data grants.
ACA frontends attach only the pull identity, with no data grants. Node/image-pull
identities have ACR Pull, never Cosmos, model or vault access. Control-plane
network and Managed Identity Operator grants belong only to AKS's control
identity. Runtime identities are not Owner or Contributor.

### Static identity-to-role proof (2026-11-04)

Reviewed both entrypoints and every authored Azure/SQL role assignment without
Azure authentication or resource queries. `${P}` below means
`/subscriptions/<subscription>/resourceGroups/<deployment-resource-group>/providers`;
`C`, `V`, `R`, `I` mean the exact created Cosmos account, Key Vault, ACR and
Application Insights component resource IDs under that prefix.

| Principal | Role | Exact assignment scope |
|---|---|---|
| `${name}-shopping-api`, `${name}-shopping-worker`, `${name}-merchant-api`, `${name}-merchant-worker` | Cosmos DB Built-in Data Contributor (`00000000-0000-0000-0000-000000000002`, **SQL**, not ARM Contributor) | `${C}/dbs/${databaseName}/colls/${runtimeContainer}` |
| `${name}-shopping-gateway`, `${name}-merchant-gateway` | Same Cosmos SQL data role | `${C}/dbs/${databaseName}/colls/identity` only |
| Four API/worker principals only | Cognitive Services OpenAI User (`5e0bd9bd-7b93-4f28-af87-19fc36ad61bd`) | Each configured OpenAI **account** `${P}/Microsoft.CognitiveServices/accounts/<new-or-existing-account>` |
| Four API/worker principals only | Custom `Commerce Claude Data Plane <resource-group-hash>` | Each configured dedicated Claude **account** `${P}/Microsoft.CognitiveServices/accounts/<new-or-existing-account>` |
| Six API/worker/gateway principals only | Monitoring Metrics Publisher (`3913510d-42f4-4e42-8a64-420c390055eb`) | `${I}` only |
| Each gateway principal | Key Vault Secrets User (`4633458b-17de-408a-b874-0445c86b69e6`) | `${V}/secrets/<that-gateway-referenced-secret-name>`; no vault-wide grant |
| WAF `${name}-waf-tls` | Key Vault Secrets User | `${V}/secrets/<certificateSecretUri-secret-name>` only; includes future versions for rotation |
| ACA `${name}-pull` or AKS `${name}-node` | AcrPull (`7f951dda-4ed3-4680-a7ca-43fe172d538d`) | `${R}` only |
| AKS `${name}-control` | Network Contributor (`4d97b98b-1d4f-4787-a291-c67834d212e7`) | `${P}/Microsoft.Network/virtualNetworks/${name}-vnet/subnets/workload` only |
| AKS `${name}-control` | Reader (`acdd72a7-3385-48ef-bd42-f606fba81ae7`), management-plane route inspection only | `${P}/Microsoft.Network/routeTables/${name}-udr` only; no route write |
| AKS `${name}-control` | Managed Identity Operator (`f1a07417-d97a-45cb-824c-7a7467783830`) | `${P}/Microsoft.ManagedIdentity/userAssignedIdentities/${name}-node` only |
| AKS shopping/merchant web identities | None | No database, model, Key Vault or telemetry roles |
| ACA shopping/merchant web processes | Pull identity only | ACR read/pull only, no database, model or Key Vault roles |

API/worker identities cannot access `identity`; gateway identities cannot access
`runtime` or model accounts. API/worker identities have no Key Vault grants and
cannot retrieve the gateway signing private key. The node/pull identity never
receives data, model or Key Vault access. AKS federation is per workload and
bound to the cluster OIDC issuer and exact
`system:serviceaccount:${namespace}:<workload-name>` subject; the node identity
has no workload federation. Azure-managed cluster/add-on identities and
provider-created node-resource-group grants are not represented as authored
workload grants and still need live post-provisioning inspection.

**Shopping/merchant limitation:** both role APIs/workers intentionally receive
the **same runtime-container scope**, as required by the shared atomic state
contract. Cosmos SQL RBAC does not enforce per-`/pk`, principal, role or vertical
isolation here. Both gateway identities likewise share the auth container.
Distinct identities, tenant/subject-bound keys, role/vertical scheduler filters
and application authorization enforce record separation; a compromised runtime
identity can access other partitions in its granted container. Do not describe
this as database-enforced shopper/merchant isolation. A stronger identity
boundary requires separate containers/installations and a coordinated runtime
contract change, not an IaC-only grant change.

**Corrections from this final review:** removed vault-wide secret reads, narrowed
AKS control networking from the whole VNet to its workload subnet, and replaced
Claude's built-in Cognitive Services User (which includes account-key listing)
with a custom role having `actions: []`, `dataActions:
["Microsoft.CognitiveServices/*"]`, and no control-plane permissions.
The custom role uses the documented built-in's data-action surface without its
key-listing/control-plane actions; it is **not a message-only API allow-list**.
Use a dedicated Claude account: all Cognitive Services data operations in that
account are granted. A finer verified Anthropic action catalog and live
inference authorization remain deployment gates, not invented operation names.
The custom role's definition is assignable within the deployment resource group,
but actual grants are account-scoped, never group- or subscription-scoped.
OpenAI User similarly includes provider-defined nonmutating metadata reads and
additional inference APIs; neither role grants model-deployment writes.

ACA derives each gateway's grant names from its `secretReferences.keyVaultUrl`.
Those URLs must refer to the created target vault. Grants are deferred until
`enableWorkloads=true`, after the operator has seeded secrets. AKS
`gatewaySecretNames.<shopping-gateway|merchant-gateway>` must list exactly the
Helm CSI `remoteName` values; leave it empty when using existing Kubernetes
secrets instead. Other workloads cannot request these grants. Use distinct
role-specific secret names, and a separate versionless TLS certificate secret.
The AKS foundation example leaves `gatewaySecretNames={}`; populate the mapping
only after importing those secrets and before deploying CSI-consuming workloads.
No secret values are written by these templates.

This proof is static, not evidence of effective live roles. Incremental ARM
deployment does **not** remove old wider assignments when scope/name changes.
For an already-deployed installation, a separately authorized operator must
explicitly revoke superseded vault/VNet grants, the prior Cognitive Services
User assignment, and any inherited grants before claiming the narrower boundary.
No such live revocation or provisioning was performed here.

## Enforced merchant boundary

Public storefront traffic reaches the public IP on port **443**. Merchant
traffic reaches only `10.40.4.10` on port **8443**, over operator-supplied VPN,
ExpressRoute or equivalent private connectivity. Merchant source CIDRs must
match that connectivity; peering, private DNS and private transport are not
created by these templates.

The merchant listener is bound to the private frontend, not the public
frontend. The NSG allows merchant port 8443 only from `privateAccessCidrs` and
denies all other sources. A request to public-IP:8443 is denied. A merchant Host
header on public-IP:443 cannot select the private listener. The public listener
uses only the shopping backend. Its separate WAF policy also blocks
URL-decoded, lowercased `/merchant` paths. The shopping gateway independently
rejects merchant paths after repeated decoding and rejects ambiguous traversal;
WAF transforms are not the sole path-normalization defense.

ACA exposes only gateways to the internal environment's VNet ingress, with IP
restrictions allowing the Application Gateway subnet. Role APIs and frontends
are environment-local, and workers have no ingress. AKS uses **two** internal
managed Gateway load balancers, one per role, with Application Gateway source
ranges and default-deny policies. Role gateway policies cannot reach the other
role's API or frontend. All application Services are ClusterIP; the managed
Gateway controller, not this chart, creates the private LoadBalancer Services.
No NGINX ingress is installed.

Private DNS maps merchant hostname to the private WAF IP. Public DNS maps the
storefront hostname to the public WAF IP. Certificates must cover both names;
the versionless Key Vault secret URI refers to an operator-imported,
exportable PFX. Register the merchant OIDC redirect URI with the **8443** port.
Merchants also require workforce authentication and approver authorization.
`COMMERCE_PRIVATE_BOUNDARY=true` asserts this enforced topology; it does not
create the boundary by itself.

## Addressing and outbound dependencies

| Subnet | Prefix | Routing |
|---|---|---|
| AzureFirewallSubnet | `10.40.0.0/26` | Azure Firewall |
| ACA workload | `10.40.2.0/23` | Default UDR to firewall; delegated to Microsoft.App/environments |
| AKS nodes/LB | `10.40.8.0/22` | Default UDR to firewall |
| Application Gateway | `10.40.4.0/24` | Platform-compatible routing, not forced-tunnelled |
| Private endpoints | `10.40.5.0/24` | Private endpoint policies disabled |
| Private runners | `10.40.6.0/24` | Default UDR to firewall |

The two workload subnets are alternatives, not resources deployed together.
AKS overlay pods use `10.244.0.0/16`, Services `10.100.0.0/16`, and DNS
`10.100.0.10`. Gateways reserve `10.40.8.20` and `.21` in Helm values. Check
corporate peering/VPN overlap before deployment. Addressing is a reviewed
Day-0 decision; changing only one CIDR is not supported. Change subnet prefixes,
firewall address, routes, chart CIDRs and DNS together if the range conflicts.
Verify the allocated firewall IP is `10.40.0.4` before enabling workloads.

Firewall DNS proxy forwards to Azure DNS and resolves VNet-linked private
zones. Private clients outside this VNet need conditional forwarding to an
organization DNS resolver connected to these zones; do not point on-premises
clients directly at `168.63.129.16`. The template creates zones for Cosmos SQL,
ACR registry/data endpoints, Key Vault and configured model endpoint families.
ACA additionally creates a zone/wildcard for the environment's actual generated
default domain. AKS manages its private API DNS zone.

Workload/runner internet traffic has no general allow rule. The explicit
allowlist includes Microsoft container image/package endpoints, managed
identity/Entra, ARM and Azure Monitor. AKS adds the documented
`AzureKubernetesService` FQDN tag, regional control-plane TCP/UDP 1194/9000 and
UDP 123 to `ntp.ubuntu.com`. ACA uses documented identity and regional Storage
service-tag HTTPS dependencies, not an unrestricted `AzureCloud:443` rule.
Private ACR, Cosmos, vault and models remain private VNet destinations.
Private Link does not mean public token, package and telemetry services are
unnecessary.

Populate `additionalEgressFqdns` with the exact customer issuer/discovery/JWKS,
commerce integration, storefront guest-JWKS and approved runner dependency
hosts. Do not add a wildcard public model endpoint to bypass private inference
failures. Review first-party platform dependency changes before each release.
Application Gateway retains its required direct management-plane outbound
connectivity; it is not claimed to be fully forced-tunnelled.

## Models and availability gates

Set `createOpenAI` with explicit `modelDeployments` entries
`{name, model, version, sku, capacity}`, or reference `existingOpenAIName`.
Set `createClaude` with explicit `claudeDeployments` entries and
`claudeAttestation` `{organizationName, countryCode, industry}`, or reference
`existingClaudeName`. Select **new OR existing** for each family, not both.
Existing accounts must be in the deployment resource group. New model accounts
use the selected region; an existing account retains its own region.

New OpenAI accounts use OpenAI User for invocation. Claude accounts use the
account-scoped custom data-plane-only role documented in the static proof above. No runtime
principal receives model deployment/control-plane Contributor. New Claude
accounts use AIServices, a project and Anthropic deployment format, with public
and local authentication disabled. Applying the provider attestation accepts
Marketplace terms; code preparation does not accept terms or invent a legal
organization. No model name, SKU or capacity is assumed available.

`modelAvailabilityVerified` deliberately allows only `true` in Bicep. Example
parameters leave it **false** and cannot deploy. Before setting true, operators
must establish catalog entitlement, Marketplace terms, region/SKU/version
support, quota headroom, private endpoint and Entra inference support.
For new accounts, actual private DNS resolution and Entra-authenticated inference
are tested after foundation creation and before workloads/ingress enablement;
the flag alone is not evidence that these tests passed. Existing accounts must already have
`publicNetworkAccess=Disabled` and `disableLocalAuth=true`; an `existing`
Bicep declaration cannot enforce or mutate those properties.

**Known schema gate:** Bicep 0.46.1's bundled
`2025-10-01-preview` deployment schema omits `modelProviderData`. Its single
BCP037 suppression is attached to the exact field required by the first-party
Claude Bicep sample. Static compilation does **not** validate that field against
the live resource provider. Server-side template validation and private Claude
inference are release blockers. If that offering cannot satisfy private
inference and Entra-only access, stop; use a different approved private offering
or do not enable Claude.

The attestation gap is a **bundled type-schema gap**, not an omitted
attestation implementation. `modules/models.bicep` already declares the new
AIServices account, project, serialized Anthropic model deployments,
`modelProviderData` attestation, private endpoint/DNS and resource-scoped
inference grants. It sets public access disabled and local authentication off,
and outputs the SDK-ready `/anthropic/` endpoint. There is no intentionally
stubbed Claude resource or missing attestation write.

What remains unproven is resource-provider acceptance of that preview field and
the chosen offering's entitlement, quota, private networking and live
Entra-authenticated invocation. The code can request a private Claude
installation; it is **not evidence that Azure will successfully create or serve
that installation**. Server-side validation and a private inference smoke test
must establish this before enablement. Existing-resource mode also requires
operators to verify its public/local authentication flags because an `existing`
declaration does not change them.

The current first-party
[Claude hosting comparison](https://learn.microsoft.com/azure/foundry/foundry-models/concepts/claude-models-hosting-comparison)
distinguishes version 2 (Hosted on Azure) from version 1 (Hosted on Anthropic
infrastructure). A private client-facing endpoint alone does not prove that
inference stays in Azure. If end-to-end Azure residency is required, select an
available Azure-hosted model/version and verify its private inference support;
do not silently deploy an Anthropic-hosted version or assume every catalog
Claude version has the same network capabilities.

## Runtime configuration

The host runs `python -m commerce_host api` or `python -m commerce_host worker`;
the gateway runs `python -m commerce_gateway`. All use port 8080 with
`/health/live` and `/health/ready`. Frontends use production Next.js on 3000.
Workers process Cosmos-backed jobs; they are not Container Apps Jobs or queue
consumers. API and worker deployment inputs must agree on role, vertical,
provider ID, backend factory, auth settings, Cosmos endpoint/database/container.

Bicep injects enforced `COMMERCE_ROLE`, `COMMERCE_VERTICAL`,
`COMMERCE_MODE=production`, `COMMERCE_APP_ROOT=/app`, `COMMERCE_COSMOS_*`, gateway upstreams and private
boundary settings. Supply non-secret purpose/provider/auth/backend configuration
in `runtimeEnvironment`. Missing `COMMERCE_BACKEND_FACTORY=module:function`
or a required capability blocks production startup; never replace it with a
multi-replica in-process fixture. See [gateway-auth.md](gateway-auth.md) for all
gateway auth and secret variables. APIs require `COMMERCE_AUTH_ISSUER`,
`COMMERCE_AUTH_AUDIENCE`, `COMMERCE_AUTH_JWKS_URL`,
`COMMERCE_ALLOWED_TENANTS`, `COMMERCE_PROVIDER_ID` and backend factory.

Provider selection uses `COMMERCE_PROVIDER=claude_foundry|azure_openai` and
`COMMERCE_MODEL`, with role/purpose overrides
`COMMERCE_{ROLE}_{PURPOSE}_{PROVIDER,MODEL,ENDPOINT}` for chat, extraction and
analysis. Supply the actual deployment name, not a catalog model identifier
when the service requires a deployment. Foundry Claude uses
`ANTHROPIC_FOUNDRY_BASE_URL=https://<resource>.services.ai.azure.com/anthropic/`;
OpenAI uses
`AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com/openai/v1/`.
The template outputs both SDK-ready endpoint families. Optional
`COMMERCE_MODEL_TIMEOUT_S` defaults to 120. Foundry token scope is
`https://ai.azure.com/.default`; OpenAI uses
`https://cognitiveservices.azure.com/.default`.

`COMMERCE_PROVIDER_ID` is **not** a model name or an arbitrary rollout label.
Compute it from the final provider environment using the immutable runtime
image, then place the returned fixed fingerprint into the deployment settings:

```python
import os
from commerce_common.providers import provider_configuration_id

print(provider_configuration_id(os.environ))
```

Resolve all role/purpose provider/model/endpoint values before computing this.
Use identical resolved settings and fingerprint for the corresponding API and
worker deployments; the host rejects mismatches. IaC does not fabricate a
fingerprint or invoke a model to compute it. Do not change providers within an
existing conversation.

Gateway OIDC client secret, signing private key and encryption keys use Key Vault
references, never frontend build arguments, Bicep plaintext values or Helm values.
APIs receive only public JWKS trust. Configure shopper guest issuer/JWKS and its
network dependencies explicitly. API identities do not read signing secrets.

### Host/gateway deployment integration contract

ACA non-secret values below go in `runtimeEnvironment.value` under the exact
workload name; Helm equivalents go in `workloads.<name>.env`. Shopping API and
worker parameter examples explicitly include both guest validation settings.
Helm refuses empty/non-HTTPS guest settings for these two workloads. ACA
parameter preflight rejects mismatched API/worker/gateway issuer or audience settings.

| Contract | Shopping API and worker | Shopping gateway | ACA / Helm placement |
|---|---|---|---|
| Customer issuer and keys | `COMMERCE_AUTH_ISSUER`, `COMMERCE_AUTH_JWKS_URL` trust only customer OIDC | `COMMERCE_OIDC_ISSUER` equals host primary issuer; discovery supplies OIDC JWKS | Per-workload non-secret env |
| Guest authority and keys | `COMMERCE_GUEST_ISSUER=https://<shopping-public-host>`; `COMMERCE_GUEST_JWKS_URL=https://<shopping-public-host>/.well-known/jwks.json`; same on API and worker | `COMMERCE_GATEWAY_ISSUER` is exactly the guest issuer; published JWKS contains public keys only | Per-workload non-secret env; public JWKS HTTPS egress/DNS must be allowed |
| Audience | Both authorities validate against the same `COMMERCE_AUTH_AUDIENCE` | `COMMERCE_OIDC_API_AUDIENCE` must equal that actual API audience; guest assertions must use it too | Per-workload non-secret env; no assumption that a real Entra audience is literally `commerce-api` |
| Tenant and role boundary | `COMMERCE_ALLOWED_TENANTS`; primary OIDC cannot claim guest and guest authority cannot issue customers/merchants | `COMMERCE_TENANT_ID` must be allowed by host; separate workforce merchant gateway | Role/vertical/private-boundary values are enforced by Bicep/chart; issuer settings are operator inputs |
| Cosmos and identity | Shared runtime container and API/worker scoped identities | Separate encrypted `identity` auth container and gateway identity | ACA injects `COMMERCE_COSMOS_*`; Helm `globalEnv` contains endpoint/database/container inputs; per-service-account federation selects identity |
| Private credentials | Public JWKS only, never gateway signing private key | OIDC client secret, signing private key and encryption key ring | ACA `secretReferences`; Helm CSI `secrets` or `existingSecretRefs`; no plaintext values |

This table states the required contract, not a live authentication result.
Merchant API/worker keep workforce OIDC trust and must not inherit shopping
guest authority settings. Cosmos expiry work runs in the existing worker with
the same container grant; it requires no new queue, scheduler or Azure resource.

ACA uses an **internal workload-profile environment**, including a Consumption
workload profile. `external:true` on gateways means visible outside the app
environment but still **inside the VNet**, not public internet. Backends use
`external:false`. Minimum two warm replicas, bounded HTTP scaling, startup,
liveness/readiness probes and 240-second termination grace are explicit.
Workers stay at the warm floor and are scaled using CPU HPA on AKS or an
operator-adjusted ACA replica floor; no unimplemented Cosmos queue scaler is
claimed. Capacity, zone redundancy and SLOs require workload measurements.

Streams are segmented at 210 seconds, below the WAF 230-second backend and ACA
standard 240-second limit. AKS HTTPRoute bounds are 225/220 seconds. Resume uses
persisted event IDs, never another chat submission. Long turns and draining must
be exercised with the real worker; heartbeat does not remove ingress timeouts.

## AKS managed Gateway API

The cluster uses Standard tier for explicit network/pool control, private API,
Entra Azure RBAC, local accounts disabled, Azure CNI Overlay/Cilium, OIDC,
workload identity, Azure Policy and Key Vault CSI. System and user pools are
separate, autoscaled and spread over requested zones; VM/zone/version
availability is an operator preflight input. The system pool is tainted.

The documented ARM `2026-03-01` fields enable managed Gateway API installation
and `webAppRouting.gatewayAPIImplementations.appRoutingIstio.mode=Enabled`.
The managed class is `approuting-istio`. Do not install self-managed Gateway API
CRDs, NGINX, or the Istio service-mesh add-on alongside it. Use CLI 2.86+ for
feature diagnostics. API schema availability at compile time does not establish
support in the selected region/version.

Helm creates eight separate Deployments/service accounts, default-deny ingress
and egress, explicit same-role service paths, DNS, Entra, Cosmos, model, vault
and telemetry HTTPS egress. Supply actual resolved **/32** private endpoint and
external dependency addresses, not `0.0.0.0/0` or entire VNets. Cosmos must use
HTTPS gateway connection mode; direct mode requires additional ports and is not
permitted by this policy. Monitor DNS/IP changes and atomically update approved
egress addresses; Kubernetes NetworkPolicy cannot match FQDNs.

Managed proxies use a supported per-Gateway ConfigMap to preserve source IP
with `externalTrafficPolicy: Local`, limit LB source ranges to Application
Gateway, and remove Cluster-mode health-probe overrides. Policy allows managed
control-plane xDS, DNS, Azure health probes, and only the corresponding BFF.
Do not inject custom sidecars into managed proxies.

Containers run nonroot with read-only filesystems, RuntimeDefault seccomp,
no privilege escalation, all capabilities dropped, bounded scratch/cache,
startup/liveness/readiness probes, requests/limits, CPU HPA, PDBs, topology
spread and rolling drain grace. Workload identity tokens are projected by the
Azure webhook; ordinary Kubernetes service-account token automount is off.
Kubernetes/CSI secrets referenced by env require a rollout after rotation;
CSI volume rotation alone does not refresh process environment values.

## Observability and privacy

Application Insights accepts Entra-authenticated telemetry; identities have
Monitoring Metrics Publisher on that component. Deployment configuration includes
`OTEL_SERVICE_NAME`, Application Insights connection metadata and AAD identity
selection. These settings do not automatically instrument an application.
Enable the runtime's supported Azure Monitor/OpenTelemetry exporter before
declaring telemetry functional, and test its authentication with local auth off.
An external OTLP collector is operator-supplied when required; no decorative
collector Deployment is installed.

Collect redacted correlation IDs, role/provider usage and latency, throttling,
ETag conflicts, lease/retry/dead-letter counts, stream reconnects, deletion and
retention backlog. Never record prompts, transcripts, cookies, tokens, raw
principal IDs or OIDC callback query strings by default. Managed Gateway access
logging is on by default and cannot be customized with Istio Telemetry in this
implementation. Restrict its access/retention and apply ingestion-time redaction
before enabling central collection; stop rollout if callback codes would be
retained. Do not enable raw Application Gateway access/WAF payload diagnostics
without an approved redaction design. Container Insights collection of managed
proxy stdout needs the same review. Consequently `enableContainerInsights`
defaults to **false** until an approved DCR transform/namespace exclusion is
installed. AKS diagnostic metrics and Entra Application Insights publishing
remain configured independently. Do not turn on stdout collection first and
plan to redact retained callback codes later.

For Helm, gateway secrets can use `workloads.<name>.secrets` with CSI
`{remoteName, alias, envName}`, or externally managed
`existingSecretRefs: [{secretName, key, envName}]`. All three required gateway
secret variables must have references; plain environment values and protected
boundary overrides are rejected. This chart never creates literal Secret values.

## Validation and release

See [azure-private-runners.md](azure-private-runners.md) for bootstrap order and
gated release, and [azure-cost-drivers.md](azure-cost-drivers.md) for cost inputs.
No workflow in this workstream logs in to Azure or provisions resources.

```powershell
python infra\scripts\validate.py --helm <helm-executable> --kubeconform <kubeconform-executable>
python -m unittest discover -s infra\scripts -p test_*.py -v
python infra\scripts\preflight.py --target container-apps --parameters infra\container-apps\main.parameters.local.json
```

Validation compiles both targets, inspects nested ARM policies, runs strict Helm
lint, renders four verticals with and without CSI references, checks Kubernetes
and pinned Gateway/CSI schemas without skipping CRDs, and rejects mutable
images. Public schema downloads contain no application content.

Offline checks do not prove Azure deployment success, image pull, live RBAC,
guest/auth contracts, private connectivity, actual Gateway programming, model
availability, quota, TLS, probe behavior, production backend capabilities,
multi-replica effects or OpenTelemetry export. Those are mandatory live gates,
not passed results.

### Recorded offline verification (prior full-schema run)

The final owned-file checks ran with Python 3.13.5, PyYAML 6.0.3,
Bicep 0.46.1, Helm 3.17.3, kubeconform 0.6.7 and Ruff 0.16.2:

```powershell
python -m ruff check infra\scripts
python -m ruff format --check infra\scripts
python infra\scripts\validate.py --helm .tools\infra-followup\helm\windows-amd64\helm.exe --kubeconform .tools\infra-followup\kubeconform\kubeconform.exe
python -m unittest discover -s infra\scripts -p test_*.py -v
```

| Check | Actual result |
|---|---|
| Bicep compilation and nested ARM policy checks | Both ACA and AKS passed; AKS Container Insights default-off wiring asserted; Claude attestation schema caveat above remains |
| Strict Helm lint/render | Four verticals × existing-secret/CSI-reference configurations passed |
| Kubernetes 1.32 and pinned Gateway API 1.3.0/CSI 1.4.8 schemas | 456 rendered objects valid in eight renders; zero invalid, errors or skipped CRDs |
| Insecure chart inputs | Nine rejection cases passed: mutable image, wrong Gateway class, public Cosmos CIDR, boundary override, swapped role, missing gateway secret references, zero warm replicas, missing guest issuer and non-HTTPS guest JWKS |
| Offline operator preflight tests | Ten tests passed, including unattested model availability, public merchant sources, missing auth/secrets, protected configuration, Claude attestation, example guest settings and host/worker/gateway auth agreement |
| Ruff check and formatting | Both passed for infrastructure Python scripts |

Downloaded local validator binaries and generated render fixtures are disposable
validation artifacts, not application/release artifacts. CI installs its own
pinned tools. This record is not a Python 3.11 application regression, a container
build, a live Gateway admission test, Azure template validation/what-if, or an
availability/quota/RBAC/private network/inference result. None of those live
operations was performed.

### Final static RBAC verification (2026-11-04)

The final scope review corrected the grants described above. Actual commands:

```powershell
New-Item -ItemType Directory -Force .validation\rbac | Out-Null
$env:AZURE_BICEP_CHECK_VERSION='false'
$env:AZURE_CORE_COLLECT_TELEMETRY='no'
az bicep build --file infra\container-apps\main.bicep --outfile .validation\rbac\aca.json --only-show-errors
az bicep build --file infra\aks\main.bicep --outfile .validation\rbac\aks.json --only-show-errors
python -m ruff check infra\scripts
python -m ruff format --check infra\scripts
python -m unittest discover -s infra\scripts -p test_*.py -v
python -c "import json,sys; sys.path.insert(0,r'infra\scripts'); from validate import check_arm; check_arm(json.load(open(r'.validation\rbac\aca.json'))); check_arm(json.load(open(r'.validation\rbac\aks.json'))); print('ACA and AKS: compiled nested ARM privacy/network/RBAC assertions passed')"
git --no-pager diff --check -- infra docs\azure-infrastructure.md docs\azure-private-runners.md
```

Both `az bicep build` commands exited 0 without diagnostics. Ruff lint/format
passed. **20 tests passed**: 13 in `infra/scripts/test_preflight.py` and seven in
`infra/scripts/test_rbac.py`. Compiled nested ARM assertions passed for both
targets, including secret-level vault scopes, subnet-only control networking,
no key-listing/generic runtime control role, and container-scoped SQL grants.
The role map was reviewed at the entrypoint call sites as well as in nested
assignment resources; empty frontend grants and conditional pull/secret grants
are intentional. This is not a live effective-access evaluation.

Only Bicep/Python/docs changed during this final review; no Helm manifests or
schemas changed, so the prior 456-object render/schema matrix was not repeated
and no removed validators were rediscovered. Compilation outputs are disposable.
No live authentication, account query, ARM validation, what-if, policy,
provider inference, resource provisioning or revocation ran. The parent
deployment plan remains **Approved, not Validated**, pending authorized live
checks; this workstream does not authorize deployment.

## First-party references

- [Managed app-routing Gateway API](https://learn.microsoft.com/azure/aks/app-routing-gateway-api)
- [Gateway annotation and ConfigMap customization](https://learn.microsoft.com/azure/aks/istio-gateway-api#annotation-customizations)
- [ACA firewall dependencies](https://learn.microsoft.com/azure/container-apps/use-azure-firewall)
- [ACA VNet security](https://learn.microsoft.com/azure/container-apps/firewall-integration)
- [Claude Bicep deployment and Marketplace attestation](https://learn.microsoft.com/azure/developer/ai/how-to/deploy-claude-foundry)
- [Cosmos transaction boundary](https://learn.microsoft.com/azure/cosmos-db/nosql/transactional-batch)
