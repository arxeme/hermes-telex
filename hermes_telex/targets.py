"""Telex target string parsing/normalization.

send_message targets (after the ``telex:`` platform prefix is stripped by the
gateway):
  - ``<conversation_id>``   (16-hex)         -> conversation_id
  - ``peer/<identity_id>``                   -> peer_id
  - ``email/<email>`` or ``<email>``         -> resolve to peer_id (adapter)
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HEX16 = re.compile(r"^[0-9a-fA-F]{16}$")


def normalize_target(raw: str | None) -> str | None:
    if not raw:
        return None
    s = raw.strip()
    return s or None


def looks_like_id(s: str) -> bool:
    return bool(_HEX16.match(s.strip()))


@dataclass
class TelexTarget:
    kind: str  # "conversation" | "peer" | "email"
    value: str


def parse_target(chat_id: str) -> TelexTarget | None:
    s = normalize_target(chat_id)
    if not s:
        return None
    if s.startswith("peer/"):
        return TelexTarget("peer", s[len("peer/"):])
    if s.startswith("email/"):
        return TelexTarget("email", s[len("email/"):])
    if "@" in s and not looks_like_id(s):
        return TelexTarget("email", s)
    return TelexTarget("conversation", s)
