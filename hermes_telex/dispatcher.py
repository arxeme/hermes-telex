"""Inbound normalization: Telex message dict -> MessageEvent -> handle_message.

Port of openclaw-telex bot.ts inbound path: watermark, self-echo suppression,
status/flag gates, access (dm/group/pairing), content + media extraction,
identity resolution, fork inheritance and mention-gap context, per-conversation
serialization, and system-event state sync.
"""

from __future__ import annotations

import asyncio
from typing import Any

from . import access, blocks
from .accounts import ResolvedTelexAccount
from .client import TelexClient
from .log import get_logger
from .media import resolve_inbound_media
from .types import ConversationKind, MessageFlag, MessageStatus

logger = get_logger("inbound")

# hermes base types (fallback stubs let this import + unit-test outside hermes).
try:  # pragma: no cover - real types inside hermes
    from gateway.platforms.base import MessageEvent, MessageType
except Exception:  # pragma: no cover
    from dataclasses import dataclass, field
    from enum import Enum

    class MessageType(Enum):  # type: ignore
        TEXT = "text"

    @dataclass
    class MessageEvent:  # type: ignore
        text: str
        message_type: Any = MessageType.TEXT
        source: Any = None
        raw_message: Any = None
        message_id: str | None = None
        media_urls: list = field(default_factory=list)
        media_types: list = field(default_factory=list)
        reply_to_message_id: str | None = None
        reply_to_text: str | None = None


_TERMINAL = {MessageStatus.COMPLETED, MessageStatus.ERROR, MessageStatus.ABORTED}
_MISSED_LIMIT = 50


class TelexDispatcher:
    def __init__(self, adapter, account: ResolvedTelexAccount, client: TelexClient):
        self.adapter = adapter
        self.account = account
        self.client = client
        # Per-conversation serialization: one agent turn at a time; different
        # conversations run concurrently.
        self._locks: dict[str, asyncio.Lock] = {}
        self._forked_seen: set[str] = set()

    def _lock(self, conversation_id: str) -> asyncio.Lock:
        lock = self._locks.get(conversation_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[conversation_id] = lock
        return lock

    async def handle(self, message: dict[str, Any]) -> None:
        conv = message.get("conversation_id")
        if not conv:
            return
        seq = message.get("seq", 0)
        status = message.get("status", MessageStatus.COMPLETED)
        flags = message.get("flags", 0)

        # 1. watermark (before any filter).
        self.client.note_message(conv, seq, status in _TERMINAL)

        # 2. self-echo suppression.
        if self.client.is_own_message(message):
            return

        # 3. status / flag gates.
        if status == MessageStatus.IN_PROGRESS:
            return
        if flags & MessageFlag.EVENT:
            await self._handle_system_event(conv, message)
            return
        if flags & MessageFlag.FORK_PREFIX:
            return  # pre-fork copied history, seeded as context only

        # 4. dedup (backfill/live overlap).
        msg_id = message.get("id", "")
        if not self.client.mark_processed(msg_id):
            return

        # Serialize per conversation.
        async with self._lock(conv):
            await self._dispatch(conv, seq, message)

    async def _dispatch(self, conv: str, seq: int, message: dict[str, Any]) -> None:
        # 5. conversation kind.
        try:
            conversation = await self.client.get_conversation(conv)
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_conversation failed conversation=%s: %s", conv, exc)
            conversation = {}
        kind = conversation.get("kind", ConversationKind.CHAT)
        is_channel = kind == ConversationKind.CHANNEL

        sender_id = message.get("sender_id", "")
        sender = await self._resolve_sender(sender_id)
        sender_email = sender.get("email") or None
        sender_name = sender.get("display_name") or None

        was_mentioned = False
        # 6. access.
        if is_channel:
            ga = access.check_group_access(
                group_policy=self.account.group_policy,
                group_allow_from=self.account.group_allow_from,
                group_sender_allow_from=self.account.group_sender_allow_from,
                conversation_id=conv, sender_id=sender_id, sender_email=sender_email,
            )
            if not ga.allowed:
                logger.info("channel drop conversation=%s reason=%s", conv, ga.reason)
                return
            was_mentioned = self.client.is_self_mentioned(message)
            if self.account.group_require_mention and not was_mentioned:
                return  # not addressed to the bot
        else:
            decision = access.check_dm_access(
                dm_policy=self.account.dm_policy, allow_from=self.account.allow_from,
                sender_id=sender_id, sender_email=sender_email,
            )
            if decision == access.DM_DENY:
                logger.info("dm drop conversation=%s sender=%s", conv, sender_id)
                return
            # DM_ALLOW / DM_PAIRING both forward; pairing is resolved by the gateway.

        # 7. content + media.
        text = blocks.extract_text(message)
        media_urls: list[str] = []
        media_types: list[str] = []
        placeholders: list[str] = []
        media = blocks.media_blocks(message)
        if media:
            for item in await resolve_inbound_media(self.client, conv, message.get("id", ""), media):
                media_urls.append(item.path)
                media_types.append(item.content_type)
                placeholders.append(item.placeholder())
        if not text and placeholders:
            text = " ".join(placeholders)

        # 8. fork / mention context preamble.
        preamble = await self._context_preamble(conv, seq, conversation, is_channel, was_mentioned)
        if preamble:
            text = f"{preamble}\n\n{text}" if text else preamble

        # 9. build + dispatch.
        source = self.adapter.build_source(
            chat_id=conv,
            chat_name=conversation.get("title") or None,
            chat_type="group" if is_channel else "dm",
            user_id=sender_email or sender_id,
            user_name=sender_name,
            user_id_alt=sender_id,
            message_id=message.get("id"),
        )
        raw = dict(message)
        if is_channel:
            raw["telex_was_mentioned"] = was_mentioned
        raw["telex_account_id"] = self.account.account_id
        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=raw,
            message_id=message.get("id"),
            media_urls=media_urls,
            media_types=media_types,
        )
        self.client.note_turn_seq(conv, seq)
        result = self.adapter.handle_message(event)
        if asyncio.iscoroutine(result):
            await result

    async def _resolve_sender(self, sender_id: str) -> dict[str, Any]:
        if not sender_id:
            return {}
        try:
            return (await self.client.resolve_identity(sender_id)) or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("identity resolve failed sender=%s: %s", sender_id, exc)
            return {}

    async def _context_preamble(
        self, conv: str, seq: int, conversation: dict[str, Any], is_channel: bool, was_mentioned: bool,
    ) -> str | None:
        """Fork-history (first turn) and mention-gap history, as a text preamble."""
        history: list[dict[str, Any]] = []
        # Fork inheritance: on first turn in a forked conversation, pull the
        # pre-fork copied history (FORK_PREFIX) as context.
        if conversation.get("fork_of_conversation_id") and conv not in self._forked_seen:
            self._forked_seen.add(conv)
            try:
                msgs = await self.client.list_messages(conv, limit=_MISSED_LIMIT)
                history.extend(m for m in msgs if (m.get("flags", 0) & MessageFlag.FORK_PREFIX))
            except Exception as exc:  # noqa: BLE001
                logger.warning("fork history fetch failed conversation=%s: %s", conv, exc)
        # Mention gap: when a mention lands after a gap, include the skipped msgs.
        if is_channel and was_mentioned:
            last = self.client.get_last_turn_seq(conv)
            if last is not None and seq - last > 1:
                try:
                    gap = await self.client.list_messages(
                        conv, after_seq=last, before_seq=seq, limit=_MISSED_LIMIT
                    )
                    history.extend(gap)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("mention gap fetch failed conversation=%s: %s", conv, exc)
        if not history:
            return None
        history = sorted(history, key=lambda m: m.get("seq", 0))
        lines = []
        sender_ids = list({m.get("sender_id", "") for m in history if m.get("sender_id")})
        try:
            idmap = await self.client.resolve_identities(sender_ids)
        except Exception:  # noqa: BLE001
            idmap = {}
        for m in history:
            body = blocks.extract_text(m).strip()
            if not body:
                continue
            who = idmap.get(m.get("sender_id", ""), {}).get("display_name") or "user"
            lines.append(f"{who}: {body}")
        if not lines:
            return None
        return "[context]\n" + "\n".join(lines)

    async def _handle_system_event(self, conv: str, message: dict[str, Any]) -> None:
        """Refresh cached conversation state on lifecycle events (no reply)."""
        try:
            block = blocks._sorted_blocks(message)
            event = (block[0].get("event") if block else None) or {}
            kind = event.get("kind")
            if kind in {"renamed", "member_added", "member_removed", "member_joined", "deleted", "created"}:
                # Force-refresh title/membership on next access.
                await self.client.get_conversation(conv, force_refresh=True)
            logger.debug("system event conversation=%s kind=%s", conv, kind)
        except Exception as exc:  # noqa: BLE001
            logger.debug("system event handling failed conversation=%s: %s", conv, exc)
