# Private build and release runners

Use an isolated, ephemeral release runner in the target VNet's `runners` subnet,
or an operator-managed peered network with private DNS forwarding and reachability
to ACR, Key Vault, Cosmos, models and the private AKS API. This repository does
not create a runner VM, VPN, ExpressRoute circuit, peering or DNS resolver.
The runner is trusted deployment infrastructure, not an application identity.

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
services. A production release workflow must be supplied by the organization
with its runner labels, approvals and OIDC identity; there is intentionally no
unguarded deployment action.

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
