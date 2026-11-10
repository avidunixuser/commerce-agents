"""Inspect demo identity or complete explicitly owned application setup."""

from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from azure.identity import AzureCliCredential

ROOT = Path(__file__).resolve().parents[2]
SCOPE_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, "https://acme.invalid/commerce-demo/access-as-user"))


class IdentityError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Graph:
    def __init__(self, tenant_id: str):
        self.credential = AzureCliCredential(tenant_id=tenant_id)
        self.transport = build_opener(NoRedirect())

    def request(
        self, path: str, body: dict | None = None, *, method: str | None = None
    ) -> dict | None:
        token = self.credential.get_token("https://graph.microsoft.com/.default").token
        request = Request(
            "https://graph.microsoft.com/v1.0" + path,
            data=json.dumps(body).encode() if body is not None else None,
            method=method or ("PATCH" if body is not None else "GET"),
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        )
        try:
            with self.transport.open(request, timeout=30) as response:
                data = response.read()
                return json.loads(data) if data else None
        except HTTPError as exc:
            raise IdentityError(f"Microsoft Graph request failed with HTTP {exc.code}") from None

    def close(self):
        self.credential.close()


def expected_roles() -> list[dict]:
    return json.loads((ROOT / "infra" / "container-apps" / "demo-app-roles.json").read_text())


def owned_patch(app: dict) -> dict:
    if app.get("signInAudience") != "AzureADMyOrg":
        raise IdentityError("Demo registration must be single-tenant")
    roles = expected_roles()
    expected = {role["value"]: role for role in roles}
    for role in app.get("appRoles", []):
        if role.get("value") not in expected or any(
            role.get(key) != expected[role["value"]][key]
            for key in ("id", "allowedMemberTypes", "isEnabled")
        ):
            raise IdentityError("Existing registration has incompatible roles; refusing overwrite")
    api = app.get("api") or {}
    scopes = api.get("oauth2PermissionScopes", [])
    for scope in scopes:
        if scope.get("id") != SCOPE_ID or scope.get("value") != "access_as_user":
            raise IdentityError(
                "Existing registration has unrelated API scopes; refusing overwrite"
            )
    if any(uri != "api://" + app["appId"] for uri in app.get("identifierUris", [])):
        raise IdentityError("Existing registration has unrelated API identifiers")
    preauthorized = api.get("preAuthorizedApplications", [])
    if any(
        entry.get("appId") != app["appId"] or entry.get("delegatedPermissionIds") != [SCOPE_ID]
        for entry in preauthorized
    ):
        raise IdentityError("Existing registration preauthorizes another client")
    if api.get("knownClientApplications"):
        raise IdentityError("Existing registration has unrelated known clients")
    return {
        "identifierUris": ["api://" + app["appId"]],
        "appRoles": roles,
        "api": {
            "requestedAccessTokenVersion": 2,
            "oauth2PermissionScopes": [
                {
                    "id": SCOPE_ID,
                    "value": "access_as_user",
                    "type": "User",
                    "isEnabled": True,
                    "adminConsentDisplayName": "Use the ACME retail demo",
                    "adminConsentDescription": "Access the isolated ACME retail demo as the signed-in user.",
                    "userConsentDisplayName": "Use the ACME retail demo",
                    "userConsentDescription": "Access the isolated ACME retail demo as you.",
                }
            ],
            "preAuthorizedApplications": [
                {
                    "appId": app["appId"],
                    "delegatedPermissionIds": [SCOPE_ID],
                }
            ],
        },
    }


def assignment_requests(service_principal_id: str, operator_id: str) -> dict:
    return {
        "requests": [
            {
                "id": role["value"],
                "method": "POST",
                "url": f"/servicePrincipals/{service_principal_id}/appRoleAssignedTo",
                "headers": {"Content-Type": "application/json"},
                "body": {
                    "principalId": operator_id,
                    "resourceId": service_principal_id,
                    "appRoleId": role["id"],
                },
            }
            for role in expected_roles()
        ]
    }


def assigned_roles(
    assignments: list[dict], service_principal_id: str, operator_id: str
) -> set[str]:
    ids = {
        row["appRoleId"]
        for row in assignments
        if row.get("principalId") == operator_id and row.get("resourceId") == service_principal_id
    }
    return {role["value"] for role in expected_roles() if role["id"] in ids}


def all_values(graph, path: str) -> list:
    values = []
    seen = set()
    while path:
        if path in seen or len(seen) >= 20:
            raise IdentityError("Demo identity lookup exceeded its bounded page limit")
        seen.add(path)
        result = graph.request(path)
        if not isinstance(result, dict):
            raise IdentityError("Missing Microsoft Graph collection")
        values.extend(result["value"])
        link = result.get("@odata.nextLink")
        if link and not link.startswith("https://graph.microsoft.com/v1.0/"):
            raise IdentityError("Unexpected Microsoft Graph continuation origin")
        path = link.removeprefix("https://graph.microsoft.com/v1.0") if link else ""
        if len(values) > 1000:
            raise IdentityError("Demo identity lookup exceeded its bounded collection limit")
    return values


def inspect(graph, config: dict, *, configure_owned: bool = False) -> dict:
    for key in ("tenantId", "oidcClientId", "operatorObjectId"):
        if str(uuid.UUID(config[key])) != config[key] or uuid.UUID(config[key]).int == 0:
            raise IdentityError("Explicit tenant, application and operator GUIDs are required")
    query = urlencode({"$filter": f"appId eq '{config['oidcClientId']}'"})
    apps = all_values(graph, "/applications?" + query)
    if len(apps) != 1:
        raise IdentityError("Expected exactly one application in the selected tenant")
    app = apps[0]
    patch = owned_patch(app)
    if configure_owned:
        me = graph.request("/me?$select=id")
        owners = all_values(graph, f"/applications/{app['id']}/owners?$select=id")
        if me["id"] != config["operatorObjectId"] or me["id"] not in {row["id"] for row in owners}:
            raise IdentityError(
                "Only the confirmed operator who owns this registration can configure it"
            )
        path = "/applications/" + app["id"]
        if not any(
            scope.get("id") == SCOPE_ID and scope.get("isEnabled")
            for scope in (app.get("api") or {}).get("oauth2PermissionScopes", [])
        ):
            # Graph validates preauthorization against already persisted scopes.
            scope_patch = {
                **patch,
                "api": {
                    key: value
                    for key, value in patch["api"].items()
                    if key != "preAuthorizedApplications"
                },
            }
            graph.request(path, scope_patch)
            staged = graph.request(path)
            if (staged.get("api") or {}).get("oauth2PermissionScopes") != patch["api"][
                "oauth2PermissionScopes"
            ]:
                raise IdentityError("API scope did not persist; no client preauthorization added")
        graph.request(path, patch)
        app = graph.request("/applications/" + app["id"])
    api = app.get("api") or {}
    configured = (
        api.get("requestedAccessTokenVersion") == 2
        and api.get("oauth2PermissionScopes") == patch["api"]["oauth2PermissionScopes"]
        and api.get("preAuthorizedApplications") == patch["api"]["preAuthorizedApplications"]
        and app.get("identifierUris") == patch["identifierUris"]
        and {role["id"] for role in app.get("appRoles", []) if role.get("isEnabled")}
        == {role["id"] for role in expected_roles()}
    )
    principals = all_values(graph, "/servicePrincipals?" + query)
    if len(principals) != 1 or principals[0].get("appOwnerOrganizationId") != config["tenantId"]:
        raise IdentityError("Expected exactly one enterprise application owned by this tenant")
    principal = principals[0]
    assignments = all_values(
        graph,
        f"/servicePrincipals/{principal['id']}/appRoleAssignedTo?$select=principalId,resourceId,appRoleId",
    )
    granted = assigned_roles(assignments, principal["id"], config["operatorObjectId"])
    return {
        "clientId": app["appId"],
        "applicationObjectId": app["id"],
        "servicePrincipalId": principal["id"],
        "configured": configured,
        "assignedRoles": sorted(granted),
        "ready": configured and granted == {"merchant", "approver"},
        "administratorAssignmentRequests": assignment_requests(
            principal["id"], config["operatorObjectId"]
        ),
    }


def complete_owned_setup(graph, config: dict) -> dict:
    result = inspect(graph, config)
    me = graph.request("/me?$select=id")
    if me["id"] != config["operatorObjectId"]:
        raise IdentityError("Only the confirmed operator can complete owned setup")
    principal_id = result["servicePrincipalId"]
    owners = all_values(graph, f"/servicePrincipals/{principal_id}/owners?$select=id")
    if me["id"] not in {row["id"] for row in owners}:
        raise IdentityError("Enterprise application ownership is required; use an administrator")
    app_owners = all_values(
        graph, f"/applications/{result['applicationObjectId']}/owners?$select=id"
    )
    if me["id"] not in {row["id"] for row in app_owners}:
        raise IdentityError("Application registration ownership is required")
    if result["ready"]:
        return result
    if not result["configured"]:
        result = inspect(graph, config, configure_owned=True)
    if not result["configured"]:
        raise IdentityError("Owned API configuration did not verify; no roles assigned")
    principal = graph.request(f"/servicePrincipals/{principal_id}?$select=appRoles")
    actual_roles = {
        role["id"]: role
        for role in principal.get("appRoles", [])
        if role.get("isEnabled") and role.get("allowedMemberTypes") == ["User"]
    }
    if any(
        actual_roles.get(role["id"], {}).get("value") != role["value"] for role in expected_roles()
    ):
        raise IdentityError("Enterprise role definitions have not propagated; no roles assigned")
    for request in result["administratorAssignmentRequests"]["requests"]:
        if request["id"] in result["assignedRoles"]:
            continue
        body = request["body"]
        assigned = graph.request(request["url"], body, method="POST")
        if not isinstance(assigned, dict) or any(
            assigned.get(key) != value for key, value in body.items()
        ):
            raise IdentityError("Unexpected owned role-assignment response; verify before retry")
    return inspect(graph, config)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    mutations = parser.add_mutually_exclusive_group()
    mutations.add_argument("--configure-owned-app", action="store_true")
    mutations.add_argument("--complete-owned-setup", action="store_true")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    graph = Graph(config["tenantId"])
    try:
        result = (
            complete_owned_setup(graph, config)
            if args.complete_owned_setup
            else inspect(graph, config, configure_owned=args.configure_owned_app)
        )
        print(json.dumps(result, indent=2))
        if not result["ready"]:
            raise SystemExit(2)
    finally:
        graph.close()


if __name__ == "__main__":
    main()
