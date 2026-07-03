"""Subscribe loop: heartbeat/stale detection, reconnect backoff, and
reconnect gap backfill (port of openclaw-telex monitor.ts).

The subscribe stream is forward-only. A stale timer (no frame for STALE_TIMEOUT)
treats the connection as half-open and reconnects. On each (re)connect the
server sends a readiness frame (message=null); we then backfill each conversation
from its watermark with list-messages(after_seq).
"""

from __future__ import annotations

import asyncio
import random
from typing import Any, Awaitable, Callable

from .client import TelexError
from .log import get_logger

logger = get_logger("subscribe")

INITIAL_BACKOFF_S = 1.0
MAX_BACKOFF_S = 30.0
STALE_TIMEOUT_S = 60.0
BACKFILL_PAGE_LIMIT = 100
BACKFILL_MAX_PAGES = 50

MessageHandler = Callable[[dict[str, Any]], Awaitable[None]]


async def _fetch_gap(client, conversation_id: str, after_seq: int) -> list[dict[str, Any]]:
    """Page list-messages(after_seq) ascending until a short page or the cap."""
    collected: list[dict[str, Any]] = []
    cursor = after_seq
    for page in range(BACKFILL_MAX_PAGES):
        msgs = await client.list_messages(
            conversation_id, after_seq=cursor, limit=BACKFILL_PAGE_LIMIT
        )
        if not msgs:
            break
        msgs = sorted(msgs, key=lambda m: m.get("seq", 0))
        collected.extend(msgs)
        highest = msgs[-1].get("seq", cursor)
        if len(msgs) < BACKFILL_PAGE_LIMIT or highest <= cursor:
            break
        cursor = highest
        if page == BACKFILL_MAX_PAGES - 1:
            logger.warning(
                "backfill hit page cap conversation=%s after_seq=%d", conversation_id, after_seq
            )
    return sorted(collected, key=lambda m: m.get("seq", 0))


async def _backfill(client, on_message: MessageHandler) -> None:
    for target in client.get_backfill_targets():
        conv = target["conversation_id"]
        after_seq = target["after_seq"]
        try:
            for msg in await _fetch_gap(client, conv, after_seq):
                await on_message(msg)
        except Exception as exc:  # noqa: BLE001 - one conversation's gap must not kill the loop
            logger.warning("backfill failed conversation=%s: %s", conv, exc)


async def run_monitor(
    client,
    on_message: MessageHandler,
    stop_event: asyncio.Event,
    *,
    account_id: str = "default",
) -> None:
    """Run the subscribe loop until stop_event is set (or a fatal auth error)."""
    backoff = INITIAL_BACKOFF_S
    while not stop_event.is_set():
        frame_event = asyncio.Event()
        ready = {"seeded": False}

        async def on_event(result: dict[str, Any]) -> None:
            frame_event.set()  # any frame (incl. heartbeat) resets the stale timer
            message = result.get("message") if isinstance(result, dict) else None
            if message is None:
                if not ready["seeded"]:
                    ready["seeded"] = True
                    await _backfill(client, on_message)
                return
            await on_message(message)

        sub_task = asyncio.create_task(client.subscribe(on_event, stop_event=stop_event))
        try:
            await _watch_stale(sub_task, frame_event, stop_event)
            await sub_task  # surface any exception from a clean stream end
            backoff = INITIAL_BACKOFF_S
        except TelexError as exc:
            if exc.http_status in (401, 403):
                logger.error("[%s] subscribe auth failure (fatal): %s", account_id, exc)
                return
            logger.warning("[%s] subscribe error: %s", account_id, exc)
        except asyncio.CancelledError:
            if stop_event.is_set():
                sub_task.cancel()
                raise
            logger.warning("[%s] subscribe stale — reconnecting", account_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[%s] subscribe error: %s", account_id, exc)
        finally:
            if not sub_task.done():
                sub_task.cancel()
                try:
                    await sub_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

        if stop_event.is_set():
            return
        await asyncio.sleep(backoff + random.uniform(0, backoff * 0.25))
        backoff = min(backoff * 2, MAX_BACKOFF_S)


async def _watch_stale(sub_task: asyncio.Task, frame_event: asyncio.Event, stop_event: asyncio.Event) -> None:
    """Wait for the subscribe task, cancelling it if no frame arrives in STALE_TIMEOUT."""
    while True:
        if sub_task.done():
            return
        try:
            await asyncio.wait_for(frame_event.wait(), timeout=STALE_TIMEOUT_S)
            frame_event.clear()
        except asyncio.TimeoutError:
            if stop_event.is_set():
                return
            sub_task.cancel()
            raise asyncio.CancelledError()
