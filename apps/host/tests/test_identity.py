import time
from types import SimpleNamespace

import jwt
import pytest
from commerce_host.identity import Principal, TokenVerifier, verify_guest_registry
from commerce_host.storage import AccessDenied, FakeDocuments, Operation, partition
from cryptography.hazmat.primitives.asymmetric import rsa


@pytest.fixture
def signing():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = TokenVerifier(
        "https://issuer.example",
        "commerce-api",
        "https://issuer.example/keys",
        frozenset({"tenant"}),
        "merchant",
    )
    verifier.keys = SimpleNamespace(
        get_signing_key_from_jwt=lambda token: SimpleNamespace(key=private.public_key())
    )
    claims = {
        "sub": "operator",
        "tid": "tenant",
        "aud": "commerce-api",
        "iss": "https://issuer.example",
        "iat": int(time.time()),
        "exp": int(time.time()) + 300,
        "roles": ["merchant"],
    }
    return private, verifier, claims


def test_merchant_requires_verified_approver_not_chat_or_preview(signing):
    private, verifier, claims = signing
    principal, _ = verifier.verify(jwt.encode(claims, private, algorithm="RS256"))
    assert principal.subject == "operator"
    with pytest.raises(AccessDenied):
        principal.require("approver")
    claims["roles"].append("approver")
    approved, _ = verifier.verify(jwt.encode(claims, private, algorithm="RS256"))
    approved.require("approver")


@pytest.mark.parametrize(
    "change,error",
    [
        ({"iss": "https://attacker.example"}, jwt.InvalidIssuerError),
        ({"aud": "another-api"}, jwt.InvalidAudienceError),
        ({"exp": 1}, jwt.ExpiredSignatureError),
        ({"tid": "foreign"}, AccessDenied),
        ({"roles": "merchant"}, AccessDenied),
        ({"roles": []}, AccessDenied),
        ({"guest": True}, AccessDenied),
        ({"guest": "false"}, AccessDenied),
        ({"sub": ""}, AccessDenied),
    ],
)
def test_bad_claims_fail_closed(signing, change, error):
    private, verifier, claims = signing
    with pytest.raises(error):
        verifier.verify(jwt.encode(dict(claims, **change), private, algorithm="RS256"))


def test_signature_and_algorithm_rejected(signing):
    _, verifier, claims = signing
    attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(jwt.InvalidSignatureError):
        verifier.verify(jwt.encode(claims, attacker, algorithm="RS256"))
    with pytest.raises(jwt.InvalidAlgorithmError):
        verifier.verify(
            jwt.encode(claims, "not-a-public-key-not-a-public-key-not-a-key", algorithm="HS256")
        )


def test_guest_epoch_expiry_and_memory_revocation():
    documents = FakeDocuments()
    guest = Principal("tenant", "anonymous", "shopping", guest=True)
    pk = partition("tenant", "guest", "anonymous")
    documents.transact(
        pk,
        [
            Operation(
                "create", "identity", {"epoch": 2, "expires": time.time() + 300, "revoked": False}
            )
        ],
    )
    verify_guest_registry(documents, guest, {"guest_epoch": 2})
    with pytest.raises(AccessDenied):
        verify_guest_registry(documents, guest, {"guest_epoch": 1})
    documents.transact(
        partition(guest.scope, "memory"), [Operation("create", "memory", {"revoked": True})]
    )
    with pytest.raises(AccessDenied):
        verify_guest_registry(documents, guest, {"guest_epoch": 2})
    verify_guest_registry(documents, guest, {"guest_epoch": 2}, allow_frozen=True)


def test_guest_authority_cannot_issue_customer_or_privileged_identities(signing):
    private, _, claims = signing
    verifier = TokenVerifier(
        "https://issuer.example",
        "commerce-api",
        "https://issuer.example/keys",
        frozenset({"tenant"}),
        "shopping",
        guest_issuer="https://guest.example",
        guest_jwks_url="https://guest.example/keys",
    )
    key = SimpleNamespace(
        get_signing_key_from_jwt=lambda token: SimpleNamespace(key=private.public_key())
    )
    verifier.keys = verifier.guest_keys = key
    with pytest.raises(AccessDenied):
        verifier.verify(
            jwt.encode(dict(claims, iss="https://guest.example"), private, algorithm="RS256")
        )
    guest_claims = dict(
        claims,
        iss="https://guest.example",
        guest=True,
        guest_epoch=0,
        roles=["approver", "guest_memory"],
        memory_consent=False,
        jti="guest-revocation-id",
    )
    principal, _ = verifier.verify(jwt.encode(guest_claims, private, algorithm="RS256"))
    assert principal.guest and principal.permissions == frozenset()
    principal, _ = verifier.verify(
        jwt.encode(
            dict(guest_claims, memory_consent=True),
            private,
            algorithm="RS256",
        )
    )
    assert principal.permissions == frozenset({"guest_memory"})
    with pytest.raises(AccessDenied):
        verifier.verify(
            jwt.encode(dict(guest_claims, iss=claims["iss"]), private, algorithm="RS256")
        )
