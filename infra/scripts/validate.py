"""Offline compilation, schema validation and isolation regression checks."""

from __future__ import annotations

import argparse
import copy
import json
import pathlib
import shutil
import subprocess
import urllib.request

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
CHART = ROOT / "deploy" / "helm" / "commerce-agents"
OUT = ROOT / ".validation" / "infra"


def run(*args: str) -> str:
    result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(f"{' '.join(args)}\n{result.stdout}\n{result.stderr}")
    if result.stderr.strip():
        print(result.stderr.strip())
    return result.stdout


def resources(template: dict):
    collection = template.get("resources", [])
    for resource in collection.values() if isinstance(collection, dict) else collection:
        yield resource
        if resource["type"].lower() == "microsoft.resources/deployments":
            yield from resources(resource["properties"]["template"])


def check_rbac(template: dict) -> None:
    for resource in resources(template):
        kind = resource["type"].lower()
        props = resource.get("properties", {})
        if kind == "microsoft.authorization/roleassignments":
            role = json.dumps(props["roleDefinitionId"])
            scope = resource.get("scope", "")
            for forbidden in (
                "8e3af657-a8ff-443c-a75c-2fe8c4bcb635",  # Owner
                "b24988ac-6180-42a0-ab88-20f7382dd24c",  # Contributor
                "a97b65f3-24c7-4388-baec-2e87135dc908",  # Includes account-key listing
            ):
                assert forbidden not in role
            for role_id, resource_type in (
                ("4633458b-17de-408a-b874-0445c86b69e6", "Microsoft.KeyVault/vaults/secrets"),
                ("7f951dda-4ed3-4680-a7ca-43fe172d538d", "Microsoft.ContainerRegistry/registries"),
                ("3913510d-42f4-4e42-8a64-420c390055eb", "Microsoft.Insights/components"),
                (
                    "4d97b98b-1d4f-4787-a291-c67834d212e7",
                    "Microsoft.Network/virtualNetworks/subnets",
                ),
                ("acdd72a7-3385-48ef-bd42-f606fba81ae7", "Microsoft.Network/routeTables"),
                (
                    "f1a07417-d97a-45cb-824c-7a7467783830",
                    "Microsoft.ManagedIdentity/userAssignedIdentities",
                ),
            ):
                if role_id in role:
                    assert f"'{resource_type}'" in scope, (role_id, scope)
        if kind == "microsoft.authorization/roledefinitions":
            assert props["assignableScopes"] == ["[resourceGroup().id]"]
            assert props["permissions"] == [
                {
                    "actions": [],
                    "notActions": [],
                    "dataActions": ["Microsoft.CognitiveServices/*"],
                    "notDataActions": [],
                }
            ]
        if kind == "microsoft.documentdb/databaseaccounts/sqlroleassignments":
            assert "/colls/" in json.dumps(props["scope"])
            assert (
                "/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002"
                in props["roleDefinitionId"]
            )


def check_arm(template: dict) -> None:
    check_rbac(template)
    types = {r["type"].lower() for r in resources(template)}
    for resource in resources(template):
        kind = resource["type"].lower()
        props = resource.get("properties", {})
        if kind in {
            "microsoft.documentdb/databaseaccounts",
            "microsoft.containerregistry/registries",
            "microsoft.keyvault/vaults",
            "microsoft.cognitiveservices/accounts",
        }:
            assert props["publicNetworkAccess"] == "Disabled", resource["name"]
        if kind in {
            "microsoft.documentdb/databaseaccounts",
            "microsoft.cognitiveservices/accounts",
        }:
            assert props["disableLocalAuth"] is True
        if kind == "microsoft.containerregistry/registries":
            assert resource["sku"]["name"] == "Premium"
            assert props["adminUserEnabled"] is False
        if kind == "microsoft.containerservice/managedclusters":
            assert props["disableLocalAccounts"] is True
            assert props["apiServerAccessProfile"]["enablePrivateCluster"] is True
            assert props["aadProfile"]["enableAzureRBAC"] is True
            ingress = props["ingressProfile"]
            assert ingress["gatewayAPI"]["installation"] == "Standard"
            assert (
                ingress["webAppRouting"]["gatewayAPIImplementations"]["appRoutingIstio"]["mode"]
                == "Enabled"
            )
        if kind == "microsoft.app/managedenvironments":
            assert props["vnetConfiguration"]["internal"] is True
            assert props["workloadProfiles"]
        if kind == "microsoft.network/applicationgateways":
            assert props["sku"]["tier"] == "WAF_v2"
            listeners = next(
                item["input"] for item in props["copy"] if item["name"] == "httpListeners"
            )
            binding = listeners["properties"]["frontendIPConfiguration"]["id"]
            assert "'shopping'), 'public', 'private'" in binding
            assert props["frontendPorts"][1]["properties"]["port"] == 8443
        if kind == "microsoft.documentdb/databaseaccounts/sqlroleassignments":
            assert "/colls/" in json.dumps(props["scope"])
    assert "microsoft.network/azurefirewalls" in types
    assert "microsoft.network/routetables" in types


def fixtures() -> dict:
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    for name in values["images"]:
        values["images"][name] = f"validation.azurecr.io/{name.lower()}@sha256:{'a' * 64}"
    values["gateway"].update(
        shoppingHost="shop.example.invalid", merchantHost="merchant.example.invalid"
    )
    values.update(tenantId="00000000-0000-0000-0000-000000000000", vaultName="validation-only")
    values["globalEnv"]["COMMERCE_COSMOS_ENDPOINT"] = "https://validation.documents.azure.com:443/"
    for index, key in enumerate(
        ("cosmosCidrs", "modelCidrs", "vaultCidrs", "identityCidrs", "telemetryCidrs")
    ):
        values["network"][key] = [f"10.40.5.{index + 10}/32"]
    for workload in values["workloads"].values():
        workload["clientId"] = "00000000-0000-0000-0000-000000000000"
        if workload["role"] == "shopping" and workload["mode"] in ("api", "worker"):
            workload["env"].update(
                COMMERCE_GUEST_ISSUER="https://shop.example.invalid",
                COMMERCE_GUEST_JWKS_URL="https://shop.example.invalid/.well-known/jwks.json",
            )
        workload["existingSecretRefs"] = []
        if workload["mode"] == "gateway":
            workload["existingSecretRefs"] = [
                {"envName": key, "secretName": "externally-managed-auth", "key": key}
                for key in (
                    "COMMERCE_OIDC_CLIENT_SECRET",
                    "COMMERCE_GATEWAY_SIGNING_KEY",
                    "COMMERCE_AUTH_ENCRYPTION_KEYS",
                )
            ]
    return values


def crd_schemas() -> pathlib.Path:
    folder = OUT / "schemas"
    folder.mkdir(parents=True, exist_ok=True)
    urls = [
        "https://raw.githubusercontent.com/kubernetes-sigs/gateway-api/v1.3.0/config/crd/standard/gateway.networking.k8s.io_gateways.yaml",
        "https://raw.githubusercontent.com/kubernetes-sigs/gateway-api/v1.3.0/config/crd/standard/gateway.networking.k8s.io_httproutes.yaml",
        "https://raw.githubusercontent.com/kubernetes-sigs/secrets-store-csi-driver/v1.4.8/deploy/secrets-store.csi.x-k8s.io_secretproviderclasses.yaml",
    ]
    for url in urls:
        # Download only public schemas; never send repository content to a remote service.
        crd = yaml.safe_load(urllib.request.urlopen(url, timeout=60).read())
        kind = crd["spec"]["names"]["kind"].lower()
        for version in crd["spec"]["versions"]:
            schema = version["schema"]["openAPIV3Schema"]
            path = folder / f"{kind}_{version['name']}.json"
            path.write_text(json.dumps(schema), encoding="utf-8")
    return folder


def check_render(documents: list[dict]) -> None:
    by_kind: dict[str, list[dict]] = {}
    for doc in documents:
        by_kind.setdefault(doc["kind"], []).append(doc)
    assert len(by_kind["Deployment"]) == 8
    assert len(by_kind["ServiceAccount"]) == 8
    assert len(by_kind["PodDisruptionBudget"]) == len(by_kind["HorizontalPodAutoscaler"]) == 8
    assert len(by_kind["Gateway"]) == 2
    for service in by_kind["Service"]:
        assert service["spec"]["type"] == "ClusterIP"
    for gateway in by_kind["Gateway"]:
        assert gateway["spec"]["gatewayClassName"] == "approuting-istio"
        assert (
            gateway["spec"]["infrastructure"]["annotations"][
                "service.beta.kubernetes.io/azure-load-balancer-internal"
            ]
            == "true"
        )
    for deployment in by_kind["Deployment"]:
        name = deployment["metadata"]["name"]
        pod = deployment["spec"]["template"]["spec"]
        assert pod["serviceAccountName"] == name
        assert pod["securityContext"]["runAsNonRoot"] is True
        container = pod["containers"][0]
        assert "@sha256:" in container["image"]
        assert container["securityContext"]["readOnlyRootFilesystem"] is True
        assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
        assert all(key in container for key in ("startupProbe", "readinessProbe", "livenessProbe"))
        env = {item["name"]: item.get("value") for item in container["env"]}
        assert len(env) == len(container["env"]), f"duplicate environment variable in {name}"
        if name == "shopping-gateway":
            assert "merchant" not in env["COMMERCE_API_UPSTREAM"]
        if name in ("shopping-api", "shopping-worker"):
            assert env["COMMERCE_GUEST_ISSUER"] == "https://shop.example.invalid"
            assert (
                env["COMMERCE_GUEST_JWKS_URL"]
                == "https://shop.example.invalid/.well-known/jwks.json"
            )
        if name.startswith("merchant"):
            assert env["COMMERCE_PRIVATE_BOUNDARY"] == "true"
    policies = {p["metadata"]["name"]: p for p in by_kind["NetworkPolicy"]}
    assert policies["default-deny"]["spec"]["podSelector"] == {}
    assert policies["default-deny"]["spec"]["policyTypes"] == ["Ingress", "Egress"]
    for role in ("shopping", "merchant"):
        policy = json.dumps(policies[f"{role}-gateway"])
        other = "merchant" if role == "shopping" else "shopping"
        assert f"{other}-api" not in policy and f"{other}-web" not in policy
        assert policies[f"{role}-worker"]["spec"]["ingress"] == []


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--helm", default="helm")
    parser.add_argument("--kubeconform", default="kubeconform")
    parser.add_argument("--az", default=shutil.which("az") or "az")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    for target in ("container-apps", "aks"):
        output = OUT / f"{target}.json"
        run(
            args.az,
            "bicep",
            "build",
            "--file",
            str(ROOT / "infra" / target / "main.bicep"),
            "--outfile",
            str(output),
        )
        compiled = json.loads(output.read_text(encoding="utf-8-sig"))
        check_arm(compiled)
        if target == "aks":
            assert compiled["parameters"]["enableContainerInsights"]["defaultValue"] is False
            cluster = next(
                resource
                for resource in resources(compiled)
                if resource["type"].lower() == "microsoft.containerservice/managedclusters"
            )
            assert (
                cluster["properties"]["addonProfiles"]["omsagent"]["enabled"]
                == "[parameters('enableContainerInsights')]"
            )
        print(f"PASS {target}: Bicep compilation and offline ARM policy checks")
    schemas = crd_schemas()
    for vertical in ("retail", "travel", "telecom", "entertainment"):
        for secret_mode in (False, True):
            values = copy.deepcopy(fixtures())
            values["vertical"] = vertical
            if secret_mode:
                for role in ("shopping", "merchant"):
                    workload = values["workloads"][f"{role}-gateway"]
                    workload["secrets"] = [
                        {
                            "remoteName": item["key"].lower().replace("_", "-"),
                            "alias": item["key"].lower().replace("_", "-"),
                            "envName": item["envName"],
                        }
                        for item in workload["existingSecretRefs"]
                    ]
                    workload["existingSecretRefs"] = []
            stem = f"{vertical}-{'csi' if secret_mode else 'no-csi'}"
            values_file = OUT / f"{stem}.values.yaml"
            values_file.write_text(yaml.safe_dump(values), encoding="utf-8")
            run(args.helm, "lint", str(CHART), "--strict", "-f", str(values_file))
            rendered = run(
                args.helm,
                "template",
                "commerce",
                str(CHART),
                "--namespace",
                "commerce",
                "-f",
                str(values_file),
            )
            check_render([doc for doc in yaml.safe_load_all(rendered) if doc])
            manifest = OUT / f"{stem}.yaml"
            manifest.write_text(rendered, encoding="utf-8")
            result = run(
                args.kubeconform,
                "-strict",
                "-summary",
                "-kubernetes-version",
                "1.32.0",
                "-schema-location",
                "default",
                "-schema-location",
                str(schemas / "{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"),
                str(manifest),
            )
            print(f"PASS {stem}: {result.strip()}")
    # Schema/template rejection is part of the regression, not an accepted insecure render.
    cases = {}
    invalid = fixtures()
    invalid["images"]["runtime"] = "validation.azurecr.io/runtime:latest"
    cases["mutable image tag"] = invalid
    invalid = fixtures()
    invalid["gateway"]["className"] = "nginx"
    cases["unsupported gateway class"] = invalid
    invalid = fixtures()
    invalid["network"]["cosmosCidrs"] = ["0.0.0.0/0"]
    cases["public Cosmos network"] = invalid
    invalid = fixtures()
    invalid["workloads"]["shopping-gateway"]["env"]["COMMERCE_PRIVATE_BOUNDARY"] = "false"
    cases["boundary environment override"] = invalid
    invalid = fixtures()
    invalid["workloads"]["shopping-gateway"]["role"] = "merchant"
    cases["swapped role identity"] = invalid
    invalid = fixtures()
    invalid["workloads"]["merchant-gateway"]["existingSecretRefs"] = []
    cases["missing gateway secret references"] = invalid
    invalid = fixtures()
    invalid["replicas"] = 0
    cases["zero warm replicas"] = invalid
    invalid = fixtures()
    invalid["workloads"]["shopping-api"]["env"]["COMMERCE_GUEST_ISSUER"] = ""
    cases["missing shopping guest issuer"] = invalid
    invalid = fixtures()
    invalid["workloads"]["shopping-worker"]["env"]["COMMERCE_GUEST_JWKS_URL"] = (
        "http://insecure.example.invalid/jwks"
    )
    cases["non-HTTPS guest JWKS"] = invalid
    for label, invalid in cases.items():
        invalid_file = OUT / "invalid.values.yaml"
        invalid_file.write_text(yaml.safe_dump(invalid), encoding="utf-8")
        try:
            run(args.helm, "template", "commerce", str(CHART), "-f", str(invalid_file))
        except RuntimeError:
            print(f"PASS {label} rejected")
        else:
            raise AssertionError(f"{label} accepted")
    print(
        "Offline checks only. No Azure resource, quota, identity or network validation performed."
    )


if __name__ == "__main__":
    main()
