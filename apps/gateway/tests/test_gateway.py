import json
import time
from dataclasses import replace

import httpx
import jwt
import pytest
from commerce_gateway.app import (
    GUEST_COOKIE,
    SESSION_COOKIE,
    canonical_path,
    create_app,
    record_key,
)
from commerce_gateway.config import Settings
from commerce_gateway.oidc import OIDC, pkce_challenge
from commerce_gateway.storage import CosmosAuthStorage, MemoryAuthStorage
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException


@pytest.fixture
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def settings(key):
    return Settings(
        role="shopping",
        origin="https://shop.example",
        api_upstream="http://api:8080",
        web_upstream="http://web:3000",
        issuer="https://login.example/tenant",
        client_id="client",
        client_secret="configured-secret",
        audience="commerce-api",
        tenant="tenant",
        gateway_issuer="https://shop.example",
        signing_kid="key-1",
        signing_key=key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
        encryption_keys=Fernet.generate_key().decode(),
        cosmos_endpoint="https://cosmos.example",
        cosmos_database="commerce",
        cosmos_container="auth",
    )


class FakeOIDC:
    async def discover(self):
        return {}

    async def authorization_url(self, flow):
        return "https://login.example/authorize?state=" + flow["state"]

    async def exchange(self, code, flow):
        return "server-access-token", {"sub": "customer", "exp": int(time.time()) + 3600}


def setup(settings, handler=None, oidc=None):
    requests = []

    def handle(request):
        requests.append(request)
        if handler:
            return handler(request)
        return httpx.Response(200, json={"ok": True})

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    store = MemoryAuthStorage()
    app = create_app(settings, store, upstream, oidc or FakeOIDC())
    browser = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=settings.origin)
    return browser, store, requests


@pytest.mark.parametrize(
    "path",
    [
        "/api/merchant/overview",
        "/api/%6derchant/overview",
        "/api/%256derchant/overview",
        "/%61pi/merchant/chat",
        "/merchant",
        "/portal",
        "/api/admin",
        "/api/merchantish",
    ],
)
async def test_public_gateway_denies_merchant(settings, path):
    browser, _, requests = setup(settings)
    async with browser:
        response = await browser.get(path)
    assert response.status_code == 404
    assert not requests


@pytest.mark.parametrize(
    "path",
    [
        "/api/%2e%2e/merchant",
        "/api\\merchant",
        "/api//merchant",
        "/api/merchant;other",
        "/api/%25252fmerchant",
        "/api/%00merchant",
        "/api/products%3fmerchant",
        "/api/products%23merchant",
    ],
)
def test_ambiguous_paths_rejected(path):
    with pytest.raises(HTTPException):
        canonical_path(path)


def test_private_boundary_required(settings):
    with pytest.raises(ValueError, match="private network"):
        replace(settings, role="merchant")
    assert replace(settings, role="merchant", private_boundary=True).role == "merchant"


@pytest.mark.parametrize("mode", ["production", "fixture"])
async def test_runtime_demo_label_comes_from_validated_server_configuration(settings, mode):
    browser, _, requests = setup(replace(settings, mode=mode))
    async with browser:
        response = await browser.get("/api/auth/session")
    assert response.status_code == 200
    assert response.json() == {
        "authenticated": False,
        "role": "shopping",
        "deployment_mode": mode,
    }
    assert not requests


def test_gateway_rejects_unknown_deployment_mode(settings):
    with pytest.raises(ValueError, match="COMMERCE_MODE"):
        replace(settings, mode="demo")


@pytest.mark.parametrize(
    "changes",
    [
        {"role": "admin"},
        {"origin": "http://insecure.example"},
        {"issuer": "https://user:password@login.example"},
        {"client_secret": ""},
        {"api_upstream": "http://api/private"},
        {"session_seconds": 299},
        {"guest_seconds": 2592001},
    ],
)
def test_configuration_rejects_insecure_or_missing_settings(settings, changes):
    with pytest.raises(ValueError):
        replace(settings, **changes)


def test_configuration_repr_never_exposes_secrets(settings):
    shown = repr(settings)
    assert settings.client_secret not in shown
    assert settings.signing_key not in shown
    assert settings.encryption_keys not in shown


async def test_private_merchant_requires_workforce_sign_in(settings):
    browser, _, requests = setup(replace(settings, role="merchant", private_boundary=True))
    async with browser:
        response = await browser.get("/")
        assert response.status_code == 303 and response.headers["location"] == "/auth/login"
        assert (await browser.get("/api/merchant/overview")).status_code == 401
        assert (await browser.post("/api/auth/guest/bootstrap")).status_code == 404
    assert not requests


async def test_merchant_auth_uses_private_host_role_prefix(settings):
    browser, store, requests = setup(replace(settings, role="merchant", private_boundary=True))
    await store.put(
        record_key("session", "workforce"),
        {
            "kind": "merchant",
            "subject": "operator",
            "csrf": "csrf",
            "access_token": "server-workforce-token",
            "expires": int(time.time()) + 100,
        },
        int(time.time()) + 100,
    )
    async with browser:
        browser.cookies.set(SESSION_COOKIE, "workforce")
        response = await browser.get("/api/auth/session")
        assert response.status_code == 200
        assert requests[-1].url.path == "/api/merchant/auth/session"
        response = await browser.post(
            "/auth/logout",
            headers={
                "Origin": settings.origin,
                "X-CSRF-Token": "csrf",
            },
        )
        assert response.status_code == 200
        assert requests[-1].url.path == "/api/merchant/auth/logout"


@pytest.mark.parametrize(
    "roles,expected",
    [
        (None, 403),
        ("merchant", 403),
        (["customer"], 403),
        (["merchant", 123], 403),
        (["merchant"], 303),
    ],
)
async def test_merchant_oidc_requires_strict_workforce_role(settings, roles, expected):
    class WorkforceOIDC(FakeOIDC):
        async def exchange(self, code, flow):
            token, claims = await super().exchange(code, flow)
            return token, {**claims, "roles": roles}

    browser, _, _ = setup(
        replace(settings, role="merchant", private_boundary=True), oidc=WorkforceOIDC()
    )
    async with browser:
        login = await browser.get("/auth/login")
        state = login.headers["location"].split("state=")[1]
        response = await browser.get("/auth/callback", params={"state": state, "code": "code"})
        assert response.status_code == expected


async def test_guest_auth_payloads_are_strict_and_bounded(settings):
    browser, _, requests = setup(settings)
    async with browser:
        headers = {"Origin": settings.origin}
        assert (
            await browser.post(
                "/api/auth/guest/bootstrap", headers=headers, json={"memory_consent": "false"}
            )
        ).status_code == 400
        assert (
            await browser.post("/api/auth/guest/bootstrap", headers=headers, json=[])
        ).status_code == 400
        assert (
            await browser.post("/api/auth/guest/bootstrap", headers=headers, content=b"x" * 4097)
        ).status_code == 413
    assert not requests


async def test_guest_activity_slides_expiry_without_plaintext_credential_ids(settings):
    browser, store, _ = setup(settings)
    async with browser:
        await browser.post(
            "/api/auth/guest/bootstrap",
            headers={"Origin": settings.origin},
            json={"memory_consent": False},
        )
        credential = browser.cookies.get(GUEST_COOKIE)
        key = record_key("guest", credential)
        record, _ = store.records[key]
        store.records[key] = (record, int(time.time()) + 30)
        response = await browser.get("/api/auth/session")
        assert response.status_code == 200
        assert "Max-Age=2592000" in response.headers["set-cookie"]
        assert store.records[key][1] > time.time() + 2591900
        assert all(credential not in stored_key for stored_key in store.records)


@pytest.mark.parametrize("audience", ["commerce-api", "api://registered-commerce-api"])
async def test_guest_requires_consent_origin_and_creates_signed_registry_binding(
    settings, key, audience
):
    settings = replace(settings, audience=audience)
    browser, store, requests = setup(settings)
    async with browser:
        assert (
            await browser.post("/api/auth/guest/bootstrap", json={"memory_consent": False})
        ).status_code == 403
        assert (
            await browser.post(
                "/api/auth/guest/bootstrap", headers={"Origin": settings.origin}, json={}
            )
        ).status_code == 400
        response = await browser.post(
            "/api/auth/guest/bootstrap",
            headers={"Origin": settings.origin},
            json={"memory_consent": False},
        )
        assert response.status_code == 201
        assert all(
            flag in response.headers["set-cookie"]
            for flag in ["HttpOnly", "Secure", "SameSite=lax", "Path=/"]
        )
        token = requests[-1].headers["authorization"].removeprefix("Bearer ")
        claims = jwt.decode(
            token,
            key.public_key(),
            algorithms=["RS256"],
            audience=audience,
            issuer=settings.gateway_issuer,
        )
        assert claims["guest"] is True and claims["roles"] == [] and claims["guest_epoch"] == 0
        assert claims["memory_consent"] is False
        assert claims["exp"] - claims["iat"] == 120
        assert "access_token" not in response.json()
        state = (await browser.get("/api/auth/session")).json()
        assert state["kind"] == "guest"
        assert (await browser.post("/api/chat", json={"message": "hi"})).status_code == 403
        response = await browser.post(
            "/api/chat",
            headers={
                "Origin": settings.origin,
                "X-CSRF-Token": state["csrf_token"],
            },
            json={"message": "hi"},
        )
        assert response.status_code == 200
        assert "cookie" not in requests[-1].headers
        assert requests[-1].headers["authorization"].startswith("Bearer ")


async def test_bad_browser_bearer_never_downgrades(settings):
    browser, _, requests = setup(settings)
    async with browser:
        response = await browser.get("/api/products", headers={"Authorization": "Bearer invalid"})
    assert response.status_code == 400
    assert not requests


async def test_callback_one_use_and_session_rotation(settings):
    browser, store, requests = setup(settings)
    async with browser:
        login = await browser.get("/auth/login")
        state = login.headers["location"].split("state=")[1]
        flow_cookie = browser.cookies.get("__Host-commerce-flow")
        response = await browser.get("/auth/callback", params={"state": state, "code": "code"})
        assert response.status_code == 303
        session_cookie = browser.cookies.get(SESSION_COOKIE)
        stored = await store.get(record_key("session", session_cookie))
        assert stored["access_token"] == "server-access-token"
        browser.cookies.set("__Host-commerce-flow", flow_cookie)
        assert (
            await browser.get("/auth/callback", params={"state": state, "code": "code"})
        ).status_code == 400
        response = await browser.get("/api/products", headers={"X-User-Id": "attacker"})
        assert response.status_code == 200
        assert requests[-1].headers["Authorization"] == "Bearer server-access-token"
        assert "X-User-Id" not in requests[-1].headers
        assert "cookie" not in requests[-1].headers


async def test_migration_explicit_and_logout_revokes(settings):
    browser, store, requests = setup(
        settings, lambda request: httpx.Response(200, json={"status": "complete"})
    )
    async with browser:
        await browser.post(
            "/api/auth/guest/bootstrap",
            headers={"Origin": settings.origin},
            json={"memory_consent": True},
        )
        guest_cookie = browser.cookies.get(GUEST_COOKIE)
        await store.put(
            record_key("session", "signed-in"),
            {
                "kind": "customer",
                "csrf": "csrf",
                "access_token": "access",
                "expires": int(time.time()) + 100,
            },
            int(time.time()) + 100,
        )
        browser.cookies.set(SESSION_COOKIE, "signed-in")
        headers = {"Origin": settings.origin, "X-CSRF-Token": "csrf"}
        assert (
            await browser.post("/api/auth/migrate", headers=headers, json={"consent": False})
        ).status_code == 400
        assert await store.get(record_key("guest", guest_cookie))
        response = await browser.post("/api/auth/migrate", headers=headers, json={"consent": True})
        assert response.status_code == 200
        assert json.loads(requests[-1].content)["guest_assertion"]
        assert await store.get(record_key("guest", guest_cookie)) is None
        assert (await browser.post("/auth/logout", headers=headers)).status_code == 200
        assert await store.get(record_key("session", "signed-in")) is None
        assert requests[-1].url.path == "/api/auth/logout"


async def test_migration_acceptance_retains_proof_until_durable_completion(settings):
    migration_id = "84979d1d-be20-4b54-a452-1463983aff58"
    status = "ready"

    def handler(request):
        if request.url.path == "/api/auth/migrate":
            return httpx.Response(202, json={"status": "accepted", "migration_id": migration_id})
        if request.url.path.endswith("/status"):
            return httpx.Response(200, json={"status": status, "migration_id": migration_id})
        return httpx.Response(200, json={"ok": True})

    browser, store, requests = setup(settings, handler)
    async with browser:
        await browser.post(
            "/api/auth/guest/bootstrap",
            headers={"Origin": settings.origin},
            json={"memory_consent": True},
        )
        guest_credential = browser.cookies.get(GUEST_COOKIE)
        guest_key = record_key("guest", guest_credential)
        await store.put(
            record_key("session", "signed-in"),
            {
                "kind": "customer",
                "subject": "customer",
                "csrf": "csrf",
                "access_token": "access",
                "expires": int(time.time()) + 100,
            },
            int(time.time()) + 100,
        )
        browser.cookies.set(SESSION_COOKIE, "signed-in")
        headers = {"Origin": settings.origin, "X-CSRF-Token": "csrf"}
        response = await browser.post("/api/auth/migrate", headers=headers, json={"consent": True})
        assert response.status_code == 202
        assert response.json() == {"status": "accepted", "migration_id": migration_id}
        assert await store.get(guest_key)
        migration_key = record_key("migration", "signed-in")
        assert (await store.get(migration_key))["guest"]["memory_consent"] is True
        state = (await browser.get("/api/auth/session")).json()
        assert state["migration"] == {"id": migration_id, "status": "accepted"}
        assert state["migration_available"] is False
        assert "guest_assertion" not in json.dumps(state)
        path = "/api/auth/migrations/" + migration_id + "/status"
        assert (await browser.post(path, headers=headers)).json()["status"] == "ready"
        assert await store.get(guest_key)
        assert json.loads(requests[-1].content)["guest_assertion"]
        status = "complete"
        assert (await browser.post(path, headers=headers)).json()["status"] == "complete"
        assert await store.get(guest_key) is None
        assert await store.get(migration_key) is None


async def test_proxy_sse_and_bounded_body(settings):
    browser, store, requests = setup(
        settings,
        lambda request: httpx.Response(
            200,
            headers={
                "Content-Type": "text/event-stream",
                "X-Turn-Id": "turn1",
                "Set-Cookie": "bad=1",
                "Connection": "keep-alive",
            },
            content=b'id: 1\nevent: text_delta\ndata: {"text":"hello"}\n\n',
        ),
    )
    await store.put(
        record_key("session", "session"),
        {
            "kind": "customer",
            "csrf": "csrf",
            "access_token": "access",
            "expires": int(time.time()) + 100,
        },
        int(time.time()) + 100,
    )
    async with browser:
        browser.cookies.set(SESSION_COOKIE, "session")
        response = await browser.get("/api/turns/turn1/events", headers={"Last-Event-ID": "0"})
        assert response.headers["x-accel-buffering"] == "no"
        assert response.headers["x-turn-id"] == "turn1"
        assert "set-cookie" not in response.headers
        assert "connection" not in response.headers
        assert response.text.startswith("id: 1\n")
        assert requests[-1].headers["Last-Event-ID"] == "0"
        response = await browser.post(
            "/api/chat",
            headers={
                "Origin": settings.origin,
                "X-CSRF-Token": "csrf",
            },
            content=b"x" * (settings.max_body_bytes + 1),
        )
        assert response.status_code == 413


async def test_store_expiry_and_atomic_consumption():
    import asyncio

    store = MemoryAuthStorage()
    await store.put("expired", {"x": 1}, int(time.time()) - 1)
    assert await store.get("expired") is None
    assert not await store.touch("expired", int(time.time()) + 60)
    assert not await store.touch("missing", int(time.time()) + 60)
    await store.put("once", {"x": 1}, int(time.time()) + 10)
    results = await asyncio.gather(store.consume("once"), store.consume("once"))
    assert sum(value is not None for value in results) == 1
    assert await store.rate_limit("network", 1, 60)
    assert not await store.rate_limit("network", 1, 60)


async def test_oidc_validates_signature_issuer_audience_tenant_and_nonce(settings, key):
    public = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    public["kid"] = "oidc-key"
    now = int(time.time())
    payload = {
        "iss": settings.issuer,
        "sub": "subject",
        "tid": settings.tenant,
        "aud": settings.audience,
        "iat": now,
        "exp": now + 60,
    }

    def handler(request):
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": settings.issuer,
                    "authorization_endpoint": "https://login.example/authorize",
                    "token_endpoint": "https://login.example/token",
                    "jwks_uri": "https://login.example/keys",
                },
            )
        return httpx.Response(200, json={"keys": [public]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        oidc = OIDC(settings, client)
        token = jwt.encode(payload, key, algorithm="RS256", headers={"kid": "oidc-key"})
        assert (await oidc.validate(token, settings.audience))["sub"] == "subject"
        for field, value in [
            ("iss", "https://evil.example"),
            ("aud", "other"),
            ("tid", "other"),
            ("exp", now - 60),
        ]:
            bad = jwt.encode(
                {**payload, field: value}, key, algorithm="RS256", headers={"kid": "oidc-key"}
            )
            with pytest.raises((ValueError, jwt.PyJWTError)):
                await oidc.validate(bad, settings.audience)
        with pytest.raises(ValueError):
            await oidc.validate(
                jwt.encode(payload, "secret" * 10, algorithm="HS256"), settings.audience
            )
    assert (
        pkce_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")
        == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    )


async def test_oidc_exchange_binds_nonce_and_access_subject(settings, key):
    public = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    public["kid"] = "oidc-key"
    now = int(time.time())
    base = {
        "iss": settings.issuer,
        "sub": "subject",
        "tid": settings.tenant,
        "iat": now,
        "exp": now + 60,
    }
    nonce = "correct"
    access_subject = "subject"
    captured = []

    def sign(claims):
        return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "oidc-key"})

    def handler(request):
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": settings.issuer,
                    "authorization_endpoint": "https://login.example/authorize",
                    "token_endpoint": "https://login.example/token",
                    "jwks_uri": "https://login.example/keys",
                },
            )
        if request.url.path == "/token":
            captured.append(request)
            return httpx.Response(
                200,
                json={
                    "id_token": sign({**base, "aud": settings.client_id, "nonce": nonce}),
                    "access_token": sign({**base, "aud": settings.audience, "sub": access_subject}),
                },
            )
        return httpx.Response(200, json={"keys": [public]})

    flow = {"state": "state", "nonce": "correct", "verifier": "verifier"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        oidc = OIDC(settings, client)
        url = await oidc.authorization_url(flow)
        assert "code_challenge_method=S256" in url and "state=state" in url
        token, claims = await oidc.exchange("code", flow)
        assert claims["sub"] == "subject"
        assert b"code_verifier=verifier" in captured[-1].content
        nonce = "incorrect"
        with pytest.raises(ValueError, match="nonce"):
            await oidc.exchange("code", flow)
        nonce = "correct"
        access_subject = "attacker"
        with pytest.raises(ValueError, match="subject"):
            await oidc.exchange("code", flow)


async def test_cosmos_payload_encryption_rotation_expiry_and_etag_consumption():
    import asyncio
    from copy import deepcopy

    from azure.cosmos.exceptions import (
        CosmosAccessConditionFailedError,
        CosmosResourceNotFoundError,
    )

    class Container:
        def __init__(self):
            self.items = {}

        async def upsert_item(self, item):
            self.items[item["id"]] = {**item, "_etag": "etag"}

        async def read_item(self, key, partition_key):
            assert key == partition_key
            if key not in self.items:
                raise CosmosResourceNotFoundError(status_code=404)
            result = deepcopy(self.items[key])
            await asyncio.sleep(0)
            return result

        async def delete_item(self, key, partition_key, etag=None, match_condition=None):
            if key not in self.items:
                raise CosmosResourceNotFoundError(status_code=404)
            if etag and etag != self.items[key]["_etag"]:
                raise CosmosAccessConditionFailedError(status_code=412)
            del self.items[key]

        async def replace_item(self, key, item, etag=None, match_condition=None):
            if key not in self.items:
                raise CosmosResourceNotFoundError(status_code=404)
            if etag and etag != self.items[key]["_etag"]:
                raise CosmosAccessConditionFailedError(status_code=412)
            self.items[key] = {**item, "_etag": "etag-new"}

    container = Container()
    old_key, new_key = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    old = CosmosAuthStorage(container, old_key)
    await old.put("credential", {"access_token": "must-not-be-plaintext"}, int(time.time()) + 60)
    assert "must-not-be-plaintext" not in json.dumps(container.items)
    with pytest.raises(ValueError, match="binding"):
        old._record({**container.items["credential"], "id": "different-principal"})
    rotated = CosmosAuthStorage(container, new_key + "," + old_key)
    assert (await rotated.get("credential"))["access_token"] == "must-not-be-plaintext"
    assert await rotated.touch("credential", int(time.time()) + 120)
    assert (
        CosmosAuthStorage(container, new_key)._record(container.items["credential"])["access_token"]
        == "must-not-be-plaintext"
    )
    results = await asyncio.gather(rotated.consume("credential"), rotated.consume("credential"))
    assert sum(result is not None for result in results) == 1
    await rotated.put("expired", {"access_token": "secret"}, int(time.time()) - 1)
    assert await rotated.get("expired") is None
    assert not await rotated.touch("expired", int(time.time()) + 120)
    assert await rotated.consume("expired") is None
