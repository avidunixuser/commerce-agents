# Azure role host

`commerce_host` hosts one role and one selected vertical. APIs and workers share
Cosmos NoSQL aggregates and immutable application images. It does not import a
demo API entry point or run a development server.

Install the repository requirements, then install this package from its directory:

```powershell
python -m pip install -r requirements-azure.txt
python -m commerce_host api
python -m commerce_host worker
```

Both commands expose port 8080 with `/health/live` and `/health/ready`.
The worker command also serves the role API. Production deploys separate API and
worker apps and disables worker ingress. The explicit singleton fixture demo
uses the worker command for both functions so its process-local backend is
shared by the HTTP routes and worker. A worker is not a conversation-specific pod.

## Configuration

Required deployment inputs:

| Variable | Meaning |
|---|---|
| `COMMERCE_ROLE` | `shopping` or `merchant` |
| `COMMERCE_VERTICAL` | `retail`, `travel`, `telecom`, or `entertainment` |
| `COMMERCE_MODE` | `production` or explicit `fixture` lab mode |
| `COMMERCE_COSMOS_ENDPOINT` | HTTPS private Cosmos endpoint |
| `COMMERCE_COSMOS_DATABASE` | Provisioned database |
| `COMMERCE_COSMOS_CONTAINER` | Provisioned role container, partition key `/pk`, TTL enabled |
| `COMMERCE_AUTH_ISSUER` | Trusted HTTPS token issuer |
| `COMMERCE_AUTH_AUDIENCE` | This role API's audience |
| `COMMERCE_AUTH_JWKS_URL` | Trusted HTTPS RS256 verification keys |
| `COMMERCE_ALLOWED_TENANTS` | Comma-separated explicit tenant allowlist |
| `COMMERCE_PROVIDER_ID` | Immutable conversation provider/deployment fingerprint |
| `COMMERCE_BACKEND_FACTORY` | Installed integration `module:function` |
| `COMMERCE_PROVIDER` | Explicit `claude_foundry`, `azure_openai`, or configured direct Claude transport |
| `COMMERCE_GUEST_ISSUER` / `COMMERCE_GUEST_JWKS_URL` | Separate trusted HTTPS guest authority for shopping |

Use the Azure credential chain and a workload managed identity for Cosmos. The
host does not accept a database account key. Production rejects the built-in
fixture factory. Model deployment configuration is server-only.
The host defaults to the explicit portable tool baseline
(`COMMERCE_PORTABLE_TOOLS=true`): client-executed tools/read-only analytics, no
Claude web search/server code execution, thinking effort or rolling cache.
Set the flag to `false` only for an explicitly configured provider-capable
deployment. Unsupported combinations fail their capability checks; no provider
fallback or silent per-request feature substitution occurs. Local demo configs
remain unchanged.

Retention overrides are `COMMERCE_TRANSCRIPT_DAYS` (30),
`COMMERCE_MEMORY_DAYS` (90), and `COMMERCE_GUEST_DAYS` (30), each 1 to 365.
`COMMERCE_STREAM_SECONDS` is at most 210. Session transcript expiry is fixed from
session creation; committing another turn does not indefinitely extend old
transcripts.
Customer fact expiry is per update, not extended by unrelated writes. Expired
facts are immediately excluded from reads and scrubbed by durable retention jobs;
outages can delay physical scrubbing and require dead-letter monitoring. Guest
facts follow the guest identity's sliding inactivity deadline. TTL refresh does
not invalidate a semantic extraction revision. Migrated facts start the target
retention period when copied, while preserving their original provenance.

## Integration boundary

An integration factory receives `(Settings, Principal)` and returns
`commerce_host.runtime.BackendBundle`. Its backend implements upstream
`StorefrontBackend` or `MerchantBackend`; config and presentation extensions use
the upstream role types. Production additionally requires the explicit
`durable_backend` capability.
Merchant integrations also require `conditional_approvals`. The host captures a
change fingerprint when accepting approval and rechecks it in the worker.
`HostMerchantContext.approved_change_fingerprint` must be checked atomically by
the authoritative backend when applying/discarding the immutable change; the
capability cannot be asserted around an unconditional update. Both role contexts
also carry the verified `tenant_id` and `principal_scope`.

This capability is a declaration that the external system is authoritative,
tenant-bound, durable, and enforces business rules. It is not a replacement for
testing the adapter. Supply custom read-only portal/vertical routes in
`BackendBundle.routes`, keyed by `GET /path`; handlers receive the verified
context, session aggregate and query parameters. Missing capabilities return an
explicit unavailable route, never fixture data or fabricated zero figures.
The bounded catalog browser is `GET /products`; a blank search is not a catalog
implementation. Entertainment mutation handlers use the explicit supported
`POST /holds/release`, `/waitlist/join`, `/waitlist/claim`,
`/tickets/transfer` and `/tickets/transfer/cancel` keys. They run through durable
action jobs and the effect journal, not the public read route. Integrations must
validate their domain inputs before external mutation.

The host journals mutating core executor calls before dispatch. Backend
integrations must provide idempotency/reconciliation for external effects; the
host never promises a transaction spanning Cosmos and a commerce platform.
A lost worker lease leaves a possibly executed turn `uncertain` rather than
replaying side effects. An operator must reconcile it with the authoritative
backend before retrying.
Uncertain sessions remain locked. Do not clear their locks or resubmit actions
until authoritative reconciliation proves the outcome. A cancellation is not a
rollback; already completed effects may require reconciliation too.

The built-in `commerce_host.fixtures:build` factory is a bounded, single-process
lab implementation. It reuses vertical classes and presentation extensions, but
its inventory/carts/change ledgers are not shared across replicas. Use upstream
`scripts/run_demo.py` for the original complete local vertical demos. Never label
these fixtures live commerce integrations.

`demo_setup.py` initializes five demo-namespaced gateway secrets inside the
private network. It requires explicit fixture mode, a selected managed
identity, named-vault write permission and scoped Cosmos access for its
conditional initialization fence. Key material is generated in-process and
sent only to Key Vault, not Cosmos or logs. A completed or interrupted setup
cannot be replayed automatically: inspect the execution and namespaced fence
before an explicitly approved retry. The
[retail demo runbook](../../docs/azure-retail-demo.md) describes the deployment
and removal of temporary setup permissions.

## API and streaming

Shopping routes use `/api`; merchant routes use `/api/merchant`. Every route
except health requires a verified JWT; a session ID alone is not authentication.
Principal and tenant come only from verified claims. Merchant tokens need the
`merchant` role and approval actions need `approver`. Public shopper APIs reject
merchant routes.

`POST /session` creates a principal-bound session. Send `X-Session-Id` thereafter.
`POST /chat` requires a UUID `Idempotency-Key`. It atomically reserves the session
and persists its turn job. The SSE response starts with `turn_accepted`, emits
the existing agent events, and closes with `reconnect` before ingress limits.
Resume through `GET /turns/{id}/events` and `Last-Event-ID: turn-id:sequence`;
do not submit another chat request to reconnect. `GET /turns/{id}` reports
durable status; `POST /turns/{id}/cancel` requests cancellation.

Direct cart and approval actions also require idempotency keys and use durable
jobs. A 202 `accepted` response means queued, not applied. Completion appears
in the job's status/result. There is no payment endpoint.

Memory routes validate facts with upstream filters. Conditional extraction uses
a captured revision to avoid resurrecting purged, deleted or manually corrected
facts. Reset revokes the old session and queues physical removal of its
transcripts/events/jobs; it never reseeds production memory.
Deletion or purge after migration consent also fences that migration rather than
allowing its older snapshot to recreate customer facts.

Guest migration requires a signed guest assertion, a signed-in destination in
the same tenant/role, and explicit consent. Its frozen-source/receipt state
machine preserves existing customer keys and records cross-partition progress.
It does not automatically merge carts, orders or transcripts.

## Persistence

`storage.py` implements conditional single-logical-partition batch operations.
`sessions.py` stages bounded immutable transcript chunks and publishes them with
a conditional manifest/state/outbox transaction. Unpublished chunks are not
visible and expire. `memory.py` keeps at most 100 validated facts per subject
in a small revision-controlled document. `jobs.py` provides leases, fencing,
tool intent/results, retry bounds and terminal/dead-letter states.

No whole-tenant partition or cross-container transaction is used. Scheduler
queries are bounded and indexed by kind/shard/availability; measure their RU
cost for the actual workload. Container TTL and composite query indexes must be
provisioned before running.

## Checks

```powershell
python -m pytest -q apps\host\tests
python -m ruff check apps\host
python -m ruff format --check apps\host
```

Fakes test conditional transactions, authorization, deletion races, migration,
worker contention and replay. They do not prove live Cosmos, OIDC, model access,
private networking, Kubernetes behavior, or external integration correctness.

## Operator redrive

Safe failed/dead memory, retention, purge and migration jobs can be redriven using
`python -m commerce_host.maintenance redrive --partition <pk> --job <job-id>
--etag <exact-etag> --operator-token-stdin`. Supply a short-lived workforce token
on standard input, not a command-line argument. Configure the separate trusted
`COMMERCE_OPERATOR_ISSUER`, `COMMERCE_OPERATOR_AUDIENCE` and
`COMMERCE_OPERATOR_JWKS_URL`; the verified token needs both `merchant` and
`operator` roles. The Azure credential chain independently needs scoped Cosmos
data-plane access. The service checks tenant, status, purpose, expiry and ETag,
then records the operator's principal hash and fences prior workers.

Use an authorized, bounded query returning only job ID, partition, ETag, purpose,
status and attempt count to inspect dead work. Do not copy transcript/job
payloads into tickets or logs. `uncertain` turns/actions cannot use this command:
reconcile with the external system before any separately reviewed recovery.
