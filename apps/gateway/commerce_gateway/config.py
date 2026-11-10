import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    role: str
    origin: str
    api_upstream: str
    web_upstream: str
    issuer: str
    client_id: str
    client_secret: str = field(repr=False)
    audience: str
    tenant: str
    gateway_issuer: str
    signing_key: str = field(repr=False)
    signing_kid: str
    encryption_keys: str = field(repr=False)
    cosmos_endpoint: str
    cosmos_database: str
    cosmos_container: str
    private_boundary: bool = False
    scope: str = "openid profile"
    session_seconds: int = 3600
    guest_seconds: int = 2592000
    max_body_bytes: int = 1048576
    mode: str = "production"

    def __post_init__(self):
        if self.role not in {"shopping", "merchant"}:
            raise ValueError("COMMERCE_ROLE must be shopping or merchant")
        if self.mode not in {"production", "fixture"}:
            raise ValueError("COMMERCE_MODE must be production or fixture")
        if self.role == "merchant" and not self.private_boundary:
            raise ValueError("Merchant gateway requires a private network boundary")
        for name in ("origin", "issuer", "gateway_issuer", "cosmos_endpoint"):
            parsed = urlsplit(getattr(self, name))
            if (
                parsed.scheme != "https"
                or not parsed.netloc
                or parsed.query
                or parsed.fragment
                or parsed.username
                or parsed.password
            ):
                raise ValueError(f"{name} must be a configured HTTPS URL")
        if urlsplit(self.origin).path not in {"", "/"}:
            raise ValueError("origin cannot contain a path")
        for name in ("api_upstream", "web_upstream"):
            parsed = urlsplit(getattr(self, name))
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.path not in {"", "/"}
            ):
                raise ValueError(f"{name} must be an explicit upstream origin")
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError(f"{name} cannot contain credentials, query, or fragment")
        for name in (
            "client_id",
            "client_secret",
            "audience",
            "tenant",
            "signing_key",
            "signing_kid",
            "encryption_keys",
            "cosmos_database",
            "cosmos_container",
        ):
            if not getattr(self, name):
                raise ValueError(f"Missing gateway setting: {name}")
        if not 300 <= self.session_seconds <= 86400:
            raise ValueError("Session lifetime must be between 300 and 86400 seconds")
        if not 3600 <= self.guest_seconds <= 2592000:
            raise ValueError("Guest inactivity lifetime must not exceed 30 days")

    @classmethod
    def from_env(cls):
        def required(name):
            value = os.getenv("COMMERCE_" + name)
            if not value:
                raise ValueError(f"Missing required setting COMMERCE_{name}")
            return value

        return cls(
            role=required("ROLE"),
            origin=required("PUBLIC_ORIGIN").rstrip("/"),
            api_upstream=required("API_UPSTREAM").rstrip("/"),
            web_upstream=required("WEB_UPSTREAM").rstrip("/"),
            issuer=required("OIDC_ISSUER"),
            client_id=required("OIDC_CLIENT_ID"),
            client_secret=required("OIDC_CLIENT_SECRET"),
            audience=required("OIDC_API_AUDIENCE"),
            tenant=required("TENANT_ID"),
            gateway_issuer=required("GATEWAY_ISSUER"),
            signing_key=required("GATEWAY_SIGNING_KEY").replace("\\n", "\n"),
            signing_kid=required("GATEWAY_SIGNING_KID"),
            encryption_keys=required("AUTH_ENCRYPTION_KEYS"),
            cosmos_endpoint=required("COSMOS_ENDPOINT"),
            cosmos_database=required("COSMOS_DATABASE"),
            cosmos_container=required("AUTH_CONTAINER"),
            private_boundary=os.getenv("COMMERCE_PRIVATE_BOUNDARY") == "true",
            scope=os.getenv("COMMERCE_OIDC_SCOPE", "openid profile"),
            session_seconds=int(os.getenv("COMMERCE_AUTH_SESSION_SECONDS", "3600")),
            guest_seconds=int(os.getenv("COMMERCE_GUEST_SECONDS", "2592000")),
            mode=os.getenv("COMMERCE_MODE", "production"),
        )
