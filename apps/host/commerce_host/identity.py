from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .storage import AccessDenied, ConflictError, Documents, Operation, partition


@dataclass(frozen=True)
class Principal:
    tenant: str
    subject: str
    role: str
    permissions: frozenset[str] = frozenset()
    guest: bool = False
    guest_epoch: int | None = None

    @property
    def scope(self) -> str:
        return partition(self.tenant, self.role, self.subject)

    def require(self, permission: str) -> None:
        if permission not in self.permissions:
            raise AccessDenied("Permission required")

    def payload(self) -> dict[str, Any]:
        return {
            "tenant": self.tenant,
            "subject": self.subject,
            "role": self.role,
            "permissions": sorted(self.permissions),
            "guest": self.guest,
            "guest_epoch": self.guest_epoch,
        }


class TokenVerifier:
    def __init__(
        self,
        issuer: str,
        audience: str,
        jwks_url: str,
        tenants: frozenset[str],
        role: str,
        *,
        guest_issuer: str | None = None,
        guest_jwks_url: str | None = None,
    ) -> None:
        import jwt

        if not all(value.startswith("https://") for value in (issuer, jwks_url)):
            raise ValueError("Production issuer and JWKS URL must use HTTPS")
        if not audience or not tenants or role not in {"shopping", "merchant"}:
            raise ValueError("Audience, allowed tenants and role are required")
        self.issuer, self.audience, self.tenants, self.role = issuer, audience, tenants, role
        self.keys = jwt.PyJWKClient(jwks_url, cache_keys=True, lifespan=300, timeout=5)
        if bool(guest_issuer) != bool(guest_jwks_url):
            raise ValueError("Guest issuer and JWKS must be configured together")
        if guest_issuer and (
            not guest_issuer.startswith("https://")
            or guest_jwks_url is None
            or not guest_jwks_url.startswith("https://")
            or guest_issuer == issuer
        ):
            raise ValueError("Guest authority must use a distinct HTTPS issuer and JWKS")
        self.guest_issuer = guest_issuer
        self.guest_keys = (
            jwt.PyJWKClient(guest_jwks_url, cache_keys=True, lifespan=300, timeout=5)
            if guest_jwks_url
            else None
        )

    def verify(self, token: str) -> tuple[Principal, dict[str, Any]]:
        import jwt

        unverified = jwt.decode(token, options={"verify_signature": False})
        issuer = unverified.get("iss")
        guest_authority = self.guest_issuer is not None and issuer == self.guest_issuer
        if issuer != self.issuer and not guest_authority:
            raise jwt.InvalidIssuerError("Untrusted issuer")
        keys = self.guest_keys if guest_authority else self.keys
        if keys is None:
            raise jwt.InvalidIssuerError("Guest authority is not configured")
        claims = jwt.decode(
            token,
            keys.get_signing_key_from_jwt(token).key,
            algorithms=["RS256"],
            audience=self.audience,
            issuer=issuer,
            options={"require": ["exp", "iat", "sub", "tid", "iss", "aud"]},
            leeway=30,
        )
        tenant, subject = claims["tid"], claims["sub"]
        if (
            not isinstance(tenant, str)
            or tenant not in self.tenants
            or not isinstance(subject, str)
            or not subject
            or len(subject) > 256
        ):
            raise AccessDenied("Tenant or subject is not authorized")
        roles = claims.get("roles", [])
        if not isinstance(roles, list) or not all(isinstance(item, str) for item in roles):
            raise AccessDenied("Invalid role claims")
        guest = claims.get("guest", False)
        if not isinstance(guest, bool):
            raise AccessDenied("Invalid guest claim")
        if guest_authority and not guest:
            raise AccessDenied("Guest authority cannot issue customer or merchant identities")
        if guest and not guest_authority:
            raise AccessDenied("Guest identity requires the configured guest authority")
        if guest:
            roles = ["guest_memory"] if claims.get("memory_consent") is True else []
        if self.role == "merchant" and (guest or "merchant" not in roles):
            raise AccessDenied("Workforce merchant permission required")
        epoch = claims.get("guest_epoch")
        if guest and (not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 0):
            raise AccessDenied("Guest epoch required")
        if guest and (
            not isinstance(claims.get("jti"), str)
            or not 1 <= len(claims["jti"]) <= 128
            or not isinstance(claims.get("memory_consent"), bool)
        ):
            raise AccessDenied("Guest revocation identity and explicit consent required")
        return Principal(tenant, subject, self.role, frozenset(roles), guest, epoch), claims


def verify_guest_registry(
    documents: Documents,
    principal: Principal,
    claims: dict[str, Any],
    *,
    allow_frozen: bool = False,
) -> None:
    if not principal.guest:
        return
    record = documents.read(partition(principal.tenant, "guest", principal.subject), "identity")
    if (
        record is None
        or record.get("revoked")
        or record.get("expires", 0) <= time.time()
        or record.get("epoch") != claims.get("guest_epoch")
        or ("revocation_id" in record and record["revocation_id"] != claims.get("jti"))
        or (
            "memory_consent" in record
            and record["memory_consent"] is not claims.get("memory_consent")
        )
    ):
        raise AccessDenied("Guest identity expired or was revoked")
    memory = documents.read(partition(principal.scope, "memory"), "memory")
    if memory and memory.get("revoked") and not allow_frozen:
        raise AccessDenied("Guest identity was migrated or deleted")


def refresh_guest_registry(
    documents: Documents,
    principal: Principal,
    claims: dict[str, Any],
    days: int,
) -> None:
    if not principal.guest:
        return
    pk = partition(principal.tenant, "guest", principal.subject)
    for attempt in range(4):
        verify_guest_registry(documents, principal, claims)
        record = documents.read(pk, "identity")
        if record is None or record.get("revoked") or record.get("epoch") != principal.guest_epoch:
            raise AccessDenied("Guest identity changed")
        expires = time.time() + days * 86400
        try:
            documents.transact(
                pk,
                [
                    Operation(
                        "replace",
                        "identity",
                        dict(record, expires=expires, ttl=days * 86400),
                        record["_etag"],
                    )
                ],
            )
            return
        except ConflictError:
            if attempt == 3:
                raise
