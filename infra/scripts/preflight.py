"""Validate operator inputs offline. This is not a quota or availability test."""

from __future__ import annotations

import argparse
import ipaddress
import json
import pathlib
import re
import sys
from urllib.parse import urlsplit

IMAGE = re.compile(r"^[a-z0-9.-]+\.azurecr\.io/[a-z0-9/._-]+@sha256:[a-f0-9]{64}$")
HOST = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
ROLES = ("shopping", "merchant")
RESERVED = {
    "COMMERCE_APP_ROOT",
    "COMMERCE_ROLE",
    "COMMERCE_VERTICAL",
    "COMMERCE_MODE",
    "COMMERCE_ENVIRONMENT",
    "COMMERCE_PRIVATE_BOUNDARY",
    "COMMERCE_API_UPSTREAM",
    "COMMERCE_WEB_UPSTREAM",
    "COMMERCE_PUBLIC_ORIGIN",
    "COMMERCE_COSMOS_ENDPOINT",
    "COMMERCE_COSMOS_DATABASE",
    "COMMERCE_COSMOS_CONTAINER",
    "COMMERCE_AUTH_CONTAINER",
    "AZURE_CLIENT_ID",
}
HOST_REQUIRED = {
    "COMMERCE_AUTH_ISSUER",
    "COMMERCE_AUTH_AUDIENCE",
    "COMMERCE_AUTH_JWKS_URL",
    "COMMERCE_ALLOWED_TENANTS",
    "COMMERCE_PROVIDER_ID",
    "COMMERCE_BACKEND_FACTORY",
}
GATEWAY_REQUIRED = {
    "COMMERCE_OIDC_ISSUER",
    "COMMERCE_OIDC_CLIENT_ID",
    "COMMERCE_OIDC_API_AUDIENCE",
    "COMMERCE_TENANT_ID",
    "COMMERCE_GATEWAY_ISSUER",
    "COMMERCE_GATEWAY_SIGNING_KID",
    "COMMERCE_OIDC_SCOPE",
}
GATEWAY_SECRETS = {
    "COMMERCE_OIDC_CLIENT_SECRET",
    "COMMERCE_GATEWAY_SIGNING_KEY",
    "COMMERCE_AUTH_ENCRYPTION_KEYS",
}


def validate(p: dict, target: str, *, phase: str = "application") -> None:
    if target not in {"container-apps", "aks"}:
        raise ValueError("Unknown deployment target")
    if phase not in {"application", "foundation"}:
        raise ValueError("Unknown deployment phase")
    if "parameters" in p:
        p = {key: value["value"] for key, value in p["parameters"].items()}
    foundation = phase == "foundation"
    if foundation:
        if target != "container-apps":
            raise ValueError("Foundation-only validation supports Container Apps, not AKS")
        if any(p.get(flag) is not False for flag in ("enableWorkloads", "enableIngress")):
            raise ValueError(
                "Foundation requires explicit enableWorkloads=false and enableIngress=false"
            )
        if any(
            p.get(key)
            for key in (
                "images",
                "runtimeEnvironment",
                "secretReferences",
                "storefrontHost",
                "merchantHost",
                "certificateSecretUri",
                "modelDeployments",
                "claudeDeployments",
                "claudeAttestation",
            )
        ):
            raise ValueError(
                "Foundation excludes application configuration, ingress credentials, model deployments and Marketplace attestation"
            )
    if any(marker in json.dumps(p) for marker in ("<", "REPLACE_", ".invalid")):
        raise ValueError("Replace every example placeholder before preflight")
    if p.get("modelAvailabilityVerified") is not True:
        raise ValueError(
            "Model availability/private inference/Entra/quota verification is not attested"
        )
    configured = 0
    for kind in ("OpenAI", "Claude"):
        create = p.get(f"create{kind}", False)
        existing = p.get(f"existing{kind}Name", "")
        if create and existing:
            raise ValueError(f"{kind}: select new OR existing, not both")
        configured += bool(create or existing)
        deployments_key = "modelDeployments" if kind == "OpenAI" else "claudeDeployments"
        if create and not foundation and not p.get(deployments_key):
            raise ValueError(
                f"{kind}: explicit model/version/deployment/SKU/capacity inputs required"
            )
    if not configured:
        raise ValueError("At least one private model resource is required")
    if (
        not foundation
        and p.get("createClaude")
        and not all(
            p.get("claudeAttestation", {}).get(key)
            for key in ("organizationName", "countryCode", "industry")
        )
    ):
        raise ValueError(
            "New Claude deployments require real organization attestation and Marketplace terms approval"
        )
    if not foundation:
        for host in ("storefrontHost", "merchantHost"):
            if not HOST.fullmatch(p.get(host, "")):
                raise ValueError(f"{host}: supply a DNS hostname, not a URL")
        if p["storefrontHost"] == p["merchantHost"]:
            raise ValueError("Storefront and merchant hosts must differ")
    cidrs = p.get("privateAccessCidrs", [])
    if not cidrs:
        raise ValueError("Private connectivity CIDRs are required")
    private_ranges = tuple(
        ipaddress.ip_network(cidr) for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    )
    for cidr in cidrs:
        network = ipaddress.ip_network(cidr)
        if network.version != 4 or not any(
            network.subnet_of(allowed) for allowed in private_ranges
        ):
            raise ValueError("Merchant source CIDRs must be bounded private IPv4 networks")
    if p.get("runtimeContainer") == "identity":
        raise ValueError("The gateway identity container cannot also hold runtime data")
    subnet_nsgs = p.get("existingSubnetNsgIds", {})
    if (
        not isinstance(subnet_nsgs, dict)
        or set(subnet_nsgs) - {"workload", "endpoints", "runners"}
        or not all(
            isinstance(resource_id, str)
            and re.fullmatch(
                r"/subscriptions/[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
                r"/resourceGroups/[a-zA-Z0-9_.()-]+"
                r"/providers/Microsoft\.Network/networkSecurityGroups/[a-zA-Z0-9_.()-]+",
                resource_id,
                re.IGNORECASE,
            )
            for resource_id in subnet_nsgs.values()
        )
    ):
        raise ValueError("Supply existing NSG resource IDs for workload/endpoints/runners only")
    if foundation:
        return
    if target == "container-apps":
        images = p.get("images", {})
        if set(images) != {"runtime", "gateway", "shoppingWeb", "merchantWeb"}:
            raise ValueError("Supply all four digest-pinned images")
        if not all(IMAGE.fullmatch(value) for value in images.values()):
            raise ValueError("Images must be private ACR repository@sha256:64hex references")
        if len({value.split("/")[0] for value in images.values()}) != 1:
            raise ValueError("All images must use this target's private ACR")
        for role in ROLES:
            for mode in ("api", "worker", "gateway"):
                name = f"{role}-{mode}"
                env = p.get("runtimeEnvironment", {}).get(name, {})
                if RESERVED.intersection(env):
                    raise ValueError(f"{name}: do not override enforced deployment environment")
                if GATEWAY_SECRETS.intersection(env):
                    raise ValueError(f"{name}: secret values must be Key Vault references")
                required = GATEWAY_REQUIRED if mode == "gateway" else HOST_REQUIRED
                if not required.issubset(env) or not all(env[key] for key in required):
                    raise ValueError(f"{name}: missing required production configuration")
                if role == "shopping" and mode in ("api", "worker"):
                    for key in ("COMMERCE_GUEST_ISSUER", "COMMERCE_GUEST_JWKS_URL"):
                        url = urlsplit(env.get(key, ""))
                        if (
                            url.scheme != "https"
                            or not url.netloc
                            or url.username
                            or url.password
                            or url.fragment
                        ):
                            raise ValueError(f"{name}: {key} must be an HTTPS authority/URL")
                    if env["COMMERCE_GUEST_ISSUER"] == env["COMMERCE_AUTH_ISSUER"]:
                        raise ValueError(f"{name}: guest and customer OIDC authorities must differ")
                refs = p.get("secretReferences", {}).get(name, [])
                if mode != "gateway" and refs:
                    raise ValueError(
                        f"{name}: no Key Vault grants are provisioned for API/worker identities"
                    )
                if mode == "gateway" and not GATEWAY_SECRETS.issubset(
                    {ref["envName"] for ref in refs}
                ):
                    raise ValueError(f"{name}: gateway secret references missing")
                for ref in refs:
                    if ref["envName"] in RESERVED or ref["envName"] in env:
                        raise ValueError(
                            f"{name}: secret environment cannot override deployment configuration"
                        )
                    if not re.fullmatch(
                        r"https://[a-z0-9-]+\.vault\.azure\.net/secrets/[a-zA-Z0-9-]+(?:/[a-zA-Z0-9]+)?",
                        ref["keyVaultUrl"],
                    ):
                        raise ValueError(f"{name}: use Key Vault secret URIs only")
        for role in ROLES:
            api = p["runtimeEnvironment"][f"{role}-api"]
            worker = p["runtimeEnvironment"][f"{role}-worker"]
            gateway = p["runtimeEnvironment"][f"{role}-gateway"]
            for key in ("COMMERCE_AUTH_ISSUER", "COMMERCE_AUTH_AUDIENCE"):
                if worker[key] != api[key]:
                    raise ValueError(f"{role}: API and worker authentication settings differ")
            if (
                gateway["COMMERCE_OIDC_ISSUER"] != api["COMMERCE_AUTH_ISSUER"]
                or gateway["COMMERCE_OIDC_API_AUDIENCE"] != api["COMMERCE_AUTH_AUDIENCE"]
            ):
                raise ValueError(f"{role}: gateway OIDC issuer/audience must match the role API")
            if role == "shopping":
                for key in ("COMMERCE_GUEST_ISSUER", "COMMERCE_GUEST_JWKS_URL"):
                    if worker[key] != api[key]:
                        raise ValueError("Shopping API and worker guest settings differ")
                if gateway["COMMERCE_GATEWAY_ISSUER"] != api["COMMERCE_GUEST_ISSUER"]:
                    raise ValueError("Shopping gateway and guest validation issuers differ")
        if p.get("enableIngress") and not p.get("enableWorkloads"):
            raise ValueError("Ingress cannot be enabled before workloads")
    else:
        if not p.get("adminGroupObjectIds") or not p.get("kubernetesVersion"):
            raise ValueError(
                "AKS Entra administration groups and supported Kubernetes version required"
            )
        for workload, names in p.get("gatewaySecretNames", {}).items():
            if workload not in {"shopping-gateway", "merchant-gateway"}:
                raise ValueError("Key Vault secret grants are gateway-only")
            if not isinstance(names, list) or not all(
                isinstance(name, str) and re.fullmatch(r"[a-zA-Z0-9-]{1,127}", name)
                for name in names
            ):
                raise ValueError("gatewaySecretNames must contain lists of Key Vault secret names")
    if p.get("enableIngress") and not re.fullmatch(
        r"https://[a-z0-9-]+\.vault\.azure\.net/secrets/[a-zA-Z0-9-]+/?",
        p.get("certificateSecretUri", ""),
    ):
        raise ValueError("Versionless Key Vault TLS certificate secret URI required before ingress")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("container-apps", "aks"), required=True)
    parser.add_argument("--phase", choices=("application", "foundation"), default="application")
    parser.add_argument("--parameters", type=pathlib.Path, required=True)
    args = parser.parse_args()
    try:
        validate(
            json.loads(args.parameters.read_text(encoding="utf-8-sig")),
            args.target,
            phase=args.phase,
        )
    except (ValueError, KeyError) as error:
        print(f"BLOCKED: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    if args.phase == "foundation":
        print(
            "Foundation-only offline input checks passed. Live resource validation remains required; application/model deployment and inference are not validated."
        )
    else:
        print(
            "Offline input checks passed. Live model entitlement/quota/private endpoints, DNS, RBAC and ingress checks remain mandatory."
        )


if __name__ == "__main__":
    main()
