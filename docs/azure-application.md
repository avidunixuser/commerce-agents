# Azure application

The shared Messages API runtime supports Foundry Claude and Azure OpenAI Responses.
The same API/gateway image artifacts are used for Container Apps and AKS; hosting
and model deployment choices are runtime configuration, not separate source forks.
The four verticals retain their original demos and eight storefront/merchant apps.

| Hosting target | Foundry Claude | Azure OpenAI Responses |
|---|---|---|
| Public-Azure Container Apps | Shared role runtime | Shared role runtime |
| Public-Azure AKS | Shared role runtime | Shared role runtime |

This is application and deployment code, not an already provisioned environment.
Live model availability, private connectivity, identity registration, commerce
adapter certification and image execution remain deployment acceptance gates.
No live orders, payments, refunds or inventory integrations are supplied.

## Install and develop

Use Python 3.11+ and Node 22. Existing demos still use their documented explicit
demo authentication and fixture backends; do not expose them as production APIs.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
npm --prefix examples ci
.\.venv\Scripts\python.exe scripts\run_demo.py retail --all
```

For production dependencies without test tools, install `requirements-azure.txt`.
It layers pinned Azure/provider additions and both host packages over the
preserved upstream pins. Repository siblings always resolve from this checkout.

The production entry points are separate processes:

```powershell
python -m commerce_host api
python -m commerce_host worker
python -m commerce_gateway
```

Their required configuration is documented in [the host](../apps/host/README.md)
and [gateway authentication](gateway-auth.md). Both API and worker expose 8080
health endpoints. A worker is a long-lived shared workload, not a pod per chat.
Production uses built frontends and ASGI servers, never demo launchers, reload
servers or `next dev`.

## Model configuration

Select one baseline transport and supply real, authorized deployment names:

```dotenv
COMMERCE_PROVIDER=claude_foundry
ANTHROPIC_FOUNDRY_BASE_URL=https://<resource>.services.ai.azure.com/anthropic/
COMMERCE_MODEL=<claude-deployment>
```

Or:

```dotenv
COMMERCE_PROVIDER=azure_openai
AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com/openai/v1/
COMMERCE_MODEL=<openai-deployment>
```

Purpose-specific overrides are `COMMERCE_SHOPPING_CHAT_MODEL`,
`COMMERCE_SHOPPING_EXTRACTION_MODEL`, `COMMERCE_MERCHANT_CHAT_MODEL`,
`COMMERCE_MERCHANT_EXTRACTION_MODEL` and `COMMERCE_MERCHANT_ANALYSIS_MODEL`.
Each also supports the corresponding `_PROVIDER` and `_ENDPOINT` override.
`COMMERCE_MODEL_TIMEOUT_S` bounds a model request. Do not copy model tokens into
browser variables, prompts, URLs or logs.

After finalizing deployment configuration, compute the conversation fingerprint:

```powershell
python -c "from commerce_common.providers import provider_configuration_id; print(provider_configuration_id())"
```

Set `COMMERCE_PROVIDER_ID` to that value for the API and worker. Startup rejects
mismatches; it does not silently substitute a new provider into existing
conversations. Deployment changes require new conversations or a separately
reviewed explicit migration. No mid-turn provider fallback is attempted.

Foundry Claude uses refreshing Entra credentials for
`https://ai.azure.com/.default`; Azure OpenAI uses
`https://cognitiveservices.azure.com/.default`. Direct Claude is an explicit
alternative documented in the provider READMEs. Claude server tools/caching are
capability-guarded; the portable baseline uses validated client tools and
read-only backend analytics. Cosmos is not a relational analytics database.

## Backend and authorization boundaries

`COMMERCE_BACKEND_FACTORY=<installed-module>:<factory>` must implement the
[backend contract](../apps/host/README.md#integration-boundary) against an
authoritative commerce system. Production requires `durable_backend` and refuses
the fixture factory. The factory receives a verified tenant/principal; every
backend lookup and custom route must preserve that boundary. The capability flag
is a certification assertion, not an implementation of a commerce system.
Merchant adapters additionally declare `conditional_approvals` and atomically
enforce the server-provided approved change fingerprint.

The built-in fixture adapter exercises all four verticals for single-process
labs. Its carts, stock and change ledger are deliberately not advertised as
multi-replica persistence. The original complete local demos remain available.
Entertainment's public demo-return control is not a production route.

Customer OIDC and merchant workforce identity terminate at the server-owned
gateway. Browser cookies are HttpOnly/Secure; API JWTs are signature/issuer/
audience/expiry/tenant validated. Separate role hosts reject cross-role routes.
Merchant approval requires a verified approver role and a server-owned action;
chat text and previews cannot authorize it. Merchant ingress additionally requires
the private network path.

Guest memory is opt-in. The separately trusted guest authority cannot mint
customer/merchant identities. Guest assertions are bound to an inactivity-limited,
revocable registry and consent. Guest profile prefetch never inherits a backend's
default customer. Account/order/ticket-wallet access requires sign-in.

Migration requires both identities and explicit consent. Existing customer keys
win. A frozen source plus destination receipt coordinates retries across
partitions; no cross-partition transaction is claimed. Carts, orders and
transcripts are not silently merged. Logout revokes the current browser's guest
identity and selected conversation; it is not an all-device OIDC revocation.

## Durable state and operations

Session state, transcript manifest, turn acceptance/completion and outbox records
publish with conditional single-partition Cosmos batches. Immutable transcript
chunks are bounded and staged behind the same session fence, so a concurrent
revocation cannot be followed by new transcript writes. Failed publication never
replaces another writer's committed state.

Every turn/action has an idempotency key and fenced worker lease. External
mutations have durable intent/results. Ambiguous effects are not blindly retried:
their session remains locked for authoritative reconciliation. Do not clear the
lock to make an uncertain operation run again. Cancellation is not rollback.
Safe memory, migration, purge and retention jobs have bounded retries/dead-letter
states; monitor failures and reconcile dead work operationally.
The host provides an authenticated, tenant-bound maintenance command for safe
failed/dead jobs; it deliberately refuses uncertain commerce effects.

SSE preserves `text_delta`, `tool_call`, `ui`, `cart_update`/`change_update` and
`turn_complete`. `turn_accepted` carries the durable execution ID. Reconnect uses
the event endpoint and cursor, not a new chat POST. Segments close within 210
seconds; heartbeats do not remove Container Apps' 240-second ingress limit.
Queued direct mutations become successful only after their durable result.

Transcript retention defaults to 30 days from session creation. Customer facts
expire 90 days after update; unrelated writes do not renew them. Expired facts
are hidden immediately and scrubbed by durable jobs, whose physical progress
must be monitored during outages. Guests expire after 30 days of inactivity.
Overrides are bounded to 1-365 days. Facts are limited to 100 per principal;
transcript chunks, event counts, aggregate sizes and page sizes are bounded.

Manual OpenTelemetry emits usage/duration/job outcomes without capturing message
bodies or request headers. Token counts are not currency estimates. Enable Azure
export only with an Application Insights connection string. Container Insights
remains disabled until an approved log-redaction/DCR policy is in place.

## Hosting and release

Use the independent [Container Apps/AKS infrastructure](azure-infrastructure.md),
[private runner runbook](azure-private-runners.md) and
[cost drivers](azure-cost-drivers.md). Release immutable image digests from an
authorized private build/release environment. Public CI performs offline checks
and builds; it does not provision Azure or push into private ACR.

The deployment uses private Cosmos, ACR Premium, Key Vault and model connectivity,
role-specific managed identities, internal application ingress and WAF routing.
Follow the network/identity preflight and validate actual model entitlement,
quota, availability and private inference before declaring an environment ready.

Azure Stack Hub is not a supported target: public-Azure managed identity and
managed service assumptions do not transfer. Azure Local portability is an
optional architecture exercise requiring its own identity, networking, data and
model endpoint design, not another parameter on these Bicep templates.

## Verification

```powershell
python -m ruff check .
python -m ruff format --check .
python scripts\check.py
python -m pytest -q
npm --prefix examples run test --workspace web-shared
npm --prefix examples run build
```

Infrastructure CI additionally compiles both Bicep targets, renders all Helm
configurations, validates Kubernetes schemas and rejects insecure inputs.
Container CI builds API/gateway images and all eight frontend images.

Tests use scripted providers, conditional copying document fakes and real
gateway-to-host ASGI integration with test signing keys. They are not emulator,
live OIDC, live model or cloud networking certification. On Windows, install
IANA timezone data; the upstream POSIX-only file-mode assertion cannot establish
Windows ACL semantics. Keep the unmodified assertion enabled in Linux CI.
