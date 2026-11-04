from datetime import datetime
from pathlib import Path

import pytest
from commerce_host.config import Settings
from commerce_host.fixtures import build
from commerce_host.identity import Principal

from merchant_agent import MerchantSessionContext
from shopping_agent import ShoppingSessionContext


@pytest.mark.parametrize("vertical", ["retail", "travel", "telecom", "entertainment"])
@pytest.mark.parametrize("role", ["shopping", "merchant"])
async def test_explicit_lab_backend_covers_every_vertical(vertical, role, monkeypatch):
    root = Path(__file__).resolve().parents[3]
    monkeypatch.syspath_prepend(str(root / "examples"))
    settings = Settings(
        role=role,
        vertical=vertical,
        mode="fixture",
        cosmos_endpoint="https://cosmos.example",
        cosmos_database="commerce",
        cosmos_container="shared",
        issuer="https://issuer.example",
        audience="commerce",
        jwks_url="https://issuer.example/keys",
        tenants=frozenset({"tenant"}),
        provider_id="test",
        backend_factory="commerce_host.fixtures:build",
    )
    principal = Principal("tenant", "user", role)
    bundle = build(settings, principal)
    if role == "shopping":
        context = ShoppingSessionContext(session_id="test", user_id="user", now=datetime.now())
        assert (await bundle.routes["GET /products"](context, None, {"limit": 1}))["products"]
        if vertical == "entertainment":
            assert "GET /tickets" in bundle.routes
            assert "POST /demo/return" not in bundle.routes
    else:
        context = MerchantSessionContext(
            session_id="test",
            merchant_id="user",
            operator="user",
            now=datetime.now(),
        )
        assert await bundle.backend.get_business_snapshot(context)
        assert "GET /overview" in bundle.routes
    assert "durable_backend" not in bundle.capabilities
