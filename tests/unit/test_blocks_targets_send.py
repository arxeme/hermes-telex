"""T-03 blocks, target parsing, T-06 chunking + fake-client send."""

from hermes_telex import blocks, send, targets
from hermes_telex.types import BlockType


def test_extract_text_ordered():
    m = {"data": {"blocks": [{"seq": 1, "type": 1, "text": "b"}, {"seq": 0, "type": 1, "text": "a"}]}}
    assert blocks.extract_text(m) == "ab"


def test_media_blocks():
    m = {"data": {"blocks": [
        {"seq": 0, "type": BlockType.IMAGE, "media": {"file_id": "f1", "name": "p.png"}},
        {"seq": 1, "type": BlockType.FILE, "media": {"file_id": "f2"}},
        {"seq": 2, "type": BlockType.TEXT, "text": "hi"},
    ]}}
    mb = blocks.media_blocks(m)
    assert [x["kind"] for x in mb] == ["image", "document"]


def test_build_blocks():
    assert blocks.text_block("hi")["type"] == BlockType.TEXT
    assert blocks.image_block("f", "n")["type"] == BlockType.IMAGE
    assert blocks.file_block("f", "n")["type"] == BlockType.FILE


def test_parse_target():
    assert targets.parse_target("0a1b2c3d4e5f6071").kind == "conversation"
    assert targets.parse_target("peer/abc").kind == "peer"
    assert targets.parse_target("email/a@b.com").value == "a@b.com"
    assert targets.parse_target("a@b.com").kind == "email"
    assert targets.parse_target("  ") is None


def test_chunk_text():
    assert send.chunk_text("", 10) == []
    assert send.chunk_text("short", 10) == ["short"]
    parts = send.chunk_text("x" * 25, 10)
    assert "".join(parts) == "x" * 25 and all(len(p) <= 10 for p in parts)


async def test_send_text_chunks(tmp_path):
    from tests.conftest import StubClient
    c = StubClient()
    await send.send_telex_message(c, conversation_id="conv", text="y" * 25, chunk_limit=10)
    assert len(c.sent) == 3
    assert all(s["conversation_id"] == "conv" for s in c.sent)


async def test_send_media_single_message(tmp_path):
    from tests.conftest import StubClient
    p = tmp_path / "pic.png"
    p.write_bytes(b"imgdata")
    c = StubClient()
    await send.send_telex_message(c, conversation_id="conv", text="caption",
                                  media_units=[(str(p), "image")], chunk_limit=100)
    assert len(c.sent) == 1
    blocks_sent = c.sent[0]["blocks"]
    assert blocks_sent[0]["type"] == BlockType.TEXT
    assert blocks_sent[1]["type"] == BlockType.IMAGE
    assert c.uploaded == [("pic.png", "image/png")]
