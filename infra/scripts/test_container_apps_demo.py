import copy
import unittest

from container_apps_demo import images_from_manifest, parameters, validate
from demo_identity import (
    SCOPE_ID,
    IdentityError,
    all_values,
    assigned_roles,
    assignment_requests,
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


class DemoTests(unittest.TestCase):
    def test_initial_parameters_never_publish_the_gateway_or_enable_a_model(self):
        foundation = parameters(config(), "foundation")["parameters"]
        self.assertIs(foundation["enableModel"]["value"], False)
        self.assertIs(foundation["enableApiAccess"]["value"], False)
        self.assertIs(foundation["secretsReady"]["value"], False)
        apps = parameters(config(), "apps", manifest())["parameters"]
        self.assertIs(apps["enablePublicIngress"]["value"], False)
        self.assertTrue(apps["providerId"]["value"].startswith("providers-v1:"))
        self.assertEqual(len(apps["images"]["value"]), 4)

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


if __name__ == "__main__":
    unittest.main()
