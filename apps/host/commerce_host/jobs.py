from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any

from .identity import Principal
from .storage import CapacityError, ConflictError, Documents, Operation

MAX_EVENTS = 10000


def job_operation(id: str, payload: dict[str, Any], *, purpose: str = "turn") -> Operation:
    return Operation(
        "create",
        f"job:{id}",
        {
            "kind": "job",
            "purpose": purpose,
            "turn_id": id,
            "payload": payload,
            "status": "ready",
            "available": time.time(),
            "shard": int(hashlib.sha256(id.encode()).hexdigest()[:8], 16) % 32,
            "attempt": 0,
            "fence": 0,
            "events": 0,
            "pending_effects": 0,
            "completed_effects": 0,
            "ttl": 30 * 86400,
            "expires": time.time() + 30 * 86400,
        },
    )


class JobQueue:
    def __init__(self, documents: Documents, owner: str, *, lease_seconds: int = 60) -> None:
        if not owner or not 30 <= lease_seconds <= 300:
            raise ValueError("Worker owner and 30 to 300 second lease required")
        self.documents, self.owner, self.lease_seconds = documents, owner, lease_seconds

    def claim(self, candidate: dict[str, Any]) -> dict[str, Any]:
        current = self.documents.read(candidate["pk"], candidate["id"])
        if (
            current is None
            or current["status"] not in {"ready", "running"}
            or current["available"] > time.time()
        ):
            raise ConflictError("Job is not available")
        if current["status"] == "running":
            safe = current["purpose"] in {"memory", "memory_retention", "purge", "migration"}
            status = (
                "ready" if safe and current["attempt"] < 5 else ("dead" if safe else "uncertain")
            )
            self.documents.transact(
                current["pk"],
                [
                    Operation(
                        "replace",
                        current["id"],
                        dict(
                            current,
                            status=status,
                            fence=current["fence"] + 1,
                            available=time.time(),
                            reason="Worker lease expired",
                        ),
                        current["_etag"],
                    )
                ],
            )
            raise ConflictError("Expired execution requires reconciliation")
        updated = dict(
            current,
            status="running",
            owner=self.owner,
            fence=current["fence"] + 1,
            attempt=current["attempt"] + 1,
            available=time.time() + self.lease_seconds,
        )
        self.documents.transact(
            current["pk"], [Operation("replace", current["id"], updated, current["_etag"])]
        )
        return self._read(current)

    def _read(self, job: dict[str, Any]) -> dict[str, Any]:
        result = self.documents.read(job["pk"], job["id"])
        if result is None:
            raise ConflictError("Job was removed or expired")
        return result

    def owned(self, job: dict[str, Any]) -> dict[str, Any]:
        current = self._read(job)
        if (
            current["status"] != "running"
            or current.get("owner") != self.owner
            or current["fence"] != job["fence"]
            or current["available"] <= time.time()
            or current.get("expires", float("inf")) <= time.time()
        ):
            raise ConflictError("Worker lease is no longer valid")
        return current

    def renew(self, job: dict[str, Any]) -> dict[str, Any]:
        current = self.owned(job)
        self.documents.transact(
            current["pk"],
            [
                Operation(
                    "replace",
                    current["id"],
                    dict(current, available=time.time() + self.lease_seconds),
                    current["_etag"],
                )
            ],
        )
        return self._read(job)

    def append_event(self, job: dict[str, Any], type: str, data: dict[str, Any]) -> None:
        current = self.owned(job)
        sequence = current["events"] + 1
        if sequence > MAX_EVENTS:
            raise CapacityError("Turn event limit reached")
        event = {
            "kind": "event",
            "turn_id": current["turn_id"],
            "sequence": sequence,
            "type": type,
            "data": data,
            "ttl": 30 * 86400,
            "expires": current["expires"],
        }
        self.documents.transact(
            current["pk"],
            [
                Operation(
                    "replace", current["id"], dict(current, events=sequence), current["_etag"]
                ),
                Operation("create", f"event:{current['turn_id']}:{sequence:06d}", event),
            ],
        )

    def finish(self, job: dict[str, Any], status: str, reason: str | None = None) -> None:
        if status not in {"complete", "failed", "cancelled", "uncertain", "dead"}:
            raise ValueError("Invalid terminal status")
        current = self.owned(job)
        body = dict(current, status=status, reason=reason, available=time.time())
        self.documents.transact(
            current["pk"], [Operation("replace", current["id"], body, current["_etag"])]
        )

    def retry(self, job: dict[str, Any], *, safe_to_retry: bool) -> None:
        current = self.owned(job)
        if not safe_to_retry:
            self.finish(current, "uncertain", "External effects require reconciliation")
            return
        if current["attempt"] >= 5:
            self.finish(current, "dead", "Retry budget exhausted")
            return
        self.documents.transact(
            current["pk"],
            [
                Operation(
                    "replace",
                    current["id"],
                    dict(current, status="ready", available=time.time() + 2 ** current["attempt"]),
                    current["_etag"],
                ),
            ],
        )

    def redrive_safe(self, pk: str, id: str, etag: str, operator: Principal) -> None:
        operator.require("operator")
        current = self.documents.read(pk, id)
        if (
            current is None
            or current["_etag"] != etag
            or current.get("expires", 0) <= time.time()
            or current["status"] not in {"dead", "failed"}
            or current["purpose"] not in {"memory", "memory_retention", "purge", "migration"}
            or current["payload"]["principal"]["tenant"] != operator.tenant
        ):
            raise ConflictError("Only a matching safe failed job can be redriven in this tenant")
        self.documents.transact(
            pk,
            [
                Operation(
                    "replace",
                    id,
                    dict(
                        current,
                        status="ready",
                        attempt=0,
                        fence=current["fence"] + 1,
                        available=time.time(),
                        redriven_by=operator.scope,
                        redriven_at=time.time(),
                    ),
                    etag,
                )
            ],
        )


class ToolJournal:
    """Reserve before external invocation; ambiguous outcomes require reconciliation."""

    def __init__(
        self,
        queue: JobQueue,
        job: dict[str, Any],
        *,
        lock: asyncio.Lock | None = None,
    ) -> None:
        self.queue, self.job = queue, job
        self.lock = lock if lock is not None else asyncio.Lock()

    async def begin_async(self, key: str, arguments_hash: str) -> dict[str, Any] | None:
        async with self.lock:
            return await asyncio.to_thread(self.begin, key, arguments_hash)

    async def complete_async(self, key: str, result: dict[str, Any]) -> None:
        async with self.lock:
            await asyncio.to_thread(self.complete, key, result)

    def begin(self, key: str, arguments_hash: str) -> dict[str, Any] | None:
        current = self.queue.owned(self.job)
        if current.get("cancel_requested"):
            raise asyncio.CancelledError("Execution cancellation requested")
        id = f"effect:{self.job['turn_id']}:{key}"
        existing = self.queue.documents.read(current["pk"], id)
        if existing:
            if existing["arguments_hash"] != arguments_hash:
                raise ConflictError("Idempotency key was reused with different arguments")
            if existing["status"] == "complete":
                return existing["result"]
            raise ConflictError("Effect outcome requires backend reconciliation")
        self.queue.documents.transact(
            current["pk"],
            [
                Operation(
                    "replace",
                    current["id"],
                    dict(current, pending_effects=current.get("pending_effects", 0) + 1),
                    current["_etag"],
                ),
                Operation(
                    "create",
                    id,
                    {
                        "kind": "effect",
                        "arguments_hash": arguments_hash,
                        "status": "pending",
                        "ttl": 30 * 86400,
                        "expires": current["expires"],
                    },
                ),
            ],
        )
        return None

    def complete(self, key: str, result: dict[str, Any]) -> None:
        current = self.queue.owned(self.job)
        id = f"effect:{self.job['turn_id']}:{key}"
        effect = self.queue.documents.read(current["pk"], id)
        if effect is None or effect["status"] != "pending":
            raise ConflictError("Effect was not reserved")
        self.queue.documents.transact(
            current["pk"],
            [
                Operation(
                    "replace",
                    current["id"],
                    dict(
                        current,
                        pending_effects=current.get("pending_effects", 0) - 1,
                        completed_effects=current.get("completed_effects", 0) + 1,
                    ),
                    current["_etag"],
                ),
                Operation(
                    "replace", id, dict(effect, status="complete", result=result), effect["_etag"]
                ),
            ],
        )
