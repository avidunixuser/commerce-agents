"""Extraction must not overwrite edits, resurrect deletions or cross a purge."""

from datetime import timedelta
from types import SimpleNamespace

import pytest

from commerce_common.fencing import Fence
from commerce_common.memory import (
    ConditionalExtractionStore,
    InMemoryMemoryStore,
    RetentionMemoryStore,
    check_memory_store,
    extract_and_store,
)
from commerce_common.testing import extraction_client
from commerce_common.types import MemoryFact

FENCE = Fence(label="test_data", notice="Data.")


class AtomicSpy(InMemoryMemoryStore):
    def __init__(self):
        super().__init__()
        self.commits = []
        self.upserts = 0

    async def upsert_facts(self, subject_id, facts):
        self.upserts += 1
        await super().upsert_facts(subject_id, facts)

    async def commit_extracted(self, subject_id, facts, revision):
        self.commits.append((subject_id, facts, revision))
        return await super().commit_extracted(subject_id, facts, revision)


async def extract(store, before_call=None):
    return await extract_and_store(
        store,
        "subject",
        extraction_client(
            [{"key": "size", "value": "prefers compact", "category": "preference"}],
            before_call=before_call,
        ),
        "model",
        "user: I prefer compact items.",
        extraction_prompt="Extract facts",
        fence=FENCE,
        write_filter=None,
    )


@pytest.mark.parametrize("retained", [False, True])
async def test_extraction_commits_once_without_unconditional_upsert(retained):
    inner = AtomicSpy()
    store = RetentionMemoryStore(inner, timedelta(days=30)) if retained else inner
    facts = await extract(store)
    assert [fact.key for fact in facts] == ["size"]
    assert len(inner.commits) == 1 and inner.upserts == 0
    assert [fact.key for fact in await store.get_facts("subject")] == ["size"]
    assert isinstance(store, ConditionalExtractionStore)


@pytest.mark.parametrize("retained", [False, True])
@pytest.mark.parametrize("mutation", ["edit", "delete", "purge", "delete_absent"])
async def test_concurrent_subject_mutation_discards_entire_batch(retained, mutation):
    inner = AtomicSpy()
    await inner.upsert_facts("subject", [MemoryFact(key="size", value="prefers large")])
    store = RetentionMemoryStore(inner, timedelta(days=30)) if retained else inner

    async def mutate(_):
        if mutation == "edit":
            await store.upsert_facts("subject", [MemoryFact(key="size", value="manual edit")])
        elif mutation in ("delete", "delete_absent"):
            await store.delete_fact("subject", "size" if mutation == "delete" else "new")
        else:
            await store.clear("subject")

    assert await extract(store, mutate) == []
    assert len(inner.commits) == 1
    facts = await inner.get_facts("subject")
    if mutation in ("delete", "purge"):
        assert not facts
    else:
        assert facts[0].value == ("manual edit" if mutation == "edit" else "prefers large")


async def test_two_extractions_from_same_snapshot_cannot_both_commit():
    store = InMemoryMemoryStore()
    revision = await store.extraction_revision("subject")
    first = MemoryFact(key="size", value="first")
    second = MemoryFact(key="size", value="second")
    assert await store.commit_extracted("subject", [first], revision)
    assert not await store.commit_extracted("subject", [second], revision)
    assert (await store.get_facts("subject"))[0].value == "first"


async def test_legacy_store_with_retention_keeps_legacy_contract():
    inner = InMemoryMemoryStore()
    legacy = SimpleNamespace(
        **{
            name: getattr(inner, name)
            for name in (
                "get_facts",
                "upsert_facts",
                "search_facts",
                "delete_fact",
                "clear",
                "purge_generation",
            )
        }
    )
    assert not isinstance(legacy, ConditionalExtractionStore)
    assert await extract(RetentionMemoryStore(legacy, timedelta(days=30)))


def test_partial_conditional_extension_fails_at_startup():
    store = InMemoryMemoryStore()
    store.commit_extracted = None
    with pytest.raises(TypeError, match="Conditional extraction"):
        check_memory_store(store)
