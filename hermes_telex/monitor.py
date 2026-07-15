"""Subscribe loop and message-sync driver (port of openclaw-telex monitor.ts).

The subscribe stream is forward-only and lossy. The driver pairs it with the
server-persisted read cursor: frames dispatch directly and settle by seq, a
watermark of contiguously settled seqs is marked back (debounced), dual-bound
list-messages windows repair gaps, and an hourly full-listing sweep is the
final backstop. A stale timer (no frame for STALE_TIMEOUT) treats the
connection as half-open and reconnects; the first frame after connect is the
server's readiness signal, which triggers reconciliation.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any, Awaitable, Callable, Coroutine

from .client import TelexClient, TelexError, auth_error, conversation_gone
from .log import get_logger
from .types import MessageStatus

logger = get_logger("subscribe")

INITIAL_BACKOFF_S = 1.0
MAX_BACKOFF_S = 30.0
STALE_TIMEOUT_S = 60.0
PAGE = 100
POISON_MAX_ATTEMPTS = 3
SWEEP_INTERVAL_S = 3600.0
REPAIR_LAZY_S = 30.0
REPAIR_BACKOFF_MAX_S = 600.0

MessageHandler = Callable[[dict[str, Any]], Awaitable[None]]


class _StaleStream(Exception):
    """Internal signal: the stream went silent past STALE_TIMEOUT."""


class TelexSyncDriver:
    """Per-account sync state machine: frame path, repair, reconcile, sweep.

    All of a conversation's work runs under its lock (the per-conversation
    executor); different conversations proceed concurrently.
    """

    def __init__(self, client: TelexClient, on_message: MessageHandler, *, account_id: str = "default"):
        self.client = client
        self.on_message = on_message
        self.account_id = account_id
        self._locks: dict[str, asyncio.Lock] = {}
        self._repair_tasks: dict[str, dict[str, Any]] = {}
        # Frame/reconcile tasks are tracked both to keep strong references (an
        # untracked create_task can be garbage-collected) and to cancel on close.
        self._tasks: set[asyncio.Task] = set()
        self._reconcile_lock = asyncio.Lock()
        self._closed = False

    def _lock(self, conversation_id: str) -> asyncio.Lock:
        lock = self._locks.get(conversation_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[conversation_id] = lock
        return lock

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        if self._closed:
            coro.close()
            return
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def submit_frame(self, message: dict[str, Any]) -> None:
        self._spawn(self.process_frame(message))

    def close(self) -> None:
        self._closed = True
        for entry in self._repair_tasks.values():
            task = entry["task"]
            if not task.done():
                task.cancel()
        self._repair_tasks.clear()
        for task in list(self._tasks):
            if not task.done():
                task.cancel()
        self._tasks.clear()

    def _drop_conversation(self, conversation_id: str) -> None:
        # The lock is retained for the driver's lifetime: popping a lock that
        # tasks still hold or await would let a re-seeded conversation run
        # concurrently under a fresh lock.
        self.client.drop_conversation(conversation_id)
        entry = self._repair_tasks.pop(conversation_id, None)
        if entry and not entry["task"].done():
            entry["task"].cancel()

    # The frame path: seed unknown conversations (the frame itself dispatches
    # first, repair covers the backlog before it), dedup by seq, settle or
    # poison-count.
    async def process_frame(self, message: dict[str, Any]) -> None:
        conv = message.get("conversation_id")
        if not conv:
            return
        seq = message.get("seq", 0)
        async with self._lock(conv):
            if self._closed:
                return
            seeded = False
            if not self.client.is_seeded(conv):
                try:
                    conversation = await self.client.get_conversation(conv, force_refresh=True)
                except Exception as exc:  # noqa: BLE001
                    if auth_error(exc):
                        logger.error("[%s] seeding rejected: check key scopes: %s", self.account_id, exc)
                        return
                    logger.warning(
                        "[%s] seeding failed, frame dropped (sweep recovers) conversation=%s: %s",
                        self.account_id, conv, exc,
                    )
                    return
                membership = conversation.get("membership") or {}
                self.client.seed_conversation(conv, membership.get("read_seq") or 0, conversation.get("last_seq") or 0)
                seeded = True
                if self.client.is_lagging(conv):
                    # The early returns below (in_progress, duplicate) must not
                    # skip the backlog just discovered by seeding.
                    self._schedule_repair(conv, 0)
            if message.get("status") == MessageStatus.IN_PROGRESS:
                return
            if self.client.is_disposed(conv, seq):
                return
            if self.client.poison_count(conv, seq) >= POISON_MAX_ATTEMPTS:
                self.client.settle(conv, seq)
                self.client.schedule_read_sync(conv)
            else:
                try:
                    await self.on_message(message)
                except Exception as exc:  # noqa: BLE001
                    if conversation_gone(exc):
                        self._drop_conversation(conv)
                        return
                    if auth_error(exc):
                        logger.error("[%s] handle rejected: check key scopes: %s", self.account_id, exc)
                        self.client.observe_seq(conv, seq)
                        return
                    self._note_poison_failure(conv, message, exc)
                    self.client.observe_seq(conv, seq)
                    self._schedule_repair(conv, 0 if seeded else REPAIR_LAZY_S)
                    return
                self.client.settle(conv, seq)
                self.client.schedule_read_sync(conv)
            self.client.observe_seq(conv, seq)
            if self.client.is_lagging(conv):
                self._schedule_repair(conv, 0 if seeded else REPAIR_LAZY_S)

    # The give-up log fires exactly on the failure that reaches the cap; the
    # skip paths that later settle the seq stay silent.
    def _note_poison_failure(self, conversation_id: str, message: dict[str, Any], exc: Exception) -> None:
        count = self.client.bump_poison(conversation_id, message.get("seq", 0))
        if count >= POISON_MAX_ATTEMPTS:
            logger.error(
                "[%s] giving up on message after repeated failures conversation=%s id=%s seq=%s attempts=%d: %s",
                self.account_id, conversation_id, message.get("id"), message.get("seq"), count, exc,
            )
        else:
            logger.warning(
                "[%s] handle failed; repair will retry conversation=%s id=%s seq=%s attempts=%d: %s",
                self.account_id, conversation_id, message.get("id"), message.get("seq"), count, exc,
            )

    def _schedule_repair(self, conversation_id: str, delay: float) -> None:
        if self._closed:
            return
        due = asyncio.get_running_loop().time() + delay
        existing = self._repair_tasks.get(conversation_id)
        if existing and not existing["task"].done():
            # Lazy/backoff requests keep the first armed timer (or a hole under
            # steady traffic would reset the backoff every frame); only an
            # immediate request (connect/sweep/seeding) preempts, and only when
            # strictly earlier.
            if delay > 0 or existing["due"] <= due:
                return
            existing["task"].cancel()
            self._repair_tasks.pop(conversation_id, None)

        async def run() -> None:
            try:
                await asyncio.sleep(delay)
                backoff = min(REPAIR_BACKOFF_MAX_S, max(delay, REPAIR_LAZY_S) * 2)
                async with self._lock(conversation_id):
                    if self._closed:
                        return
                    lagging = await self._repair_window(conversation_id)
                if lagging:
                    self._repair_tasks.pop(conversation_id, None)
                    self._schedule_repair(conversation_id, backoff)
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001
                if conversation_gone(exc):
                    self._drop_conversation(conversation_id)
                    return
                if auth_error(exc):
                    logger.error(
                        "[%s] repair rejected: check key scopes conversation=%s: %s",
                        self.account_id, conversation_id, exc,
                    )
                    return  # state kept; the sweep retries hourly
                logger.warning(
                    "[%s] repair failed, backing off conversation=%s: %s",
                    self.account_id, conversation_id, exc,
                )
                self._repair_tasks.pop(conversation_id, None)
                self._schedule_repair(conversation_id, min(REPAIR_BACKOFF_MAX_S, max(delay, REPAIR_LAZY_S) * 2))

        self._repair_tasks[conversation_id] = {"task": asyncio.create_task(run()), "due": due}

    # One dual-bound repair window: mark first (the watermark may already cover
    # the lag), read [cursor+1, cursor+PAGE], settle each row; a foreign
    # in_progress row stops the watermark but not the dispatch of later rows.
    # Returns whether lag remains. Caller holds the conversation lock.
    async def _repair_window(self, conversation_id: str) -> bool:
        client = self.client
        try:
            await client.sync_read_cursor(conversation_id)
        except Exception as exc:  # noqa: BLE001
            if conversation_gone(exc) or auth_error(exc):
                raise
            logger.warning("[%s] inline mark failed conversation=%s: %s", self.account_id, conversation_id, exc)
        if not client.is_lagging(conversation_id):
            return False
        base = client.get_cursor(conversation_id)
        window = await client.list_messages(
            conversation_id, after_seq=base, before_seq=base + PAGE + 1, limit=PAGE
        )
        for i, message in enumerate(window):
            if message.get("seq") != base + 1 + i:
                raise TelexError(f"non-contiguous repair window at seq {message.get('seq')}")
        for message in window:
            seq = message.get("seq", 0)
            if client.is_disposed(conversation_id, seq):
                continue
            if message.get("status") == MessageStatus.IN_PROGRESS:
                if client.is_own_message(message):
                    client.settle(conversation_id, seq)
                continue
            if client.poison_count(conversation_id, seq) >= POISON_MAX_ATTEMPTS:
                client.settle(conversation_id, seq)
                continue
            try:
                await self.on_message(message)
            except Exception as exc:  # noqa: BLE001
                # Conversation-gone / configuration errors are classified by the
                # scheduler, not counted as poison.
                if conversation_gone(exc) or auth_error(exc):
                    raise
                self._note_poison_failure(conversation_id, message, exc)
                raise TelexError(f"handle failed at seq {seq}: {exc}") from exc
            client.settle(conversation_id, seq)
        try:
            await client.sync_read_cursor(conversation_id)
        except Exception as exc:  # noqa: BLE001
            if conversation_gone(exc) or auth_error(exc):
                raise
            logger.warning("[%s] inline mark failed conversation=%s: %s", self.account_id, conversation_id, exc)
        return client.is_lagging(conversation_id)

    # One full-listing reconciliation (connect and the sweep): adopt each
    # membership's read_seq, schedule repair where lagging, and drop
    # conversations known before the listing began but absent from its complete
    # result. Rounds are serialized by a lock so every caller gets a fresh
    # listing (a readiness request must not be satisfied by a snapshot that
    # predates the new subscription). Per-conversation work is spawned onto each
    # conversation's executor, never awaited inline: the subscribe reader awaits
    # reconcile, so blocking on a busy conversation lock would stall the whole
    # stream. Returns False when the listing failed.
    async def reconcile(self) -> bool:
        async with self._reconcile_lock:
            if self._closed:
                return True
            try:
                known_at_start = set(self.client.known_conversations())
                res = await self.client.list_conversations()
                conversations = res["conversations"]
            except Exception as exc:  # noqa: BLE001
                if auth_error(exc):
                    logger.error("[%s] listing rejected: check key scopes: %s", self.account_id, exc)
                else:
                    logger.warning("[%s] conversation listing failed: %s", self.account_id, exc)
                return False
            listed = set()
            for conversation in conversations:
                conv_id = conversation.get("id", "")
                if not conv_id:
                    continue
                listed.add(conv_id)
                self._spawn(self._reconcile_seed(conv_id, conversation))
            for conv_id in known_at_start:
                if conv_id not in listed:
                    self._spawn(self._reconcile_drop(conv_id))
            return True

    async def _reconcile_seed(self, conv_id: str, conversation: dict[str, Any]) -> None:
        membership = conversation.get("membership") or {}
        async with self._lock(conv_id):
            if self._closed:
                return
            self.client.seed_conversation(
                conv_id, membership.get("read_seq") or 0, conversation.get("last_seq") or 0
            )
            if self.client.is_lagging(conv_id):
                self._schedule_repair(conv_id, 0)

    async def _reconcile_drop(self, conv_id: str) -> None:
        async with self._lock(conv_id):
            self._drop_conversation(conv_id)


async def run_monitor(
    client: TelexClient,
    on_message: MessageHandler,
    stop_event: asyncio.Event,
    *,
    account_id: str = "default",
) -> None:
    """Run the subscribe loop until stop_event is set (or a fatal auth error)."""
    driver = TelexSyncDriver(client, on_message, account_id=account_id)
    backoff = INITIAL_BACKOFF_S

    async def sweep_loop() -> None:
        while not stop_event.is_set():
            await asyncio.sleep(SWEEP_INTERVAL_S)
            await driver.reconcile()

    sweep_task = asyncio.create_task(sweep_loop())
    try:
        while not stop_event.is_set():
            try:
                await client.ensure_self_id()
            except Exception as exc:  # noqa: BLE001
                # Never blocks the stream: seeding arms the identity from
                # membership rows before any dispatch.
                if auth_error(exc):
                    logger.error(
                        "[%s] get-identity rejected: check key scopes; "
                        "identity will be armed from membership rows: %s",
                        account_id, exc,
                    )
                else:
                    logger.warning(
                        "[%s] get-identity failed; identity will be armed from membership rows: %s",
                        account_id, exc,
                    )
            frame_event = asyncio.Event()
            ready = {"seeded": False}

            async def on_event(result: dict[str, Any]) -> None:
                frame_event.set()  # any frame (incl. heartbeat) resets the stale timer
                # The first frame is the server's readiness signal: the
                # subscription is live, so reconciling now cannot miss messages.
                if not ready["seeded"]:
                    ready["seeded"] = True
                    if not await driver.reconcile():
                        # Retry on the next frame instead of waiting for a reconnect.
                        ready["seeded"] = False
                message = result.get("message") if isinstance(result, dict) else None
                if message is None:
                    return
                # Only enqueue: the frame loop must never block on a slow turn.
                driver.submit_frame(message)

            sub_task = asyncio.create_task(client.subscribe(on_event, stop_event=stop_event))
            try:
                await _watch_stale(sub_task, frame_event, stop_event)
                if stop_event.is_set():
                    # subscribe() only notices the stop flag on the next chunk;
                    # a half-open stream would wedge shutdown. The finally
                    # blocks cancel and join sub_task.
                    return
                await sub_task  # surface any exception from a clean stream end
                backoff = INITIAL_BACKOFF_S
            except TelexError as exc:
                if auth_error(exc):
                    logger.error("[%s] subscribe auth failure (fatal): %s", account_id, exc)
                    return
                logger.warning("[%s] subscribe error: %s", account_id, exc)
            except _StaleStream:
                logger.warning("[%s] subscribe stale - reconnecting", account_id)
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
    finally:
        sweep_task.cancel()
        driver.close()


async def _watch_stale(sub_task: asyncio.Task, frame_event: asyncio.Event, stop_event: asyncio.Event) -> None:
    """Wait for the subscribe task, cancelling it if no frame arrives in STALE_TIMEOUT.

    Waits on the subscribe task itself alongside the frame/stop signals, so a
    stream failure surfaces immediately (with its real error) instead of being
    misreported as staleness after a silent minute. Staleness is signalled with
    _StaleStream, never CancelledError: a genuine external cancellation of
    run_monitor must propagate, not be misread as a reconnect request.
    """
    while True:
        if sub_task.done():
            return
        frame_waiter = asyncio.create_task(frame_event.wait())
        stop_waiter = asyncio.create_task(stop_event.wait())
        try:
            done, _pending = await asyncio.wait(
                {sub_task, frame_waiter, stop_waiter},
                timeout=STALE_TIMEOUT_S,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            frame_waiter.cancel()
            stop_waiter.cancel()
        if sub_task in done or stop_waiter in done:
            return
        if frame_waiter in done:
            frame_event.clear()
            continue
        # Timeout: no frame, no completion, no stop - the connection is half-open.
        sub_task.cancel()
        raise _StaleStream()
