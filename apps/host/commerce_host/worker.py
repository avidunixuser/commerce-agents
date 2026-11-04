from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from typing import Any

from .api import memory_store
from .config import Settings
from .identity import Principal, verify_guest_registry
from .jobs import JobQueue, ToolJournal, job_operation
from .memory import CosmosMemoryStore
from .runtime import RuntimeFactory, change_fingerprint
from .sessions import CosmosSessionStore
from .storage import AccessDenied, ConflictError, Documents, Operation
from .telemetry import record_job, record_turn

logger = logging.getLogger(__name__)


class Worker:
    def __init__(self, settings: Settings, documents: Documents, factory: RuntimeFactory) -> None:
        self.settings, self.documents, self.factory = settings, documents, factory
        self.queue = JobQueue(documents, str(uuid.uuid4()))
        self.sessions = CosmosSessionStore(documents, settings.transcript_days)
        self.stopping = asyncio.Event()
        self.lock = asyncio.Lock()
        self.available = True

    async def run(self) -> None:
        from azure.core.exceptions import AzureError

        delay = 1
        while not self.stopping.is_set():
            try:
                found = await self.poll()
                self.available, delay = True, 1
            except AzureError as error:
                self.available = False
                logger.error("worker persistence unavailable type=%s", type(error).__name__)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.stopping.wait(), timeout=delay)
                delay = min(delay * 2, 30)
                continue
            if not found:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.stopping.wait(), timeout=2)

    async def poll(self) -> bool:
        found = False
        for shard in range(32):
            candidates = await asyncio.to_thread(
                self.documents.due_jobs,
                shard,
                time.time(),
                20,
                role=self.settings.role,
                vertical=self.settings.vertical,
            )
            for candidate in candidates:
                if self.stopping.is_set():
                    return found
                try:
                    job = await asyncio.to_thread(self.queue.claim, candidate)
                except ConflictError:
                    continue
                found = True
                await self.execute(job)
        return found

    async def execute(self, job: dict[str, Any]) -> None:
        payload = job["payload"]
        principal = Principal(
            **dict(payload["principal"], permissions=frozenset(payload["principal"]["permissions"]))
        )
        current_task = asyncio.current_task()
        lease_failed = False

        async def renew() -> None:
            nonlocal lease_failed
            try:
                while True:
                    await asyncio.sleep(15)
                    async with self.lock:
                        fresh = await asyncio.to_thread(self.queue.owned, job)
                        if fresh.get("cancel_requested") or self.stopping.is_set():
                            if current_task:
                                current_task.cancel()
                            return
                        if session is not None:
                            await asyncio.to_thread(
                                self.sessions.require,
                                principal,
                                session.id,
                                self.settings.provider_id,
                            )
                        await asyncio.to_thread(self.queue.renew, job)
            except Exception as error:
                lease_failed = True
                logger.error("execution lease failed type=%s", type(error).__name__)
                if current_task:
                    current_task.cancel()

        renewal = asyncio.create_task(renew())
        session = None
        try:
            current = await asyncio.to_thread(self.queue.owned, job)
            if current.get("cancel_requested") or self.stopping.is_set():
                if job["purpose"] in {"turn", "action"}:
                    session = await asyncio.to_thread(
                        self.sessions.require,
                        principal,
                        payload["session_id"],
                        self.settings.provider_id,
                    )
                raise asyncio.CancelledError
            if job["purpose"] == "memory_retention":
                memory = memory_store(self.documents, self.settings, principal)
                await asyncio.to_thread(memory.prune_expired)
                async with self.lock:
                    current = await asyncio.to_thread(self.queue.owned, job)
                    await asyncio.to_thread(self.queue.finish, current, "complete")
                return
            if job["purpose"] == "migration":
                from .migration import migrate_memory

                source_principal = Principal(
                    **dict(
                        payload["source"],
                        permissions=frozenset(payload["source"]["permissions"]),
                    )
                )
                await migrate_memory(
                    CosmosMemoryStore(
                        self.documents,
                        source_principal,
                        vertical=self.settings.vertical,
                        retention_days=self.settings.guest_days,
                    ),
                    CosmosMemoryStore(
                        self.documents,
                        principal,
                        vertical=self.settings.vertical,
                        retention_days=self.settings.memory_days,
                    ),
                    consent=True,
                )
                async with self.lock:
                    current = await asyncio.to_thread(self.queue.owned, job)
                    await asyncio.to_thread(self.queue.finish, current, "complete")
                return
            if job["purpose"] == "purge":
                for kind in ("transcript", "event", "effect", "job"):
                    while True:
                        page = await asyncio.to_thread(
                            self.documents.page, job["pk"], kind, limit=90
                        )
                        pending = [item for item in page if item["id"] != job["id"]]
                        if not pending:
                            break
                        async with self.lock:
                            current = await asyncio.to_thread(self.queue.owned, job)
                            await asyncio.to_thread(
                                self.documents.transact,
                                job["pk"],
                                [
                                    Operation("replace", current["id"], current, current["_etag"]),
                                    *[
                                        Operation("delete", item["id"], etag=item["_etag"])
                                        for item in pending
                                    ],
                                ],
                            )
                async with self.lock:
                    current = await asyncio.to_thread(self.queue.owned, job)
                    await asyncio.to_thread(self.queue.finish, current, "complete")
                return
            await asyncio.to_thread(
                verify_guest_registry, self.documents, principal, payload.get("claims", {})
            )
            session = await asyncio.to_thread(
                self.sessions.require,
                principal,
                payload["session_id"],
                self.settings.provider_id,
            )
            memory = (
                memory_store(self.documents, self.settings, principal)
                if not principal.guest or "guest_memory" in principal.permissions
                else CosmosMemoryStore(self.documents, principal)
            )
            agent, state_type, bundle = self.factory.build(
                principal, memory, ToolJournal(self.queue, job, lock=self.lock)
            )
            context = self.factory.context(principal, session.id)
            if principal.role == "shopping" and payload.get("page"):
                from shopping_agent import PageContext

                context.page = PageContext.model_validate(payload["page"])
            if job["purpose"] == "memory":
                expected = payload.get("memory_revision")
                if expected and await memory.extraction_revision(principal.subject) == expected:
                    memory.snapshot_revision = expected
                    await agent.update_memory(payload["messages"], context, strict=True)
                async with self.lock:
                    current = await asyncio.to_thread(self.queue.owned, job)
                    await asyncio.to_thread(self.queue.finish, current, "complete")
                return
            if session.document["active_turn"] != job["turn_id"]:
                raise ConflictError("Session no longer owns this execution")
            state = state_type.model_validate(session.document["state"])
            completion = None
            action_result = None
            if job["purpose"] == "action":
                from commerce_common.streaming import AgentEvent

                action = payload["action"]
                if action["tool"] in {"apply_change", "discard_change"}:
                    principal.require("approver")
                    if time.time() > action["approved_until"]:
                        raise AccessDenied("Approval expired")
                    pending = await bundle.backend.get_pending_changes(context)
                    target = next(
                        (
                            item
                            for item in pending
                            if item.change_id == action["arguments"]["change_id"]
                        ),
                        None,
                    )
                    if target is None or change_fingerprint(target) != action["target_fingerprint"]:
                        raise AccessDenied("Approval target changed")
                    context.approved_change_fingerprint = action["target_fingerprint"]
                    state.remember_change(target)
                    state.approved_change_ids.add(action["arguments"]["change_id"])
                    state.host_action_change_ids.add(action["arguments"]["change_id"])
                if action["tool"].startswith("route:"):
                    import hashlib
                    import json

                    handler = bundle.routes.get(action["tool"][len("route:") :])
                    if handler is None:
                        raise AccessDenied("Backend mutation capability not configured")
                    journal = ToolJournal(self.queue, job, lock=self.lock)
                    signature = hashlib.sha256(
                        json.dumps(action, sort_keys=True).encode()
                    ).hexdigest()
                    cached = await journal.begin_async("route", signature)
                    if cached is not None:
                        action_result = cached
                    else:
                        action_result = await handler(context, session, action["arguments"])
                        await journal.complete_async("route", action_result)
                    state = state_type.model_validate(session.document["state"])
                else:
                    executor = agent.executor_class(
                        backend=agent.backend,
                        config=agent.config,
                        skills=agent.skills,
                        session=context,
                        state=state,
                        memory=agent.memory,
                        extensions=agent.extra_presentation_tools,
                    )
                    try:
                        outcome = await executor.execute(action["tool"], action["arguments"])
                    finally:
                        if principal.role == "merchant":
                            state.approved_change_ids.clear()
                            state.host_action_change_ids.clear()
                    action_result = {
                        "ok": not outcome.refused,
                        "reason": outcome.result_text if outcome.refused else None,
                        **{
                            key: event.data[key]
                            for event in outcome.events
                            for key in ("cart", "change")
                            if key in event.data
                        },
                    }
                completion = AgentEvent.turn_complete("action", {}, 0, 0)
            else:
                notes = session.document.pop("pending_app_events", [])
                message = payload["message"]
                if notes:
                    message = (
                        "[App events since your last reply: " + " ".join(notes) + "]\n\n" + message
                    )
                session.messages.append({"role": "user", "content": message})
                async for event in agent.stream_turn(session.messages, context, state):
                    if event.type == "turn_complete":
                        completion = event
                        continue
                    async with self.lock:
                        await asyncio.to_thread(
                            self.queue.append_event, job, event.type, event.data
                        )
            if completion is None:
                raise RuntimeError("Model did not complete its turn")
            async with self.lock:
                current = await asyncio.to_thread(self.queue.owned, job)
                if current.get("cancel_requested"):
                    raise asyncio.CancelledError
                if current.get("pending_effects", 0):
                    raise ConflictError("An external effect requires reconciliation")
                if action_result and action_result.get("ok"):
                    pending = session.document.setdefault("pending_app_events", [])
                    pending.append(f"{payload['action']['tool']} completed.")
                    session.document["pending_app_events"] = pending[-32:]
                session.document.update(state=state.model_dump(mode="json"), active_turn=None)
                sequence = current["events"] + 1
                operations = [
                    Operation(
                        "replace",
                        current["id"],
                        dict(current, status="complete", events=sequence, result=action_result),
                        current["_etag"],
                    ),
                    Operation(
                        "create",
                        f"event:{job['turn_id']}:{sequence:06d}",
                        {
                            "kind": "event",
                            "turn_id": job["turn_id"],
                            "sequence": sequence,
                            "type": completion.type,
                            "data": completion.data,
                            "ttl": self.settings.transcript_days * 86400,
                        },
                    ),
                ]
                if agent.memory.enabled and job["purpose"] == "turn":
                    operations.append(
                        job_operation(
                            str(uuid.uuid4()),
                            dict(payload, messages=session.messages),
                            purpose="memory",
                        )
                    )
                await asyncio.to_thread(self.sessions.commit, session, operations)
            record_turn(self.settings.role, self.settings.provider_id, completion.data)
            record_job(self.settings.role, "complete")
        except asyncio.CancelledError:
            with contextlib.suppress(ConflictError):
                async with self.lock:
                    current = await asyncio.to_thread(self.queue.owned, job)
                    uncertain = lease_failed or bool(
                        current.get("pending_effects", 0) or current.get("completed_effects", 0)
                    )
                    await asyncio.to_thread(
                        self.queue.finish,
                        current,
                        "uncertain" if uncertain else "cancelled",
                        "Execution requires reconciliation" if uncertain else "Execution cancelled",
                    )
            if self.stopping.is_set():
                return
        except (ConflictError, AccessDenied) as error:
            record_job(self.settings.role, "rejected")
            logger.warning("execution rejected type=%s", type(error).__name__)
            with contextlib.suppress(ConflictError):
                async with self.lock:
                    current = await asyncio.to_thread(self.queue.owned, job)
                    uncertain = bool(
                        current.get("pending_effects", 0) or current.get("completed_effects", 0)
                    )
                    status = (
                        "uncertain"
                        if uncertain
                        else ("cancelled" if current.get("cancel_requested") else "failed")
                    )
                    await asyncio.to_thread(
                        self.queue.finish,
                        current,
                        status,
                        "Execution requires reconciliation" if uncertain else "Execution rejected",
                    )
        except Exception as error:
            record_job(self.settings.role, "failed")
            logger.error("execution failed type=%s", type(error).__name__)
            with contextlib.suppress(ConflictError):
                async with self.lock:
                    current = await asyncio.to_thread(self.queue.owned, job)
                    if job["purpose"] in {"memory", "memory_retention", "purge", "migration"}:
                        await asyncio.to_thread(self.queue.retry, current, safe_to_retry=True)
                    else:
                        uncertain = bool(
                            current.get("pending_effects", 0) or current.get("completed_effects", 0)
                        )
                        await asyncio.to_thread(
                            self.queue.finish,
                            current,
                            "uncertain" if uncertain else "failed",
                            "Execution requires reconciliation"
                            if uncertain
                            else "Execution failed",
                        )
        finally:
            renewal.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await renewal
            if session and job["purpose"] in {"turn", "action"}:
                latest = self.documents.read(session.pk, "session")
                status = self.documents.read(session.pk, job["id"])
                if (
                    latest
                    and status
                    and latest["active_turn"] == job["turn_id"]
                    and status["status"] in {"cancelled", "failed"}
                ):
                    # Preserve the last committed state, never overwrite with a failed turn.
                    with contextlib.suppress(ConflictError):
                        await asyncio.to_thread(
                            self.documents.transact,
                            session.pk,
                            [
                                Operation(
                                    "replace",
                                    "session",
                                    dict(latest, active_turn=None),
                                    latest["_etag"],
                                )
                            ],
                        )
