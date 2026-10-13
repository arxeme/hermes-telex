"""Gateway prompts rendered as Telex interactions and resolved from the answers to them."""

import asyncio
import sys
import threading
import types

import pytest

from hermes_telex import adapter as adp
from hermes_telex.client import TelexError
from hermes_telex.types import (
    BlockType,
    ConversationKind,
    InteractionButtonStyle,
    InteractionMode,
    InteractionStatus,
    MessageFlag,
)
from tests.conftest import StubClient


SKIPPED = "\x00skipped"
COMMAND = "rm -rf /tmp/x"


class FakeHermes:
    def __init__(self):
        self.clarify_entries: dict[str, types.SimpleNamespace] = {}
        self.clarify_resolved: list[tuple[str, str]] = []
        self.approval_queues: dict[str, list[str]] = {}
        self.approval_commands: dict[str, str] = {}
        self.approval_resolved: list[tuple[str, str, str]] = []
        self.confirms: dict[str, dict] = {}
        self.confirm_resolved: list[tuple[str, str, str]] = []

    def install(self, monkeypatch):
        hermes = self
        clarify = types.ModuleType("tools.clarify_gateway")
        clarify._lock = threading.RLock()
        clarify._entries = self.clarify_entries
        clarify.SKIPPED = SKIPPED
        clarify.get_clarify_timeout = lambda: 3600

        def resolve_gateway_clarify(clarify_id, response):
            entry = hermes.clarify_entries.get(clarify_id)
            if entry is None:
                return False
            entry.event.set()
            hermes.clarify_resolved.append((clarify_id, response))
            return True

        clarify.resolve_gateway_clarify = resolve_gateway_clarify

        approval = types.ModuleType("tools.approval")
        approval.list_gateway_approvals = lambda session_key: [
            {"request_id": request_id, "command": hermes.approval_commands.get(request_id, COMMAND)}
            for request_id in hermes.approval_queues.get(session_key, [])
        ]

        def resolve_gateway_approval(session_key, choice, request_id=None):
            queue = hermes.approval_queues.get(session_key, [])
            if not queue or (request_id and request_id not in queue):
                return 0
            request_id = request_id or queue[0]
            queue.remove(request_id)
            hermes.approval_resolved.append((session_key, request_id, choice))
            return 1

        approval.resolve_gateway_approval = resolve_gateway_approval
        exec_approval = types.ModuleType("gateway.platforms.base_exec_approval")
        exec_approval.approval_timeout_seconds = lambda: 300
        gateway_run = types.ModuleType("gateway.run")
        gateway_run._redact_approval_command = lambda command: command.replace("SECRET", "***")

        confirm = types.ModuleType("tools.slash_confirm")
        confirm.DEFAULT_TIMEOUT_SECONDS = 300
        confirm.get_pending = lambda session_key: hermes.confirms.get(session_key)

        async def resolve(session_key, confirm_id, choice):
            hermes.confirms.pop(session_key, None)
            hermes.confirm_resolved.append((session_key, confirm_id, choice))
            return "reloaded"

        confirm.resolve = resolve

        monkeypatch.setitem(sys.modules, "tools", types.ModuleType("tools"))
        monkeypatch.setitem(sys.modules, "tools.clarify_gateway", clarify)
        monkeypatch.setitem(sys.modules, "tools.approval", approval)
        monkeypatch.setitem(sys.modules, "gateway.platforms.base_exec_approval", exec_approval)
        monkeypatch.setitem(sys.modules, "gateway.run", gateway_run)
        monkeypatch.setitem(sys.modules, "tools.slash_confirm", confirm)
        return self


@pytest.fixture
def hermes(monkeypatch):
    return FakeHermes().install(monkeypatch)


def _adapter(monkeypatch, extra=None):
    monkeypatch.setenv(adp.INTERNAL_ALLOW_ALL_ENV, "sentinel")

    class Cfg:
        pass

    Cfg.extra = {"api_key": "k", "bot_id": "bot1", "dm_policy": "open", "allow_from": ["*"],
                 "group_policy": "open", **(extra or {})}
    adapter = adp.TelexAdapter(Cfg())
    adapter._is_sender_authorized = lambda user_id, chat_type=None, chat_id=None: True
    rt = next(iter(adapter._runtimes.values()))
    client = StubClient(bot_id="bot1")
    client.identities["u1"] = {"id": "u1", "email": "a@b.com", "display_name": "Alice"}
    client.identities["u2"] = {"id": "u2", "email": "x@y.com", "display_name": "Mallory"}
    client.conversations["c1"] = {"id": "c1", "kind": 0, "title": "Room"}
    rt.client = client
    rt.dispatcher = adp.TelexDispatcher(adapter, rt.account, client)
    return adapter, rt, client


def _interaction(message):
    return next(b["interaction"] for b in message["blocks"] if b["type"] == BlockType.INTERACTION)


def _answered(message_id, interaction_id, status, answer=None, *, answerer="u1", asker="bot1", flags=MessageFlag.EVENT):
    interaction = {"id": interaction_id, "status": status, "answerer_id": answerer, "questions": [{"answer": answer}]}
    details = {"message_id": message_id, "asker_id": asker, "interaction": interaction}
    return {"id": f"e-{message_id}", "conversation_id": "c1", "seq": 9, "sender_id": answerer, "status": 0,
            "flags": flags,
            "data": {"blocks": [{"type": BlockType.EVENT,
                                 "event": {"kind": "interaction_answered", "details": details}}]}}


def _answer_to(client, index, status, answer=None, *, answerer="u1"):
    return _answered(f"m{index}", _interaction(client.sent[index])["id"], status, answer, answerer=answerer)


async def _send_clarify(adapter, hermes, *, clarify_id="abc", question="Which env?", choices=("staging", "prod"),
                        multi_select=False):
    hermes.clarify_entries[clarify_id] = types.SimpleNamespace(
        multi_select=multi_select, event=threading.Event()
    )
    return await adapter.send_clarify("c1", question, list(choices), clarify_id, "sk")


def _approval(command=COMMAND):
    actions = [("Allow Once", "once", "primary"), ("Allow Session", "session", ""), ("Deny", "deny", "danger")]
    return types.SimpleNamespace(
        chat_id="c1", session_key="sk", metadata=None, command=command, text=f"Run {command}?", actions=actions
    )


async def _deliver(adapter, rt, message):
    await adapter._handle_inbound(rt, message)


async def test_clarify_goes_out_as_select_card(monkeypatch, hermes):
    adapter, _rt, client = _adapter(monkeypatch)

    result = await _send_clarify(adapter, hermes)

    assert result.success
    interaction = _interaction(client.sent[0])
    assert interaction["id"] == "abc"
    assert interaction["mode"] == InteractionMode.SELECT
    assert interaction["questions"] == [{
        "text": "Which env?", "options": [{"id": "0", "label": "staging"}, {"id": "1", "label": "prod"}],
        "allow_free_text": True,
    }]
    assert interaction["deadline"]


async def test_multi_select_resolves_as_json_array(monkeypatch, hermes):
    adapter, rt, client = _adapter(monkeypatch)
    await _send_clarify(adapter, hermes, question="Which?", choices=["a", "b", "c"], multi_select=True)
    assert _interaction(client.sent[0])["questions"][0]["multi_select"] is True

    await _deliver(adapter, rt, _answer_to(client, 0, InteractionStatus.ANSWERED,
                                            {"option_ids": ["0", "2"], "text": "d"}))

    assert hermes.clarify_resolved == [("abc", '["a", "c", "d"]')]


async def test_skip_resolves_as_skipped(monkeypatch, hermes):
    adapter, rt, client = _adapter(monkeypatch)
    await _send_clarify(adapter, hermes)

    await _deliver(adapter, rt, _answer_to(client, 0, InteractionStatus.SKIPPED))

    assert hermes.clarify_resolved == [("abc", SKIPPED)]


async def test_lapsed_prompts_are_pruned(monkeypatch, hermes):
    adapter, _rt, _client = _adapter(monkeypatch)
    router = adapter._interactions
    await _send_clarify(adapter, hermes)
    router._prompts["m0"].expires_at = 0

    await _send_clarify(adapter, hermes, clarify_id="def")

    assert list(router._prompts) == ["m1"]


async def test_unwaited_answer_closes_card(monkeypatch, hermes):
    adapter, rt, client = _adapter(monkeypatch)
    await _send_clarify(adapter, hermes)
    hermes.clarify_entries.clear()

    await _deliver(adapter, rt, _answer_to(client, 0, InteractionStatus.ANSWERED,
                                            {"option_ids": ["0"]}))

    assert client.closed_interactions == [("m0", "abc")]


@pytest.mark.parametrize("verdict, kind, chat_type", [
    (False, ConversationKind.CHAT, "dm"),
    (None, ConversationKind.CHAT, "dm"),
    (False, ConversationKind.CHANNEL, "group"),
])
async def test_gateway_refusal_closes_card(monkeypatch, hermes, verdict, kind, chat_type):
    adapter, rt, client = _adapter(monkeypatch)
    client.conversations["c1"]["kind"] = kind
    await _send_clarify(adapter, hermes)
    checked = []

    def refuse(user_id, chat_type=None, chat_id=None):
        checked.append((user_id, chat_type, chat_id))
        return verdict

    adapter._is_sender_authorized = refuse
    await _deliver(adapter, rt, _answer_to(client, 0, InteractionStatus.ANSWERED,
                                            {"option_ids": ["0"]}))

    assert checked == [("a@b.com", chat_type, "c1")]
    assert hermes.clarify_resolved == []
    assert client.closed_interactions == [("m0", "abc")]


async def test_retired_clarify_closes_open_card(monkeypatch, hermes):
    adapter, rt, client = _adapter(monkeypatch)
    await _send_clarify(adapter, hermes)
    await _send_clarify(adapter, hermes, clarify_id="def")

    await _deliver(adapter, rt, _answer_to(client, 1, InteractionStatus.SKIPPED))
    await adapter.retire_clarify_card("def", "expired")
    assert client.closed_interactions == []
    await adapter.retire_clarify_card("abc", "expired")
    assert client.closed_interactions == [("m0", "abc")]


async def test_refused_sender_closes_card(monkeypatch, hermes):
    adapter, rt, client = _adapter(monkeypatch, {"dm_policy": "allowlist", "allow_from": ["a@b.com"]})
    await _send_clarify(adapter, hermes)

    await _deliver(adapter, rt, _answer_to(client, 0, InteractionStatus.ANSWERED,
                                            {"option_ids": ["0"]}, answerer="u2"))

    assert hermes.clarify_resolved == []
    assert client.closed_interactions == [("m0", "abc")]


async def test_approval_card_buttons_and_typing(monkeypatch, hermes):
    adapter, rt, client = _adapter(monkeypatch)
    resumed = []
    adapter._interactions._resume_typing = resumed.append
    hermes.approval_queues["sk"] = ["r1"]

    result = await adapter._send_exec_approval_prompt(_approval())

    assert result.success
    assert client.sent[0]["blocks"][0]["type"] == BlockType.TEXT
    interaction = _interaction(client.sent[0])
    assert interaction["mode"] == InteractionMode.BUTTON
    assert [(o["id"], o["label"], o.get("style")) for o in interaction["questions"][0]["options"]] == [
        ("once", "Allow Once", InteractionButtonStyle.PRIMARY),
        ("session", "Allow Session", None),
        ("deny", "Deny", InteractionButtonStyle.DANGER),
    ]
    assert interaction["deadline"]
    await _deliver(adapter, rt, _answer_to(client, 0, InteractionStatus.ANSWERED,
                                            {"option_ids": ["session"]}))
    assert resumed == ["c1"]
    assert client.closed_interactions == []


async def test_concurrent_approval_cards_bind_their_own_requests(monkeypatch, hermes):
    adapter, rt, client = _adapter(monkeypatch)
    hermes.approval_queues["sk"] = ["r1", "r2", "r3"]
    hermes.approval_commands.update(r1="deploy --token SECRET", r2="cmd two", r3="cmd two")
    send = client.send_message

    async def yielding_send(**kwargs):
        await asyncio.sleep(0)
        return await send(**kwargs)

    client.send_message = yielding_send
    results = await asyncio.gather(*(adapter._send_exec_approval_prompt(_approval(command)) for command in (
        "cmd two", "deploy --token ***", "cmd two", "cmd three")))

    assert [result.success for result in results] == [True, True, True, False]
    assert sorted(prompt.request_id for prompt in adapter._interactions._prompts.values()) == ["r1", "r2", "r3"]
    await _deliver(adapter, rt, _answer_to(client, 1, InteractionStatus.ANSWERED, {"option_ids": ["deny"]}))
    assert hermes.approval_resolved == [("sk", "r1", "deny")]


async def test_stale_approval_click_closes_card(monkeypatch, hermes):
    adapter, rt, client = _adapter(monkeypatch)
    hermes.approval_queues["sk"] = ["r1"]
    await adapter._send_exec_approval_prompt(_approval())
    # The first approval ended through another path and a second one is now waiting.
    hermes.approval_queues["sk"] = ["r2"]

    await _deliver(adapter, rt, _answer_to(client, 0, InteractionStatus.ANSWERED,
                                            {"option_ids": ["always"]}))

    assert hermes.approval_resolved == []
    interaction_id = _interaction(client.sent[0])["id"]
    assert client.closed_interactions == [("m0", interaction_id)]


async def test_confirm_result_uses_asking_account(monkeypatch, hermes):
    accounts = {"default": {"api_key": "k1", "bot_id": "bot1"}, "support": {"api_key": "k2", "bot_id": "bot2"}}
    adapter, _rt, first = _adapter(monkeypatch, {"accounts": accounts})
    support = adapter._runtimes["support"]
    second = StubClient(bot_id="bot2")
    second.identities.update(first.identities)
    second.conversations.update(first.conversations)
    support.client = second
    support.dispatcher = adp.TelexDispatcher(adapter, support.account, second)
    hermes.confirms["sk"] = {"confirm_id": "cf1"}
    await adapter.send_slash_confirm("c1", "Reload MCP", "Clears the cache.", "sk", "cf1",
                                     metadata={"telex_account_id": "support"})

    await _deliver(adapter, support, _answered("m0", _interaction(second.sent[0])["id"], InteractionStatus.ANSWERED,
                                               {"option_ids": ["once"]}, asker="bot2"))
    for task in list(adapter._interactions._tasks):
        await task

    assert hermes.confirm_resolved == [("sk", "cf1", "once")]
    assert second.sent[-1]["blocks"][0]["text"] == "reloaded"
    assert first.sent == []


async def test_stale_confirm_closes_card(monkeypatch, hermes):
    adapter, rt, client = _adapter(monkeypatch)
    hermes.confirms["sk"] = {"confirm_id": "cf1"}
    await adapter.send_slash_confirm("c1", "Reload MCP", "Clears the cache.", "sk", "cf1")
    hermes.confirms.clear()

    await _deliver(adapter, rt, _answer_to(client, 0, InteractionStatus.ANSWERED,
                                            {"option_ids": ["once"]}))

    assert hermes.confirm_resolved == []
    assert client.closed_interactions == [("m0", _interaction(client.sent[0])["id"])]


async def test_unknown_answer_closes_card(monkeypatch, hermes):
    adapter, rt, client = _adapter(monkeypatch)

    await _deliver(adapter, rt, _answered("old", "q", InteractionStatus.SKIPPED))
    assert client.closed_interactions == []
    await _deliver(adapter, rt, _answered("old", "q", InteractionStatus.ANSWERED, {"text": "prod"}))
    assert client.closed_interactions == [("old", "q")]


async def test_foreign_answer_events_ignored(monkeypatch, hermes):
    adapter, rt, client = _adapter(monkeypatch)
    await _send_clarify(adapter, hermes)
    answer = {"option_ids": ["0"]}

    await _deliver(adapter, rt, _answered("m0", "abc", InteractionStatus.ANSWERED, answer, asker="bot2"))
    await _deliver(adapter, rt, _answered("m0", "abc", InteractionStatus.ANSWERED, answer,
                                          flags=MessageFlag.EVENT | MessageFlag.FORK_PREFIX))
    await _deliver(adapter, rt, _answered("m0", "abc", InteractionStatus.ANSWERED, answer, flags=0))

    assert hermes.clarify_resolved == []
    assert client.closed_interactions == []


async def test_failed_check_keeps_prompt_for_retry(monkeypatch, hermes):
    adapter, rt, client = _adapter(monkeypatch)
    await _send_clarify(adapter, hermes)
    answered = _answer_to(client, 0, InteractionStatus.ANSWERED, {"option_ids": ["1"]})
    lookup = client.resolve_identity

    async def unavailable(ident_id):
        raise RuntimeError("identity lookup unavailable")

    client.resolve_identity = unavailable
    with pytest.raises(RuntimeError):
        await _deliver(adapter, rt, answered)
    client.resolve_identity = lookup
    await _deliver(adapter, rt, answered)

    assert hermes.clarify_resolved == [("abc", "prod")]


async def test_channel_card_answerers_from_allowlist(monkeypatch, hermes):
    adapter, _rt, client = _adapter(monkeypatch, {"group_sender_allow_from": ["a@b.com", "u3", "u4"]})
    client.conversations["c1"]["kind"] = ConversationKind.CHANNEL
    client.members["c1"] = ["bot1", "u1", "u3"]

    await _send_clarify(adapter, hermes)

    assert _interaction(client.sent[0])["answerer_ids"] == ["u3", "u1"]


async def test_wildcard_allowlist_sets_no_answerers(monkeypatch, hermes):
    adapter, _rt, client = _adapter(monkeypatch, {"group_sender_allow_from": ["*", "u3"]})
    client.conversations["c1"]["kind"] = ConversationKind.CHANNEL
    client.members["c1"] = ["bot1", "u1", "u3"]

    await _send_clarify(adapter, hermes)

    assert "answerer_ids" not in _interaction(client.sent[0])


async def test_refused_card_reports_failure(monkeypatch, hermes):
    adapter, _rt, client = _adapter(monkeypatch)

    async def refused(**kwargs):
        raise TelexError("Telex API error: invalid_argument", http_status=400)

    send = client.send_message
    client.send_message = refused
    hermes.approval_queues["sk"] = ["r1"]
    assert (await adapter._send_exec_approval_prompt(_approval())).success is False
    assert adapter._interactions._prompts == {}
    client.send_message = send
    assert (await adapter._send_exec_approval_prompt(_approval())).success

    adapter, _rt, client = _adapter(monkeypatch, {"group_sender_allow_from": ["u4"]})
    client.conversations["c1"]["kind"] = ConversationKind.CHANNEL
    client.members["c1"] = ["bot1", "u1"]
    assert (await _send_clarify(adapter, hermes)).success is False
    assert client.sent == []
    assert adapter._interactions._prompts == {}

