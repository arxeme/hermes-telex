"""Telex block construction and parsing (message ``data.blocks``)."""

from __future__ import annotations

from typing import Any

from .types import BlockType, MEDIA_BLOCK_TYPES


# -- outbound construction -------------------------------------------------

def text_block(text: str) -> dict[str, Any]:
    return {"type": BlockType.TEXT, "text": text}


def thinking_block(text: str) -> dict[str, Any]:
    return {"type": BlockType.THINKING, "text": text}


def media_block(kind: str, file_id: str, name: str = "", size: int = 0, mime: str = "") -> dict[str, Any]:
    media: dict[str, Any] = {"file_id": file_id}
    if name:
        media["name"] = name
    if size:
        media["size"] = size
    if mime:
        media["mime"] = mime
    return {"type": MEDIA_BLOCK_TYPES.get(kind, BlockType.FILE), "media": media}


def image_block(file_id: str, name: str = "") -> dict[str, Any]:
    return media_block("image", file_id, name)


def file_block(file_id: str, name: str = "", size: int = 0, mime: str = "") -> dict[str, Any]:
    return media_block("document", file_id, name, size, mime)


def tool_block(tool_id: str, name: str, status: int, input_: dict, output: dict) -> dict[str, Any]:
    return {
        "type": BlockType.TOOL,
        "tool": {"id": tool_id, "name": name, "status": status, "input": input_, "output": output},
    }


# -- inbound parsing -------------------------------------------------------

def _sorted_blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    blocks = ((message.get("data") or {}).get("blocks")) or []
    return sorted(blocks, key=lambda b: b.get("seq", 0))


def extract_text(message: dict[str, Any]) -> str:
    """Concatenate all TEXT blocks (by seq) into the message body."""
    parts = [b.get("text", "") for b in _sorted_blocks(message) if b.get("type") == BlockType.TEXT]
    return "".join(parts)


_MEDIA_KINDS = {
    BlockType.IMAGE: "image",
    BlockType.VIDEO: "video",
    BlockType.AUDIO: "audio",
    BlockType.FILE: "document",
}


def media_blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Return [{kind, media}] for each IMAGE/VIDEO/AUDIO/FILE block."""
    out = []
    for b in _sorted_blocks(message):
        kind = _MEDIA_KINDS.get(b.get("type"))
        if kind and b.get("media"):
            out.append({"kind": kind, "media": b["media"]})
    return out
