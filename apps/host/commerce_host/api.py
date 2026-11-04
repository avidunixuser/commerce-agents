from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from shopping_agent import PageContext

from .config import Settings
from .identity import Principal, TokenVerifier, refresh_guest_registry, verify_guest_registry
from .jobs import job_operation
from .memory import CosmosMemoryStore
from .migration import migrate_memory
from .runtime import RuntimeFactory, change_fingerprint
from .sessions import CosmosSessionStore, Session
from .storage import AccessDenied, CapacityError, ConflictError, Documents, Operation, partition


class ChatInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=4000)
    page: PageContext | None = None


class MemoryEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=64)
    value: str = Field(min_length=1, max_length=200)


class MigrationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    guest_assertion: str = Field(min_length=1, max_length=16000)
    consent: bool


class MemoryDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str | None = Field(default=None, min_length=1, max_length=64)
    purge: bool = False


class ResetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    purge_memory: bool = False


class GuestBootstrap(BaseModel):
    model_config = ConfigDict(extra="forbid")
    principal_id: str = Field(min_length=1, max_length=256)
    revocation_id: str = Field(min_length=1, max_length=128)
    memory_consent: bool = Field(strict=True)


def principal_payload(principal: Principal) -> dict[str, Any]:
    return principal.payload()


def memory_store(
    documents: Documents, settings: Settings, principal: Principal
) -> CosmosMemoryStore:
    if principal.guest:
        principal.require("guest_memory")
    return CosmosMemoryStore(
        documents,
        principal,
        retention_days=settings.guest_days if principal.guest else settings.memory_days,
        vertical=settings.vertical,
    )


def create_app(
    settings: Settings,
    documents: Documents,
    factory: RuntimeFactory,
    verifier: TokenVerifier,
    ready: Callable[[], bool] | None = None,
) -> FastAPI:
    app = FastAPI(title="Commerce role API", docs_url=None, redoc_url=None, openapi_url=None)
    sessions = CosmosSessionStore(documents, settings.transcript_days)
    prefix = "/api" if settings.role == "shopping" else "/api/merchant"

    @app.middleware("http")
    async def authenticated(request: Request, call_next):
        from fastapi.responses import JSONResponse
        from jwt import PyJWTError

        if request.url.path.startswith("/health/"):
            return await call_next(request)
        if settings.role == "shopping" and (
            request.url.path.casefold() == "/api/merchant"
            or request.url.path.casefold().startswith("/api/merchant/")
        ):
            return JSONResponse({"detail": "Not found"}, status_code=404)
        if not request.url.path.startswith(prefix + "/"):
            return JSONResponse({"detail": "Not found"}, status_code=404)
        authorization = request.headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            return JSONResponse({"detail": "Authentication required"}, status_code=401)
        try:
            principal, claims = await asyncio.to_thread(verifier.verify, authorization[7:])
            guest_bootstrap = (
                principal.guest
                and request.method == "POST"
                and request.url.path == prefix + "/auth/guest/bootstrap"
            )
            guest_logout = (
                principal.guest
                and request.method == "POST"
                and request.url.path == prefix + "/auth/logout"
            )
            if not guest_bootstrap and not guest_logout:
                await asyncio.to_thread(verify_guest_registry, documents, principal, claims)
                await asyncio.to_thread(
                    refresh_guest_registry,
                    documents,
                    principal,
                    claims,
                    settings.guest_days,
                )
            if (
                principal.guest
                and "guest_memory" in principal.permissions
                and not guest_bootstrap
                and not guest_logout
            ):
                # Extend physical TTL only to the authority's refreshed inactivity deadline.
                await asyncio.to_thread(CosmosMemoryStore(documents, principal)._meta)
            request.state.principal, request.state.claims = principal, claims
        except PyJWTError:
            return JSONResponse({"detail": "Invalid authentication"}, status_code=401)
        except AccessDenied:
            return JSONResponse({"detail": "Access denied"}, status_code=403)
        except ConflictError:
            return JSONResponse({"detail": "Identity changed; retry"}, status_code=409)
        try:
            return await call_next(request)
        except AccessDenied:
            return JSONResponse({"detail": "Access denied"}, status_code=403)
        except PyJWTError:
            return JSONResponse({"detail": "Invalid identity proof"}, status_code=401)
        except ConflictError:
            return JSONResponse(
                {"detail": "State changed; reload before retrying"}, status_code=409
            )
        except CapacityError as error:
            return JSONResponse({"detail": str(error)}, status_code=413)

    async def current(request: Request) -> Session:
        return await asyncio.to_thread(
            sessions.require,
            request.state.principal,
            request.headers.get("x-session-id", ""),
            settings.provider_id,
        )

    def store_for(principal: Principal) -> CosmosMemoryStore:
        return memory_store(documents, settings, principal)

    @app.get("/health/live")
    async def live():
        return {"status": "live"}

    @app.get("/health/ready")
    async def readiness():
        if ready is not None and not ready():
            raise HTTPException(503, "Worker is not ready")
        await asyncio.to_thread(documents.read, "health", "probe")
        return {"status": "ready"}

    @app.post(prefix + "/auth/guest/bootstrap")
    async def bootstrap(body: GuestBootstrap, request: Request):
        principal, claims = request.state.principal, request.state.claims
        if (
            not principal.guest
            or settings.role != "shopping"
            or body.principal_id != principal.subject
            or body.revocation_id != claims.get("jti")
            or body.memory_consent is not claims.get("memory_consent")
            or principal.guest_epoch != 0
        ):
            raise AccessDenied("Bootstrap must match the signed guest identity")
        pk = partition(principal.tenant, "guest", principal.subject)
        record = await asyncio.to_thread(documents.read, pk, "identity")
        if record is None:
            await asyncio.to_thread(
                documents.transact,
                pk,
                [
                    Operation(
                        "create",
                        "identity",
                        {
                            "epoch": 0,
                            "revoked": False,
                            "expires": time.time() + settings.guest_days * 86400,
                            "ttl": settings.guest_days * 86400,
                            "revocation_id": body.revocation_id,
                            "memory_consent": body.memory_consent,
                        },
                    )
                ],
            )
        else:
            await asyncio.to_thread(verify_guest_registry, documents, principal, claims)
        return {"ok": True}

    @app.get(prefix + "/auth/session")
    async def authentication_session(request: Request):
        return {
            "authenticated": True,
            "guest": request.state.principal.guest,
            "role": settings.role,
        }

    @app.post(prefix + "/auth/logout")
    async def logout(request: Request):
        principal, claims = request.state.principal, request.state.claims
        guests = [(principal, claims)] if principal.guest else []
        proof = request.headers.get("x-commerce-guest-assertion")
        if proof and not principal.guest:
            guest, guest_claims = await asyncio.to_thread(verifier.verify, proof)
            if not guest.guest or guest.tenant != principal.tenant:
                raise AccessDenied("Guest proof belongs to another identity boundary")
            guests.append((guest, guest_claims))
        for guest, guest_claims in guests:
            pk = partition(guest.tenant, "guest", guest.subject)
            record = await asyncio.to_thread(documents.read, pk, "identity")
            if record is None:
                continue
            if record.get("revocation_id") != guest_claims.get("jti"):
                raise AccessDenied("Guest revocation identity does not match")
            if record.get("revoked"):
                continue
            await asyncio.to_thread(
                verify_guest_registry,
                documents,
                guest,
                guest_claims,
                allow_frozen=True,
            )
            await CosmosMemoryStore(
                documents,
                guest,
                retention_days=settings.guest_days,
                vertical=settings.vertical,
            ).revoke()
            await asyncio.to_thread(
                documents.transact,
                pk,
                [
                    Operation(
                        "replace",
                        "identity",
                        dict(record, revoked=True, epoch=record["epoch"] + 1),
                        record["_etag"],
                    )
                ],
            )
        if request.headers.get("x-session-id"):
            session = await current(request)
            await asyncio.to_thread(
                sessions.revoke,
                session,
                [
                    job_operation(
                        str(uuid.uuid4()),
                        {
                            "principal": principal.payload(),
                            "session_id": session.id,
                            "vertical": settings.vertical,
                        },
                        purpose="purge",
                    )
                ],
            )
        return {"ok": True}

    @app.post(prefix + "/session")
    async def start(request: Request):
        principal = request.state.principal
        _, state_type, _ = factory.build(
            principal,
            CosmosMemoryStore(documents, principal, retention_days=settings.memory_days),
        )
        session = await asyncio.to_thread(
            sessions.start, principal, settings.provider_id, state_type().model_dump(mode="json")
        )
        return {"session_id": session.id, "guest": principal.guest}

    async def events(request: Request, session: Session, turn_id: str):
        raw_cursor = request.headers.get("last-event-id", "").rsplit(":", 1)[-1]
        if raw_cursor and (not raw_cursor.isdecimal() or len(raw_cursor) > 6):
            raise HTTPException(400, "Invalid event cursor")
        cursor = int(raw_cursor or "0")
        job = await asyncio.to_thread(documents.read, session.pk, f"job:{turn_id}")
        if job is None:
            raise HTTPException(404, "Turn not found")

        async def stream():
            nonlocal cursor
            yield f"event: turn_accepted\ndata: {json.dumps({'turn_id': turn_id})}\n\n"
            deadline = time.monotonic() + settings.segment_seconds
            while time.monotonic() < deadline and not await request.is_disconnected():
                # Revalidate revocation/expiry while a long-lived stream remains open.
                if request.state.claims.get("exp", 0) <= time.time():
                    return
                await current(request)
                await asyncio.to_thread(
                    verify_guest_registry,
                    documents,
                    request.state.principal,
                    request.state.claims,
                )
                page = await asyncio.to_thread(
                    documents.page,
                    session.pk,
                    "event",
                    after=f"event:{turn_id}:{cursor:06d}",
                    limit=100,
                )
                selected = [event for event in page if event["turn_id"] == turn_id]
                for event in selected:
                    cursor = event["sequence"]
                    yield (
                        f"id: {turn_id}:{cursor}\nevent: {event['type']}\n"
                        f"data: {json.dumps(event['data'])}\n\n"
                    )
                status = await asyncio.to_thread(documents.read, session.pk, f"job:{turn_id}")
                if status is None:
                    raise RuntimeError("Turn disappeared")
                if cursor >= status["events"] and status["status"] in {
                    "complete",
                    "failed",
                    "dead",
                    "cancelled",
                    "uncertain",
                }:
                    if status["status"] != "complete":
                        yield (
                            "event: error\ndata: "
                            + json.dumps(
                                {
                                    "message": "Turn interrupted; check its status before retrying.",
                                    "status": status["status"],
                                }
                            )
                            + "\n\n"
                        )
                    return
                if not selected:
                    yield ": keepalive\n\n"
                    await asyncio.sleep(0.5)
            yield "event: reconnect\ndata: {}\n\n"

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no", "X-Turn-Id": turn_id},
        )

    @app.post(prefix + "/chat")
    async def chat(body: ChatInput, request: Request):
        if len(body.model_dump_json().encode()) > 8192:
            raise HTTPException(413, "Chat input exceeds 8 KiB")
        session = await current(request)
        raw_key = request.headers.get("idempotency-key", "")
        try:
            turn_id = str(uuid.UUID(raw_key))
        except ValueError as error:
            raise HTTPException(400, "A UUID Idempotency-Key is required") from error
        import hashlib

        digest = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
        existing = await asyncio.to_thread(documents.read, session.pk, f"job:{turn_id}")
        if existing:
            if existing["payload"]["request_hash"] != digest:
                raise HTTPException(409, "Idempotency key names a different request")
            return await events(request, session, turn_id)
        if session.document["active_turn"]:
            raise HTTPException(409, "A turn is already active")
        session.document["active_turn"] = turn_id
        payload = {
            "principal": principal_payload(request.state.principal),
            "claims": {
                key: request.state.claims[key]
                for key in ("guest_epoch", "memory_consent", "jti")
                if key in request.state.claims
            },
            "session_id": session.id,
            "provider": settings.provider_id,
            "vertical": settings.vertical,
            "message": body.message,
            "page": body.page.model_dump(mode="json") if body.page else None,
            "request_hash": digest,
            "memory_revision": (
                await store_for(request.state.principal).extraction_revision(
                    request.state.principal.subject
                )
                if not request.state.principal.guest
                or "guest_memory" in request.state.principal.permissions
                else None
            ),
        }
        await asyncio.to_thread(sessions.commit, session, [job_operation(turn_id, payload)])
        return await events(request, session, turn_id)

    @app.get(prefix + "/turns/{turn_id}/events")
    async def resume(turn_id: uuid.UUID, request: Request):
        return await events(request, await current(request), str(turn_id))

    @app.get(prefix + "/turns/{turn_id}")
    async def turn_status(turn_id: uuid.UUID, request: Request):
        session = await current(request)
        job = await asyncio.to_thread(documents.read, session.pk, f"job:{turn_id}")
        if job is None:
            raise HTTPException(404, "Turn not found")
        return {
            "turn_id": str(turn_id),
            "status": job["status"],
            "events": job["events"],
            "result": job.get("result"),
        }

    @app.post(prefix + "/turns/{turn_id}/cancel")
    async def cancel(turn_id: uuid.UUID, request: Request):
        session = await current(request)
        job = await asyncio.to_thread(documents.read, session.pk, f"job:{turn_id}")
        if job is None:
            raise HTTPException(404, "Turn not found")
        if job["status"] not in {"ready", "running"}:
            raise HTTPException(409, "Turn already ended")
        await asyncio.to_thread(
            documents.transact,
            session.pk,
            [Operation("replace", job["id"], dict(job, cancel_requested=True), job["_etag"])],
        )
        return {"status": "cancellation_requested"}

    @app.get(prefix + "/memory")
    async def read_memory(request: Request):
        await current(request)
        store = store_for(request.state.principal)
        facts = await store.get_facts(request.state.principal.subject)
        return {"facts": [fact.model_dump(mode="json") for fact in facts]}

    @app.patch(prefix + "/memory")
    async def edit_memory(body: MemoryEdit, request: Request):
        await current(request)
        principal = request.state.principal
        store = store_for(principal)
        agent, _, _ = factory.build(principal, store)
        facts = {fact.key: fact for fact in await store.get_facts(principal.subject)}
        if body.key not in facts:
            raise HTTPException(404, "Fact not found")
        try:
            fact = agent.memory.validate(
                body.key,
                body.value,
                facts[body.key].category.value,
                source_session_id=facts[body.key].source_session_id,
            )
        except ValueError as error:
            raise HTTPException(422, "Memory edit violates the memory policy") from error
        await store.upsert_facts(principal.subject, [fact])
        return {"fact": fact.model_dump(mode="json")}

    @app.delete(prefix + "/memory")
    async def delete_memory(body: MemoryDelete, request: Request):
        await current(request)
        principal = request.state.principal
        store = store_for(principal)
        if body.key is not None:
            return {"ok": await store.delete_fact(principal.subject, body.key)}
        if not body.purge:
            raise HTTPException(422, "Specify a fact key or explicit purge=true")
        await store.clear(principal.subject)
        return {"ok": True}

    @app.post(prefix + "/reset")
    async def reset(body: ResetInput, request: Request):
        session = await current(request)
        if body.purge_memory:
            await store_for(request.state.principal).clear(request.state.principal.subject)
        cleanup = job_operation(
            str(uuid.uuid4()),
            {
                "principal": principal_payload(request.state.principal),
                "session_id": session.id,
                "vertical": settings.vertical,
            },
            purpose="purge",
        )
        await asyncio.to_thread(sessions.revoke, session, [cleanup])
        return await start(request)

    @app.post(prefix + "/auth/migrate")
    async def migrate(body: MigrationInput, request: Request):
        customer = request.state.principal
        guest, claims = await asyncio.to_thread(verifier.verify, body.guest_assertion)
        await asyncio.to_thread(verify_guest_registry, documents, guest, claims, allow_frozen=True)
        migration_id = str(uuid.uuid5(uuid.NAMESPACE_URL, guest.scope + ":" + customer.scope))
        work = job_operation(
            migration_id,
            {
                "principal": principal_payload(customer),
                "source": principal_payload(guest),
                "vertical": settings.vertical,
            },
            purpose="migration",
        )
        await migrate_memory(
            store_for(guest), store_for(customer), consent=body.consent, queued_work=work
        )
        from fastapi.responses import JSONResponse

        return JSONResponse({"status": "accepted", "migration_id": migration_id}, status_code=202)

    @app.post(prefix + "/auth/migrations/{migration_id}/status")
    async def migration_status(migration_id: uuid.UUID, body: MigrationInput, request: Request):
        customer = request.state.principal
        guest, claims = await asyncio.to_thread(verifier.verify, body.guest_assertion)
        await asyncio.to_thread(verify_guest_registry, documents, guest, claims, allow_frozen=True)
        expected = uuid.uuid5(uuid.NAMESPACE_URL, guest.scope + ":" + customer.scope)
        if (
            migration_id != expected
            or not body.consent
            or not guest.guest
            or customer.guest
            or customer.tenant != guest.tenant
        ):
            raise AccessDenied("Migration belongs to another identity")
        source = store_for(guest)
        job = await asyncio.to_thread(documents.read, source.pk, f"job:{migration_id}")
        if job is None:
            raise HTTPException(404, "Migration not found")
        return {"migration_id": str(migration_id), "status": job["status"]}

    async def direct_action(request: Request, tool: str, arguments: dict[str, Any]):
        session = await current(request)
        principal = request.state.principal
        key = request.headers.get("idempotency-key", "")
        try:
            id = str(uuid.UUID(key))
        except ValueError as error:
            raise HTTPException(400, "A UUID Idempotency-Key is required") from error
        old = await asyncio.to_thread(documents.read, session.pk, f"job:{id}")
        if old is not None:
            previous = old["payload"].get("action", {})
            if previous.get("arguments") != arguments or previous.get("tool") != tool:
                raise HTTPException(409, "Idempotency key names a different action")
        else:
            if session.document["active_turn"]:
                raise HTTPException(409, "Another turn or action is active")
            fingerprint = None
            if tool in {"apply_change", "discard_change"}:
                principal.require("approver")
                bundle = factory.bundle(principal)
                pending = await bundle.backend.get_pending_changes(
                    factory.context(principal, session.id)
                )
                target = next(
                    (item for item in pending if item.change_id == arguments["change_id"]),
                    None,
                )
                if target is None:
                    raise HTTPException(404, "Pending change not found")
                fingerprint = change_fingerprint(target)
            session.document["active_turn"] = id
            payload = {
                "principal": principal_payload(principal),
                "claims": {
                    key: request.state.claims[key]
                    for key in ("guest_epoch", "memory_consent", "jti")
                    if key in request.state.claims
                },
                "session_id": session.id,
                "provider": settings.provider_id,
                "vertical": settings.vertical,
                "action": {
                    "tool": tool,
                    "arguments": arguments,
                    "approved_until": time.time() + 300,
                    "target_fingerprint": fingerprint,
                },
            }
            await asyncio.to_thread(
                sessions.commit, session, [job_operation(id, payload, purpose="action")]
            )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            job = await asyncio.to_thread(documents.read, session.pk, f"job:{id}")
            if job and job["status"] == "complete":
                if not job["result"].get("ok"):
                    raise HTTPException(
                        400,
                        str(job["result"].get("reason") or "Action was rejected")[:300],
                    )
                return job["result"]
            if job and job["status"] not in {"ready", "running"}:
                raise HTTPException(409, "Action interrupted; reconcile before retrying")
            await asyncio.sleep(0.25)
        from fastapi.responses import JSONResponse

        return JSONResponse({"status": "accepted", "turn_id": id}, status_code=202)

    @app.post(prefix + "/cart/add")
    async def add_cart(request: Request):
        if settings.role != "shopping":
            raise HTTPException(404, "Not found")
        body = await request.json()
        if (
            not isinstance(body, dict)
            or set(body) != {"product_id", "quantity"}
            or not isinstance(body["product_id"], str)
            or not isinstance(body["quantity"], int)
            or isinstance(body["quantity"], bool)
            or not 1 <= body["quantity"] <= 100
        ):
            raise HTTPException(422, "Invalid cart arguments")
        if settings.vertical == "telecom":
            principal = request.state.principal
            bundle = factory.bundle(principal)
            session = await current(request)
            product = await bundle.backend.get_product_details(
                factory.context(principal, session.id), body["product_id"]
            )
            if product is None or product.category not in {"devices", "add-ons"}:
                raise HTTPException(400, "Plan changes require the assistant's disclosure flow")
        return await direct_action(request, "add_to_cart", body)

    @app.post(prefix + "/changes/{change_id:path}/{action}")
    async def change_action(change_id: str, action: str, request: Request):
        if settings.role != "merchant" or action not in {"apply", "discard"}:
            raise HTTPException(404, "Not found")
        return await direct_action(request, f"{action}_change", {"change_id": change_id})

    @app.get(prefix + "/{path:path}")
    async def surface(path: str, request: Request):
        session = await current(request)
        principal = request.state.principal
        memory = CosmosMemoryStore(documents, principal, retention_days=settings.memory_days)
        agent, _, bundle = factory.build(principal, memory)
        context = factory.context(principal, session.id)
        backend = bundle.backend
        if principal.guest and (
            path in {"tickets", "orders", "account"}
            or path.startswith(("tickets/", "orders/", "account/"))
        ):
            raise AccessDenied("Sign in to access an account, orders or tickets")
        handler = bundle.routes.get(f"GET /{path}")
        if handler is not None:
            return await handler(context, session, dict(request.query_params))
        if path.startswith("products/"):
            handler = bundle.routes.get("GET /products/{id}")
            if handler is not None:
                return await handler(
                    context,
                    session,
                    {**dict(request.query_params), "id": path[len("products/") :]},
                )
        if principal.role == "shopping":
            if path == "products":
                query = request.query_params.get("query", "")
                if not query:
                    raise HTTPException(
                        501, "Configure the backend's bounded catalog browser route"
                    )
                products = await backend.search_products(context, query, limit=100)
                return {"products": [item.model_dump(mode="json") for item in products]}
            if path.startswith("products/"):
                product = await backend.get_product_details(context, path[len("products/") :])
                if product is None:
                    raise HTTPException(404, "Product not found")
                return product.model_dump(mode="json")
            if path == "cart":
                from shopping_agent.serialization import cart_payload

                return cart_payload(await backend.get_cart(context))
            if path in {"orders", "account"} and principal.guest:
                raise AccessDenied("Sign in to access an account or orders")
            if path == "orders":
                return {
                    "orders": [
                        item.model_dump(mode="json")
                        for item in await backend.get_orders(context, limit=20)
                    ]
                }
            if path == "account":
                return {"account": await backend.get_account_context(context)}
        else:
            if path == "listings":
                items = await backend.search_listings(
                    context, request.query_params.get("query", ""), limit=100
                )
                return {
                    "total": len(items),
                    "listings": [item.model_dump(mode="json") for item in items],
                }
            if path.startswith("listings/"):
                id = path[len("listings/") :]
                listing = await backend.get_listing(context, id)
                if listing is None:
                    raise HTTPException(404, "Listing not found")
                pricing = await backend.get_pricing_context(context, id)
                return {
                    "listing": listing.model_dump(mode="json"),
                    "pricing": pricing.model_dump(mode="json") if pricing else None,
                }
            if path == "alerts":
                return {
                    "inventory": [
                        item.model_dump(mode="json")
                        for item in await backend.get_inventory_alerts(context)
                    ],
                    "order_issues": [
                        item.model_dump(mode="json")
                        for item in await backend.get_order_issues(context)
                    ],
                }
        handler = bundle.routes.get(f"GET /{path}")
        if handler is None:
            raise HTTPException(404, "Backend capability not configured")
        return await handler(context, session, request.query_params)

    @app.post(prefix + "/{path:path}")
    async def vertical_action(path: str, request: Request):
        allowed = {
            "holds/release",
            "waitlist/join",
            "waitlist/claim",
            "tickets/transfer",
            "tickets/transfer/cancel",
        }
        if settings.role != "shopping" or path not in allowed:
            raise HTTPException(404, "Not found")
        if request.state.principal.guest and path.startswith("tickets/"):
            raise AccessDenied("Sign in to manage tickets")
        body = await request.json()
        if not isinstance(body, dict) or len(json.dumps(body).encode()) > 8192:
            raise HTTPException(422, "Invalid action arguments")
        return await direct_action(request, f"route:POST /{path}", body)

    return app
