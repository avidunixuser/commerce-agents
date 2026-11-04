"""Idempotent memory migration across partitions, explicitly not a distributed transaction."""

from __future__ import annotations

import asyncio
import time
import uuid

from .memory import CosmosMemoryStore
from .storage import AccessDenied, ConflictError, Operation, partition


async def migrate_memory(
    source: CosmosMemoryStore,
    destination: CosmosMemoryStore,
    *,
    consent: bool,
    queued_work: Operation | None = None,
) -> None:
    if (
        not consent
        or not source.principal.guest
        or destination.principal.guest
        or source.principal.tenant != destination.principal.tenant
        or source.principal.role != destination.principal.role
    ):
        raise AccessDenied("Verified guest possession, customer identity and consent required")

    def migrate() -> None:
        old = source._meta()
        target = destination._meta()
        id = f"migration:{partition(source.pk, destination.pk)}"
        existing = source.documents.read(source.pk, id)
        if existing is None:
            if old["revoked"] or target["revoked"]:
                raise AccessDenied("Memory subject revoked")
            source.documents.transact(
                source.pk,
                [
                    Operation(
                        "replace",
                        "memory",
                        dict(
                            old,
                            revoked=True,
                            generation=old["generation"] + 1,
                            revision=uuid.uuid4().hex,
                        ),
                        old["_etag"],
                    ),
                    Operation(
                        "create",
                        id,
                        {
                            "kind": "migration",
                            "target": destination.pk,
                            "target_generation": target["generation"],
                            "target_deletion_generation": target.get("deletion_generation", 0),
                            "facts": old["facts"],
                            "status": "frozen",
                            "ttl": 30 * 86400,
                        },
                    ),
                    *([queued_work] if queued_work else []),
                ],
            )
            existing = source.documents.read(source.pk, id)
        if existing is None:
            raise RuntimeError("Migration reservation missing")
        if existing["target"] != destination.pk:
            raise AccessDenied("Guest migration is bound to another customer")
        if existing["status"] == "complete":
            return
        if queued_work is not None:
            return
        applied = destination.documents.read(destination.pk, id)
        if applied is None:
            target = destination._meta()
            if (
                target["revoked"]
                or target["generation"] != existing["target_generation"]
                or target.get("deletion_generation", 0) != existing["target_deletion_generation"]
            ):
                raise ConflictError("Customer memory was deleted; migration cannot replay")
            now = time.time()
            # Existing customer keys, including expired keys, win. No silent overwrite.
            facts = {
                key: dict(value, expires=now + destination.ttl, migrated_at=now)
                for key, value in existing["facts"].items()
                if (source.principal.guest or value["expires"] > now) and key not in target["facts"]
            }
            facts.update(target["facts"])
            if len(facts) > 100:
                raise ConflictError("Customer memory capacity requires explicit cleanup")
            retention = destination.retention_operation(facts)
            destination.documents.transact(
                destination.pk,
                [
                    Operation(
                        "replace",
                        "memory",
                        dict(target, facts=facts, revision=uuid.uuid4().hex),
                        target["_etag"],
                    ),
                    Operation(
                        "create",
                        id,
                        {"kind": "migration_receipt", "status": "complete", "ttl": 30 * 86400},
                    ),
                    *([retention] if retention else []),
                ],
            )
        current = source._meta()
        source.documents.transact(
            source.pk,
            [
                Operation(
                    "replace",
                    "memory",
                    dict(current, facts={}, revision=uuid.uuid4().hex),
                    current["_etag"],
                ),
                Operation(
                    "replace",
                    id,
                    dict(existing, status="complete", facts={}),
                    existing["_etag"],
                ),
            ],
        )

    await asyncio.to_thread(migrate)
