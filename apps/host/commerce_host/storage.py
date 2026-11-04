"""Conditional single-partition transactions shared by production stores and test fakes."""

from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol


class ConflictError(RuntimeError):
    """A conditional transaction lost a race; no operation was committed."""


class AccessDenied(PermissionError):
    """The authenticated principal cannot access this aggregate."""


class CapacityError(ValueError):
    """An aggregate exceeded an explicit persistence bound."""


def partition(*parts: str) -> str:
    if not parts or any(not part or len(part) > 1024 for part in parts):
        raise ValueError("Partition components must be nonempty and bounded")
    return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class Operation:
    kind: Literal["create", "replace", "delete"]
    id: str
    body: Mapping[str, Any] | None = None
    etag: str | None = None


def validate_batch(operations: Sequence[Operation]) -> None:
    if not operations or len(operations) > 100:
        raise CapacityError("A transaction requires 1 to 100 operations")
    if len({op.id for op in operations}) != len(operations):
        raise ValueError("A transaction must not mutate an item twice")
    if len(json.dumps([op.__dict__ for op in operations]).encode()) > 1_800_000:
        raise CapacityError("Transaction exceeds conservative Cosmos payload bound")


class Documents(Protocol):
    def read(self, pk: str, id: str) -> dict[str, Any] | None: ...

    def transact(self, pk: str, operations: Sequence[Operation]) -> None: ...

    def page(
        self, pk: str, kind: str, *, after: str = "", limit: int = 100
    ) -> list[dict[str, Any]]: ...

    def due_jobs(
        self,
        shard: int,
        now: float,
        limit: int = 20,
        *,
        role: str | None = None,
        vertical: str | None = None,
    ) -> list[dict[str, Any]]: ...


class FakeDocuments:
    """An atomic, copying fake with real conditional-write semantics, not an emulator."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._sequence = 0
        self.fail_at: int | None = None

    def read(self, pk: str, id: str) -> dict[str, Any] | None:
        with self._lock:
            return copy.deepcopy(self.items.get((pk, id)))

    def transact(self, pk: str, operations: Sequence[Operation]) -> None:
        validate_batch(operations)
        with self._lock:
            pending = copy.deepcopy(self.items)
            for index, op in enumerate(operations):
                if self.fail_at == index:
                    raise ConflictError("Injected transaction failure")
                current = pending.get((pk, op.id))
                if op.kind == "create":
                    if current is not None:
                        raise ConflictError("Item already exists")
                elif current is None or not op.etag or current["_etag"] != op.etag:
                    raise ConflictError("Item version changed")
                if op.kind == "delete":
                    del pending[(pk, op.id)]
                else:
                    self._sequence += 1
                    body = copy.deepcopy(dict(op.body or {}))
                    body.update(id=op.id, pk=pk, _etag=str(self._sequence))
                    pending[(pk, op.id)] = body
            self.items = pending

    def page(
        self, pk: str, kind: str, *, after: str = "", limit: int = 100
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise CapacityError("Page size must be 1 to 100")
        with self._lock:
            return copy.deepcopy(
                sorted(
                    (
                        item
                        for (key, id), item in self.items.items()
                        if key == pk and item.get("kind") == kind and id > after
                    ),
                    key=lambda item: item["id"],
                )[:limit]
            )

    def due_jobs(
        self,
        shard: int,
        now: float,
        limit: int = 20,
        *,
        role: str | None = None,
        vertical: str | None = None,
    ) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(
                [
                    item
                    for item in self.items.values()
                    if item.get("kind") == "job"
                    and item["shard"] == shard
                    and item["status"] in {"ready", "running"}
                    and item["available"] <= now
                    and (role is None or item["payload"].get("principal", {}).get("role") == role)
                    and (vertical is None or item["payload"].get("vertical") == vertical)
                ][:limit]
            )


class CosmosDocuments:
    """SDK data plane only. Container provisioning and RBAC belong to infrastructure."""

    def __init__(self, container: Any) -> None:
        self.container = container

    def read(self, pk: str, id: str) -> dict[str, Any] | None:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        try:
            return dict(self.container.read_item(item=id, partition_key=pk))
        except CosmosResourceNotFoundError:
            return None

    def transact(self, pk: str, operations: Sequence[Operation]) -> None:
        from azure.cosmos.exceptions import CosmosBatchOperationError

        validate_batch(operations)
        batch = []
        for op in operations:
            body = {key: value for key, value in (op.body or {}).items() if not key.startswith("_")}
            body.update(id=op.id, pk=pk)
            if "expires" in body:
                body["ttl"] = max(1, int(body["expires"] - time.time()))
            if op.kind == "create":
                args = (body,)
                kwargs = {}
            else:
                if not op.etag:
                    raise ValueError("Conditional ETag is required")
                args = (op.id,) if op.kind == "delete" else (op.id, body)
                kwargs = {"if_match_etag": op.etag}
            batch.append((op.kind, args, kwargs))
        try:
            self.container.execute_item_batch(batch_operations=batch, partition_key=pk)
        except CosmosBatchOperationError as error:
            if error.status_code in {409, 412} or any(
                response.get("statusCode") in {409, 412} for response in error.operation_responses
            ):
                raise ConflictError("Aggregate version changed") from error
            raise

    def page(
        self, pk: str, kind: str, *, after: str = "", limit: int = 100
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise CapacityError("Page size must be 1 to 100")
        return list(
            self.container.query_items(
                query="SELECT TOP @limit * FROM c WHERE c.kind=@kind AND c.id>@after ORDER BY c.id",
                parameters=[
                    {"name": "@limit", "value": limit},
                    {"name": "@kind", "value": kind},
                    {"name": "@after", "value": after},
                ],
                partition_key=pk,
                max_item_count=limit,
            )
        )

    def due_jobs(
        self,
        shard: int,
        now: float,
        limit: int = 20,
        *,
        role: str | None = None,
        vertical: str | None = None,
    ) -> list[dict[str, Any]]:
        if not 0 <= shard < 32 or not 1 <= limit <= 100:
            raise ValueError("Invalid scheduler query bounds")
        filters = ""
        parameters = [
            {"name": "@limit", "value": limit},
            {"name": "@shard", "value": shard},
            {"name": "@now", "value": now},
        ]
        if role is not None:
            filters += " AND c.payload.principal.role=@role"
            parameters.append({"name": "@role", "value": role})
        if vertical is not None:
            filters += " AND c.payload.vertical=@vertical"
            parameters.append({"name": "@vertical", "value": vertical})
        return list(
            self.container.query_items(
                query=(
                    "SELECT TOP @limit * FROM c WHERE c.kind='job' AND c.shard=@shard "
                    "AND c.status IN ('ready','running') AND c.available<=@now" + filters
                ),
                parameters=parameters,
                enable_cross_partition_query=True,
                max_item_count=limit,
            )
        )
