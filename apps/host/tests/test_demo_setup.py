import copy

import pytest
from azure.cosmos.exceptions import CosmosResourceExistsError
from commerce_host.demo_setup import NoRedirect, SetupError, Vault, initialize, signing_key


class Container:
    def __init__(self):
        self.lock = None

    def create_item(self, item):
        if self.lock:
            raise CosmosResourceExistsError(message="already exists")
        self.lock = {**copy.deepcopy(item), "_etag": "created"}
        return copy.deepcopy(self.lock)

    def replace_item(self, item_id, item, *, etag, match_condition):
        assert item_id == self.lock["id"] and etag == self.lock["_etag"]
        assert match_condition.name == "IfNotModified"
        self.lock = copy.deepcopy(item)


class Secrets:
    def __init__(self):
        self.values = {}
        self.writes = []

    def request(self, method, name, body=None):
        if method == "GET":
            return copy.deepcopy(self.values.get(name))
        assert method == "PUT"
        self.writes.append(name)
        self.values[name] = copy.deepcopy(body)
        return copy.deepcopy(body)


def test_setup_writes_only_namespaced_secrets_and_persists_no_key_material():
    container, vault = Container(), Secrets()
    names = initialize(container, vault, "acmedemo", "configured-oidc-secret")
    assert len(names) == len(vault.writes) == 5
    assert all(name.startswith("acmedemo-") for name in names)
    assert container.lock["state"] == "complete"
    assert "value" not in container.lock
    assert "configured-oidc-secret" not in str(container.lock)
    assert all(item["tags"] == {"commerce-demo": "acmedemo"} for item in vault.values.values())


def test_second_execution_cannot_rotate_or_overwrite_keys():
    container, vault = Container(), Secrets()
    initialize(container, vault, "acmedemo", "configured-oidc-secret")
    before = copy.deepcopy(vault.values)
    with pytest.raises(SetupError, match="already recorded"):
        initialize(container, vault, "acmedemo", "different-secret")
    assert vault.values == before and len(vault.writes) == 5


@pytest.mark.parametrize("kind", ["unowned", "changed-oidc", "invalid-signing"])
def test_partial_failure_retains_the_fence_and_never_reports_complete(kind):
    container, vault = Container(), Secrets()
    suffix = "shopping-signing-key" if kind == "invalid-signing" else "oidc-client-secret"
    vault.values["acmedemo-" + suffix] = {
        "tags": {"commerce-demo": "not-ours" if kind == "unowned" else "acmedemo"},
        "value": "different-secret" if kind == "changed-oidc" else "invalid",
    }
    with pytest.raises((SetupError, ValueError)):
        initialize(container, vault, "acmedemo", "configured-oidc-secret")
    assert container.lock["state"] == "running"
    with pytest.raises(SetupError, match="already recorded"):
        initialize(container, vault, "acmedemo", "configured-oidc-secret")


def test_existing_owned_signing_key_is_not_rotated_during_reconciled_setup():
    container, vault = Container(), Secrets()
    value = signing_key()
    vault.values["acmedemo-shopping-signing-key"] = {
        "tags": {"commerce-demo": "acmedemo"},
        "value": value,
    }
    initialize(container, vault, "acmedemo", "configured-oidc-secret")
    assert vault.values["acmedemo-shopping-signing-key"]["value"] == value
    assert "acmedemo-shopping-signing-key" not in vault.writes


@pytest.mark.parametrize(
    "uri",
    [
        "http://vault.vault.azure.net",
        "https://vault.example",
        "https://vault.vault.azure.net/path",
        "https://vault.vault.azure.net?secret=value",
        "https://user:password@vault.vault.azure.net",
        "https://vault.vault.azure.net:8443",
    ],
)
def test_secret_transport_rejects_untrusted_origins(uri):
    with pytest.raises(SetupError, match="HTTPS origin"):
        Vault(uri, None)


def test_bearer_credential_cannot_follow_http_redirect():
    assert (
        NoRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere.example") is None
    )


@pytest.mark.parametrize(
    "prefix,secret", [("../another", "configured"), ("acmedemo", ""), ("x" * 21, "configured")]
)
def test_invalid_input_never_reserves_a_setup_lock(prefix, secret):
    container = Container()
    with pytest.raises(SetupError):
        initialize(container, Secrets(), prefix, secret)
    assert container.lock is None
