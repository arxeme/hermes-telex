"""Shared test fakes for offline unit tests (no network, no live hermes)."""

from __future__ import annotations

from typing import Any

import pytest

from hermes_telex.client import TelexClient

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@pytest.fixture(autouse=True)
def _register_telex_platform():
    """Make ``Platform("telex")`` resolvable when a real hermes is importable.

    ``TelexAdapter.__init__`` calls ``Platform(TELEX_PLATFORM)``, and hermes only
    mints that pseudo-member once the platform is in its registry. Production
    always registers first (``platform_registry.create_adapter`` runs the
    factory after ``register``), so a test that constructs an adapter directly
    has to do the same or it fails on an enum lookup that can never happen at
    runtime. Without hermes on the path the plugin's fallback ``Platform`` stub
    accepts anything and this is a no-op.
    """
    try:
        from gateway.platform_registry import platform_registry
    except Exception:  # pragma: no cover - offline runs use the plugin's stub
        yield
        return

    if not platform_registry.is_registered("telex"):
        from hermes_telex import adapter as _adp

        class _RegOnlyCtx:
            def register_platform(self, **kwargs):
                from gateway.platform_registry import PlatformEntry

                platform_registry.register(PlatformEntry(**kwargs))

            def register_tool(self, **kwargs):
                pass

        _adp.register(_RegOnlyCtx())
    yield


class StubClient(TelexClient):
    """Real TelexClient (keeps watermark/dedup/echo logic) with network methods
    replaced by canned data. Set the ``*_data`` attributes per test."""

    def __init__(self, api_key="k", base_url="https://t", bot_id=None):
        super().__init__(api_key, base_url, bot_id)
        self.conversations: dict[str, dict] = {}
        self.identities: dict[str, dict] = {}
        self.messages_by_conv: dict[str, list[dict]] = {}
        self.sent: list[dict] = []
        self.activities: list[tuple[str, str]] = []
        self.uploaded: list[tuple[str, str]] = []
        self.downloads: dict[str, tuple[bytes, str]] = {}
        self.posts: list[tuple[str, dict]] = []
        self.post_replies: dict[str, dict] = {}
        self.created_chats: list[dict] = []

    async def _post(self, path: str, body: dict):
        # Only the low-level paths not stubbed above reach here (e.g. /mark-read
        # from sync_read_cursor), so the real watermark logic stays under test.
        self.posts.append((path, body))
        if path.endswith("/mark-read"):
            return {"read_seq": body.get("read_seq")}
        return self.post_replies.get(path, {})

    async def get_conversation(self, conversation_id: str, force_refresh: bool = False) -> dict:
        return self.conversations.get(conversation_id, {"id": conversation_id, "kind": 0})

    async def list_messages(self, conversation_id, *, before_seq=None, after_seq=None, limit=None):
        msgs = self.messages_by_conv.get(conversation_id, [])
        out = []
        for m in msgs:
            seq = m.get("seq", 0)
            if after_seq is not None and seq <= after_seq:
                continue
            if before_seq is not None and seq >= before_seq:
                continue
            out.append(m)
        return out[: (limit or len(out))]

    async def get_identities(self, ids, emails):
        out = []
        for i in ids:
            if i in self.identities:
                out.append(self.identities[i])
        for e in emails:
            for ident in self.identities.values():
                if ident.get("email", "").lower() == e.lower():
                    out.append(ident)
        return out

    async def resolve_identity(self, ident_id):
        return self.identities.get(ident_id)

    async def resolve_identities(self, ids):
        return {i: self.identities[i] for i in ids if i in self.identities}

    async def send_message(self, *, conversation_id=None, peer_id=None, message_id=None,
                           blocks, mention_ids=None, status=None):
        msg = {"id": f"m{len(self.sent)}", "sender_id": self.self_id or "bot",
               "conversation_id": conversation_id or peer_id, "blocks": blocks, "status": status or 0}
        self.sent.append({"conversation_id": conversation_id, "peer_id": peer_id,
                          "message_id": message_id, "blocks": blocks, "status": status})
        self.record_sent(msg)
        return msg

    async def set_activity(self, conversation_id, status):
        self.activities.append((conversation_id, status))

    async def create_chat(self, peer_id, blocks, title=None):
        self.created_chats.append({"peer_id": peer_id, "blocks": blocks, "title": title})
        return {"id": "newchat", "kind": 0, "title": title or "", "peer_id": peer_id}

    async def upload_file(self, name, mime, data):
        self.uploaded.append((name, mime))
        return {"file_id": f"f{len(self.uploaded)}", "name": name, "size": len(data), "mime": mime}

    async def download_file(self, file_id, *, conversation_id=None, message_id=None):
        # Real PNG magic bytes: hermes validates that image content matches the
        # extension it is asked to cache as, so a placeholder like b"bytes" is
        # refused and the caller degrades to a plain link instead of caching.
        return self.downloads.get(file_id, (_PNG_MAGIC + b"stub", "image/png"))

    async def close(self):
        pass


class FakeAdapter:
    """Minimal adapter surface the dispatcher needs."""

    def __init__(self):
        self.events: list[Any] = []

    def build_source(self, **kwargs):
        return kwargs

    async def handle_message(self, event):
        self.events.append(event)
