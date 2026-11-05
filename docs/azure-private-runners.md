# Private build and release runners

Use an isolated, ephemeral release runner in the target VNet's `runners` subnet,
or an operator-managed peered network with private DNS forwarding and reachability
to ACR, Key Vault, Cosmos, models and the private AKS API. The optional
`infra/private-build/` implementation creates **disposable private build VMs**,
not a registered GitHub self-hosted runner, general release runner, VPN,
ExpressRoute circuit, peering or DNS resolver.
The runner is trusted deployment infrastructure, not an application identity.

## Protected private image builds

For a public repository, `.github/workflows/private-build.yml` keeps the
control job on a GitHub-hosted runner. An approved `workflow_dispatch` on `main`
uses Entra OIDC to create a fresh, private Ubuntu D4ds_v5 VM, invoke managed
Run Command, and remove that VM/NIC after the build. No GitHub listener or PAT
is installed inside the private network. Public PR jobs cannot claim the VM.
An environment gate alone would **not** prevent a repository-wide self-hosted
runner from receiving another workflow's job.

The `commerce-build` environment requires an owner review, prohibits
administrator bypass, and permits only the `main` branch, not tags. The
single-owner setup allows the owner to approve their own dispatch; it is not
a two-person review policy. The scripts never approve pending runs. The
customized OIDC subject binds the immutable owner/repository IDs, environment,
main ref, manual event, exact workflow path and GitHub-hosted environment.
Changing repository OIDC customization affects future OIDC workflows too.
JWTs and model credentials are never stored in config, ARM parameters or logs.

The controller receives a dedicated build-group VM/deployment/NIC role,
read/join on the existing runner subnet, and permission to attach **only** the
builder identity. The builder receives `AcrPush` on the selected registry only.
Neither gets Cosmos, model, vault, IAM-write or subscription-wide
Owner/Contributor grants. The controller identity is not attached to the VM.
The existing subnet NSG is preserved; a separate NIC NSG denies inbound
connections and lateral private traffic other than the approved DNS/platform
and endpoint HTTPS exceptions. Internet HTTPS still follows the existing
firewall UDR and runner-only FQDN rules. The Azure Artifacts blob suffix is
necessary for redirects from the existing npm lockfile; it is not a general
`*.blob.core.windows.net` allow.

### Prepare and commission

Use the existing Bicep preparation/validation/deployment approval process.
Do not run these deployment/setup commands merely to try the example.

1. Copy `infra/private-build/config.example.json` into an ignored `.azure/`
   file and replace every placeholder with approved values. Discover GitHub's
   actual immutable prefix with
   `gh api repos/OWNER/REPO/actions/oidc/customization/sub`. Pin an actual
   Gen2, Trusted-Launch-capable `Canonical:ubuntu-24_04-lts:server` image
   version and verify regional DDSv5 and total vCPU quotas. No `latest` image
   or name-only OIDC subject is accepted.
2. Generate bootstrap parameters:
   `python infra/scripts/private_build.py --config .azure/private-build.config.json parameters --output .azure/private-build.parameters.json`.
   Compile/validate `infra/private-build/main.bicep`; review its subscription
   what-if and four exact role assignments before approved deployment. It
   creates the build resource group, two identities, federation, NIC NSG and
   runner-only firewall rule group; **no VM or application is created by
   this bootstrap**. Reuse an existing group only after separately reviewing
   ownership/tags; do not overwrite another team's group.
3. After successful bootstrap, generate a **new** runtime config from its
   verified outputs:
   `python infra/scripts/private_build.py --config .azure/private-build.config.json hydrate --deployment-name APPROVED_BOOTSTRAP_DEPLOYMENT --output .azure/private-build.runtime.json`.
   Verify all four grants and allow propagation. Runtime config contains
   nonsecret IDs, never access keys or tokens.
4. Only with explicit policy-change approval, configure the GitHub environment,
   immutable OIDC customization and nonsecret variables:
   `python infra/scripts/private_build_github.py --config .azure/private-build.runtime.json setup --approve-policy-change`.
   The helper refuses unrelated existing policies or different variable values;
   investigate partial setup rather than weakening a protection rule.
5. Review/merge the workflow through the repository's normal process, then
   dispatch it on main and manually approve the environment gate. `smoke`
   builds/import-checks/pushes the existing gateway; `all` serially builds the
   API, gateway and all eight vertical frontends from the exact triggering
   commit. Source ref, subnet, identity, registry and arbitrary shell commands
   are not workflow inputs. Record private DNS/TLS checks and immutable digests.
   The control job itself cannot access the private registry.

An explicitly approved operator smoke test can use `private_build.py ... run`
with `--operator`, the current reviewed main `--source-sha`, a unique
`--run-id`, a new `--state-file`, and `--approve-disposable-cleanup`. Operator
mode is forbidden in Actions. This proves private execution but is **not**
evidence that GitHub environment approval/OIDC worked. Validate the actual VM
deployment after bootstrap and before the first billable creation. Every
controller run repeats provider validation and what-if, refusing anything but
the exact two fresh VM/NIC creations; unexpected modifications, deletions or
unsupported preview entries stop before creation.

### Lifetime, recovery and release limits

The local 128-GiB ephemeral OS is discarded with the VM. Never reuse a dirty
host or call stop-deallocate on this SKU/disk configuration: ephemeral OS VMs
do not support that lifecycle. The controller cancels/settles its own ARM
deployment before deleting only its exact, generation-tagged VM/NIC. It never
deletes a resource group, shared NSG, identities, registry images or unrelated
resources. Recovery state binds the subscription/group, run, generation and
source; foreign tags or scope mismatches fail closed. Keep its nonsecret
state/log artifact until cleanup is verified.

Failures and normal cancellation attempt cleanup; a force-killed job, lost
Azure authentication or failed deletion can leave a **billable VM**. This is
not a hard spending cap. Inspect the exact recorded resource IDs, then run
`private_build.py --config RUNTIME_CONFIG cleanup --state-file STATE --approve-disposable-cleanup`
only with deletion approval. It is idempotent and refuses unrelated resources.
Do not remove the build group or foundation as a cleanup shortcut.

Retail Sweden Central Linux D4ds_v5 compute was USD 0.24/hour when planned
(USD 0.72 for three hours; USD 175.20 for 730 hours if cleanup failed).
Network, firewall processing, image storage/telemetry and the existing
foundation add charges. Successful VM/NIC deletion leaves no idle build VM
or persistent OS disk. Refresh rates before deployment.

Build/push success is **not production release approval**: vulnerability
scanning/signing, authoritative backend integration, OIDC/model acceptance,
runtime image-pull identity tests and application rollout remain separate
gates. The build identity cannot be used to certify Cosmos/model/vault
authorization. No AKS is required or installed by this build path.

## Identity and trust

Use GitHub Actions OIDC federation constrained to the exact repository and
protected environment. Require environment approval and branch protection.
Separate:

- Image build/promotion identity: ACR push/import permissions only.
- IaC identity: resource-group deployment permissions; tightly scoped RBAC
  delegation for the roles actually declared by Bicep. Claude additionally needs
  permission to create the resource-group-assignable custom role definition
  (`Microsoft.Authorization/roleDefinitions/write`); this is an IaC operator
  permission, never a runtime grant.
- AKS release identity: Entra access plus namespace-scoped deployment permissions.
- Runtime identities: only data/model/secret/telemetry grants in the templates.

No deploy credential is available on pull requests. In particular, never run
untrusted PR code on a persistent private release runner, and never use
`pull_request_target` to execute a fork's code. Do not grant the runner's
managed identity to untrusted jobs. OIDC tenant/subscription/application IDs are
protected environment inputs, not committed personal values.

The infrastructure workflow is **offline only**, with `contents:read` and no
`id-token:write`. It compiles Bicep and runs Helm/schema/policy tests on a hosted
runner. It does not claim that a public hosted runner can reach private Azure
services. A production application-release workflow must be supplied by the organization
with its runner labels, approvals and OIDC identity; there is intentionally no
unguarded application-deployment action. The optional private-build workflow
above is image build infrastructure only, not a production application rollout.

## Bootstrap order

Provisioning requires separate explicit approval. The following is the release
order, not a command executed during code preparation:

1. Select subscription/resource group, public-Azure region, corporate
   connectivity, nonoverlapping address ranges, AKS version/VM zones and model
   deployments. Confirm quotas, entitlement/Marketplace approval, exact private
   inference support and Entra invocation. Run offline `preflight.py` after
   replacing every placeholder. Keep `modelAvailabilityVerified=false` until
   the real checks are complete.
2. Deploy the chosen Bicep target in **foundation mode**:
   `enableIngress=false`; ACA also `enableWorkloads=false`. This creates
   private services, network, identities/grants and the hosting environment or
   cluster without depending on an image that does not exist. No public
   placeholder image is substituted. For a first installation without an
   existing validated model account, an operator must validate the chosen new
   model schema/entitlement/private capability before creating it; the example
   is not a universal model bootstrap command.
3. Connect/commission the private runner. Verify firewall IP/UDR and DNS first:
   registry login **and regional data** endpoints, Cosmos document endpoint,
   vault, model inference endpoint and AKS private API all resolve privately.
   Wait for endpoint connection approval/provisioning, route propagation and
   RBAC propagation. Test pulls with the designated pull/node identity; do not
   enable a public registry temporarily.
4. Build the runtime, gateway, shopping frontend and merchant frontend on the
   private build runner, using the repository Dockerfiles and selected vertical.
   Scan/sign the images and push to private ACR. Promote immutable OCI manifests
   rather than rebuild for another host/provider. Record all four digests in
   approved release metadata; do not copy credentials into build contexts.
5. From private connectivity, import the real TLS certificate and create
   role-specific OIDC client, signing and encryption secrets in the target vault.
   Grant write permission only to the secret operator; the WAF and gateway
   identities have read-only access to their individual secret names, not the
   vault. For AKS CSI, set Bicep `gatewaySecretNames` to exactly match each
   gateway's Helm `remoteName` values; leave empty for existing Kubernetes
   secrets. ACA derives those names from `secretReferences.keyVaultUrl` in the
   target vault. The WAF reads only its versionless TLS secret. Remove bootstrap privileges after
   configuration. Register exact HTTPS callback origins, including merchant
   port 8443. Supply the real backend plugin and provider/auth purpose settings.
6. ACA: enable workloads with exact digest references and `enableIngress=false`.
   Check runtime configuration, private pulls, revision readiness, probes,
   worker leases and environment-local backend DNS before enabling edge traffic.
   AKS: create the namespace with enforced restricted Pod Security Admission
   labels, grant release permissions, apply explicit values, then use
   `helm upgrade --install --atomic --wait --timeout 15m` from the private runner.
   Helm values must contain eight client IDs matching Bicep's OIDC subjects.
   Do not rename the service accounts or namespace without updating federation.
7. AKS: verify managed CRDs, `GatewayClass/approuting-istio`, controller pods and
   webhooks, `Gateway` Programmed and HTTPRoute Accepted/ResolvedRefs conditions,
   private LB addresses/source ranges and network policies. Publish private DNS
   records for the two LB IPs and pass those names to Bicep. No self-managed
   Gateway CRD installation is permitted; schemas downloaded by CI are never
   applied to a cluster.
8. Enable Application Gateway only after TLS secret access, backend readiness
   and private DNS succeed. Configure public storefront and private merchant DNS.
   Run external/private listener, TLS, host-header and encoded path isolation
   tests below before admitting traffic.

Foundation and application deployment use incremental updates. Disabling
`enableIngress` on an existing installation is **not** an emergency deletion or
traffic kill switch: resources omitted by an incremental ARM update are not
automatically deleted. Use an approved WAF deny policy/backend drain or private
network ACL action for emergency isolation. Do not run ARM complete mode as a
shortcut.

## Network, image and secrets gates

From the private runner, validate the actual endpoint DNS chain and HTTPS using
managed identity. From an external test host, prove that:

- Private ACR, vault, Cosmos and model data endpoints cannot be reached publicly.
- Public WAF port 8443 is denied; merchant Host on port 443 cannot reach a
  merchant backend.
- Public storefront `/api/merchant`, encoded/case variants, double encoding and
  traversal cannot reach merchant routes.
- Private merchant port 8443 works only through approved source CIDRs, with
  workforce sign-in; non-approvers cannot approve changes.
- Direct ACA gateway ingress outside the WAF subnet is denied; internal APIs and
  frontends are not VNet-exposed. On AKS, both role LBs are private and deny
  non-WAF sources; shopping pods cannot reach merchant APIs.

Verify API/worker Cosmos SQL scopes, ETag/lease operations and a real private
model call under their own identity. Verify node/pull identities cannot access
Cosmos/model/vault and runtime identities cannot push images.

Populate Helm dependency `/32` lists from actual private endpoints and approved
Entra/OIDC/telemetry address resolution. Update them before DNS changes, never
by adding unrestricted egress. Confirm CSI token exchange reaches Entra and
private vault. Because environment-backed secrets do not hot-reload, secret
rotation requires coordinated gateway rollout with the old decryption keys
retained. Never print secret values or rendered secrets in CI.

## Rollout, recovery and rollback

Scale/roll one role at a time. Stop taking new worker leases, checkpoint/drain
in-flight turns and test stale-owner fencing before termination grace expires.
SSE reconnects must resume by durable cursor without duplicate chat submission.
Observe retries, dead letters, ETag conflicts and deletion backlog.

ACA uses single active revision mode; deploy an approved previous digest to
roll back rather than claim parallel revision traffic shifting. AKS uses
rolling Deployments, HPA floors and PDBs. `helm rollback` restores a previous
release's image/config, not external effects or Cosmos schema/data. Never roll
back to code that bypasses a newer deletion generation or job fencing rule.
Keep database changes backward compatible through the rollback window.

Cosmos continuous backup is a recovery control, not an automatic application
undo. A point-in-time restore creates a new account; privately reconnect it,
reapply scoped data-plane grants and reconcile revoked principals, effects,
jobs and purge generations before switching endpoints. Do not restore deleted
personal data into service merely because a backup contains it.

No live checks in this runbook have been performed by offline CI.
