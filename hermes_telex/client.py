"""Telex OpenAPI HTTP client (async, aiohttp).

Faithful port of openclaw-telex/src/client.ts: identities/conversations/
messages/files endpoints, LRU+TTL caches, self-echo suppression, the
per-conversation message-sync state (cursor / settled / poison), and the NDJSON
subscribe stream reader.

All requests carry the ``X-API-Key`` header. The API key is never logged.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable

import aiohttp

from .log import get_logger, mask_key
from .types import OPENAPI_PREFIX

logger = get_logger("client")

HTTP_TIMEOUT_S = 15
FILE_TIMEOUT_S = 60
SENT_IDS_MAX = 2000
IDENTITY_CACHE_MAX = 2000
CONVERSATION_CACHE_MAX = 2000
CACHE_TTL_S = 10 * 60
MARK_READ_DEBOUNCE_S = 3.0


class TelexError(Exception):
    """A Telex API error carrying the HTTP status and stable gRPC code/message."""

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        code: int | None = None,
        api_message: str | None = None,
    ):
        super().__init__(message)
        self.http_status = http_status
        self.code = code
        self.telex_message = message
        # The stable body identifier (e.g. not_a_member, insufficient_scope):
        # 403/404 alone cannot distinguish configuration errors from lost
        # memberships, so callers branch on this.
        self.api_message = api_message


# In-stream error frames carry only the stable body code (no HTTP status), so
# classification must not require a status match.
def conversation_gone(exc: Exception) -> bool:
    if not isinstance(exc, TelexError):
        return False
    return exc.api_message in ("not_a_member", "conversation_not_found")


def auth_error(exc: Exception) -> bool:
    if not isinstance(exc, TelexError):
        return False
    return exc.http_status == 401 or exc.api_message in (
        "insufficient_scope", "invalid_api_key", "api_key_empty",
    )


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
        # Per-conversation message-sync state: cursor = local cache of the server
        # read_seq (everything at or below it is settled), settled = the disposed
        # seqs above it, poison = handle failure counts.
        self._sync: dict[str, dict[str, Any]] = {}
        self._read_sync_tasks: dict[str, asyncio.Task] = {}
        # Highest seq that entered an agent turn (mention-gap backfill).
        self._last_turn_seq: dict[str, int] = {}
        self._conversation_cache = _TTLCache(CONVERSATION_CACHE_MAX, CACHE_TTL_S)
        self._identity_cache = _TTLCache(IDENTITY_CACHE_MAX, CACHE_TTL_S)
        # One aiohttp session per event loop: hermes may drive the plugin from
        # several loops concurrently, and a session is bound to its creating loop.
        # Those loops live on different threads, so the registry needs a lock.
        self._sessions: dict[asyncio.AbstractEventLoop, aiohttp.ClientSession] = {}
        self._sessions_lock = threading.Lock()

    # -- session lifecycle --------------------------------------------------

    def _get_session(self) -> aiohttp.ClientSession:
        """Return a ClientSession bound to the current running loop.

        An aiohttp session is tied to the loop it was created on, and hermes may
        call the plugin from more than one loop concurrently (the gateway monitor
        loop plus tool-execution loops). Keep one session per loop so concurrent
        loops never close each other's connections; drop sessions whose loop has
        closed so one-shot loops don't leak.
        """
        loop = asyncio.get_running_loop()
        with self._sessions_lock:
            for dead in [lp for lp in self._sessions if lp.is_closed()]:
                self._sessions.pop(dead, None)
            session = self._sessions.get(loop)
            if session is None or session.closed:
                session = aiohttp.ClientSession()
                self._sessions[loop] = session
            return session

    async def close(self) -> None:
        for task in self._read_sync_tasks.values():
            task.cancel()
        self._read_sync_tasks.clear()
        with self._sessions_lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            if not session.closed:
                try:
                    await session.close()
                except RuntimeError:
                    pass  # session bound to another/closed loop; drop it

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
                api_message = data.get("message") if isinstance(data, dict) else None
                message = api_message or f"HTTP {res.status}"
                raise TelexError(
                    f"Telex API error: {message}",
                    http_status=res.status,
                    code=code,
                    api_message=api_message,
                )
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
        status: int | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"data": {"blocks": blocks}}
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

    # Marking: push the watermark (the contiguous settled prefix from the cursor)
    # to the server, then adopt the effective cursor it returns.
    async def sync_read_cursor(self, conversation_id: str) -> None:
        state = self._sync.get(conversation_id)
        if state is None:
            return
        watermark = state["cursor"]
        while watermark + 1 in state["settled"]:
            watermark += 1
        if watermark <= state["cursor"]:
            return
        res = await self._post("/mark-read", {"conversation_id": conversation_id, "read_seq": watermark})
        self.update_cursor(conversation_id, res.get("read_seq") or watermark)

    # Keep-first debounce: an armed timer is never reset, so marks land every
    # ~3s under continuous traffic. Failures wait for the next settle or repair.
    def schedule_read_sync(self, conversation_id: str) -> None:
        existing = self._read_sync_tasks.get(conversation_id)
        if existing and not existing.done():
            return

        async def run() -> None:
            try:
                await asyncio.sleep(MARK_READ_DEBOUNCE_S)
                await self.sync_read_cursor(conversation_id)
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001
                if conversation_gone(exc):
                    self.drop_conversation(conversation_id)
                elif auth_error(exc):
                    logger.error(
                        "read-cursor sync rejected: check key scopes conversation=%s: %s", conversation_id, exc
                    )
                else:
                    logger.warning("read-cursor sync failed conversation=%s: %s", conversation_id, exc)

        self._read_sync_tasks[conversation_id] = asyncio.create_task(run())

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
                api_message = body.get("message") if isinstance(body, dict) else None
                raise TelexError(
                    f"Telex upload error: {api_message or f'HTTP {res.status}'}",
                    http_status=res.status,
                    code=body.get("code") if isinstance(body, dict) else None,
                    api_message=api_message,
                )
            return body if isinstance(body, dict) else {}

    def file_download_url(self, file_id: str) -> str:
        from urllib.parse import quote
        return f"{self.base_url}{OPENAPI_PREFIX}/download-file?file_id={quote(file_id)}"

    async def download_file(self, file_id: str) -> tuple[bytes, str]:
        timeout = aiohttp.ClientTimeout(total=FILE_TIMEOUT_S)
        session = self._get_session()
        async with session.get(self.file_download_url(file_id), timeout=timeout) as res:
            if res.status >= 400:
                detail = await res.text()
                api_message = None
                try:
                    parsed = json.loads(detail)
                    api_message = parsed.get("message") if isinstance(parsed, dict) else None
                except ValueError:
                    pass  # non-JSON error body; classification falls back to the status
                raise TelexError(
                    f"Telex download error: HTTP {res.status} {detail}",
                    http_status=res.status,
                    api_message=api_message,
                )
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
        self.arm_self_id((conversation.get("membership") or {}).get("identity_id"))
        # Unlike client.ts, no sync-state adoption here: hermes tool calls run
        # on other worker threads, and mutating the sync collections outside
        # the driver's conversation lock races the monitor loop.
        self._conversation_cache.set(conversation_id, conversation)
        return conversation

    async def list_conversations(
        self, *, kind: int | None = None, offset: int | None = None, limit: int | None = None,
    ) -> dict[str, Any]:
        res = await self._get("/list-conversations", {"kind": kind, "offset": offset, "limit": limit})
        conversations = res.get("conversations") or []
        for conversation in conversations:
            identity_id = (conversation.get("membership") or {}).get("identity_id")
            if identity_id:
                self.arm_self_id(identity_id)
                break
        return {"conversations": conversations, "total": res.get("total") or 0}

    async def create_channel(self, title: str, identity_ids: list[str]) -> dict[str, Any]:
        res = await self._post("/create-channel", {"title": title, "identity_ids": identity_ids})
        return res.get("conversation", {})

    async def list_members(self, conversation_id: str) -> list[dict[str, Any]]:
        res = await self._get("/list-members", {"conversation_id": conversation_id})
        return res.get("members") or []

    async def add_members(self, conversation_id: str, identity_ids: list[str]) -> list[dict[str, Any]]:
        res = await self._post("/add-members", {"conversation_id": conversation_id, "identity_ids": identity_ids})
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
                api_message: str | None = None
                try:
                    parsed_detail = json.loads(detail)
                    if isinstance(parsed_detail, dict):
                        api_message = parsed_detail.get("message")
                except json.JSONDecodeError:
                    pass
                raise TelexError(
                    f"Telex subscribe failed: HTTP {res.status} {detail}",
                    http_status=res.status,
                    api_message=api_message,
                )
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
                        code = err.get("code") if isinstance(err, dict) else None
                        raise TelexError(f"Telex subscribe stream error: {msg}", code=code, api_message=msg)
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

    # Without it, backfilled own messages dispatch as inbound and channel
    # mentions are settled as ineligible until the first send reveals the id.
    async def ensure_self_id(self) -> None:
        if self._self_id:
            return
        res = await self._get("/get-identity")
        identity = res.get("identity") or {}
        if identity.get("id"):
            self._self_id = identity["id"]

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

    def is_seeded(self, conversation_id: str) -> bool:
        return conversation_id in self._sync

    def seed_conversation(self, conversation_id: str, cursor: int, max_seen: int) -> None:
        state = self._sync.get(conversation_id)
        if state is None:
            state = {"cursor": 0, "settled": set(), "max_seen": 0, "poison": {}}
            self._sync[conversation_id] = state
        self.update_cursor(conversation_id, cursor)
        state["max_seen"] = max(state["max_seen"], max_seen)

    # The single entry point for cursor updates: advance, then prune both tables.
    def update_cursor(self, conversation_id: str, value: int) -> None:
        state = self._sync.get(conversation_id)
        if state is None or value <= state["cursor"]:
            return
        state["cursor"] = value
        state["settled"] = {seq for seq in state["settled"] if seq > value}
        state["poison"] = {seq: n for seq, n in state["poison"].items() if seq > value}

    def get_cursor(self, conversation_id: str) -> int:
        state = self._sync.get(conversation_id)
        return state["cursor"] if state else 0

    def is_disposed(self, conversation_id: str, seq: int) -> bool:
        state = self._sync.get(conversation_id)
        return state is None or seq <= state["cursor"] or seq in state["settled"]

    def settle(self, conversation_id: str, seq: int) -> None:
        state = self._sync.get(conversation_id)
        if state is None:
            return
        if seq > state["cursor"]:
            state["settled"].add(seq)
        state["max_seen"] = max(state["max_seen"], seq)

    def observe_seq(self, conversation_id: str, seq: int) -> None:
        state = self._sync.get(conversation_id)
        if state is not None:
            state["max_seen"] = max(state["max_seen"], seq)

    def is_lagging(self, conversation_id: str) -> bool:
        state = self._sync.get(conversation_id)
        return bool(state) and state["cursor"] < state["max_seen"]

    # Returns the new count; the caller logs the give-up exactly when it reaches N.
    def bump_poison(self, conversation_id: str, seq: int) -> int:
        state = self._sync.get(conversation_id)
        if state is None or seq <= state["cursor"]:
            return 0
        count = state["poison"].get(seq, 0) + 1
        state["poison"][seq] = count
        return count

    def poison_count(self, conversation_id: str, seq: int) -> int:
        state = self._sync.get(conversation_id)
        return state["poison"].get(seq, 0) if state else 0

    def known_conversations(self) -> list[str]:
        return list(self._sync)

    def drop_conversation(self, conversation_id: str) -> None:
        self._sync.pop(conversation_id, None)
        self._last_turn_seq.pop(conversation_id, None)
        task = self._read_sync_tasks.pop(conversation_id, None)
        if task and not task.done():
            task.cancel()

    def note_turn_seq(self, conversation_id: str, seq: int) -> None:
        if seq > self._last_turn_seq.get(conversation_id, 0):
            self._last_turn_seq[conversation_id] = seq

    def get_last_turn_seq(self, conversation_id: str) -> int | None:
        return self._last_turn_seq.get(conversation_id)


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
