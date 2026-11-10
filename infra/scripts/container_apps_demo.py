"""Fail-closed parameter generation for the opt-in retail Container Apps demo."""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import uuid
from pathlib import Path

from commerce_common.providers import provider_configuration_id

IMAGE_KEYS = {
    "runtime": "api",
    "gateway": "gateway",
    "shoppingWeb": "retail-storefront-web",
    "merchantWeb": "retail-merchant-web",
}
BASE_FIELDS = {
    "subscriptionId",
    "tenantId",
    "resourceGroup",
    "location",
    "name",
    "vnetName",
    "subnetPrefix",
    "endpointSubnetPrefix",
    "dnsProxyAddress",
    "routeTableName",
    "workspaceName",
    "insightsName",
    "registryName",
    "cosmosName",
    "vaultName",
    "openAIName",
    "privateEnvironmentName",
    "pullIdentityName",
    "databaseName",
    "modelName",
    "modelVersion",
    "oidcClientId",
    "operatorObjectId",
    "privateAccessCidrs",
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def private_network(value: str):
    network = ipaddress.ip_network(value)
    require(
        network.version == 4
        and any(
            network.subnet_of(ipaddress.ip_network(cidr))
            for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
        ),
        "Demo network inputs must be RFC1918 IPv4 ranges",
    )
    return network


def validate(config: dict) -> None:
    require(set(config) == BASE_FIELDS, "Demo configuration has missing or unrecognized fields")
    for key in BASE_FIELDS - {"privateAccessCidrs"}:
        require(isinstance(config[key], str) and bool(config[key]), f"Required string: {key}")
        require(config[key] == config[key].strip(), f"Whitespace in {key}")
    for key in ("subscriptionId", "tenantId", "oidcClientId", "operatorObjectId"):
        require(
            str(uuid.UUID(config[key])) == config[key] and uuid.UUID(config[key]).int != 0,
            f"Real {key} required",
        )
    require(
        re.fullmatch(r"[a-z][a-z0-9-]{2,11}", config["name"]) is not None,
        "Bounded demo prefix required",
    )
    require(
        re.fullmatch(r"[a-z]+[0-9]*", config["location"]) is not None,
        "Public Azure region required",
    )
    require(
        re.fullmatch(r"[a-z0-9]{5,50}", config["registryName"]) is not None,
        "ACR resource name required",
    )
    for key in (
        "resourceGroup",
        "vnetName",
        "routeTableName",
        "workspaceName",
        "insightsName",
        "cosmosName",
        "vaultName",
        "openAIName",
        "privateEnvironmentName",
        "pullIdentityName",
        "databaseName",
    ):
        require(
            re.fullmatch(r"[A-Za-z0-9_-]{1,63}", config[key]) is not None,
            f"Resource name required: {key}",
        )
    require(
        re.fullmatch(r"[A-Za-z0-9.-]+", config["modelName"]) is not None,
        "Verified model name required",
    )
    require(
        re.fullmatch(r"[A-Za-z0-9.-]+", config["modelVersion"]) is not None,
        "Verified model version required",
    )
    subnet = private_network(config["subnetPrefix"])
    endpoints = private_network(config["endpointSubnetPrefix"])
    require(
        subnet.prefixlen <= 27 and not subnet.overlaps(endpoints),
        "Non-overlapping ACA subnet of /27 or larger required",
    )
    dns = ipaddress.ip_address(config["dnsProxyAddress"])
    require(
        dns.version == 4
        and any(
            dns in private_network(cidr)
            for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
        ),
        "Private DNS proxy required",
    )
    require(
        isinstance(config["privateAccessCidrs"], list) and bool(config["privateAccessCidrs"]),
        "Explicit merchant private access required",
    )
    for cidr in config["privateAccessCidrs"]:
        private_network(cidr)
    require(
        config["privateEnvironmentName"] != config["name"] + "-shopper",
        "Merchant environment must be separate and internal",
    )


def images_from_manifest(config: dict, manifest: dict) -> dict:
    registry = config["registryName"] + ".azurecr.io/"
    require(isinstance(manifest, dict), "Build manifest must be an object")
    require(
        set(manifest)
        == set(IMAGE_KEYS.values())
        | {
            "travel-storefront-web",
            "travel-merchant-web",
            "telecom-storefront-web",
            "telecom-merchant-web",
            "entertainment-storefront-web",
            "entertainment-merchant-web",
        },
        "Exact ten-image private-build manifest required",
    )
    for source, value in manifest.items():
        require(
            isinstance(value, str)
            and value.startswith(registry)
            and re.fullmatch(r"[a-z0-9.]+/[a-z0-9/_-]+@sha256:[a-f0-9]{64}", value) is not None,
            f"Immutable private image digest required: {source}",
        )
    return {key: manifest[source] for key, source in IMAGE_KEYS.items()}


def identity_principals(config: dict, identities: list[dict] | None) -> dict[str, str]:
    require(
        isinstance(identities, list) and all(isinstance(item, dict) for item in identities),
        "Fresh managed-identity inventory required",
    )
    result = {}
    for key, suffix in {
        "shoppingApi": "shopping-api",
        "merchantApi": "merchant-api",
        "shoppingGateway": "shopping-gateway",
        "merchantGateway": "merchant-gateway",
    }.items():
        name = f"{config['name']}-{suffix}"
        matches = [item for item in identities if item.get("name") == name]
        require(len(matches) == 1, f"Exact managed identity required: {name}")
        identity = matches[0]
        expected = (
            f"/subscriptions/{config['subscriptionId']}/resourceGroups/{config['resourceGroup']}"
            f"/providers/Microsoft.ManagedIdentity/userAssignedIdentities/{name}"
        )
        require(
            isinstance(identity.get("id"), str)
            and identity["id"].lower() == expected.lower()
            and identity.get("tenantId") == config["tenantId"]
            and isinstance(identity.get("type"), str)
            and identity["type"].lower() == "microsoft.managedidentity/userassignedidentities",
            f"Managed identity tenant/resource mismatch: {name}",
        )
        principal = identity.get("principalId")
        require(
            isinstance(principal, str)
            and str(uuid.UUID(principal)) == principal
            and uuid.UUID(principal).int != 0,
            f"Real managed-identity principal required: {name}",
        )
        result[key] = principal
    require(len(set(result.values())) == 4, "Separate workload identities required")
    return result


def parameters(
    config: dict,
    stage: str,
    manifest: dict | None = None,
    *,
    identities: list[dict] | None = None,
) -> dict:
    validate(config)
    if stage == "foundation":
        keys = {
            "name",
            "location",
            "vnetName",
            "subnetPrefix",
            "endpointSubnetPrefix",
            "dnsProxyAddress",
            "routeTableName",
            "workspaceName",
            "registryName",
            "cosmosName",
            "vaultName",
            "databaseName",
        }
        values = {key: config[key] for key in keys}
    elif stage == "access":
        keys = {"name", "openAIName", "insightsName", "vaultName", "modelName", "modelVersion"}
        values = {key: config[key] for key in keys}
        values.update(
            principalIds=identity_principals(config, identities),
            enableModel=True,
            secretsReady=False,
            modelCapacity=100,
        )
    elif stage == "apps":
        require(manifest is not None, "Reviewed private-build manifest required")
        keys = {
            "name",
            "location",
            "privateEnvironmentName",
            "pullIdentityName",
            "registryName",
            "cosmosName",
            "vaultName",
            "openAIName",
            "databaseName",
            "oidcClientId",
            "insightsName",
            "operatorObjectId",
            "privateAccessCidrs",
        }
        values = {key: config[key] for key in keys}
        env = {
            "COMMERCE_PROVIDER": "azure_openai",
            "COMMERCE_MODEL": config["name"] + "-mini",
            "AZURE_OPENAI_ENDPOINT": f"https://{config['openAIName']}.openai.azure.com/openai/v1/",
        }
        values.update(
            providerId=provider_configuration_id(env),
            images=images_from_manifest(config, manifest),
            enablePublicIngress=False,
        )
    else:
        raise ValueError("Only foundation, access and apps stages produce non-secret parameters")
    return {
        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
        "contentVersion": "1.0.0.0",
        "parameters": {key: {"value": value} for key, value in values.items()},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--identities", type=Path)
    parser.add_argument("stage", choices=("foundation", "access", "apps"))
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    manifest = json.loads(args.manifest.read_text(encoding="utf-8-sig")) if args.manifest else None
    require(
        args.identities is None or args.stage == "access", "Identity inventory is for access only"
    )
    identities = (
        json.loads(args.identities.read_text(encoding="utf-8-sig")) if args.identities else None
    )
    print(json.dumps(parameters(config, args.stage, manifest, identities=identities), indent=2))


if __name__ == "__main__":
    main()
