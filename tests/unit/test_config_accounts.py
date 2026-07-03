"""T-01 config/validation + T-12 multi-account."""

import pytest

from hermes_telex import accounts, config
from hermes_telex.types import DEFAULT_BASE_URL


def test_defaults_and_resolution():
    acc = accounts.resolve_account({"api_key": "k"}, "default")
    assert acc.configured and acc.enabled
    assert acc.base_url == DEFAULT_BASE_URL
    assert acc.dm_policy == "allowlist"
    assert acc.group_policy == "disabled"
    assert acc.group_require_mention is True
    assert acc.processing_indicator == "activity"
    assert all(acc.tools.values())


def test_open_requires_wildcard():
    config.validate_account({"dm_policy": "open", "allow_from": ["*"]})
    with pytest.raises(config.TelexConfigError):
        config.validate_account({"dm_policy": "open", "allow_from": []})


def test_multi_account_merge_and_enabled():
    extra = {
        "api_key": "base", "base_url": "https://x",
        "accounts": {
            "a": {"api_key": "ka", "dm_policy": "open", "allow_from": ["*"]},
            "b": {"enabled": False, "api_key": "kb"},
            "c": {},  # inherits base api_key? no — base api_key is a base default
        },
    }
    ids = set(accounts.list_account_ids(extra))
    assert ids == {"a", "b", "c"}
    a = accounts.resolve_account(extra, "a")
    assert a.base_url == "https://x" and a.dm_policy == "open" and a.api_key == "ka"
    # account "c" inherits the base api_key (shared default)
    c = accounts.resolve_account(extra, "c")
    assert c.api_key == "base"
    enabled = {x.account_id for x in accounts.list_enabled_accounts(extra)}
    assert "b" not in enabled and "a" in enabled  # b disabled


def test_voyager_server_format_accounts_default():
    # Exact shape stored in the Voyager test server's config.yaml: everything
    # under extra.accounts.default (no top-level account fields).
    extra = {
        "accounts": {
            "default": {
                "allow_from": ["yuy@sea.com"],
                "api_key": "4eb678bda6771",
                "base_url": "http://192.168.100.1:8000",
                "bot_id": "38e2206954dee62f",
                "dm_policy": "allowlist",
                "enabled": True,
                "group_policy": "open",
                "group_require_mention": False,
                "group_sender_allow_from": ["yuy@sea.com"],
            }
        }
    }
    assert accounts.list_account_ids(extra) == ["default"]
    a = accounts.resolve_account(extra, "default")
    assert a.configured and a.enabled
    assert a.api_key == "4eb678bda6771"
    assert a.base_url == "http://192.168.100.1:8000"
    assert a.bot_id == "38e2206954dee62f"
    assert a.dm_policy == "allowlist"
    assert a.allow_from == ["yuy@sea.com"]
    assert a.group_policy == "open"
    assert a.group_require_mention is False
    assert a.group_sender_allow_from == ["yuy@sea.com"]
    enabled = accounts.list_enabled_accounts(extra)
    assert [x.account_id for x in enabled] == ["default"]


def test_tools_toggle_parsing():
    acc = accounts.resolve_account({"api_key": "k", "tools": {"search_identities": False}}, "default")
    assert acc.tools["search_identities"] is False
    assert acc.tools["list_members"] is True


def test_env_config(monkeypatch):
    monkeypatch.setenv("TELEX_API_KEY", "envkey")
    monkeypatch.setenv("TELEX_DM_POLICY", "open")
    monkeypatch.setenv("TELEX_ALLOW_FROM", "*")
    extra = config.config_from_env()
    assert extra["api_key"] == "envkey" and extra["dm_policy"] == "open" and extra["allow_from"] == ["*"]
