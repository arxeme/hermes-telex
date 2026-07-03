"""T-01 registration + T-13 enforces_own_access_policy + monitor backfill."""

from hermes_telex import adapter as adp
from hermes_telex import monitor
from tests.conftest import StubClient


class FakeCtx:
    def __init__(self):
        self.platform = None
        self.tools = []

    def register_platform(self, **kwargs):
        self.platform = kwargs

    def register_tool(self, **kwargs):
        self.tools.append(kwargs)


def test_register_platform_kwargs():
    ctx = FakeCtx()
    adp.register(ctx)
    p = ctx.platform
    assert p["name"] == "telex"
    assert p["cron_deliver_env_var"] == "TELEX_HOME_CHANNEL"
    assert callable(p["standalone_sender_fn"])
    assert callable(p["env_enablement_fn"])
    assert p["max_message_length"] == adp.MAX_MESSAGE_LENGTH
    # access enforced in-plugin, not via gateway allowlist
    assert "allowed_users_env" not in p
    # tool registered
    assert any(t["name"] == "telex" for t in ctx.tools)


def test_adapter_enforces_own_policy_and_dm_policy():
    class Cfg:
        extra = {"api_key": "k", "dm_policy": "pairing"}
    a = adp.TelexAdapter(Cfg())
    assert a.enforces_own_access_policy is True
    assert a._dm_policy == "pairing"


def test_check_requirements_no_env(monkeypatch):
    # check_fn is a dependency check only; must NOT require TELEX_API_KEY
    # (config may live in config.yaml). aiohttp is installed in the test venv.
    monkeypatch.delenv("TELEX_API_KEY", raising=False)
    assert adp.check_telex_requirements() is True


def test_adapter_constructs_with_accounts_default():
    # Exact Voyager config.yaml shape must build a runtime + validate connected.
    class Cfg:
        extra = {"accounts": {"default": {
            "api_key": "4eb678bda6771", "base_url": "http://192.168.100.1:8000",
            "bot_id": "38e2206954dee62f", "dm_policy": "allowlist",
            "allow_from": ["yuy@sea.com"], "group_policy": "open",
            "group_require_mention": False, "group_sender_allow_from": ["yuy@sea.com"],
            "enabled": True,
        }}}
    a = adp.TelexAdapter(Cfg())
    assert len(a._runtimes) == 1 and "default" in a._runtimes
    assert a._dm_policy == "allowlist"
    assert adp._is_telex_connected(Cfg()) is True


def test_env_enablement_flat_and_home(monkeypatch):
    monkeypatch.setenv("TELEX_API_KEY", "envk")
    monkeypatch.setenv("TELEX_HOME_CHANNEL", "0a1b2c3d4e5f6071")
    result = adp._telex_env_enablement()
    # flat fields (merged into extra by the core hook), plus home_channel key
    assert result["api_key"] == "envk"
    assert result["home_channel"]["chat_id"] == "0a1b2c3d4e5f6071"


def test_env_enablement_home_channel_without_api_key(monkeypatch):
    # Regression: /sethome writes TELEX_HOME_CHANNEL to .env while api_key lives
    # in config.yaml. The home channel MUST still be surfaced (not gated on api_key).
    monkeypatch.delenv("TELEX_API_KEY", raising=False)
    monkeypatch.setenv("TELEX_HOME_CHANNEL", "b7cbc72a481784b0")
    result = adp._telex_env_enablement()
    assert result is not None
    assert result.get("home_channel", {}).get("chat_id") == "b7cbc72a481784b0"
    assert "api_key" not in result  # nothing from env when only HOME_CHANNEL is set


async def test_monitor_backfill_paging():
    c = StubClient()
    # settled cursor at 2; messages 3,4,5 arrive during downtime
    c.note_message("conv", 2, terminal=True)
    c.messages_by_conv["conv"] = [
        {"id": "m3", "conversation_id": "conv", "seq": 3, "status": 0, "flags": 0,
         "sender_id": "u", "data": {"blocks": []}},
        {"id": "m4", "conversation_id": "conv", "seq": 4, "status": 0, "flags": 0,
         "sender_id": "u", "data": {"blocks": []}},
    ]
    seen = []

    async def on_message(m):
        seen.append(m["seq"])

    from hermes_telex.monitor import _backfill
    await _backfill(c, on_message)
    assert seen == [3, 4]


async def test_standalone_send(tmp_path, monkeypatch):
    # standalone_sender_fn uses a real client; patch send to avoid network
    sent = {}

    async def fake_send(client, **kwargs):
        sent.update(kwargs)
        return {"id": "mid1"}

    monkeypatch.setattr(adp.sendmod, "send_telex_message", fake_send)

    class PCfg:
        extra = {"api_key": "k", "base_url": "https://t"}
    res = await adp._telex_standalone_send(PCfg(), "0a1b2c3d4e5f6071", "hello")
    assert res == {"success": True, "message_id": "mid1"}
    assert sent["conversation_id"] == "0a1b2c3d4e5f6071"
