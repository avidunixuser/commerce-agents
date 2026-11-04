# Azure cost drivers

This design prioritizes a private production boundary. It is not a free-tier
demo and does not provide a traffic-based price estimate. Obtain current
regional prices and model deployment terms for the actual subscription before
approval. No live cost, quota or capacity query was performed.

| Driver | Why it exists | Inputs to price and measure |
|---|---|---|
| Application Gateway WAF_v2 | Public TLS storefront, private merchant listener, prevention rules | Baseline gateway hours, minimum capacity two, capacity units, TLS/connections/throughput and processed bytes |
| Azure Firewall Standard | Controlled egress and DNS proxy | Deployment hours and processed traffic; typically a significant fixed baseline even when the app is idle |
| Private endpoints and DNS | Cosmos, ACR, vault and enabled model families | Endpoint count, hourly and data processing rates, DNS zones/queries and network topology |
| ACR Premium | Private Link and immutable images | Registry tier, image/retention storage and egress; both role frontend images per enabled vertical |
| Cosmos NoSQL | Atomic partitions, durable jobs, auth records and guest memory | Shared database autoscale maximum 4000 RU/s by default, storage, backup retention, indexed writes, retries, query/RU consumption |
| ACA alternative | Eight role-separated warm app processes | Default minimum two replicas each, 0.5 vCPU/1 GiB per replica, CPU/memory execution, requests and logs |
| AKS alternative | Private cluster and two node pools | Standard control-plane tier, three system plus three user nodes at floor, selected VM/disk/zones, LB/proxy overhead and logs |
| Models | Chat, memory extraction and merchant analysis | Actual provider/deployment SKU, reserved capacity versus tokens, reasoning/output/context lengths, retries and per-purpose usage |
| Key Vault | TLS and gateway signing/OIDC/encryption material | Operations, certificate lifecycle and retention |
| Telemetry | Failure, usage and job visibility without content logs | Workspace ingestion, retention, export, diagnostic metrics/alerts and redaction processing |
| Private runner/connectivity | Builds and control-plane/private service access | Ephemeral build compute/storage, approved tool/package access, VPN/ExpressRoute/DNS resolver if not already provided |

ACA and AKS are alternatives; do not price both unless operating both. Each
independent target creates its own shared services unless the IaC is deliberately
adapted to an approved shared-service topology. Cosmos/registry/model private
endpoint counts derive from enabled model families, not from four hypothetical
simultaneous verticals. This chart runs one chosen vertical with eight
Deployments, not one pod per conversation.

Reduce costs only after measuring correctness and SLOs. First bound prompt/
transcript windows and tool output, extraction frequency, event replay,
retention, index usage, RU-heavy discovery queries and telemetry cardinality.
Measure worker lease/retry amplification and idle floors. Reduce duplicate
telemetry and exclude content rather than sacrificing privacy to debug volume.
Cost allocation tags and budget alerts belong to the organization's subscription
governance; no unsupported exact monthly amount is embedded here.

Do not save money by making data/model endpoints public, sharing node and
runtime identities, removing merchant isolation, allowing a mutable image tag,
disabling WAF, switching private ACR to Basic, or scaling every worker to zero
without a real wake-up mechanism. Cosmos TTL is asynchronous; enforce expiry
on reads/writes as well as storage cleanup.

Available-zone, region, VM and model-SKU changes need renewed capacity and
private-connectivity validation. Azure Stack Hub is not a supported target.
Azure Local can host compatible containers only with separately engineered
network/identity/service integrations; public-Azure managed services and Bicep
resources do not run there unchanged.
