from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass
from typing import Any

from .identity import Principal
from .storage import AccessDenied, CapacityError, ConflictError, Documents, Operation, partition

MAX_CHUNK_BYTES = 48_000
MAX_CHUNKS = 128


@dataclass
class Session:
    pk: str
    document: dict[str, Any]
    messages: list[dict[str, Any]]

    @property
    def id(self) -> str:
        return self.document["session_id"]


class CosmosSessionStore:
    def __init__(self, documents: Documents, transcript_days: int = 30) -> None:
        if not 1 <= transcript_days <= 365:
            raise ValueError("Transcript retention must be 1 to 365 days")
        self.documents, self.ttl = documents, transcript_days * 86400

    def start(self, principal: Principal, provider: str, state: dict[str, Any]) -> Session:
        id = secrets.token_urlsafe(24)
        pk = partition(principal.scope, id)
        body = {
            "kind": "session",
            "session_id": id,
            "owner": principal.scope,
            "provider": provider,
            "version": 1,
            "state": state,
            "chunks": [],
            "pending_app_events": [],
            "created": time.time(),
            "expires": time.time() + self.ttl,
            "ttl": self.ttl,
            "active_turn": None,
            "revoked": False,
        }
        self.documents.transact(pk, [Operation("create", "session", body)])
        return self.require(principal, id, provider)

    def require(self, principal: Principal, id: str, provider: str) -> Session:
        if not id or len(id) > 128:
            raise AccessDenied("Invalid session")
        pk = partition(principal.scope, id)
        body = self.documents.read(pk, "session")
        if (
            body is None
            or body["owner"] != principal.scope
            or body.get("revoked")
            or body["expires"] <= time.time()
        ):
            raise AccessDenied("Session not available")
        if body["provider"] != provider:
            raise ConflictError("Conversation is pinned to another model deployment")
        messages = []
        if len(body["chunks"]) > MAX_CHUNKS:
            raise CapacityError("Transcript manifest exceeded its bound")
        for chunk_id in body["chunks"]:
            chunk = self.documents.read(pk, chunk_id)
            if chunk is None:
                raise RuntimeError("Committed transcript chunk is missing")
            messages.extend(chunk["messages"])
        return Session(pk, body, messages)

    def commit(self, session: Session, extra: list[Operation] | None = None) -> None:
        if session.document["expires"] <= time.time():
            raise AccessDenied("Session expired")
        chunks: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        for message in session.messages:
            if len(json.dumps(message).encode()) > MAX_CHUNK_BYTES:
                raise CapacityError("A transcript message exceeds its storage bound")
            if len(json.dumps([*current, message]).encode()) > MAX_CHUNK_BYTES:
                chunks.append(current)
                current = []
            current.append(message)
        if current:
            chunks.append(current)
        if len(chunks) > MAX_CHUNKS:
            raise CapacityError("Transcript requires compaction before further turns")
        generation = secrets.token_hex(16)
        ids = [f"transcript:{generation}:{index:04d}" for index in range(len(chunks))]
        # Staged chunks are immutable. Only the conditional manifest publishes them.
        for id, messages in zip(ids, chunks, strict=True):
            live = self.documents.read(session.pk, "session")
            if live is None or live.get("revoked") or live["_etag"] != session.document["_etag"]:
                raise ConflictError("Session changed before transcript staging")
            staged = dict(live, staging_fence=id)
            self.documents.transact(
                session.pk,
                [
                    Operation("replace", "session", staged, live["_etag"]),
                    Operation(
                        "create",
                        id,
                        {
                            "kind": "transcript",
                            "messages": messages,
                            "expires": session.document["expires"],
                            "ttl": max(1, int(session.document["expires"] - time.time())),
                        },
                    ),
                ],
            )
            after = self.documents.read(session.pk, "session")

            def semantic(body: dict[str, Any]) -> dict[str, Any]:
                return {
                    key: value
                    for key, value in body.items()
                    if not key.startswith("_") and key != "ttl"
                }

            if after is None or semantic(after) != semantic(staged):
                raise ConflictError("Session changed during transcript staging")
            session.document["_etag"] = after["_etag"]
        body = dict(session.document)
        body.pop("staging_fence", None)
        body.update(
            chunks=ids,
            version=body["version"] + 1,
            ttl=max(1, int(body["expires"] - time.time())),
        )
        bounded_extra = [
            Operation(
                op.kind,
                op.id,
                dict(op.body, expires=body["expires"], ttl=body["ttl"])
                if op.body is not None
                else None,
                op.etag,
            )
            for op in extra or []
        ]
        operations = [
            Operation("replace", "session", body, session.document["_etag"]),
            *bounded_extra,
        ]
        self.documents.transact(session.pk, operations)
        committed = self.documents.read(session.pk, "session")
        if committed is None:
            raise RuntimeError("Session disappeared after commit")
        session.document = committed

    def revoke(self, session: Session, extra: list[Operation] | None = None) -> None:
        session.document.update(
            revoked=True,
            active_turn=None,
            state={},
            chunks=[],
            pending_app_events=[],
        )
        session.messages = []
        self.commit(session, extra)
