"""Config defaults, coercion, validation, and env translation.

The canonical config is YAML under ``platforms.telex.extra`` (a translation of
openclaw-telex's ``channels.telex``); a single-account env quickstart is also
supported and seeded into ``extra`` by the ``env_enablement_fn`` hook.
"""

from __future__ import annotations

import os

from .types import DEFAULT_BASE_URL

DM_POLICIES = {"open", "allowlist", "pairing"}
GROUP_POLICIES = {"disabled", "allowlist", "open"}
PROCESSING_INDICATORS = {"activity", "off"}

TOOL_KEYS = (
    "search_identities",
    "get_identities",
    "list_conversations",
    "get_conversation_info",
    "create_channel",
    "list_members",
    "add_members",
    "get_conversation_messages",
)

DEFAULTS = {
    "base_url": DEFAULT_BASE_URL,
    "dm_policy": "allowlist",
    "group_policy": "disabled",
    "group_require_mention": True,
    "processing_indicator": "activity",
}


class TelexConfigError(ValueError):
    """Raised when config fails validation (e.g. dm_policy=open without '*')."""


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [p.strip() for p in value.replace("\n", ",").replace(";", ",").split(",") if p.strip()]
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


def _as_bool(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def default_tools() -> dict[str, bool]:
    return {k: True for k in TOOL_KEYS}


def coerce_tools(raw) -> dict[str, bool]:
    tools = default_tools()
    if isinstance(raw, dict):
        for k in TOOL_KEYS:
            if k in raw:
                tools[k] = _as_bool(raw[k], True)
    return tools


def validate_account(cfg: dict, account_id: str = "default") -> None:
    """Validate a resolved account dict; raise TelexConfigError on problems."""
    dm = str(cfg.get("dm_policy", DEFAULTS["dm_policy"])).lower()
    if dm not in DM_POLICIES:
        raise TelexConfigError(f"[{account_id}] dm_policy must be one of {sorted(DM_POLICIES)}")
    gp = str(cfg.get("group_policy", DEFAULTS["group_policy"])).lower()
    if gp not in GROUP_POLICIES:
        raise TelexConfigError(f"[{account_id}] group_policy must be one of {sorted(GROUP_POLICIES)}")
    pi = str(cfg.get("processing_indicator", DEFAULTS["processing_indicator"])).lower()
    if pi not in PROCESSING_INDICATORS:
        raise TelexConfigError(f"[{account_id}] processing_indicator must be one of {sorted(PROCESSING_INDICATORS)}")
    if dm == "open":
        allow = _as_list(cfg.get("allow_from"))
        if "*" not in allow:
            raise TelexConfigError(
                f'[{account_id}] dm_policy="open" requires allow_from to include "*"'
            )


def config_from_env() -> dict | None:
    """Build a single-account ``extra`` dict from TELEX_* env vars.

    Returns None when TELEX_API_KEY is unset. Used by ``env_enablement_fn``.
    """
    api_key = os.getenv("TELEX_API_KEY", "").strip()
    if not api_key:
        return None
    extra: dict = {
        "api_key": api_key,
        "base_url": os.getenv("TELEX_BASE_URL", "").strip() or DEFAULT_BASE_URL,
    }
    if os.getenv("TELEX_BOT_ID"):
        extra["bot_id"] = os.getenv("TELEX_BOT_ID", "").strip()
    if os.getenv("TELEX_DM_POLICY"):
        extra["dm_policy"] = os.getenv("TELEX_DM_POLICY", "").strip().lower()
    if os.getenv("TELEX_ALLOW_FROM"):
        extra["allow_from"] = _as_list(os.getenv("TELEX_ALLOW_FROM"))
    if os.getenv("TELEX_GROUP_POLICY"):
        extra["group_policy"] = os.getenv("TELEX_GROUP_POLICY", "").strip().lower()
    if os.getenv("TELEX_GROUP_ALLOW_FROM"):
        extra["group_allow_from"] = _as_list(os.getenv("TELEX_GROUP_ALLOW_FROM"))
    if os.getenv("TELEX_GROUP_SENDER_ALLOW_FROM"):
        extra["group_sender_allow_from"] = _as_list(os.getenv("TELEX_GROUP_SENDER_ALLOW_FROM"))
    if os.getenv("TELEX_GROUP_REQUIRE_MENTION"):
        extra["group_require_mention"] = _as_bool(os.getenv("TELEX_GROUP_REQUIRE_MENTION"), True)
    if os.getenv("TELEX_PROCESSING_INDICATOR"):
        extra["processing_indicator"] = os.getenv("TELEX_PROCESSING_INDICATOR", "").strip().lower()
    return extra
