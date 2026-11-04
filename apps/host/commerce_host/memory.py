from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from commerce_common.memory import match_facts
from commerce_common.types import MemoryFact

from .identity import Principal
from .storage import AccessDenied, CapacityError, ConflictError, Documents, Operation, partition


class CosmosMemoryStore:
    """Bound to a verified subject; revision CAS fences every extraction write."""

    def __init__(
        self,
        documents: Documents,
        principal: Principal,
        *,
        retention_days: int = 90,
        vertical: str = "retail",
    ) -> None:
        if not 1 <= retention_days <= 365:
            raise ValueError("Memory retention must be 1 to 365 days")
        self.documents, self.principal = documents, principal
        self.pk = partition(principal.scope, "memory")
        self.ttl = retention_days * 86400
        self.snapshot_revision: str | None = None
        self.vertical = vertical

    def retention_operation(self, values: dict[str, Any]) -> Operation | None:
        if self.principal.guest or not values:
            return None
        from .jobs import job_operation

        work = job_operation(
            str(uuid.uuid4()),
            {"principal": self.principal.payload(), "vertical": self.vertical},
            purpose="memory_retention",
        )
        deadline = min(value["expires"] for value in values.values())
        return Operation(
            "create",
            work.id,
            dict(
                work.body,
                available=deadline,
                expires=deadline + 30 * 86400,
            ),
        )

    def prune_expired(self) -> None:
        meta = self._meta()
        if meta["revoked"]:
            return
        values = {
            key: value for key, value in meta["facts"].items() if value["expires"] > time.time()
        }
        if len(values) == len(meta["facts"]):
            return
        operations = [
            Operation(
                "replace",
                "memory",
                dict(meta, facts=values, revision=uuid.uuid4().hex),
                meta["_etag"],
            )
        ]
        work = self.retention_operation(values)
        if work:
            operations.append(work)
        self.documents.transact(self.pk, operations)

    def _subject(self, subject: str) -> None:
        if subject != self.principal.subject:
            raise AccessDenied("Memory belongs to another subject")

    def _guest_expiry(self) -> float | None:
        if not self.principal.guest:
            return None
        record = self.documents.read(
            partition(self.principal.tenant, "guest", self.principal.subject), "identity"
        )
        if (
            record is None
            or record.get("revoked")
            or record.get("expires", 0) <= time.time()
            or (
                self.principal.guest_epoch is not None
                and record.get("epoch") != self.principal.guest_epoch
            )
        ):
            raise AccessDenied("Guest identity expired or was revoked")
        return record["expires"]

    def _meta(self) -> dict[str, Any]:
        existing = self.documents.read(self.pk, "memory")
        if existing is not None:
            if self.principal.guest and not existing["revoked"]:
                expiry = self._guest_expiry()
                if existing.get("expires") != expiry:
                    self.documents.transact(
                        self.pk,
                        [
                            Operation(
                                "replace",
                                "memory",
                                dict(
                                    existing, expires=expiry, ttl=max(1, int(expiry - time.time()))
                                ),
                                existing["_etag"],
                            )
                        ],
                    )
                    refreshed = self.documents.read(self.pk, "memory")
                    if refreshed is None:
                        raise RuntimeError("Memory disappeared")
                    return refreshed
            return existing
        expiry = self._guest_expiry()
        try:
            self.documents.transact(
                self.pk,
                [
                    Operation(
                        "create",
                        "memory",
                        {
                            "kind": "memory",
                            "generation": 0,
                            "deletion_generation": 0,
                            "revision": uuid.uuid4().hex,
                            "facts": {},
                            "revoked": False,
                            **(
                                {"expires": expiry, "ttl": max(1, int(expiry - time.time()))}
                                if expiry is not None
                                else {}
                            ),
                        },
                    )
                ],
            )
        except ConflictError:
            if self.documents.read(self.pk, "memory") is None:
                raise
        result = self.documents.read(self.pk, "memory")
        if result is None:
            raise RuntimeError("Memory metadata was not created")
        return result

    def _facts(self, meta: dict[str, Any]) -> list[MemoryFact]:
        if meta["revoked"]:
            raise AccessDenied("Memory subject revoked")
        self._guest_expiry()
        now = time.time()
        return [
            MemoryFact.model_validate(value["fact"])
            for value in meta["facts"].values()
            if self.principal.guest or value["expires"] > now
        ]

    async def get_facts(self, subject_id: str) -> list[MemoryFact]:
        self._subject(subject_id)
        return self._facts(await asyncio.to_thread(self._meta))

    async def search_facts(self, subject_id: str, query: str) -> list[MemoryFact]:
        return match_facts(await self.get_facts(subject_id), query)

    async def extraction_revision(self, subject_id: str) -> str:
        self._subject(subject_id)
        meta = await asyncio.to_thread(self._meta)
        self._facts(meta)
        return self.snapshot_revision or meta.get("revision", meta["_etag"])

    async def commit_extracted(
        self, subject_id: str, facts: list[MemoryFact], revision: str
    ) -> bool:
        self._subject(subject_id)
        return await asyncio.to_thread(self._write, facts, revision)

    def _write(self, facts: list[MemoryFact], revision: str | None = None) -> bool:
        meta = self._meta()
        if meta["revoked"] or (
            revision is not None and meta.get("revision", meta["_etag"]) != revision
        ):
            return False
        self._facts(meta)
        now = time.time()
        values = {
            key: dict(value)
            for key, value in meta["facts"].items()
            if self.principal.guest or value["expires"] > now
        }
        for fact in facts:
            values[fact.key] = {
                "fact": fact.model_dump(mode="json"),
                "expires": time.time() + self.ttl,
            }
        if len(values) > 100:
            raise CapacityError("Memory is limited to 100 facts; remove older facts first")
        body = dict(meta, facts=values, revision=uuid.uuid4().hex)
        operations = [Operation("replace", "memory", body, meta["_etag"])]
        retention = self.retention_operation(values)
        if retention:
            operations.append(retention)
        try:
            self.documents.transact(self.pk, operations)
        except ConflictError:
            if revision is not None:
                return False
            raise
        return True

    async def upsert_facts(self, subject_id: str, facts: list[MemoryFact]) -> None:
        self._subject(subject_id)
        if not await asyncio.to_thread(self._write, facts):
            raise AccessDenied("Memory subject revoked")

    async def delete_fact(self, subject_id: str, key: str) -> bool:
        self._subject(subject_id)

        def delete() -> bool:
            meta = self._meta()
            self._facts(meta)
            removed = key in meta["facts"]
            values = dict(meta["facts"])
            values.pop(key, None)
            self.documents.transact(
                self.pk,
                [
                    Operation(
                        "replace",
                        "memory",
                        dict(
                            meta,
                            facts=values,
                            revision=uuid.uuid4().hex,
                            deletion_generation=meta.get("deletion_generation", 0) + 1,
                        ),
                        meta["_etag"],
                    )
                ],
            )
            return removed

        return await asyncio.to_thread(delete)

    async def clear(self, subject_id: str) -> None:
        self._subject(subject_id)

        def clear() -> None:
            meta = self._meta()
            self.documents.transact(
                self.pk,
                [
                    Operation(
                        "replace",
                        "memory",
                        dict(
                            meta,
                            facts={},
                            generation=meta["generation"] + 1,
                            revision=uuid.uuid4().hex,
                        ),
                        meta["_etag"],
                    )
                ],
            )

        await asyncio.to_thread(clear)

    async def purge_generation(self, subject_id: str) -> int:
        self._subject(subject_id)
        return (await asyncio.to_thread(self._meta))["generation"]

    async def revoke(self) -> None:
        def revoke() -> None:
            meta = self._meta()
            if meta["revoked"]:
                return
            self.documents.transact(
                self.pk,
                [
                    Operation(
                        "replace",
                        "memory",
                        dict(
                            meta,
                            facts={},
                            revoked=True,
                            generation=meta["generation"] + 1,
                            revision=uuid.uuid4().hex,
                        ),
                        meta["_etag"],
                    )
                ],
            )

        await asyncio.to_thread(revoke)
