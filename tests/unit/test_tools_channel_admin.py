"""Tool channel-management actions: request bodies, permission-name translation,
argument validation, and the cache eviction every conversation write performs.
"""

import json

import pytest

from hermes_telex import accounts, tools
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


async def test_settings_deny_sets_bits_and_allow_clears_them(wired):
    client, _ = wired
    # The names are applied to the mask the channel holds now, and the whole value is sent back.
    client.conversations["c1"] = {"id": "c1", "kind": 1, "flags": 1}
    await _call(
        action="update_conversation_settings",
        conversation_id="c1",
        deny=["rename", "announcement"],
        allow=["add_members"],
    )
    assert client.posts[-1] == ("/update-conversation-settings", {"conversation_id": "c1", "flags": 12})


async def test_settings_rejects_an_unknown_permission(wired):
    client, _ = wired
    out = await _call(action="update_conversation_settings", conversation_id="c1", deny=["post_messages"])
    assert "unknown permissions: post_messages" in out["error"]
    assert client.posts == []


async def test_settings_requires_a_permission_or_an_announcement(wired):
    client, _ = wired
    out = await _call(action="update_conversation_settings", conversation_id="c1")
    assert "allow or deny" in out["error"]
    assert client.posts == []


async def test_announcement_passes_text_through_and_empty_clears(wired):
    client, _ = wired
    await _call(action="update_conversation_settings", conversation_id="c1", announcement="ship friday")
    assert client.posts[-1] == (
        "/update-conversation-settings",
        {"conversation_id": "c1", "announcement": "ship friday"},
    )

    await _call(action="update_conversation_settings", conversation_id="c1", announcement="")
    assert client.posts[-1] == ("/update-conversation-settings", {"conversation_id": "c1", "announcement": ""})


async def test_delete_conversation_reports_the_deleted_id(wired):
    client, _ = wired
    out = await _call(action="delete_conversation", conversation_id="c1")
    assert client.posts[-1] == ("/delete-conversation", {"conversation_id": "c1"})
    assert out["deleted"] == "c1"


async def test_remove_members_resolves_a_mix_of_id_and_email(wired):
    client, _ = wired
    await _call(action="remove_members", conversation_id="c1", identity_ids=["u2"], emails=["a@b.com"])
    assert client.posts[-1] == ("/remove-members", {"conversation_id": "c1", "identity_ids": ["u2", "u1"]})


async def test_remove_members_requires_a_member(wired):
    client, _ = wired
    out = await _call(action="remove_members", conversation_id="c1")
    assert "identity_id or email" in out["error"]
    assert client.posts == []


async def test_member_role_maps_the_role_word_to_the_wire_value(wired):
    client, _ = wired
    await _call(action="update_member_role", conversation_id="c1", identity_id="u2", role="admin")
    assert client.posts[-1] == ("/update-member-role", {"conversation_id": "c1", "identity_id": "u2", "role": 1})

    await _call(action="update_member_role", conversation_id="c1", identity_id="u2", role="member")
    assert client.posts[-1] == ("/update-member-role", {"conversation_id": "c1", "identity_id": "u2", "role": 0})


async def test_member_role_rejects_an_unknown_role(wired):
    client, _ = wired
    out = await _call(action="update_member_role", conversation_id="c1", identity_id="u2", role="captain")
    assert "member, admin or owner" in out["error"]
    assert client.posts == []


async def test_member_role_hands_the_channel_over(wired):
    client, _ = wired
    await _call(action="update_member_role", conversation_id="c1", identity_id="u2", role="owner")
    assert client.posts[-1] == ("/update-member-role", {"conversation_id": "c1", "identity_id": "u2", "role": 2})


async def test_member_role_resolves_an_email(wired):
    client, _ = wired
    await _call(action="update_member_role", conversation_id="c1", email="a@b.com", role="admin")
    assert client.posts[-1] == ("/update-member-role", {"conversation_id": "c1", "identity_id": "u1", "role": 1})


async def test_member_role_requires_exactly_one_identity(wired):
    client, _ = wired
    out = await _call(action="update_member_role", conversation_id="c1", role="admin")
    assert "exactly one" in out["error"]
    assert client.posts == []


@pytest.mark.parametrize(
    "args",
    [
        {"action": "update_conversation_settings", "deny": ["rename"]},
        {"action": "remove_members", "identity_ids": ["u2"]},
        {"action": "update_member_role", "identity_id": "u2", "role": "admin"},
        {"action": "delete_conversation"},
    ],
)
async def test_every_write_evicts_the_cached_conversation(wired, args):
    client, _ = wired
    client._conversation_cache.set("c1", {"id": "c1", "kind": 1, "flags": 0})
    await _call(conversation_id="c1", **args)
    assert client._conversation_cache.get("c1") is None


async def test_conversation_info_lists_what_a_plain_member_may_do(wired):
    client, _ = wired
    # flags 6 = remove_members | rename restricted to the owner and admins.
    client.conversations["c1"] = {
        "id": "c1", "kind": 1, "title": "Sprint", "flags": 6,
        "data": {"announcement": "ship friday"},
        "membership": {"identity_id": "bot", "role": 0},
    }
    out = await _call(action="get_conversation_info", conversation_id="c1")
    assert out["conversation"]["member_permissions"] == {
        "add_members": True,
        "remove_members": False,
        "rename": False,
        "announcement": True,
        "mention_all": True,
    }
    assert out["conversation"]["my_role"] == "member"
    assert out["conversation"]["announcement"] == "ship friday"
    assert "flags" not in out["conversation"]


async def test_conversation_info_reports_the_same_settings_to_an_admin(wired):
    client, _ = wired
    client.conversations["c1"] = {
        "id": "c1", "kind": 1, "flags": 6, "membership": {"identity_id": "bot", "role": 1},
    }
    out = await _call(action="get_conversation_info", conversation_id="c1")
    assert out["conversation"]["my_role"] == "admin"
    assert out["conversation"]["member_permissions"]["rename"] is False


async def test_conversation_info_omits_the_role_without_a_membership(wired):
    client, _ = wired
    client.conversations["c1"] = {"id": "c1", "kind": 1, "flags": 6}
    out = await _call(action="get_conversation_info", conversation_id="c1")
    assert out["conversation"]["member_permissions"]["rename"] is False
    assert "my_role" not in out["conversation"]


async def test_conversation_info_leaves_a_chat_without_channel_fields(wired):
    client, _ = wired
    client.conversations["c2"] = {"id": "c2", "kind": 0, "title": "Alice", "flags": 0}
    out = await _call(action="get_conversation_info", conversation_id="c2")
    assert "member_permissions" not in out["conversation"]
    assert "announcement" not in out["conversation"]


async def test_settings_mask_is_a_union_not_a_sum(wired):
    client, _ = wired
    # A repeated name must not carry into the next bit: rename twice is still rename, not announcement.
    await _call(action="update_conversation_settings", conversation_id="c1", deny=["rename", "rename"])
    assert client.posts[-1][1]["flags"] == 4
