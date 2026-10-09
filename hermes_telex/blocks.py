"""Telex block construction and parsing (message ``data.blocks``)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .types import (
    BlockType,
    INTERACTION_STATUS_LABELS,
    InteractionStatus,
    MEDIA_BLOCK_TYPES,
    MessageFlag,
)


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


def interaction_block(interaction: dict[str, Any]) -> dict[str, Any]:
    return {"type": BlockType.INTERACTION, "interaction": interaction}


def tool_block(tool_id: str, name: str, status: int, input_: dict, output: dict) -> dict[str, Any]:
    return {
        "type": BlockType.TOOL,
        "tool": {"id": tool_id, "name": name, "status": status, "input": input_, "output": output},
    }


# -- inbound parsing -------------------------------------------------------

def _sorted_blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    blocks = ((message.get("data") or {}).get("blocks")) or []
    return sorted(blocks, key=lambda b: b.get("seq", 0))


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


def mentions(message: dict[str, Any], self_id: str | None) -> bool:
    data = message.get("data") or {}
    return bool(data.get("mention_all")) or (bool(self_id) and self_id in (data.get("mention_ids") or []))


def _interaction_markup(message_id: str, interaction: dict[str, Any], self_id: str | None, asked: bool) -> str:
    status = interaction.get("status", InteractionStatus.OPEN)
    deadline = interaction.get("deadline")
    # Telex expires an interaction lazily, so one past its deadline can still read OPEN.
    if status == InteractionStatus.OPEN and deadline and datetime.fromisoformat(deadline) <= datetime.now(timezone.utc):
        status = InteractionStatus.EXPIRED
    answerer_ids = interaction.get("answerer_ids") or []
    answerable = status == InteractionStatus.OPEN and (self_id in answerer_ids or (asked and not answerer_ids))
    attrs: dict[str, Any] = {}
    if answerable:
        attrs["message_id"] = message_id
        attrs["interaction_id"] = interaction.get("id", "")
    attrs["status"] = INTERACTION_STATUS_LABELS.get(status, status)
    if interaction.get("answerer_id"):
        attrs["answerer"] = interaction["answerer_id"]
    lines = [f"<interaction {_markup_attrs(attrs)}>"]
    for question in interaction.get("questions") or []:
        flags = {}
        if answerable and question.get("multi_select"):
            flags["multi_select"] = "true"
        if answerable and question.get("allow_free_text"):
            flags["free_text"] = "true"
        lines.append(f"<question {_markup_attrs(flags)}>" if flags else "<question>")
        if question.get("text"):
            lines.append(question["text"])
        labels = {}
        for option in question.get("options") or []:
            label = option.get("label", "")
            labels[option.get("id")] = label
            line = f"- {option.get('id')}: {label}" if answerable else f"- {label}"
            if option.get("description"):
                line += f" ({option['description']})"
            lines.append(line)
        answer = question.get("answer") or {}
        chosen = [labels.get(option_id, option_id) for option_id in answer.get("option_ids") or []]
        if answer.get("text"):
            chosen.append(answer["text"])
        if chosen:
            lines.append("answer: " + ", ".join(chosen))
        lines.append("</question>")
    lines.append("</interaction>")
    return "\n".join(lines)


def _markup_attrs(attrs: dict[str, Any]) -> str:
    return " ".join(f'{k}="{v}"' for k, v in attrs.items())


def message_text(message: dict[str, Any], self_id: str | None, *, direct: bool) -> str:
    """A Chat message asks its one other member; in a Channel a mention does."""
    asked = direct or mentions(message, self_id)
    # Telex refuses answers on a fork's copy, and the agent's own question is for the person it asked.
    if not self_id or message.get("flags", 0) & MessageFlag.FORK_PREFIX or message.get("sender_id") == self_id:
        self_id, asked = None, False
    parts, text = [], ""
    for b in _sorted_blocks(message):
        if b.get("type") == BlockType.TEXT:
            text += b.get("text", "")
        elif b.get("type") == BlockType.INTERACTION and b.get("interaction"):
            parts += [text, _interaction_markup(message.get("id", ""), b["interaction"], self_id, asked)]
            text = ""
    parts.append(text)
    return "\n".join(part for part in parts if part)


def event(message: dict[str, Any]) -> dict[str, Any]:
    for b in _sorted_blocks(message):
        if b.get("type") == BlockType.EVENT and b.get("event"):
            return b["event"]
    return {}
