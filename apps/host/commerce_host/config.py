from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} must be configured")
    return value


@dataclass(frozen=True)
class Settings:
    role: str
    vertical: str
    mode: str
    cosmos_endpoint: str
    cosmos_database: str
    cosmos_container: str
    issuer: str
    audience: str
    jwks_url: str
    tenants: frozenset[str]
    provider_id: str
    backend_factory: str
    transcript_days: int = 30
    memory_days: int = 90
    guest_days: int = 30
    segment_seconds: int = 210
    guest_issuer: str | None = None
    guest_jwks_url: str | None = None
    portable_tools: bool = True

    def __post_init__(self) -> None:
        if self.role not in {"shopping", "merchant"}:
            raise ValueError("COMMERCE_ROLE must be shopping or merchant")
        if self.vertical not in {"retail", "travel", "telecom", "entertainment"}:
            raise ValueError("COMMERCE_VERTICAL must select a supported vertical")
        if self.mode not in {"production", "fixture"}:
            raise ValueError("COMMERCE_MODE must be production or fixture")
        for value in (self.cosmos_endpoint, self.issuer, self.jwks_url):
            parsed = urlparse(value)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("Production service endpoints must be credential-free HTTPS URLs")
        if bool(self.guest_issuer) != bool(self.guest_jwks_url):
            raise ValueError("Guest issuer and JWKS must be configured together")
        for value in (self.guest_issuer, self.guest_jwks_url):
            if value is not None:
                parsed = urlparse(value)
                if (
                    parsed.scheme != "https"
                    or not parsed.hostname
                    or parsed.username
                    or parsed.query
                    or parsed.fragment
                ):
                    raise ValueError("Guest authority requires credential-free HTTPS URLs")
        if not self.tenants or not self.provider_id or not self.backend_factory:
            raise ValueError("Tenant, provider deployment and backend factory are required")
        if any(not tenant or tenant.strip() != tenant for tenant in self.tenants):
            raise ValueError("Allowed tenants must be nonempty and whitespace-free")
        if self.mode == "production" and self.backend_factory.startswith("commerce_host.fixtures:"):
            raise ValueError("Fixture backends cannot be used in production")
        if not 1 <= self.segment_seconds <= 210:
            raise ValueError("SSE segment duration must be 1 to 210 seconds")
        if any(
            not 1 <= days <= 365
            for days in (self.transcript_days, self.memory_days, self.guest_days)
        ):
            raise ValueError("Retention settings must be 1 to 365 days")

    @classmethod
    def from_env(cls) -> Settings:
        portable = os.environ.get("COMMERCE_PORTABLE_TOOLS", "true").strip().lower()
        if portable not in {"true", "false"}:
            raise ValueError("COMMERCE_PORTABLE_TOOLS must be true or false")
        return cls(
            role=required("COMMERCE_ROLE"),
            vertical=required("COMMERCE_VERTICAL"),
            mode=required("COMMERCE_MODE"),
            cosmos_endpoint=required("COMMERCE_COSMOS_ENDPOINT"),
            cosmos_database=required("COMMERCE_COSMOS_DATABASE"),
            cosmos_container=required("COMMERCE_COSMOS_CONTAINER"),
            issuer=required("COMMERCE_AUTH_ISSUER"),
            audience=required("COMMERCE_AUTH_AUDIENCE"),
            jwks_url=required("COMMERCE_AUTH_JWKS_URL"),
            tenants=frozenset(
                tenant.strip() for tenant in required("COMMERCE_ALLOWED_TENANTS").split(",")
            ),
            provider_id=required("COMMERCE_PROVIDER_ID"),
            backend_factory=required("COMMERCE_BACKEND_FACTORY"),
            transcript_days=int(os.environ.get("COMMERCE_TRANSCRIPT_DAYS", "30")),
            memory_days=int(os.environ.get("COMMERCE_MEMORY_DAYS", "90")),
            guest_days=int(os.environ.get("COMMERCE_GUEST_DAYS", "30")),
            segment_seconds=int(os.environ.get("COMMERCE_STREAM_SECONDS", "210")),
            guest_issuer=os.environ.get("COMMERCE_GUEST_ISSUER") or None,
            guest_jwks_url=os.environ.get("COMMERCE_GUEST_JWKS_URL") or None,
            portable_tools=portable == "true",
        )
