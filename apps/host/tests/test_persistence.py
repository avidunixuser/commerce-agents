import time

import pytest
from commerce_host.identity import Principal
from commerce_host.jobs import JobQueue, ToolJournal, job_operation
from commerce_host.memory import CosmosMemoryStore
from commerce_host.migration import migrate_memory
from commerce_host.sessions import CosmosSessionStore
from commerce_host.storage import (
    AccessDenied,
    CapacityError,
    ConflictError,
    FakeDocuments,
    Operation,
    partition,
)

from commerce_common.types import MemoryCategory, MemoryFact


@pytest.fixture
def principal():
    return Principal("tenant", "user", "shopping")


def fact(key="preference", value="blue"):
    return MemoryFact(key=key, value=value, category=MemoryCategory.PREFERENCE)


def test_transaction_rolls_back_every_operation():
    store = FakeDocuments()
    store.fail_at = 1
    with pytest.raises(ConflictError):
        store.transact("p", [Operation("create", "a", {}), Operation("create", "b", {})])
    assert store.read("p", "a") is None
    assert store.read("p", "b") is None


def test_etag_conflict_keeps_committed_transcript_and_state(principal):
    documents = FakeDocuments()
    store = CosmosSessionStore(documents)
    one = store.start(principal, "provider", {"seen": []})
    two = store.require(principal, one.id, "provider")
    one.messages.append({"role": "user", "content": "first"})
    one.document["state"] = {"seen": ["product"]}
    store.commit(one)
    two.messages.append({"role": "user", "content": "lost"})
    with pytest.raises(ConflictError):
        store.commit(two)
    loaded = store.require(principal, one.id, "provider")
    assert loaded.messages == one.messages
    assert loaded.document["state"] == {"seen": ["product"]}


def test_session_principal_tenant_and_provider_binding(principal):
    store = CosmosSessionStore(FakeDocuments())
    session = store.start(principal, "provider", {})
    for other in (
        Principal("other", "user", "shopping"),
        Principal("tenant", "other", "shopping"),
        Principal("tenant", "user", "merchant"),
    ):
        with pytest.raises(AccessDenied):
            store.require(other, session.id, "provider")
    with pytest.raises(ConflictError):
        store.require(principal, session.id, "changed")
    session.document["expires"] = time.time() - 1
    store.documents.transact(
        session.pk,
        [Operation("replace", "session", session.document, session.document["_etag"])],
    )
    with pytest.raises(AccessDenied):
        store.require(principal, session.id, "provider")


def test_transcript_rejects_oversized_message_without_manifest_change(principal):
    store = CosmosSessionStore(FakeDocuments())
    session = store.start(principal, "provider", {})
    session.messages.append({"role": "user", "content": "x" * 50000})
    with pytest.raises(CapacityError):
        store.commit(session)
    assert store.require(principal, session.id, "provider").messages == []


async def test_memory_purge_and_key_delete_fence_pending_extraction(principal):
    documents = FakeDocuments()
    one = CosmosMemoryStore(documents, principal)
    two = CosmosMemoryStore(documents, principal)
    await one.upsert_facts("user", [fact()])
    before_delete = await one.extraction_revision("user")
    await two.delete_fact("user", "preference")
    assert not await one.commit_extracted("user", [fact()], before_delete)
    before_clear = await one.extraction_revision("user")
    await two.clear("user")
    assert not await one.commit_extracted("user", [fact()], before_clear)
    assert await one.get_facts("user") == []
    assert await one.purge_generation("user") == 1


async def test_manual_correction_fences_extraction_and_subject_reads(principal):
    memory = CosmosMemoryStore(FakeDocuments(), principal)
    revision = await memory.extraction_revision("user")
    await memory.upsert_facts("user", [fact(value="green")])
    assert not await memory.commit_extracted("user", [fact()], revision)
    assert (await memory.get_facts("user"))[0].value == "green"
    with pytest.raises(AccessDenied):
        await memory.get_facts("another-user")


def test_job_and_session_acceptance_are_atomic(principal):
    documents = FakeDocuments()
    store = CosmosSessionStore(documents)
    session = store.start(principal, "provider", {})
    documents.fail_at = 1
    with pytest.raises(ConflictError):
        store.commit(session, [job_operation("turn", {"message": "hello"})])
    assert documents.read(session.pk, "job:turn") is None
    assert store.require(principal, session.id, "provider").document["version"] == 1


def test_worker_contention_events_and_expired_lease(principal):
    documents = FakeDocuments()
    session = CosmosSessionStore(documents).start(principal, "provider", {})
    documents.transact(session.pk, [job_operation("turn", {})])
    job = documents.read(session.pk, "job:turn")
    queue = JobQueue(documents, "one")
    running = queue.claim(job)
    with pytest.raises(ConflictError):
        JobQueue(documents, "two").claim(job)
    queue.append_event(running, "text_delta", {"text": "hello"})
    assert documents.read(session.pk, "job:turn")["events"] == 1
    current = documents.read(session.pk, "job:turn")
    documents.transact(
        session.pk,
        [Operation("replace", current["id"], dict(current, available=0), current["_etag"])],
    )
    with pytest.raises(ConflictError):
        JobQueue(documents, "two").claim(documents.read(session.pk, "job:turn"))
    assert documents.read(session.pk, "job:turn")["status"] == "uncertain"
    with pytest.raises(ConflictError):
        queue.append_event(running, "text_delta", {"text": "stale"})


def test_tool_journal_rejects_ambiguous_replay(principal):
    documents = FakeDocuments()
    session = CosmosSessionStore(documents).start(principal, "provider", {})
    documents.transact(session.pk, [job_operation("turn", {})])
    queue = JobQueue(documents, "one")
    job = queue.claim(documents.read(session.pk, "job:turn"))
    journal = ToolJournal(queue, job)
    assert journal.begin("tool", "hash") is None
    with pytest.raises(ConflictError):
        journal.begin("tool", "hash")
    journal.complete("tool", {"ok": True})
    assert journal.begin("tool", "hash") == {"ok": True}
    with pytest.raises(ConflictError):
        journal.begin("tool", "different")


async def test_migration_requires_consent_preserves_customer_and_revokes_guest(principal):
    documents = FakeDocuments()
    guest = Principal("tenant", "anonymous", "shopping", guest=True)
    documents.transact(
        partition("tenant", "guest", "anonymous"),
        [
            Operation(
                "create", "identity", {"epoch": 0, "revoked": False, "expires": time.time() + 86400}
            )
        ],
    )
    source = CosmosMemoryStore(documents, guest, retention_days=30)
    target = CosmosMemoryStore(documents, principal)
    await source.upsert_facts("anonymous", [fact(value="blue"), fact("size", "small")])
    await target.upsert_facts("user", [fact(value="green")])
    revision = await source.extraction_revision("anonymous")
    with pytest.raises(AccessDenied):
        await migrate_memory(source, target, consent=False)
    await migrate_memory(source, target, consent=True)
    assert {item.key: item.value for item in await target.get_facts("user")} == {
        "preference": "green",
        "size": "small",
    }
    assert not await source.commit_extracted("anonymous", [fact()], revision)
    with pytest.raises(AccessDenied):
        await source.get_facts("anonymous")
    await migrate_memory(source, target, consent=True)


async def test_fact_retention_is_per_update_and_durably_scrubbed(principal, monkeypatch):
    documents = FakeDocuments()
    memory = CosmosMemoryStore(documents, principal, retention_days=1)
    now = time.time()
    await memory.upsert_facts("user", [fact("first")])
    expiry = documents.read(memory.pk, "memory")["facts"]["first"]["expires"]
    monkeypatch.setattr(time, "time", lambda: now + 43200)
    await memory.upsert_facts("user", [fact("second")])
    assert documents.read(memory.pk, "memory")["facts"]["first"]["expires"] == expiry
    work = documents.page(memory.pk, "job")
    assert work and all(item["purpose"] == "memory_retention" for item in work)
    monkeypatch.setattr(time, "time", lambda: now + 86401)
    assert [item.key for item in await memory.get_facts("user")] == ["second"]
    memory.prune_expired()
    assert set(documents.read(memory.pk, "memory")["facts"]) == {"second"}
    assert any(item["available"] > time.time() for item in documents.page(memory.pk, "job"))


async def test_active_guest_facts_follow_identity_inactivity_not_fact_age(monkeypatch):
    documents = FakeDocuments()
    now = time.time()
    guest = Principal("tenant", "guest", "shopping", guest=True, guest_epoch=0)
    identity_pk = partition("tenant", "guest", "guest")
    documents.transact(
        identity_pk,
        [
            Operation(
                "create",
                "identity",
                {"epoch": 0, "revoked": False, "expires": now + 30 * 86400},
            )
        ],
    )
    memory = CosmosMemoryStore(documents, guest, retention_days=30)
    await memory.upsert_facts("guest", [fact()])
    monkeypatch.setattr(time, "time", lambda: now + 29 * 86400)
    identity = documents.read(identity_pk, "identity")
    documents.transact(
        identity_pk,
        [
            Operation(
                "replace",
                "identity",
                dict(identity, expires=now + 59 * 86400),
                identity["_etag"],
            )
        ],
    )
    assert await memory.get_facts("guest")
    assert documents.read(memory.pk, "memory")["expires"] == now + 59 * 86400
    monkeypatch.setattr(time, "time", lambda: now + 40 * 86400)
    assert await memory.get_facts("guest")
    monkeypatch.setattr(time, "time", lambda: now + 60 * 86400)
    with pytest.raises(AccessDenied):
        await memory.get_facts("guest")


def test_safe_job_recovery_fences_stale_workers_and_terminal_writes(principal):
    documents = FakeDocuments()
    session = CosmosSessionStore(documents).start(principal, "provider", {})
    documents.transact(session.pk, [job_operation("retention", {}, purpose="memory_retention")])
    old_queue = JobQueue(documents, "old")
    old = old_queue.claim(documents.read(session.pk, "job:retention"))
    current = documents.read(session.pk, old["id"])
    documents.transact(
        session.pk,
        [
            Operation(
                "replace",
                old["id"],
                dict(current, available=0),
                current["_etag"],
            )
        ],
    )
    new_queue = JobQueue(documents, "new")
    with pytest.raises(ConflictError):
        new_queue.claim(old)
    assert documents.read(session.pk, old["id"])["status"] == "ready"
    with pytest.raises(ConflictError):
        old_queue.finish(old, "complete")
    new = new_queue.claim(documents.read(session.pk, old["id"]))
    new_queue.finish(new, "complete")
    with pytest.raises(ConflictError):
        new_queue.finish(new, "failed")


def test_transcript_staging_cannot_follow_revocation_or_adopt_concurrent_state(principal):
    class RacingDocuments(FakeDocuments):
        racing = False

        def transact(self, pk, operations):
            super().transact(pk, operations)
            if self.racing and any(op.id.startswith("transcript:") for op in operations):
                current = self.read(pk, "session")
                super().transact(
                    pk,
                    [
                        Operation(
                            "replace",
                            "session",
                            dict(current, state={"other": "writer"}),
                            current["_etag"],
                        )
                    ],
                )

    documents = RacingDocuments()
    store = CosmosSessionStore(documents)
    session = store.start(principal, "provider", {})
    session.messages = [{"role": "user", "content": "hello"}]
    documents.racing = True
    with pytest.raises(ConflictError):
        store.commit(session)
    assert store.require(principal, session.id, "provider").document["state"] == {"other": "writer"}
    documents.racing = False
    current = store.require(principal, session.id, "provider")
    store.revoke(current)
    count = len(documents.page(session.pk, "transcript"))
    with pytest.raises(ConflictError):
        store.commit(session)
    assert len(documents.page(session.pk, "transcript")) == count


def test_cosmos_transport_uses_conditional_batch_and_fixed_expiry(monkeypatch):
    from commerce_host.storage import CosmosDocuments

    class Container:
        def execute_item_batch(self, **kwargs):
            self.last_batch = kwargs

        def query_items(self, **kwargs):
            self.last_query = kwargs
            return []

    container = Container()
    documents = CosmosDocuments(container)
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now)
    documents.transact(
        "principal-partition",
        [
            Operation(
                "replace",
                "state",
                {"expires": now + 100, "ttl": 999, "_etag": "old", "_ts": 123, "safe": True},
                "old",
            )
        ],
    )
    assert container.last_batch["partition_key"] == "principal-partition"
    kind, args, options = container.last_batch["batch_operations"][0]
    assert kind == "replace" and options == {"if_match_etag": "old"}
    assert args[1]["ttl"] == 100
    assert "_etag" not in args[1] and "_ts" not in args[1]
    documents.due_jobs(0, now, role="shopping", vertical="retail")
    query = container.last_query
    assert "c.payload.principal.role=@role" in query["query"]
    assert "c.payload.vertical=@vertical" in query["query"]
    assert {"name": "@limit", "value": 20} in query["parameters"]


async def test_customer_delete_fences_already_consented_migration(principal):
    documents = FakeDocuments()
    guest = Principal("tenant", "guest", "shopping", guest=True)
    documents.transact(
        partition("tenant", "guest", "guest"),
        [
            Operation(
                "create",
                "identity",
                {"epoch": 0, "revoked": False, "expires": time.time() + 86400},
            )
        ],
    )
    source, target = CosmosMemoryStore(documents, guest), CosmosMemoryStore(documents, principal)
    await source.upsert_facts("guest", [fact()])
    await target.upsert_facts("user", [fact(value="green")])
    await migrate_memory(
        source,
        target,
        consent=True,
        queued_work=job_operation("migration", {}, purpose="migration"),
    )
    await target.delete_fact("user", "preference")
    with pytest.raises(ConflictError):
        await migrate_memory(source, target, consent=True)
    assert await target.get_facts("user") == []


def test_operator_redrive_requires_permission_tenant_fence_and_safe_purpose(principal):
    documents = FakeDocuments()
    session = CosmosSessionStore(documents).start(principal, "provider", {})
    documents.transact(
        session.pk,
        [
            job_operation(
                "cleanup",
                {"principal": principal.payload()},
                purpose="purge",
            )
        ],
    )
    queue = JobQueue(documents, "worker")
    job = queue.claim(documents.read(session.pk, "job:cleanup"))
    queue.finish(job, "dead")
    dead = documents.read(session.pk, job["id"])
    with pytest.raises(AccessDenied):
        queue.redrive_safe(session.pk, job["id"], dead["_etag"], principal)
    operator = Principal("other-tenant", "operator", "merchant", frozenset({"operator"}))
    with pytest.raises(ConflictError):
        queue.redrive_safe(session.pk, job["id"], dead["_etag"], operator)
    operator = Principal("tenant", "operator", "merchant", frozenset({"operator"}))
    queue.redrive_safe(session.pk, job["id"], dead["_etag"], operator)
    assert documents.read(session.pk, job["id"])["status"] == "ready"
    with pytest.raises(ConflictError):
        queue.redrive_safe(session.pk, job["id"], dead["_etag"], operator)
    ready = documents.read(session.pk, job["id"])
    documents.transact(
        session.pk,
        [
            Operation(
                "replace",
                job["id"],
                dict(ready, purpose="action", status="uncertain"),
                ready["_etag"],
            )
        ],
    )
    uncertain = documents.read(session.pk, job["id"])
    with pytest.raises(ConflictError):
        queue.redrive_safe(session.pk, job["id"], uncertain["_etag"], operator)
