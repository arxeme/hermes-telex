"""T-02/T-04/T-05 client logic: self-echo, watermark, dedup, subscribe stream."""

import pytest

from hermes_telex.client import TelexClient, TelexError


def _c():
    return TelexClient("k", "https://t", bot_id="bot1")


def test_self_echo_via_bot_id_and_sent_cache():
    c = _c()
    assert c.is_own_message({"id": "x", "sender_id": "bot1"}) is True
    assert c.is_own_message({"id": "y", "sender_id": "other"}) is False
    c.record_sent({"id": "z", "sender_id": "bot2"})  # learned self id wins
    assert c.self_id == "bot2"
    assert c.is_own_message({"id": "z", "sender_id": "someoneelse"}) is True  # sent-id cache


def test_dedup_by_settle():
    # Dedup is now cursor/settled based (the monitor gates on is_disposed).
    c = _c()
    c.seed_conversation("conv", 0, 0)
    assert c.is_disposed("conv", 5) is False
    c.settle("conv", 5)
    assert c.is_disposed("conv", 5) is True


def test_unseeded_conversation_is_disposed():
    # Nothing is replayed for a conversation that was never seeded from the
    # server read cursor — otherwise a restart would re-dispatch history.
    c = _c()
    assert c.is_disposed("never-seeded", 10) is True


def test_seed_from_server_read_cursor():
    c = _c()
    c.seed_conversation("conv", cursor=7, max_seen=9)
    assert c.get_cursor("conv") == 7
    assert c.is_disposed("conv", 7) is True    # at/below the read cursor
    assert c.is_disposed("conv", 8) is False   # unread
    assert c.is_lagging("conv") is True        # cursor 7 < max_seen 9


def test_cursor_advance_prunes_settled_and_poison():
    c = _c()
    c.seed_conversation("conv", 0, 0)
    c.settle("conv", 3)
    c.bump_poison("conv", 4)
    c.update_cursor("conv", 5)                 # cursor passes both
    assert c.get_cursor("conv") == 5
    assert c.is_disposed("conv", 3) is True    # below cursor
    assert c.poison_count("conv", 4) == 0      # pruned
    assert c.is_disposed("conv", 6) is False


def test_poison_counts_up():
    c = _c()
    c.seed_conversation("conv", 0, 0)
    assert c.bump_poison("conv", 9) == 1
    assert c.bump_poison("conv", 9) == 2
    assert c.poison_count("conv", 9) == 2


def test_turn_seq():
    c = _c()
    c.note_turn_seq("conv", 3)
    c.note_turn_seq("conv", 2)
    assert c.get_last_turn_seq("conv") == 3


def test_session_recreated_per_event_loop():
    # Regression: tool registry runs handlers in fresh event loops; a session
    # bound to another loop must not be reused ("Timeout context manager
    # should be used inside a task").
    import asyncio
    import warnings

    c = _c()

    async def grab():
        return c._get_session()

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # unclosed-session noise from loop 1
        s1 = asyncio.run(grab())
        s2 = asyncio.run(grab())
    assert s1 is not s2


class _Stream:
    def __init__(self, chunks, content_type="text/event-stream"):
        self.status = 200
        self.headers = {"Content-Type": content_type}
        self.content = self
        self._chunks = chunks
        self.request_headers = None

    def get(self, url, headers, timeout):
        self.request_headers = headers
        return self

    async def iter_any(self):
        for chunk in self._chunks:
            yield chunk

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


async def test_subscribe_reads_server_sent_events():
    c = _c()
    stream = _Stream([
        b": ping\n\n",
        b'data: {"result": {"message": null}}\r\n\r\n',
        b'data: {"result":\ndata: {"message": {"id": "m1", "text": "caf\xc3',
        b'\xa9"}}}\n\n',
    ])
    c._get_session = lambda: stream
    events, activity = [], []

    await c.subscribe(events.append, on_activity=lambda: activity.append(1))

    assert stream.request_headers["Accept"] == "text/event-stream"
    assert events == [{"message": None}, {"message": {"id": "m1", "text": "caf\u00e9"}}]
    assert len(activity) == 4


async def test_subscribe_raises_a_stream_error_event():
    c = _c()
    c._get_session = lambda: _Stream([b'data: {"error": {"code": 16, "message": "unauthenticated"}}\n\n'])

    with pytest.raises(TelexError) as raised:
        await c.subscribe(lambda event: None)

    assert raised.value.api_message == "unauthenticated"


async def test_subscribe_refuses_a_stream_that_is_not_server_sent_events():
    c = _c()
    c._get_session = lambda: _Stream([b'{"result": {"message": null}}\n'], content_type="application/json")
    events = []

    with pytest.raises(TelexError):
        await c.subscribe(events.append)

    assert events == []
