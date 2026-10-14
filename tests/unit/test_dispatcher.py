"""T-05/T-13 dispatcher: gates, access, mention, content, quotes, dedup."""

import pytest

from hermes_telex import accounts
from hermes_telex.dispatcher import MessageType, TelexDispatcher
from hermes_telex.types import BlockType, InteractionMode, MessageFlag, MessageStatus
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


async def test_dispatcher_does_not_dedup():
    # Layering: dedup/settlement belong to the monitor (is_disposed/settle);
    # the dispatcher only decides eligibility and hands off. See
    # test_client_logic.test_dedup_by_settle for the dedup contract itself.
    disp, adapter, _ = _setup({"dm_policy": "open", "allow_from": ["*"]})
    await disp.handle(_msg(mid="dup"))
    await disp.handle(_msg(mid="dup"))
    assert len(adapter.events) == 2


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


async def test_channel_command_after_the_bots_own_mention():
    disp, adapter, _ = _setup({"group_policy": "open"}, channel=True)
    await disp.handle(_msg(conv="c1", mention_ids=["bot1"], text="[@Bot](mention:bot1) /reset"))
    assert adapter.events[0].text == "/reset"

    # Nothing but the mention: the strip would leave an empty event, so the text stays as sent.
    disp, adapter, _ = _setup({"group_policy": "open"}, channel=True)
    await disp.handle(_msg(conv="c1", mid="z", mention_ids=["bot1"], text="[@Bot](mention:bot1)"))
    assert adapter.events[0].text == "[@Bot](mention:bot1)"


async def test_context_skips_pending_events_and_own():
    disp, adapter, client = _setup({"group_policy": "open"}, channel=True)
    client.conversations["c1"]["fork_of_conversation_id"] = "c0"
    client.messages_by_conv["c1"] = [
        _msg(seq=1, text="inherited", flags=MessageFlag.FORK_PREFIX),
        _msg(seq=2, text="abandoned", flags=MessageFlag.FORK_PREFIX, status=MessageStatus.ABORTED),
        _msg(seq=4, text="missed"),
        _msg(seq=5, text="own-reply", sender="bot1"),
        _msg(seq=6, text="half-written", status=MessageStatus.IN_PROGRESS),
        _msg(seq=7, text="failed", status=MessageStatus.ERROR),
        _msg(seq=8, text="member-joined", flags=MessageFlag.EVENT),
    ]
    client.note_turn_seq("c1", 3)

    await disp.handle(_msg(seq=9, text="now", mention_ids=["bot1"]))

    text = adapter.events[0].text
    assert "inherited" in text and "missed" in text
    for dropped in ("abandoned", "own-reply", "half-written", "failed", "member-joined"):
        assert dropped not in text


async def test_interaction_ids_only_where_asked():
    disp, adapter, _ = _setup({"dm_policy": "open", "allow_from": ["*"]})
    message = _msg(text="Two questions")
    message["data"]["blocks"] += [
        {"seq": 1, "type": BlockType.INTERACTION, "interaction": {
            "id": "env", "mode": InteractionMode.SELECT, "answerer_ids": ["bot1"], "questions": [{
                "text": "Which env?",
                "options": [{"id": "stg", "label": "staging"}, {"id": "prd", "label": "production"}]}]}},
        {"seq": 2, "type": BlockType.INTERACTION, "interaction": {
            "id": "go", "mode": InteractionMode.BUTTON, "answerer_ids": ["u9"],
            "questions": [{"options": [{"id": "ok", "label": "Approve"}]}]}},
    ]

    await disp.handle(message)

    text = adapter.events[0].text
    assert text.startswith("Two questions\n")
    assert '<interaction message_id="m1" interaction_id="env" status="open">\n<question>\nWhich env?' in text
    assert "- stg: staging" in text
    assert '<interaction status="open">' in text
    assert "- Approve" in text and "- ok:" not in text


async def test_channel_interaction_asks_only_when_mentioned():
    disp, adapter, _ = _setup({"group_policy": "open", "group_require_mention": False}, channel=True)
    for seq, mention_ids in ((1, None), (2, ["bot1"])):
        message = _msg(seq=seq, text="Anyone?", mention_ids=mention_ids)
        message["data"]["blocks"].append({"seq": 1, "type": BlockType.INTERACTION, "interaction": {
            "id": "env", "mode": InteractionMode.SELECT, "questions": [{
                "text": "Which env?",
                "options": [{"id": "stg", "label": "staging"}, {"id": "prd", "label": "production"}]}]}})
        await disp.handle(message)

    unmentioned, mentioned = (event.text for event in adapter.events)
    assert "interaction_id" not in unmentioned and "- staging" in unmentioned
    assert 'interaction_id="env"' in mentioned and "- stg: staging" in mentioned


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
    assert ev.media_text_inlined == [False]
    assert "[image: p.png]" in ev.text   # placeholder used when text empty


@pytest.mark.parametrize("sender, own, text", [
    ("u2", False, "Bob (id u2): earlier words"),
    ("bot1", True, "earlier words"),
])
async def test_quote_fills_reply_fields(sender, own, text):
    disp, adapter, client = _setup({"dm_policy": "open", "allow_from": ["*"]})
    client.identities["u2"] = {"id": "u2", "display_name": "Bob"}
    client.post_replies["/batch-get-messages"] = {"messages": [_msg(sender=sender, text="earlier words", mid="q1")]}

    await disp.handle(dict(_msg(seq=2, text="what about this?"), quote_id="q1"))

    ev = adapter.events[0]
    assert (ev.reply_to_message_id, ev.reply_to_text, ev.reply_to_is_own_message) == ("q1", text, own)


@pytest.mark.parametrize("current_media, media_types, message_type", [
    (None, ["image/gif"], MessageType.TEXT),
    ({"file_id": "f0", "name": "now.png"}, ["image/png", "image/gif"], MessageType.PHOTO),
])
async def test_quoted_media_follows_current_media(current_media, media_types, message_type):
    disp, adapter, client = _setup({"dm_policy": "open", "allow_from": ["*"]})
    quoted = _msg(text="", mid="q1", media={"file_id": "f1", "name": "then.gif", "mime": "image/gif"})
    client.post_replies["/batch-get-messages"] = {"messages": [quoted]}

    await disp.handle(dict(_msg(seq=2, text="and this?", media=current_media), quote_id="q1"))

    ev = adapter.events[0]
    assert (ev.media_types, ev.message_type) == (media_types, message_type)


async def test_failed_quote_read_dispatches_without_quote():
    disp, adapter, client = _setup({"dm_policy": "open", "allow_from": ["*"]})

    async def unavailable(*_args):
        raise RuntimeError("batch-get-messages unavailable")

    client.batch_get_messages = unavailable

    await disp.handle(dict(_msg(seq=2, text="and this?"), quote_id="q1"))

    assert adapter.events[0].reply_to_message_id is None


async def test_telex_tool_handler_accepts_registry_kwargs():
    # Regression: tools.registry calls handler(args, **kwargs) e.g. task_id.
    from hermes_telex import tools
    # unknown action returns JSON error, but must NOT raise on the extra kwarg
    out = await tools.telex_tool_handler({"action": "nope"}, task_id="t1", session_key="s1")
    assert '"error"' in out
