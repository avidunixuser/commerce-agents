from __future__ import annotations

import asyncio
import contextlib
import logging
import sys

from .api import create_app
from .config import Settings
from .identity import TokenVerifier
from .runtime import RuntimeFactory
from .storage import CosmosDocuments
from .worker import Worker


async def serve(mode: str) -> None:
    import uvicorn
    from azure.cosmos import CosmosClient
    from azure.identity import DefaultAzureCredential

    from .telemetry import configure

    settings = Settings.from_env()
    if settings.mode == "fixture":
        import os
        from pathlib import Path

        root = Path(os.environ.get("COMMERCE_APP_ROOT") or Path(__file__).resolve().parents[3])
        sys.path.insert(0, str(root / "examples"))
    configure()
    credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
    client = CosmosClient(settings.cosmos_endpoint, credential=credential, logging_enable=False)
    documents = CosmosDocuments(
        client.get_database_client(settings.cosmos_database).get_container_client(
            settings.cosmos_container
        )
    )
    verifier = TokenVerifier(
        settings.issuer,
        settings.audience,
        settings.jwks_url,
        settings.tenants,
        settings.role,
        guest_issuer=settings.guest_issuer,
        guest_jwks_url=settings.guest_jwks_url,
    )
    factory = RuntimeFactory(settings)
    worker = Worker(settings, documents, factory) if mode == "worker" else None
    task = asyncio.create_task(worker.run()) if worker else None
    app = create_app(
        settings,
        documents,
        factory,
        verifier,
        ready=lambda: task is None or (not task.done() and worker.available),
    )
    try:
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="0.0.0.0",
                port=8080,
                access_log=False,
                timeout_graceful_shutdown=30,
            )
        )
        await server.serve()
    finally:
        if worker and task:
            worker.stopping.set()
            try:
                await asyncio.wait_for(task, timeout=30)
            except TimeoutError:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        client.close()
        credential.close()
        await factory.aclose()


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in {"api", "worker"}:
        raise SystemExit("Usage: python -m commerce_host api|worker")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(serve(sys.argv[1]))


if __name__ == "__main__":
    main()
