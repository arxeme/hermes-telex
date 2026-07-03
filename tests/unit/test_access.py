"""T-13 access control: matching rule + dm/group policies."""

from hermes_telex import access


def test_sender_allowed_wildcard_id_email():
    assert access.is_sender_allowed("id1", "A@B.com", ["a@b.com"]) is True   # case-insensitive email
    assert access.is_sender_allowed("id1", None, ["*"]) is True              # wildcard
    assert access.is_sender_allowed("id1", "x@y.com", [" id1 "]) is True     # exact id (trimmed)
    assert access.is_sender_allowed("id1", "x@y.com", ["other"]) is False


def test_dm_allowlist():
    assert access.check_dm_access(dm_policy="allowlist", allow_from=["a@b.com"],
                                  sender_id="i", sender_email="a@b.com") == access.DM_ALLOW
    assert access.check_dm_access(dm_policy="allowlist", allow_from=[],
                                  sender_id="i", sender_email="x@y.com") == access.DM_DENY


def test_dm_open_and_pairing():
    assert access.check_dm_access(dm_policy="open", allow_from=["*"],
                                  sender_id="i", sender_email="x") == access.DM_ALLOW
    # pairing: unknown sender is forwarded to the gateway handshake
    assert access.check_dm_access(dm_policy="pairing", allow_from=[],
                                  sender_id="i", sender_email="x") == access.DM_PAIRING
    # pairing: pre-listed sender is allowed directly
    assert access.check_dm_access(dm_policy="pairing", allow_from=["i"],
                                  sender_id="i", sender_email="x") == access.DM_ALLOW


def test_group_policies():
    assert access.check_group_access(group_policy="disabled", group_allow_from=None,
                                     group_sender_allow_from=None, conversation_id="c",
                                     sender_id="s", sender_email=None).allowed is False
    assert access.check_group_access(group_policy="allowlist", group_allow_from=["c1"],
                                     group_sender_allow_from=None, conversation_id="c2",
                                     sender_id="s", sender_email=None).allowed is False
    assert access.check_group_access(group_policy="allowlist", group_allow_from=["c1"],
                                     group_sender_allow_from=None, conversation_id="c1",
                                     sender_id="s", sender_email=None).allowed is True
    # sender allowlist inside an allowed channel
    assert access.check_group_access(group_policy="open", group_allow_from=None,
                                     group_sender_allow_from=["a@b.com"], conversation_id="c",
                                     sender_id="s", sender_email="x@y.com").allowed is False
