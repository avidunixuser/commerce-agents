# Azure deployment preparation

Status: Approved; implementation and offline checks completed. **Not Validated
for Azure deployment.** Cloud deployment is not authorized.

## Architecture

One shared Python/Next.js application, Claude and Azure OpenAI Responses providers,
Cosmos NoSQL persistence and durable workers, and independent Bicep deployments for
Azure Container Apps and AKS. Both targets use private dependencies, managed
identities, WAF storefront ingress, and private workforce merchant access.

## Approved scope

Preserve the pinned upstream baseline, four verticals, eight web apps, core safety
gates and Apache attribution. Implement production authentication, durable guest
memory and consent-based migration (customer facts win), conditional atomic
persistence, resumable streams, container images, infrastructure, tests and docs.
Use Cosmos outbox workers, not Service Bus.

Retention defaults: guest identity/memory 30 days of inactivity, customer facts
90 days since update, transcripts 30 days. Explicit deletion revokes access
immediately and fences stale extraction before physical cleanup.

## Execution milestones

1. Import upstream history without changing target main or source checkout.
2. Define runtime/provider/configuration contracts.
3. Implement atomic Cosmos stores and deletion fencing.
4. Implement durable worker execution and resumable SSE.
5. Implement Claude and Azure OpenAI provider parity.
6. Implement gateway authentication, guests and migration.
7. Wire both roles, all verticals and all frontend surfaces.
8. Build non-root production containers.
9. Implement shared private Bicep infrastructure.
10. Implement Container Apps target.
11. Implement AKS target and Helm workloads.
12. Run regression/static checks, invoke azure-validate, document and publish PR.

## Deployment inputs and live gates

Subscription, location, model deployment availability/quotas, OIDC registrations,
DNS/certificates, private connectivity and authoritative commerce integrations are
operator inputs, not assumed or validated. No cloud resources have been created.
Capacity checks are deferred to a separately approved deployment. Runtime
identities must not use account keys or broad control-plane permissions.

## Role assignment verification

Static verification passed for both targets. API/worker identities have
container-scoped Cosmos SQL data roles and account-scoped model invocation roles.
Gateways have only the auth container and individually referenced Key Vault
secrets. Frontends have no database/model/secret roles; image-pull and AKS control
identities are separate from runtime identities. The exact role/scope matrix and
compiled ARM assertions are recorded in
[azure-infrastructure.md](../docs/azure-infrastructure.md).

Shopping/merchant record separation inside the shared runtime container is
application-enforced, not partition-level Cosmos RBAC. The custom Claude role
has no control-plane/key-listing actions, but covers all Cognitive Services data
operations in its dedicated account; it is not a message-only role. Effective
live access and any narrower provider action catalog remain deployment gates.

## 6. Validation steps

Applied the standalone Bicep recipe from `azure-validate` to both targets:

- [x] Compile both Bicep entrypoints.
- [ ] Server-side ARM template validation with approved deployment inputs.
- [ ] What-if against an explicitly selected subscription/resource group.
- [ ] Azure authentication and target subscription/location confirmation.
- [x] Bicep/static network/RBAC assertions and Python linting.
- [ ] Subscription Azure Policy evaluation.
- [x] Application regressions, browser contracts and production web builds.
- [x] Helm lint/render and strict Kubernetes/Gateway/CSI schema validation.
- [x] Production container builds and frontend serving checks in actual PR CI.

**All validation checks pass: No.** No Azure account query, ARM validation,
what-if, live policy query or provisioning was performed. No subscription or
credentials are needed to reproduce the completed offline application checks.
Do not invoke `azure-deploy` or mark this plan `Validated` until the outstanding
checks pass and deployment is separately approved.

## 7. Validation proof

Local checks completed on the Windows preparation host with Python 3.13.5.
Actual GitHub Actions results for application commit `672f43a` supplement that
evidence: [Linux/web CI](https://github.com/avidunixuser/commerce-agents/actions/runs/41927736133)
and [image CI](https://github.com/avidunixuser/commerce-agents/actions/runs/41927736443)
both passed.

| Check | Command / evidence | Actual outcome |
|---|---|---|
| Full Python regression | `.venv\Scripts\python.exe -m pytest -q -k 'not test_the_file_store_is_owner_only_and_keeps_purge_generations_across_instances'` | 1,281 passed, 1 skipped, 1 deselected |
| Linux Python 3.11 and 3.12 | Actual CI `pytest -q`, without the Windows deselection | 1,282 passed, 1 skipped on each version |
| Dependency consistency | `python -m pip --python .venv\Scripts\python.exe check` | No broken requirements |
| Python lint/format | `python -m ruff check .`; `python -m ruff format --check .` in the project environment | Passed; 247 files formatted |
| Upstream repository contracts | `python scripts\check.py` in the project environment | Clean: all verticals, skills, fixtures, seven upstream packages and managed-agent manifests |
| Browser contracts | `npm --prefix examples run test --workspace web-shared` | 9 passed |
| Production frontends | `npm --prefix examples run build` | All eight applications built; gateway workstream also checked standalone serving |
| Production images | Actual `containers` CI matrix | API/gateway and all eight frontend images built; non-root configuration/import checks and eight frontend HTTP serving checks passed |
| Provider/approval integration | Included in full regression | All 16 vertical/role/provider grounded-turn cases, real async credential construction, and actual core-executor approval/result replay passed without live inference |
| Bicep | `az bicep build --file infra\container-apps\main.bicep`; same for `infra\aks\main.bicep`, using disposable `--outfile` paths | Both exited 0; final nested ARM privacy/network/RBAC assertions passed |
| Infrastructure contracts | `python -m unittest discover -s infra\scripts -p test_*.py -v` | 20 passed after final scope corrections |
| Helm/Kubernetes | `python infra\scripts\validate.py --helm <pinned-helm> --kubeconform <pinned-kubeconform>` | Prior unchanged-chart matrix: 456 valid objects across eight renders; 9 insecure-input rejection cases passed; pinned versions/commands in infrastructure runbook |
| CI configuration | YAML parse of `.github\workflows\*.yml` | All three files parsed; application, web, dependency-confusion and image jobs passed in actual CI |

The deselected test is the unchanged upstream POSIX `0600` assertion that cannot
pass with Windows file-mode semantics. It is **not** skipped or weakened in the
repository: Linux CI runs it normally. The existing skipped test and upstream
dependency warnings are not presented as additional passing checks.

No local Docker daemon/tool is available; image evidence above is from actual
Linux CI, not a local build or Azure runtime test. Live Cosmos transaction
behavior, OIDC registrations, effective RBAC, TLS/private
DNS/networking, Gateway admission/routing, model availability/quota/private
inference, telemetry export and authoritative commerce adapter certification
remain release gates. The Foundry preview `modelProviderData` schema caveat is
documented in the infrastructure runbook. Existing-account security flags and
any inherited grants also need live verification. Fixture backends are
explicitly lab-only and rejected in production.
