# Azure deployment plan

**Status: Private image-build milestone complete. Production rollout deferred.**

## Scope

The completed scope is the private build and push of ten application images:
the gateway, the role API, and the storefront and merchant frontends for retail,
travel, telecom and entertainment. This is build acceptance, not application
deployment, image promotion or production commerce acceptance.

Container Apps is the selected application hosting target. The repository also
contains independent AKS infrastructure, but AKS installation is excluded from
this rollout. No additional build or workload provisioning is authorized by
this plan.

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
