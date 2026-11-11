# Azure deployment plan

**Status: Deployed - six Container Apps healthy; operator-only shopper catalog, chat and durable replay verified.**

## Current deployment request

Deploy an isolated retail sample-data demo. The user selected this mode and
confirmed reuse of the existing subscription, Sweden Central and foundation.
The subscription name and identifiers remain in ignored local configuration.
AKS installation remains excluded. This is a small, cost-oriented demonstration,
not production commerce integration.

The original Container Apps environment is internal. The merchant workloads
are deployed there, and the shopper workloads are deployed in a separate
external demo environment. An environment domain is not a verified public
application URL. Preserve both networks and the production deployment defaults.

The user approved the concrete rollout below. The initial demo environment,
subnet, NSG, containers, identities and scoped native Cosmos grants are deployed.
The model and four scoped API grants are also deployed and verified. The
supplied directory registration is reused. The approved callbacks and ID-token
setting are configured, and one 30-day OIDC credential was created. Both
temporary setup grants and the stopped setup job have been removed after
successful private readiness, once-only initialization and separate read-back.
The five namespaced secrets and complete durable setup fence are verified.
All six exact secret-scoped runtime reader grants are deployed and verified.
All six application resources are deployed. Operator-only public shopper
ingress is now enabled after its separate validation and private readiness.
Both role APIs pass local readiness, reject missing/invalid authentication and
complete real private Azure OpenAI inference using their separate managed
identities. Both gateway application containers initially failed because their image
omitted the async Azure SDK transport dependency `aiohttp`. The accepted repair
image is now deployed to both gateways. Actual latest-revision application
containers and the shopper authentication sidecar are ready, started and
running. Private HTTP checks pass for Cosmos/OIDC/API readiness, public-key
discovery, frontend availability, missing-identity rejection and wrong-role
route rejection. Public health and anonymous API rejection pass. The operator
completed platform and gateway sign-in. After the referrer repair, ordinary
browser session creation, streaming product comparison, a context-aware
follow-up and persisted event replay after a fresh document load pass.
The catalog startup race is repaired: fresh signed-in pages read products with
200, render the featured catalog and show no 403 warning. Private merchant
interactive sign-in/approval and an actual second-account allowlist rejection
have not been verified.

The async transport repair passed normal source review and the single approved
protected gateway build. Its digest and independent disposable-runner cleanup
were accepted before the validated two-image update. The user subsequently
approved the same-origin referrer policy repair and one additional protected
gateway-only build. Its source is merged after exact-head CI, its protected
build and independent cleanup passed, and the validated two-image update is
deployed. All identity, secret, ingress, auth and API/frontend image settings
are preserved.
Do not repair running containers with ad-hoc package installation.

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
- [x] Publish source through reviewed CI and merge.
- [x] Complete one protected image build and verify cleanup.
- [x] Validate and deploy demo foundation, access, private setup and closed-ingress apps.
- [x] Verify private setup cleanup, exact runtime readers and live auth configuration.
- [x] Verify both role APIs, auth rejection and real managed-identity model inference.
- [x] Obtain approval for one protected gateway-only repair build and two-gateway update.
- [x] Repair gateway dependencies and verify focused regression coverage.
- [x] Publish the gateway repair through normal exact-head CI and merge.
- [x] Validate and complete the single protected repair build, digest acceptance and independent cleanup.
- [x] Validate and update only the two gateways with public shopper ingress closed.
- [x] Complete application verification and separately validate public shopper ingress.
- [x] Complete interactive shopper sign-in, conversation, streaming and persistence acceptance.

## 7. Validation Proof

Foundation, access, workload and repair validations are recorded below.
The final isolated-demo acceptance includes actual replicas, private/public
HTTP boundaries and signed-in shopper catalog, streaming and durable replay.
Image-build acceptance alone is not application or production acceptance.

Source checks passed for the gateway, private setup and managed-identity telemetry (75 tests),
infrastructure tooling (66 tests, one existing skip), both retail production
frontend builds, Ruff, repository consistency, all three demo Bicep entrypoints
and their compiled resource-boundary checks. Shared browser contracts pass
17 tests. The full local Python run passes 1,302 tests with one skip but fails
one unchanged POSIX file-permission assertion on Windows; Linux CI must pass
before source merge. Both Linux Python suites, all eight frontend builds,
all ten container builds and offline infrastructure checks passed in source CI.
The reviewed source is merged through PRs #9 and #10. These are
source checks, not live deployment acceptance.

Initial foundation provider validation passed. Its first preview exposed four
unresolvable role-assignment IDs referencing not-yet-created API identities.
The executed foundation version gated model/telemetry grants behind
`enableApiAccess=false`. The corrected entrypoint is initial-only; later access
uses the separate literal-principal entrypoint described below.
The rerun passes provider validation and an exact first-stage preview: fourteen
owned creates, no modifications, deletions or unsupported entries. Explicit
subscription/tenant authentication and an unused namespace are verified.
Inherited policy assignments were retrieved; the initial provider validation
reports no policy denial. This does not validate later model, secret or
application stages.

The standalone Bicep validation checklist applies separately to each approved
deployment stage:

- [x] Compile the demo foundation, access, setup and application templates.
- [x] Validate each stage with `az deployment group validate` and actual approved parameters.
- [x] Inspect each stage with `az deployment group what-if`; reject unrelated changes.
- [x] Refresh explicit subscription/tenant authentication before execution.
- [x] Check applicable Azure Policy assignments.
- [x] Verify runtime and temporary role definitions, scopes and required data operations.
- [x] Verify the reviewed source release and new private immutable image manifest.
- [x] Record all passing proof before setting the relevant deployment stage Validated.

### Static role verification

The four new runtime identities use native Cosmos data roles scoped to their
respective demo containers. Only the two role API identities receive model
inference and component-scoped Monitoring Metrics Publisher grants in the
explicit API-access stage. Only gateway identities receive their three
namespaced secret readers. Existing image-pull identity permissions are reused,
not duplicated on application identities. The setup identity's vault writer
and demo-container data grant require separate approval and exact revocation.
No new runtime Owner, Contributor or account-key access is defined. Live role
verification is recorded in the corresponding stage proofs.

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
Graph permissions were added. The new private image release is accepted;
application workload validation remains incomplete.

### Protected private-build stage validation

The current execution scope is only the one approved protected all-image build
from the reviewed main commit. It does not authorize application deployment,
temporary secret-writer grants, public ingress or a build retry.

- [x] All private-build stage validation checks pass.
  - [x] Bicep compilation: `az bicep build --file infra/private-build/vm.bicep`.
  - [x] Template validation: `az deployment group validate` for the concrete run's VM and NIC.
  - [x] What-if preview: `az deployment group what-if` contains exactly the two owned creates, with no modifications, deletions, unsupported entries or diagnostics.
  - [x] Authentication: explicit subscription, tenant and enabled-account checks pass.
  - [x] Azure Policy: inherited assignments were retrieved; provider validation reports no denial.
  - [x] Capacity: regional and Ddsv5-family usage remain zero against 100-vCPU limits; the build needs four vCPUs.
  - [x] Static identity/network verification: the VM has only the commissioned builder identity, a private NIC and the existing scoped build NSG; no role assignments or public IP are created.
  - [x] Source and protection: all sixteen source checks pass; protected configuration matches the commissioned runtime, main-only review remains enabled and administrator bypass remains disabled.

The exact preview used the pending run's name and source revision; the workflow
revalidates its own generated bootstrap key and ownership generation before
creation. The controller's `preview_vm` enforced the resource-ID allowlist.
Per-run parameters, quota results, policy reads and validation evidence remain
in ignored local files. The concrete
[protected build](https://github.com/avidunixuser/commerce-agents/actions/runs/42872613861)
received the user's explicit approval for that exact run. Normal environment
review was submitted as the authorized repository owner, without changing or
bypassing protection rules. `azure-deploy` was invoked for this bounded stage.
Live reads verified its exact private VM and NIC, the reviewed source tags and a
running VM; no public IP was created. The build succeeded and its protected
artifact contains the exact ten-image digest manifest from the reviewed main
commit. All required build, recovery-cleanup and artifact steps succeeded.
Independent final ARM reads confirm zero remaining VMs, NICs, managed disks or
public IPs in the build group. Image acceptance does not validate application
deployment. Do not approve another run or retry automatically.

### Initial demo foundation validation

Validation completed at `2026-11-10T18:32:20Z` for only the initial closed-default
foundation. Fresh read-only identity inspection remains ready and the new
protected image release has been accepted.

- [x] All initial-foundation validation checks pass.
  - [x] Bicep compilation: `az bicep build --file infra/container-apps/demo-foundation.bicep`; pinned compiler `0.46.1` and its build stamp verified.
  - [x] Template validation: `az deployment group validate` succeeds with the approved private parameter file.
  - [x] What-if preview: `az deployment group what-if` contains exactly fourteen approved resource IDs, all creates, with no modifications, deletions, unsupported entries or diagnostics.
  - [x] Authentication: explicit subscription, tenant and enabled-account checks pass.
  - [x] Azure Policy: inherited assignments were retrieved; fresh provider validation reports no denial.
  - [x] Static roles: four native Cosmos assignments target only the new demo containers; model, telemetry, secret-reader and temporary setup grants remain disabled.
  - [x] Release and capacity: all source checks pass, ten immutable images are accepted, the existing regional environment limit allows the second environment and the namespace remains unused.

The approved additions are one external environment, its subnet and NSG, two
shared-throughput Cosmos containers, five managed identities and four scoped
native Cosmos assignments. `enableModel`, `enableApiAccess` and `secretsReady`
are all false. No application, OIDC credential, temporary writer permission or
public application ingress is part of this stage. Record its live outputs and
the new environment's actual core quota before validating later stages.

The initial deployment succeeded. Live reads verify the new external environment,
its assigned domain and all five new identities. Four native Cosmos grants
have the exact intended container scopes; the setup identity has no native
data grant. `az containerapp env list-usages` independently reports zero of
500 consumption cores for each environment, exceeding the required four-core
headroom. The second environment's limit is observed, not copied from the first.
The next stage must independently validate the model and API/telemetry grants.

### Remaining-stage preparation correction

The model/API-access provider validation succeeds, but its full-foundation
what-if does not pass. It contains four unsupported assignment names because
ARM cannot resolve managed-identity references inside their GUID expressions,
even after the identities exist. It also proposes eight updates to the existing
environment, containers, native assignments and subnet. Nothing from this
failed preview has been applied.

The user approved a dedicated access entrypoint using freshly read, validated principal
IDs as literal parameters. It creates only the already approved model and four
API inference/telemetry grants. A later invocation adds only the six approved
named-secret readers. Keep their role IDs, scopes and stable assignment names;
do not redeclare the environment, network, containers or native data grants.
Make the foundation initial-only, update parameter generation, offline checks,
tests and the runbook, then publish the correction through passing source CI.
Application image inputs remain unchanged, so reuse the accepted immutable
images rather than launch another private build. Verify that separation before
deployment. Re-run exact provider validation and what-if for each corrected
stage; unsupported entries and unrelated changes remain blockers.

The correction is implemented. Four demo templates and both production targets
compile and pass isolation checks. Seventy infrastructure tests pass with one
existing skip; Ruff and repository consistency checks pass. The shared
named-secret reader module retains the original assignment GUID inputs.
No application image input differs from the accepted image source.

### Isolated model and API access validation

Provider validation and the exact access preview completed at
`2026-11-10T18:57:22Z`. The preview contains only the intended model and four
API grants. All fifteen expected checks pass on the corrected source, merged
through PR #11. Fresh post-merge provider validation and preview pass at
`2026-11-10T19:07:45Z` with the same five exact creates.

- [x] All isolated-access validation checks pass.
  - [x] Bicep compilation: `az bicep build --file infra/container-apps/demo-access.bicep`; all four demo and both production entrypoints compile and pass resource-boundary checks.
  - [x] Template validation: `az deployment group validate --validation-level Provider` succeeds with the dedicated access template and generated literal-principal parameters.
  - [x] What-if preview: `az deployment group what-if --validation-level Provider --result-format FullResourcePayloads` reports exactly five intended creates, no modifications, deletions, unsupported entries or diagnostics.
  - [x] Authentication: explicit subscription, tenant and enabled account match; freshly read identity resource IDs, tenants and principals match the generated parameters.
  - [x] Azure Policy: the policy tool retrieves nine inherited assignments; provider validation reports no denial and no policy change is proposed.
  - [x] Static roles and preview bindings: two exact API principals receive only account-scoped OpenAI User and component-scoped Monitoring Metrics Publisher. Stable assignment GUID recipes and service-principal types are retained; gateways and setup receive no new grant.
  - [x] Image provenance: no Dockerfile, requirements, package, application, example, license, ignore or private-build-script input differs from the accepted image source. Reuse its ten immutable images without another private build.
  - [x] Corrected source CI and exact-head merge pass without bypass.
  - [x] Refresh model quota and identity bindings; verify recorded template and parameter fingerprints before application.

The ignored validation proof records the five resource IDs and template and
parameter hashes. The sealed Bicep principal type compiles to an ARM schema
definition; the only additional declared account is an `existing` reference,
not a creatable account. The isolation check excludes that read-only declaration
without permitting any foundation resource creation or update.
The offered regional Standard SKU supports Responses and has 100 units required
against observed usage zero and limit 5,000; its inference retirement is in the
future. Fine-tuned and global SKUs are excluded. The deployed model remains
subject to actual regional capacity. This validation authorizes no temporary
secret setup grant, credential creation, application workload or public ingress.

`azure-deploy` executed only `retail-demo-access-initial` in incremental mode;
the deployment succeeded at `2026-11-10T19:08:55Z`. Live verification at
`2026-11-10T19:11:23Z` confirms the exact model/version, Standard capacity 100
and disabled automatic upgrades. `az role assignment list` at the model
account and Application Insights component verifies all four expected
assignment IDs, principals, roles and exact scopes. The model account still
disables public access and local authentication and retains its approved
private endpoint. This is management-plane deployment verification, not
inference or application acceptance.

Fresh owned-registration inspection verifies both user roles and API settings;
the application has no redirect URLs or password credentials. The next bounded
setup proposes adding only the three exact HTTPS callbacks and one 30-day OIDC
credential, plus the two temporary setup grants. Its separate user approval
must include revoking those exact grants and deleting only the stopped
credential-bearing setup job. No credential, temporary setup grant, job or
application has been created.

The user explicitly approved this bounded private setup: the three callbacks,
one 30-day credential, both temporary grants, once-only initialization, exact
grant revocation and deletion of only the stopped setup job. Microsoft documents
ID-token issuance as required for Container Apps EasyAuth's confidential-client
hybrid sign-in. The user separately approved only that required setting.
Implicit access-token issuance remains disabled and the gateway
retains its authorization-code/PKCE flow. No directory permissions or roles are
added by that setting.

The private operator transport is prepared and passes focused linting. Its
credential value stays in memory and secure TLS request bodies; only explicitly
selected metadata is written. A persisted attempt marker prevents blind replay
of password creation. The setup template compiles and its non-secret validation
parameters select the accepted API image. Fresh ownership, actual environment
domains and the exact existing registry-scoped image-pull grant are verified.
The next step validates the prospective three-create setup stage before any
credential or permission write.

### Private credential/setup validation

The following proof records the successful pre-deployment checks, not a
successful setup deployment. The subsequent failure and cleanup gate below
supersede the stage's earlier Validated status.

The private setup stage passes at `2026-11-10T19:31:09Z`. No credential or
temporary grant has been created during validation.

- [x] All private setup validation checks pass.
  - [x] Bicep compilation: `az bicep build --file infra/container-apps/demo-setup.bicep`; the reviewed source and accepted immutable API image are unchanged.
  - [x] Template validation: the private operator transport submits the compiled template to ARM's resource-group deployment `validate` operation with Provider validation and a non-credential placeholder.
  - [x] What-if preview: its ARM `whatIf` operation reports exactly three approved creates: the manual setup job, vault-scoped Secrets Officer and new-demo-container-scoped native Cosmos Data Contributor. No modifications, deletions, unsupported entries or diagnostics are accepted.
  - [x] Authentication and identity: current Graph ownership/API-role readiness, exact assigned environment domains and the tenant/resource-bound setup identity pass. Only the exact existing-identity reference is accepted where ARM leaves a principal expression unresolved; its live principal is independently bound and must be checked after deployment.
  - [x] Azure Policy and static roles: current provider validation reports no denial; definitions, target scopes, manual trigger, one replica, 300-second timeout, zero retries and exact image/command are verified.
  - [x] Source, pull access and safeguards: all expected source CI passes; the dedicated pull identity's exact registry-scoped AcrPull grant exists. Focused lint and eight malformed-preview rejection cases pass. Config, template, manifest and non-secret parameter hashes are retained privately.
  - [x] Consent: both temporary grants, one 30-day credential, exact callbacks, ID-token-only issuance change, once-only initialization, exact grant revocation and deletion of the stopped setup job are explicitly approved.

The ARM REST asynchronous what-if result stores its change list under
`properties.changes`; the transport validates that real envelope rather than
mistaking the missing flattened CLI field for an unsupported resource. Only a
placeholder-bearing preview can be retained in full. Real credential-bearing
validation is repeated in memory before applying the same exact resource IDs.
An attempt marker is persisted before password creation; uncertain creation is
never replayed automatically. Actual initialization and both permission
revocations remain unverified.

### Private setup execution reconciliation

The original incremental setup deployment completed successfully just after
the transport's 900-second polling deadline. Earlier job GET responses showed
a failed/default/empty representation while the parent deployment remained
Running. Final reads at `2026-11-10T19:50:49Z` show Succeeded, Manual and the
intended single container and native secret name. No credential was recreated,
no execution was started and no network setting was changed.

AppLens could not authenticate its configured connection. Resource Health
does not support this resource type. Direct environment detectors had
incomplete DNS/network telemetry and did not establish a network fault.
The actual workload NSG has no explicit rules. No network repair is justified
by these observations.

- [x] All private readiness-execution validation checks pass.
  - [x] Recompile `demo-setup.bicep`; its SHA-256 matches the originally validated and deployed template exactly.
  - [x] Retain the successful original provider validation and exact three-create preview; no infrastructure redeployment is proposed for this execution.
  - [x] Refresh explicit subscription, tenant and enabled-account authentication.
  - [x] Reconcile the original deployment and exact live job image, default command, environment, two identities, registry, native secret reference, dependency bindings, resource limits and Manual trigger.
  - [x] Verify both exact temporary grants and the single approved credential's metadata, callbacks, roles and ID-token setting.
  - [x] Verify there are no existing job executions.
  - [x] Compile and lint the read-only readiness override and its guarded execution transport; retain its code hash in ignored proof.
  - [x] Preserve the accepted application image inputs; no new build is dispatched.

This validation authorizes only one read-only execution of the existing job,
using the documented JobExecutionTemplate override. It checks dependency DNS
against the approved private endpoint subnet, Cosmos and vault read access,
the absent setup fence and the five unused secret names. It does not run
initialization, create secrets or change infrastructure. Persist an attempt
marker before submission and reconcile its recorded execution on uncertainty.
Actual successful private readiness is required before validating the
once-only initializer. Both temporary grants still require exact revocation
and the stopped setup job requires deletion after initialization and verification.

The private readiness execution succeeded. Live execution metadata confirms
the exact validated Python override and accepted image, not the default
initializer. Its successful exit proves exclusive private-subnet DNS results
for vault/Cosmos, authorized reads, an absent fence and five absent secret names.
The stopped job's live log-stream command did not return promptly; only that
local diagnostic command was stopped. No cloud execution was stopped or retried.

- [x] All once-only initialization validation checks pass at `2026-11-10T19:58:50Z`.
  - [x] `demo_setup_run.py validate-initialize` rechecks the exact deployed job, both grants, registration metadata and default initializer.
  - [x] The only existing execution is the successful read-only probe; its actual complete command equals the validated code and hash.
  - [x] No initializer attempt marker exists. The execution transport records one before submission and refuses replay.
  - [x] Focused lint passes. The reviewed initializer's durable fence and zero-retry policy remain unchanged.

This stage authorizes one default-command initializer execution only.
Application rollout and public ingress remain excluded. Verify completion and
the five stored names without exposing values before revoking both temporary
grants and deleting the stopped job under the existing cleanup approval.

### Completed private setup and cleanup

The default initializer succeeded once. A separate read-only override then
verified the complete durable fence, exact five secret names, ownership tags,
valid key formats, the approved OIDC value match and private dependency DNS.
Only metadata was printed. No fence was cleared and no credential was rotated.

The approved cleanup completed at `2026-11-10T20:06:10Z`. Both exact temporary
assignments return 404 and the stopped setup job is absent; the identically
named managed identity remains intact. Independent Azure CLI queries also
return empty results for the setup principal's vault and native Cosmos grants
and for the exact job name. The OIDC credential's durable copy is now only in
the private vault, not the deleted job or any local file.

The next access-stage preview initially included an unwanted model update.
Nothing from it was applied. Disable model redeclaration for this later
incremental stage; retaining the existing deployment needs no model PUT.
Keep the four existing API grants unchanged and create only the six
named-secret reader assignments. Re-run provider validation and strict preview
before setting this stage Validated.

### Named-secret reader validation

- [x] All named-secret reader validation checks pass.
  - [x] Compile the unchanged dedicated access entrypoint.
  - [x] `az deployment group validate --validation-level Provider` succeeds with model redeclaration disabled and secretsReady enabled.
  - [x] `az deployment group what-if --validation-level Provider --result-format FullResourcePayloads` has exactly six creates, four unchanged API grants, and no modifications, deletions, unsupported entries or diagnostics.
  - [x] Fresh identity GETs bind all four literal principals to their exact tenant and resource IDs.
  - [x] `demo_reader_proof.py` verifies all six assignment IDs with the documented ARM GUID algorithm, exact vault/secret scopes, gateway principals and Secrets User role.
  - [x] Static review of `secret-access.bicep` confirms no vault-wide runtime reader or writer grant.
  - [x] Record template/parameter hashes and exact approved IDs in ignored validation proof; preserve the successful setup and independent cleanup evidence.

Only these six scoped assignments may be applied. Retain the existing model
without redeclaring it. No workload, network, credential or public-ingress
change belongs to this stage.

The reader-only deployment succeeded at `2026-11-10T20:12:48Z`. Fresh GETs
verify all six assignment IDs, ServicePrincipal types, principals, roles and
exact secret scopes. No model update or vault-wide runtime reader was applied.

### Closed-ingress six-app validation

- [x] All closed-ingress application validation checks pass.
  - [x] Compile `demo-apps.bicep`; generate parameters from the actual demo configuration and accepted immutable manifest with enablePublicIngress false.
  - [x] `az deployment group validate --validation-level Provider` succeeds.
  - [x] `az deployment group what-if --validation-level Provider --result-format FullResourcePayloads` has exactly seven creates: six apps and the shopper authentication configuration. No modifications, deletions, unsupported entries or diagnostics.
  - [x] `demo_apps_proof.py` verifies each image, command, port, identity, registry, environment boundary, replica/resource/draining limit, probe set, role/provider/state setting and gateway routing.
  - [x] Fresh existing-resource GETs bind only the explicitly whitelisted reference/format expressions. Unknown unresolved expressions are rejected, not evaluated or defaulted.
  - [x] The compiled secret-name definitions, exact three vault targets per gateway and identity bindings match the approved design. ARM redacts thirteen secret-name/reference fields in its preview; actual names must be verified after deployment before public ingress.
  - [x] The proposed shopper auth configuration enables HTTPS, tenant/client checks and the exact operator allowlist, with only the three approved health/JWKS exceptions.
  - [x] Each environment independently reports zero Consumption cores used against limit 500; the planned three apps require 1.5 cores per environment.
  - [x] Source differences from the accepted image commit affect only reviewed infrastructure, documentation and the deployment plan, not image inputs. The worktree has only the deployment plan tracked as modified.
  - [x] Record exact create IDs and template/parameter hashes in ignored proof.

Apply only this incremental seven-resource stage. All shopper ingress remains
environment-internal. The merchant gateway is environment-exposed only inside
the existing internal environment and retains its exact VNet allowlist.
Read actual resolved secret names, references and shopper auth configuration;
verify application readiness and access boundaries before separately
validating any public-ingress change.

The closed-ingress deployment succeeded and actual secret references and Entra
auth metadata passed strict read-back. Subsequent replica-level checks found
both gateway application containers in `CrashLoopBackOff`. The merchant gateway
console traceback identifies `ImportError: aiohttp package is not installed`
while constructing the async managed-identity credential. The shopper platform
auth sidecar is healthy, but its gateway application container is not.

Bounded, exact-replica execution verified local readiness and missing/invalid
bearer rejection on the merchant API. Both role APIs independently completed a
real Azure OpenAI Responses request using their respective managed identities;
each returned the exact synthetic readiness response with 23 input and 2 output
tokens. No customer data or tokens were printed. These checks do not establish
gateway readiness, browser sign-in, SSE or persisted conversation acceptance.
The full exec payload exceeded a platform WebSocket request limit; short probes
worked. SDK HTTP 400 observations alone were not an established Cosmos failure.

### Gateway repair validation

- [x] All protected gateway-only build validation checks pass.
  - [x] The repair is merged through PR #12 after all fourteen expected exact-head checks pass, including the actual gateway Docker build and offline async transport construction.
  - [x] The merged main revision has the identical tested tree; source and Docker inputs are unchanged from the successful PR checks.
  - [x] `az bicep build --file infra/private-build/vm.bicep` succeeds for the unchanged runner template.
  - [x] `az deployment group validate --validation-level Provider` succeeds for the separate build resource group.
  - [x] `az deployment group what-if --validation-level Provider --no-pretty-print` and the strict local guard allow only the two exact fresh private VM/NIC creates, with no public IP, permission change, diagnostics or other modification.
  - [x] Protected nonsecret build configuration matches the previously accepted runtime binding. Required reviewer and main-only environment policies remain in force; no review bypass or automatic build retry is authorized.
  - [x] Static runner template review confirms the existing scoped builder identity, private subnet/NSG, exact platform image, no new role assignments and exact owned-resource cleanup.

This validation authorizes the single approved protected `smoke` build only.
The workflow repeats provider validation and strict preview against its actual
run-specific VM/NIC before provisioning. Normal environment review is still
required. The two-gateway image rollout needs a separate digest-bound preview
after the build and cleanup pass. Gateway health remains a known failed
acceptance gate, not a passed precondition inferred from ARM.

The one approved protected build succeeded after the user's exact-run normal
environment review. Its manifest contains only the gateway image from the
merged repair source. Fresh ARM reads independently verify the owned VM, NIC
and OS disk absent, and no resource with the run's generation or owned name
remains. No additional build or retry was used.

### Two-gateway image-only repair validation

- [x] All image-only rollout validation checks pass.
  - [x] Capture secret-free live gateway snapshots and the exact shopper auth configuration.
  - [x] Calibrate the literal resource projection with provider validation and a no-op preview: exactly the two existing gateways are `NoChange`; no other resource is modified.
  - [x] Accept the exact single gateway digest from the protected workflow only after source, successful steps, manifest cardinality and independent cleanup verify.
  - [x] Generate the two-resource release with only each `app` container image changed; reject concurrent configuration changes.
  - [x] Run provider validation and a strict full-payload preview; require exactly two image-only modifications and no create, delete, permission or ingress change.
  - [x] Check the existing separate pull identity and registry scope; no new grant or propagation workaround.
  - [x] Record template and image fingerprints before application.
  - [x] After deployment, require actual latest-revision app containers and auth sidecars ready/started/running; compare complete writable configuration and unchanged shopper auth.
  - [x] Run private HTTP readiness, identity rejection and gateway/frontend routing checks before any public-ingress stage.

`demo_gateway_repair.py generate` captures exactly the two unchanged live
gateway configurations and replaces only the image with the accepted protected
build digest. `az deployment group validate --validation-level Provider` and
`az deployment group what-if --validation-level Provider --no-pretty-print
--result-format FullResourcePayloads` both succeed for this literal template.
`demo_gateway_repair.py preview-proof` verifies exactly two modifications.
Each provider before snapshot equals its accepted no-op calibration; its after
snapshot differs only in the single application image. The sole effective
delta is that image; all `NoEffect` entries match exact existing identity IDs,
principal/client metadata, hostname or ephemeral-storage metadata. Fresh live
configuration reads still match the original snapshots. Unrelated resources
are ignored, and there are no permission, secret, ingress or auth changes.

The local validation proof binds the template SHA-256, immutable gateway digest
and exact resource inventory. The existing separate pull identity retains
`AcrPull` scoped to the existing registry. Static inspection finds no role
assignment resources in the repair template. Previously accepted secret-scoped
and native Cosmos grants are unchanged. The actual gateway Docker build and
offline async transport gate passed in both reviewed CI and the one protected
repair build; no additional build is needed. Provider validation reports no
policy denial. This validation authorizes only the two-gateway image update,
not public shopper activation or application acceptance.

On 2026-11-11 UTC, `retail-demo-gateway-repair` completed successfully.
`demo_gateway_repair.py live-proof` confirms both repaired images, complete
writable configuration preservation, unchanged shopper auth and actual healthy
latest-revision containers. Replica-pinned `RETAIL_GATEWAY_PROBE` checks pass
separately for shopping and merchant: live/ready endpoints, private Cosmos/OIDC/API
readiness, public-only signing key, frontend availability, fixture metadata,
missing-identity rejection, wrong-role rejection and browser bearer rejection.
The merchant frontend correctly redirects unauthenticated browsers to sign-in;
the same replica independently reaches its private frontend. No ingress was
opened by this repair.

### Operator-only public shopper activation

The original approved rollout includes this stage. Change only the repaired
shopper gateway ingress `external` setting to true after validating fresh
platform authentication: exact tenant, client, audience, HTTPS requirement,
operator object-ID allowlist and health/public-key exceptions. Preserve the
entire auth resource and every other writable app setting. Capture all six
application snapshots and reject concurrent or unrelated changes. The merchant
environment and gateway remain private; the four API/frontend apps remain
environment-internal. No role, credential, model, image or foundation changes.

- [x] Generate a snapshot-bound single-resource activation template.
- [x] Validate with Provider checks and a strict full-payload preview.
- [x] Record template fingerprint, exact auth binding and unchanged isolation.
- [x] Deploy only after this stage is Validated.
- [x] Verify actual replica readiness, public health and anonymous sign-in enforcement.
- [x] Verify interactive operator sign-in and a persisted streamed conversation.

On 2026-11-11 UTC, `demo_public_shopper.py generate` snapshots all six apps and
verifies the exact existing operator-only HTTPS auth configuration.
`az deployment group validate --validation-level Provider` and
`az deployment group what-if --validation-level Provider --no-pretty-print
--result-format FullResourcePayloads` succeed without policy denial or
diagnostics. `demo_public_shopper.py preview-proof` confirms exactly one
effective change: shopper gateway ingress `external: false` to `true`.
Complete provider snapshots otherwise match. Provider-only metadata deltas
must match existing exact identity, hostname and ephemeral-storage metadata.
Fresh reads of all six app configurations and auth still match captured
snapshots; the template and auth SHA-256 values are recorded in ignored proof.
Static review confirms no role, image, model, credential or backing-service
resource in this template. Prior accepted immutable builds and current private
HTTP/replica checks remain applicable; no rebuild is required. This validation
does not prove interactive sign-in, which remains a post-deployment check.

The activation deployment and six-app live configuration/replica proof pass.
Public health endpoints return 200; anonymous API requests return 401 and the
browser opens the configured tenant's sign-in page. The operator completed
platform sign-in and the gateway's separate OIDC flow; authenticated customer
metadata returns 200. Normal frontend session creation then fails with 403.
The authentication sidecar explicitly reports CSRF rejection with an empty
referrer. The same signed-in browser request, with its normal CSRF token and
an explicit same-origin referrer policy, returns 200 and creates a session.
The gateway's `no-referrer` response policy is the demonstrated cause.

The user approved changing the response policy to `same-origin`, adding
regression coverage, and one additional normally reviewed protected
gateway-only build and image-only rollout. Preserve platform and gateway CSRF
checks, cross-site referrer privacy, operator-only auth, current public shopper
ingress and private merchant isolation. No permission, credential or API/
frontend image changes are authorized. Publish and validate the source,
build once, independently verify digest/cleanup, then validate the exact
two-image update before deploying. Interactive acceptance remains incomplete.

### Same-origin referrer build validation

- [x] All protected referrer build validation checks pass.
  - [x] Gateway regressions: 62 pass, including policy checks for both roles and existing CSRF rejection checks; focused Ruff and formatting pass.
  - [x] PR #13 passes all fourteen expected exact-head checks, including the actual gateway Docker build; normal merge has the identical tested tree.
  - [x] Pinned Bicep compilation and Provider validation pass for the unchanged disposable VM template.
  - [x] Fresh what-if and the existing strict runner guard allow only the exact new private VM and NIC creates, with no public IP, permission changes or unrelated resources.
  - [x] Protected build configuration equals the accepted runtime configuration; normal required-reviewer and main-only policies remain intact.
  - [x] Static runner inspection confirms the existing builder identity and private subnet/NSG, exact platform image and no role assignments.

On 2026-11-11 UTC, `demo_referrer_build.py validate-build` compiles the unchanged
runner template and runs `az deployment group validate` and `what-if` at Provider
level. Both succeed without diagnostics or policy denial. The existing
`private_build.preview_vm` guard accepts only the exact two fresh VM/NIC
creates. The protected workflow repeats these checks for its actual run
generation. This validation permits the one approved `smoke` build only,
after normal environment review; it does not permit another build retry or
an application image rollout before digest and cleanup acceptance.

The approved build succeeded with every build, cleanup and evidence step
successful. Its manifest contains exactly one immutable gateway digest from
the reviewed source. Independent ARM reads confirm the exact owned VM, NIC and
OS disk absent and no run-owned/generation-tagged resources remaining. The
original complete-image release and async-transport repair proofs are retained
separately. No automatic retry or additional build was used.

- [x] All referrer image-only rollout validation checks pass.
  - [x] Capture fresh six-app and unchanged operator-only auth snapshots.
  - [x] Calibrate the literal gateway projection with Provider validation and exactly two `NoChange` targets.
  - [x] Accept the exact single repair digest only after source, successful workflow and independent cleanup verification.
  - [x] Generate, provider-validate and strictly preview exactly two image-only modifications.
  - [x] Bind template/image fingerprints, unchanged auth and all four unchanged API/frontend configurations.
  - [x] Deploy only after validation; require actual ready replicas and private/public HTTP behavior.
  - [x] Verify ordinary signed-in browser POSTs, streaming and persisted conversation state.

On 2026-11-11 UTC, `demo_gateway_repair.py generate --referrer` creates the
two-resource digest-bound template. `az deployment group validate` at Provider
level and full-payload `what-if` both succeed without diagnostics or policy
denial. `demo_gateway_repair.py preview-proof --referrer` confirms exactly two
image changes against the accepted no-op provider calibration. Every effective
delta is the single gateway application image; `NoEffect` entries must match
exact existing read-only metadata. Fresh reads reject concurrent changes to
either gateway or any of the four API/frontend apps. Shopper auth and both
ingress boundaries are unchanged. The proof records template and image
fingerprints. Static inspection finds no new roles, secrets, access policies
or backing services; existing scoped data grants and pull identity remain
applicable. The accepted protected image and exact-head Docker CI provide
actual build verification. This validation permits only this two-image update.

The referrer image deployment succeeds. `live-proof --referrer` confirms exact
images, complete writable settings, unchanged auth, unchanged API/frontend
apps and actual ready latest-revision containers. Replica-pinned private
probes pass for both roles, including the `same-origin` response header.
Public liveness/readiness/JWKS return 200 and anonymous API requests return 401.

The operator's normal browser page creates a session with 200 and reads memory,
cart and orders with 200. Two ordinary chat requests return 200 and complete;
the first emits tool calls/results, product comparison UI and text deltas.
The follow-up correctly names the lighter product and its earlier weight.
The observed stream contains two accepted/completed turns, six tool calls and
results, one completed UI event and 132 text deltas. A fresh document load can
read the completed prior turn and replay its persisted text/completion events
with 200. No order or payment is created. No token, cookie or raw user content
is retained in verification artifacts.

At this stage the frontend still displayed a 403 warning, and one comparison
repeated its narrative. The subsequent catalog stage below identifies and
resolves the warning without weakening access control. Private merchant browser
sign-in/approval has not been exercised, and the exact operator allowlist is
configuration-verified rather than tested with a second account. No VPN or
public merchant exposure was added.

### Session-bound catalog repair

The user identified the failed request in their normal browser:
`GET /api/products?limit=100`. A direct signed-in request without a conversation
session reproduces 403 with `Access denied`. Catalog components mount before
session initialization, and the shared hook caches the failed response as
an empty catalog for the page lifetime. Do not remove the backend's
principal/session binding to accommodate that race.

The user approved a shared-hook fix and one `retail-storefront` protected build
and shopper-web image-only redeployment. The shared frame supplies its committed
session to catalog consumers, the telecom page supplies its session explicitly,
cache entries are bound to that session, and failed reads are evicted. Local
unbound showcases remain supported. Add the exact bounded image choice to
the existing workflow/controller and remote selector; retain the normal
review gate, fixed source, identities, private network and cleanup contract.
Do not rebuild gateways, APIs or other frontends for this rollout.

- [x] Pass catalog race/cache/session-change regressions and affected web builds.
- [x] Pass targeted build selection and manifest-cardinality checks.
- [x] Publish source and merge only after exact-head CI.
- [x] Validate the unchanged disposable VM plan and protected configuration.
- [x] Run the single approved normally reviewed retail storefront build.
- [x] Verify its exact digest/source and independent compute cleanup.
- [x] Validate and deploy exactly the shopper-web image change.
- [x] Verify normal-browser catalog startup has no 403 and retains streamed chat.

Local validation passed: the shared web test command covers 23 cases, including
six catalog regressions. Retail and telecom production Next.js builds completed
with TypeScript checking. The private-build unittest command passed 27 cases
with two Linux-only checks deferred to CI. `scripts/check.py` and focused Ruff
checks passed. This is source validation, not live release authorization proof.

### Retail storefront build validation

Validation completed at `2026-11-11T03:30:40Z` through `azure-validate` for the
single approved private build. All 16 exact-head PR checks passed, including
the Linux shell image selector and all eight frontend images. The merged source
is `f70dca132308460e16edfef39be8a93c156f4011`.

`demo_storefront_build.py validate-build` verified the exact reviewed main,
unchanged protected configuration, owner review with administrator bypass
disabled and main-only dispatch. Azure authentication matches the already
approved tenant/subscription. Bicep `0.46.1` compiled the unchanged VM template.
Provider-level resource-group validation and a full-payload what-if passed;
the strict guard accepted only the exact fresh disposable VM/NIC creations
and known unchanged bootstrap resources. The VM/bootstrap templates and scoped
role definitions are byte-identical to the accepted previous build stage.
The current policy assignments were retrieved through the Azure Policy tool;
provider validation reports no blocking policy error for this Sweden Central
template. No new roles, credentials or network changes are proposed.

The generated validation and preview evidence is retained in ignored
`demo-storefront-repair.*` files. This status authorizes only the one approved
`retail-storefront` build under normal exact-run review, not application rollout.
The new digest, independent cleanup, snapshot-bound image-only preview and live
browser acceptance are still required before completing the deployment.

### Retail storefront image-only rollout

The normally owner-reviewed single build succeeded. Its exact source and
one-image manifest were verified; separate ARM reads confirm the owned VM,
NIC and OS disk are absent and no run/generation-tagged resources remain.
The accepted immutable storefront digest is
`sha256:74ce5110d51f6eda9468e17c0ea8a7ce16ddc8ca5d5a7e014e0b4bb39cbabb59`.

Validate a literal one-resource template from a fresh six-app/auth snapshot.
Require provider validation, an accepted no-op calibration and a full-payload
preview whose only effective change is the shopper-web container image.
Require all other five app configurations and operator-only auth unchanged,
the existing registry-pull grant, and a final concurrent-state guard before
the approved incremental deployment. No full foundation deployment is allowed.

Image-only validation completed at `2026-11-11T03:45:07Z` through
`azure-validate`. The exact literal snapshot template passed resource-group
provider validation and full-payload what-if. Its no-op calibration is
`NoChange`; its release preview is exactly one effective container-image
modification. Strict provider-only metadata checks, all six live configuration
comparisons and operator-auth equality passed. The existing attached managed
identity has the exact registry-scoped `AcrPull`; no new grant or propagation
wait is required. Azure resource ID comparisons are case-insensitive.
The deployment template SHA-256 is
`db09dd1273a6edd1d27eb0565ccdef2c9cc29a9116588eac01a0b004922ef2e9`.
Only the existing shopper-web resource is included; the final pre-apply guard
must still pass. Earlier VM-only build validation does not substitute for
this application release proof.

The incremental `retail-demo-storefront-repair` deployment succeeded with
exactly the existing shopper-web resource. Separate read-back confirms its
accepted digest, five unchanged app configurations, unchanged operator auth,
and ready/started/running application containers on all six latest revisions.
Both replica-pinned private gateway probes pass, including frontend readiness,
private dependencies, same-origin referrer policy and unauthenticated/wrong-role
rejection. Public health returns 200, anonymous APIs return 401, and sign-in
redirects to the configured tenant. Three fresh signed-in page loads show the
catalog request returning 200 with populated featured products and no warning.

Final browser acceptance completed at `2026-11-11T03:59:17Z`. Ordinary
comparison and follow-up chat requests both return 200 and finish streaming.
The comparison renders the two-product UI; the follow-up correctly recalls
the lighter product and its earlier weight. Its named SSE events include one
accepted turn, 20 text deltas and one completion. There are no stream read
errors or visible access-denied warnings. A fresh document load reads that
completed turn and replays its persisted text/completion events with 200.
The cart remains empty; no order or payment is created. Temporary browser
observation hooks and replay metadata were removed. The sanitized ignored
browser proof contains only statuses, counts and acceptance flags.

The private merchant deployment remains deliberately VNet-only. Its actual
replica, frontend, private dependencies and authentication/routing boundaries
are verified, but interactive merchant approval and a second-account operator
allowlist test are not claimed. Neither VPN nor public merchant exposure was
authorized. This is completion of the approved isolated fixture demo, not
production-commerce acceptance.

- [x] All gateway repair validation checks pass.
  - [x] Declare the async HTTP transport in gateway package dependencies, retaining the existing release constraint.
  - [x] `pytest apps/gateway/tests -q`: 58 pass, including real offline Azure async client construction and managed-identity selection; focused ruff check and format checks pass.
  - [x] Add a Docker build-time gate that constructs and closes both async clients without requesting tokens or connecting to a service. Docker is unavailable locally; exact-head container CI must execute this gate before the private build.
  - [x] Publish the surgical repair and pass exact-head required CI before merging.
  - [x] Validate the existing protected `smoke` build plan and normal review; execute only one approved build.
  - [x] Verify gateway digest, reviewed source, non-root/transport checks and exact disposable compute cleanup.
  - [x] Record a two-gateway image-only preview; reject unrelated changes or public exposure.
  - [x] Verify actual gateway replicas and HTTP readiness after update, not only ARM provisioning metadata.

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
accepted. Demo source artifacts are published and merged, the image releases
are deployed, and the scoped application acceptance is recorded above.

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

Retail production integration remains outside the completed isolated fixture
demo. Before implementing or deploying that production release:

1. Supply the authoritative retail backend contract or existing adapter,
   including idempotency and conditional merchant approval semantics.
2. Configure customer and workforce OIDC registrations, audiences and approver
   policy; select actual model deployments with available capacity.
3. Confirm domains, certificates, private merchant access, deployment context,
   quotas, policy, costs and least-privilege runtime assignments.
4. Approve a scoped Container Apps rollout plan. Complete `azure-prepare`,
   `azure-validate` and `azure-deploy` for that plan, then verify private service
   access, authentication, durable state, inference and real backend behavior.

Build success does not validate these production gates. The isolated fixture
demo above is a separate application/model rollout, not production acceptance.
WAF and AKS workloads are not deployed.

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
