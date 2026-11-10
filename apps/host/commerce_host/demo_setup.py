"""One-shot private initialization of an explicitly isolated demo's gateway keys."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from azure.core import MatchConditions
from azure.cosmos import CosmosClient
from azure.cosmos.exceptions import CosmosResourceExistsError
from azure.identity import ManagedIdentityCredential
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


class SetupError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def signing_key() -> str:
    return (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )


def validate_value(kind: str, value: str) -> None:
    if kind == "signing-key":
        key = serialization.load_pem_private_key(value.encode(), password=None)
        if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
            raise SetupError("Demo signing key must be RSA with at least 2048 bits")
    elif kind == "encryption-keys":
        Fernet(value.encode())
    elif kind != "oidc-client-secret" or not value:
        raise SetupError("Invalid demo secret kind or value")


class Vault:
    def __init__(self, uri: str, credential) -> None:
        parsed = urlsplit(uri)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or not parsed.hostname.endswith(".vault.azure.net")
            or parsed.path not in {"", "/"}
            or parsed.port is not None
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise SetupError("Demo setup requires an explicit public-Azure Key Vault HTTPS origin")
        self.uri = uri.rstrip("/")
        self.credential = credential
        self.transport = build_opener(NoRedirect())

    def request(self, method: str, name: str, body: dict | None = None) -> dict | None:
        token = self.credential.get_token("https://vault.azure.net/.default").token
        request = Request(
            self.uri + "/secrets/" + name + "?api-version=7.4",
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
            method=method,
        )
        try:
            with self.transport.open(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as exc:
            if method == "GET" and exc.code == 404:
                return None
            raise SetupError(f"Key Vault {method} failed with HTTP {exc.code}") from None


def initialize(container, vault, prefix: str, oidc_secret: str) -> list[str]:
    if not re.fullmatch(r"[a-z][a-z0-9-]{2,19}", prefix) or not oidc_secret.strip():
        raise SetupError("A bounded demo namespace and OIDC secret are required")
    lock = {
        "id": "gateway-secret-setup",
        "pk": "demo-setup:" + prefix,
        "kind": "demo_setup",
        "owner": str(uuid.uuid4()),
        "state": "running",
        "ttl": -1,
    }
    try:
        lock = container.create_item(lock)
    except CosmosResourceExistsError:
        raise SetupError(
            "Demo setup is already recorded; reconcile its execution before an explicit retry"
        ) from None
    definitions: list[tuple[str, str, Callable[[], str]]] = [
        ("oidc-client-secret", "oidc-client-secret", lambda: oidc_secret)
    ]
    for role in ("shopping", "merchant"):
        definitions.extend(
            [
                (role + "-signing-key", "signing-key", signing_key),
                (
                    role + "-encryption-keys",
                    "encryption-keys",
                    lambda: Fernet.generate_key().decode(),
                ),
            ]
        )
    names = []
    for suffix, kind, generate in definitions:
        name = prefix + "-" + suffix
        existing = vault.request("GET", name)
        if existing is not None:
            if existing.get("tags", {}).get("commerce-demo") != prefix:
                raise SetupError("Refusing to overwrite a secret outside this demo namespace")
            value = existing["value"]
            validate_value(kind, value)
            if kind == "oidc-client-secret" and value != oidc_secret:
                raise SetupError(
                    "Existing OIDC secret differs; use the explicit rotation procedure"
                )
        else:
            value = generate()
            validate_value(kind, value)
            stored = vault.request("PUT", name, {"value": value, "tags": {"commerce-demo": prefix}})
            if stored is None or stored.get("value") != value:
                raise SetupError("Key Vault did not confirm the requested secret")
        names.append(name)
    container.replace_item(
        lock["id"],
        {**lock, "state": "complete", "secretNames": names},
        etag=lock["_etag"],
        match_condition=MatchConditions.IfNotModified,
    )
    return names


def main() -> None:
    from .config import required

    if required("COMMERCE_MODE") != "fixture":
        raise SetupError("Secret initialization is only available for explicit fixture deployments")
    credential = ManagedIdentityCredential(client_id=required("AZURE_CLIENT_ID"))
    try:
        with CosmosClient(
            required("COMMERCE_COSMOS_ENDPOINT"), credential=credential, logging_enable=False
        ) as client:
            container = client.get_database_client(
                required("COMMERCE_COSMOS_DATABASE")
            ).get_container_client(required("COMMERCE_COSMOS_CONTAINER"))
            names = initialize(
                container,
                Vault(required("COMMERCE_DEMO_VAULT_URI"), credential),
                required("COMMERCE_DEMO_SECRET_PREFIX"),
                required("COMMERCE_OIDC_CLIENT_SECRET"),
            )
            print(json.dumps({"status": "complete", "secretNames": names}))
    finally:
        credential.close()


if __name__ == "__main__":
    main()
