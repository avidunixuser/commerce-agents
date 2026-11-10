import os
from contextlib import asynccontextmanager

import uvicorn
from azure.cosmos.aio import CosmosClient
from azure.identity.aio import ManagedIdentityCredential

from .app import create_app
from .config import Settings
from .storage import CosmosAuthStorage


def main():
    settings = Settings.from_env()
    credential = ManagedIdentityCredential(client_id=os.environ.get("AZURE_CLIENT_ID"))
    cosmos = CosmosClient(settings.cosmos_endpoint, credential=credential)
    container = cosmos.get_database_client(settings.cosmos_database).get_container_client(
        settings.cosmos_container
    )
    app = create_app(settings, CosmosAuthStorage(container, settings.encryption_keys))
    gateway_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app):
        try:
            properties = await container.read()
            if (
                properties.get("partitionKey", {}).get("paths") != ["/pk"]
                or "defaultTtl" not in properties
            ):
                raise ValueError("Gateway auth container requires partition /pk and enabled TTL")
            async with gateway_lifespan(app):
                yield
        finally:
            await cosmos.close()
            await credential.close()

    app.router.lifespan_context = lifespan
    # Access logging would include OIDC callback authorization codes in query strings.
    uvicorn.run(app, host="0.0.0.0", port=8080, access_log=False, proxy_headers=False)


if __name__ == "__main__":
    main()
