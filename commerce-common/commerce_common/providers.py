"""Model transports for the existing commerce turn loops.

History contains portable text, tool calls and results. Provider-private output lives
under ``_provider`` on assistant messages, not among portable content blocks. Hosts must
persist that metadata with the conversation and bind the provider for its lifetime.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Literal, NotRequired, Protocol, TypedDict, runtime_checkable
from urllib.parse import urlsplit

from .turn import usage_totals


class ProviderError(ValueError):
    """A response or configuration cannot safely enter the tool executor."""


class HistoryBlock(TypedDict, total=False):
    type: Literal["text", "tool_use", "tool_result"]
    text: str
    id: str
    name: str
    input: dict[str, Any]
    tool_use_id: str
    content: str
    is_error: bool


class Continuation(TypedDict):
    name: Literal["claude", "azure_openai"]
    output: list[dict[str, Any]]
    identity: NotRequired[str]


class HistoryMessage(TypedDict):
    role: Literal["user", "assistant"]
    content: str | list[HistoryBlock]
    _provider: NotRequired[Continuation]


@dataclass
class Block:
    """Portable assistant text or a complete, validated tool invocation."""

    type: Literal["text", "tool_use"]
    text: str = ""
    id: str = ""
    name: str = ""
    input: dict[str, Any] = field(default_factory=dict)

    def model_dump(self, **_: Any) -> dict[str, Any]:
        if self.type == "text":
            return {"type": "text", "text": self.text}
        return {"type": "tool_use", "id": self.id, "name": self.name, "input": self.input}


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class RoundResponse:
    content: list[Block]
    stop_reason: str | None
    usage: Usage = field(default_factory=Usage)
    continuation: dict[str, Any] | None = None
    container: Any = None

    def model_dump_json(self) -> str:
        # Deliberately omit opaque/signed data from debug logs.
        return json.dumps({"content": [b.model_dump() for b in self.content]})


@dataclass(frozen=True)
class RoundRequest:
    """Application-owned request; no provider transport or previous response id."""

    model: str
    max_tokens: int
    system: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    messages: list[HistoryMessage]
    tool_choice: dict[str, str] = field(default_factory=lambda: {"type": "auto"})
    settings: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, request: dict[str, Any]) -> RoundRequest:
        known = {"model", "max_tokens", "system", "tools", "messages", "tool_choice"}
        system = request.get("system", [])
        if isinstance(system, str):
            system = [{"type": "text", "text": system}]
        return cls(
            model=request["model"],
            max_tokens=request["max_tokens"],
            system=system,
            tools=request.get("tools", []),
            messages=request["messages"],
            tool_choice=request.get("tool_choice", {"type": "auto"}),
            settings={k: v for k, v in request.items() if k not in known},
        )


@dataclass(frozen=True)
class StreamEvent:
    """Transport-independent stream fragment. Only ``tool_end`` can dispatch."""

    kind: Literal["text", "tool_start", "arguments", "tool_end", "usage"]
    index: int = 0
    text: str = ""
    id: str = ""
    name: str = ""
    usage: Usage | None = None


class ProviderStream(Protocol):
    def __aiter__(self) -> AsyncIterator[StreamEvent]: ...

    async def get_final_message(self) -> RoundResponse: ...


@runtime_checkable
class ModelProvider(Protocol):
    name: str

    def validate_config(self, config: Any, *, purpose: str = "chat") -> None: ...

    def stream(self, request: RoundRequest) -> Any: ...

    async def complete(self, request: RoundRequest) -> RoundResponse: ...

    async def aclose(self) -> None: ...


async def complete_model(client_or_provider: Any, request: dict[str, Any]) -> Any:
    """Extraction seam: preserve the legacy ``client=`` API without a duck client."""
    if isinstance(client_or_provider, ModelProvider):
        return await client_or_provider.complete(RoundRequest.from_dict(request))
    return await client_or_provider.messages.create(**request)


def _dump(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else value.model_dump(exclude_none=True)


def _history(
    messages: list[dict[str, Any]], provider: str, identity: str | None = None
) -> list[dict[str, Any]]:
    for message in messages:
        private = message.get("_provider")
        if private and private.get("name") != provider:
            raise ProviderError("A conversation cannot change model provider")
        if private and identity is not None and private.get("identity") != identity:
            raise ProviderError("A conversation cannot change provider deployment identity")
    return messages


def _arguments(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise ProviderError("Function arguments are not complete JSON") from exc
    if not isinstance(value, dict):
        raise ProviderError("Function arguments must be a JSON object")
    return value


def _claude_request(request: RoundRequest, identity: str | None = None) -> dict[str, Any]:
    messages = []
    for message in _history(request.messages, "claude", identity):
        private = message.get("_provider")
        content = copy.deepcopy(private["output"] if private else message["content"])
        portable = message.get("content")
        if (
            private
            and isinstance(portable, list)
            and portable
            and portable[-1].get("cache_control")
            and content
        ):
            content[-1]["cache_control"] = portable[-1]["cache_control"]
        messages.append(
            {
                "role": message["role"],
                "content": content,
            }
        )
    return {
        "model": request.model,
        "max_tokens": request.max_tokens,
        "system": request.system,
        "tools": request.tools,
        "messages": messages,
        "tool_choice": request.tool_choice,
        **request.settings,
    }


def _claude_response(response: Any) -> RoundResponse:
    output = [_dump(block) for block in response.content]
    content = []
    ids: set[str] = set()
    for block in output:
        if block["type"] == "text":
            content.append(Block("text", text=block["text"]))
        elif block["type"] == "tool_use":
            if block["id"] in ids or not isinstance(block["input"], dict):
                raise ProviderError("Duplicate call id or invalid function arguments")
            ids.add(block["id"])
            content.append(
                Block("tool_use", id=block["id"], name=block["name"], input=block["input"])
            )
    counts = getattr(response, "usage", None)
    return RoundResponse(
        content,
        response.stop_reason,
        Usage(**{key: getattr(counts, key, 0) or 0 for key in usage_totals()}),
        {"name": "claude", "output": output},
        getattr(response, "container", None),
    )


class _ClaudeStream:
    def __init__(self, stream: Any, identity: str | None = None) -> None:
        self._stream = stream
        self._ids: set[str] = set()
        self._usage = Usage()
        self._identity = identity

    async def __aiter__(self) -> AsyncIterator[StreamEvent]:
        async for event in self._stream:
            kind = event.type
            if kind in ("message_start", "message_delta"):
                counts = getattr(event.message if kind == "message_start" else event, "usage", None)
                if counts:
                    for key in usage_totals():
                        if isinstance(value := getattr(counts, key, None), int):
                            setattr(self._usage, key, value)
                    yield StreamEvent(
                        "usage",
                        usage=Usage(**vars(self._usage)),
                    )
            elif kind == "content_block_start":
                block = event.content_block
                if block.type == "tool_use":
                    if block.id in self._ids:
                        raise ProviderError("Duplicate function call id")
                    self._ids.add(block.id)
                    yield StreamEvent("tool_start", event.index, id=block.id, name=block.name)
            elif kind == "content_block_delta":
                delta = event.delta
                if delta.type == "text_delta":
                    yield StreamEvent("text", event.index, text=delta.text)
                elif delta.type == "input_json_delta":
                    yield StreamEvent("arguments", event.index, text=delta.partial_json)
            elif kind == "content_block_stop":
                yield StreamEvent("tool_end", event.index)

    async def get_final_message(self) -> RoundResponse:
        response = _claude_response(await self._stream.get_final_message())
        if self._identity is not None:
            response.continuation["identity"] = self._identity
        return response


class ClaudeProvider:
    name = "claude"

    def __init__(
        self,
        client: Any,
        *,
        model: str | None = None,
        identity: str | None = None,
        supports_code_execution: bool | None = None,
    ) -> None:
        from anthropic import AsyncAnthropicFoundry

        self.client = client
        self.model = model
        self.identity = identity
        self._owned_credential: Any = None
        self.supports_code_execution = (
            not isinstance(client, AsyncAnthropicFoundry)
            if supports_code_execution is None
            else supports_code_execution
        )

    def validate_config(self, config: Any, *, purpose: str = "chat") -> None:
        if (
            purpose == "analysis"
            and config.analysis_use_code_execution
            and not self.supports_code_execution
        ):
            raise ProviderError("Claude Foundry does not support the hosted code execution tool")

    @asynccontextmanager
    async def stream(self, request: RoundRequest) -> AsyncIterator[ProviderStream]:
        body = _claude_request(request, self.identity)
        if self.model:
            body["model"] = self.model
        async with self.client.messages.stream(**body) as stream:
            yield _ClaudeStream(stream, self.identity)

    async def complete(self, request: RoundRequest) -> RoundResponse:
        body = _claude_request(request, self.identity)
        if self.model:
            body["model"] = self.model
        response = _claude_response(await self.client.messages.create(**body))
        if self.identity is not None:
            response.continuation["identity"] = self.identity
        return response

    async def aclose(self) -> None:
        try:
            await self.client.close()
        finally:
            if self._owned_credential is not None:
                await self._owned_credential.close()


def _responses_history(
    messages: list[dict[str, Any]], identity: str | None = None
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for message in _history(messages, "azure_openai", identity):
        private = message.get("_provider")
        if private:
            # Preserve the exact order, including reasoning before calls and messages.
            items.extend(copy.deepcopy(private["output"]))
            continue
        content = message["content"]
        if isinstance(content, str):
            items.append({"role": message["role"], "content": content})
            continue
        texts: list[dict[str, str]] = []
        for block in content:
            kind = block["type"]
            if kind != "text" and texts:
                items.append({"role": message["role"], "content": texts})
                texts = []
            if kind == "text":
                texts.append(
                    {
                        "type": "output_text" if message["role"] == "assistant" else "input_text",
                        "text": block["text"],
                    }
                )
            elif kind == "tool_use":
                items.append(
                    {
                        "type": "function_call",
                        "call_id": block["id"],
                        "name": block["name"],
                        "arguments": json.dumps(block["input"]),
                    }
                )
            elif kind == "tool_result":
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": block["tool_use_id"],
                        "output": block["content"],
                    }
                )
            else:
                raise ProviderError("Provider-specific blocks are not portable history")
        if texts:
            items.append({"role": message["role"], "content": texts})
    return items


class AzureOpenAIProvider:
    """Azure OpenAI v1 Responses transport, with application-owned continuation."""

    name = "azure_openai"

    def __init__(
        self, client: Any, *, model: str | None = None, identity: str | None = None
    ) -> None:
        self.client = client
        self.model = model
        self.identity = identity
        self._owned_credential: Any = None

    def validate_config(self, config: Any, *, purpose: str = "chat") -> None:
        if config.enable_web_search:
            raise ProviderError("Azure OpenAI does not support Claude server web search")
        if config.thinking_effort is not None:
            raise ProviderError("Claude thinking settings are not supported by Azure OpenAI")
        if config.rolling_conversation_cache:
            raise ProviderError("Disable rolling_conversation_cache for Azure OpenAI")
        if purpose == "analysis" and config.analysis_use_code_execution:
            raise ProviderError("Disable analysis_use_code_execution for Azure OpenAI")

    def _request(self, request: RoundRequest) -> dict[str, Any]:
        if request.settings.get("container"):
            raise ProviderError("Claude code containers are not supported")
        thinking = request.settings.get("thinking", {})
        if thinking.get("type") not in (None, "disabled") or request.settings.get("output_config"):
            raise ProviderError("Claude reasoning settings are not supported")
        if set(request.settings) - {"thinking"}:
            raise ProviderError("Unsupported provider request settings")
        tools = []
        for tool in request.tools:
            if tool.get("type") or tool.get("allowed_callers"):
                raise ProviderError("Only portable client function tools are supported")
            tools.append(
                {
                    "type": "function",
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": tool["input_schema"],
                    "strict": False,
                }
            )
        choice = request.tool_choice
        tool_choice: Any = (
            {"type": "function", "name": choice["name"]}
            if choice["type"] == "tool"
            else choice["type"]
        )
        return {
            "model": self.model or request.model,
            "max_output_tokens": request.max_tokens,
            "instructions": "\n\n".join(block["text"] for block in request.system),
            "input": _responses_history(request.messages, self.identity),
            "tools": tools,
            "tool_choice": tool_choice,
            "store": False,
            "include": ["reasoning.encrypted_content"],
        }

    @asynccontextmanager
    async def stream(self, request: RoundRequest) -> AsyncIterator[ProviderStream]:
        stream = await self.client.responses.create(**self._request(request), stream=True)
        try:
            yield _ResponsesStream(stream, self.identity)
        finally:
            await stream.close()

    async def complete(self, request: RoundRequest) -> RoundResponse:
        response = await self.client.responses.create(**self._request(request))
        final = _responses_response(response)
        if self.identity is not None:
            final.continuation["identity"] = self.identity
        return final

    async def aclose(self) -> None:
        try:
            await self.client.close()
        finally:
            if self._owned_credential is not None:
                await self._owned_credential.close()


def _responses_response(response: Any) -> RoundResponse:
    if response.status not in ("completed",):
        raise ProviderError(f"Responses round did not complete: {response.status}")
    output = [_dump(item) for item in response.output]
    content: list[Block] = []
    ids: set[str] = set()
    refused = False
    for item in output:
        if item["type"] == "function_call":
            call_id = item["call_id"]
            if call_id in ids or item.get("status", "completed") != "completed":
                raise ProviderError("Duplicate or incomplete function call")
            ids.add(call_id)
            content.append(
                Block(
                    "tool_use",
                    id=call_id,
                    name=item["name"],
                    input=_arguments(item["arguments"]),
                )
            )
        elif item["type"] == "message":
            for block in item["content"]:
                if block["type"] == "output_text":
                    content.append(Block("text", text=block["text"]))
                elif block["type"] == "refusal":
                    refused = True
                    content.append(Block("text", text=block["refusal"]))
        elif item["type"] != "reasoning":
            raise ProviderError("Unsupported Responses output item")
    if refused and ids:
        raise ProviderError("A refused response cannot dispatch function calls")
    counts = getattr(response, "usage", None)
    cached = getattr(getattr(counts, "input_tokens_details", None), "cached_tokens", 0) or 0
    return RoundResponse(
        content,
        "refusal" if refused else "tool_use" if ids else "end_turn",
        Usage(
            input_tokens=max(0, (getattr(counts, "input_tokens", 0) or 0) - cached),
            output_tokens=getattr(counts, "output_tokens", 0) or 0,
            cache_read_input_tokens=cached,
        ),
        {"name": "azure_openai", "output": output},
    )


class _ResponsesStream:
    def __init__(self, stream: Any, identity: str | None = None) -> None:
        self._stream = stream
        self._final: RoundResponse | None = None
        self._identity = identity

    async def __aiter__(self) -> AsyncIterator[StreamEvent]:
        ids: set[str] = set()
        calls: dict[int, tuple[str, str]] = {}
        buffers: dict[int, str] = {}
        done: set[int] = set()
        async for event in self._stream:
            kind = event.type
            index = getattr(event, "output_index", 0)
            if kind == "response.output_item.added" and event.item.type == "function_call":
                if index in calls or event.item.call_id in ids:
                    raise ProviderError("Duplicate function call")
                ids.add(event.item.call_id)
                calls[index] = (event.item.call_id, event.item.name)
                buffers[index] = ""
                yield StreamEvent("tool_start", index, id=event.item.call_id, name=event.item.name)
            elif kind == "response.function_call_arguments.delta":
                if index not in calls or index in done:
                    raise ProviderError("Arguments for an unknown or closed function call")
                buffers[index] += event.delta
                yield StreamEvent("arguments", index, text=event.delta)
            elif kind in ("response.output_text.delta", "response.refusal.delta"):
                yield StreamEvent("text", index, text=event.delta)
            elif kind == "response.output_item.done" and event.item.type == "function_call":
                if index not in calls or index in done:
                    raise ProviderError("Duplicate or unknown completed function call")
                call_id, name = calls[index]
                if (event.item.call_id, event.item.name) != (call_id, name):
                    raise ProviderError("Function identity changed while streaming")
                args = event.item.arguments
                if getattr(event.item, "status", "completed") != "completed":
                    raise ProviderError("Incomplete streamed function call")
                _arguments(args)
                if buffers[index] and buffers[index] != args:
                    raise ProviderError("Function arguments changed after streaming")
                if not buffers[index]:
                    buffers[index] = args
                    yield StreamEvent("arguments", index, text=args)
                # Defer dispatch until response.completed validates the entire round.
                done.add(index)
            elif kind == "response.completed":
                final = _responses_response(event.response)
                final_calls = [block for block in final.content if block.type == "tool_use"]
                if len(final_calls) != len(calls) or done != set(calls):
                    raise ProviderError("Incomplete streamed function calls")
                expected = {
                    call_id: (name, _arguments(buffers[index]))
                    for index, (call_id, name) in calls.items()
                }
                if any(expected.get(b.id) != (b.name, b.input) for b in final_calls):
                    raise ProviderError("Final function calls differ from streamed calls")
                self._final = final
                if self._identity is not None:
                    final.continuation["identity"] = self._identity
                yield StreamEvent("usage", usage=final.usage)
                for index in calls:
                    yield StreamEvent("tool_end", index)
                return
            elif kind in ("error", "response.failed", "response.incomplete"):
                raise ProviderError("Responses stream failed or was incomplete")
        raise ProviderError("Responses stream ended without a completed response")

    async def get_final_message(self) -> RoundResponse:
        if self._final is None:
            raise ProviderError("Responses stream has not completed")
        return self._final


def foundry_claude_provider(
    *, endpoint: str, credential: Any, model: str | None = None, timeout: float = 120
) -> ClaudeProvider:
    """Use a refreshing asynchronous Entra credential; never capture a token string."""
    from anthropic import AsyncAnthropicFoundry
    from azure.identity.aio import get_bearer_token_provider

    token_provider = get_bearer_token_provider(credential, "https://ai.azure.com/.default")
    return ClaudeProvider(
        AsyncAnthropicFoundry(
            base_url=endpoint, azure_ad_token_provider=token_provider, timeout=timeout
        ),
        model=model,
        supports_code_execution=False,
    )


def azure_openai_provider(
    *, endpoint: str, credential: Any, model: str | None = None, timeout: float = 120
) -> AzureOpenAIProvider:
    """Azure v1 endpoint, for example ``https://RESOURCE.openai.azure.com/openai/v1/``."""
    from azure.identity.aio import get_bearer_token_provider
    from openai import AsyncOpenAI

    token_provider = get_bearer_token_provider(
        credential, "https://cognitiveservices.azure.com/.default"
    )
    return AzureOpenAIProvider(
        AsyncOpenAI(base_url=endpoint, api_key=token_provider, timeout=timeout), model=model
    )


def _environment_selection(
    env: Mapping[str, str], role: str, purpose: str
) -> tuple[str, str | None, str]:
    prefix = f"COMMERCE_{role.upper()}_{purpose.upper()}_"
    mode = env.get(prefix + "PROVIDER", env.get("COMMERCE_PROVIDER", "claude_foundry"))
    model = env.get(prefix + "MODEL", env.get("COMMERCE_MODEL"))
    endpoint_key = {
        "azure_openai": "AZURE_OPENAI_ENDPOINT",
        "claude_foundry": "ANTHROPIC_FOUNDRY_BASE_URL",
        "anthropic": "ANTHROPIC_BASE_URL",
    }.get(mode, "")
    endpoint = env.get(prefix + "ENDPOINT", env.get(endpoint_key, ""))
    if mode == "anthropic" and not endpoint:
        endpoint = "https://api.anthropic.com"
    endpoint = endpoint.rstrip("/")
    if mode == "azure_openai" and endpoint and urlsplit(endpoint).path == "":
        endpoint += "/openai/v1"
    return mode, model, endpoint


def provider_configuration_id(environ: Mapping[str, str] | None = None) -> str:
    """Fingerprint all resolved role/purpose transports, excluding credentials and ids.

    Compute this from the deployment environment before setting COMMERCE_PROVIDER_ID.
    A single deployment-wide fingerprint lets the host pin both role sessions even
    when chat, extraction and analysis select different providers or models.
    """
    env = os.environ if environ is None else environ
    selections = {
        f"{role}.{purpose}": _environment_selection(env, role, purpose)
        for role in ("shopping", "merchant")
        for purpose in ("chat", "extraction", "analysis")
    }
    encoded = json.dumps(selections, sort_keys=True, separators=(",", ":")).encode()
    return "providers-v1:" + hashlib.sha256(encoded).hexdigest()


def create_provider_from_env(
    *,
    role: Literal["shopping", "merchant"],
    purpose: Literal["chat", "extraction", "analysis"],
    credential: Any = None,
    environ: Mapping[str, str] | None = None,
) -> ClaudeProvider | AzureOpenAIProvider:
    """Build a purpose-specific production transport without making a network call.

    The host owns a supplied async Entra credential. With none supplied this factory
    owns DefaultAzureCredential; close the returned provider at application shutdown.
    """
    from anthropic import AsyncAnthropic

    if role not in ("shopping", "merchant") or purpose not in ("chat", "extraction", "analysis"):
        raise ProviderError("Unknown provider role or purpose")
    env = os.environ if environ is None else environ
    prefix = f"COMMERCE_{role.upper()}_{purpose.upper()}_"
    mode, model, endpoint = _environment_selection(env, role, purpose)
    if not model:
        raise ProviderError(f"{prefix}MODEL or COMMERCE_MODEL is required")
    identity = provider_configuration_id(env)
    if env.get("COMMERCE_PROVIDER_ID", identity) != identity:
        raise ProviderError("COMMERCE_PROVIDER_ID differs from the actual provider configuration")
    try:
        timeout = float(env.get("COMMERCE_MODEL_TIMEOUT_S", "120"))
        if not 0 < timeout <= 600:
            raise ValueError
    except ValueError as exc:
        raise ProviderError("COMMERCE_MODEL_TIMEOUT_S must be in (0, 600]") from exc
    if mode not in ("claude_foundry", "azure_openai", "anthropic"):
        raise ProviderError("Provider must be claude_foundry, azure_openai or anthropic")
    endpoint_key = {
        "azure_openai": "AZURE_OPENAI_ENDPOINT",
        "claude_foundry": "ANTHROPIC_FOUNDRY_BASE_URL",
        "anthropic": "ANTHROPIC_BASE_URL",
    }[mode]
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ProviderError(f"{endpoint_key} must be an HTTPS endpoint without credentials")
    if mode == "anthropic":
        key = env.get("ANTHROPIC_API_KEY")
        if not key:
            raise ProviderError("Direct Anthropic mode requires ANTHROPIC_API_KEY")
        return ClaudeProvider(
            AsyncAnthropic(api_key=key, base_url=endpoint, timeout=timeout),
            model=model,
            identity=identity,
        )
    owned = credential is None
    if owned:
        from azure.identity.aio import DefaultAzureCredential

        credential = DefaultAzureCredential()
    factory = azure_openai_provider if mode == "azure_openai" else foundry_claude_provider
    provider = factory(endpoint=endpoint, credential=credential, model=model, timeout=timeout)
    provider.identity = identity
    if owned:
        provider._owned_credential = credential
    return provider
