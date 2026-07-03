"""Telex OpenAPI HTTP client (async, aiohttp).

Faithful port of openclaw-telex/src/client.ts: identities/conversations/
messages/files endpoints, LRU+TTL caches, self-echo suppression, per-conversation
backfill watermark, dedup, and the NDJSON subscribe stream reader.

All requests carry the ``X-API-Key`` header. The API key is never logged.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable

import aiohttp

from .log import get_logger, mask_key
from .types import OPENAPI_PREFIX, MessageStatus

logger = get_logger("client")

HTTP_TIMEOUT_S = 15
FILE_TIMEOUT_S = 60
SENT_IDS_MAX = 2000
IDENTITY_CACHE_MAX = 2000
CONVERSATION_CACHE_MAX = 2000
CACHE_TTL_S = 10 * 60


class TelexError(Exception):
    """A Telex API error carrying the HTTP status and stable gRPC code/message."""

    def __init__(self, message: str, *, http_status: int | None = None, code: int | None = None):
        super().__init__(message)
        self.http_status = http_status
        self.code = code
        self.telex_message = message


def _lru_set(d: OrderedDict, key: Any, value: Any, max_size: int) -> None:
    if key in d:
        d.move_to_end(key)
    d[key] = value
    while len(d) > max_size:
        d.popitem(last=False)


class _TTLCache:
    """LRU cache with per-entry TTL (mirrors client.ts readCache/writeCache)."""

    def __init__(self, max_size: int, ttl_s: float):
        self._d: OrderedDict[str, tuple[Any, float]] = OrderedDict()
        self._max = max_size
        self._ttl = ttl_s

    def get(self, key: str) -> Any | None:
        entry = self._d.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if expires_at <= time.monotonic():
            self._d.pop(key, None)
            return None
        self._d.move_to_end(key)
        return value

    def set(self, key: str, value: Any) -> None:
        _lru_set(self._d, key, (value, time.monotonic() + self._ttl), self._max)


class TelexClient:
    def __init__(self, api_key: str, base_url: str, bot_id: str | None = None):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        # Echo suppression: subscribe fans the bot's own sends back to it.
        self._self_id: str | None = (bot_id or "").strip() or None
        self._sent_message_ids: OrderedDict[str, bool] = OrderedDict()
        # Per-conversation backfill watermark: {settled, pending:set}.
        self._cursors: dict[str, dict[str, Any]] = {}
        # Highest seq that entered an agent turn (mention-gap backfill).
        self._last_turn_seq: dict[str, int] = {}
        # Dedup of dispatched message ids (at-least-once delivery).
        self._processed_ids: OrderedDict[str, bool] = OrderedDict()
        self._conversation_cache = _TTLCache(CONVERSATION_CACHE_MAX, CACHE_TTL_S)
        self._identity_cache = _TTLCache(IDENTITY_CACHE_MAX, CACHE_TTL_S)
        self._session: aiohttp.ClientSession | None = None
        self._session_loop: asyncio.AbstractEventLoop | None = None

    # -- session lifecycle --------------------------------------------------

    def _get_session(self) -> aiohttp.ClientSession:
        """Return a ClientSession bound to the CURRENT running loop.

        The tool registry runs async handlers in a fresh event loop when called
        from sync contexts; an aiohttp session cannot be reused across loops
        ("Timeout context manager should be used inside a task"). Recreate the
        session whenever the running loop changed, best-effort closing the old
        one on its own loop.
        """
        loop = asyncio.get_event_loop()
        if (
            self._session is None
            or self._session.closed
            or self._session_loop is not loop
        ):
            old, old_loop = self._session, self._session_loop
            if old is not None and not old.closed and old_loop is not None and not old_loop.is_closed():
                try:
                    old_loop.call_soon_threadsafe(lambda: old_loop.create_task(old.close()))
                except RuntimeError:
                    pass  # old loop already gone; session is GC'd
            self._session = aiohttp.ClientSession()
            self._session_loop = loop
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    # -- core HTTP ----------------------------------------------------------

    async def _api_call(self, method: str, path: str, body: Any | None = None) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        headers = {"x-api-key": self.api_key, "Content-Type": "application/json"}
        timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_S)
        session = self._get_session()
        async with session.request(
            method, url, headers=headers,
            data=json.dumps(body) if body is not None else None,
            timeout=timeout,
        ) as res:
            text = await res.text()
            data = json.loads(text) if text else {}
            if res.status >= 400:
                code = data.get("code") if isinstance(data, dict) else None
                message = (data.get("message") if isinstance(data, dict) else None) or f"HTTP {res.status}"
                raise TelexError(f"Telex API error: {message}", http_status=res.status, code=code)
            return data if isinstance(data, dict) else {}

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        qs = ""
        if params:
            pairs = [
                (k, str(v)) for k, v in params.items()
                if v is not None and v != ""
            ]
            if pairs:
                from urllib.parse import urlencode
                qs = "?" + urlencode(pairs)
        return await self._api_call("GET", f"{OPENAPI_PREFIX}{path}{qs}")

    async def _post(self, path: str, body: Any) -> dict[str, Any]:
        return await self._api_call("POST", f"{OPENAPI_PREFIX}{path}", body)

    # -- messages -----------------------------------------------------------

    async def send_message(
        self,
        *,
        conversation_id: str | None = None,
        peer_id: str | None = None,
        message_id: str | None = None,
        blocks: list[dict[str, Any]],
        mention_ids: list[str] | None = None,
        status: int | None = None,
    ) -> dict[str, Any]:
        data: dict[str, Any] = {"blocks": blocks}
        if mention_ids:
            data["mention_ids"] = mention_ids
        body: dict[str, Any] = {"data": data}
        if conversation_id:
            body["conversation_id"] = conversation_id
        if peer_id:
            body["peer_id"] = peer_id
        if message_id:
            body["message_id"] = message_id
        if status is not None:
            body["status"] = status
        res = await self._post("/send-message", body)
        message = res.get("message", {})
        self.record_sent(message)
        return message

    async def set_activity(self, conversation_id: str, status: str) -> None:
        await self._post("/set-activity", {"conversation_id": conversation_id, "status": status})

    async def list_messages(
        self,
        conversation_id: str,
        *,
        before_seq: int | None = None,
        after_seq: int | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        res = await self._get("/list-messages", {
            "conversation_id": conversation_id,
            "before_seq": before_seq,
            "after_seq": after_seq,
            "limit": limit,
        })
        return res.get("messages") or []

    # -- files --------------------------------------------------------------

    async def upload_file(self, name: str, mime: str, data: bytes) -> dict[str, Any]:
        url = f"{self.base_url}{OPENAPI_PREFIX}/upload-file"
        form = aiohttp.FormData()
        form.add_field("file", data, filename=name, content_type=mime or "application/octet-stream")
        timeout = aiohttp.ClientTimeout(total=FILE_TIMEOUT_S)
        session = self._get_session()
        async with session.post(url, headers={"x-api-key": self.api_key}, data=form, timeout=timeout) as res:
            text = await res.text()
            body = json.loads(text) if text else {}
            if res.status >= 400:
                message = (body.get("message") if isinstance(body, dict) else None) or f"HTTP {res.status}"
                raise TelexError(f"Telex upload error: {message}", http_status=res.status)
            return body if isinstance(body, dict) else {}

    async def download_file(
        self, file_id: str, *, conversation_id: str | None = None, message_id: str | None = None,
    ) -> tuple[bytes, str]:
        from urllib.parse import urlencode
        params = {"file_id": file_id}
        if conversation_id:
            params["conversation_id"] = conversation_id
        if message_id:
            params["message_id"] = message_id
        url = f"{self.base_url}{OPENAPI_PREFIX}/download-file?{urlencode(params)}"
        timeout = aiohttp.ClientTimeout(total=FILE_TIMEOUT_S)
        session = self._get_session()
        async with session.get(url, headers={"x-api-key": self.api_key}, timeout=timeout) as res:
            if res.status >= 400:
                detail = await res.text()
                raise TelexError(f"Telex download error: HTTP {res.status} {detail}", http_status=res.status)
            content_type = res.headers.get("content-type", "application/octet-stream")
            buf = await res.read()
            return buf, content_type

    # -- conversations / identities / members -------------------------------

    async def get_conversation(self, conversation_id: str, force_refresh: bool = False) -> dict[str, Any]:
        if not force_refresh:
            cached = self._conversation_cache.get(conversation_id)
            if cached is not None:
                return cached
        res = await self._get("/get-conversation", {"conversation_id": conversation_id})
        conversation = res.get("conversation", {})
        self._conversation_cache.set(conversation_id, conversation)
        return conversation

    async def list_conversations(
        self, *, kind: int | None = None, offset: int | None = None, limit: int | None = None,
    ) -> dict[str, Any]:
        res = await self._get("/list-conversations", {"kind": kind, "offset": offset, "limit": limit})
        return {"conversations": res.get("conversations") or [], "total": res.get("total") or 0}

    async def list_members(self, conversation_id: str) -> list[dict[str, Any]]:
        res = await self._get("/list-members", {"conversation_id": conversation_id})
        return res.get("members") or []

    async def search_identities(self, query: str, limit: int | None = None) -> list[dict[str, Any]]:
        res = await self._get("/search-identities", {"query": query, "limit": limit})
        return res.get("identities") or []

    async def get_identities(self, ids: list[str], emails: list[str]) -> list[dict[str, Any]]:
        res = await self._post("/batch-get-identities", {"ids": ids, "emails": emails})
        identities = res.get("identities") or []
        for identity in identities:
            if identity.get("id"):
                self._identity_cache.set(identity["id"], identity)
        return identities

    async def resolve_identities(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        missing: list[str] = []
        for ident_id in ids:
            hit = self._identity_cache.get(ident_id)
            if hit is not None:
                out[ident_id] = hit
            else:
                missing.append(ident_id)
        if missing:
            for identity in await self.get_identities(missing, []):
                out[identity["id"]] = identity
        return out

    async def resolve_identity(self, ident_id: str) -> dict[str, Any] | None:
        return (await self.resolve_identities([ident_id])).get(ident_id)

    # -- subscribe ----------------------------------------------------------

    async def subscribe(
        self,
        on_event: Callable[[dict[str, Any]], Awaitable[None] | None],
        *,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        """Consume the NDJSON subscribe stream, calling on_event per frame.

        Each line is grpc-gateway wrapped: ``{"result": <event>}`` for a frame
        (message or null heartbeat) or ``{"error": {...}}`` which ends the stream
        (raised as TelexError). Returns when the stream closes.
        """
        url = f"{self.base_url}{OPENAPI_PREFIX}/subscribe"
        headers = {"x-api-key": self.api_key, "Accept": "application/json"}
        # No total timeout: this is a long-lived stream. sock_read guards silence,
        # but the caller's stale timer is the authoritative half-open detector.
        timeout = aiohttp.ClientTimeout(total=None, sock_read=None)
        session = self._get_session()
        async with session.get(url, headers=headers, timeout=timeout) as res:
            if res.status != 200:
                detail = await res.text()
                raise TelexError(f"Telex subscribe failed: HTTP {res.status} {detail}", http_status=res.status)
            buffer = ""
            async for chunk in res.content.iter_any():
                if stop_event is not None and stop_event.is_set():
                    return
                buffer += chunk.decode("utf-8", errors="replace")
                while True:
                    nl = buffer.find("\n")
                    if nl < 0:
                        break
                    line = buffer[:nl].strip()
                    buffer = buffer[nl + 1:]
                    if not line:
                        continue
                    try:
                        parsed = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(parsed, dict) and parsed.get("error"):
                        err = parsed["error"]
                        msg = err.get("message", "unknown") if isinstance(err, dict) else "unknown"
                        raise TelexError(f"Telex subscribe stream error: {msg}")
                    if isinstance(parsed, dict) and "result" in parsed:
                        result = on_event(parsed["result"])
                        if asyncio.iscoroutine(result):
                            await result

    # -- self-echo / watermark / dedup -------------------------------------

    def record_sent(self, message: dict[str, Any]) -> None:
        if message.get("sender_id"):
            self._self_id = message["sender_id"]
        if message.get("id"):
            _lru_set(self._sent_message_ids, message["id"], True, SENT_IDS_MAX)

    def arm_self_id(self, bot_id: str | None) -> None:
        if self._self_id:
            return
        trimmed = (bot_id or "").strip()
        if trimmed:
            self._self_id = trimmed

    @property
    def self_id(self) -> str | None:
        return self._self_id

    def is_own_message(self, message: dict[str, Any]) -> bool:
        if self._self_id and message.get("sender_id") == self._self_id:
            return True
        return message.get("id") in self._sent_message_ids

    def is_self_mentioned(self, message: dict[str, Any]) -> bool:
        data = message.get("data") or {}
        if data.get("mention_all"):
            return True
        if not self._self_id:
            return False
        return self._self_id in (data.get("mention_ids") or [])

    def note_message(self, conversation_id: str, seq: int, terminal: bool) -> None:
        entry = self._cursors.get(conversation_id)
        if entry is None:
            entry = {"settled": seq - 1, "pending": set()}
            self._cursors[conversation_id] = entry
        if terminal:
            if seq > entry["settled"]:
                entry["settled"] = seq
            entry["pending"].discard(seq)
        else:
            entry["pending"].add(seq)

    def get_backfill_targets(self) -> list[dict[str, Any]]:
        out = []
        for conversation_id, entry in self._cursors.items():
            after_seq = entry["settled"]
            for seq in entry["pending"]:
                after_seq = min(after_seq, seq - 1)
            out.append({"conversation_id": conversation_id, "after_seq": after_seq})
        return out

    def note_turn_seq(self, conversation_id: str, seq: int) -> None:
        if seq > self._last_turn_seq.get(conversation_id, 0):
            self._last_turn_seq[conversation_id] = seq

    def get_last_turn_seq(self, conversation_id: str) -> int | None:
        return self._last_turn_seq.get(conversation_id)

    def mark_processed(self, message_id: str) -> bool:
        if message_id in self._processed_ids:
            return False
        _lru_set(self._processed_ids, message_id, True, SENT_IDS_MAX)
        return True


_client_cache: dict[str, TelexClient] = {}


def get_telex_client(api_key: str, base_url: str, bot_id: str | None = None) -> TelexClient:
    key = f"{base_url}:{api_key}"
    client = _client_cache.get(key)
    if client is None:
        client = TelexClient(api_key, base_url, bot_id)
        _client_cache[key] = client
        logger.debug("created Telex client base_url=%s key=%s", base_url, mask_key(api_key))
    elif bot_id:
        client.arm_self_id(bot_id)
    return client


def resolve_telex_client(api_key: str | None, base_url: str | None, bot_id: str | None = None) -> TelexClient | None:
    if not api_key or not base_url:
        return None
    return get_telex_client(api_key, base_url, bot_id)
