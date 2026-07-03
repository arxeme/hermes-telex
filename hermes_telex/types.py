"""Telex wire-protocol enums and constants (mirror voyager telex.proto).

Telex JSON emits enums as integers and snake_case field names; messages are
passed around as plain dicts (like openclaw-telex's TS interfaces), accessed
with these constants.
"""

from __future__ import annotations

OPENAPI_PREFIX = "/voyager/v1/openapi/telex"
DEFAULT_BASE_URL = "https://voyager.ingarena.net"


class BlockType:
    TEXT = 1
    IMAGE = 2
    VIDEO = 3
    AUDIO = 4
    FILE = 5
    THINKING = 11
    TOOL = 12
    EVENT = 21


class ConversationKind:
    CHAT = 0
    CHANNEL = 1


class MemberRole:
    MEMBER = 0
    ADMIN = 1
    OWNER = 2


class MessageStatus:
    COMPLETED = 0
    IN_PROGRESS = 1
    ERROR = 2
    ABORTED = 3


class MessageFlag:
    NONE = 0
    EVENT = 1
    EDITED = 2
    FORK_PREFIX = 4


class IdentityKind:
    USER = 0
    MATE_INSTANCE = 1
    BOT = 2


class ToolStatus:
    IN_PROGRESS = 0
    SUCCESS = 1
    ERROR = 2
    ABORTED = 3


# Media block types keyed by inbound "kind".
MEDIA_BLOCK_TYPES = {
    "image": BlockType.IMAGE,
    "video": BlockType.VIDEO,
    "audio": BlockType.AUDIO,
    "document": BlockType.FILE,
}

# Human-readable labels for tool output (enum int -> lowercase label).
MESSAGE_STATUS_LABELS = {0: "completed", 1: "in_progress", 2: "error", 3: "aborted"}
CONVERSATION_KIND_LABELS = {0: "chat", 1: "channel"}
MEMBER_ROLE_LABELS = {0: "member", 1: "admin", 2: "owner"}
IDENTITY_KIND_LABELS = {0: "user", 1: "mate_instance", 2: "bot"}
TOOL_STATUS_LABELS = {0: "in_progress", 1: "success", 2: "error", 3: "aborted"}


def message_flag_labels(flags: int) -> list[str]:
    """Expand a message flags bitmask into a label list."""
    out: list[str] = []
    if flags & MessageFlag.EVENT:
        out.append("event")
    if flags & MessageFlag.EDITED:
        out.append("edited")
    if flags & MessageFlag.FORK_PREFIX:
        out.append("fork_prefix")
    return out
