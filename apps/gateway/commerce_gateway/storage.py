"""Auth records use opaque random keys, encrypted payloads, and enforced read-time expiry."""

import asyncio
import json
import time
from contextlib import suppress
from copy import deepcopy
from typing import Protocol

from cryptography.fernet import Fernet, MultiFernet


class AuthStorage(Protocol):
    async def get(self, key: str) -> dict | None: ...
    async def put(self, key: str, record: dict, expires: int) -> None: ...
    async def consume(self, key: str) -> dict | None: ...
    async def delete(self, key: str) -> None: ...
    async def touch(self, key: str, expires: int) -> bool: ...
    async def rate_limit(self, key: str, limit: int, window: int) -> bool: ...


class MemoryAuthStorage:
    """Injectable test store; never selected by production startup."""

    def __init__(self):
        self.records: dict[str, tuple[dict, int]] = {}
        self.lock = asyncio.Lock()

    async def get(self, key):
        entry = self.records.get(key)
        return deepcopy(entry[0]) if entry and entry[1] > time.time() else None

    async def put(self, key, record, expires):
        self.records[key] = (deepcopy(record), expires)

    async def consume(self, key):
        async with self.lock:
            record = await self.get(key)
            self.records.pop(key, None)
            return record

    async def delete(self, key):
        self.records.pop(key, None)

    async def touch(self, key, expires):
        async with self.lock:
            record = await self.get(key)
            if record is None:
                return False
            self.records[key] = (record, expires)
            return True

    async def rate_limit(self, key, limit, window):
        async with self.lock:
            now = int(time.time())
            entry = self.records.get("limit:" + key)
            count, expires = (
                (entry[0]["count"], entry[1]) if entry and entry[1] > now else (0, now + window)
            )
            if count >= limit:
                return False
            self.records["limit:" + key] = ({"count": count + 1}, expires)
            return True


class CosmosAuthStorage:
    """One-use OIDC flow consumes use an ETag-conditional delete across replicas."""

    def __init__(self, container, keys: str):
        self.container = container
        self.cipher = MultiFernet([Fernet(key.strip().encode()) for key in keys.split(",")])

    def _record(self, doc):
        if doc["expires"] <= time.time():
            return None
        payload = json.loads(self.cipher.decrypt(doc["payload"].encode()))
        if payload["key"] != doc["id"]:
            raise ValueError("Encrypted auth record binding mismatch")
        return payload["record"]

    async def get(self, key):
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        try:
            return self._record(await self.container.read_item(key, partition_key=key))
        except CosmosResourceNotFoundError:
            return None

    async def put(self, key, record, expires):
        await self.container.upsert_item(
            {
                "id": key,
                "pk": key,
                "expires": expires,
                "ttl": max(1, expires - int(time.time())),
                "payload": self.cipher.encrypt(
                    json.dumps({"key": key, "record": record}).encode()
                ).decode(),
            }
        )

    async def consume(self, key):
        from azure.core import MatchConditions
        from azure.cosmos.exceptions import (
            CosmosAccessConditionFailedError,
            CosmosResourceNotFoundError,
        )

        try:
            doc = await self.container.read_item(key, partition_key=key)
            await self.container.delete_item(
                key,
                partition_key=key,
                etag=doc["_etag"],
                match_condition=MatchConditions.IfNotModified,
            )
            return self._record(doc)
        except (CosmosResourceNotFoundError, CosmosAccessConditionFailedError):
            return None

    async def delete(self, key):
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        with suppress(CosmosResourceNotFoundError):
            await self.container.delete_item(key, partition_key=key)

    async def touch(self, key, expires):
        from azure.core import MatchConditions
        from azure.cosmos.exceptions import (
            CosmosAccessConditionFailedError,
            CosmosResourceNotFoundError,
        )

        for _ in range(3):
            try:
                doc = await self.container.read_item(key, partition_key=key)
                if doc["expires"] <= time.time():
                    return False
                updated = {
                    **doc,
                    "expires": expires,
                    "ttl": max(1, expires - int(time.time())),
                    "payload": self.cipher.rotate(doc["payload"].encode()).decode(),
                }
                await self.container.replace_item(
                    key,
                    updated,
                    etag=doc["_etag"],
                    match_condition=MatchConditions.IfNotModified,
                )
                return True
            except CosmosResourceNotFoundError:
                return False
            except CosmosAccessConditionFailedError:
                continue
        return False

    async def rate_limit(self, key, limit, window):
        import hashlib

        from azure.core import MatchConditions
        from azure.cosmos.exceptions import (
            CosmosAccessConditionFailedError,
            CosmosResourceExistsError,
            CosmosResourceNotFoundError,
        )

        key = "limit:" + hashlib.sha256(key.encode()).hexdigest()
        for _ in range(5):
            now = int(time.time())
            try:
                doc = await self.container.read_item(key, partition_key=key)
            except CosmosResourceNotFoundError:
                try:
                    await self.container.create_item(
                        {
                            "id": key,
                            "pk": key,
                            "count": 1,
                            "expires": now + window,
                            "ttl": window,
                        }
                    )
                    return True
                except CosmosResourceExistsError:
                    continue
            expired = doc["expires"] <= now
            if not expired and doc["count"] >= limit:
                return False
            updated = {
                **doc,
                "count": 1 if expired else doc["count"] + 1,
                "expires": now + window if expired else doc["expires"],
                "ttl": window if expired else max(1, doc["expires"] - now),
            }
            try:
                await self.container.replace_item(
                    key,
                    updated,
                    etag=doc["_etag"],
                    match_condition=MatchConditions.IfNotModified,
                )
                return True
            except CosmosAccessConditionFailedError:
                continue
        return False
