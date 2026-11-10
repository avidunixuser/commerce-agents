import copy
import unittest
import uuid

from container_apps_demo import identity_principals, images_from_manifest, parameters, validate
from demo_identity import (
    SCOPE_ID,
    IdentityError,
    all_values,
    assigned_roles,
    assignment_requests,
    complete_owned_setup,
    expected_roles,
    inspect,
    owned_patch,
)


def config():
    return {
        "subscriptionId": "11111111-1111-4111-8111-111111111111",
        "tenantId": "22222222-2222-4222-8222-222222222222",
        "resourceGroup": "validation",
        "location": "swedencentral",
        "name": "acmedemo",
        "vnetName": "existing-vnet",
        "subnetPrefix": "10.80.8.0/23",
        "endpointSubnetPrefix": "10.80.5.0/24",
        "dnsProxyAddress": "10.80.0.4",
        "routeTableName": "existing-routes",
        "workspaceName": "existing-logs",
        "insightsName": "existing-insights",
        "registryName": "validation",
        "cosmosName": "existing-cosmos",
        "vaultName": "existing-vault",
        "openAIName": "existing-openai",
        "privateEnvironmentName": "existing-private-aca",
        "pullIdentityName": "existing-pull",
        "databaseName": "commerce",
        "modelName": "verified-model",
        "modelVersion": "verified-version",
        "oidcClientId": "33333333-3333-4333-8333-333333333333",
        "operatorObjectId": "44444444-4444-4444-8444-444444444444",
        "privateAccessCidrs": ["10.80.0.0/16"],
    }


def manifest():
    return {
        name: f"validation.azurecr.io/{name}@sha256:{'a' * 64}"
        for name in (
            "api",
            "gateway",
            "retail-storefront-web",
            "retail-merchant-web",
            "travel-storefront-web",
            "travel-merchant-web",
            "telecom-storefront-web",
            "telecom-merchant-web",
            "entertainment-storefront-web",
            "entertainment-merchant-web",
        )
    }


def identity_inventory():
    current = config()
    return [
        {
            "name": f"{current['name']}-{suffix}",
            "id": (
                f"/subscriptions/{current['subscriptionId']}/resourceGroups/{current['resourceGroup']}"
                f"/providers/Microsoft.ManagedIdentity/userAssignedIdentities/{current['name']}-{suffix}"
            ),
            "type": "Microsoft.ManagedIdentity/userAssignedIdentities",
            "tenantId": current["tenantId"],
            "principalId": str(uuid.UUID(int=index + 1)),
        }
        for index, suffix in enumerate(
            ("shopping-api", "merchant-api", "shopping-gateway", "merchant-gateway")
        )
    ]


class OwnedIdentityGraph:
    def __init__(self):
        self.app = {
            "id": "app-object",
            "appId": config()["oidcClientId"],
            "signInAudience": "AzureADMyOrg",
        }
        self.operator = config()["operatorObjectId"]
        self.app_owned = True
        self.principal_owned = True
        self.tenant = config()["tenantId"]
        self.propagated = True
        self.assignments = []
        self.writes = []

    def request(self, path, body=None, *, method=None):
        if method == "POST":
            self.writes.append((method, path, copy.deepcopy(body)))
            self.assignments.append(copy.deepcopy(body))
            return copy.deepcopy(body)
        if body is not None:
            self.writes.append(("PATCH", path, copy.deepcopy(body)))
            self.app.update(copy.deepcopy(body))
            return None
        if path.startswith("/applications?"):
            return {"value": [copy.deepcopy(self.app)]}
        if path == "/applications/app-object":
            return copy.deepcopy(self.app)
        if path.startswith("/applications/app-object/owners"):
            return {"value": [{"id": self.operator}] if self.app_owned else []}
        if path == "/me?$select=id":
            return {"id": self.operator}
        if path.startswith("/servicePrincipals?"):
            return {"value": [{"id": "resource", "appOwnerOrganizationId": self.tenant}]}
        if path.startswith("/servicePrincipals/resource/owners"):
            return {"value": [{"id": self.operator}] if self.principal_owned else []}
        if path.startswith("/servicePrincipals/resource/appRoleAssignedTo"):
            return {"value": copy.deepcopy(self.assignments)}
        if path == "/servicePrincipals/resource?$select=appRoles":
            return {"appRoles": self.app.get("appRoles", []) if self.propagated else []}
        raise AssertionError(f"Unexpected Graph read: {path}")


class DemoTests(unittest.TestCase):
    def test_initial_parameters_never_publish_the_gateway_or_enable_a_model(self):
        foundation = parameters(config(), "foundation")["parameters"]
        for key in ("enableModel", "enableApiAccess", "secretsReady", "principalIds"):
            self.assertNotIn(key, foundation)
        apps = parameters(config(), "apps", manifest())["parameters"]
        self.assertIs(apps["enablePublicIngress"]["value"], False)
        self.assertTrue(apps["providerId"]["value"].startswith("providers-v1:"))
        self.assertEqual(len(apps["images"]["value"]), 4)

    def test_access_uses_literal_principals_without_redeploying_the_foundation(self):
        inventory = identity_inventory()
        access = parameters(config(), "access", identities=inventory)["parameters"]
        self.assertEqual(
            access["principalIds"]["value"],
            {
                key: item["principalId"]
                for key, item in zip(
                    ("shoppingApi", "merchantApi", "shoppingGateway", "merchantGateway"),
                    inventory,
                    strict=True,
                )
            },
        )
        self.assertIs(access["enableModel"]["value"], True)
        self.assertIs(access["secretsReady"]["value"], False)
        self.assertEqual(access["modelCapacity"]["value"], 100)
        self.assertFalse(
            {"vnetName", "subnetPrefix", "cosmosName", "workspaceName"} & access.keys()
        )

    def test_access_requires_a_complete_unique_inventory(self):
        for inventory in (None, {}, [], ["invalid"], identity_inventory()[:-1]):
            with self.subTest(inventory=inventory), self.assertRaises(ValueError):
                parameters(config(), "access", identities=inventory)
        duplicate = identity_inventory()
        duplicate.append(copy.deepcopy(duplicate[0]))
        with self.assertRaises(ValueError):
            parameters(config(), "access", identities=duplicate)

    def test_access_rejects_cross_tenant_resource_or_invalid_principals(self):
        for key, value in (
            ("tenantId", config()["operatorObjectId"]),
            ("id", "/subscriptions/another-subscription/resourceGroups/validation"),
            ("id", None),
            ("type", "Microsoft.Graph/servicePrincipals"),
            ("type", None),
            ("principalId", "a-display-name"),
            ("principalId", None),
            ("principalId", str(uuid.UUID(int=0))),
        ):
            inventory = identity_inventory()
            inventory[0][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                parameters(config(), "access", identities=inventory)
        inventory = identity_inventory()
        inventory[1]["principalId"] = inventory[0]["principalId"]
        with self.assertRaises(ValueError):
            identity_principals(config(), inventory)

    def test_access_does_not_depend_on_inventory_order_or_resource_id_casing(self):
        inventory = identity_inventory()
        expected = identity_principals(config(), inventory)
        for item in inventory:
            item["id"] = item["id"].upper()
        self.assertEqual(identity_principals(config(), list(reversed(inventory))), expected)

    def test_demo_cannot_override_auth_or_escape_the_private_boundary(self):
        for key, value in (
            ("oidcClientId", "a-display-name"),
            ("privateAccessCidrs", ["0.0.0.0/0"]),
            ("privateAccessCidrs", []),
            ("subnetPrefix", "10.80.5.0/24"),
            ("subnetPrefix", "10.80.8.0/28"),
            ("dnsProxyAddress", "8.8.8.8"),
            ("privateEnvironmentName", "acmedemo-shopper"),
            ("COMMERCE_MODE", "production"),
        ):
            with self.subTest(key=key):
                current = config()
                current[key] = value
                with self.assertRaises((ValueError, TypeError)):
                    validate(current)

    def test_images_are_the_exact_private_ten_image_manifest(self):
        for image in (
            "validation.azurecr.io/api:latest",
            f"public.example/api@sha256:{'a' * 64}",
            "validation.azurecr.io/api@sha256:broken",
        ):
            current = manifest()
            current["api"] = image
            with self.subTest(image=image), self.assertRaises(ValueError):
                images_from_manifest(config(), current)
        current = manifest()
        del current["travel-merchant-web"]
        with self.assertRaises(ValueError):
            images_from_manifest(config(), current)
        current = manifest()
        current["travel-merchant-web"] = "unverified:latest"
        with self.assertRaises(ValueError):
            images_from_manifest(config(), current)

    def test_owned_application_patch_grants_no_directory_permissions(self):
        app = {"appId": config()["oidcClientId"], "signInAudience": "AzureADMyOrg"}
        patch = owned_patch(app)
        self.assertNotIn("requiredResourceAccess", patch)
        self.assertEqual(patch["api"]["requestedAccessTokenVersion"], 2)
        self.assertEqual(patch["api"]["oauth2PermissionScopes"][0]["id"], SCOPE_ID)
        self.assertTrue(all(role["allowedMemberTypes"] == ["User"] for role in patch["appRoles"]))
        app.update(copy.deepcopy(patch))
        self.assertEqual(owned_patch(app), patch)
        app["appRoles"][0]["value"] = "unrelated-role"
        with self.assertRaises(IdentityError):
            owned_patch(app)

    def test_admin_assignments_must_match_operator_and_resource_not_just_role_id(self):
        requests = assignment_requests("resource", "operator")
        assignments = [request["body"] for request in requests["requests"]]
        self.assertEqual(
            assigned_roles(assignments, "resource", "operator"), {"merchant", "approver"}
        )
        for key in ("principalId", "resourceId"):
            altered = copy.deepcopy(assignments)
            for item in altered:
                item[key] = "someone-else"
            self.assertEqual(assigned_roles(altered, "resource", "operator"), set())
        self.assertEqual(assigned_roles([], "resource", "operator"), set())
        self.assertEqual({role["value"] for role in expected_roles()}, {"merchant", "approver"})

    def test_graph_pagination_is_bounded_even_when_pages_have_no_rows(self):
        class Repeated:
            def request(self, path):
                return {"value": [], "@odata.nextLink": "https://graph.microsoft.com/v1.0/loop"}

        with self.assertRaises(IdentityError):
            all_values(Repeated(), "/loop")

    def test_unowned_registration_cannot_be_patched(self):
        class Unowned:
            def __init__(self):
                self.writes = []

            def request(self, path, body=None):
                if body is not None:
                    self.writes.append(body)
                    raise AssertionError("Unexpected write")
                if path.startswith("/applications?"):
                    return {
                        "value": [
                            {
                                "id": "app-object",
                                "appId": config()["oidcClientId"],
                                "signInAudience": "AzureADMyOrg",
                            }
                        ]
                    }
                if path == "/me?$select=id":
                    return {"id": config()["operatorObjectId"]}
                return {"value": []}

        graph = Unowned()
        with self.assertRaises(IdentityError):
            inspect(graph, config(), configure_owned=True)
        self.assertEqual(graph.writes, [])

    def test_api_configuration_does_not_substitute_for_administrator_assignments(self):
        app = {
            "id": "app-object",
            "appId": config()["oidcClientId"],
            "signInAudience": "AzureADMyOrg",
        }
        app.update(owned_patch(app))

        class Configured:
            def request(self, path):
                if path.startswith("/applications?"):
                    return {"value": [app]}
                if path.startswith("/servicePrincipals?"):
                    return {
                        "value": [
                            {
                                "id": "resource",
                                "appOwnerOrganizationId": config()["tenantId"],
                            }
                        ]
                    }
                return {"value": []}

        result = inspect(Configured(), config())
        self.assertTrue(result["configured"])
        self.assertEqual(result["assignedRoles"], [])
        self.assertFalse(result["ready"])

    def test_existing_dual_ownership_can_complete_only_the_demo_user_roles(self):
        graph = OwnedIdentityGraph()
        result = complete_owned_setup(graph, config())
        self.assertTrue(result["ready"])
        self.assertEqual(len(graph.writes), 4)
        self.assertEqual(graph.writes[0][0:2], ("PATCH", "/applications/app-object"))
        self.assertNotIn("preAuthorizedApplications", graph.writes[0][2]["api"])
        self.assertIn("preAuthorizedApplications", graph.writes[1][2]["api"])
        for method, path, body in graph.writes[2:]:
            self.assertEqual(
                (method, path), ("POST", "/servicePrincipals/resource/appRoleAssignedTo")
            )
            self.assertEqual(body["principalId"], config()["operatorObjectId"])
            self.assertEqual(body["resourceId"], "resource")
            self.assertEqual(set(body), {"principalId", "resourceId", "appRoleId"})
        self.assertTrue(complete_owned_setup(graph, config())["ready"])
        self.assertEqual(len(graph.writes), 4)

    def test_owned_setup_refuses_missing_ownership_wrong_operator_or_wrong_tenant(self):
        for attribute, value in (
            ("app_owned", False),
            ("principal_owned", False),
            ("operator", "someone-else"),
            ("tenant", "another-tenant"),
        ):
            with self.subTest(attribute=attribute):
                graph = OwnedIdentityGraph()
                setattr(graph, attribute, value)
                with self.assertRaises(IdentityError):
                    complete_owned_setup(graph, config())
                self.assertEqual(graph.writes, [])

    def test_owned_setup_only_assigns_the_missing_role(self):
        graph = OwnedIdentityGraph()
        graph.app.update(owned_patch(graph.app))
        graph.assignments = [
            assignment_requests("resource", config()["operatorObjectId"])["requests"][0]["body"]
        ]
        self.assertTrue(complete_owned_setup(graph, config())["ready"])
        self.assertEqual(len(graph.writes), 1)
        self.assertEqual(graph.writes[0][2]["appRoleId"], expected_roles()[1]["id"])

    def test_owned_setup_never_assigns_roles_before_definitions_propagate(self):
        graph = OwnedIdentityGraph()
        graph.propagated = False
        with self.assertRaises(IdentityError):
            complete_owned_setup(graph, config())
        self.assertEqual([method for method, _, _ in graph.writes], ["PATCH", "PATCH"])

    def test_denied_owned_assignment_never_escalates_or_retries(self):
        class Denied(OwnedIdentityGraph):
            def request(self, path, body=None, *, method=None):
                if method == "POST":
                    self.writes.append((method, path, body))
                    raise IdentityError("Microsoft Graph request failed with HTTP 403")
                return super().request(path, body, method=method)

        graph = Denied()
        with self.assertRaisesRegex(IdentityError, "403"):
            complete_owned_setup(graph, config())
        self.assertEqual([method for method, _, _ in graph.writes], ["PATCH", "PATCH", "POST"])

    def test_unexpected_assignment_result_stops_before_the_next_role(self):
        class Unexpected(OwnedIdentityGraph):
            def request(self, path, body=None, *, method=None):
                result = super().request(path, body, method=method)
                return {"principalId": "someone-else"} if method == "POST" else result

        graph = Unexpected()
        with self.assertRaisesRegex(IdentityError, "Unexpected owned role-assignment"):
            complete_owned_setup(graph, config())
        self.assertEqual([method for method, _, _ in graph.writes], ["PATCH", "PATCH", "POST"])

    def test_scope_is_verified_before_client_preauthorization(self):
        class NoScopePersistence(OwnedIdentityGraph):
            def request(self, path, body=None, *, method=None):
                result = super().request(path, body, method=method)
                if body is not None and method != "POST":
                    self.app["api"]["oauth2PermissionScopes"] = []
                return result

        graph = NoScopePersistence()
        with self.assertRaisesRegex(IdentityError, "API scope did not persist"):
            complete_owned_setup(graph, config())
        self.assertEqual([method for method, _, _ in graph.writes], ["PATCH"])


if __name__ == "__main__":
    unittest.main()
