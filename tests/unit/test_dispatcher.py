"""T-05/T-13 dispatcher: gates, access, mention, content, dedup."""

from hermes_telex import accounts
from hermes_telex.dispatcher import TelexDispatcher
from hermes_telex.types import BlockType, MessageFlag, MessageStatus
from tests.conftest import FakeAdapter, StubClient


def _setup(extra_overrides=None, *, channel=False):
    extra = {"api_key": "k", "bot_id": "bot1"}
    extra.update(extra_overrides or {})
    account = accounts.resolve_account(extra, "default")
    client = StubClient(bot_id="bot1")
    client.identities["u1"] = {"id": "u1", "email": "a@b.com", "display_name": "Alice"}
    client.conversations["c1"] = {"id": "c1", "kind": 1 if channel else 0, "title": "Room"}
    adapter = FakeAdapter()
    disp = TelexDispatcher(adapter, account, client)
    return disp, adapter, client


def _msg(seq=1, sender="u1", text="hi", status=MessageStatus.COMPLETED, flags=0,
         mention_ids=None, mention_all=False, conv="c1", mid=None, media=None):
    blocks = [{"seq": 0, "type": BlockType.TEXT, "text": text}] if text else []
    if media:
        blocks.append({"seq": 1, "type": BlockType.IMAGE, "media": media})
    data = {"blocks": blocks}
    if mention_ids:
        data["mention_ids"] = mention_ids
    if mention_all:
        data["mention_all"] = True
    return {"id": mid or f"m{seq}", "conversation_id": conv, "seq": seq,
            "sender_id": sender, "status": status, "flags": flags, "data": data}


async def test_self_echo_dropped():
    disp, adapter, _ = _setup({"dm_policy": "open", "allow_from": ["*"]})
    await disp.handle(_msg(sender="bot1"))
    assert adapter.events == []


async def test_in_progress_and_flags_dropped():
    disp, adapter, _ = _setup({"dm_policy": "open", "allow_from": ["*"]})
    await disp.handle(_msg(seq=1, status=MessageStatus.IN_PROGRESS, mid="a"))
    await disp.handle(_msg(seq=2, flags=MessageFlag.EVENT, mid="b"))
    await disp.handle(_msg(seq=3, flags=MessageFlag.FORK_PREFIX, mid="c"))
    assert adapter.events == []


async def test_dedup():
    disp, adapter, _ = _setup({"dm_policy": "open", "allow_from": ["*"]})
    await disp.handle(_msg(mid="dup"))
    await disp.handle(_msg(mid="dup"))
    assert len(adapter.events) == 1


async def test_dm_allowlist():
    disp, adapter, _ = _setup({"dm_policy": "allowlist", "allow_from": ["a@b.com"]})
    await disp.handle(_msg())
    assert len(adapter.events) == 1
    disp2, adapter2, _ = _setup({"dm_policy": "allowlist", "allow_from": ["other@x.com"]})
    await disp2.handle(_msg())
    assert adapter2.events == []


async def test_dm_pairing_forwards():
    # pairing: unknown sender is still forwarded (gateway runs the handshake)
    disp, adapter, _ = _setup({"dm_policy": "pairing", "allow_from": []})
    await disp.handle(_msg())
    assert len(adapter.events) == 1


async def test_channel_policies_and_mention():
    # disabled -> drop
    disp, adapter, _ = _setup({"group_policy": "disabled"}, channel=True)
    await disp.handle(_msg(conv="c1"))
    assert adapter.events == []
    # open + require_mention default true -> drop unmentioned
    disp, adapter, _ = _setup({"group_policy": "open"}, channel=True)
    await disp.handle(_msg(conv="c1", mid="x"))
    assert adapter.events == []
    # mentioned -> dispatched with was_mentioned flag
    disp, adapter, _ = _setup({"group_policy": "open"}, channel=True)
    await disp.handle(_msg(conv="c1", mid="y", mention_ids=["bot1"]))
    assert len(adapter.events) == 1
    assert adapter.events[0].raw_message["telex_was_mentioned"] is True


async def test_channel_allowlist():
    disp, adapter, _ = _setup(
        {"group_policy": "allowlist", "group_allow_from": ["cX"], "group_require_mention": False},
        channel=True,
    )
    await disp.handle(_msg(conv="c1"))   # c1 not in allowlist
    assert adapter.events == []


async def test_content_and_source():
    disp, adapter, _ = _setup({"dm_policy": "open", "allow_from": ["*"]})
    await disp.handle(_msg(text="hello world"))
    ev = adapter.events[0]
    assert ev.text == "hello world"
    assert ev.source["chat_type"] == "dm"
    assert ev.source["chat_id"] == "c1"
    assert ev.source["user_id"] == "a@b.com"       # resolved email
    assert ev.source["user_id_alt"] == "u1"


async def test_inbound_media_downloaded():
    disp, adapter, client = _setup({"dm_policy": "open", "allow_from": ["*"]})
    await disp.handle(_msg(text="", media={"file_id": "f1", "name": "p.png"}))
    ev = adapter.events[0]
    assert ev.media_urls and ev.media_types
    assert "[image: p.png]" in ev.text   # placeholder used when text empty


async def test_telex_tool_handler_accepts_registry_kwargs():
    # Regression: tools.registry calls handler(args, **kwargs) e.g. task_id.
    from hermes_telex import tools
    # unknown action returns JSON error, but must NOT raise on the extra kwarg
    out = await tools.telex_tool_handler({"action": "nope"}, task_id="t1", session_key="s1")
    assert '"error"' in out
