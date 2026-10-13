"""get_conversation_messages carries the message each listed message quotes."""

import json

import pytest

from hermes_telex import accounts, tools
from hermes_telex.types import BlockType
from tests.conftest import StubClient


@pytest.fixture
def client(monkeypatch):
    client = StubClient()
    account = accounts.resolve_account({"api_key": "k"}, "default")
    monkeypatch.setattr(tools, "_resolve_client_and_account", lambda: (client, account))
    return client


def _message(mid, text):
    return {"id": mid, "seq": 1, "sender_id": "u1", "flags": 0,
            "data": {"blocks": [{"seq": 0, "type": BlockType.TEXT, "text": text}]}}


async def _call(**args):
    return json.loads(await tools.telex_tool_handler(args))


async def test_listed_messages_carry_the_quoted_message(client):
    client.messages_by_conv["c1"] = [dict(_message("m2", "replying"), quote_id="q1")]
    client.post_replies["/batch-get-messages"] = {"messages": [_message("q1", "earlier words")]}

    [message] = (await _call(action="get_conversation_messages", conversation_id="c1"))["messages"]

    assert message["quoted_message"]["text"] == "earlier words"


async def test_failed_quote_read_lists_without_quoted_message(client):
    client.messages_by_conv["c1"] = [dict(_message("m2", "replying"), quote_id="q1")]

    async def unavailable(*_args):
        raise RuntimeError("batch-get-messages unavailable")

    client.batch_get_messages = unavailable

    [message] = (await _call(action="get_conversation_messages", conversation_id="c1"))["messages"]

    assert message["quote_id"] == "q1" and "quoted_message" not in message
