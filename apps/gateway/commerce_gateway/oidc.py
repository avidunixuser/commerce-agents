import base64
import hashlib
import hmac
import time
from urllib.parse import urlencode, urlsplit

import jwt


def pkce_challenge(verifier: str) -> str:
    return (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )


class OIDC:
    def __init__(self, settings, client):
        self.settings = settings
        self.client = client
        self.metadata = None
        self.jwks = None
        self.cached_until = 0

    async def discover(self):
        if self.metadata and self.cached_until > time.time():
            return self.metadata
        response = await self.client.get(
            self.settings.issuer.rstrip("/") + "/.well-known/openid-configuration"
        )
        response.raise_for_status()
        metadata = response.json()
        if metadata["issuer"] != self.settings.issuer:
            raise ValueError("OIDC discovery issuer mismatch")
        for name in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            if urlsplit(metadata[name]).scheme != "https":
                raise ValueError("OIDC endpoints require HTTPS")
        keys = await self.client.get(metadata["jwks_uri"])
        keys.raise_for_status()
        self.metadata, self.jwks = metadata, keys.json()
        self.cached_until = time.time() + 300
        return metadata

    async def authorization_url(self, flow):
        metadata = await self.discover()
        return (
            metadata["authorization_endpoint"]
            + "?"
            + urlencode(
                {
                    "client_id": self.settings.client_id,
                    "response_type": "code",
                    "redirect_uri": self.settings.origin + "/auth/callback",
                    "scope": self.settings.scope,
                    "state": flow["state"],
                    "nonce": flow["nonce"],
                    "code_challenge": pkce_challenge(flow["verifier"]),
                    "code_challenge_method": "S256",
                }
            )
        )

    async def validate(self, token: str, audience: str):
        await self.discover()
        header = jwt.get_unverified_header(token)
        if header.get("alg") not in {"RS256", "ES256", "EdDSA"}:
            raise ValueError("Unsupported OIDC signature algorithm")
        matches = [
            key
            for key in self.jwks["keys"]
            if key.get("kid") == header.get("kid")
            and key.get("use", "sig") == "sig"
            and key.get("alg", header["alg"]) == header["alg"]
        ]
        if len(matches) != 1:
            self.cached_until = 0
            await self.discover()
            matches = [
                key
                for key in self.jwks["keys"]
                if key.get("kid") == header.get("kid")
                and key.get("use", "sig") == "sig"
                and key.get("alg", header["alg"]) == header["alg"]
            ]
        if len(matches) != 1:
            raise ValueError("Unknown OIDC signing key")
        claims = jwt.decode(
            token,
            jwt.PyJWK.from_dict(matches[0]).key,
            algorithms=[header["alg"]],
            issuer=self.settings.issuer,
            audience=audience,
            options={"require": ["exp", "iat", "sub", "iss", "aud"]},
        )
        if claims.get("tid") != self.settings.tenant:
            raise ValueError("OIDC tenant mismatch")
        if not claims["sub"]:
            raise ValueError("Missing subject")
        return claims

    async def exchange(self, code, flow):
        metadata = await self.discover()
        response = await self.client.post(
            metadata["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.settings.origin + "/auth/callback",
                "client_id": self.settings.client_id,
                "client_secret": self.settings.client_secret,
                "code_verifier": flow["verifier"],
            },
        )
        response.raise_for_status()
        tokens = response.json()
        identity = await self.validate(tokens["id_token"], self.settings.client_id)
        if not hmac.compare_digest(str(identity.get("nonce", "")), flow["nonce"]):
            raise ValueError("OIDC nonce mismatch")
        if identity.get("azp", self.settings.client_id) != self.settings.client_id:
            raise ValueError("OIDC authorized party mismatch")
        if (
            isinstance(identity["aud"], list)
            and len(identity["aud"]) > 1
            and identity.get("azp") != self.settings.client_id
        ):
            raise ValueError("Multi-audience OIDC token requires an authorized party")
        access = await self.validate(tokens["access_token"], self.settings.audience)
        # Access tokens can use a resource-specific sub; oid is stable across Entra resources.
        if identity.get("oid", identity["sub"]) != access.get("oid", access["sub"]):
            raise ValueError("OIDC token subject mismatch")
        return tokens["access_token"], access
