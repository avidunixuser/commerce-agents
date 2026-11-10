from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from commerce_gateway import __main__ as entrypoint
from fastapi import FastAPI


async def test_real_async_clients_construct_with_managed_identity(monkeypatch):
    monkeypatch.setenv("IDENTITY_ENDPOINT", "http://127.0.0.1:9/identity")
    monkeypatch.setenv("IDENTITY_HEADER", "offline-test-only")
    credential = entrypoint.ManagedIdentityCredential(
        client_id="11111111-1111-4111-8111-111111111111"
    )
    try:
        cosmos = entrypoint.CosmosClient("https://cosmos.invalid", credential=credential)
        try:
            assert cosmos.get_database_client("commerce").get_container_client("identity")
        finally:
            await cosmos.close()
    finally:
        await credential.close()


@pytest.mark.parametrize("client_id", ["11111111-1111-4111-8111-111111111111", None])
async def test_gateway_selects_runtime_identity_and_closes_clients(monkeypatch, client_id):
    if client_id is None:
        monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
    else:
        monkeypatch.setenv("AZURE_CLIENT_ID", client_id)
    settings = SimpleNamespace(
        cosmos_endpoint="https://cosmos.example",
        cosmos_database="commerce",
        cosmos_container="identity",
        encryption_keys="configured-encryption-keys",
    )
    credential = SimpleNamespace(close=AsyncMock())
    credential_factory = MagicMock(return_value=credential)
    container = MagicMock()
    container.read = AsyncMock(return_value={"partitionKey": {"paths": ["/pk"]}, "defaultTtl": -1})
    cosmos = MagicMock()
    cosmos.close = AsyncMock()
    cosmos.get_database_client.return_value.get_container_client.return_value = container
    cosmos_factory = MagicMock(return_value=cosmos)
    app = FastAPI()
    storage_factory = MagicMock()
    create_app = MagicMock(return_value=app)
    run = MagicMock()
    monkeypatch.setattr(entrypoint.Settings, "from_env", lambda: settings)
    monkeypatch.setattr(entrypoint, "ManagedIdentityCredential", credential_factory)
    monkeypatch.setattr(entrypoint, "CosmosClient", cosmos_factory)
    monkeypatch.setattr(entrypoint, "CosmosAuthStorage", storage_factory)
    monkeypatch.setattr(entrypoint, "create_app", create_app)
    monkeypatch.setattr(entrypoint.uvicorn, "run", run)

    entrypoint.main()

    credential_factory.assert_called_once_with(client_id=client_id)
    cosmos_factory.assert_called_once_with(settings.cosmos_endpoint, credential=credential)
    cosmos.get_database_client.assert_called_once_with("commerce")
    cosmos.get_database_client.return_value.get_container_client.assert_called_once_with("identity")
    storage_factory.assert_called_once_with(container, settings.encryption_keys)
    create_app.assert_called_once_with(settings, storage_factory.return_value)
    run.assert_called_once_with(
        app, host="0.0.0.0", port=8080, access_log=False, proxy_headers=False
    )
    async with app.router.lifespan_context(app):
        container.read.assert_awaited_once()
        credential.close.assert_not_awaited()
    cosmos.close.assert_awaited_once()
    credential.close.assert_awaited_once()
