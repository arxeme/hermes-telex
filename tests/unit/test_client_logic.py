"""T-02/T-04/T-05 client pure logic: self-echo, watermark, dedup, mention."""

from hermes_telex.client import TelexClient


def _c():
    return TelexClient("k", "https://t", bot_id="bot1")


def test_self_echo_via_bot_id_and_sent_cache():
    c = _c()
    assert c.is_own_message({"id": "x", "sender_id": "bot1"}) is True
    assert c.is_own_message({"id": "y", "sender_id": "other"}) is False
    c.record_sent({"id": "z", "sender_id": "bot2"})  # learned self id wins
    assert c.self_id == "bot2"
    assert c.is_own_message({"id": "z", "sender_id": "someoneelse"}) is True  # sent-id cache


def test_dedup():
    c = _c()
    assert c.mark_processed("m1") is True
    assert c.mark_processed("m1") is False


def test_watermark_backfill_targets_skip_pending():
    c = _c()
    c.note_message("conv", 5, terminal=True)
    c.note_message("conv", 6, terminal=False)   # in-progress: cursor must stay below it
    c.note_message("conv", 7, terminal=True)
    targets = {t["conversation_id"]: t["after_seq"] for t in c.get_backfill_targets()}
    assert targets["conv"] == 5   # clamped below pending seq 6


def test_first_seq_floors_below_history():
    c = _c()
    c.note_message("conv", 10, terminal=True)   # first seen frame
    assert {t["conversation_id"]: t["after_seq"] for t in c.get_backfill_targets()}["conv"] == 10


def test_self_mentioned():
    c = _c()
    assert c.is_self_mentioned({"data": {"mention_all": True}}) is True
    assert c.is_self_mentioned({"data": {"mention_ids": ["bot1"]}}) is True
    assert c.is_self_mentioned({"data": {"mention_ids": ["x"]}}) is False


def test_turn_seq():
    c = _c()
    c.note_turn_seq("conv", 3)
    c.note_turn_seq("conv", 2)
    assert c.get_last_turn_seq("conv") == 3
