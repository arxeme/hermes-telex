"""T-03 blocks, target parsing, T-06 chunking + fake-client send."""

import pytest

from hermes_telex import blocks, send, targets
from hermes_telex.types import BlockType, MessageFlag


def test_media_blocks():
    m = {"data": {"blocks": [
        {"seq": 0, "type": BlockType.IMAGE, "media": {"file_id": "f1", "name": "p.png"}},
        {"seq": 1, "type": BlockType.FILE, "media": {"file_id": "f2"}},
        {"seq": 2, "type": BlockType.TEXT, "text": "hi"},
    ]}}
    mb = blocks.media_blocks(m)
    assert [x["kind"] for x in mb] == ["image", "document"]


def test_mentions():
    assert blocks.mentions({"data": {"mention_all": True}}, "bot1") is True
    assert blocks.mentions({"data": {"mention_ids": ["bot1"]}}, "bot1") is True
    assert blocks.mentions({"data": {"mention_ids": ["x"]}}, "bot1") is False


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


@pytest.mark.parametrize("sender, flags, self_id, answerable", [
    ("u1", 0, "bot1", True),
    ("u1", MessageFlag.FORK_PREFIX, "bot1", False),
    ("bot1", 0, "bot1", False),
    ("u1", 0, None, False),
])
def test_answer_ids_only_when_answerable(sender, flags, self_id, answerable):
    interaction = {"id": "env", "mode": 1, "questions": [{
        "text": "Which env?", "allow_free_text": True,
        "options": [{"id": "stg", "label": "staging"}, {"id": "prd", "label": "production"}]}]}
    m = {"id": "m1", "sender_id": sender, "flags": flags,
         "data": {"blocks": [{"seq": 0, "type": BlockType.INTERACTION, "interaction": interaction}]}}
    text = blocks.message_text(m, self_id, direct=True)
    assert "staging" in text
    assert ('interaction_id="env"' in text) is answerable
    assert ('<question free_text="true">' in text) is answerable


def test_interactions_keep_block_order():
    q1, q2 = ({"id": qid, "mode": 1, "questions": [{"options": [{"id": "a", "label": "A"}]}]} for qid in ("q1", "q2"))
    m = {"id": "m1", "sender_id": "u1", "flags": 0, "data": {"blocks": [
        {"seq": 3, "type": BlockType.INTERACTION, "interaction": q2},
        {"seq": 0, "type": BlockType.TEXT, "text": "First "},
        {"seq": 1, "type": BlockType.TEXT, "text": "note"},
        {"seq": 2, "type": BlockType.INTERACTION, "interaction": q1},
        {"seq": 4, "type": BlockType.TEXT, "text": "Last note"},
    ]}}
    markup = [f'<interaction message_id="m1" interaction_id="{qid}" status="open">\n<question>\n- a: A\n</question>\n'
              "</interaction>" for qid in ("q1", "q2")]
    assert blocks.message_text(m, "bot1", direct=True) == "\n".join(["First note", *markup, "Last note"])


def test_answered_and_multi_select_interactions_markup():
    answered = {"id": "env", "mode": 1, "status": 1, "answerer_id": "u1", "questions": [{
        "text": "Which env?", "options": [{"id": "stg", "label": "staging"}, {"id": "prd", "label": "production"}],
        "answer": {"option_ids": ["stg", "gone"], "text": "asap"}}]}
    open_multi = {"id": "pick", "mode": 1, "questions": [{
        "text": "Pick", "multi_select": True, "options": [{"id": "a", "label": "A"}]}]}
    m = {"id": "m1", "sender_id": "u1", "flags": 0, "data": {"blocks": [
        {"seq": 0, "type": BlockType.INTERACTION, "interaction": answered},
        {"seq": 1, "type": BlockType.INTERACTION, "interaction": open_multi},
    ]}}
    assert blocks.message_text(m, "bot1", direct=True) == "\n".join([
        '<interaction status="answered" answerer="u1">', "<question>", "Which env?", "- staging", "- production",
        "answer: staging, gone, asap", "</question>", "</interaction>",
        '<interaction message_id="m1" interaction_id="pick" status="open">', '<question multi_select="true">',
        "Pick", "- a: A", "</question>", "</interaction>",
    ])


def test_lapsed_open_interaction_reads_expired():
    interaction = {"id": "env", "mode": 1, "deadline": "2020-01-01T00:00:00Z",
                   "questions": [{"options": [{"id": "stg", "label": "staging"}]}]}
    m = {"id": "m1", "sender_id": "u1", "flags": 0,
         "data": {"blocks": [{"seq": 0, "type": BlockType.INTERACTION, "interaction": interaction}]}}
    text = blocks.message_text(m, "bot1", direct=True)
    assert 'status="expired"' in text and "interaction_id" not in text
    interaction["deadline"] = "2999-01-01T00:00:00Z"
    assert 'interaction_id="env"' in blocks.message_text(m, "bot1", direct=True)
