"""Deliberately low-cardinality telemetry; no request URLs, headers or message bodies."""

from __future__ import annotations

import os
from typing import Any


def configure() -> None:
    mode = os.environ.get("COMMERCE_TELEMETRY", "console")
    if mode == "console":
        return
    if mode != "azure":
        raise ValueError("COMMERCE_TELEMETRY must be console or azure")
    if not os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING"):
        raise ValueError("Azure telemetry requires APPLICATIONINSIGHTS_CONNECTION_STRING")
    from azure.monitor.opentelemetry import configure_azure_monitor

    configure_azure_monitor(
        logger_name="commerce_host",
        enable_live_metrics=False,
        instrumentation_options={
            name: {"enabled": False}
            for name in (
                "azure_sdk",
                "django",
                "fastapi",
                "flask",
                "psycopg2",
                "requests",
                "urllib",
                "urllib3",
            )
        },
    )


def record_turn(role: str, provider: str, completion: dict[str, Any]) -> None:
    from opentelemetry import metrics

    meter = metrics.get_meter("commerce_host")
    attributes = {"role": role, "provider": provider.split(":", 1)[0]}
    usage = completion.get("usage", {})
    for kind in ("input_tokens", "output_tokens", "cache_read_input_tokens"):
        value = usage.get(kind, 0)
        if isinstance(value, int) and value >= 0:
            meter.create_counter(f"commerce.model.{kind}").add(value, attributes)
    elapsed = completion.get("elapsed_ms")
    if isinstance(elapsed, (int, float)) and elapsed >= 0:
        meter.create_histogram("commerce.turn.duration", unit="ms").record(elapsed, attributes)


def record_job(role: str, outcome: str) -> None:
    from opentelemetry import metrics

    metrics.get_meter("commerce_host").create_counter("commerce.jobs").add(
        1, {"role": role, "outcome": outcome}
    )
