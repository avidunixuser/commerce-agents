"""Explicit single-process lab fixtures. Never selected by production configuration."""

from __future__ import annotations

import importlib
from functools import lru_cache
from typing import Any

from .config import Settings
from .identity import Principal
from .runtime import BackendBundle

_STOREFRONTS = {
    "retail": ("mock_retail", "MockRetail"),
    "travel": ("mock_travel", "MockTravel"),
    "telecom": ("mock_telecom", "MockTelecom"),
    "entertainment": ("mock_ticketing", "MockTicketing"),
}
_MERCHANTS = {
    "retail": "MockRetailMerchant",
    "travel": "MockTravelMerchant",
    "telecom": "MockTelecomMerchant",
    "entertainment": "MockTicketingMerchant",
}


@lru_cache(maxsize=32)
def _storefront(vertical: str, scope: str) -> Any:
    module, name = _STOREFRONTS[vertical]
    return getattr(importlib.import_module(f"{vertical}.api.{module}"), name)()


@lru_cache(maxsize=32)
def build(settings: Settings, principal: Principal) -> BackendBundle:
    if settings.mode != "fixture":
        raise ValueError("Fixture backend requires COMMERCE_MODE=fixture")
    prefix = f"{settings.vertical}.api"
    storefront = _storefront(settings.vertical, principal.scope)
    config_module = importlib.import_module(f"{prefix}.agent_config")
    if settings.role == "shopping":
        extensions = []
        executor = None
        routes = {}

        async def catalog(context, session, params):
            from fastapi import HTTPException

            from demo_common.storefront_fixtures import SUMMARY_EXCLUDES

            try:
                limit = max(1, min(int(params.get("limit", 24)), 100))
            except ValueError as error:
                raise HTTPException(422, "Invalid catalog limit") from error
            products = []
            for id in storefront.products:
                product = await storefront.get_product_details(context, id)
                if product and (
                    not params.get("category") or product.category == params["category"]
                ):
                    products.append(product.model_dump(mode="json", exclude=set(SUMMARY_EXCLUDES)))
                if len(products) >= limit:
                    break
            return {"products": products}

        routes["GET /products"] = catalog
        if settings.vertical == "retail":

            async def product_detail(context, session, params):
                product = await storefront.get_product_details(context, params["id"])
                if product is None:
                    from fastapi import HTTPException

                    raise HTTPException(404, "Product not found")
                return product.model_dump(mode="json") | {
                    "price_intelligence": storefront.price_intelligence(product.product_id),
                    "review_aspects": storefront.review_aspects(product.product_id),
                }

            routes["GET /products/{id}"] = product_detail
        if settings.vertical == "travel":
            extensions.append(
                importlib.import_module(f"{prefix}.itinerary").build_itinerary_extension()
            )
        elif settings.vertical == "telecom":
            extensions.append(
                importlib.import_module(f"{prefix}.plan_matrix").build_plan_matrix_extension()
            )
        elif settings.vertical == "entertainment":
            extensions.extend(
                [
                    importlib.import_module(f"{prefix}.venue_map").build_venue_map_extension(),
                    importlib.import_module(f"{prefix}.hold_view").build_hold_view_extension(),
                ]
            )
            executor = importlib.import_module(f"{prefix}.mock_ticketing").TicketingToolExecutor
            routes.update(_ticket_routes(storefront))
        return BackendBundle(
            backend=storefront,
            config=config_module.build_shopping_config(),
            extensions=tuple(extensions),
            executor_class=executor,
            routes=routes,
        )
    config = config_module.build_merchant_config(storefront.store_name).model_copy(
        update={"require_host_approval": True}
    )
    merchant = getattr(
        importlib.import_module(f"{prefix}.mock_merchant"), _MERCHANTS[settings.vertical]
    )(storefront, config)
    extensions = []
    routes = {}
    if settings.vertical != "retail":
        module_name, builder, path, read = {
            "travel": ("occupancy", "build_occupancy_extension", "occupancy", "occupancy_overview"),
            "telecom": ("plan_mix", "build_plan_mix_extension", "base", "base_overview"),
            "entertainment": (
                "event_pacing",
                "build_event_pacing_extension",
                "pacing",
                "pacing_overview",
            ),
        }[settings.vertical]
        extensions.append(
            getattr(importlib.import_module(f"{prefix}.{module_name}"), builder)(merchant)
        )

        async def portal(context, session, params):
            return getattr(merchant, read)()

        routes[f"GET /{path}"] = portal

    async def overview(context, session, params):
        snapshot = await merchant.get_business_snapshot(context)
        inventory = await merchant.get_inventory_alerts(context)
        issues = await merchant.get_order_issues(context)
        pending = await merchant.get_pending_changes(context)
        result = {
            "snapshot": snapshot.model_dump(mode="json"),
            "needs_attention": {
                "inventory": [item.model_dump(mode="json") for item in inventory],
                "order_issues": [item.model_dump(mode="json") for item in issues],
                "pending_changes": [item.model_dump(mode="json") for item in pending],
            },
            "recent_orders": [],
            "recent_changes": [item.model_dump(mode="json") for item in merchant.ledger.resolved()],
        }
        if settings.vertical == "retail":
            result.update(
                trends=merchant.kpi_trends(),
                trends_prior=merchant.kpi_trends(periods_back=1),
                insights=merchant.home_insights(),
            )
        else:
            result["today"] = merchant.today_snapshot()
        return result

    routes["GET /overview"] = overview
    return BackendBundle(merchant, config, tuple(extensions), routes=routes)


def _ticket_routes(backend: Any) -> dict[str, Any]:
    from entertainment.api.ticketing import HOLD_TTL_S, OFFER_CLAIM_WINDOW_S, TicketingError
    from shopping_agent import ShoppingSessionState

    engine = backend.engine

    def holds(session_id):
        return {
            "hold_minutes": HOLD_TTL_S // 60,
            "offer_window_minutes": OFFER_CLAIM_WINDOW_S // 60,
            "holds": [
                {
                    "hold_id": hold.hold_id,
                    "product_id": hold.product_id,
                    "quantity": hold.quantity,
                    "expires_at": hold.expires_at.isoformat(),
                    "seconds_remaining": engine.seconds_until(hold.expires_at),
                }
                for hold in engine.holds_for_session(session_id)
            ],
        }

    async def read_holds(context, session, params):
        return holds(context.session_id)

    async def read_waitlist(context, session, params):
        return {
            "entries": [
                {"product_id": entry.product_id, "quantity": entry.quantity, "position": position}
                for entry, position in engine.waitlist_entries_for(context.user_id)
            ],
            "offers": [
                {
                    "offer_id": offer.offer_id,
                    "product_id": offer.product_id,
                    "quantity": offer.quantity,
                    "expires_at": offer.expires_at.isoformat(),
                    "seconds_remaining": engine.seconds_until(offer.expires_at),
                }
                for offer in engine.offers_for(context.user_id)
            ],
        }

    async def read_tickets(context, session, params):
        recipients = {
            ticket_id: transfer.recipient
            for transfer in engine.transfers_for(context.user_id)
            if transfer.status == "pending"
            for ticket_id in transfer.ticket_ids
        }
        tickets = []
        for ticket in engine.tickets_for(context.user_id):
            product = backend.products.get(ticket.product_id)
            attributes = product.attributes if product else {}
            tickets.append(
                {
                    "ticket_id": ticket.ticket_id,
                    "event": attributes.get("event_name", ticket.product_id),
                    "date": attributes.get("event_date"),
                    "venue": attributes.get("venue"),
                    "tier": attributes.get("tier"),
                    "seat": ticket.seat,
                    "status": ticket.status,
                    "entry_code": engine.barcode(ticket.ticket_id),
                    "entry_code_rotates_s": 60,
                    "transfer_recipient": recipients.get(ticket.ticket_id),
                }
            )
        return {"tickets": tickets}

    def action(kind):
        async def route(context, session, args):
            from pydantic import BaseModel, ConfigDict, Field

            class Parameters(BaseModel):
                model_config = ConfigDict(extra="forbid")
                hold_id: str | None = Field(default=None, min_length=1, max_length=80)
                product_id: str | None = Field(default=None, min_length=1, max_length=80)
                quantity: int = Field(default=1, ge=1, le=8)
                offer_id: str | None = Field(default=None, min_length=1, max_length=80)
                transfer_id: str | None = Field(default=None, min_length=1, max_length=80)
                ticket_ids: list[str] | None = Field(default=None, min_length=1, max_length=4)
                recipient: str | None = Field(default=None, min_length=1, max_length=80)

            values = Parameters.model_validate(args)
            state = ShoppingSessionState.model_validate(session.document["state"])
            try:
                if kind == "release":
                    if not values.hold_id:
                        raise ValueError("hold_id is required")
                    engine.release_hold_by_id(values.hold_id, context.user_id)
                    return {"ok": True, **holds(context.session_id)}
                if kind == "join":
                    if not values.product_id or values.product_id not in state.seen_products:
                        return {"ok": False, "reason": "Search for this ticket tier first"}
                    position = engine.join_waitlist(
                        context.user_id, context.session_id, values.product_id, values.quantity
                    )
                    return {"ok": True, "position": position}
                if kind == "claim":
                    if not values.offer_id:
                        raise ValueError("offer_id is required")
                    hold = engine.claim_offer(values.offer_id, context.user_id, context.session_id)
                    product = backend.get_live_product(hold.product_id)
                    if product:
                        state.remember_products([product])
                    session.document["state"] = state.model_dump(mode="json")
                    return {"ok": True, **holds(context.session_id)}
                if kind == "transfer":
                    if not values.ticket_ids or not values.recipient:
                        raise ValueError("ticket_ids and recipient are required")
                    transfer = engine.initiate_transfer(
                        context.user_id, values.ticket_ids, values.recipient.strip()
                    )
                    return {
                        "ok": True,
                        "transfer": {
                            "transfer_id": transfer.transfer_id,
                            "ticket_ids": transfer.ticket_ids,
                            "recipient": transfer.recipient,
                            "status": transfer.status,
                        },
                    }
                if not values.transfer_id:
                    raise ValueError("transfer_id is required")
                transfer = engine.cancel_transfer(context.user_id, values.transfer_id)
                return {"ok": True, "status": transfer.status}
            except TicketingError as error:
                return {"ok": False, "reason": str(error)}

        return route

    return {
        "GET /holds": read_holds,
        "GET /waitlist": read_waitlist,
        "GET /tickets": read_tickets,
        "POST /holds/release": action("release"),
        "POST /waitlist/join": action("join"),
        "POST /waitlist/claim": action("claim"),
        "POST /tickets/transfer": action("transfer"),
        "POST /tickets/transfer/cancel": action("cancel"),
    }
