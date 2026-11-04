"""Credential-free transport, safe-executor parity and continuation regressions."""

from __future__ import annotations

import asyncio
import copy
import importlib
import json
from types import SimpleNamespace as NS

import pytest

from commerce_common.providers import (
    AzureOpenAIProvider,
    ClaudeProvider,
    ProviderError,
    RoundRequest,
    create_provider_from_env,
    provider_configuration_id,
)
from commerce_common.streaming import AgentEvent, ToolOutcome
from commerce_common.testing import FakeBlock, FakeClient, text_message, tool_use_message
from commerce_common.turn import EagerDispatcher, StreamedRound, assistant_message, call_usage


def item(**kwargs):
    return FakeBlock(**kwargs)


def function(name="read", args='{"key": "value"}', call_id="call-1"):
    return item(
        type="function_call",
        id=f"fc-{call_id}",
        call_id=call_id,
        name=name,
        arguments=args,
        status="completed",
    )


def response(*output, status="completed"):
    return NS(
        status=status,
        output=list(output),
        usage=NS(input_tokens=20, output_tokens=7, input_tokens_details=NS(cached_tokens=5)),
    )


def text_output(text="Done", refusal=False):
    block = (
        {"type": "refusal", "refusal": text}
        if refusal
        else {
            "type": "output_text",
            "text": text,
            "annotations": [],
        }
    )
    return item(type="message", id="msg-1", role="assistant", content=[block], status="completed")


def script(final):
    events = []
    for index, output in enumerate(final.output):
        if output.type == "function_call":
            events.append(NS(type="response.output_item.added", output_index=index, item=output))
            args = output.arguments
            for delta in (args[: len(args) // 2], args[len(args) // 2 :]):
                events.append(
                    NS(
                        type="response.function_call_arguments.delta",
                        output_index=index,
                        delta=delta,
                    )
                )
            events.append(NS(type="response.output_item.done", output_index=index, item=output))
        elif output.type == "message":
            for block in output.content:
                refused = block["type"] == "refusal"
                events.append(
                    NS(
                        type="response.refusal.delta" if refused else "response.output_text.delta",
                        output_index=index,
                        delta=block["refusal" if refused else "text"],
                    )
                )
    events.append(NS(type=f"response.{final.status}", response=final))
    return events


class ScriptStream:
    def __init__(self, events):
        self.events = events
        self.closed = False

    async def __aiter__(self):
        for event in self.events:
            if isinstance(event, BaseException):
                raise event
            yield event

    async def close(self):
        self.closed = True


class ResponsesClient:
    def __init__(self, finals=(), *, events=None):
        self.finals = list(finals)
        self.events = events
        self.calls = []
        self.streams = []
        self.responses = NS(create=self.create)

    async def create(self, **body):
        self.calls.append(copy.deepcopy(body))
        if body.get("stream"):
            events = self.events if self.events is not None else script(self.finals.pop(0))
            stream = ScriptStream(events)
            self.streams.append(stream)
            return stream
        return self.finals.pop(0)


def request(messages=None, **extra):
    return RoundRequest.from_dict(
        {
            "model": "deployment",
            "max_tokens": 500,
            "system": [{"type": "text", "text": "rules"}],
            "messages": messages or [{"role": "user", "content": "hello"}],
            "tools": [
                {
                    "name": "read",
                    "description": "Read records",
                    "input_schema": {"type": "object", "properties": {"key": {"type": "string"}}},
                }
            ],
            **extra,
        }
    )


async def relay(provider, model_request=None, execute=None):
    seen = []

    async def default_execute(name, args):
        seen.append((name, args))
        return ToolOutcome("ok")

    dispatcher = EagerDispatcher(execute or default_execute, True)
    streamed = StreamedRound()
    try:
        async with provider.stream(model_request or request()) as stream:
            events = [
                event
                async for event in streamed.relay(
                    stream,
                    dispatcher,
                    lambda name, call_id, args: AgentEvent.tool_call(name, call_id, args),
                )
            ]
            final = await stream.get_final_message()
        calls = [b for b in final.content if b.type == "tool_use"]
        await dispatcher.collect(calls)
        return events, final, seen
    finally:
        dispatcher.cancel()


async def test_responses_round_preserves_ordered_opaque_items_without_server_storage():
    reasoning = item(type="reasoning", id="rs-1", summary=[], encrypted_content="opaque")
    client = ResponsesClient([response(reasoning, function(), text_output())])
    provider = AzureOpenAIProvider(client)
    events, final, seen = await relay(
        provider, request(tool_choice={"type": "tool", "name": "read"})
    )
    assert seen == [("read", {"key": "value"})]
    assert [event.type for event in events] == ["text_delta", "tool_call"]
    body = client.calls[0]
    assert body["store"] is False
    assert body["include"] == ["reasoning.encrypted_content"]
    assert body["tool_choice"] == {"type": "function", "name": "read"}
    assert "previous_response_id" not in body
    assert body["tools"][0]["parameters"]["type"] == "object"
    assert call_usage(final) == {
        "input_tokens": 15,
        "output_tokens": 7,
        "cache_read_input_tokens": 5,
        "cache_creation_input_tokens": 0,
    }
    history = [
        {"role": "user", "content": "hello"},
        assistant_message(final),
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call-1",
                    "content": "records",
                }
            ],
        },
    ]
    replay = provider._request(request(history))["input"]
    assert [b.get("type") for b in replay] == [
        None,
        "reasoning",
        "function_call",
        "message",
        "function_call_output",
    ]
    assert replay[1]["encrypted_content"] == "opaque"
    assert all(b["type"] in ("text", "tool_use") for b in history[1]["content"])
    assert client.streams[0].closed


@pytest.mark.parametrize("choice", ["auto", "none"])
async def test_responses_maps_auto_none_refusal_and_usage(choice):
    client = ResponsesClient([response(text_output("Cannot do that", refusal=True))])
    events, final, _ = await relay(
        AzureOpenAIProvider(client), request(tool_choice={"type": choice})
    )
    assert final.stop_reason == "refusal"
    assert events[0].data["text"] == "Cannot do that"
    assert client.calls[0]["tool_choice"] == choice


@pytest.mark.parametrize("bad_args", ['{"key":', "[]", "null", '"string"'])
async def test_malformed_arguments_never_dispatch(bad_args):
    client = ResponsesClient([response(function(args=bad_args))])
    seen = []

    async def execute(name, args):
        seen.append(name)
        return ToolOutcome("bad")

    with pytest.raises(ProviderError):
        await relay(AzureOpenAIProvider(client), execute=execute)
    assert seen == [] and client.streams[0].closed


@pytest.mark.parametrize("failure", ["incomplete", "failed", "error", "truncated", "duplicate"])
async def test_partial_failed_and_duplicate_calls_do_not_dispatch(failure):
    final = response(function())
    events = script(final)
    if failure == "truncated":
        events = events[:-1]
    elif failure == "duplicate":
        events.insert(1, events[0])
    elif failure == "error":
        events[-1] = NS(type="error", code="error")
    else:
        events[-1] = NS(type=f"response.{failure}", response=response(status=failure))
    client = ResponsesClient(events=events)
    seen = []

    async def execute(name, args):
        seen.append(name)
        return ToolOutcome("bad")

    with pytest.raises(ProviderError):
        await relay(AzureOpenAIProvider(client), execute=execute)
    assert not seen and client.streams[0].closed


async def test_changed_final_arguments_do_not_dispatch():
    events = script(response(function()))
    events[-1] = NS(type="response.completed", response=response(function(args='{"key":"other"}')))
    with pytest.raises(ProviderError, match="differ"):
        await relay(AzureOpenAIProvider(ResponsesClient(events=events)))


async def test_cancellation_closes_transport_and_cancels_same_dispatcher():
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def execute(name, args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    client = ResponsesClient([response(function())])
    pending = asyncio.create_task(relay(AzureOpenAIProvider(client), execute=execute))
    await started.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    await asyncio.wait_for(cancelled.wait(), 1)
    assert client.streams[0].closed


async def test_signed_claude_continuation_is_separate_and_provider_bound():
    final = text_message("hello")
    final.content.insert(0, item(type="thinking", thinking="private", signature="signed"))
    client = FakeClient([final, text_message("next")])
    provider = ClaudeProvider(client)
    _, completed, _ = await relay(provider)
    history = [{"role": "user", "content": "hello"}, assistant_message(completed)]
    assert history[1]["content"] == [{"type": "text", "text": "hello"}]
    assert history[1]["_provider"]["output"][0]["signature"] == "signed"
    history.append({"role": "user", "content": "again"})
    await relay(provider, request(history))
    assert client.calls[1]["messages"][1]["content"][0]["signature"] == "signed"
    with pytest.raises(ProviderError, match="cannot change"):
        AzureOpenAIProvider(ResponsesClient())._request(request(history))


def test_capability_guards_are_explicit():
    from merchant_agent import MerchantAgentConfig

    provider = AzureOpenAIProvider(ResponsesClient())
    base = MerchantAgentConfig(
        thinking_effort=None, rolling_conversation_cache=False, analysis_use_code_execution=False
    )
    provider.validate_config(base, purpose="analysis")
    for fields, purpose in [
        ({"enable_web_search": True}, "chat"),
        ({"thinking_effort": "low"}, "chat"),
        ({"rolling_conversation_cache": True}, "chat"),
        ({"analysis_use_code_execution": True}, "analysis"),
    ]:
        with pytest.raises(ProviderError):
            provider.validate_config(base.model_copy(update=fields), purpose=purpose)
    with pytest.raises(ProviderError, match="portable"):
        provider._request(request(tools=[{"type": "web_search_20250305", "name": "web_search"}]))


def extensions(vertical, role, backend):
    modules = {
        ("travel", "shopping"): [("itinerary", "build_itinerary_extension")],
        ("travel", "merchant"): [("occupancy", "build_occupancy_extension")],
        ("telecom", "shopping"): [("plan_matrix", "build_plan_matrix_extension")],
        ("telecom", "merchant"): [("plan_mix", "build_plan_mix_extension")],
        ("entertainment", "shopping"): [
            ("venue_map", "build_venue_map_extension"),
            ("hold_view", "build_hold_view_extension"),
        ],
        ("entertainment", "merchant"): [("event_pacing", "build_event_pacing_extension")],
    }
    return [
        getattr(importlib.import_module(f"{vertical}.api.{module}"), factory)(
            *([backend] if role == "merchant" else [])
        )
        for module, factory in modules.get((vertical, role), [])
    ]


@pytest.mark.parametrize("role", ["shopping", "merchant"])
@pytest.mark.parametrize("vertical", ["retail", "travel", "telecom", "entertainment"])
@pytest.mark.parametrize("transport", ["claude", "azure_openai"])
async def test_both_role_loops_keep_all_vertical_tool_sets(
    role,
    vertical,
    transport,
    backend,
    config,
    skills,
    session,
    state,
):
    from merchant_agent_runtime import MerchantAgent
    from shopping_agent_runtime import ShoppingAgent

    config = config.model_copy(
        update={
            "thinking_effort": None,
            "rolling_conversation_cache": False,
            "analysis_use_code_execution": False,
            "require_host_approval": True,
        }
    )
    read = "search_products" if role == "shopping" else "search_listings"
    args = {"query": "tent"}
    blocked = "add_to_cart" if role == "shopping" else "apply_change"
    blocked_args = (
        {"product_id": "unseen", "quantity": 1}
        if role == "shopping"
        else {
            "change_id": "unapproved",
        }
    )
    if transport == "claude":
        client = FakeClient(
            [
                tool_use_message(read, args),
                tool_use_message(blocked, blocked_args),
                text_message("Done"),
            ]
        )
        provider = ClaudeProvider(client)
    else:
        client = ResponsesClient(
            [
                response(function(read, json.dumps(args))),
                response(function(blocked, json.dumps(blocked_args))),
                response(text_output()),
            ]
        )
        provider = AzureOpenAIProvider(client)
    extra = extensions(vertical, role, backend)
    cls = ShoppingAgent if role == "shopping" else MerchantAgent
    agent = cls(
        backend=backend,
        config=config,
        skills=skills,
        provider=provider,
        extra_presentation_tools=extra,
    )
    messages = [{"role": "user", "content": "Find a tent"}]
    events = [e async for e in agent.stream_turn(messages, session, state)]
    assert events[-1].type == "turn_complete"
    results = [e for e in events if e.type == "tool_result"]
    assert len(results) == 2 and results[1].data["status"] == "blocked"
    definitions = client.calls[0]["tools"]
    assert {e.name for e in extra} <= {tool["name"] for tool in definitions}
    assert messages[1]["_provider"]["name"] == transport
    if role == "shopping":
        assert not backend.cart_items
    else:
        assert not state.approved_change_ids


@pytest.mark.parametrize("role", ["shopping", "merchant"])
async def test_grounding_and_iteration_cap_use_provider_tool_choice(
    role,
    backend,
    config,
    skills,
    session,
    state,
):
    from merchant_agent_runtime import MerchantAgent
    from shopping_agent_runtime import ShoppingAgent

    config = config.model_copy(
        update={
            "max_tool_iterations": 1,
            "thinking_effort": None,
            "rolling_conversation_cache": False,
        }
    )
    read = "search_policies" if role == "shopping" else "get_business_snapshot"
    client = ResponsesClient(
        [
            response(function(read, "{}")),
            response(text_output()),
        ]
    )
    cls = ShoppingAgent if role == "shopping" else MerchantAgent
    agent = cls(backend=backend, skills=skills, config=config, provider=AzureOpenAIProvider(client))
    text = "What is the return policy?" if role == "shopping" else "How is revenue performing?"
    events = [
        e async for e in agent.stream_turn([{"role": "user", "content": text}], session, state)
    ]
    assert client.calls[0]["tool_choice"] == {"type": "function", "name": read}
    assert client.calls[1]["tool_choice"] == "none"
    assert events[-1].type == "turn_complete"


@pytest.mark.parametrize("role", ["shopping", "merchant"])
async def test_extraction_uses_its_own_provider_and_only_portable_transcript(
    role,
    backend,
    config,
    skills,
    session,
):
    from commerce_common.memory import InMemoryMemoryStore
    from merchant_agent_runtime import MerchantAgent
    from shopping_agent_runtime import ShoppingAgent

    client = ResponsesClient(
        [
            response(
                function(
                    "record_fact",
                    json.dumps(
                        {"key": "size", "value": "prefers compact items", "category": "preference"}
                    ),
                )
            )
        ]
    )
    memory = InMemoryMemoryStore()
    cls = ShoppingAgent if role == "shopping" else MerchantAgent
    chat = FakeClient([])
    agent = cls(
        backend=backend,
        config=config,
        skills=skills,
        client=chat,
        memory_store=memory,
        extraction_provider=AzureOpenAIProvider(client, model="extraction-deployment"),
    )
    messages = [
        {"role": "user", "content": "I prefer compact items."},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": "read", "id": "tool", "input": {}}],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "tool", "content": "SECRET TOOL DATA"}
            ],
        },
        {"role": "assistant", "content": [{"type": "text", "text": "Understood."}]},
    ]
    facts = await agent.update_memory(messages, session)
    assert [fact.key for fact in facts] == ["size"]
    assert client.calls[0]["model"] == "extraction-deployment"
    assert "SECRET TOOL DATA" not in json.dumps(client.calls[0])
    assert not chat.calls


@pytest.mark.parametrize("role", ["merchant"])
async def test_portable_analysis_uses_its_purpose_model_read_executor_and_usage(
    role,
    backend,
    config,
    session,
    state,
):
    from commerce_common.delegation import DelegationContext
    from commerce_common.turn import usage_totals
    from merchant_agent.analysis import SUBMIT_ANALYSIS_TOOL
    from merchant_agent_runtime.analysis import AnalysisRunner

    config = config.model_copy(
        update={
            "thinking_effort": None,
            "rolling_conversation_cache": False,
            "analysis_use_code_execution": False,
            "enable_analysis": True,
        }
    )
    submission = {
        "question": "What changed?",
        "headline": "Snapshot reviewed",
        "findings": ["The snapshot provides the current performance."],
    }
    client = ResponsesClient(
        [
            response(function("get_business_snapshot", "{}")),
            response(function(SUBMIT_ANALYSIS_TOOL, json.dumps(submission))),
        ]
    )
    runner = AnalysisRunner(
        client=None,
        backend=backend,
        config=config,
        provider=AzureOpenAIProvider(client, model="analysis-deployment"),
    )
    usage = usage_totals()
    context = DelegationContext(
        backend=backend,
        config=config,
        session=session,
        state=state,
        usage=usage,
    )
    result = await runner.run(context, {"question": "What changed?"})
    assert result.headline == "Snapshot reviewed"
    assert usage["input_tokens"] == 30 and usage["cache_read_input_tokens"] == 10
    assert all(call["model"] == "analysis-deployment" for call in client.calls)
    assert not state.seen_listings
    assert any(item.get("type") == "function_call_output" for item in client.calls[1]["input"])


async def test_real_openai_sdk_streams_and_replays_stateless_responses():
    import httpx
    from openai import AsyncOpenAI

    final = {
        "id": "resp-1",
        "object": "response",
        "created_at": 1,
        "model": "deployment",
        "status": "completed",
        "output": [
            {"type": "reasoning", "id": "rs-1", "summary": [], "encrypted_content": "opaque"},
            function().model_dump(),
        ],
        "usage": {
            "input_tokens": 20,
            "output_tokens": 7,
            "total_tokens": 27,
            "input_tokens_details": {"cached_tokens": 5},
            "output_tokens_details": {"reasoning_tokens": 1},
        },
    }
    events = [
        {"type": "response.output_item.added", "output_index": 1, "item": final["output"][1]},
        {
            "type": "response.function_call_arguments.delta",
            "output_index": 1,
            "item_id": "fc-call-1",
            "delta": '{"key": "value"}',
        },
        {
            "type": "response.function_call_arguments.done",
            "output_index": 1,
            "item_id": "fc-call-1",
            "arguments": '{"key": "value"}',
        },
        {"type": "response.output_item.done", "output_index": 1, "item": final["output"][1]},
        {"type": "response.completed", "response": final},
    ]
    bodies = []

    def handle(http_request):
        assert str(http_request.url) == "https://test.openai.azure.com/openai/v1/responses"
        bodies.append(json.loads(http_request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content="".join(f"data: {json.dumps(event)}\n\n" for event in events).encode(),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        sdk = AsyncOpenAI(
            base_url="https://test.openai.azure.com/openai/v1/",
            api_key="test",
            http_client=http,
        )
        provider = AzureOpenAIProvider(sdk)
        _, completed, seen = await relay(provider)
        assert seen == [("read", {"key": "value"})]
        assert completed.continuation["output"][0]["encrypted_content"] == "opaque"
        assert bodies[0]["store"] is False
        assert "previous_response_id" not in bodies[0]
        await sdk.close()


@pytest.mark.parametrize("mode", ["claude_foundry", "azure_openai"])
async def test_factory_constructs_owned_async_credentials_without_network(mode, monkeypatch):
    import socket

    import httpx
    from azure.identity.aio import DefaultAzureCredential

    def no_socket_connection(*args, **kwargs):
        raise AssertionError("Credential construction must not connect to a network")

    async def no_http_request(*args, **kwargs):
        raise AssertionError("Provider construction must not issue an HTTP request")

    async def no_token_request(*args, **kwargs):
        raise AssertionError("Provider construction must not acquire an Entra token")

    monkeypatch.setattr(socket.socket, "connect", no_socket_connection)
    monkeypatch.setattr(httpx.AsyncClient, "send", no_http_request)
    monkeypatch.setattr(DefaultAzureCredential, "get_token", no_token_request)
    env = {
        "COMMERCE_PROVIDER": mode,
        "COMMERCE_MODEL": "credential-construction-deployment",
        "ANTHROPIC_FOUNDRY_BASE_URL": "https://foundry.example/anthropic/",
        "AZURE_OPENAI_ENDPOINT": "https://openai.example/openai/v1/",
    }
    env["COMMERCE_PROVIDER_ID"] = provider_configuration_id(env)
    provider = create_provider_from_env(role="shopping", purpose="chat", environ=env)
    try:
        assert isinstance(provider._owned_credential, DefaultAzureCredential)
        assert provider.identity == env["COMMERCE_PROVIDER_ID"]
        assert provider.model == "credential-construction-deployment"
        assert isinstance(
            provider, ClaudeProvider if mode == "claude_foundry" else AzureOpenAIProvider
        )
    finally:
        await provider.aclose()


async def test_foundry_factory_uses_refreshing_entra_ai_scope_without_network():
    import time

    from azure.core.credentials import AccessToken

    class Credential:
        def __init__(self):
            self.scopes = []

        async def get_token(self, *scopes, **kwargs):
            self.scopes.append(scopes)
            return AccessToken(f"token-{len(self.scopes)}", int(time.time()) + 1)

    credential = Credential()
    provider = create_provider_from_env(
        role="shopping",
        purpose="chat",
        credential=credential,
        environ={
            "COMMERCE_PROVIDER": "claude_foundry",
            "COMMERCE_MODEL": "claude-deployment",
            "ANTHROPIC_FOUNDRY_BASE_URL": "https://test.services.ai.azure.com/anthropic/",
        },
    )
    assert not credential.scopes
    callback = provider.client._azure_ad_token_provider
    assert await callback() == "token-1"
    assert await callback() == "token-2"
    assert credential.scopes == [("https://ai.azure.com/.default",)] * 2
    await provider.aclose()


def test_factory_purpose_overrides_and_identity(monkeypatch):
    import commerce_common.providers as providers

    captured = []

    def factory(**kwargs):
        captured.append(kwargs)
        return AzureOpenAIProvider(ResponsesClient(), model=kwargs["model"])

    monkeypatch.setattr(providers, "azure_openai_provider", factory)
    env = {
        "COMMERCE_PROVIDER": "claude_foundry",
        "COMMERCE_MODEL": "chat",
        "COMMERCE_MERCHANT_ANALYSIS_PROVIDER": "azure_openai",
        "COMMERCE_MERCHANT_ANALYSIS_MODEL": "analysis",
        "AZURE_OPENAI_ENDPOINT": "https://test.openai.azure.com",
    }
    env["COMMERCE_PROVIDER_ID"] = provider_configuration_id(env)
    provider = create_provider_from_env(
        role="merchant",
        purpose="analysis",
        credential=object(),
        environ=env,
    )
    assert provider.model == "analysis" and provider.identity == env["COMMERCE_PROVIDER_ID"]
    assert captured[0]["endpoint"] == "https://test.openai.azure.com/openai/v1"
    with pytest.raises(ProviderError, match="identity"):
        provider._request(
            request(
                [
                    {
                        "role": "assistant",
                        "content": [],
                        "_provider": {
                            "name": "azure_openai",
                            "identity": "deployment-v0",
                            "output": [],
                        },
                    }
                ]
            )
        )
    with pytest.raises(ProviderError, match="actual provider configuration"):
        create_provider_from_env(
            role="merchant",
            purpose="analysis",
            credential=object(),
            environ=env | {"COMMERCE_MODEL": "changed-chat-model"},
        )


@pytest.mark.parametrize("role", ["shopping", "merchant"])
async def test_strict_memory_failures_reach_durable_worker(
    role,
    backend,
    config,
    skills,
    session,
):
    from commerce_common.memory import InMemoryMemoryStore
    from merchant_agent_runtime import MerchantAgent
    from shopping_agent_runtime import ShoppingAgent

    class FailingProvider(AzureOpenAIProvider):
        async def complete(self, request):
            raise RuntimeError("model unavailable")

    cls = ShoppingAgent if role == "shopping" else MerchantAgent
    agent = cls(
        backend=backend,
        config=config,
        skills=skills,
        client=FakeClient([]),
        memory_store=InMemoryMemoryStore(),
        extraction_provider=FailingProvider(None),
    )
    messages = [{"role": "user", "content": "I prefer compact."}]
    assert await agent.update_memory(messages, session) == []
    with pytest.raises(RuntimeError, match="model unavailable"):
        await agent.update_memory(messages, session, strict=True)


@pytest.mark.parametrize("role", ["shopping"])
async def test_responses_presentation_partials_and_chips_use_existing_renderer(
    role,
    backend,
    config,
    skills,
    session,
    state,
):
    from shopping_agent_runtime import ShoppingAgent

    guide = {
        "title": "Tent setup",
        "sections": [{"heading": "Choose a site", "body": "Find a flat site."}],
    }
    client = ResponsesClient(
        [
            response(
                function("present_guide", json.dumps(guide), "guide"),
                function("present_suggestions", '{"suggestions":["Show tents"]}', "chips"),
            )
        ]
    )
    agent = ShoppingAgent(
        backend=backend,
        skills=skills,
        config=config.model_copy(
            update={"rolling_conversation_cache": False, "thinking_effort": None}
        ),
        provider=AzureOpenAIProvider(client),
    )
    messages = [{"role": "user", "content": "Help set up a tent"}]
    events = [event async for event in agent.stream_turn(messages, session, state)]
    assert any(
        event.type == "ui_partial" and event.data["component"] == "guide" for event in events
    )
    assert [event.data["component"] for event in events if event.type == "ui"] == [
        "guide",
        "suggestions",
    ]
    assert len(client.calls) == 1
    assert messages[-1]["content"][0]["type"] == "tool_result"


@pytest.mark.parametrize("role", ["merchant"])
async def test_responses_staging_renders_preview_without_host_approval(
    role,
    backend,
    config,
    skills,
    session,
    state,
):
    from merchant_agent_runtime import MerchantAgent

    config = config.model_copy(
        update={
            "rolling_conversation_cache": False,
            "thinking_effort": None,
            "require_host_approval": True,
        }
    )
    client = ResponsesClient(
        [
            response(function("search_listings", '{"query":"planter"}', "read")),
            response(
                function(
                    "stage_inventory_action",
                    '{"items":[{"listing_id":"L-202","action":"restock","quantity":24}]}',
                    "stage",
                )
            ),
            response(
                function("present_suggestions", '{"suggestions":["Review changes"]}', "chips")
            ),
        ]
    )
    agent = MerchantAgent(
        backend=backend,
        skills=skills,
        config=config,
        provider=AzureOpenAIProvider(client),
    )
    events = [
        event
        async for event in agent.stream_turn(
            [{"role": "user", "content": "Restock the planter by 24"}], session, state
        )
    ]
    assert any(event.type == "change_update" for event in events)
    preview = next(event for event in events if event.data.get("component") == "change_preview")
    assert preview.data["stream_id"] == "stage"
    assert state.seen_changes and not state.approved_change_ids
    assert all(change.status.value == "staged" for change in state.seen_changes.values())


@pytest.mark.parametrize("role", ["merchant"])
async def test_responses_change_followthrough_keeps_one_host_reminder(
    role,
    backend,
    config,
    skills,
    session,
    state,
):
    from merchant_agent.gates import STAGING_FOLLOWTHROUGH_REMINDER
    from merchant_agent_runtime import MerchantAgent

    client = ResponsesClient(
        [response(text_output("I can help.")), response(text_output("Review it."))]
    )
    agent = MerchantAgent(
        backend=backend,
        skills=skills,
        config=config.model_copy(
            update={"rolling_conversation_cache": False, "thinking_effort": None}
        ),
        provider=AzureOpenAIProvider(client),
    )
    messages = [{"role": "user", "content": "Take the storage bins from $29 down to $26."}]
    _ = [event async for event in agent.stream_turn(messages, session, state)]
    assert len(client.calls) == 2
    assert (
        sum(
            block.get("text") == STAGING_FOLLOWTHROUGH_REMINDER
            for message in messages
            if isinstance(message["content"], list)
            for block in message["content"]
        )
        == 1
    )


@pytest.mark.parametrize("role", ["shopping", "merchant"])
@pytest.mark.parametrize("mode", ["claude_foundry", "azure_openai"])
async def test_production_runtime_factory_wires_and_pins_every_model_purpose(
    role,
    mode,
    backend,
    config,
    monkeypatch,
):
    """Exercise the host factory and real role constructors without an SDK/network call."""
    import os
    import sys
    from dataclasses import replace
    from types import ModuleType

    import azure.identity.aio
    import httpx

    import commerce_common.providers as providers
    from commerce_common.delegation import DelegationContext
    from commerce_common.testing import FakeCreateClient, create_response, tool_use_block
    from commerce_common.turn import usage_totals
    from merchant_agent.analysis import SUBMIT_ANALYSIS_TOOL

    runtime = pytest.importorskip("commerce_host.runtime")
    from commerce_host.config import Settings
    from commerce_host.identity import Principal
    from commerce_host.memory import CosmosMemoryStore
    from commerce_host.storage import FakeDocuments

    class Credential:
        closed = False

        async def get_token(self, *args, **kwargs):
            raise AssertionError("Integration tests must not acquire an Azure token")

        async def close(self):
            self.closed = True

    monkeypatch.setattr(azure.identity.aio, "DefaultAzureCredential", Credential)

    async def no_network(*args, **kwargs):
        raise AssertionError("Integration tests must not make HTTP requests")

    monkeypatch.setattr(httpx.AsyncClient, "send", no_network)
    for package in ("shopping_agent_runtime.orchestrator", "merchant_agent_runtime.orchestrator"):
        module = importlib.import_module(package)

        def no_default_client(*args, **kwargs):
            raise AssertionError("RuntimeFactory must not construct default AsyncAnthropic")

        monkeypatch.setattr(module, "AsyncAnthropic", no_default_client)
    for key in list(os.environ):
        if key.startswith("COMMERCE_"):
            monkeypatch.delenv(key)
    for key, value in {
        "COMMERCE_PROVIDER": mode,
        "COMMERCE_MODEL": "default-deployment",
        "ANTHROPIC_FOUNDRY_BASE_URL": "https://foundry.example/anthropic/",
        "AZURE_OPENAI_ENDPOINT": "https://openai.example/openai/v1/",
        "COMMERCE_SHOPPING_CHAT_MODEL": "shopping-chat",
        "COMMERCE_SHOPPING_EXTRACTION_MODEL": "shopping-extraction",
        "COMMERCE_MERCHANT_CHAT_MODEL": "merchant-chat",
        "COMMERCE_MERCHANT_EXTRACTION_MODEL": "merchant-extraction",
        "COMMERCE_MERCHANT_ANALYSIS_MODEL": "merchant-analysis",
    }.items():
        monkeypatch.setenv(key, value)
    identity = provider_configuration_id(os.environ)
    monkeypatch.setenv("COMMERCE_PROVIDER_ID", identity)

    fact = {"key": "size", "value": "prefers compact items", "category": "preference"}
    submission = {
        "question": "What is available?",
        "headline": "The analysis purpose is configured",
        "findings": ["The configured delegate completed its bounded submission."],
    }
    created = []

    def transport(**kwargs):
        model = kwargs["model"]
        if mode == "claude_foundry":
            if model.endswith("-chat"):
                client = FakeClient([text_message("Configured provider response.")])
            else:
                tool = SUBMIT_ANALYSIS_TOOL if model.endswith("-analysis") else "record_fact"
                payload = submission if model.endswith("-analysis") else fact
                client = FakeCreateClient([create_response(tool_use_block(tool, payload))])
            provider = ClaudeProvider(client, model=model, supports_code_execution=False)
        else:
            if model.endswith("-chat"):
                final = response(text_output("Configured provider response."))
            else:
                tool = SUBMIT_ANALYSIS_TOOL if model.endswith("-analysis") else "record_fact"
                payload = submission if model.endswith("-analysis") else fact
                final = response(function(tool, json.dumps(payload)))
            client = ResponsesClient([final])
            provider = AzureOpenAIProvider(client, model=model)

        async def close_client():
            return None

        client.close = close_client
        created.append(provider)
        return provider

    monkeypatch.setattr(
        providers,
        "foundry_claude_provider" if mode == "claude_foundry" else "azure_openai_provider",
        transport,
    )
    core_config = config.model_copy(
        update={
            "thinking_effort": None,
            "rolling_conversation_cache": False,
            **(
                {
                    "enable_analysis": True,
                    "analysis_use_code_execution": False,
                    "require_host_approval": True,
                }
                if role == "merchant"
                else {}
            ),
        }
    )
    integration = ModuleType("provider_contract_integration")
    bundle = runtime.BackendBundle(
        backend=backend,
        config=core_config,
        capabilities=frozenset(
            {"durable_backend", "conditional_approvals"}
            if role == "merchant"
            else {"durable_backend"}
        ),
    )
    integration.build = lambda settings, principal: bundle
    monkeypatch.setitem(sys.modules, integration.__name__, integration)
    settings = Settings(
        role=role,
        vertical="retail",
        mode="production",
        cosmos_endpoint="https://cosmos.example",
        cosmos_database="commerce",
        cosmos_container=role,
        issuer="https://issuer.example",
        audience="commerce-api",
        jwks_url="https://issuer.example/keys",
        tenants=frozenset({"tenant"}),
        provider_id=identity,
        backend_factory=f"{integration.__name__}:build",
    )
    principal = Principal("tenant", "user", role)
    memory = CosmosMemoryStore(FakeDocuments(), principal)
    factory = runtime.RuntimeFactory(settings)
    try:
        agent, state_type, returned_bundle = factory.build(principal, memory)
        assert returned_bundle is bundle
        assert issubclass(agent.executor_class, runtime.JournaledExecutor)
        assert agent.provider.identity == agent.extraction_provider.identity == identity
        assert agent.provider.model == f"{role}-chat"
        assert agent.extraction_provider.model == f"{role}-extraction"
        assert agent.client is None
        state = state_type()
        session = factory.context(principal, "provider-integration-session")
        messages = [{"role": "user", "content": "I prefer compact items."}]
        events = [event async for event in agent.stream_turn(messages, session, state)]
        assert events[-1].type == "turn_complete"
        assert messages[-1]["_provider"]["identity"] == settings.provider_id
        facts = await agent.update_memory(messages, session, strict=True)
        assert [saved.key for saved in facts] == ["size"]
        assert [saved.key for saved in await memory.get_facts(principal.subject)] == ["size"]
        if role == "merchant":
            (delegate,) = agent.delegates
            runner = delegate.run.__self__
            assert runner._provider.model == "merchant-analysis"
            assert runner._provider.identity == identity
            result = await delegate.run(
                DelegationContext(
                    backend=backend,
                    config=core_config,
                    session=session,
                    state=state,
                    usage=usage_totals(),
                ),
                {"question": "What is available?"},
            )
            assert result.headline == submission["headline"]
        assert len(created) == (3 if role == "merchant" else 2)
        for provider in created:
            assert provider.client.calls[0]["model"] == provider.model
        repeated_agent, _, _ = factory.build(principal, memory)
        assert repeated_agent.provider is agent.provider
        assert repeated_agent.extraction_provider is agent.extraction_provider
        assert len(created) == (3 if role == "merchant" else 2)
        with pytest.raises(ValueError):
            runtime.RuntimeFactory(replace(settings, provider_id="incorrect-deployment"))
        monkeypatch.setenv(f"COMMERCE_{role.upper()}_CHAT_MODEL", "changed-deployment")
        with pytest.raises(ValueError):
            runtime.RuntimeFactory(settings)
        with pytest.raises(ProviderError, match="actual provider configuration"):
            create_provider_from_env(role=role, purpose="chat", credential=Credential())
    finally:
        for provider in created:
            await provider.aclose()


@pytest.mark.parametrize("vertical", ["retail", "travel", "telecom", "entertainment"])
@pytest.mark.parametrize("role", ["shopping", "merchant"])
@pytest.mark.parametrize("mode", ["claude_foundry", "azure_openai"])
async def test_runtime_factory_vertical_provider_grounded_turn_matrix(
    vertical,
    role,
    mode,
    monkeypatch,
):
    """Run each real fixture backend through its actual role executor and grounding."""
    import os

    import azure.identity.aio
    import httpx

    import commerce_common.providers as providers
    from commerce_common.testing import FakeCreateClient

    runtime = pytest.importorskip("commerce_host.runtime")
    from commerce_host.config import Settings
    from commerce_host.identity import Principal
    from commerce_host.memory import CosmosMemoryStore
    from commerce_host.storage import FakeDocuments

    class Credential:
        async def get_token(self, *args, **kwargs):
            raise AssertionError("The provider matrix must not acquire tokens")

        async def close(self):
            return None

    monkeypatch.setattr(azure.identity.aio, "DefaultAzureCredential", Credential)

    async def no_network(*args, **kwargs):
        raise AssertionError("The provider matrix must not make HTTP requests")

    monkeypatch.setattr(httpx.AsyncClient, "send", no_network)
    for package in ("shopping_agent_runtime.orchestrator", "merchant_agent_runtime.orchestrator"):
        module = importlib.import_module(package)

        def no_default_client(*args, **kwargs):
            raise AssertionError("RuntimeFactory must supply the configured provider")

        monkeypatch.setattr(module, "AsyncAnthropic", no_default_client)
    for key in list(os.environ):
        if key.startswith("COMMERCE_"):
            monkeypatch.delenv(key)
    for key, value in {
        "COMMERCE_PROVIDER": mode,
        "COMMERCE_MODEL": "default-fixture-deployment",
        "ANTHROPIC_FOUNDRY_BASE_URL": "https://foundry.example/anthropic/",
        "AZURE_OPENAI_ENDPOINT": "https://openai.example/openai/v1/",
        f"COMMERCE_{role.upper()}_CHAT_MODEL": f"{role}-{vertical}-chat",
        f"COMMERCE_{role.upper()}_EXTRACTION_MODEL": f"{role}-{vertical}-extraction",
        f"COMMERCE_{role.upper()}_ANALYSIS_MODEL": f"{role}-{vertical}-analysis",
    }.items():
        monkeypatch.setenv(key, value)
    identity = provider_configuration_id(os.environ)
    monkeypatch.setenv("COMMERCE_PROVIDER_ID", identity)

    portable_fields = {
        "thinking_effort": None,
        "rolling_conversation_cache": False,
        "enable_web_search": False,
        **({"analysis_use_code_execution": False} if role == "merchant" else {}),
    }

    read = "search_policies" if role == "shopping" else "get_business_snapshot"
    arguments = {"query": "refund"} if role == "shopping" else {}
    created = []

    def transport(**kwargs):
        model = kwargs["model"]
        if mode == "claude_foundry":
            client = (
                FakeClient(
                    [
                        tool_use_message(read, arguments),
                        text_message("Grounded fixture answer."),
                    ]
                )
                if model.endswith("-chat")
                else FakeCreateClient([])
            )
            provider = ClaudeProvider(client, model=model, supports_code_execution=False)
        else:
            client = ResponsesClient(
                [
                    response(function(read, json.dumps(arguments))),
                    response(text_output("Grounded fixture answer.")),
                ]
                if model.endswith("-chat")
                else []
            )
            provider = AzureOpenAIProvider(client, model=model)

        async def close_client():
            return None

        client.close = close_client
        created.append(provider)
        return provider

    monkeypatch.setattr(
        providers,
        "foundry_claude_provider" if mode == "claude_foundry" else "azure_openai_provider",
        transport,
    )
    settings = Settings(
        role=role,
        vertical=vertical,
        mode="fixture",
        cosmos_endpoint="https://cosmos.example",
        cosmos_database="commerce",
        cosmos_container=role,
        issuer="https://issuer.example",
        audience="commerce-api",
        jwks_url="https://issuer.example/keys",
        tenants=frozenset({"matrix-tenant"}),
        provider_id=identity,
        backend_factory="commerce_host.fixtures:build",
    )
    principal = Principal("matrix-tenant", f"{role}-{vertical}-subject", role)
    memory = CosmosMemoryStore(FakeDocuments(), principal)
    factory = runtime.RuntimeFactory(settings)
    try:
        agent, state_type, bundle = factory.build(principal, memory)
        assert all(
            getattr(agent.config, field) == value for field, value in portable_fields.items()
        ), f"{vertical}/{role}/{mode} requires portable fields {portable_fields}"
        context = factory.context(principal, f"{vertical}-{role}-provider-matrix")
        if role == "merchant":
            assert context.tenant_id == principal.tenant
            assert context.principal_scope == principal.scope
        if role == "shopping" and vertical == "entertainment":
            from entertainment.api.mock_ticketing import TicketingToolExecutor

            assert issubclass(agent.executor_class, TicketingToolExecutor)
        text = "What is your return policy?" if role == "shopping" else "How is revenue performing?"
        history = [{"role": "user", "content": text}]
        events = [event async for event in agent.stream_turn(history, context, state_type())]
        results = [event for event in events if event.type == "tool_result"]
        assert len(results) == 1
        assert results[0].data["tool"] == read
        assert not results[0].data["is_error"], results[0].data
        assert results[0].data["status"] == "ok"
        assert any(
            event.type == "text_delta" and event.data["text"] == "Grounded fixture answer."
            for event in events
        )
        assert events[-1].type == "turn_complete"
        assert events[-1].data["usage"]["input_tokens"] > 0
        assert len(agent.provider.client.calls) == 2
        assert agent.provider.client.calls[0]["tool_choice"] == {
            "type": "tool" if mode == "claude_foundry" else "function",
            "name": read,
        }
        assert {extension.name for extension in bundle.extensions} <= {
            tool["name"] for tool in agent.provider.client.calls[0]["tools"]
        }
        for message in history:
            if message["role"] == "assistant":
                assert message["_provider"]["identity"] == identity
        assert history[2]["content"][0]["type"] == "tool_result"
        assert not history[2]["content"][0]["is_error"]
    finally:
        for provider in created:
            await provider.aclose()
