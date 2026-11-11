# Retail demo identity

The Container Apps retail demo uses a single-tenant Microsoft Entra application.
The shopper entry additionally restricts platform authentication to the approved
operator. Merchant access remains private and requires the `merchant` app role;
applying a staged change separately requires `approver`. Sample-data mode does
not disable these checks.

The [gateway contract](gateway-auth.md) defines token validation and browser
credential handling. The
[app-role manifest](../infra/container-apps/demo-app-roles.json) defines the two
user roles. It grants no Microsoft Graph application permissions, directory
administrator role or Azure resource access.

## Administrator setup

An operator who owns both the registration and enterprise application can use
the explicit owned-setup path. Otherwise an application administrator performs
the directory assignments from their own authenticated administrative
environment. Do not change another operator's shared Azure CLI profile or send
administrator credentials to the application.
The configured deployment tenant and operator object ID are private deployment
inputs. The local operator ID is retained in the ignored
`.azure/demo-operator-object-id.json`, not in this document.

1. Use the supplied isolated demo registration in the confirmed deployment
   tenant, with accounts in this organizational directory only. If none has
   been supplied, register `Zava Retail Demo`. Confirm the approved operator
   owns it. Do not reuse a production application's credentials or permissions.
2. In the application manifest, set `api.requestedAccessTokenVersion` to `2`.
   Set the application ID URI to `api://<application-client-id>`. Expose the
   delegated API scope `access_as_user` for the demo API. Do not add Microsoft
   Graph permissions as a substitute for this API scope. Prefer the
   owned-registration helper in the [retail runbook](azure-retail-demo.md),
   which supplies the exact scope ID and self-client preauthorization. Manual
   setup must preserve that generated scope ID rather than create a different
   scope with the same name.
3. Configure the `appRoles` collection from the supplied app-role manifest.
   Keep both roles enabled with allowed member type `User` and the exact
   `merchant` and `approver` values.
4. In the corresponding enterprise application, assign the approved operator
   both roles. These are app-specific user assignments, not directory roles.
   Do not assign arbitrary users or groups and do not change production apps.
5. Confirm the operator can manage the demo registration's eventual exact HTTPS
   redirect URLs. The deployment supplies `/auth/callback` on each gateway
   origin and `/.auth/login/aad/callback` on the public shopper origin after
   Azure assigns its hostname. Do not register invented hostnames, wildcard
   redirects or HTTP callbacks to bypass that dependency.
6. Return only the application client ID and confirmation of the assignments
   through the deployment operator's private channel. The rollout verifies
   the tenant, scope, role definitions and actual operator assignments before
   billable application/model deployment.

The requested OIDC scope is
`openid profile api://<application-client-id>/access_as_user`. Configure the
v2 API access-token audience as the application client ID. A Graph access token
does not authorize this API. If tenant consent policy requires administrator
consent, the administrator handles that consent explicitly; do not broaden the
application's requested permissions.

Do not generate or share a client secret for this handoff. Credential creation
and transfer to private Key Vault use the separately approved secure setup
stage. Do not paste secrets into chat, commit them, use Docker build arguments
or return them in browser configuration.

## Assignment verification

Registration policy or Global Reader alone does not grant permission to assign
merchant roles. Existing enterprise-application ownership also contributes to
effective permissions: an owner can manage assignments on that application,
subject to the calling client's Graph permissions and tenant restrictions. See
[enterprise application ownership](https://learn.microsoft.com/en-us/entra/identity/enterprise-apps/overview-assign-app-owners).
The Microsoft Graph
[app-role assignment API](https://learn.microsoft.com/en-us/graph/api/serviceprincipal-post-approleassignedto?view=graph-rest-1.0)
requires its delegated permissions and a supported directory role or custom
permission. Do not infer either sufficient access or an administrator blocker
from the directory-role name alone.

The explicitly authorized `--complete-owned-setup` option checks both ownerships,
the exact operator and tenant, and the propagated user-role definitions before
assigning only missing demo roles. It never grants directory privileges or
Microsoft Graph permissions. A denied API operation remains a real administrator
gate; do not broaden permissions or switch callers to bypass it.

Before deploying, read the enterprise application's assignments and verify that
the approved operator's principal ID has both role IDs from the manifest on
this exact resource service principal. A role definition alone is not an
assignment. Retain the verification evidence privately. If configuration,
consent or assignments are missing, stop; never mint a replacement merchant
token or weaken `commerce_host` authorization.
