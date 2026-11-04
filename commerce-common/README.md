# commerce-common (package `commerce_common`)

The mechanisms both agent roles build on. The role packages (`shopping_agent`,
`merchant_agent`) add their types, prompts, tool contracts, and gates on top; the
runtimes import both. One module per mechanism:

| Module | Holds |
|---|---|
| `config.py` | `BaseAgentConfig`: identity, models, budgets, capabilities, memory, caps |
| `fencing.py` | `Fence`: sanitizing and fencing text the model reads as data; chip hygiene |
| `memory.py` | `MemoryStore` contract, write filter, `validate_fact`, extraction, `MemoryRuntime` |
| `skills.py` | `SkillRegistry`: loads `SKILL.md` directories and renders the prompt index |
| `prompt_assembly.py` | Where the cache breakpoints go (static system block, tool array, the newest persisted message) and where the per-request context block and its clock go |
| `grounding.py` | `GroundingRule` and the matchers; `first_forced_tool` |
| `presentation.py` | `PresentationComponent`, `PresentationExtension`, `run_presentation`; `enrich_partial` for `ui_partial` frames |
| `delegation.py` | `DelegateExtension`: an isolated model task behind a tool |
| `execution.py` | `BaseToolExecutor`: dispatch, failure ladder, skills, presentation, delegates, memory; `split_status` (a call's `status` line, handed to the host) |
| `streaming.py` | `AgentEvent` (the event list is in the module docstring), `ToolOutcome`, `to_sse` |
| `turn.py` | Messages API turn helpers: user text, history compaction, eager tool dispatch, `StreamedRound` (a round's `ui_partial` frames as it streams; an error result for unparsed input), `round_closes_turn` (a round of rendered presentation calls, chips among them, ends the turn), tool-result blocks, usage, the model-call log record, transcript slice |
| `providers.py` | Typed portable history, requests, stream fragments and rounds; Claude Messages and Azure OpenAI Responses transports; purpose-specific Entra factories |
| `agent_sdk.py` | The SDK runtimes' shared plumbing and its `round_closes_turn` hook (needs `claude-agent-sdk`) |
| `mcp_server.py` | The MCP servers' shared plumbing: `registrar` (leaves out switched-off tools and the `status` argument) and the loopback bind guard (needs `mcp`) |
| `manifest.py` | Resolves a Managed Agents manifest; run by `scripts/deploy_managed_agent.sh` |
| `testing.py` | Scripted stand-ins for the streaming and `create` client calls, plus a spy memory store, for credential-free tests |
| `types.py` | `MemoryFact`, `MemoryCategory`, `ClockContext` |

Extras: `[sdk]` and `[mcp]` install the optional dependencies of `agent_sdk.py` and
`mcp_server.py`; `[examples]` adds what the example hosts import, and `requirements-dev.txt` pins the test tools.

Tests: `pytest commerce-common/tests`.

## Model providers

The Messages API role runtimes accept `provider=` and `extraction_provider=`; the merchant
runtime also accepts `analysis_provider=`. These transports use the existing role loops,
executors, grounding, presentation, provenance and approval gates. Existing `client=`
callers and the Agent SDK remain supported.

Install `[azure]` for the Azure model SDK and async Entra credentials. The production host
calls `create_provider_from_env(role="shopping" | "merchant",
purpose="chat" | "extraction" | "analysis", credential=async_credential)` and passes each
result to its corresponding agent argument. Call `provider.aclose()` on shutdown. A supplied
credential remains host-owned; an omitted credential creates a provider-owned
`DefaultAzureCredential`.

| Environment | Meaning |
|---|---|
| `COMMERCE_PROVIDER` | `claude_foundry` (default), `azure_openai`, or explicit direct `anthropic` |
| `COMMERCE_MODEL` | Deployment/model name; required unless overridden for the purpose |
| `COMMERCE_{ROLE}_{PURPOSE}_PROVIDER`, `_MODEL`, `_ENDPOINT` | Optional purpose overrides, with uppercase role and purpose |
| `COMMERCE_PROVIDER_ID` | Must equal `provider_configuration_id(environ)`; deployment-wide fingerprint persisted and checked on continuation |
| `ANTHROPIC_FOUNDRY_BASE_URL` | HTTPS Claude base URL ending in `/anthropic/` |
| `AZURE_OPENAI_ENDPOINT` | HTTPS resource root or v1 base URL ending in `/openai/v1/` |
| `ANTHROPIC_API_KEY` | Required only for explicit direct `anthropic` mode |
| `COMMERCE_MODEL_TIMEOUT_S` | Request timeout in seconds, default 120, maximum 600 |

`provider_configuration_id` hashes the resolved provider mode, model and endpoint for
every role/purpose, excluding credentials and the fingerprint variable itself. Compute
it from the final deployment environment before setting `COMMERCE_PROVIDER_ID`. The
factory fails before client creation when a supplied fingerprint differs from the
actual configuration; every returned provider exposes the computed value as `identity`.

Azure OpenAI rejects Claude-only server tools, Claude reasoning settings, and explicit
rolling conversation caching. Configure `enable_web_search=False`, `thinking_effort=None`
and `rolling_conversation_cache=False`; analysis additionally requires
`analysis_use_code_execution=False`. Claude Foundry also rejects the hosted analysis
code-execution sandbox. Portable analysis retains the core read tools and a backend's
optional read-only analytical query interface.

Persist assistant `_provider` metadata alongside portable `content`. It contains ordered
native output and opaque/signed continuation; do not send it to browsers or move it across
providers. Azure requests use `store=False`, replay ordered output including encrypted
reasoning, and never rely on `previous_response_id`. Responses tool calls dispatch only
after a completed response validates every call, never from partial JSON. Text and partial
presentation frames still stream through the common round accumulator.

## Conditional extraction

`ConditionalExtractionStore` is an optional extension to the existing `MemoryStore`
contract: `extraction_revision(subject_id) -> str` and
`commit_extracted(subject_id, facts, revision) -> bool`. A production implementation must
atomically compare the subject revision and commit the batch. Every edit, delete (including
an absent-key delete) and purge invalidates the revision. The runtime captures it before
reading existing facts and invokes one conditional commit after the model returns.
`RetentionMemoryStore` forwards this extension without separate pre-commit deletions.
Legacy stores retain the generation-only behavior, which is not an atomic production
guarantee. Durable workers call `agent.update_memory(messages, session, strict=True)` so
model/storage failures can be retried; the default logs failures and returns no facts.
