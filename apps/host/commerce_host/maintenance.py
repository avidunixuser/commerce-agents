"""Explicit, authenticated operator maintenance. Never replay ambiguous commerce effects."""

from __future__ import annotations

import argparse
import sys

from .config import Settings, required
from .identity import TokenVerifier
from .jobs import JobQueue
from .storage import CosmosDocuments


def main() -> None:
    from azure.cosmos import CosmosClient
    from azure.identity import DefaultAzureCredential

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["redrive"])
    parser.add_argument("--partition", required=True)
    parser.add_argument("--job", required=True)
    parser.add_argument("--etag", required=True)
    parser.add_argument("--operator-token-stdin", action="store_true", required=True)
    args = parser.parse_args()
    settings = Settings.from_env()
    verifier = TokenVerifier(
        required("COMMERCE_OPERATOR_ISSUER"),
        required("COMMERCE_OPERATOR_AUDIENCE"),
        required("COMMERCE_OPERATOR_JWKS_URL"),
        settings.tenants,
        "merchant",
    )
    token = sys.stdin.readline(16001).strip()
    if not token or len(token) > 16000:
        raise SystemExit("A bounded workforce operator JWT is required on standard input")
    operator, _ = verifier.verify(token)
    operator.require("operator")
    credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
    client = CosmosClient(settings.cosmos_endpoint, credential=credential, logging_enable=False)
    try:
        documents = CosmosDocuments(
            client.get_database_client(settings.cosmos_database).get_container_client(
                settings.cosmos_container,
            )
        )
        JobQueue(documents, "operator-maintenance").redrive_safe(
            args.partition,
            args.job,
            args.etag,
            operator,
        )
        print("Safe job redrive committed")
    finally:
        client.close()
        credential.close()


if __name__ == "__main__":
    main()
