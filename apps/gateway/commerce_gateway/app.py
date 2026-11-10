import hashlib
import hmac
import json
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from urllib.parse import unquote

import httpx
import jwt
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse

from .oidc import OIDC

SESSION_COOKIE = "__Host-commerce-session"
GUEST_COOKIE = "__Host-commerce-guest"
FLOW_COOKIE = "__Host-commerce-flow"
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
SHOPPING_ROUTES = {
    "session",
    "chat",
    "products",
    "cart",
    "orders",
    "memory",
    "reset",
    "turns",
    "account",
    "holds",
    "waitlist",
    "tickets",
}
HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "content-length",
}


def record_key(kind: str, credential: str) -> str:
    return kind + ":" + hashlib.sha256(credential.encode()).hexdigest()


def canonical_path(raw: str) -> str:
    path = raw
    for _ in range(4):
        decoded = unquote(path, errors="strict")
        if decoded == path:
            break
        path = decoded
    if (
        "%" in path
        or "\\" in path
        or "//" in path
        or ";" in path
        or "?" in path
        or "#" in path
        or any(ord(char) < 32 or ord(char) == 127 for char in path)
        or any(part in {".", ".."} for part in path.split("/"))
    ):
        raise HTTPException(400, "Invalid request path")
    return path


def cookie(response, name, value, max_age):
    response.set_cookie(
        name, value, max_age=max_age, secure=True, httponly=True, samesite="lax", path="/"
    )


def create_app(settings, storage, client=None, oidc=None):
    owns_client = client is None
    client = client or httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10, read=230, write=30, pool=10),
        follow_redirects=False,
    )
    oidc = oidc or OIDC(settings, client)
    # PKCS8 RSA and Ed25519 both use BEGIN PRIVATE KEY.
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

    key = serialization.load_pem_private_key(settings.signing_key.encode(), password=None)
    if isinstance(key, ed25519.Ed25519PrivateKey):
        algorithm = "EdDSA"
    elif isinstance(key, rsa.RSAPrivateKey) and key.key_size >= 2048:
        algorithm = "RS256"
    else:
        raise ValueError("Gateway signing key must be Ed25519 or RSA >=2048 bits")
    jwk = json.loads(jwt.algorithms.get_default_algorithms()[algorithm].to_jwk(key.public_key()))
    jwk.update(kid=settings.signing_kid, use="sig", alg=algorithm)

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            if owns_client:
                await client.aclose()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    auth_prefix = "/api" if settings.role == "shopping" else "/api/merchant"

    def assertion(guest):
        now = int(time.time())
        return jwt.encode(
            {
                "iss": settings.gateway_issuer,
                "aud": settings.audience,
                "sub": guest["principal_id"],
                "tid": settings.tenant,
                "role": "shopping",
                "roles": [],
                "kind": "guest",
                "guest": True,
                "guest_epoch": guest.get("guest_epoch", 0),
                "jti": guest["revocation_id"],
                "memory_consent": guest["memory_consent"],
                "iat": now,
                "exp": now + 120,
            },
            key,
            algorithm=algorithm,
            headers={"kid": settings.signing_kid},
        )

    def csrf(request, record):
        origin = request.headers.get("origin", "")
        supplied = request.headers.get("x-csrf-token", "")
        if origin != settings.origin or not hmac.compare_digest(supplied, record.get("csrf", "")):
            raise HTTPException(403, "CSRF validation failed")
        if not supplied:
            raise HTTPException(403, "CSRF token required")

    async def json_body(request):
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 4096:
                raise HTTPException(413, "Identity request body too large")
        try:
            data = json.loads(body)
        except (ValueError, UnicodeError) as exc:
            raise HTTPException(400, "Invalid JSON request") from exc
        if not isinstance(data, dict):
            raise HTTPException(400, "JSON object required")
        return data

    async def identity(request, required=True):
        sid = request.cookies.get(SESSION_COOKIE)
        if sid:
            record = await storage.get(record_key("session", sid))
            if record and record["expires"] > time.time():
                return record, record["access_token"]
            raise HTTPException(401, "Session expired; sign in again")
        gid = request.cookies.get(GUEST_COOKIE)
        if gid and settings.role == "shopping":
            guest_key = record_key("guest", gid)
            record = await storage.get(guest_key)
            if record:
                request.state.renew_guest = gid
                return record, assertion(record)
            raise HTTPException(401, "Guest identity expired")
        if required:
            raise HTTPException(401, "Authentication required")
        return None, None

    async def internal(path, token, body=None, guest_token=None, method="POST"):
        headers = {"Authorization": "Bearer " + token}
        if guest_token:
            headers["X-Commerce-Guest-Assertion"] = guest_token
        response = await client.request(
            method, settings.api_upstream + path, headers=headers, json=body
        )
        if not response.is_success:
            raise HTTPException(
                response.status_code if response.status_code < 500 else 502,
                "Identity operation was refused",
            )
        return response

    @app.middleware("http")
    async def security(request, call_next):
        try:
            raw = request.scope.get("raw_path", b"/").decode("ascii")
            path = canonical_path(raw)
            # Do not let browser bearer/forwarded identity headers override BFF credentials.
            if request.headers.get("authorization"):
                return JSONResponse(
                    {"detail": "Browser bearer credentials are not accepted"}, status_code=400
                )
            if settings.role == "shopping" and (
                path.startswith(("/merchant", "/portal", "/api/merchant"))
                or path.startswith("/api/")
                and path.split("/")[2] not in SHOPPING_ROUTES | {"auth"}
            ):
                return JSONResponse({"detail": "Not found"}, status_code=404)
            if (
                settings.role == "merchant"
                and path.startswith("/api/")
                and not (path.startswith("/api/merchant/") or path.startswith("/api/auth/"))
            ):
                return JSONResponse({"detail": "Not found"}, status_code=404)
            request.state.canonical_path = path
            response = await call_next(request)
            renew = getattr(request.state, "renew_guest", None)
            if (
                renew
                and response.status_code < 400
                and path not in {"/auth/logout", "/api/auth/migrate"}
                and await storage.touch(
                    record_key("guest", renew), int(time.time()) + settings.guest_seconds
                )
            ):
                cookie(response, GUEST_COOKIE, renew, settings.guest_seconds)
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
            response.headers["Content-Security-Policy"] = (
                "frame-ancestors 'none'; base-uri 'self'; object-src 'none'"
            )
            if path.startswith(("/auth/", "/api/")):
                response.headers["Cache-Control"] = "no-store"
            return response
        except (HTTPException, UnicodeError) as exc:
            return JSONResponse(
                {"detail": getattr(exc, "detail", "Invalid request path")},
                status_code=getattr(exc, "status_code", 400),
            )
        except Exception:
            # Never render token exchange failures, claims, upstream URLs, or key material.
            return JSONResponse({"detail": "Gateway operation failed"}, status_code=502)

    @app.get("/health/live")
    async def live():
        return {"status": "live"}

    @app.get("/health/ready")
    async def ready():
        try:
            await storage.get("readiness")
            await oidc.discover()
            response = await client.get(settings.api_upstream + "/health/ready")
            response.raise_for_status()
            return {"status": "ready"}
        except Exception:
            return JSONResponse({"status": "not_ready"}, status_code=503)

    @app.get("/.well-known/jwks.json")
    async def jwks():
        return {"keys": [jwk]}

    @app.get("/auth/login")
    async def login(request: Request):
        network = request.client.host if request.client else "unknown"
        if not await storage.rate_limit("login:" + network, 60, 600):
            raise HTTPException(429, "Sign-in rate limit exceeded")
        flow_id = secrets.token_urlsafe(32)
        flow = {name: secrets.token_urlsafe(32) for name in ("state", "nonce", "verifier")}
        await storage.put(record_key("flow", flow_id), flow, int(time.time()) + 600)
        response = RedirectResponse(await oidc.authorization_url(flow), status_code=303)
        cookie(response, FLOW_COOKIE, flow_id, 600)
        return response

    @app.get("/auth/callback")
    async def callback(request: Request):
        flow_id = request.cookies.get(FLOW_COOKIE, "")
        flow = await storage.consume(record_key("flow", flow_id))
        if not flow or not hmac.compare_digest(
            request.query_params.get("state", ""), flow["state"]
        ):
            raise HTTPException(400, "Invalid or expired sign-in flow")
        code = request.query_params.get("code")
        if not code:
            raise HTTPException(400, "Sign-in did not complete")
        token, claims = await oidc.exchange(code, flow)
        if settings.role == "merchant":
            roles = claims.get("roles", [])
            if (
                not isinstance(roles, list)
                or not all(isinstance(role, str) for role in roles)
                or "merchant" not in roles
            ):
                raise HTTPException(403, "Workforce merchant permission required")
        expires = min(int(time.time()) + settings.session_seconds, int(claims["exp"]))
        sid = secrets.token_urlsafe(32)
        await storage.put(
            record_key("session", sid),
            {
                "kind": "customer" if settings.role == "shopping" else "merchant",
                "csrf": secrets.token_urlsafe(32),
                "access_token": token,
                "expires": expires,
                "subject": claims.get("oid", claims["sub"]),
            },
            expires,
        )
        previous = request.cookies.get(SESSION_COOKIE)
        if previous:
            await storage.delete(record_key("session", previous))
        response = RedirectResponse("/", status_code=303)
        cookie(response, SESSION_COOKIE, sid, expires - int(time.time()))
        response.delete_cookie(FLOW_COOKIE, secure=True, httponly=True, samesite="lax")
        # Retain the guest credential separately, for explicit possession-based migration only.
        return response

    @app.get("/api/auth/session")
    async def session(request: Request):
        record, token = await identity(request, required=False)
        if not record:
            return {
                "authenticated": False,
                "role": settings.role,
                "deployment_mode": settings.mode,
            }
        await internal(auth_prefix + "/auth/session", token, method="GET")
        guest = await storage.get(record_key("guest", request.cookies.get(GUEST_COOKIE, "")))
        migration = await storage.get(
            record_key("migration", request.cookies.get(SESSION_COOKIE, ""))
        )
        return {
            "authenticated": True,
            "kind": record["kind"],
            "role": settings.role,
            "deployment_mode": settings.mode,
            "csrf_token": record["csrf"],
            "memory_consent": record.get("memory_consent", False),
            "migration_available": record["kind"] == "customer"
            and guest is not None
            and migration is None,
            "migration": {"id": migration["id"], "status": migration["status"]}
            if migration
            else None,
        }

    @app.post("/api/auth/guest/bootstrap")
    async def bootstrap(request: Request):
        if settings.role != "shopping":
            raise HTTPException(404, "Not found")
        if request.headers.get("origin") != settings.origin:
            raise HTTPException(403, "Same-origin request required")
        network = request.client.host if request.client else "unknown"
        if not await storage.rate_limit("bootstrap:" + network, 20, 600):
            raise HTTPException(429, "Guest bootstrap rate limit exceeded")
        record, _ = await identity(request, required=False)
        if record:
            raise HTTPException(409, "Identity already exists")
        body = await json_body(request)
        if type(body.get("memory_consent")) is not bool:
            raise HTTPException(400, "Explicit memory consent choice required")
        gid = secrets.token_urlsafe(32)
        record = {
            "kind": "guest",
            "principal_id": secrets.token_urlsafe(32),
            "revocation_id": secrets.token_urlsafe(32),
            "guest_epoch": 0,
            "memory_consent": body["memory_consent"],
            "csrf": secrets.token_urlsafe(32),
        }
        await internal(
            "/api/auth/guest/bootstrap",
            assertion(record),
            {
                "principal_id": record["principal_id"],
                "revocation_id": record["revocation_id"],
                "memory_consent": record["memory_consent"],
            },
        )
        await storage.put(
            record_key("guest", gid), record, int(time.time()) + settings.guest_seconds
        )
        response = JSONResponse(
            {
                "kind": "guest",
                "csrf_token": record["csrf"],
                "memory_consent": record["memory_consent"],
            },
            status_code=201,
        )
        cookie(response, GUEST_COOKIE, gid, settings.guest_seconds)
        return response

    @app.post("/api/auth/migrate")
    async def migrate(request: Request):
        record, token = await identity(request)
        csrf(request, record)
        guest_key = record_key("guest", request.cookies.get(GUEST_COOKIE, ""))
        guest = await storage.get(guest_key)
        body = await json_body(request)
        if (
            settings.role != "shopping"
            or record["kind"] != "customer"
            or not guest
            or body.get("consent") is not True
        ):
            raise HTTPException(
                400, "Customer sign-in, guest possession, and explicit consent required"
            )
        result = await internal(
            "/api/auth/migrate", token, {"consent": True, "guest_assertion": assertion(guest)}
        )
        data = result.json()
        if result.status_code == 202:
            migration_id = str(uuid.UUID(data["migration_id"]))
            await storage.put(
                record_key("migration", request.cookies.get(SESSION_COOKIE, "")),
                {
                    "id": migration_id,
                    "status": "accepted",
                    "guest": guest,
                    "guest_key": guest_key,
                    "customer_subject": record["subject"],
                    "consent": True,
                },
                record["expires"],
            )
            return JSONResponse(
                {"status": "accepted", "migration_id": migration_id}, status_code=202
            )
        if data.get("status") != "complete":
            raise HTTPException(502, "Migration did not report a durable outcome")
        await storage.delete(guest_key)
        response = JSONResponse({"status": "complete"})
        response.delete_cookie(GUEST_COOKIE, secure=True, httponly=True, samesite="lax")
        return response

    @app.post("/api/auth/migrations/{migration_id}/status")
    async def migration_status(migration_id: str, request: Request):
        record, token = await identity(request)
        csrf(request, record)
        key = record_key("migration", request.cookies.get(SESSION_COOKIE, ""))
        migration = await storage.get(key)
        if (
            settings.role != "shopping"
            or record["kind"] != "customer"
            or not migration
            or migration["id"] != migration_id
            or migration["customer_subject"] != record["subject"]
        ):
            raise HTTPException(404, "Migration not found")
        result = await internal(
            "/api/auth/migrations/" + migration["id"] + "/status",
            token,
            {"consent": True, "guest_assertion": assertion(migration["guest"])},
        )
        status = result.json()["status"]
        if status not in {
            "accepted",
            "ready",
            "pending",
            "running",
            "retry",
            "complete",
            "failed",
            "dead",
            "cancelled",
            "uncertain",
        }:
            raise HTTPException(502, "Unknown migration status")
        response = JSONResponse({"migration_id": migration_id, "status": status})
        if status == "complete":
            await storage.delete(migration["guest_key"])
            await storage.delete(key)
            response.delete_cookie(GUEST_COOKIE, secure=True, httponly=True, samesite="lax")
        return response

    @app.post("/auth/logout")
    async def logout(request: Request):
        record, token = await identity(request)
        csrf(request, record)
        guest = await storage.get(record_key("guest", request.cookies.get(GUEST_COOKIE, "")))
        migration_key = record_key("migration", request.cookies.get(SESSION_COOKIE, ""))
        migration = await storage.get(migration_key)
        if not guest and migration:
            guest = migration["guest"]
        await internal(
            auth_prefix + "/auth/logout",
            token,
            guest_token=assertion(guest) if guest and record["kind"] != "guest" else None,
        )
        for kind, name in (("session", SESSION_COOKIE), ("guest", GUEST_COOKIE)):
            credential = request.cookies.get(name)
            if credential:
                await storage.delete(record_key(kind, credential))
        await storage.delete(migration_key)
        response = JSONResponse({"ok": True})
        for name in (SESSION_COOKIE, GUEST_COOKIE, FLOW_COOKIE):
            response.delete_cookie(name, secure=True, httponly=True, samesite="lax")
        return response

    @app.api_route("/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"])
    async def proxy(request: Request, path: str):
        path = request.state.canonical_path
        is_api = path.startswith("/api/")
        if path.startswith(("/auth/", "/api/auth/")):
            raise HTTPException(404, "Not found")
        headers = {}
        if is_api:
            record, token = await identity(request)
            if request.method in UNSAFE:
                csrf(request, record)
            headers["Authorization"] = "Bearer " + token
            for name in (
                "content-type",
                "accept",
                "x-session-id",
                "idempotency-key",
                "last-event-id",
            ):
                if name in request.headers:
                    headers[name] = request.headers[name]
        else:
            if settings.role == "merchant":
                record, _ = await identity(request, required=False)
                if not record:
                    return RedirectResponse("/auth/login", status_code=303)
            # Never forward auth cookies or client-supplied identity headers to Next.js.
            for name in (
                "accept",
                "content-type",
                "rsc",
                "next-router-state-tree",
                "next-router-prefetch",
                "next-url",
                "if-none-match",
            ):
                if name in request.headers:
                    headers[name] = request.headers[name]
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > settings.max_body_bytes:
                raise HTTPException(413, "Request body too large")
        upstream = settings.api_upstream if is_api else settings.web_upstream
        url = httpx.URL(upstream + path).copy_with(query=request.scope.get("query_string", b""))
        req = client.build_request(request.method, url, headers=headers, content=bytes(body))
        result = await client.send(req, stream=True)
        outgoing = {
            name: value
            for name, value in result.headers.items()
            if name.lower() not in HOP_HEADERS | {"set-cookie", "content-encoding", "server"}
        }
        if result.headers.get("content-type", "").startswith("text/event-stream"):
            outgoing.update({"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
        # Upstream redirects cannot leak tokens/cookies and are never followed server-side.
        if 300 <= result.status_code < 400 and "location" in outgoing:
            location = outgoing["location"]
            if not location.startswith("/") or location.startswith("//"):
                await result.aclose()
                raise HTTPException(502, "Unsafe upstream redirect")

        async def chunks():
            try:
                async for chunk in result.aiter_bytes():
                    if await request.is_disconnected():
                        break
                    yield chunk
            finally:
                await result.aclose()

        return StreamingResponse(chunks(), status_code=result.status_code, headers=outgoing)

    return app
