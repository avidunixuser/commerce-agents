from __future__ import annotations

import argparse
import copy
import dataclasses
import json
import os
import pathlib
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import private_build as build
import validate
import yaml
from private_build_github import check_claims, setup


def fixture() -> build.Config:
    return build.Config(
        subscription_id="11111111-1111-4111-8111-111111111111",
        tenant_id="22222222-2222-4222-8222-222222222222",
        location="swedencentral",
        build_resource_group="rg-commerce-build",
        foundation_resource_group="rg-commerce-aca",
        name="commerce-build",
        vnet_name="commerce-vnet",
        firewall_policy_name="commerce-egress",
        registry_name="commercebuildtest",
        repository="test-owner/commerce",
        oidc_subject_prefix="repo:test-owner@123/commerce@456",
        image_version="24.04.202610210",
        dns_proxy_address="10.40.0.4",
        private_endpoint_cidr="10.40.5.0/24",
        controller_client_id="33333333-3333-4333-8333-333333333333",
        builder_client_id="44444444-4444-4444-8444-444444444444",
    )


class PrivateBuildTests(unittest.TestCase):
    def setUp(self):
        self.config = fixture()
        self.state = {
            "run_id": "manual-test",
            "generation": "a" * 32,
            "sha": "b" * 40,
            "name": "cb-manual-test",
            "group_id": self.config.group_id,
        }

    def resources(self):
        tags = build.run_tags("manual-test", "a" * 32, "b" * 40)
        return [
            {"id": f"{self.config.group_id}/providers/{provider}/{kind}/{name}", "tags": dict(tags)}
            for provider, kind, name in (
                ("Microsoft.Compute", "virtualMachines", "cb-manual-test"),
                ("Microsoft.Network", "networkInterfaces", "cb-manual-test-nic"),
            )
        ]

    def test_invalid_configuration_is_rejected(self):
        for field, value in (
            ("subscription_id", "00000000-0000-0000-0000-000000000000"),
            ("tenant_id", "not-a-uuid"),
            ("registry_name", "evil.example/registry"),
            ("repository", "owner/../../repo"),
            ("image_version", "latest"),
            ("oidc_subject_prefix", "repo:test-owner/commerce"),
            ("oidc_subject_prefix", "repo:other@123/commerce@456"),
            ("dns_proxy_address", "8.8.8.8"),
            ("dns_proxy_address", "127.0.0.1"),
            ("private_endpoint_cidr", "0.0.0.0/0"),
            ("private_endpoint_cidr", "127.0.0.0/24"),
            ("private_endpoint_cidr", "0.0.0.0/24"),
            ("private_endpoint_cidr", "10.0.0.0/8"),
            ("build_resource_group", self.config.foundation_resource_group),
        ):
            with self.subTest(field=field, value=value), self.assertRaises((ValueError, TypeError)):
                dataclasses.replace(self.config, **{field: value})

    def test_parameters_are_bootstrap_only(self):
        parameters = build.bootstrap_parameters(self.config)["parameters"]
        self.assertNotIn("enableWorkloads", parameters)
        self.assertNotIn("enableIngress", parameters)
        self.assertNotIn("sshPublicKey", parameters)
        self.assertEqual(parameters["oidcSubjectPrefix"]["value"], self.config.oidc_subject_prefix)

    def test_only_exact_protected_workflow_source_is_accepted(self):
        sha = "b" * 40
        environment = {
            "GITHUB_ACTIONS": "true",
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_REPOSITORY": self.config.repository,
            "GITHUB_WORKFLOW_REF": f"{self.config.repository}/.github/workflows/private-build.yml@refs/heads/main",
            "GITHUB_SHA": sha,
            "RUNNER_ENVIRONMENT": "github-hosted",
        }
        with patch.dict(os.environ, environment, clear=True):
            build.workflow_source(self.config, sha)
        for key in environment:
            with (
                self.subTest(key=key),
                patch.dict(os.environ, {**environment, key: "untrusted"}, clear=True),
                self.assertRaises(ValueError),
            ):
                build.workflow_source(self.config, sha)
        with patch.dict(os.environ, environment, clear=True), self.assertRaises(ValueError):
            build.workflow_source(self.config, sha, operator=True)

    def test_claims_require_exact_subject_issuer_and_audience(self):
        claims = {
            "iss": "https://token.actions.githubusercontent.com",
            "aud": "api://AzureADTokenExchange",
            "sub": self.config.subject,
        }
        check_claims(self.config, claims)
        for key in claims:
            with self.subTest(key=key), self.assertRaises(ValueError):
                check_claims(self.config, {**claims, key: "untrusted"})

    def test_policy_mutation_requires_explicit_approval(self):
        with patch("private_build_github.api") as api, self.assertRaises(ValueError):
            setup(self.config, False)
        api.assert_not_called()

    def test_cleanup_deletes_only_exact_owned_vm_then_nic(self):
        resources = self.resources()
        resources.append({"id": self.config.builder_id, "tags": {}})
        deleted = []

        def fake_az(config, *args):
            self.assertEqual(args[:2], ("resource", "delete"))
            deleted.append(args[3])
            resources[:] = [r for r in resources if r["id"] != args[3]]

        with (
            patch.object(build, "group_resources", side_effect=lambda _: list(resources)),
            patch.object(build, "az", side_effect=fake_az),
        ):
            build.cleanup(self.config, self.state)
        self.assertEqual(len(deleted), 2)
        self.assertIn("/virtualMachines/", deleted[0])
        self.assertIn("/networkInterfaces/", deleted[1])
        self.assertEqual([r["id"] for r in resources], [self.config.builder_id])

    def test_cleanup_refuses_foreign_tags_scope_and_name(self):
        for mutation in ("generation", "group", "name"):
            resources = self.resources()
            state = copy.deepcopy(self.state)
            if mutation == "generation":
                resources[0]["tags"]["commerceGeneration"] = "c" * 32
            elif mutation == "group":
                state["group_id"] = "/subscriptions/other/resourceGroups/other"
            else:
                state["name"] = "shared-resource"
            with (
                self.subTest(mutation=mutation),
                patch.object(build, "group_resources", return_value=resources),
            ):
                with patch.object(build, "az") as az, self.assertRaises(ValueError):
                    build.cleanup(self.config, state)
                az.assert_not_called()

    def test_cleanup_is_idempotent_when_resources_are_absent(self):
        with (
            patch.object(build, "group_resources", return_value=[]),
            patch.object(build, "az") as az,
        ):
            build.cleanup(self.config, self.state)
        az.assert_not_called()

    def test_missing_deployment_is_not_success(self):
        with patch.object(build, "az", return_value=[]):
            with self.assertRaises(ValueError):
                build.settle_deployment(self.config, "cb-test")
            build.settle_deployment(self.config, "cb-test", cancel=True)

    def test_azure_wrong_tenant_or_subscription_is_rejected(self):
        for key in ("id", "tenantId", "state"):
            account = {
                "id": self.config.subscription_id,
                "tenantId": self.config.tenant_id,
                "state": "Enabled",
            }
            account[key] = "unexpected"
            with patch.object(build, "az", return_value=account), self.assertRaises(ValueError):
                build.check_context(self.config)

    def test_build_failure_still_settles_then_cleans(self):
        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(
                approve_disposable_cleanup=True,
                source_sha="b" * 40,
                operator=True,
                run_id="manual-test",
                state_file=pathlib.Path(directory) / "state.json",
                images="smoke",
            )
            events = []

            def fake_command(*arguments):
                if arguments[0] == "ssh-keygen":
                    pathlib.Path(arguments[-1] + ".pub").write_text("ssh-rsa test")

            with (
                patch.object(build, "workflow_source"),
                patch.object(build, "check_context"),
                patch.object(build, "group_resources", return_value=[]),
                patch.object(build, "command", side_effect=fake_command),
                patch.object(build, "preview_vm"),
                patch.object(build, "az"),
                patch.object(build, "remote_build", side_effect=RuntimeError("build failed")),
                patch.object(
                    build,
                    "settle_deployment",
                    side_effect=lambda *a, **k: events.append(("settle", k.get("cancel", False))),
                ),
                patch.object(
                    build, "cleanup", side_effect=lambda *a: events.append(("cleanup", True))
                ),
                self.assertRaisesRegex(RuntimeError, "build failed"),
            ):
                build.execute(self.config, args)
            self.assertEqual(events, [("settle", False), ("settle", True), ("cleanup", True)])
            self.assertEqual(
                json.loads(args.state_file.read_text())["group_id"], self.config.group_id
            )

    def test_remote_failure_cancel_and_incomplete_manifest_are_not_success(self):
        cases = [
            {"executionState": "Failed", "exitCode": 1, "error": "dependency denied"},
            {"executionState": "Canceled", "exitCode": 0},
            {"executionState": "Succeeded", "exitCode": 1},
            {"executionState": "Succeeded", "exitCode": 0, "output": "partial output"},
            {
                "executionState": "Succeeded",
                "exitCode": 0,
                "output": 'COMMERCE_BUILD_RESULT={"api":"sha256:' + "a" * 64 + '"}',
            },
        ]
        for view in cases:
            with (
                self.subTest(view=view),
                tempfile.TemporaryDirectory() as directory,
                patch.object(
                    build, "az", side_effect=[None, {"properties": {"instanceView": view}}]
                ),
                self.assertRaises((ValueError, RuntimeError)),
            ):
                build.remote_build(self.config, self.state, "smoke", pathlib.Path(directory))

    def test_remote_success_requires_exact_digest_manifest(self):
        output = "COMMERCE_BUILD_RESULT=" + json.dumps({"gateway": "sha256:" + "a" * 64})
        view = {"executionState": "Succeeded", "exitCode": 0, "output": output}
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(build, "az", side_effect=[None, {"properties": {"instanceView": view}}]),
        ):
            self.assertEqual(
                build.remote_build(self.config, self.state, "smoke", pathlib.Path(directory)),
                output,
            )

    def test_retail_storefront_requires_only_its_exact_digest(self):
        for manifest in (
            {"retail-storefront-web": "sha256:" + "a" * 64},
            {"gateway": "sha256:" + "a" * 64},
            {"retail-storefront-web": "sha256:" + "a" * 64, "api": "sha256:" + "b" * 64},
        ):
            output = "COMMERCE_BUILD_RESULT=" + json.dumps(manifest)
            view = {"executionState": "Succeeded", "exitCode": 0, "output": output}
            with (
                self.subTest(manifest=manifest),
                tempfile.TemporaryDirectory() as directory,
                patch.object(
                    build, "az", side_effect=[None, {"properties": {"instanceView": view}}]
                ),
            ):
                if set(manifest) == {"retail-storefront-web"}:
                    self.assertEqual(
                        build.remote_build(
                            self.config, self.state, "retail-storefront", pathlib.Path(directory)
                        ),
                        output,
                    )
                else:
                    with self.assertRaisesRegex(ValueError, "manifest mismatch"):
                        build.remote_build(
                            self.config, self.state, "retail-storefront", pathlib.Path(directory)
                        )

    @unittest.skipIf(os.name == "nt", "Build selection executes under the Linux runner's Bash")
    def test_build_selection_does_not_build_unrequested_images(self):
        source = (build.ROOT / "infra" / "scripts" / "private_build.sh").read_text()
        selection = source[
            source.index('if [ "$MODE" = retail-storefront ]') : source.index(
                'docker logout "$REGISTRY"'
            )
        ]
        all_images = [
            "gateway",
            "api",
            *(
                f"{vertical}-{app}"
                for vertical in ("retail", "travel", "telecom", "entertainment")
                for app in ("storefront-web", "merchant-web")
            ),
        ]
        for mode, expected in (
            ("smoke", ["gateway"]),
            ("retail-storefront", ["retail-storefront-web"]),
            ("all", all_images),
        ):
            result = subprocess.run(
                ["bash", "-eu", "-c", 'build_image() { echo "$1"; }\n' + selection],
                env={**os.environ, "MODE": mode},
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertEqual(result.stdout.splitlines(), expected)

    def test_remote_entrypoint_preserves_bash_and_explicit_query_arguments(self):
        manifest = "COMMERCE_BUILD_RESULT=" + json.dumps({"gateway": "sha256:" + "a" * 64})

        def fake_az(config, *args):
            if "put" in args:
                body = pathlib.Path(args[args.index("--body") + 1][1:])
                script = json.loads(body.read_text())["properties"]["source"]["script"]
                self.assertTrue(
                    script.startswith("#!/usr/bin/env bash\nexport COMMERCE_BUILD_CONFIG=")
                )
                return None
            self.assertIn("--url-parameters", args)
            self.assertNotIn("&", args[args.index("--url") + 1])
            return {
                "properties": {
                    "instanceView": {
                        "executionState": "Succeeded",
                        "exitCode": 0,
                        "output": manifest,
                    }
                }
            }

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(build, "az", side_effect=fake_az),
        ):
            build.remote_build(self.config, self.state, "smoke", pathlib.Path(directory))

    @unittest.skipIf(os.name == "nt", "APT sandbox permissions require a POSIX filesystem")
    def test_public_apt_files_are_readable_under_restrictive_umask(self):
        source = (build.ROOT / "infra" / "scripts" / "private_build.sh").read_text()
        start = source.index(
            "curl --fail --silent --show-error https://packages.microsoft.com/keys/"
        )
        end = source.index("apt-get ", start)
        bootstrap = (
            source[start:end]
            .replace("/usr/share/keyrings/commerce-microsoft.gpg", "key.gpg")
            .replace("/etc/apt/sources.list.d/commerce-azure-cli.list", "source.list")
        )
        script = (
            "umask 077\n"
            "curl() { printf '%s' public-key; }\n"
            "gpg() { cat > key.gpg; }\n" + bootstrap + "printf '%s' private-config > private.env\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(
                ["bash", "-eu", "-o", "pipefail", "-c", script],
                cwd=directory,
                check=True,
                capture_output=True,
                text=True,
            )
            for name in ("key.gpg", "source.list"):
                self.assertEqual(
                    stat.S_IMODE((pathlib.Path(directory) / name).stat().st_mode), 0o644
                )
            self.assertEqual(
                stat.S_IMODE((pathlib.Path(directory) / "private.env").stat().st_mode), 0o600
            )
            self.assertIn(
                "signed-by=key.gpg", (pathlib.Path(directory) / "source.list").read_text()
            )

    def test_active_foreign_deployment_cannot_be_canceled(self):
        with (
            patch.object(build, "az", side_effect=[["Running"], {}]) as az,
            self.assertRaisesRegex(ValueError, "unrelated deployment"),
        ):
            build.settle_deployment(self.config, self.state["name"], cancel=True, state=self.state)
        self.assertFalse(any("cancel" in call.args for call in az.call_args_list))

    def test_no_lifecycle_mutation_without_approval(self):
        with patch.object(build, "az") as az, self.assertRaises(ValueError):
            build.execute(self.config, argparse.Namespace(approve_disposable_cleanup=False))
        az.assert_not_called()

    def test_vm_preview_rejects_modifications_deletes_and_unrelated_resources(self):
        validation = {"properties": {"provisioningState": "Succeeded"}}
        changes = [{"changeType": "Create", "resourceId": r["id"]} for r in self.resources()]
        for mutation in ("none", "modify", "delete", "foreign", "unsupported"):
            altered = copy.deepcopy(changes)
            if mutation in ("modify", "delete", "unsupported"):
                altered[0]["changeType"] = mutation.capitalize()
            elif mutation == "foreign":
                altered[0]["resourceId"] = self.config.builder_id
            preview = {"status": "Succeeded", "changes": altered}
            with (
                self.subTest(mutation=mutation),
                patch.object(build, "az", side_effect=[validation, preview]),
            ):
                if mutation == "none":
                    build.preview_vm(
                        self.config,
                        self.state,
                        pathlib.Path("vm.json"),
                        pathlib.Path("params.json"),
                    )
                else:
                    with self.assertRaisesRegex(ValueError, "two exact"):
                        build.preview_vm(
                            self.config,
                            self.state,
                            pathlib.Path("vm.json"),
                            pathlib.Path("params.json"),
                        )

    def test_vm_preview_accepts_ignored_bootstrap_resources(self):
        controller_id = (
            f"{self.config.group_id}/providers/Microsoft.ManagedIdentity/"
            f"userAssignedIdentities/{self.config.name}-controller"
        )
        changes = [{"changeType": "Create", "resourceId": r["id"]} for r in self.resources()]
        changes.extend(
            {"changeType": "Ignore", "resourceId": resource_id.upper()}
            for resource_id in (self.config.builder_id, controller_id, self.config.nsg_id)
        )
        with patch.object(
            build,
            "az",
            side_effect=[
                {"properties": {"provisioningState": "Succeeded"}},
                {
                    "status": "Succeeded",
                    "changes": changes,
                    "potentialChanges": [],
                    "diagnostics": None,
                },
            ],
        ):
            build.preview_vm(
                self.config, self.state, pathlib.Path("vm.json"), pathlib.Path("params.json")
            )

    def test_vm_preview_rejects_foreign_ignored_resources(self):
        for resource_id in (
            self.resources()[0]["id"],
            self.config.builder_id + "/federatedIdentityCredentials/unrelated",
            self.config.builder_id + "-foreign",
        ):
            changes = [{"changeType": "Create", "resourceId": r["id"]} for r in self.resources()]
            changes.append({"changeType": "Ignore", "resourceId": resource_id})
            with (
                self.subTest(resource_id=resource_id),
                patch.object(
                    build,
                    "az",
                    side_effect=[
                        {"properties": {"provisioningState": "Succeeded"}},
                        {"status": "Succeeded", "changes": changes},
                    ],
                ),
                self.assertRaisesRegex(ValueError, "known bootstrap"),
            ):
                build.preview_vm(
                    self.config, self.state, pathlib.Path("vm.json"), pathlib.Path("params.json")
                )

    def test_vm_preview_rejects_changes_to_bootstrap_resources(self):
        for change_type in ("Create", "Modify", "Delete", "Unsupported", "NoChange"):
            changes = [{"changeType": "Create", "resourceId": r["id"]} for r in self.resources()]
            changes.append({"changeType": change_type, "resourceId": self.config.builder_id})
            with (
                self.subTest(change_type=change_type),
                patch.object(
                    build,
                    "az",
                    side_effect=[
                        {"properties": {"provisioningState": "Succeeded"}},
                        {"status": "Succeeded", "changes": changes},
                    ],
                ),
                self.assertRaisesRegex(ValueError, "two exact"),
            ):
                build.preview_vm(
                    self.config, self.state, pathlib.Path("vm.json"), pathlib.Path("params.json")
                )

    def test_vm_preview_rejects_unresolved_potential_changes_and_diagnostics(self):
        changes = [{"changeType": "Create", "resourceId": r["id"]} for r in self.resources()]
        for field in ("potentialChanges", "diagnostics"):
            with (
                self.subTest(field=field),
                patch.object(
                    build,
                    "az",
                    side_effect=[
                        {"properties": {"provisioningState": "Succeeded"}},
                        {"status": "Succeeded", "changes": changes, field: [{"unresolved": True}]},
                    ],
                ),
                self.assertRaisesRegex(ValueError, "unresolved"),
            ):
                build.preview_vm(
                    self.config, self.state, pathlib.Path("vm.json"), pathlib.Path("params.json")
                )

    def test_hydration_binds_outputs_to_the_approved_bootstrap(self):
        outputs = {
            "controllerClientId": self.config.controller_client_id,
            "builderClientId": self.config.builder_client_id,
            "builderIdentityId": self.config.builder_id,
            "nsgId": self.config.nsg_id,
            "subnetId": self.config.subnet_id,
            "oidcSubject": self.config.subject,
        }
        deployment = {
            "properties": {
                "provisioningState": "Succeeded",
                "outputs": {key: {"value": value} for key, value in outputs.items()},
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory) / "config.json"
            target = pathlib.Path(directory) / "runtime.json"
            source.write_text(json.dumps(dataclasses.asdict(self.config)))
            arguments = [
                "private_build.py",
                "--config",
                str(source),
                "hydrate",
                "--deployment-name",
                "reviewed-bootstrap",
                "--output",
                str(target),
            ]
            with (
                patch("sys.argv", arguments),
                patch.object(build, "check_context"),
                patch.object(build, "az", return_value=deployment),
            ):
                build.main()
                self.assertEqual(build.Config(**json.loads(target.read_text())), self.config)
                with self.assertRaisesRegex(ValueError, "overwrite"):
                    build.main()

    def test_workflow_is_manual_hosted_protected_and_pinned(self):
        workflow = yaml.load(
            (build.ROOT / ".github" / "workflows" / "private-build.yml").read_text(),
            Loader=yaml.BaseLoader,
        )
        self.assertEqual(set(workflow["on"]), {"workflow_dispatch"})
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        jobs = list(workflow["jobs"].values())
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["permissions"], {"contents": "read", "id-token": "write"})
        self.assertEqual(jobs[0]["runs-on"], "ubuntu-latest")
        self.assertEqual(jobs[0]["environment"], "commerce-build")
        self.assertIn("refs/heads/main", jobs[0]["if"])
        self.assertEqual(workflow["defaults"]["run"]["shell"], "bash")
        self.assertEqual(workflow["concurrency"]["cancel-in-progress"], "false")
        for step in jobs[0]["steps"]:
            if "uses" in step:
                self.assertRegex(step["uses"], r"^[A-Za-z0-9_/-]+@[0-9a-f]{40}$")

    def test_compiled_policy_rejects_broader_vm_identity_or_persistent_disk(self):
        bootstrap_path = validate.OUT / "private-build-main.json"
        node_path = validate.OUT / "private-build-vm.json"
        if not bootstrap_path.exists() or not node_path.exists():
            self.skipTest("compile private-build templates with validate.py first")
        bootstrap = json.loads(bootstrap_path.read_text())
        node = json.loads(node_path.read_text())
        validate.check_private_build(bootstrap, node)
        for mutation in ("public-ip", "identity", "disk", "role"):
            altered = copy.deepcopy(node)
            control = copy.deepcopy(bootstrap)
            vm = next(
                r
                for r in validate.resources(altered)
                if r["type"] == "Microsoft.Compute/virtualMachines"
            )
            nic = next(
                r
                for r in validate.resources(altered)
                if r["type"] == "Microsoft.Network/networkInterfaces"
            )
            if mutation == "public-ip":
                nic["properties"]["ipConfigurations"][0]["properties"]["publicIPAddress"] = {
                    "id": "public"
                }
            elif mutation == "identity":
                vm["identity"]["type"] = "SystemAssigned, UserAssigned"
            elif mutation == "disk":
                vm["properties"]["storageProfile"]["osDisk"]["diffDiskSettings"] = {}
            else:
                role = next(
                    r
                    for r in validate.resources(control)
                    if r["type"] == "Microsoft.Authorization/roleDefinitions"
                )
                role["properties"]["permissions"][0]["actions"].append(
                    "Microsoft.Authorization/roleAssignments/write"
                )
            with self.subTest(mutation=mutation), self.assertRaises(AssertionError):
                validate.check_private_build(control, altered)


if __name__ == "__main__":
    unittest.main()
