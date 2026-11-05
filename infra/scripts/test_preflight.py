import copy
import json
import pathlib
import unittest

from preflight import GATEWAY_REQUIRED, GATEWAY_SECRETS, HOST_REQUIRED, validate


def fixture():
    params = {
        "modelAvailabilityVerified": True,
        "existingOpenAIName": "existing-private-account",
        "storefrontHost": "shop.example.test",
        "merchantHost": "merchant.example.test",
        "privateAccessCidrs": ["10.80.0.0/16"],
        "images": {
            key: f"validation.azurecr.io/{key.lower()}@sha256:{'a' * 64}"
            for key in ("runtime", "gateway", "shoppingWeb", "merchantWeb")
        },
        "runtimeEnvironment": {},
        "secretReferences": {},
    }
    for role in ("shopping", "merchant"):
        for mode in ("api", "worker", "gateway"):
            key = f"{role}-{mode}"
            required = GATEWAY_REQUIRED if mode == "gateway" else HOST_REQUIRED
            params["runtimeEnvironment"][key] = dict.fromkeys(required, "configured")
            if role == "shopping" and mode in ("api", "worker"):
                params["runtimeEnvironment"][key].update(
                    COMMERCE_GUEST_ISSUER="https://shop.example.test",
                    COMMERCE_GUEST_JWKS_URL="https://shop.example.test/.well-known/jwks.json",
                )
            if mode == "gateway":
                if role == "shopping":
                    params["runtimeEnvironment"][key]["COMMERCE_GATEWAY_ISSUER"] = (
                        "https://shop.example.test"
                    )
                params["secretReferences"][key] = [
                    {
                        "name": env.lower().replace("_", "-"),
                        "envName": env,
                        "keyVaultUrl": "https://private-vault.vault.azure.net/secrets/reference",
                    }
                    for env in GATEWAY_SECRETS
                ]
    return params


class PreflightTests(unittest.TestCase):
    def test_retained_subnet_nsgs(self):
        params = fixture()
        nsg_id = (
            "/subscriptions/00000000-0000-0000-0000-000000000000"
            "/resourceGroups/validation/providers/Microsoft.Network/networkSecurityGroups/nsg"
        )
        params["existingSubnetNsgIds"] = dict.fromkeys(("workload", "endpoints", "runners"), nsg_id)
        validate(params, "container-apps")
        for value in (
            None,
            [],
            {"gateway": nsg_id},
            {"workload": ""},
            {"endpoints": nsg_id.replace("networkSecurityGroups", "routeTables")},
            {"runners": 123},
        ):
            with self.subTest(value=value):
                params["existingSubnetNsgIds"] = value
                with self.assertRaises(ValueError):
                    validate(params, "container-apps")

    def test_aca_examples_include_shopping_guest_settings(self):
        path = (
            pathlib.Path(__file__).resolve().parents[1]
            / "container-apps"
            / "main.parameters.example.json"
        )
        environment = json.loads(path.read_text())["parameters"]["runtimeEnvironment"]["value"]
        gateway_issuer = environment["shopping-gateway"]["COMMERCE_GATEWAY_ISSUER"]
        for name in ("shopping-api", "shopping-worker"):
            self.assertEqual(environment[name]["COMMERCE_GUEST_ISSUER"], gateway_issuer)
            self.assertTrue(
                environment[name]["COMMERCE_GUEST_JWKS_URL"].endswith("/.well-known/jwks.json")
            )

    def test_configured_private_target(self):
        validate(fixture(), "container-apps")

    def test_invalid_inputs(self):
        for key, value in (
            ("modelAvailabilityVerified", False),
            ("existingOpenAIName", ""),
            ("createOpenAI", True),
            ("privateAccessCidrs", ["0.0.0.0/0"]),
            ("privateAccessCidrs", ["8.8.8.0/24"]),
            ("merchantHost", "shop.example.test"),
            ("storefrontHost", "https://shop.example.test"),
            ("enableIngress", True),
        ):
            with self.subTest(key=key, value=value):
                params = fixture()
                params[key] = value
                with self.assertRaises(ValueError):
                    validate(params, "container-apps")

    def test_mutable_image_and_secret_literal_rejected(self):
        params = fixture()
        params["images"]["runtime"] = "validation.azurecr.io/runtime:latest"
        with self.assertRaises(ValueError):
            validate(params, "container-apps")
        params = fixture()
        params["secretReferences"]["shopping-gateway"][0]["keyVaultUrl"] = "literal-secret"
        with self.assertRaises(ValueError):
            validate(params, "container-apps")

    def test_enforced_boundary_cannot_be_overridden(self):
        for key in (
            "COMMERCE_ROLE",
            "COMMERCE_PRIVATE_BOUNDARY",
            "COMMERCE_MODE",
            "COMMERCE_API_UPSTREAM",
        ):
            params = fixture()
            params["runtimeEnvironment"]["shopping-gateway"][key] = "override"
            with self.assertRaises(ValueError):
                validate(params, "container-apps")

    def test_missing_auth_and_secret_rejected(self):
        for category, name in (
            ("runtimeEnvironment", "merchant-api"),
            ("secretReferences", "merchant-gateway"),
        ):
            params = fixture()
            del params[category][name]
            with self.assertRaises(ValueError):
                validate(params, "container-apps")

    def test_new_claude_requires_attestation(self):
        params = fixture()
        params["createClaude"] = True
        params["claudeDeployments"] = [{"name": "configured"}]
        with self.assertRaises(ValueError):
            validate(params, "container-apps")
        params["claudeAttestation"] = {
            "organizationName": "Operator supplied",
            "countryCode": "US",
            "industry": "retail",
        }
        validate(params, "container-apps")

    def test_aks_needs_version_and_administrators(self):
        params = copy.deepcopy(fixture())
        with self.assertRaises(ValueError):
            validate(params, "aks")
        params.update(kubernetesVersion="1.32", adminGroupObjectIds=["configured"])
        validate(params, "aks")

    def test_aks_secret_grants_are_gateway_only_and_name_scoped(self):
        params = fixture()
        params.update(kubernetesVersion="1.32", adminGroupObjectIds=["configured"])
        params["gatewaySecretNames"] = {"shopping-gateway": ["shopping-signing-key"]}
        validate(params, "aks")
        for grants in (
            {"shopping-api": ["signing-key"]},
            {"shopping-gateway": "signing-key"},
            {"shopping-gateway": ["https://vault.vault.azure.net/secrets/signing-key"]},
        ):
            params["gatewaySecretNames"] = grants
            with self.subTest(grants=grants), self.assertRaises(ValueError):
                validate(params, "aks")

    def test_runtime_identity_cannot_request_ungranted_vault_secrets(self):
        params = fixture()
        params["secretReferences"]["shopping-api"] = params["secretReferences"]["shopping-gateway"]
        with self.assertRaises(ValueError):
            validate(params, "container-apps")

    def test_ingress_certificate_requires_versionless_secret_uri(self):
        params = fixture()
        params.update(enableIngress=True, enableWorkloads=True)
        params["certificateSecretUri"] = "https://private-vault.vault.azure.net/secrets/tls"
        validate(params, "container-apps")
        for uri in (
            "https://private-vault.vault.azure.net",
            "https://private-vault.vault.azure.net/secrets/tls/version",
        ):
            params["certificateSecretUri"] = uri
            with self.subTest(uri=uri), self.assertRaises(ValueError):
                validate(params, "container-apps")

    def test_guest_auth_settings_must_be_https_and_distinct(self):
        for key, value in (
            ("COMMERCE_GUEST_ISSUER", ""),
            ("COMMERCE_GUEST_JWKS_URL", "http://shop.example.test/.well-known/jwks.json"),
            ("COMMERCE_GUEST_ISSUER", "configured"),
            ("COMMERCE_GUEST_ISSUER", "https://user:password@shop.example.test"),
        ):
            params = fixture()
            params["runtimeEnvironment"]["shopping-api"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                validate(params, "container-apps")
        params = fixture()
        params["runtimeEnvironment"]["shopping-api"]["COMMERCE_AUTH_ISSUER"] = (
            "https://shop.example.test"
        )
        with self.assertRaises(ValueError):
            validate(params, "container-apps")

    def test_gateway_and_worker_auth_must_match_api(self):
        for workload, key in (
            ("shopping-worker", "COMMERCE_GUEST_ISSUER"),
            ("shopping-gateway", "COMMERCE_GATEWAY_ISSUER"),
            ("shopping-gateway", "COMMERCE_OIDC_API_AUDIENCE"),
            ("merchant-worker", "COMMERCE_AUTH_AUDIENCE"),
        ):
            params = fixture()
            params["runtimeEnvironment"][workload][key] = "https://mismatch.example.test"
            with self.subTest(workload=workload, key=key), self.assertRaises(ValueError):
                validate(params, "container-apps")


class FoundationPreflightTests(unittest.TestCase):
    def fixture(self):
        return {
            "modelAvailabilityVerified": True,
            "createOpenAI": True,
            "createClaude": True,
            "privateAccessCidrs": ["10.40.6.0/24"],
            "enableWorkloads": False,
            "enableIngress": False,
            "images": {},
            "runtimeEnvironment": {},
            "secretReferences": {},
            "storefrontHost": "",
            "merchantHost": "",
            "certificateSecretUri": "",
            "modelDeployments": [],
            "claudeDeployments": [],
            "claudeAttestation": {},
        }

    def test_empty_application_and_model_configuration_accepted_only_explicitly(self):
        params = self.fixture()
        validate(params, "container-apps", phase="foundation")
        validate(
            {"parameters": {key: {"value": value} for key, value in params.items()}},
            "container-apps",
            phase="foundation",
        )
        with self.assertRaises(ValueError):
            validate(params, "container-apps")

    def test_workloads_and_ingress_must_be_explicitly_disabled(self):
        for flag in ("enableWorkloads", "enableIngress"):
            for value in (True, None, 0, "false"):
                params = self.fixture()
                params[flag] = value
                with self.subTest(flag=flag, value=value), self.assertRaises(ValueError):
                    validate(params, "container-apps", phase="foundation")
            params = self.fixture()
            del params[flag]
            with self.assertRaises(ValueError):
                validate(params, "container-apps", phase="foundation")

    def test_application_ingress_model_and_marketplace_configuration_rejected(self):
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
        ):
            params = self.fixture()
            params[key] = "configured"
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate(params, "container-apps", phase="foundation")

    def test_private_boundary_and_model_account_checks_remain_required(self):
        for key, value in (
            ("privateAccessCidrs", []),
            ("privateAccessCidrs", ["0.0.0.0/0"]),
            ("privateAccessCidrs", ["8.8.8.0/24"]),
            ("modelAvailabilityVerified", False),
            ("runtimeContainer", "identity"),
            ("existingClaudeName", "duplicate-account"),
        ):
            params = self.fixture()
            params[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate(params, "container-apps", phase="foundation")
        params = self.fixture()
        params.update(createOpenAI=False, createClaude=False)
        with self.assertRaises(ValueError):
            validate(params, "container-apps", phase="foundation")

    def test_aks_and_unknown_phases_are_rejected(self):
        with self.assertRaises(ValueError):
            validate(self.fixture(), "aks", phase="foundation")
        with self.assertRaises(ValueError):
            validate(self.fixture(), "container-apps", phase="unknown")


if __name__ == "__main__":
    unittest.main()
