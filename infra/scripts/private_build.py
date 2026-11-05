"""Protected-workflow controller for fresh, private, single-build Azure VMs."""

from __future__ import annotations

import argparse
import base64
import dataclasses
import ipaddress
import json
import os
import pathlib
import re
import shutil
import signal
import subprocess
import tempfile
import time
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[2]
CLAIMS = ["repo", "environment", "ref", "event_name", "workflow_ref", "runner_environment"]
MANAGER = "commerce-private-build"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def identifier(value: str, pattern: str, label: str) -> str:
    require(isinstance(value, str) and re.fullmatch(pattern, value) is not None, label)
    return value


@dataclasses.dataclass(frozen=True)
class Config:
    subscription_id: str
    tenant_id: str
    location: str
    build_resource_group: str
    foundation_resource_group: str
    name: str
    vnet_name: str
    firewall_policy_name: str
    registry_name: str
    repository: str
    oidc_subject_prefix: str
    image_version: str
    dns_proxy_address: str
    private_endpoint_cidr: str
    controller_client_id: str = ""
    builder_client_id: str = ""

    def __post_init__(self) -> None:
        for field in ("subscription_id", "tenant_id"):
            value = getattr(self, field)
            require(str(uuid.UUID(value)) == value and uuid.UUID(value).int != 0, field)
        for field in ("controller_client_id", "builder_client_id"):
            value = getattr(self, field)
            if value:
                require(str(uuid.UUID(value)) == value and uuid.UUID(value).int != 0, field)
        for field in ("build_resource_group", "foundation_resource_group", "vnet_name"):
            identifier(getattr(self, field), r"[A-Za-z0-9_-]{1,80}", field)
        identifier(self.firewall_policy_name, r"[A-Za-z0-9_-]{1,80}", "firewall policy")
        identifier(self.name, r"[a-z][a-z0-9-]{2,24}", "build prefix")
        identifier(self.location, r"[a-z]+[0-9]*", "public Azure region")
        identifier(self.registry_name, r"[a-z0-9]{5,50}", "ACR name")
        identifier(self.image_version, r"\d+\.\d+\.\d+", "pin an exact Ubuntu image version")
        identifier(self.repository, r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", "repository")
        owner, repo = self.repository.split("/")
        require(repo not in {".", ".."}, "repository")
        identifier(
            self.oidc_subject_prefix,
            rf"repo:{re.escape(owner)}@[1-9]\d*/{re.escape(repo)}@[1-9]\d*",
            "use the discovered immutable OIDC repo prefix",
        )
        require(self.build_resource_group != self.foundation_resource_group, "separate build group")
        dns = ipaddress.ip_address(self.dns_proxy_address)
        endpoints = ipaddress.ip_network(self.private_endpoint_cidr)
        private_ranges = [
            ipaddress.ip_network(prefix)
            for prefix in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
        ]
        require(
            dns.version == 4 and any(dns in network for network in private_ranges),
            "private IPv4 DNS proxy required",
        )
        require(
            endpoints.version == 4
            and endpoints.prefixlen >= 24
            and any(endpoints.subnet_of(network) for network in private_ranges),
            "bounded private IPv4 endpoint subnet required",
        )

    @property
    def group_id(self) -> str:
        return f"/subscriptions/{self.subscription_id}/resourceGroups/{self.build_resource_group}"

    @property
    def builder_id(self) -> str:
        return f"{self.group_id}/providers/Microsoft.ManagedIdentity/userAssignedIdentities/{self.name}-builder"

    @property
    def nsg_id(self) -> str:
        return f"{self.group_id}/providers/Microsoft.Network/networkSecurityGroups/{self.name}-build-nic"

    @property
    def subnet_id(self) -> str:
        return (
            f"/subscriptions/{self.subscription_id}/resourceGroups/{self.foundation_resource_group}"
            f"/providers/Microsoft.Network/virtualNetworks/{self.vnet_name}/subnets/runners"
        )

    @property
    def subject(self) -> str:
        return (
            f"{self.oidc_subject_prefix}:environment:commerce-build:ref:refs/heads/main"
            f":event_name:workflow_dispatch:workflow_ref:{self.repository}"
            "/.github/workflows/private-build.yml@refs/heads/main:runner_environment:github-hosted"
        )


def command(*args: str, payload: dict | None = None) -> object:
    executable = shutil.which(args[0])
    if executable is None:
        raise RuntimeError(f"Required tool not found: {args[0]}")
    result = subprocess.run(
        [executable, *args[1:]],
        input=json.dumps(payload) if payload is not None else None,
        capture_output=True,
        text=True,
        check=False,
        timeout=240,
    )
    if result.returncode:
        raise RuntimeError(f"{args[0]} {args[1]} failed:\n{result.stderr}\n{result.stdout}")
    if result.stderr.strip():
        print(result.stderr.strip(), flush=True)
    return json.loads(result.stdout) if result.stdout.strip() else None


def az(config: Config, *args: str) -> object:
    return command(
        "az",
        *args,
        "--subscription",
        config.subscription_id,
        "--output",
        "json",
        "--only-show-errors",
    )


def check_context(config: Config) -> None:
    account = az(config, "account", "show")
    require(
        account["id"] == config.subscription_id
        and account["tenantId"] == config.tenant_id
        and account["state"] == "Enabled",
        "Azure subscription/tenant/state differs from approved configuration",
    )


def workflow_source(config: Config, sha: str, operator: bool = False) -> None:
    identifier(sha, r"[0-9a-f]{40}", "full source commit SHA required")
    if operator:
        require(not os.environ.get("GITHUB_ACTIONS"), "operator mode is forbidden in Actions")
        main = command("gh", "api", f"repos/{config.repository}/commits/main")
        require(main["sha"] == sha, "operator smoke builds require the current reviewed main SHA")
        return
    expected = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_REPOSITORY": config.repository,
        "GITHUB_WORKFLOW_REF": f"{config.repository}/.github/workflows/private-build.yml@refs/heads/main",
        "GITHUB_SHA": sha,
        "RUNNER_ENVIRONMENT": "github-hosted",
    }
    require(
        all(os.environ.get(key) == value for key, value in expected.items()),
        "only the protected, manually dispatched main workflow may control private builds",
    )


def bootstrap_parameters(config: Config) -> dict:
    mapping = {
        "location": config.location,
        "buildResourceGroup": config.build_resource_group,
        "foundationResourceGroup": config.foundation_resource_group,
        "name": config.name,
        "vnetName": config.vnet_name,
        "firewallPolicyName": config.firewall_policy_name,
        "registryName": config.registry_name,
        "repository": config.repository,
        "oidcSubjectPrefix": config.oidc_subject_prefix,
        "dnsProxyAddress": config.dns_proxy_address,
        "privateEndpointCidr": config.private_endpoint_cidr,
    }
    return {
        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
        "contentVersion": "1.0.0.0",
        "parameters": {key: {"value": value} for key, value in mapping.items()},
    }


def run_tags(run_id: str, generation: str, sha: str) -> dict:
    identifier(run_id, r"[a-z][a-z0-9-]{2,30}", "bounded run ID required")
    identifier(generation, r"[0-9a-f]{32}", "build generation required")
    identifier(sha, r"[0-9a-f]{40}", "full source SHA required")
    return {
        "managedBy": MANAGER,
        "commerceRun": run_id,
        "commerceGeneration": generation,
        "commerceSource": sha,
    }


def owned_resource(resource: dict, resource_id: str, tags: dict) -> None:
    require(resource["id"].lower() == resource_id.lower(), "cleanup resource ID mismatch")
    require(
        all(resource.get("tags", {}).get(key) == value for key, value in tags.items()),
        f"refusing cleanup of unowned resource {resource_id}",
    )


def group_resources(config: Config) -> list:
    return az(config, "resource", "list", "--resource-group", config.build_resource_group)


def state_tags(config: Config, state: dict) -> dict:
    require(state["group_id"] == config.group_id, "cleanup subscription/resource group mismatch")
    tags = run_tags(state["run_id"], state["generation"], state["sha"])
    require(state["name"] == f"cb-{state['run_id']}", "cleanup name mismatch")
    return tags


def cleanup(config: Config, state: dict) -> None:
    tags = state_tags(config, state)
    expected_name = state["name"]
    for kind, suffix in (("virtualMachines", ""), ("networkInterfaces", "-nic")):
        provider = "Microsoft.Compute" if kind == "virtualMachines" else "Microsoft.Network"
        resource_id = f"{config.group_id}/providers/{provider}/{kind}/{expected_name}{suffix}"
        resources = group_resources(config)
        resource = next((r for r in resources if r["id"].lower() == resource_id.lower()), None)
        if resource is not None:
            owned_resource(resource, resource_id, tags)
            print(f"Deleting owned disposable resource: {resource_id}", flush=True)
            az(config, "resource", "delete", "--ids", resource_id)
        deadline = time.monotonic() + 180
        while any(r["id"].lower() == resource_id.lower() for r in group_resources(config)):
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Cleanup not complete; resource can remain billable: {resource_id}"
                )
            time.sleep(5)


def settle_deployment(
    config: Config, name: str, *, cancel: bool = False, state: dict | None = None
) -> None:
    states = az(
        config,
        "deployment",
        "group",
        "list",
        "--resource-group",
        config.build_resource_group,
        "--query",
        f"[?name=='{name}'].properties.provisioningState",
    )
    if not states:
        require(cancel, f"Deployment {name} was not accepted")
        return
    if cancel and states[0] not in {"Succeeded", "Failed", "Canceled"}:
        require(
            state is not None, "recovery state is required before canceling an active deployment"
        )
        state_tags(config, state)
        require(state["name"] == name, "deployment cancellation name mismatch")
        parameters = az(
            config,
            "deployment",
            "group",
            "show",
            "--resource-group",
            config.build_resource_group,
            "--name",
            name,
            "--query",
            "properties.parameters",
        )
        for parameter, key in (
            ("generation", "generation"),
            ("runId", "run_id"),
            ("sourceSha", "sha"),
        ):
            require(
                parameters.get(parameter, {}).get("value") == state[key],
                "refusing to cancel an unrelated deployment generation",
            )
        az(
            config,
            "deployment",
            "group",
            "cancel",
            "--resource-group",
            config.build_resource_group,
            "--name",
            name,
        )
    deadline = time.monotonic() + (600 if cancel else 1800)
    while time.monotonic() < deadline:
        state = az(
            config,
            "deployment",
            "group",
            "show",
            "--resource-group",
            config.build_resource_group,
            "--name",
            name,
            "--query",
            "properties.provisioningState",
        )
        if state in {"Succeeded", "Failed", "Canceled"}:
            if not cancel and state != "Succeeded":
                raise RuntimeError(
                    f"Private VM deployment {name} ended {state}; inspect its ARM operations"
                )
            return
        time.sleep(10)
    raise TimeoutError(
        f"Deployment {name} did not settle; do not delete resources while ARM can recreate them"
    )


def remote_build(config: Config, state: dict, mode: str, folder: pathlib.Path) -> str:
    identifier(mode, r"smoke|all", "build mode")
    values = {
        "repository": config.repository,
        "sha": state["sha"],
        "tag": state["name"],
        "registry": f"{config.registry_name}.azurecr.io",
        "registry_name": config.registry_name,
        "location": config.location,
        "builder_client_id": config.builder_client_id,
        "subscription_id": config.subscription_id,
        "tenant_id": config.tenant_id,
        "private_endpoint_cidr": config.private_endpoint_cidr,
        "mode": mode,
    }
    encoded = base64.b64encode(json.dumps(values).encode()).decode()
    source = (ROOT / "infra" / "scripts" / "private_build.sh").read_text(encoding="utf-8")
    header, separator, body = source.partition("\n")
    require(header == "#!/usr/bin/env bash" and bool(separator), "trusted Bash entrypoint required")
    script = f"{header}\nexport COMMERCE_BUILD_CONFIG='{encoded}'\n{body}"
    body = folder / "run-command.json"
    body.write_text(
        json.dumps(
            {
                "location": config.location,
                "properties": {
                    "source": {"script": script},
                    "asyncExecution": True,
                    "timeoutInSeconds": 9000,
                    "treatFailureAsDeploymentFailure": True,
                },
            }
        ),
        encoding="utf-8",
    )
    url = (
        f"https://management.azure.com{config.group_id}/providers/Microsoft.Compute/virtualMachines"
        f"/{state['name']}/runCommands/build?api-version=2024-11-01"
    )
    az(
        config,
        "rest",
        "--resource",
        "https://management.azure.com/",
        "--method",
        "put",
        "--url",
        url,
        "--body",
        f"@{body}",
    )
    deadline = time.monotonic() + 9300
    while time.monotonic() < deadline:
        response = az(
            config,
            "rest",
            "--resource",
            "https://management.azure.com/",
            "--method",
            "get",
            "--url",
            url,
            "--url-parameters",
            "$expand=instanceView",
        )
        view = response["properties"].get("instanceView", {})
        status = view.get("executionState")
        if status in {"Succeeded", "Failed", "Canceled", "TimedOut"}:
            if status != "Succeeded" or view.get("exitCode") != 0:
                raise RuntimeError(
                    f"Private build {status}: {view.get('error', '')}\n{view.get('output', '')}"
                )
            output = view.get("output", "")
            lines = [
                line for line in output.splitlines() if line.startswith("COMMERCE_BUILD_RESULT=")
            ]
            require(len(lines) == 1, "private build did not return its digest manifest")
            manifest = json.loads(lines[0].partition("=")[2])
            expected = (
                {"gateway"}
                if mode == "smoke"
                else {
                    "gateway",
                    "api",
                    *(
                        f"{vertical}-{app}"
                        for vertical in ("retail", "travel", "telecom", "entertainment")
                        for app in ("storefront-web", "merchant-web")
                    ),
                }
            )
            require(set(manifest) == expected, "private build image manifest mismatch")
            for image, digest in manifest.items():
                identifier(
                    image,
                    r"gateway|api|(?:retail|travel|telecom|entertainment)-(?:storefront-web|merchant-web)",
                    "image name",
                )
                identifier(digest, r"sha256:[0-9a-f]{64}", "immutable image digest")
            return output
        time.sleep(15)
    raise TimeoutError("Private build exceeded its control-plane wait budget")


def preview_vm(
    config: Config, state: dict, template: pathlib.Path, parameters: pathlib.Path
) -> None:
    arguments = (
        "--resource-group",
        config.build_resource_group,
        "--name",
        state["name"],
        "--template-file",
        str(template),
        "--parameters",
        f"@{parameters}",
    )
    validation = az(config, "deployment", "group", "validate", *arguments)
    require(
        validation.get("properties", {}).get("provisioningState") == "Succeeded"
        and not validation.get("error"),
        "private VM provider validation did not succeed",
    )
    preview = az(config, "deployment", "group", "what-if", *arguments, "--no-pretty-print")
    require(preview.get("status") == "Succeeded" and not preview.get("error"), "VM what-if failed")
    expected = {
        f"{config.group_id}/providers/Microsoft.Compute/virtualMachines/{state['name']}".lower(),
        f"{config.group_id}/providers/Microsoft.Network/networkInterfaces/{state['name']}-nic".lower(),
    }
    changes = preview.get("changes", [])
    require(
        len(changes) == 2
        and all(change["changeType"] == "Create" for change in changes)
        and {change["resourceId"].lower() for change in changes} == expected,
        "VM preview must contain only the two exact fresh VM/NIC creations",
    )
    print(
        "Provider validation passed; VM preview contains only the owned fresh VM/NIC.", flush=True
    )


def execute(config: Config, args: argparse.Namespace) -> None:
    require(args.approve_disposable_cleanup, "explicit approval of VM/NIC lifecycle is required")
    require(
        bool(config.controller_client_id and config.builder_client_id),
        "hydrate both identity client IDs after bootstrap",
    )
    workflow_source(config, args.source_sha, args.operator)
    check_context(config)
    state = {
        "run_id": args.run_id,
        "generation": uuid.uuid4().hex,
        "sha": args.source_sha,
        "name": f"cb-{args.run_id}",
        "group_id": config.group_id,
    }
    run_tags(state["run_id"], state["generation"], state["sha"])
    require(not args.state_file.exists(), "use a new state file; never overwrite recovery state")
    names = {state["name"], state["name"] + "-nic"}
    require(
        not any(r["name"] in names for r in group_resources(config)),
        "run resource name already exists",
    )
    args.state_file.parent.mkdir(parents=True, exist_ok=True)
    args.state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="commerce-private-build-") as directory:
        folder = pathlib.Path(directory)
        key = folder / "bootstrap-key"
        command("ssh-keygen", "-q", "-t", "rsa", "-b", "3072", "-N", "", "-f", str(key))
        template = folder / "vm.json"
        command(
            "az",
            "bicep",
            "build",
            "--file",
            str(ROOT / "infra" / "private-build" / "vm.bicep"),
            "--outfile",
            str(template),
        )
        values = {
            "location": config.location,
            "name": state["name"],
            "runId": state["run_id"],
            "generation": state["generation"],
            "sourceSha": state["sha"],
            "subnetId": config.subnet_id,
            "nsgId": config.nsg_id,
            "builderIdentityId": config.builder_id,
            "sshPublicKey": key.with_suffix(".pub").read_text().strip(),
            "imageVersion": config.image_version,
        }
        parameters = folder / "vm.parameters.json"
        parameters.write_text(
            json.dumps({"parameters": {k: {"value": v} for k, v in values.items()}}),
            encoding="utf-8",
        )
        preview_vm(config, state, template, parameters)
        try:
            az(
                config,
                "deployment",
                "group",
                "create",
                "--resource-group",
                config.build_resource_group,
                "--name",
                state["name"],
                "--template-file",
                str(template),
                "--parameters",
                f"@{parameters}",
                "--no-wait",
            )
            settle_deployment(config, state["name"])
            print(remote_build(config, state, args.images, folder), flush=True)
        finally:
            print(
                f"Settling and cleaning owned build {state['name']}; recovery state: {args.state_file}",
                flush=True,
            )
            settle_deployment(config, state["name"], cancel=True, state=state)
            cleanup(config, state)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=pathlib.Path, required=True)
    sub = parser.add_subparsers(dest="action", required=True)
    parameters = sub.add_parser("parameters")
    parameters.add_argument("--output", type=pathlib.Path, required=True)
    hydrate = sub.add_parser("hydrate")
    hydrate.add_argument("--deployment-name", required=True)
    hydrate.add_argument("--output", type=pathlib.Path, required=True)
    run = sub.add_parser("run")
    run.add_argument("--source-sha", required=True)
    run.add_argument("--run-id", required=True)
    run.add_argument("--images", choices=("smoke", "all"), default="smoke")
    run.add_argument("--state-file", type=pathlib.Path, required=True)
    run.add_argument("--operator", action="store_true")
    run.add_argument("--approve-disposable-cleanup", action="store_true")
    recover = sub.add_parser("cleanup")
    recover.add_argument("--state-file", type=pathlib.Path, required=True)
    recover.add_argument("--approve-disposable-cleanup", action="store_true")
    args = parser.parse_args()
    config = Config(**json.loads(args.config.read_text(encoding="utf-8-sig")))
    if args.action == "parameters":
        require(not args.output.exists(), "refusing to overwrite an existing parameters file")
        args.output.write_text(json.dumps(bootstrap_parameters(config), indent=2), encoding="utf-8")
    elif args.action == "hydrate":
        require(not args.output.exists(), "refusing to overwrite an existing runtime configuration")
        identifier(args.deployment_name, r"[A-Za-z0-9_-]{1,64}", "bootstrap deployment name")
        check_context(config)
        deployment = az(config, "deployment", "sub", "show", "--name", args.deployment_name)
        require(
            deployment["properties"]["provisioningState"] == "Succeeded",
            "bootstrap did not succeed",
        )
        outputs = deployment["properties"]["outputs"]
        require(outputs["oidcSubject"]["value"] == config.subject, "bootstrap federation differs")
        for key, expected in (
            ("subnetId", config.subnet_id),
            ("nsgId", config.nsg_id),
            ("builderIdentityId", config.builder_id),
        ):
            require(outputs[key]["value"].lower() == expected.lower(), f"bootstrap {key} mismatch")
        runtime = dataclasses.replace(
            config,
            controller_client_id=outputs["controllerClientId"]["value"],
            builder_client_id=outputs["builderClientId"]["value"],
        )
        args.output.write_text(json.dumps(dataclasses.asdict(runtime), indent=2), encoding="utf-8")
    elif args.action == "run":

        def interrupted(signum, frame):
            raise RuntimeError(f"Build interrupted by signal {signum}; attempting scoped cleanup")

        signal.signal(signal.SIGTERM, interrupted)
        execute(config, args)
    else:
        require(args.approve_disposable_cleanup, "explicit cleanup approval is required")
        check_context(config)
        state = json.loads(args.state_file.read_text(encoding="utf-8"))
        state_tags(config, state)
        settle_deployment(config, state["name"], cancel=True, state=state)
        cleanup(config, state)


if __name__ == "__main__":
    main()
