"""Network-free gateway -> real host app, JWT verifier, stores, and migration worker."""

import inspect
import time
import uuid
from dataclasses import dataclass
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
from commerce_gateway.app import GUEST_COOKIE, SESSION_COOKIE, create_app, record_key
from commerce_gateway.config import Settings as GatewaySettings
from commerce_gateway.storage import MemoryAuthStorage
from commerce_host.api import create_app as create_host_app
from commerce_host.config import Settings as HostSettings
from commerce_host.identity import Principal, TokenVerifier
from commerce_host.jobs import JobQueue
from commerce_host.memory import CosmosMemoryStore
from commerce_host.runtime import RuntimeFactory
from commerce_host.storage import FakeDocuments, Operation, partition
from commerce_host.worker import Worker
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from commerce_common.providers import provider_configuration_id
from commerce_common.types import MemoryCategory, MemoryFact


class UnusedModelProvider:
    """Session creation and memory migration must not invoke a model."""

    def __init__(self, identity):
        self.identity = identity

    def validate_config(self, config, **options):
        pass

    async def aclose(self):
        pass

    def stream(self, request):
        raise AssertionError("This identity/memory integration test must not call a model")


class LocalTransport(httpx.AsyncBaseTransport):
    def __init__(self, host_app, oidc):
        self.host = httpx.ASGITransport(app=host_app)
        self.oidc = httpx.MockTransport(oidc)
        self.host_requests = []

    async def handle_async_request(self, request):
        if request.url.host == "api":
            self.host_requests.append(request)
            return await self.host.handle_async_request(request)
        if request.url.host == "identity.example":
            return await self.oidc.handle_async_request(request)
        raise AssertionError(f"Unexpected external network request to {request.url.host}")

    async def aclose(self):
        await self.host.aclose()
        await self.oidc.aclose()


@dataclass
class Stack:
    browser: httpx.AsyncClient
    gateway: MemoryAuthStorage
    documents: FakeDocuments
    settings: HostSettings
    factory: RuntimeFactory
    transport: LocalTransport
    origin: str
    upstream: httpx.AsyncClient

    async def login(self):
        response = await self.browser.get("/auth/login")
        assert response.status_code == 303, response.text
        query = parse_qs(httpx.URL(response.headers["location"]).query.decode())
        response = await self.browser.get(
            "/auth/callback",
            params={
                "state": query["state"][0],
                "code": "code:" + query["nonce"][0],
            },
        )
        assert response.status_code == 303, response.text

    async def state(self):
        response = await self.browser.get("/api/auth/session")
        assert response.status_code == 200, response.text
        return response.json()

    async def headers(self):
        state = await self.state()
        return {"Origin": self.origin, "X-CSRF-Token": state["csrf_token"]}

    async def bootstrap(self, consent=True):
        response = await self.browser.post(
            "/api/auth/guest/bootstrap",
            headers={"Origin": self.origin},
            json={"memory_consent": consent},
        )
        assert response.status_code == 201, response.text
        return await self.state()

    async def start(self):
        prefix = "/api" if self.settings.role == "shopping" else "/api/merchant"
        response = await self.browser.post(prefix + "/session", headers=await self.headers())
        assert response.status_code == 200, response.text
        return response.json()


@pytest.fixture
async def stack_factory(monkeypatch):
    stacks = []
    monkeypatch.setenv("COMMERCE_PROVIDER", "azure_openai")
    monkeypatch.setenv("COMMERCE_MODEL", "interop-model")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://models.example")
    monkeypatch.setattr(
        "commerce_common.providers.create_provider_from_env",
        lambda **options: UnusedModelProvider(provider_configuration_id()),
    )

    async def build(role="shopping", audience="commerce-api"):
        customer_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        guest_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        origin = "https://shop.example" if role == "shopping" else "https://portal.example"
        issuer = "https://identity.example/tenant"
        customer_jwks = "https://identity.example/keys"
        guest_jwks = origin + "/.well-known/jwks.json"
        public = jwt.algorithms.RSAAlgorithm.to_jwk(customer_key.public_key(), as_dict=True)
        public.update(kid="customer-key", alg="RS256", use="sig")
        key_sets = {customer_jwks: {"keys": [public]}}

        def fetch_keys(client):
            # Only the OIDC/JWKS HTTP boundary is replaced; host JWT verification is real.
            if client.uri not in key_sets:
                raise jwt.InvalidTokenError("Unknown trusted JWKS endpoint")
            return key_sets[client.uri]

        monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", fetch_keys)

        def oidc(request):
            if request.url.path.endswith("openid-configuration"):
                return httpx.Response(
                    200,
                    json={
                        "issuer": issuer,
                        "authorization_endpoint": "https://identity.example/authorize",
                        "token_endpoint": "https://identity.example/token",
                        "jwks_uri": customer_jwks,
                    },
                )
            if str(request.url) == customer_jwks:
                return httpx.Response(200, json=key_sets[customer_jwks])
            if request.url.path == "/token":
                body = parse_qs(request.content.decode())
                assert body["code_verifier"][0]
                nonce = body["code"][0].removeprefix("code:")
                now = int(time.time())
                claims = {
                    "iss": issuer,
                    "sub": "customer",
                    "tid": "tenant",
                    "iat": now,
                    "exp": now + 3600,
                    "jti": str(uuid.uuid4()),
                    "roles": ["merchant", "approver"] if role == "merchant" else ["customer"],
                }

                def sign(extra):
                    return jwt.encode(
                        {**claims, **extra},
                        customer_key,
                        algorithm="RS256",
                        headers={"kid": "customer-key"},
                    )

                return httpx.Response(
                    200,
                    json={
                        "id_token": sign({"aud": "client", "nonce": nonce}),
                        "access_token": sign({"aud": audience}),
                    },
                )
            raise AssertionError(f"Unexpected OIDC request: {request.url.path}")

        settings = HostSettings(
            role=role,
            vertical="retail",
            mode="fixture",
            cosmos_endpoint="https://cosmos.example",
            cosmos_database="commerce",
            cosmos_container=role,
            issuer=issuer,
            audience=audience,
            jwks_url=customer_jwks,
            tenants=frozenset({"tenant"}),
            provider_id=provider_configuration_id(),
            backend_factory="commerce_host.fixtures:build",
            segment_seconds=1,
        )
        options = {}
        if "guest_issuer" in inspect.signature(TokenVerifier).parameters:
            options.update(guest_issuer=origin, guest_jwks_url=guest_jwks)
        verifier = TokenVerifier(
            issuer, audience, customer_jwks, frozenset({"tenant"}), role, **options
        )
        documents = FakeDocuments()
        factory = RuntimeFactory(settings)
        factory.providers.update(
            {
                purpose: UnusedModelProvider(settings.provider_id)
                for purpose in ("chat", "extraction", "analysis")
            }
        )
        host_app = create_host_app(settings, documents, factory, verifier)
        transport = LocalTransport(host_app, oidc)
        upstream = httpx.AsyncClient(transport=transport, follow_redirects=False)
        gateway_settings = GatewaySettings(
            role=role,
            origin=origin,
            api_upstream="http://api:8080",
            web_upstream="http://web:3000",
            issuer=issuer,
            client_id="client",
            client_secret="oidc-fixture-secret",
            audience=audience,
            tenant="tenant",
            gateway_issuer=origin,
            signing_key=guest_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ).decode(),
            signing_kid="guest-key",
            encryption_keys=Fernet.generate_key().decode(),
            cosmos_endpoint="https://cosmos.example",
            cosmos_database="commerce",
            cosmos_container="auth",
            private_boundary=role == "merchant",
        )
        gateway = MemoryAuthStorage()
        app = create_app(gateway_settings, gateway, upstream)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=origin
        ) as probe:
            response = await probe.get("/.well-known/jwks.json")
        assert response.status_code == 200
        key_sets[guest_jwks] = response.json()
        assert all("d" not in key for key in key_sets[guest_jwks]["keys"])
        browser = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin)
        stack = Stack(browser, gateway, documents, settings, factory, transport, origin, upstream)
        stacks.append(stack)
        return stack

    yield build
    for stack in stacks:
        await stack.browser.aclose()
        await stack.upstream.aclose()
        await stack.factory.aclose()


@pytest.mark.parametrize("role", ["shopping", "merchant"])
async def test_real_host_customer_session_and_logout(stack_factory, role):
    stack = await stack_factory(role)
    async with stack.browser:
        await stack.login()
        state = await stack.state()
        assert state["kind"] == ("customer" if role == "shopping" else "merchant")
        session = await stack.start()
        assert session["session_id"] and session["guest"] is False
        response = await stack.browser.post("/auth/logout", headers=await stack.headers())
        assert response.status_code == 200, response.text
        assert not stack.browser.cookies.get(SESSION_COOKIE)
        assert (await stack.state())["authenticated"] is False
    await stack.transport.aclose()


async def test_real_host_guest_session_idle_renewal_expiry_and_logout(stack_factory):
    stack = await stack_factory()
    async with stack.browser:
        await stack.bootstrap(consent=True)
        credential = stack.browser.cookies.get(GUEST_COOKIE)
        auth_key = record_key("guest", credential)
        guest, _ = stack.gateway.records[auth_key]
        pk = partition("tenant", "guest", guest["principal_id"])
        registry = stack.documents.read(pk, "identity")
        assert registry and registry["epoch"] == 0 and not registry["revoked"]
        assert registry["memory_consent"] is True
        session = await stack.start()
        assert session["guest"] is True
        registry = stack.documents.read(pk, "identity")
        stack.documents.transact(
            pk,
            [
                Operation(
                    "replace",
                    "identity",
                    dict(registry, expires=time.time() + 30),
                    registry["_etag"],
                )
            ],
        )
        stack.gateway.records[auth_key] = (guest, int(time.time()) + 30)
        await stack.state()
        assert stack.documents.read(pk, "identity")["expires"] > time.time() + 2591900
        assert stack.gateway.records[auth_key][1] > time.time() + 2591900
        response = await stack.browser.post("/auth/logout", headers=await stack.headers())
        assert response.status_code == 200, response.text
        revoked = stack.documents.read(pk, "identity")
        assert revoked["revoked"] or revoked["epoch"] != 0
        assert await stack.gateway.get(auth_key) is None
        assert not stack.browser.cookies.get(GUEST_COOKIE)
    await stack.transport.aclose()


async def test_real_host_expired_guest_registry_cannot_be_renewed(stack_factory):
    stack = await stack_factory()
    async with stack.browser:
        await stack.bootstrap(consent=False)
        credential = stack.browser.cookies.get(GUEST_COOKIE)
        guest = await stack.gateway.get(record_key("guest", credential))
        pk = partition("tenant", "guest", guest["principal_id"])
        registry = stack.documents.read(pk, "identity")
        expired = time.time() - 1
        stack.documents.transact(
            pk,
            [
                Operation(
                    "replace",
                    "identity",
                    dict(registry, expires=expired),
                    registry["_etag"],
                )
            ],
        )
        response = await stack.browser.get("/api/auth/session")
        assert response.status_code in {401, 403}, response.text
        assert stack.documents.read(pk, "identity")["expires"] == expired
        assert "set-cookie" not in response.headers
    await stack.transport.aclose()


async def test_real_host_guest_uses_configured_api_audience(stack_factory):
    stack = await stack_factory(audience="api://registered-commerce-api")
    async with stack.browser:
        await stack.bootstrap(consent=False)
        assert (await stack.start())["guest"] is True
        token = stack.transport.host_requests[-1].headers["authorization"].removeprefix("Bearer ")
        assert (
            jwt.decode(token, options={"verify_signature": False})["aud"]
            == "api://registered-commerce-api"
        )
    await stack.transport.aclose()


async def test_real_host_customer_logout_revokes_unmigrated_guest(stack_factory):
    stack = await stack_factory()
    async with stack.browser:
        await stack.bootstrap(consent=True)
        credential = stack.browser.cookies.get(GUEST_COOKIE)
        key = record_key("guest", credential)
        guest = await stack.gateway.get(key)
        pk = partition("tenant", "guest", guest["principal_id"])
        await stack.login()
        response = await stack.browser.post("/auth/logout", headers=await stack.headers())
        assert response.status_code == 200, response.text
        registry = stack.documents.read(pk, "identity")
        assert registry["revoked"] or registry["epoch"] != 0
        assert await stack.gateway.get(key) is None
        assert not stack.browser.cookies.get(GUEST_COOKIE)
        assert not stack.browser.cookies.get(SESSION_COOKIE)
    await stack.transport.aclose()


async def test_real_host_migration_waits_for_worker_and_customer_facts_win(stack_factory):
    stack = await stack_factory()
    async with stack.browser:
        await stack.bootstrap(consent=True)
        guest_credential = stack.browser.cookies.get(GUEST_COOKIE)
        guest_auth_key = record_key("guest", guest_credential)
        guest_session = await stack.start()
        guest_auth = await stack.gateway.get(guest_auth_key)
        guest_principal = Principal(
            "tenant", guest_auth["principal_id"], "shopping", frozenset({"guest_memory"}), True, 0
        )
        await CosmosMemoryStore(stack.documents, guest_principal, retention_days=30).upsert_facts(
            guest_principal.subject,
            [
                MemoryFact(key="shoe_size", value="8", category=MemoryCategory.PREFERENCE),
                MemoryFact(
                    key="preferred_brand", value="Other", category=MemoryCategory.PREFERENCE
                ),
            ],
        )
        headers = {**await stack.headers(), "X-Session-Id": guest_session["session_id"]}
        for key, value in (("shoe_size", "9"), ("preferred_brand", "Acme")):
            response = await stack.browser.patch(
                "/api/memory", headers=headers, json={"key": key, "value": value}
            )
            assert response.status_code == 200, response.text
        await stack.login()
        customer_session = await stack.start()
        await CosmosMemoryStore(
            stack.documents, Principal("tenant", "customer", "shopping")
        ).upsert_facts(
            "customer",
            [
                MemoryFact(key="shoe_size", value="11", category=MemoryCategory.PREFERENCE),
            ],
        )
        headers = {**await stack.headers(), "X-Session-Id": customer_session["session_id"]}
        response = await stack.browser.patch(
            "/api/memory", headers=headers, json={"key": "shoe_size", "value": "10"}
        )
        assert response.status_code == 200, response.text
        response = await stack.browser.post(
            "/api/auth/migrate", headers=headers, json={"consent": False}
        )
        assert response.status_code == 400
        response = await stack.browser.post(
            "/api/auth/migrate", headers=headers, json={"consent": True}
        )
        assert response.status_code == 202, response.text
        migration_id = response.json()["migration_id"]
        path = "/api/auth/migrations/" + migration_id + "/status"
        response = await stack.browser.post(path, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["status"] != "complete"
        assert await stack.gateway.get(guest_auth_key)
        job = next(
            item
            for item in stack.documents.items.values()
            if item.get("purpose") == "migration" and item["id"] == "job:" + migration_id
        )
        worker = Worker(stack.settings, stack.documents, stack.factory)
        claimed = JobQueue(stack.documents, "interop-worker").claim(job)
        worker.queue = JobQueue(stack.documents, "interop-worker")
        await worker.execute(claimed)
        response = await stack.browser.post(path, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "complete"
        assert await stack.gateway.get(guest_auth_key) is None
        assert not stack.browser.cookies.get(GUEST_COOKIE)
        response = await stack.browser.get("/api/memory", headers=headers)
        assert response.status_code == 200, response.text
        facts = {fact["key"]: fact["value"] for fact in response.json()["facts"]}
        assert facts["shoe_size"] == "10"
        assert facts["preferred_brand"] == "Acme"
    await stack.transport.aclose()
