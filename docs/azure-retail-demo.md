# Retail demo on Container Apps

The opt-in retail demo deploys six singleton apps from immutable images. Its
three shopper components use a separate external Consumption environment; only
the gateway can acquire public ingress, after operator-only Entra authentication
is configured. Its three merchant components use the existing internal
environment. Private merchant browsing requires private connectivity and DNS.

The role API uses `python -m commerce_host worker`, serving HTTP and durable work
with the same fixture instance. This is intentionally different from production
API/worker separation. Fixture commerce state is process-local, can differ
between roles and can reset on eviction, restart or revision changes. Session,
identity and outbox records use isolated Cosmos containers, not durable fixture
inventory. There are no real payments, orders or fulfillment.

Role hosts reuse the existing privacy-limited Application Insights telemetry,
authenticated with their explicit managed identities and component-scoped
Monitoring Metrics Publisher grants. Gateway and frontend logs use the existing
environment Log Analytics integration. No request-body auto-instrumentation is
enabled.

## Interfaces

| File | Purpose |
|------|---------|
| `infra/container-apps/demo-foundation.bicep` | Initial-only demo subnet, environment, containers, identities and native Cosmos grants |
| `infra/container-apps/demo-access.bicep` | Model and scoped API/secret access using verified literal principal IDs |
| `infra/container-apps/demo-setup.bicep` | Manual private initialization job and explicitly approved temporary permissions |
| `infra/container-apps/demo-apps.bicep` | Role apps and Entra platform authentication; public ingress defaults off |
| `infra/container-apps/demo.config.example.json` | Non-secret operator configuration |
| `infra/scripts/container_apps_demo.py` | Fail-closed validation and non-secret parameter generation |
| `infra/scripts/demo_identity.py` | Tenant-bound identity inspection and optional owned-registration configuration |
| `apps/host/commerce_host/demo_setup.py` | One-shot, Cosmos-fenced initialization of five namespaced vault secrets |

These are separate entrypoints. Do not enable fixtures in the production
`main.bicep`, replace the existing VNet subnet array or relax private backing
service access. Foundation references must name existing services in the same
resource group. The new subnet must be unused, inside the existing VNet and
attached to the existing firewall route. Public ACA environment ingress does
not traverse its subnet NSG or firewall.

Outbound private-address denials use explicit RFC1918 ranges after the DNS,
private-endpoint and intra-environment allowances. Do not replace these ranges
with the `VirtualNetwork` service tag: the default firewall UDR can expand that
tag to `0.0.0.0/0` and unintentionally block required public HTTPS dependencies.

## Prepare

Install the repository's existing Azure requirements. Copy the configuration
example into an ignored `.azure/` file and replace every placeholder with the
confirmed target and identity values. The client ID is a GUID, not an app name.
Use the [identity setup](azure-demo-identity.md) for directory requirements.

```powershell
python infra\scripts\demo_identity.py --config .azure\demo.runtime.json
python infra\scripts\container_apps_demo.py --config .azure\demo.runtime.json foundation > .azure\demo-foundation.parameters.json
python infra\scripts\container_apps_demo.py --config .azure\demo.runtime.json --manifest .azure\demo-images.json apps > .azure\demo-apps.parameters.json
```

The manifest is the exact ten-image result from one reviewed protected private
build. Independently match its source SHA to the accepted workflow evidence;
a digest identifies content but does not prove source or review. Never substitute
an older image that lacks the setup module or demo labeling.

`demo_identity.py` is read-only unless explicitly passed
`--configure-owned-app`. That option checks the signed-in operator owns this
single-tenant registration before configuring its own API scope, v2 tokens and
role definitions. It does not assign roles, create directory-admin grants,
add Graph permissions or create client secrets. Incompatible existing roles,
scopes or identifiers are rejected rather than overwritten.

When explicitly authorized, an operator who owns both the registration and its
enterprise application can run:

```powershell
python infra\scripts\demo_identity.py --config .azure\demo.runtime.json --complete-owned-setup
```

This configures only the owned API and assigns missing demo user roles only to
the confirmed operator. It checks both ownerships before writes, rejects
cross-tenant principals and requires propagated role definitions before role
assignment. It never escalates directory privileges. If propagation is incomplete, inspect and rerun
after it completes. Read-only verification precedes repeated assignments;
uncertain failures are never blindly replayed. A permission denial requires the
administrator path, not new Graph permissions or directory roles.

The identity inspector exits nonzero until both real operator assignments and
the API configuration are verified. It returns non-secret administrator batch
requests for the missing assignment work. A successful batch HTTP envelope is
not proof that its individual operations succeeded; inspect each result and
rerun the read-only inspector. Global Reader alone is not assignment authority;
verify effective application ownership rather than assume it.

## Validation and release

Complete `azure-prepare`, `azure-validate` and `azure-deploy` for the approved
stage. Compile all four templates and run their offline policy checks through
`infra/scripts/validate.py`. Complete scoped Azure validation, policy and ARM
what-if before applying a stage. Stop on deletion, replacement of shared
resources, unexpected scopes, unsupported model SKU or missing identity access.
Do not treat local parameter generation as Azure validation.

Record source, manifest, actual role assignments, model offerings, quota and
what-if evidence privately. If the registration still needs an owned API update,
validate that registration-only stage independently; passing that stage does
not authorize or validate the billable app rollout.

The foundation creates no model or Azure model/telemetry/secret role assignments.
Before billable rollout, verify directory configuration and assignments. Read actual
new-environment consumption quota after creation, allowing revision-transition
headroom. Verify model availability and regional quota before explicitly
enabling the selected model. Keep existing Cosmos shared-throughput maximum
unchanged.

After the initial foundation succeeds, read its real managed identities into an
ignored inventory and generate access parameters:

```powershell
az identity list --subscription $SubscriptionId --resource-group $ResourceGroup --output json > .azure\demo-identities.json
python infra\scripts\container_apps_demo.py --config .azure\demo.runtime.json --identities .azure\demo-identities.json access > .azure\demo-access.parameters.json
```

The generator binds each of the four separate principal IDs to the exact
subscription, resource group, name and tenant. Do not hand-edit or reuse a stale
inventory; reread it and compare immediately before validation and deployment.
The dedicated access template creates the approved model plus only the two API
identities' inference and Application Insights grants. Initial access parameters
keep secret readers disabled. Its role names use literal principal inputs,
not unresolved resource references inside GUID expressions.

Do not reapply the initial foundation to change access. A full-foundation
preview can contain unresolved role names and unrelated normalization updates,
even when the identities already exist. Validate the dedicated access stage,
require the exact model and four grant creates, and reject `Unsupported`
entries or changes outside that stage.

## Private secret setup

The setup template takes an ARM `secureString` parameter for the OIDC credential.
Use an authenticated deployment mechanism that submits secure parameters in the
TLS request body; do not put a secret in CLI arguments, command transcripts,
source files, build arguments or browser configuration. Do not log request
bodies or run the setup with Azure CLI HTTP debug logging.

Before deploying this stage, obtain approval for both temporary assignments:
Key Vault Secrets Officer on the named existing vault, and native Cosmos data
access on only the new demo runtime container for the initialization fence.
The role IDs returned by the template identify the exact assignments to revoke.
Approval for the runtime secret readers is not approval for vault-wide setup
permission.

Run the manual job only after private DNS and RBAC propagation are verified.
It has one replica, no automatic retries and a five-minute execution limit.
Secrets are tagged with the demo namespace. Existing unrelated secrets are never
overwritten; existing owned keys are validated and retained. An unexpected
OIDC credential change requires explicit rotation.

A conditional Cosmos create records the setup fence before secret writes.
Duplicate executions fail without rotating keys. Failures deliberately retain
the fence in `running` state: do not automatically delete it or retry.
Inspect the exact stopped execution and partially created namespaced secrets
before approving removal of only that setup document. It has partition key
`demo-setup:<prefix>` and ID `gateway-secret-setup`. Never clear business session
locks as a substitute.

After confirming successful initialization, revoke both exact temporary
assignments and independently read back their absence. Remove the stopped
setup job's native secret by deleting the exact owned job after approval.
Its presence as an idle job is not continued compute execution, but its stored
OIDC credential should not remain as an unnecessary second copy. Do not claim
setup cleanup if any permission or credential-bearing job remains.

Reapply the reviewed access stage with `enableModel=true` and
`secretsReady=true` to retain model/API access and create the
per-secret gateway reader assignments. Normal runtime identities cannot write
vault secrets or obtain database account keys.

## Publish and verify

Deploy apps with `enablePublicIngress=false`. Configure exact redirect URLs on
the isolated registration after obtaining the assigned environment domains.
Keep the private merchant gateway in the internal environment and preserve
its private-client allowlist.

Read the live shopper auth configuration and verify platform authentication,
HTTPS, tenant, client ID, operator allowlist and the three non-sensitive health/
JWKS exceptions before enabling public ingress. An existing public gateway
whose policy differs must be closed before policy changes. Do not expose it
first and repair access control afterward.

Verify private image pull, vault references, Cosmos reads and worker health,
real model inference, unauthenticated/unauthorized rejection, sign-in, a sample
conversation, streaming and persisted session state. Verify merchant isolation
and role/approval rejection from the private network. Report incomplete
interactive checks explicitly. Return the actual gateway URL only after live
verification; an environment domain is not a deployed app.

The six apps use 3 vCPU and 6 GiB in steady state. Workers poll durable queues,
so do not estimate the whole demo as idle. Compute, database utilization, model
tokens, logs and network remain billable; no parameter here is a monetary cap.
