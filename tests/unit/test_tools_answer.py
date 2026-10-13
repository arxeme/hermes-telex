"""Tool actions an agent answers interactions with: reading their ids, and answer_interaction."""

import json

import pytest

from hermes_telex import accounts, tools
from hermes_telex.client import TelexClient
from hermes_telex.types import BlockType, ConversationKind, InteractionMode
from tests.conftest import StubClient


@pytest.fixture
def client(monkeypatch):
    client = StubClient(bot_id="bot1")
    account = accounts.resolve_account({"api_key": "k"}, "default")
    monkeypatch.setattr(tools, "_resolve_client_and_account", lambda: (client, account))
    return client


async def _call(**args):
    return json.loads(await tools.telex_tool_handler(args))


@pytest.mark.parametrize("kind, asked", [(ConversationKind.CHAT, True), (ConversationKind.CHANNEL, False)])
async def test_listed_messages_carry_ids_when_asked(client, kind, asked):
    client.conversations["c1"] = {"id": "c1", "kind": kind}
    client.messages_by_conv["c1"] = [{"id": "m1", "seq": 1, "sender_id": "u1", "flags": 0, "data": {"blocks": [
        {"seq": 0, "type": BlockType.INTERACTION, "interaction": {
            "id": "env", "mode": InteractionMode.SELECT,
            "questions": [{"options": [{"id": "stg", "label": "staging"}]}]}}]}}]

    out = await _call(action="get_conversation_messages", conversation_id="c1")

    text = out["messages"][0]["text"]
    assert ('interaction_id="env"' in text) is asked and "staging" in text


@pytest.mark.parametrize("given, sent", [
    ({"answers": [{"option_ids": ["stg"]}]}, {"answers": [{"option_ids": ["stg"]}]}),
    ({"skip": True}, {"skip": True}),
])
async def test_answer_posts_body_and_acks(client, given, sent):
    client.post_replies["/answer-interaction"] = {"message": {"id": "m1", "conversation_id": "c1", "seq": 3}}

    out = await _call(action="answer_interaction", message_id="m1", interaction_id="env", **given)

    assert client.posts == [("/answer-interaction", {"message_id": "m1", "interaction_id": "env", **sent})]
    assert out == {"message": {"id": "m1", "conversation_id": "c1", "seq": 3}}


async def test_refused_answer_returns_detail(client, monkeypatch):
    body = {"code": 3, "message": "invalid_option", "details": [{
        "@type": "type.googleapis.com/errors.Error", "error": "invalid_option",
        "detail": {"interaction_id": "env", "question": 0, "option_id": "qa"}}]}

    class Response:
        status = 400

        async def text(self):
            return json.dumps(body)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class Session:
        def request(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(client, "_post", TelexClient._post.__get__(client))
    monkeypatch.setattr(client, "_get_session", lambda: Session())

    out = await _call(action="answer_interaction", message_id="m1", interaction_id="env",
                      answers=[{"option_ids": ["qa"]}])

    assert out["error"] == "Telex API error: invalid_option"
    assert out["detail"] == {"interaction_id": "env", "question": 0, "option_id": "qa"}
