from __future__ import annotations

import hashlib
import importlib
import json
import os
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from commerce_common.streaming import AgentEvent, ToolOutcome
from merchant_agent import MerchantSessionContext, MerchantSessionState
from merchant_agent.executor import MerchantToolExecutor
from shopping_agent import ShoppingSessionContext, ShoppingSessionState
from shopping_agent.executor import ShoppingToolExecutor
from shopping_agent.types import UserPreferences

from .config import Settings
from .identity import Principal
from .jobs import ToolJournal
from .memory import CosmosMemoryStore
from .storage import AccessDenied

MUTATIONS = frozenset(
    {
        "add_to_cart",
        "update_cart_item",
        "remove_from_cart",
        "save_memory",
        "apply_change",
        "discard_change",
        "checkout",
    }
)


@dataclass
class BackendBundle:
    """A real integration supplies backends and vertical extensions, never model credentials."""

    backend: Any
    config: Any
    extensions: tuple[Any, ...] = ()
    executor_class: type[Any] | None = None
    capabilities: frozenset[str] = frozenset()
    routes: dict[str, Any] = field(default_factory=dict)


def change_fingerprint(change: Any) -> str:
    return hashlib.sha256(
        json.dumps(change.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class HostShoppingContext(ShoppingSessionContext):
    tenant_id: str
    principal_scope: str


class HostMerchantContext(MerchantSessionContext):
    tenant_id: str
    principal_scope: str
    approved_change_fingerprint: str | None = None


class GuestBackend:
    """Guest prefetches must not inherit a backend's fallback customer profile."""

    def __init__(self, backend: Any) -> None:
        self.backend = backend

    def __getattr__(self, name: str) -> Any:
        return getattr(self.backend, name)

    async def get_preferences(self, context: Any) -> UserPreferences:
        return UserPreferences(user_id=context.user_id, display_name="Guest")

    async def get_account_context(self, context: Any) -> None:
        return None

    async def get_orders(self, context: Any, limit: int = 5) -> Any:
        raise AccessDenied("Guest identities do not have customer orders")

    async def get_order(self, context: Any, order_id: str) -> Any:
        raise AccessDenied("Guest identities do not have customer orders")

    async def get_fulfillment_options(self, context: Any, product_ids: Any) -> Any:
        raise AccessDenied("Sign in to use personalized fulfillment")


class JournaledExecutor:
    journal: ToolJournal | None = None
    principal: Principal
    _call_sequence: int = 0
    schemas: dict[str, Any]

    async def execute(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        if self.principal.guest and name in {"get_orders", "get_order", "get_account_context"}:
            return ToolOutcome.error("Sign in to access an account or orders.")
        is_write = name in MUTATIONS or name.startswith("stage_")
        if not is_write:
            return await super().execute(name, arguments)
        import jsonschema

        schema = self.schemas.get(name)
        if schema is None:
            return ToolOutcome.error("This tool is not enabled for the deployment.")
        try:
            jsonschema.Draft202012Validator(schema).validate(arguments)
        except jsonschema.ValidationError:
            return ToolOutcome.error(f"Invalid {name} arguments. Follow the tool input schema.")
        if self.journal is None:
            raise RuntimeError("Mutating tools require a durable execution journal")
        self._call_sequence += 1
        key = f"{self._call_sequence:04d}"
        digest = hashlib.sha256(
            json.dumps([name, arguments], sort_keys=True, default=str).encode()
        ).hexdigest()
        cached = await self.journal.begin_async(key, digest)
        if cached is not None:
            return ToolOutcome(
                result_text=cached["result_text"],
                events=[AgentEvent.model_validate(event) for event in cached["events"]],
                is_error=cached["is_error"],
                blocked=cached["blocked"],
            )
        outcome = await super().execute(name, arguments)
        if not outcome.is_error or name == "save_memory":
            await self.journal.complete_async(
                key,
                {
                    "result_text": outcome.result_text,
                    "events": [event.model_dump(mode="json") for event in outcome.events],
                    "is_error": outcome.is_error,
                    "blocked": outcome.blocked,
                },
            )
        return outcome


class RuntimeFactory:
    def __init__(self, settings: Settings) -> None:
        from commerce_common.providers import provider_configuration_id

        self.settings = settings
        if not os.environ.get("COMMERCE_PROVIDER", "").strip():
            raise ValueError(
                "COMMERCE_PROVIDER must be explicitly configured for hosted applications"
            )
        if provider_configuration_id(os.environ) != settings.provider_id:
            raise ValueError("COMMERCE_PROVIDER_ID does not match model deployment configuration")
        self.providers: dict[str, Any] = {}
        module, separator, name = settings.backend_factory.partition(":")
        if not separator:
            raise ValueError("COMMERCE_BACKEND_FACTORY must be module:function")
        self.factory = getattr(importlib.import_module(module), name)
        self.provider("chat")

    def provider(self, purpose: str) -> Any:
        from commerce_common.providers import create_provider_from_env

        if purpose not in self.providers:
            self.providers[purpose] = create_provider_from_env(
                role=self.settings.role, purpose=purpose
            )
        return self.providers[purpose]

    async def aclose(self) -> None:
        for provider in self.providers.values():
            await provider.aclose()

    def bundle(self, principal: Principal) -> BackendBundle:
        result = self.factory(self.settings, principal)
        if not isinstance(result, BackendBundle):
            raise TypeError("Backend factory must return BackendBundle")
        if self.settings.mode == "production" and "durable_backend" not in result.capabilities:
            raise ValueError("Production requires an authoritative durable backend adapter")
        if (
            self.settings.mode == "production"
            and self.settings.role == "merchant"
            and "conditional_approvals" not in result.capabilities
        ):
            raise ValueError("Merchant backends must enforce conditional approval fingerprints")
        return replace(result, backend=GuestBackend(result.backend)) if principal.guest else result

    def build(
        self,
        principal: Principal,
        memory: CosmosMemoryStore,
        journal: ToolJournal | None = None,
    ) -> tuple[Any, Any, BackendBundle]:
        from merchant_agent_runtime import MerchantAgent
        from shopping_agent_runtime import ShoppingAgent

        bundle = self.bundle(principal)
        shopping = self.settings.role == "shopping"
        base = bundle.executor_class or (ShoppingToolExecutor if shopping else MerchantToolExecutor)
        executor = type(
            "DurableExecutor",
            (JournaledExecutor, base),
            {"journal": journal, "principal": principal},
        )
        config = bundle.config
        if self.settings.portable_tools:
            features = {
                "thinking_effort": None,
                "rolling_conversation_cache": False,
                "enable_web_search": False,
            }
            if not shopping:
                features["analysis_use_code_execution"] = False
            config = config.model_copy(update=features)
        if principal.guest:
            config = config.model_copy(
                update={
                    "enable_orders": False,
                    "enable_fulfillment": False,
                    "enable_memory": "guest_memory" in principal.permissions,
                }
            )
        if not shopping and not config.require_host_approval:
            raise ValueError("Production merchant execution must require host approval")
        root = Path(os.environ.get("COMMERCE_APP_ROOT") or Path(__file__).resolve().parents[3])
        role_dir = "shopping-agent" if shopping else "merchant-agent"
        agent_type = ShoppingAgent if shopping else MerchantAgent
        provider_arguments = {
            "provider": self.provider("chat"),
            "extraction_provider": self.provider("extraction") if config.enable_memory else None,
        }
        if not shopping:
            provider_arguments["analysis_provider"] = (
                self.provider("analysis") if config.enable_analysis else None
            )
        agent = agent_type(
            backend=bundle.backend,
            config=config,
            memory_store=memory,
            skills_dir=root / role_dir / "skills",
            extra_presentation_tools=bundle.extensions,
            executor_class=executor,
            **provider_arguments,
        )
        from commerce_common.execution import contracts_by_name

        if shopping:
            from shopping_agent.tools.registry import build_tools

            tools = build_tools(agent.config, agent.skills.names, agent.extra_presentation_tools)
        else:
            from merchant_agent.tools.registry import build_tools

            tools = build_tools(
                agent.config,
                agent.skills.names,
                agent.extra_presentation_tools,
                agent.extra_delegates,
            )
        executor.schemas = {
            name: contract["input_schema"] for name, contract in contracts_by_name(tools).items()
        }
        state_type = ShoppingSessionState if shopping else MerchantSessionState
        return agent, state_type, bundle

    def context(self, principal: Principal, session_id: str) -> Any:
        if principal.role != self.settings.role:
            raise AccessDenied("Wrong application role")
        if principal.role == "shopping":
            return HostShoppingContext(
                session_id=session_id,
                user_id=principal.subject,
                now=datetime.now(),
                tenant_id=principal.tenant,
                principal_scope=principal.scope,
            )
        return HostMerchantContext(
            session_id=session_id,
            merchant_id=principal.subject,
            operator=principal.subject,
            now=datetime.now(),
            tenant_id=principal.tenant,
            principal_scope=principal.scope,
        )
