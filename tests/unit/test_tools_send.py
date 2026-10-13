"""Tool send_message action: targeting, validation, media, per-account gate.

These exist because hermes's core send_message cannot address Telex ids (its
_parse_target_ref has no telex branch), so the plugin owns sending.
"""

import json

import pytest

from hermes_telex import accounts, tools
from hermes_telex.types import BlockType
from tests.conftest import StubClient


@pytest.fixture
def wired(monkeypatch):
    """A StubClient wired in as the tool's resolved account."""
    client = StubClient()
    client.identities["u1"] = {"id": "u1", "email": "a@b.com", "display_name": "Alice"}
    account = accounts.resolve_account({"api_key": "k"}, "default")
    monkeypatch.setattr(tools, "_resolve_client_and_account", lambda: (client, account))
    return client, account


async def _call(**args):
    return json.loads(await tools.telex_tool_handler(args))


async def test_send_to_conversation(wired):
    client, _ = wired
    out = await _call(action="send_message", conversation_id="c1", text="hello")
    assert "error" not in out
    assert client.sent[0]["conversation_id"] == "c1"
    assert client.sent[0]["blocks"][0]["text"] == "hello"
    assert out["message"]["id"]


async def test_send_to_peer_and_by_email(wired):
    client, _ = wired
    await _call(action="send_message", peer_id="u1", text="hi")
    assert client.sent[-1]["peer_id"] == "u1"
    await _call(action="send_message", email="A@B.com", text="hi")   # case-insensitive
    assert client.sent[-1]["peer_id"] == "u1"


async def test_send_unknown_email_fails_before_sending(wired):
    client, _ = wired
    out = await _call(action="send_message", email="nobody@x.com", text="hi")
    assert "no identity found" in out["error"]
    assert client.sent == []


async def test_send_requires_exactly_one_target(wired):
    both = await _call(action="send_message", conversation_id="c1", peer_id="u1", text="hi")
    assert "exactly one target" in both["error"]
    neither = await _call(action="send_message", text="hi")
    assert "exactly one target" in neither["error"]


async def test_send_quote_requires_conversation(wired, tmp_path):
    client, _ = wired
    pic = tmp_path / "pic.png"
    pic.write_bytes(b"img")
    out = await _call(action="send_message", peer_id="u1", quote_id="q1", text="hi", media_paths=[str(pic)])
    assert "quote_id needs conversation_id" in out["error"]
    assert client.sent == [] and client.uploaded == []


async def test_send_requires_content(wired):
    out = await _call(action="send_message", conversation_id="c1")
    assert "text and/or media_paths" in out["error"]


async def test_send_with_media(wired, tmp_path):
    client, _ = wired
    pic = tmp_path / "shot.png"
    pic.write_bytes(b"imgdata")
    out = await _call(action="send_message", conversation_id="c1",
                      text="see", media_paths=[str(pic)])
    assert "error" not in out
    blocks = client.sent[-1]["blocks"]
    assert blocks[0]["type"] == BlockType.TEXT
    assert blocks[1]["type"] == BlockType.IMAGE      # kind inferred from extension
    assert client.uploaded == [("shot.png", "image/png")]


async def test_send_mention_token_passes_through(wired):
    # Mentions are inline tokens; the server resolves them from the text.
    client, _ = wired
    await _call(action="send_message", conversation_id="c1",
                text="ping [@](mention:u1)")
    assert "[@](mention:u1)" in client.sent[-1]["blocks"][0]["text"]


async def test_create_chat_action_removed(wired):
    # send_message(peer_id) opens the default 1:1 itself; a create_chat action
    # would let the agent fragment a human's chat list with parallel titled
    # conversations, so it must not exist.
    out = await _call(action="create_chat", peer_id="u1", text="hi")
    assert "unknown action" in out["error"]


async def test_actions_disabled_per_account(monkeypatch):
    client = StubClient()
    account = accounts.resolve_account(
        {"api_key": "k", "tools": {"send_message": False}}, "default"
    )
    monkeypatch.setattr(tools, "_resolve_client_and_account", lambda: (client, account))
    out = await _call(action="send_message", conversation_id="c1", text="hi")
    assert "disabled" in out["error"]
    assert client.sent == []


async def test_send_blocked_when_account_ambiguous(monkeypatch):
    monkeypatch.setattr(tools, "_resolve_client_and_account", lambda: (None, "ambiguous"))
    out = await _call(action="send_message", conversation_id="c1", text="hi")
    assert "multiple Telex accounts" in out["error"]
