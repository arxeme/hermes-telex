"""Access control: DM and channel policies (port of openclaw-telex access.ts
plus the DM decision from bot.ts).

The plugin enforces open/allowlist and all channel policies itself
(``enforces_own_access_policy=True``). ``dm_policy="pairing"`` is delegated to
the hermes-agent gateway's built-in pairing handshake — for pairing the
dispatcher forwards the DM instead of pre-filtering it.
"""

from __future__ import annotations

from dataclasses import dataclass


def is_sender_allowed(sender_id: str, email: str | None, allow_from: list[str]) -> bool:
    """Shared allowlist rule: trim; "*" allows all; exact id match;
    case-insensitive email match. (Port of isTelexSenderAllowed.)"""
    for entry in allow_from:
        e = entry.strip()
        if e == "*":
            return True
        if e == sender_id:
            return True
        if email and e.lower() == email.lower():
            return True
    return False


@dataclass
class GroupAccess:
    allowed: bool
    reason: str | None = None


def check_group_access(
    *,
    group_policy: str,
    group_allow_from: list[str] | None,
    group_sender_allow_from: list[str] | None,
    conversation_id: str,
    sender_id: str,
    sender_email: str | None,
) -> GroupAccess:
    """Port of access.ts::checkGroupAccess (require_mention handled separately)."""
    if group_policy == "disabled":
        return GroupAccess(False, "groupPolicy is disabled")

    if group_policy == "allowlist":
        allow = group_allow_from or []
        if conversation_id not in allow:
            return GroupAccess(False, f"channel {conversation_id} not in group_allow_from")

    if group_sender_allow_from:
        if not is_sender_allowed(sender_id, sender_email, group_sender_allow_from):
            return GroupAccess(False, f"sender {sender_id} not in group_sender_allow_from")

    return GroupAccess(True)


# DM decision values.
DM_ALLOW = "allow"      # dispatch; gateway trusts (enforces_own_access_policy)
DM_DENY = "deny"        # drop before dispatch
DM_PAIRING = "pairing"  # forward to gateway; it runs the pairing handshake


def check_dm_access(
    *,
    dm_policy: str,
    allow_from: list[str] | None,
    sender_id: str,
    sender_email: str | None,
) -> str:
    """Decide a DM: DM_ALLOW / DM_DENY / DM_PAIRING.

    - open:      allow (config validation guarantees "*" in allow_from).
    - allowlist: allow if the sender matches allow_from, else deny.
    - pairing:   allow if pre-listed in allow_from, else DM_PAIRING (the gateway
                 consults its pairing store / issues a pairing code).
    """
    allow = allow_from or []
    if dm_policy == "open":
        return DM_ALLOW
    if dm_policy == "allowlist":
        return DM_ALLOW if is_sender_allowed(sender_id, sender_email, allow) else DM_DENY
    if dm_policy == "pairing":
        if is_sender_allowed(sender_id, sender_email, allow):
            return DM_ALLOW
        return DM_PAIRING
    # Unknown policy: deny by default.
    return DM_DENY
