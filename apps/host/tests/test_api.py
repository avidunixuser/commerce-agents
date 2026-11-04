import time
import uuid
from types import SimpleNamespace

import jwt
import pytest
from commerce_host.api import create_app
from commerce_host.config import Settings
from commerce_host.identity import Principal
from commerce_host.jobs import JobQueue
from commerce_host.storage import FakeDocuments
from commerce_host.worker import Worker
from fastapi.testclient import TestClient
from pydantic import BaseModel

from commerce_common.streaming import AgentEvent


class State(BaseModel):
    seen: list[str] = []


class ScriptedAgent:
    memory = SimpleNamespace(enabled=False)

    async def stream_turn(self, messages, context, state):
        state.seen.append("grounded-product")
        yield AgentEvent.text_delta("hello")
        messages.append({"role": "assistant", "content": "hello"})
        yield AgentEvent.turn_complete("end_turn", {"input_tokens": 2}, 1, 0)


class Factory:
    def build(self, principal, memory, journal=None):
        return ScriptedAgent(), State, SimpleNamespace()

    def context(self, principal, session_id):
        return SimpleNamespace(session_id=session_id)


class Verifier:
    def verify(self, token):
        if token != "verified":
            raise jwt.InvalidTokenError("invalid")
        return Principal("tenant", "user", "shopping"), {"exp": time.time() + 300}


@pytest.fixture
def settings():
    return Settings(
        role="shopping",
        vertical="retail",
        mode="production",
        cosmos_endpoint="https://cosmos.example",
        cosmos_database="commerce",
        cosmos_container="shopping",
        issuer="https://issuer.example",
        audience="commerce-api",
        jwks_url="https://issuer.example/keys",
        tenants=frozenset({"tenant"}),
        provider_id="test-provider",
        backend_factory="integration:factory",
        segment_seconds=1,
    )


def test_configuration_rejects_bad_endpoints_and_fixture_production(settings):
    from dataclasses import replace

    for fields in (
        {"cosmos_endpoint": "http://insecure"},
        {"backend_factory": "commerce_host.fixtures:build"},
        {"tenants": frozenset()},
        {"segment_seconds": 240},
    ):
        with pytest.raises(ValueError):
            replace(settings, **fields)


def test_api_never_downgrades_invalid_token_to_guest(settings):
    documents = FakeDocuments()
    with TestClient(create_app(settings, documents, Factory(), Verifier())) as client:
        assert client.post("/api/session").status_code == 401
        assert (
            client.post("/api/session", headers={"Authorization": "Bearer forged"}).status_code
            == 401
        )
        response = client.post(
            "/api/session",
            json={"user_id": "victim", "tenant": "other"},
            headers={"Authorization": "Bearer verified"},
        )
        assert response.status_code == 200
        records = [item for item in documents.items.values() if item["kind"] == "session"]
        assert records[0]["owner"] == Principal("tenant", "user", "shopping").scope
        assert (
            client.get(
                "/api/memory",
                headers={"Authorization": "Bearer verified", "X-Session-Id": "foreign"},
            ).status_code
            == 403
        )
        assert (
            client.get(
                "/api/merchant/overview", headers={"Authorization": "Bearer verified"}
            ).status_code
            == 404
        )


async def test_durable_worker_publishes_completion_only_with_committed_state(settings):
    documents = FakeDocuments()
    factory = Factory()
    app = create_app(settings, documents, factory, Verifier())
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer verified"}
        session_id = client.post("/api/session", headers=headers).json()["session_id"]
        headers.update({"X-Session-Id": session_id, "Idempotency-Key": str(uuid.uuid4())})
        accepted = client.post("/api/chat", json={"message": "hello"}, headers=headers)
        assert accepted.status_code == 200
        assert "turn_accepted" in accepted.text
        assert "reconnect" in accepted.text
        candidate = next(item for item in documents.items.values() if item["kind"] == "job")
        worker = Worker(settings, documents, factory)
        job = JobQueue(documents, worker.queue.owner).claim(candidate)
        await worker.execute(job)
        completed = documents.read(job["pk"], job["id"])
        state = documents.read(job["pk"], "session")
        assert completed["status"] == "complete"
        assert state["state"]["seen"] == ["grounded-product"]
        assert state["active_turn"] is None
        assert completed["events"] == 2
        replay = client.get(
            f"/api/turns/{job['turn_id']}/events",
            headers={**headers, "Last-Event-ID": f"{job['turn_id']}:1"},
        )
        assert "turn_complete" in replay.text
        assert "text_delta" not in replay.text
        duplicate = client.post("/api/chat", json={"message": "hello"}, headers=headers)
        assert duplicate.status_code == 200
        assert len([item for item in documents.items.values() if item["kind"] == "job"]) == 1
        assert (
            client.post("/api/chat", json={"message": "changed"}, headers=headers).status_code
            == 409
        )


async def test_cancelled_queued_turn_never_invokes_model(settings):
    documents = FakeDocuments()
    factory = Factory()
    with TestClient(create_app(settings, documents, factory, Verifier())) as client:
        headers = {"Authorization": "Bearer verified"}
        session_id = client.post("/api/session", headers=headers).json()["session_id"]
        turn_id = str(uuid.uuid4())
        headers.update({"X-Session-Id": session_id, "Idempotency-Key": turn_id})
        client.post("/api/chat", json={"message": "hello"}, headers=headers)
        assert client.post(f"/api/turns/{turn_id}/cancel", headers=headers).status_code == 200
        worker = Worker(settings, documents, factory)
        candidate = next(item for item in documents.items.values() if item["kind"] == "job")
        job = worker.queue.claim(candidate)
        await worker.execute(job)
        assert documents.read(job["pk"], job["id"])["status"] == "cancelled"
        assert documents.read(job["pk"], "session")["state"]["seen"] == []
        assert documents.read(job["pk"], "session")["active_turn"] is None
        assert documents.page(job["pk"], "event") == []


async def test_unresolved_effect_cannot_publish_success_or_unlock_session(settings):
    class AmbiguousAgent(ScriptedAgent):
        async def stream_turn(self, messages, context, state):
            self.journal.begin("backend", "arguments-hash")
            yield AgentEvent.turn_complete("end_turn", {}, 1, 0)

    class AmbiguousFactory(Factory):
        def build(self, principal, memory, journal=None):
            agent = AmbiguousAgent()
            agent.journal = journal
            return agent, State, SimpleNamespace()

    documents = FakeDocuments()
    factory = AmbiguousFactory()
    with TestClient(create_app(settings, documents, factory, Verifier())) as client:
        headers = {"Authorization": "Bearer verified"}
        session_id = client.post("/api/session", headers=headers).json()["session_id"]
        headers.update({"X-Session-Id": session_id, "Idempotency-Key": str(uuid.uuid4())})
        client.post("/api/chat", json={"message": "hello"}, headers=headers)
        worker = Worker(settings, documents, factory)
        candidate = next(item for item in documents.items.values() if item["kind"] == "job")
        job = worker.queue.claim(candidate)
        await worker.execute(job)
        assert documents.read(job["pk"], job["id"])["status"] == "uncertain"
        assert documents.read(job["pk"], "session")["active_turn"] == job["turn_id"]
        assert documents.page(job["pk"], "event") == []
        headers["Idempotency-Key"] = str(uuid.uuid4())
        assert (
            client.post("/api/chat", json={"message": "retry"}, headers=headers).status_code == 409
        )


def test_nonapprover_cannot_enqueue_merchant_action(settings):
    from dataclasses import replace

    class MerchantVerifier:
        def verify(self, token):
            return Principal("tenant", "operator", "merchant", frozenset({"merchant"})), {
                "exp": time.time() + 300,
            }

    documents = FakeDocuments()
    app = create_app(replace(settings, role="merchant"), documents, Factory(), MerchantVerifier())
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer verified"}
        session_id = client.post("/api/merchant/session", headers=headers).json()["session_id"]
        headers.update({"X-Session-Id": session_id, "Idempotency-Key": str(uuid.uuid4())})
        assert (
            client.post(
                "/api/merchant/changes/change-id/apply",
                headers=headers,
            ).status_code
            == 403
        )
        assert not any(item["kind"] == "job" for item in documents.items.values())


async def test_changed_approval_target_cannot_reach_executor(settings):
    from dataclasses import replace

    from commerce_host.api import principal_payload
    from commerce_host.jobs import job_operation
    from commerce_host.runtime import change_fingerprint
    from commerce_host.sessions import CosmosSessionStore

    from merchant_agent import MerchantSessionState

    class Change(BaseModel):
        change_id: str = "change-id"
        version: int = 1

    target = Change()
    accepted_fingerprint = change_fingerprint(target)
    target.version = 2

    class Backend:
        async def get_pending_changes(self, context):
            return [target]

    class MerchantFactory(Factory):
        def build(self, principal, memory, journal=None):
            return ScriptedAgent(), MerchantSessionState, SimpleNamespace(backend=Backend())

    settings = replace(settings, role="merchant")
    principal = Principal("tenant", "operator", "merchant", frozenset({"merchant", "approver"}))
    documents = FakeDocuments()
    store = CosmosSessionStore(documents)
    session = store.start(
        principal, settings.provider_id, MerchantSessionState().model_dump(mode="json")
    )
    session.document["active_turn"] = "approval"
    store.commit(
        session,
        [
            job_operation(
                "approval",
                {
                    "principal": principal_payload(principal),
                    "session_id": session.id,
                    "action": {
                        "tool": "apply_change",
                        "arguments": {"change_id": "change-id"},
                        "approved_until": time.time() + 300,
                        "target_fingerprint": accepted_fingerprint,
                    },
                },
                purpose="action",
            )
        ],
    )
    worker = Worker(settings, documents, MerchantFactory())
    job = worker.queue.claim(documents.read(session.pk, "job:approval"))
    await worker.execute(job)
    assert documents.read(session.pk, job["id"])["status"] == "failed"
    assert documents.page(session.pk, "effect") == []
    assert documents.read(session.pk, "session")["active_turn"] is None


async def test_verified_approval_uses_core_executor_and_replays_committed_result(
    settings, monkeypatch
):
    import asyncio
    from dataclasses import replace
    from pathlib import Path

    from commerce_host.fixtures import _storefront
    from commerce_host.runtime import RuntimeFactory

    from commerce_common.providers import provider_configuration_id
    from merchant_agent.types import ActorKind, ChangeItem, ChangeKind

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "examples"))
    monkeypatch.setenv("COMMERCE_PROVIDER", "claude_foundry")
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_BASE_URL", "https://models.example/anthropic/")
    monkeypatch.setenv("COMMERCE_MODEL", "test-model")
    settings = replace(
        settings,
        role="merchant",
        mode="fixture",
        backend_factory="commerce_host.fixtures:build",
        provider_id=provider_configuration_id(),
    )
    principal = Principal(
        "tenant", str(uuid.uuid4()), "merchant", frozenset({"merchant", "approver"})
    )

    class MerchantVerifier:
        def verify(self, token):
            return principal, {"exp": time.time() + 300}

    documents = FakeDocuments()
    factory = RuntimeFactory(settings)
    backend = factory.bundle(principal).backend
    listing = next(iter(_storefront("retail", principal.scope).products))
    change = backend.ledger.stage(
        kind=ChangeKind.INVENTORY_ACTION,
        summary="Pause the approved listing",
        items=[ChangeItem(target=listing, field="status", before="active", after="paused")],
        actor=principal.subject,
        actor_kind=ActorKind.AGENT,
    )
    with TestClient(create_app(settings, documents, factory, MerchantVerifier())) as client:
        headers = {"Authorization": "Bearer verified"}
        session_id = client.post("/api/merchant/session", headers=headers).json()["session_id"]
        headers.update({"X-Session-Id": session_id, "Idempotency-Key": str(uuid.uuid4())})
        path = f"/api/merchant/changes/{change.change_id}/apply"
        request = asyncio.create_task(asyncio.to_thread(client.post, path, headers=headers))
        candidate = None
        for _ in range(100):
            candidate = next(
                (item for item in documents.items.values() if item["kind"] == "job"), None
            )
            if candidate is not None:
                break
            await asyncio.sleep(0.01)
        assert candidate is not None
        worker = Worker(settings, documents, factory)
        job = worker.queue.claim(candidate)
        await worker.execute(job)
        response = await asyncio.wait_for(request, timeout=25)
        assert response.status_code == 200, response.text
        assert response.json()["ok"] is True
        assert response.json()["change"]["status"] == "applied"
        assert client.post(path, headers=headers).json() == response.json()
        committed = documents.read(job["pk"], "session")
        assert committed["active_turn"] is None
        assert not committed["state"]["approved_change_ids"]
        assert not committed["state"]["host_action_change_ids"]
        assert documents.read(job["pk"], job["id"])["pending_effects"] == 0
        assert len(backend.ledger.resolved()) == 1
