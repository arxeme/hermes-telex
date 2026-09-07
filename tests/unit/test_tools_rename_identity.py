"""Tool rename_conversation and update_identity actions: request bodies,
cache eviction, argument validation, per-account gate, and the is_default
field the rename restriction depends on.
"""

import json

import pytest

from hermes_telex import accounts, tools
from tests.conftest import StubClient


@pytest.fixture
def wired(monkeypatch):
    """A StubClient wired in as the tool's resolved account."""
    client = StubClient()
    account = accounts.resolve_account({"api_key": "k"}, "default")
    monkeypatch.setattr(tools, "_resolve_client_and_account", lambda: (client, account))
    return client, account


async def _call(**args):
    return json.loads(await tools.telex_tool_handler(args))


async def test_rename_passes_arguments_through(wired):
    client, _ = wired
    out = await _call(action="rename_conversation", conversation_id="c1", title="Sprint 12")
    assert "error" not in out
    assert client.posts[-1] == ("/rename-conversation", {"conversation_id": "c1", "title": "Sprint 12"})


async def test_rename_evicts_the_cached_conversation(wired):
    client, _ = wired
    client._conversation_cache.set("c1", {"id": "c1", "title": "old"})
    await _call(action="rename_conversation", conversation_id="c1", title="new")
    # The rename's own EVENT message is filtered as a self-send before the
    # refresh hook, so nothing else would drop the stale title inside the TTL.
    assert client._conversation_cache.get("c1") is None


async def test_conversation_info_exposes_is_default(wired):
    client, _ = wired
    client.conversations["c1"] = {"id": "c1", "kind": 0, "is_default": True, "title": "Alice"}
    out = await _call(action="get_conversation_info", conversation_id="c1")
    # The server refuses to rename the default chat, so the agent needs to see
    # which one it is before calling.
    assert out["conversation"]["is_default"] is True


async def test_update_identity_sends_only_the_provided_fields(wired):
    client, _ = wired
    await _call(action="update_identity", display_name="Bot One")
    assert client.posts[-1] == ("/update-identity", {"display_name": "Bot One"})

    await _call(action="update_identity", description="does things")
    assert client.posts[-1] == ("/update-identity", {"description": "does things"})

    await _call(action="update_identity", display_name="Bot One", description="")
    assert client.posts[-1] == ("/update-identity", {"display_name": "Bot One", "description": ""})


async def test_update_identity_requires_one_field(wired):
    client, _ = wired
    out = await _call(action="update_identity")
    assert "display_name and/or description" in out["error"]
    assert client.posts == []


@pytest.mark.parametrize(
    "action,args",
    [
        ("rename_conversation", {"conversation_id": "c1", "title": "t"}),
        ("update_identity", {"display_name": "n"}),
    ],
)
async def test_actions_disabled_per_account(monkeypatch, action, args):
    client = StubClient()
    account = accounts.resolve_account({"api_key": "k", "tools": {action: False}}, "default")
    monkeypatch.setattr(tools, "_resolve_client_and_account", lambda: (client, account))
    out = await _call(action=action, **args)
    assert "disabled" in out["error"]
    assert client.posts == []
