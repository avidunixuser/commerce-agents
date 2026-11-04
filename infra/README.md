# Azure infrastructure

The Bicep targets deploy independently:

- `container-apps/main.bicep`: internal workload-profile environment, role
  gateways/frontends/APIs/workers and WAF.
- `aks/main.bicep`: private Entra/OIDC cluster, managed app-routing Gateway API,
  workload identities and WAF; application resources use the Helm chart at
  `deploy/helm/commerce-agents`.
- `modules`: shared network/private DNS, data, registry, vault, models,
  identities/RBAC, TLS WAF and telemetry.
- `scripts/validate.py`: offline Bicep, Helm, Kubernetes/CRD schema and isolation
  checks. Requires Azure CLI with Bicep, Helm, kubeconform and PyYAML.
- `scripts/preflight.py`: fail-closed operator input checks, not a live quota or
  entitlement check.

Copy a `main.parameters.example.json` to a git-ignored
`main.parameters.local.json`, replace every placeholder and complete the live
preflight gates. The examples intentionally cannot deploy: model availability
is unattested and all workload/ingress enablement is off.

```powershell
python infra\scripts\validate.py
python -m unittest discover -s infra\scripts -p test_*.py -v
python infra\scripts\preflight.py --target container-apps --parameters infra\container-apps\main.parameters.local.json
```

Only after separate provisioning approval and a configured private runner:

```powershell
az deployment group validate --resource-group $ResourceGroup --template-file infra\container-apps\main.bicep --parameters infra\container-apps\main.parameters.local.json
az deployment group what-if --resource-group $ResourceGroup --template-file infra\container-apps\main.bicep --parameters infra\container-apps\main.parameters.local.json
# Reviewed foundation configuration first; workload and ingress phases follow the runbook.
az deployment group create --resource-group $ResourceGroup --template-file infra\container-apps\main.bicep --parameters infra\container-apps\main.parameters.local.json
```

Use the AKS paths instead for that target, not in addition to ACA. Template
validation and what-if still require subscription access and are not part of
offline CI.

See [infrastructure and runtime configuration](../docs/azure-infrastructure.md),
[private release/bootstrap runbook](../docs/azure-private-runners.md) and
[cost drivers](../docs/azure-cost-drivers.md). The runbook's live validation,
private model availability and Claude schema gates are mandatory; compilation
does not make the deployment production-ready.
