# Azure deployment plan

**Status: Ready for Validation - owned identity completed; workload release pending.**

## Current deployment request

Deploy an isolated retail sample-data demo. The user selected this mode and
confirmed reuse of the existing subscription, Sweden Central and foundation.
The subscription name and identifiers remain in ignored local configuration.
AKS installation remains excluded. This is a small, cost-oriented demonstration,
not production commerce integration.

The existing Container Apps environment is internal and has no application
workloads. Its domain is not a public application URL. The proposed public
shopper entry therefore needs a separate external demo environment. Keep the
merchant workloads in the existing internal environment, without replacing its
network or changing the production deployment defaults.

The user approved the concrete rollout below. No new application, model, directory
registration, subnet or environment has been provisioned for this request.
The previous private-image-build acceptance does not validate this rollout.

## Proposed retail demo

Use standalone Bicep and Azure CLI orchestration, reusing the existing Bicep
modules, immutable-image workflow and application contracts. Do not initialize
a new project or redeploy the complete foundation template.

| Component | Placement and configuration |
|-----------|-----------------------------|
| Shopper gateway | Public HTTPS in a new VNet-integrated Consumption environment; Entra platform authentication restricted to the operator's object ID |
| Shopper API and retail storefront | Environment-internal ingress in the new environment |
| Merchant gateway | VNet-exposed ingress in the existing internal environment; no public merchant endpoint |
| Merchant API and retail merchant frontend | Environment-internal ingress in the existing internal environment |
| Role workers | Existing `commerce_host worker` command serves API and worker in the same process, one instance per role |
| Model | Existing private Azure OpenAI account; `gpt-4.1-mini`, version `2025-04-14`, regional Standard deployment, capacity 100 |
| Session and identity state | Two new demo-only Cosmos containers, partition `/pk`, enabled TTL, sharing the existing database autoscale throughput |
| Credentials | Existing private Key Vault, demo-only secret names and per-secret runtime readers |
| Runtime access | Separate role and gateway managed identities; reuse the separate registry-pull identity |
| Monitoring | Existing Log Analytics and Application Insights; no prompts, credentials or authorization codes in logs |

Deploy six long-running apps, each with 0.5 vCPU, 1 GiB memory, one warm replica
and maximum one replica. Use single active revisions and the existing draining,
stream segmentation and durable-job mechanisms. Revision transitions and
restarts can reset fixture commerce state; this is not a high-availability
backend design.

The new environment uses a new, non-overlapping delegated subnet and a dedicated
NSG, with the existing firewall route and private-service DNS. Deploy the subnet
as a child of the existing VNet; never replace the VNet's subnet collection.
Public environment ingress does not pass through the subnet firewall or NSG.
Entra authentication and the operator allowlist are therefore required before
enabling the shopper gateway's public ingress. Keep all other shopper ingress
environment-internal.

The merchant gateway's private-boundary setting corresponds to an actual
internal environment. Its browser URL requires a working private network path
and private DNS. This rollout does not provision a VPN or claim the merchant
portal is publicly reachable. Private service checks run inside the VNet.

The public demo uses the platform-managed HTTPS hostname. It does not deploy
Application Gateway WAF, custom domains or certificates, and does not replace
the production private-environment/WAF design. Existing ACR, Cosmos, Key Vault
and model endpoints remain private; do not enable account keys or public access.

## Identity and secret gates

Use the supplied single-tenant demo OIDC registration with exact gateway and
platform callback URLs, the application's own API scope and v2 access tokens. Configure
the real issuer, audience and tenant; do not substitute a Graph access token.
Keep the existing gateway token validation, CSRF, principal binding and
server-enforced merchant/approver roles.

Read-only checks confirm that tenant policy allows user application registration.
The operator has the Global Reader directory role, not an application
administrator role. The supplied application name resolves to one application
in the confirmed tenant, and the operator owns its registration. Its API scope,
v2 token setting and role definitions are configured. Both merchant/approver
assignments are verified on the exact operator and enterprise application.
The operator also owns the enterprise application. Its existing ownership can
authorize application-specific user assignments, subject to Graph client
permissions and tenant restrictions. The user explicitly approved use of that
existing ownership for only the two demo roles, without directory escalation
or new Graph permissions. A denied API operation remains an administrator gate.
Establish the required identity configuration before
starting billable application/model rollout; stop for administrator action
instead of weakening role checks or declaring the merchant app usable.

Use a manual, short-lived setup job inside the existing private environment to
initialize only demo-namespaced Key Vault secrets. Generate gateway signing and
encryption keys inside that job. Transfer any OIDC secret through secure
deployment parameters and secret references, never source, command output,
browser configuration or URLs.

The setup identity needs a temporary Key Vault Secrets Officer assignment on the
existing vault and a temporary Cosmos data role scoped to the new demo runtime
container for its setup fence. Obtain explicit approval for both assignments
at execution, revoke their exact IDs after setup, verify their absence, and stop
if revocation fails. Incremental ARM deployment with a disabled permission flag
does not revoke an existing assignment.
Runtime identities receive only named-secret reader grants, scoped Cosmos
data-plane access and role-specific model permissions; no runtime
Owner/Contributor grants. Do not change the protected build identities' grants.

Initially deploy the shopper gateway without public ingress. Enable its Entra
authentication, tenant and operator restrictions before publishing it. Only
non-sensitive health and public-key discovery routes may be exempted where
required. Reject unauthenticated API access and non-allowlisted users.

## Application and release changes

Fix gateway managed-identity selection to honor `AZURE_CLIENT_ID`, with a focused
test: each gateway has a workload identity and a separate image-pull identity.
Do not merge those identities to avoid selecting the correct one.

Add an opt-in demo Bicep entrypoint, staged setup/deployment tooling, configuration
examples, tests and a runbook. Preserve production fail-closed defaults and
explicit `COMMERCE_MODE=fixture` on demo role hosts. Display sample-data labeling
in the retail demo without changing production behavior.

The fixture catalog, inventory and commerce operations are process-local,
role/principal-scoped sample state. They can differ between roles and reset on
restart or cache eviction. Cosmos session, identity and workflow records remain
durable; this does not make fixture inventory or orders durable. Checkout is a
host handoff, not payment processing. Do not claim real orders or fulfillment.

Publish source changes through a feature-branch PR with required attribution and
merge only after passing CI. A new gateway/setup image requires one new protected private build.
Use one reviewed source revision and exact image digests, with the existing
normal environment review and exact disposable VM/NIC cleanup. No protection
bypass or additional build retry is authorized by this proposed plan.

## Resource inventory and capacity

Read-only quota evidence is retained in ignored local files. Quota headroom
does not guarantee physical capacity or model deployment success.

| Resource or capacity | Proposed addition | Observed usage and limit |
|----------------------|-------------------|--------------------------|
| Container Apps environments | 1 | Regional environment usage 1 of 50; total becomes 2 |
| Long-running Container Apps | 6 | Existing apps 0; three apps in each environment |
| Consumption CPU | 3 vCPU steady-state across both environments | Existing internal environment uses 0 of 500 cores; new-environment quota is not available before creation |
| Manual setup job | 1, bounded execution only | Existing internal environment has capacity; no continuously running setup worker |
| Build VM | 1 disposable D4ds_v5, 4 vCPU | Regional and Ddsv5-family usage each 0 of 100 vCPU; VM usage 0 of 25,000 |
| Build NIC | 1 disposable | Regional usage 7 of 65,536 |
| Demo subnet | 1 | Existing VNet has 5 subnets; total 6, below returned limit 3,000 |
| Demo NSG | 1 | Regional usage 6 of 5,000; total 7 |
| Azure OpenAI Standard model capacity | 100 units | Regional gpt-4.1-mini Standard usage 0 of 5,000; version/SKU offered by the account |
| Cosmos containers | 2 | One database and two containers currently; total five database/container resources against account limit 500, four shared-throughput containers against limit 25 |
| Workload/setup managed identities | 5 | Four runtime identities and one temporary setup identity; no directory app-role escalation |
| OIDC application and service principal | Reuse the supplied registration and enterprise application | Operator owns both objects; API settings and both actual user-role assignments are verified |
| Cosmos data-plane role assignments | 5, including one temporary setup assignment | Existing account has 6 against limit 2,000 |
| Azure RBAC grants | 11, including temporary setup grant | Two model, two telemetry, six per-secret runtime readers and one temporary vault writer; visible subscription assignments 48 against limit 4,000 |
| Demo Key Vault secrets | 5 | Existing vault reused; apply secret-size and request-rate limits, not a fictional resource quota |

Sources: Azure quota CLI for Microsoft.App, Microsoft.Compute and
Microsoft.Network; account model offerings and Cognitive Services usage API;
Cosmos ARM reads and
[Cosmos service limits](https://learn.microsoft.com/en-us/azure/cosmos-db/concepts-limits)
after the Cosmos quota CLI returned `BadRequest`.

The consumption-core quota is environment-scoped. After creating the new
environment, read its actual quota before deploying workloads; require at least
4 cores for the planned replicas and revision-transition headroom. Do not copy
the old environment's 500-core limit to the new environment. The existing
database has shared autoscale maximum 4,000 RU/s; do not raise that maximum for
the demo. No new database account, registry, vault, firewall, AKS cluster,
separately managed public-IP resource or reserved model capacity is proposed.

## Cost and acceptance gates

Six 0.5-vCPU/1-GiB apps cost approximately USD 2.33/day at all-idle rates or
USD 7.78/day at all-active rates before free grants, using current Sweden Central
Consumption prices. The two API/worker processes poll durable queues and should
not be budgeted as all-idle. Model tokens, Cosmos autoscale utilization, storage,
logs, network and applicable environment features are additional usage charges.
These estimates are not a spending cap. Existing foundation charges continue.
The proposed Standard model capacity is a rate allocation, not provisioned
throughput or a token-spend cap.

The disposable build VM's listed compute rate is approximately USD 0.24/hour;
its billable duration includes setup and cleanup, not only Docker execution.
Do not leave the build VM or setup permissions running after their bounded work.

1. Approve this demo architecture and cost before implementation.
2. Implement and check the gateway fix, demo controls, source release and Bicep.
3. Check identity permissions, configuration, policy, quotas, exact RBAC changes,
   secure secret handling and staged ARM what-if; stop on unresolved blockers.
4. Complete the protected private image build and verify immutable digests,
   normal review and exact compute cleanup.
5. Mark the plan Ready for Validation, invoke `azure-validate` and record actual
   proof before `azure-deploy`. Validate each staged release before applying it.
6. Establish the identity configuration, private setup and model deployment,
   then both environments' apps. Keep public ingress closed until access
   controls are verified.
7. Verify private image pull, Key Vault/Cosmos/model access, worker readiness,
   anonymous/unauthorized rejection, browser sign-in, a real sample shopping
   turn, streaming and persisted session state. Verify merchant isolation and
   role/approval rejection from the private network.
8. Report the actual shopper HTTPS URL and the merchant private-access
   requirement. Record any unverified interactive or private-network checks
   explicitly; do not declare full acceptance while those checks remain open.

### Execution state

- [x] User selected isolated retail sample-data mode and existing Azure target.
- [x] Inspect shared hosts, images, networks, model offerings and quota evidence.
- [x] User approves this concrete plan.
- [x] Fix explicit gateway workload-identity selection and its entrypoint tests.
- [x] Resolve identity configuration and actual operator assignments using explicitly approved existing ownership.
- [x] Implement demo artifacts, secret initializer, identity inspection and sample-data labeling.
- [ ] Publish source through reviewed CI and merge.
- [ ] Complete one protected image build and verify cleanup.
- [ ] Complete `azure-validate` and record validation proof.
- [ ] Complete staged `azure-deploy` and functional verification.

## 7. Validation Proof

Application deployment validation has not run. Read-only discovery and the
previous image-build acceptance are not substitute validation proof.

Source checks passed for the gateway, private setup and managed-identity telemetry (75 tests),
infrastructure tooling (59 tests, one existing skip), both retail production
frontend builds, Ruff, repository consistency, all three demo Bicep entrypoints
and their compiled resource-boundary checks. Shared browser contracts pass
17 tests. The full local Python run passes 1,302 tests with one skip but fails
one unchanged POSIX file-permission assertion on Windows; Linux CI must pass
before source merge. All eight frontend builds pass. These are
source checks, not live deployment acceptance.

Initial foundation provider validation passed. Its first preview exposed four
unresolvable role-assignment IDs referencing not-yet-created API identities.
The foundation now gates model/telemetry grants behind `enableApiAccess=false`
by default. Validate the initial identity/environment stage separately, then
resolve the actual principal IDs and validate the explicit API-access update.
The rerun passes provider validation and an exact first-stage preview: fourteen
owned creates, no modifications, deletions or unsupported entries. Explicit
subscription/tenant authentication and an unused namespace are verified.
Inherited policy assignments were retrieved; the initial provider validation
reports no policy denial. This does not validate later model, secret or
application stages.

The standalone Bicep validation checklist applies separately to each approved
deployment stage:

- [x] Compile the demo foundation, setup and application templates.
- [ ] Validate each stage with `az deployment group validate` and actual approved parameters.
- [ ] Inspect each stage with `az deployment group what-if`; reject unrelated changes.
- [ ] Refresh explicit subscription/tenant authentication before execution.
- [ ] Check applicable Azure Policy assignments.
- [ ] Verify runtime and temporary role definitions, scopes and required data operations.
- [ ] Verify the reviewed source release and new private immutable image manifest.
- [ ] Record all passing proof before setting the relevant deployment stage Validated.

### Static role verification

The four new runtime identities use native Cosmos data roles scoped to their
respective demo containers. Only the two role API identities receive model
inference and component-scoped Monitoring Metrics Publisher grants in the
explicit API-access stage. Only gateway identities receive their three
namespaced secret readers. Existing image-pull identity permissions are reused,
not duplicated on application identities. The setup identity's vault writer
and demo-container data grant require separate approval and exact revocation.
No new runtime Owner, Contributor or account-key access is defined. Live role
verification remains a post-application gate.

Owned-registration configuration is a separate, non-billable directory stage.
Its exact API/role-definition patch and administrator assignment request bodies
are generated privately without secrets. The user authorized the existing owner
to configure this application and assign only its two demo user roles to the
confirmed operator. Its scope excludes credentials, directory roles, new Graph
permissions, Azure resources and public ingress.

The owned-identity stage validation passes: read-only Microsoft Graph confirms
the exact operator owns both the single-tenant registration and its enterprise
application; the proposed patch has only API settings, role definitions and the
application ID URI. The owned-setup tests pass, including wrong-tenant,
wrong-operator, missing-ownership, propagation and idempotency cases. All 66
infrastructure tests pass with one existing skip; Ruff passes. Scope persistence
is verified before client preauthorization, as required by the live Graph API.
Actual command
timestamps and target IDs are retained in ignored local evidence. This Graph
stage uses explicit request preview, not a fictional ARM what-if for directory
objects. The owned-identity stage executed successfully and read-back verifies
the API settings and both exact user assignments. No directory privileges or
Graph permissions were added. Workload validation and publication of the new
private image release remain incomplete.

### Owned-identity readiness

The Microsoft Graph
[app-role assignment API](https://learn.microsoft.com/en-us/graph/api/serviceprincipal-post-approleassignedto?view=graph-rest-1.0)
requires a supported directory role or custom permission in addition to Graph
permissions. Global Reader alone is not sufficient. Existing object ownership
also contributes effective permissions and is verified for both application
objects; a denied assignment API still requires administrator assistance.

The user approved using existing dual ownership to complete this identity
stage, with administrator assistance only if the API denies that operation.
Keep both roles in scope; do not silently switch to a shopper-only release. The
[administrator handoff](../docs/azure-demo-identity.md) and
[user-role manifest](../infra/container-apps/demo-app-roles.json) define the
required isolated registration. The supplied application has been resolved;
its client ID, object IDs, ownership proof and exact administrator assignment
requests are retained privately. Owned API configuration and both exact
merchant/approver assignments now verify; this identity gate is resolved.

Do not grant the operator a privileged directory role automatically, weaken the
host's merchant checks, or deploy an unusable merchant portal and report it as
accepted. Demo source artifacts are implemented locally; source publication,
the new image release and Azure deployment remain incomplete.

## Completed private-build scope

The completed scope is the private build and push of ten application images:
the gateway, the role API, and the storefront and merchant frontends for retail,
travel, telecom and entertainment. This is build acceptance, not application
deployment, image promotion or production commerce acceptance.

Container Apps is the selected application hosting target. The repository also
contains independent AKS infrastructure, but AKS installation is excluded from
this rollout. That closed milestone authorized no additional build or workload
provisioning. The proposed demo rollout above requires its own approval.

## Architecture and interfaces

The shared Python and Next.js application supports Claude and Azure OpenAI
Responses providers, Cosmos DB for NoSQL persistence, authenticated role APIs
and durable outbox workers. External commerce systems remain authoritative;
fixture backends are lab-only and are rejected in production.

Application resources, networking and identity boundaries are described in
[Azure infrastructure](../docs/azure-infrastructure.md). The
[private runner runbook](../docs/azure-private-runners.md) defines the protected
GitHub workflow, disposable build VM, separate controller and builder identities,
scoped RBAC, private registry access and exact owned-resource cleanup.

## Completed build acceptance

- The gateway, API and all eight frontend images built and pushed to private ACR
  from a single reviewed source commit, with an exact ten-image digest manifest.
- All eight frontends self-host their original font families. The runtime
  images retain the font license notices.
- Production web builds, shared frontend tests and public container CI passed.
- The private build enforced source pinning, non-root images, gateway/API import
  checks, private registry DNS/TLS and managed-identity authentication.
- Live checks verified the build identities' scoped grants and the private
  VM/NIC configuration. Independent final reads confirmed no disposable VM,
  NIC, managed disk or public IP remained in the build resource group.

Public evidence: [font repair #7](https://github.com/avidunixuser/commerce-agents/pull/7)
and [successful private image build](https://github.com/avidunixuser/commerce-agents/actions/runs/42113812062).
Managed Run Command provides a bounded output tail, not complete per-stage logs.
Image scanning, signing and promotion are separate release gates.

## Production rollout gates

Retail production integration and application deployment are deferred outside
the completed build milestone. Before implementing or deploying that release:

1. Supply the authoritative retail backend contract or existing adapter,
   including idempotency and conditional merchant approval semantics.
2. Configure customer and workforce OIDC registrations, audiences and approver
   policy; select actual model deployments with available capacity.
3. Confirm domains, certificates, private merchant access, deployment context,
   quotas, policy, costs and least-privilege runtime assignments.
4. Approve a scoped Container Apps rollout plan. Complete `azure-prepare`,
   `azure-validate` and `azure-deploy` for that plan, then verify private service
   access, authentication, durable state, inference and real backend behavior.

Build success does not validate these production gates. No application, model,
WAF or AKS workload deployment is claimed by this milestone.

## Private configuration and evidence

Keep actual Azure identifiers, deployment parameters, image references, recovery
state and operational verification records in ignored `.azure/` files. Use the
[configuration example](../infra/private-build/config.example.json) and runbook
to prepare approved environment-specific values; never commit credentials.

The full local operational plan is preserved separately in the ignored
`.azure/deployment-plan.private.md`. This public plan intentionally omits
environment identifiers, local paths and approval/execution transcripts.
Deleting disposable compute does not remove existing shared infrastructure or
stored images; those resources remain billable.
