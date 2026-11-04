# Browser gateway, identity, and containers

The production browser entry point is `python -m commerce_gateway` from
`apps/gateway` (port 8080). `commerce_host api` and `commerce_host worker` are
separate internal processes. Neither role API nor Next.js is a public ingress.
Deploy **different gateway, web, API, worker, managed identities, and configuration**
for `shopping` and `merchant`; select one vertical explicitly. The merchant
gateway requires both a real private ingress/network boundary and
`COMMERCE_PRIVATE_BOUNDARY=true`. That setting is an operator assertion, not a
substitute for a private endpoint, VPN, ingress restriction, or firewall.

## Required gateway configuration

| Variable | Meaning |
|---|---|
| `COMMERCE_ROLE` | `shopping` or `merchant` |
| `COMMERCE_PUBLIC_ORIGIN` | Exact externally visible HTTPS origin, no path |
| `COMMERCE_API_UPSTREAM` | Same-role internal API origin, e.g. `http://api:8080` |
| `COMMERCE_WEB_UPSTREAM` | Same-role internal Next.js origin, e.g. `http://web:3000` |
| `COMMERCE_OIDC_ISSUER` | Exact HTTPS issuer; customer OIDC/External ID for shopping, workforce Entra for merchant |
| `COMMERCE_OIDC_CLIENT_ID` | Confidential web application's client ID |
| `COMMERCE_OIDC_CLIENT_SECRET` | Injected secret reference; never a frontend build argument |
| `COMMERCE_OIDC_API_AUDIENCE` | Access-token audience the internal role API validates |
| `COMMERCE_OIDC_SCOPE` | `openid profile` plus the configured API's delegated scope |
| `COMMERCE_TENANT_ID` | Exact required `tid`; map customer issuer tenant claims explicitly at the issuer |
| `COMMERCE_GATEWAY_ISSUER` | HTTPS issuer for guest assertions, distinct from OIDC issuer |
| `COMMERCE_GATEWAY_SIGNING_KEY` | Injected RSA >=2048-bit PEM (RS256 recommended); Ed25519 supported if API explicitly allows EdDSA |
| `COMMERCE_GATEWAY_SIGNING_KID` | Unique published signing-key identifier |
| `COMMERCE_AUTH_ENCRYPTION_KEYS` | Comma-separated Fernet keys, new primary first, retained decryption keys afterward; injected secret references |
| `COMMERCE_COSMOS_ENDPOINT` | HTTPS data-plane endpoint |
| `COMMERCE_COSMOS_DATABASE` | Existing database |
| `COMMERCE_AUTH_CONTAINER` | Existing container partitioned on `/pk`, with TTL enabled |
| `COMMERCE_PRIVATE_BOUNDARY` | Must be `true` for merchants |

`COMMERCE_AUTH_SESSION_SECONDS` defaults to 3600 and is bounded to 300–86400.
Sessions expire no later than their access tokens. No refresh token is retained:
expiry requires another code flow. `COMMERCE_GUEST_SECONDS` defaults to 2592000,
bounded to 3600–2592000. Browser guest credentials expire after 30 days of
inactivity; authenticated guest activity conditionally renews the opaque cookie
and encrypted auth record. The host must also conditionally renew the durable
guest registry expiry on validated guest activity, without reviving an expired
or revoked identity, and enforce memory-retention policy from that registry.

Register **exactly** `<origin>/auth/callback` as the provider redirect URI.
Discovery issuer and JWT issuer, signatures, key IDs, algorithm, audience,
tenant, expiry, subject binding, nonce and authorized party are checked.
The provider must return JWT access tokens for this API, not opaque tokens.
Merchant tokens must carry the `merchant` app role; approval requires the
separate `approver` role enforced by the API.

Use Cosmos **data-plane RBAC** for the gateway's managed identity: read/create/
replace/delete auth items only, not account keys or control-plane Contributor.
The gateway never provisions containers. Its `AuthStorage` protocol permits
testing without cloud access. Production startup exclusively constructs
`CosmosAuthStorage` with `ManagedIdentityCredential`; there is no in-memory fallback.
Auth payloads, including OIDC access tokens, PKCE verifier, state and nonce, are
encrypted before Cosmos writes. Expiry is enforced on reads, not only eventual
TTL deletion. OIDC flows are consumed with ETag-conditional deletion, preventing
cross-replica callback replay. Rate-limit counters use ETags too.
Auth-record keys are SHA-256 hashes of opaque browser credentials: plaintext
cookie credentials are not stored as Cosmos document IDs. Guest renewal uses
ETag-conditional replacement, never an upsert that could resurrect logout.
Encrypted payloads are bound to their document IDs to reject cross-record swaps.

Encryption rotation: inject the new primary **and** old decryption keys on all
replicas, roll replicas, retain old keys at least the maximum credential lifetime,
then remove them. New writes use the primary. Do not remove an active signing key
from API trust until issued assertions expire; coordinate signing-key replacement
with the API's JWKS refresh/rollout. Secrets come from deployment secret references,
not checked-in configuration, Docker build args, or `.env` files copied into images.

## Internal API integration contract

The gateway forwards customer/merchant access tokens exclusively through
server-side `Authorization: Bearer`. It does not forward browser Authorization,
cookies, identity headers, or provider tokens to Next.js. Bare forwarded subject/
tenant headers are never identity. Invalid browser bearer credentials are rejected,
not downgraded to guests. Browser cookies are opaque, `__Host-` scoped, Secure,
HttpOnly and SameSite=Lax. Unsafe API operations and logout require exact Origin
plus the server-record CSRF token returned by `/api/auth/session`.
Browser auth endpoints remain `/api/auth/*` for both roles. Server-to-server
merchant auth session/logout calls use `/api/merchant/auth/session` and
`/api/merchant/auth/logout`, matching the host's merchant route prefix.

Configure customer/workforce API validation using `COMMERCE_AUTH_ISSUER`,
`COMMERCE_AUTH_AUDIENCE`, `COMMERCE_AUTH_JWKS_URL` and allowed tenants. Guest
assertions require a **separate trusted issuer/key set**:
`COMMERCE_GUEST_ISSUER`, `COMMERCE_GUEST_JWKS_URL`, with the same configured API
audience as `COMMERCE_AUTH_AUDIENCE`.
The guest JWKS is published at `/.well-known/jwks.json`; no private key is exposed.

| Deployment consumer | Setting | Derivation |
|---|---|---|
| Shopping gateway | `COMMERCE_GATEWAY_ISSUER` | Shopping gateway's configured HTTPS issuer, normally its public HTTPS origin |
| Shopping API and worker | `COMMERCE_GUEST_ISSUER` | Exact same value as gateway `COMMERCE_GATEWAY_ISSUER` |
| Shopping API and worker | `COMMERCE_GUEST_JWKS_URL` | Shopping gateway public HTTPS origin + `/.well-known/jwks.json` |
| Each gateway | `COMMERCE_OIDC_ISSUER` | Exact host `COMMERCE_AUTH_ISSUER` for that role |
| Each gateway | `COMMERCE_OIDC_API_AUDIENCE` | Actual customer/workforce token audience, matching host `COMMERCE_AUTH_AUDIENCE` |
| Each gateway | `COMMERCE_TENANT_ID` | One tenant present in the corresponding host `COMMERCE_ALLOWED_TENANTS` |

Guest assertions use the gateway's required `COMMERCE_OIDC_API_AUDIENCE`, exactly
matching the host's `COMMERCE_AUTH_AUDIENCE`, including real Entra application
resource audiences. `commerce-api` is only an example configured audience, not a
hardcoded substitute for an application's registered audience.

Guest JWT claims:

```json
{
  "iss": "<configured gateway issuer>", "aud": "<configured API audience>",
  "sub": "<cryptographically random principal ID>", "tid": "<configured tenant>",
  "role": "shopping", "roles": [], "kind": "guest", "guest": true,
  "guest_epoch": 0, "jti": "<random revocation ID>",
  "memory_consent": false, "iat": 0, "exp": 120
}
```

Times are actual current Unix timestamps; the assertion lifetime is 120 seconds.
The API must check durable principal/revocation state and guest epoch on **every**
operation. Signature validity alone is insufficient. Guest credentials must never
authorize customer orders/account data or merchant capabilities.

| Endpoint | Gateway/API contract |
|---|---|
| `POST /api/auth/guest/bootstrap` | Public gateway requires exact Origin, explicit boolean `memory_consent`, and bounded network rate. Internal API receives signed guest Bearer plus `{principal_id, revocation_id, memory_consent}`. API validates signature first, creates durable registry binding from the signed claims, and enforces per-principal limits; all subsequent routes require that binding. Cookie issued only after success. |
| `GET /api/auth/session` | Internal API receives current signed identity, rejects revoked/expired principals; browser sees only safe state/CSRF, never tokens. |
| `POST /api/auth/migrate` | Customer Bearer, body `{consent:true,guest_assertion:"<short-lived signed guest JWT>"}` added exclusively server-side. API validates both issuers, tenant match, live guest possession, epoch and consent; durably freezes/revokes guest generation and accepts an idempotent migration job before responding. |
| `POST /api/auth/migrations/{id}/status` | Customer Bearer plus the same JSON proof/consent. Gateway retains encrypted proof tied to the customer browser session on 202; browser receives only the ID and safe status. API permits frozen proof solely for migration retries/status. Guest proof/auth cookie are removed only after durable `complete`, never at acceptance. UI polls boundedly and distinguishes pending, failure and completion. |
| `POST /api/auth/logout` | Current Bearer, optional guest assertion when a customer also possesses an unmigrated guest credential. API revokes guest registry credentials/generations and clears guest memory before success. BFF removes corresponding encrypted auth records and cookies. Customer facts are retained; logout does not sign out every device. |

The host owns the migration ledger and memory jobs: conditional destination
writes preserve existing customer facts, repeated submissions are idempotent,
revoked guest generations fence pending extraction, and cleanup is observable.
There is **no** cross-container transaction claim. No automatic transcript,
commerce account, order or cart merge occurs. The UI presents an explicit import
checkbox and explains customer-fact precedence. Unselected guest memory stays off.
Guest state is not promoted into customer identity during login; the old opaque
credential is retained separately solely to permit consent-based migration.
The host registry is in its runtime Documents container at
`partition(tenant, "guest", sub)`, document `identity`, with `epoch`, `revoked`,
`expires`, `revocation_id`, and strict boolean `memory_consent`. The gateway's encrypted auth container is a separate
store; the host creates and conditionally renews its own registry at bootstrap
and validated guest activity.

## Streaming and frontend behavior

All eight production Next builds use same-origin `/api`, regardless of
`NEXT_PUBLIC_API_URL`. `next dev` preserves each vertical's localhost demo default.
Demo profile selectors are disabled in production and production session requests
never send a browser-selected `user_id`. Shared identity controls expose guest
memory choice, login, logout, and explicit migration consent. Request errors are
shown rather than silently dropping the last stream failure.

Chat POST uses a fresh UUID `Idempotency-Key`. The host returns SSE with durable
`id:` fields and `X-Turn-Id` (or `turn_id` in an event), and `turn_complete` marks
completion. Reconnect uses `GET /api[/merchant]/turns/{id}/events` and
`Last-Event-ID`, **never another chat POST**; replay IDs are deduplicated.
Cancellation aborts the browser fetch and calls `POST .../turns/{id}/cancel`.
The gateway streams upstream chunks directly, disables SSE buffering, closes
upstream responses on disconnect, and forwards durable cursor headers.
The host must segment streams below the ingress timeout; heartbeat does not remove
that timeout. The gateway's upstream read timeout is 230 seconds.

Queued cart and merchant mutations carry UUID idempotency keys. HTTP 202 is only
acceptance: the shared client polls `GET /api[/merchant]/turns/{id}`, and returns
the actual `result` only after durable `status: "complete"`. Concurrent identical
submissions are deduplicated. Uncertain, failed, timed-out, or interrupted checks
retain the accepted turn ID; repeating the action resumes status checks rather
than sending another mutation. Missing durable IDs block blind resubmission.
Pending-action tracking is browser-memory state, not a claim of cross-reload
exactly-once execution; operators must reconcile unknown outcomes before retrying.

Public shopping gateways use a default-deny API root allowlist and reject merchant
paths after repeated percent decoding. Traversal, backslashes, ambiguous separators,
control characters and residual percent encoding are rejected before proxying.
Shopping gateways contain no merchant upstream. Private merchant gateways also
require workforce browser authentication before rendering portal pages.

Access logging is disabled in the Python gateway because callback query strings
contain authorization codes. Configure ingress/OIDC telemetry to redact query
strings, cookies, Authorization and guest assertions. No model credentials belong
in a frontend bundle or gateway settings.

## Images and local topology

Build from the repository root with `Dockerfile.api`, `Dockerfile.gateway`, and
`Dockerfile.web`. The web image requires `VERTICAL=retail|travel|telecom|entertainment`
and `WEB_APP=storefront-web|merchant-web`. Images are multi-stage and nonroot,
Python 3.11 / Node 22. Python wheels include local role/common packages; runtime
images also include role skill trees and vertical examples/assets.
Both Python builders constrain dependency resolution with the pinned entries from
`requirements.txt` and `requirements-azure.txt`, excluding recursive/editable
directives from the generated constraints. The API builds common `[examples,azure]`
and host `[telemetry]` extras into one wheelhouse, containing both Azure OpenAI and
Foundry Claude support. Runtime installation is offline from that wheelhouse.
There is no provider build argument.
The API image sets `COMMERCE_APP_ROOT=/app`; installed host runtime skill discovery
must honor this setting rather than deriving a source checkout from `__file__`.
Next standalone
tracing includes the shared workspace. `.dockerignore` excludes environment files,
keys, caches, virtual environments, tests and local build artifacts.
Production build scripts explicitly select Next's supported Webpack backend:
Next 16.3's Turbopack Google-font import-map handling failed on this Windows
checkout. Entertainment additionally uses locally packaged Fontsource Big Shoulders,
Archivo and IBM Plex Mono faces to avoid Next's Google-URL extension parsing bug.
These preserve the original faces/weights, are self-hosted, and carry the original
SIL Open Font License notices; the web runtime image includes copies in `/app/licenses`.
Development server defaults are unchanged.

`compose.yaml` models internal API/worker/web and loopback gateway only. Set
`COMMERCE_ROLE`, `COMMERCE_VERTICAL`, `COMMERCE_WEB_APP` and external
`COMMERCE_API_ENV_FILE`/`COMMERCE_GATEWAY_ENV_FILE` paths. Supply an HTTPS
front door for Secure cookies and real OIDC, Cosmos/MI and model/integration
configuration; there are intentionally no fake credentials or auth bypass.
API and worker both inherit the port-8080 `/health/live` image healthcheck;
the host also exposes `/health/ready`.
Compose is not evidence that a local Docker engine has managed identity or a
private merchant production boundary.

Validation:

```powershell
$env:PYTHONPATH = 'apps\gateway;apps\host'
.venv\Scripts\python.exe -m pytest --confcutdir=apps\gateway apps\gateway\tests -q
npm --prefix examples run test --workspace web-shared
npm --prefix examples run build
```

Container CI commands (repository-root context, no credentials/build secrets):

```powershell
docker build -f Dockerfile.api -t commerce-api:ci .
docker build -f Dockerfile.gateway -t commerce-gateway:ci .
docker build -f Dockerfile.web --build-arg VERTICAL=retail --build-arg WEB_APP=storefront-web -t commerce-web:retail-shopping-ci .
```

For all eight web images, matrix `VERTICAL` over `retail`, `travel`, `telecom`,
`entertainment` and `WEB_APP` over `storefront-web`, `merchant-web`; API and gateway
need no build arguments. `npm --prefix examples run test --workspace web-shared`
is the exact network-free browser transport test command; the eight builds use
the committed npm lockfile and `npm ci` in Docker.

`tests/test_host_interop.py` sends actual BFF requests through an ASGI transport
to the real host app, real JWT verifier, session/memory stores, runtime factory
and migration worker. Only OIDC HTTP/JWKS, model providers and persistence/commerce
backends are local fixtures; no host route or identity-verification result is
mocked. It requires the host bootstrap/session/logout and migration/status
contracts above and deliberately fails if they are missing.
