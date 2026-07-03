"""Outbound message composition (port of openclaw-telex send.ts / outbound.ts).

One-shot COMPLETED delivery (openclaw does not stream): text is chunked for
readability; text + media are combined into a single multi-block message.
"""

from __future__ import annotations

from typing import Any

from . import blocks as blk
from . import media as media_mod
from .log import get_logger

logger = get_logger("outbound")

# Well under Telex's 1 MiB data cap; accounts for CJK + JSON overhead.
DEFAULT_TEXT_CHUNK_LIMIT = 200_000


def chunk_text(text: str, limit: int) -> list[str]:
    """Split text into <=limit chunks, preferring newline then space boundaries."""
    if len(text) <= limit:
        return [text] if text else []
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        cut = window.rfind("\n")
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut < limit // 2:
            cut = limit
        chunks.append(rest[:cut])
        rest = rest[cut:]
    if rest:
        chunks.append(rest)
    return chunks


async def _upload_media_block(client, path: str, kind: str) -> dict[str, Any] | None:
    """Upload a local file and return a media block, or None on failure."""
    try:
        data, name, mime = media_mod.read_outbound_file(path)
        media = await client.upload_file(name, mime, data)
        return blk.media_block(
            kind, media.get("file_id", ""), media.get("name", name),
            media.get("size", 0), media.get("mime", mime),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("outbound media upload failed path=%s: %s", path, exc)
        return None


async def send_telex_message(
    client,
    *,
    conversation_id: str | None = None,
    peer_id: str | None = None,
    text: str | None = None,
    media_units: list[tuple[str, str]] | None = None,  # [(path, kind)]
    mention_ids: list[str] | None = None,
    chunk_limit: int = DEFAULT_TEXT_CHUNK_LIMIT,
) -> dict[str, Any] | None:
    """Send text (chunked) and/or media. Returns the last message dict sent."""
    media_units = media_units or []
    last: dict[str, Any] | None = None

    if media_units:
        # One multi-block message: [text?, media...].
        message_blocks: list[dict[str, Any]] = []
        if text:
            message_blocks.append(blk.text_block(text))
        for path, kind in media_units:
            mb = await _upload_media_block(client, path, kind)
            message_blocks.append(mb if mb else blk.text_block(f"[attachment unavailable: {path}]"))
        return await client.send_message(
            conversation_id=conversation_id, peer_id=peer_id,
            blocks=message_blocks, mention_ids=mention_ids,
        )

    for chunk in chunk_text(text or "", chunk_limit):
        last = await client.send_message(
            conversation_id=conversation_id, peer_id=peer_id,
            blocks=[blk.text_block(chunk)], mention_ids=mention_ids,
        )
        # Mentions only need to ride the first chunk.
        mention_ids = None
    return last
