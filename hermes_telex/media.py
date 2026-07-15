"""Inbound media download (→ hermes cache) and outbound file preparation.

Port of openclaw-telex media.ts, adapted to hermes: inbound blocks are
downloaded via the OpenAPI file endpoint and cached with the base adapter's
``cache_*_from_bytes`` helpers; outbound media arrives as local file paths
(the gateway extracts ``MEDIA:<path>`` before ``send``), so we just read + cap.
"""

from __future__ import annotations

import mimetypes
import os
from dataclasses import dataclass
from typing import Any

from .log import get_logger

logger = get_logger("media")

MAX_MEDIA_BYTES = 20 * 1024 * 1024  # 20 MiB (server upload cap)

# hermes base-adapter cache helpers (fallback to a temp dir outside hermes).
try:  # pragma: no cover - exercised inside hermes
    from gateway.platforms.base import (
        cache_audio_from_bytes,
        cache_document_from_bytes,
        cache_image_from_bytes,
        cache_video_from_bytes,
    )
    _HAS_CACHE = True
except Exception:  # pragma: no cover
    _HAS_CACHE = False

    import tempfile

    def _tmp(data: bytes, suffix: str) -> str:
        fd, path = tempfile.mkstemp(suffix=suffix)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return path

    def cache_image_from_bytes(data: bytes, ext: str = ".jpg") -> str:  # type: ignore
        return _tmp(data, ext)

    def cache_video_from_bytes(data: bytes, ext: str = ".mp4") -> str:  # type: ignore
        return _tmp(data, ext)

    def cache_audio_from_bytes(data: bytes, ext: str = ".ogg") -> str:  # type: ignore
        return _tmp(data, ext)

    def cache_document_from_bytes(data: bytes, filename: str) -> str:  # type: ignore
        return _tmp(data, os.path.splitext(filename)[1] or ".bin")


_PLACEHOLDER = {"image": "image", "video": "video", "audio": "audio", "document": "file"}


@dataclass
class InboundMedia:
    path: str
    content_type: str
    kind: str  # image | video | audio | document
    name: str

    def placeholder(self) -> str:
        return f"[{_PLACEHOLDER.get(self.kind, 'file')}: {self.name or self.kind}]"


def _ext_for(name: str, content_type: str, kind: str) -> str:
    ext = os.path.splitext(name)[1]
    if ext:
        return ext
    guessed = mimetypes.guess_extension(content_type.split(";")[0].strip()) if content_type else None
    if guessed:
        return guessed
    return {"image": ".jpg", "video": ".mp4", "audio": ".ogg", "document": ".bin"}.get(kind, ".bin")


def media_markdown_link(client, entry: dict[str, Any]) -> str:
    media = entry.get("media") or {}
    file_id = media.get("file_id")
    kind = entry.get("kind", "document")
    name = media.get("name") or kind
    if not file_id:
        return f"[{_PLACEHOLDER.get(kind, 'file')}: {name}]"
    url = client.file_download_url(file_id)
    return f"![{name}]({url})" if kind == "image" else f"[{name}]({url})"


async def resolve_inbound_media(client, media_blocks: list[dict[str, Any]]) -> tuple[list[InboundMedia], list[str]]:
    out: list[InboundMedia] = []
    links: list[str] = []
    for entry in media_blocks:
        kind = entry["kind"]
        media = entry["media"] or {}
        file_id = media.get("file_id")
        if not file_id:
            continue
        name = media.get("name") or file_id
        try:
            data, content_type = await client.download_file(file_id)
        except Exception as exc:  # noqa: BLE001 - don't block dispatch on media failure
            logger.warning("inbound media download failed file_id=%s: %s", file_id, exc)
            links.append(media_markdown_link(client, entry))
            continue
        if len(data) > MAX_MEDIA_BYTES:
            logger.warning("inbound media too large file_id=%s size=%d", file_id, len(data))
            links.append(media_markdown_link(client, entry))
            continue
        mime = media.get("mime") or content_type
        ext = _ext_for(name, mime, kind)
        try:
            if kind == "image":
                path = cache_image_from_bytes(data, ext)
            elif kind == "video":
                path = cache_video_from_bytes(data, ext)
            elif kind == "audio":
                path = cache_audio_from_bytes(data, ext)
            else:
                path = cache_document_from_bytes(data, name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("caching inbound media failed file_id=%s: %s", file_id, exc)
            links.append(media_markdown_link(client, entry))
            continue
        out.append(InboundMedia(path=path, content_type=mime, kind=kind, name=name))
    return out, links


def read_outbound_file(path: str) -> tuple[bytes, str, str]:
    """Read a local file for upload; returns (bytes, name, mime). Enforces cap."""
    real = os.path.expanduser(path)
    size = os.path.getsize(real)
    if size > MAX_MEDIA_BYTES:
        raise ValueError(f"attachment too large ({size} bytes > {MAX_MEDIA_BYTES})")
    with open(real, "rb") as f:
        data = f.read()
    name = os.path.basename(real)
    mime = mimetypes.guess_type(real)[0] or "application/octet-stream"
    return data, name, mime
