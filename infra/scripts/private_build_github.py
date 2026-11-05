"""Explicit, fail-closed GitHub environment/OIDC setup and claim checking."""

from __future__ import annotations

import argparse
import base64
import dataclasses
import json
import os
import pathlib
import urllib.parse
import urllib.request

from private_build import CLAIMS, Config, command, require


def api(path: str, *, payload: dict | None = None, method: str = "GET") -> object:
    args = ["gh", "api", path, "--method", method]
    if payload is not None:
        args += ["--input", "-"]
    return command(*args, payload=payload)


def setup(config: Config, approved: bool) -> None:
    require(approved, "explicit GitHub policy-change approval is required")
    require(
        bool(config.controller_client_id and config.builder_client_id),
        "bootstrap identity IDs required",
    )
    prefix = f"repos/{config.repository}"
    repo = api(prefix)
    user = api("user")
    require(
        repo["permissions"]["admin"]
        and repo["owner"]["type"] == "User"
        and repo["owner"]["id"] == user["id"]
        and repo["default_branch"] == "main",
        "setup requires the repository owner and confirmed main branch",
    )
    expected_prefix = (
        f"repo:{repo['owner']['login']}@{repo['owner']['id']}/{repo['name']}@{repo['id']}"
    )
    require(config.oidc_subject_prefix == expected_prefix, "immutable repository identity mismatch")
    oidc = api(f"{prefix}/actions/oidc/customization/sub")
    require(
        oidc.get("use_immutable_subject") is True, "immutable GitHub OIDC must already be enabled"
    )
    require(oidc.get("sub_claim_prefix") == expected_prefix, "discovered OIDC prefix mismatch")
    require(
        oidc["use_default"] or oidc.get("include_claim_keys") == CLAIMS,
        "refusing to overwrite an unrelated OIDC customization",
    )
    environments = api(f"{prefix}/environments?per_page=100")
    require(
        environments["total_count"] <= 100, "review paginated environments before changing policy"
    )
    environment = next(
        (e for e in environments["environments"] if e["name"].lower() == "commerce-build"), None
    )
    if environment is None:
        api(
            f"{prefix}/environments/commerce-build",
            method="PUT",
            payload={
                "wait_timer": 0,
                "prevent_self_review": False,
                "can_admins_bypass": False,
                "reviewers": [{"type": "User", "id": user["id"]}],
                "deployment_branch_policy": {
                    "protected_branches": False,
                    "custom_branch_policies": True,
                },
            },
        )
    else:
        environment = api(f"{prefix}/environments/commerce-build")
        reviews = [r for r in environment["protection_rules"] if r["type"] == "required_reviewers"]
        require(
            len(reviews) == 1
            and reviews[0].get("prevent_self_review") is False
            and {(r["type"], r["reviewer"]["id"]) for r in reviews[0]["reviewers"]}
            == {("User", user["id"])}
            and environment.get("can_admins_bypass") is False
            and environment["deployment_branch_policy"]
            == {"protected_branches": False, "custom_branch_policies": True},
            "existing environment differs; refusing a policy overwrite",
        )
    policies = api(f"{prefix}/environments/commerce-build/deployment-branch-policies?per_page=100")
    require(policies["total_count"] <= 100, "review paginated branch policies")
    if policies["total_count"] == 0:
        api(
            f"{prefix}/environments/commerce-build/deployment-branch-policies",
            method="POST",
            payload={"name": "main", "type": "branch"},
        )
    else:
        require(
            [(p["name"], p["type"]) for p in policies["branch_policies"]] == [("main", "branch")],
            "existing environment has broader branch/tag access; refusing overwrite",
        )
    if oidc["use_default"]:
        api(
            f"{prefix}/actions/oidc/customization/sub",
            method="PUT",
            payload={
                "use_default": False,
                "use_immutable_subject": True,
                "include_claim_keys": CLAIMS,
            },
        )
    values = {
        "AZURE_CLIENT_ID": config.controller_client_id,
        "AZURE_TENANT_ID": config.tenant_id,
        "AZURE_SUBSCRIPTION_ID": config.subscription_id,
        "COMMERCE_BUILD_CONFIG": json.dumps(dataclasses.asdict(config), separators=(",", ":")),
    }
    variables = api(f"{prefix}/environments/commerce-build/variables?per_page=100")
    require(variables["total_count"] <= 100, "review paginated environment variables")
    existing = {v["name"]: v["value"] for v in variables["variables"]}
    for key, value in values.items():
        if key in existing:
            require(existing[key] == value, f"refusing overwrite of environment variable {key}")
        else:
            api(
                f"{prefix}/environments/commerce-build/variables",
                method="POST",
                payload={"name": key, "value": value},
            )
    print("Protected build environment configured; no workflow run was approved or dispatched.")


def check_claims(config: Config, claims: dict) -> None:
    require(
        claims.get("iss") == "https://token.actions.githubusercontent.com"
        and claims.get("aud") == "api://AzureADTokenExchange"
        and claims.get("sub") == config.subject,
        "emitted OIDC issuer/audience/subject differs from the exact federated credential",
    )


def verify_claims(config: Config) -> None:
    url = urllib.parse.urlsplit(os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"])
    require(
        url.scheme == "https"
        and url.hostname is not None
        and url.hostname.endswith(".actions.githubusercontent.com"),
        "unexpected GitHub token request host",
    )
    query = urllib.parse.parse_qsl(url.query, keep_blank_values=True)
    require(not any(k == "audience" for k, _ in query), "unexpected predefined OIDC audience")
    query.append(("audience", "api://AzureADTokenExchange"))
    request = urllib.request.Request(
        urllib.parse.urlunsplit(url._replace(query=urllib.parse.urlencode(query))),
        headers={"Authorization": f"Bearer {os.environ['ACTIONS_ID_TOKEN_REQUEST_TOKEN']}"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        token = json.load(response)["value"]
    payload = token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    check_claims(config, claims)
    # Azure/login subsequently verifies the signature through Entra token exchange.
    print("Exact protected build OIDC claims checked; token not logged.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=pathlib.Path, required=True)
    parser.add_argument("action", choices=("setup", "verify-claims"))
    parser.add_argument("--approve-policy-change", action="store_true")
    args = parser.parse_args()
    config = Config(**json.loads(args.config.read_text(encoding="utf-8-sig")))
    if args.action == "setup":
        setup(config, args.approve_policy_change)
    else:
        verify_claims(config)


if __name__ == "__main__":
    main()
