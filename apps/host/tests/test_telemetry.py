import sys
from types import SimpleNamespace

import pytest
from commerce_host.telemetry import configure


@pytest.mark.parametrize("client_id", ["workload-identity", None])
def test_azure_telemetry_uses_the_workload_identity(monkeypatch, client_id):
    credential = object()
    clients = []
    calls = []

    def create_credential(**kwargs):
        clients.append(kwargs)
        return credential

    monkeypatch.setenv("COMMERCE_TELEMETRY", "azure")
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "InstrumentationKey=fixture")
    if client_id:
        monkeypatch.setenv("AZURE_CLIENT_ID", client_id)
    else:
        monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
    monkeypatch.setattr("azure.identity.ManagedIdentityCredential", create_credential)
    monkeypatch.setitem(
        sys.modules,
        "azure.monitor.opentelemetry",
        SimpleNamespace(configure_azure_monitor=lambda **kwargs: calls.append(kwargs)),
    )
    configure()
    assert clients == [{"client_id": client_id}]
    assert calls[0]["credential"] is credential
    assert calls[0]["enable_live_metrics"] is False
    assert all(not value["enabled"] for value in calls[0]["instrumentation_options"].values())
