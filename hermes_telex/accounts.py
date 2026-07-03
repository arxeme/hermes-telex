"""Multi-account resolution (port of openclaw-telex accounts.ts).

The top-level ``platforms.telex.extra`` provides shared base config; each
``accounts.<id>`` entry overrides it. With no ``accounts`` key there is a single
"default" account taken from the top level.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import config as cfg

DEFAULT_ACCOUNT_ID = "default"

# Account-scoped keys that an accounts.<id> entry may override.
_SCALAR_KEYS = (
    "enabled", "api_key", "base_url", "bot_id",
    "dm_policy", "group_policy", "group_require_mention", "processing_indicator",
)
_LIST_KEYS = ("allow_from", "group_allow_from", "group_sender_allow_from")


@dataclass
class ResolvedTelexAccount:
    account_id: str
    enabled: bool
    configured: bool
    api_key: str
    base_url: str
    bot_id: str | None
    dm_policy: str
    allow_from: list[str]
    group_policy: str
    group_allow_from: list[str]
    group_sender_allow_from: list[str]
    group_require_mention: bool
    processing_indicator: str
    tools: dict[str, bool] = field(default_factory=cfg.default_tools)


def list_account_ids(extra: dict) -> list[str]:
    accounts = extra.get("accounts") if isinstance(extra, dict) else None
    if isinstance(accounts, dict) and accounts:
        return list(accounts.keys())
    return [DEFAULT_ACCOUNT_ID]


def _merge(base: dict, account: dict | None) -> dict:
    merged = dict(base)
    if isinstance(account, dict):
        for k, v in account.items():
            if v is not None:
                merged[k] = v
    return merged


def resolve_account(extra: dict, account_id: str) -> ResolvedTelexAccount:
    """Merge ``accounts.<id>`` over any top-level base and apply defaults.

    Voyager's canonical config nests every account (including ``default``) under
    ``extra.accounts.<id>``; a bare top-level ``extra`` (no ``accounts``) is also
    accepted as the default account. So an ``accounts`` entry — for ANY id,
    including ``default`` — takes precedence over top-level base fields.
    """
    base = {k: v for k, v in (extra or {}).items() if k != "accounts"}
    accounts = (extra or {}).get("accounts") or {}
    account = accounts.get(account_id)
    merged = _merge(base, account)

    api_key = str(merged.get("api_key", "") or "").strip()
    # enabled defaults True; explicit False disables.
    enabled = merged.get("enabled", True) is not False

    return ResolvedTelexAccount(
        account_id=account_id,
        enabled=enabled,
        configured=bool(api_key),
        api_key=api_key,
        base_url=str(merged.get("base_url") or cfg.DEFAULTS["base_url"]).rstrip("/"),
        bot_id=(str(merged["bot_id"]).strip() if merged.get("bot_id") else None),
        dm_policy=str(merged.get("dm_policy") or cfg.DEFAULTS["dm_policy"]).lower(),
        allow_from=cfg._as_list(merged.get("allow_from")),
        group_policy=str(merged.get("group_policy") or cfg.DEFAULTS["group_policy"]).lower(),
        group_allow_from=cfg._as_list(merged.get("group_allow_from")),
        group_sender_allow_from=cfg._as_list(merged.get("group_sender_allow_from")),
        group_require_mention=cfg._as_bool(
            merged.get("group_require_mention"), cfg.DEFAULTS["group_require_mention"]
        ),
        processing_indicator=str(
            merged.get("processing_indicator") or cfg.DEFAULTS["processing_indicator"]
        ).lower(),
        tools=cfg.coerce_tools(merged.get("tools")),
    )


def resolve_all_accounts(extra: dict) -> list[ResolvedTelexAccount]:
    return [resolve_account(extra, aid) for aid in list_account_ids(extra)]


def list_enabled_accounts(extra: dict) -> list[ResolvedTelexAccount]:
    return [a for a in resolve_all_accounts(extra) if a.enabled and a.configured]
