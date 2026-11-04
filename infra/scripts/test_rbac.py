import unittest

from validate import check_rbac


def assignment(role, scope):
    return {
        "type": "Microsoft.Authorization/roleAssignments",
        "scope": scope,
        "properties": {"roleDefinitionId": role},
    }


class StaticRbacTests(unittest.TestCase):
    def test_secret_scope_allows_rotation_without_vault_wide_access(self):
        check_rbac(
            {
                "resources": [
                    assignment(
                        "4633458b-17de-408a-b874-0445c86b69e6",
                        "[resourceId('Microsoft.KeyVault/vaults/secrets', 'vault', 'key')]",
                    )
                ]
            }
        )

    def test_vault_wide_secret_grant_rejected(self):
        with self.assertRaises(AssertionError):
            check_rbac(
                {
                    "resources": [
                        assignment(
                            "4633458b-17de-408a-b874-0445c86b69e6",
                            "[resourceId('Microsoft.KeyVault/vaults', 'vault')]",
                        )
                    ]
                }
            )

    def test_key_listing_and_generic_control_roles_rejected(self):
        for role in (
            "a97b65f3-24c7-4388-baec-2e87135dc908",
            "8e3af657-a8ff-443c-a75c-2fe8c4bcb635",
            "b24988ac-6180-42a0-ab88-20f7382dd24c",
        ):
            with self.subTest(role=role), self.assertRaises(AssertionError):
                check_rbac({"resources": [assignment(role, "[resourceGroup().id]")]})

    def test_pull_grant_cannot_be_resource_group_scoped(self):
        with self.assertRaises(AssertionError):
            check_rbac(
                {
                    "resources": [
                        assignment("7f951dda-4ed3-4680-a7ca-43fe172d538d", "[resourceGroup().id]")
                    ]
                }
            )

    def test_control_network_grant_cannot_cover_other_subnets(self):
        role = "4d97b98b-1d4f-4787-a291-c67834d212e7"
        check_rbac(
            {
                "resources": [
                    assignment(
                        role,
                        "[resourceId('Microsoft.Network/virtualNetworks/subnets', 'vnet', 'workload')]",
                    )
                ]
            }
        )
        with self.assertRaises(AssertionError):
            check_rbac(
                {
                    "resources": [
                        assignment(
                            role, "[resourceId('Microsoft.Network/virtualNetworks', 'vnet')]"
                        )
                    ]
                }
            )

    def test_custom_claude_role_has_no_control_plane_actions(self):
        resource = {
            "type": "Microsoft.Authorization/roleDefinitions",
            "properties": {
                "assignableScopes": ["[resourceGroup().id]"],
                "permissions": [
                    {
                        "actions": [],
                        "notActions": [],
                        "dataActions": ["Microsoft.CognitiveServices/*"],
                        "notDataActions": [],
                    }
                ],
            },
        }
        check_rbac({"resources": [resource]})
        resource["properties"]["permissions"][0]["actions"] = [
            "Microsoft.CognitiveServices/accounts/listkeys/action"
        ]
        with self.assertRaises(AssertionError):
            check_rbac({"resources": [resource]})

    def test_cosmos_grant_is_container_scoped_sql_data_role(self):
        resource = {
            "type": "Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments",
            "properties": {
                "scope": "[format('{0}/dbs/commerce/colls/runtime', parameters('cosmosId'))]",
                "roleDefinitionId": "[format('{0}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002', parameters('cosmosId'))]",
            },
        }
        check_rbac({"resources": [resource]})
        resource["properties"]["scope"] = "[parameters('cosmosId')]"
        with self.assertRaises(AssertionError):
            check_rbac({"resources": [resource]})


if __name__ == "__main__":
    unittest.main()
