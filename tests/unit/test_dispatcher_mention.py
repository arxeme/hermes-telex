"""Leading self-mention stripping: the gateway detects a command only at the start of the text."""

from hermes_telex.dispatcher import _strip_leading_self_mention as strip

BOT = "503599c2d96937e3"


def test_strips_the_bots_own_leading_mention():
    assert strip(f"[@Bot](mention:{BOT}) /reset", BOT) == "/reset"
    assert strip(f"(mention:{BOT}) /reset", BOT) == "/reset"
    # A display name carrying "]" reaches the client backslash-escaped.
    assert strip(rf"[@A\]B](mention:{BOT}) /reset", BOT) == "/reset"


def test_keeps_a_mention_of_someone_else():
    other = f"[@Alice](mention:00000000000186a4) hello"
    assert strip(other, BOT) == other


def test_keeps_mention_all_and_inline_mentions():
    assert strip("[@all](mention:all) ping", BOT) == "[@all](mention:all) ping"
    assert strip(f"ping [@Bot](mention:{BOT})", BOT) == f"ping [@Bot](mention:{BOT})"


def test_no_self_id_or_empty_text_is_a_no_op():
    assert strip(f"[@Bot](mention:{BOT}) /reset", None) == f"[@Bot](mention:{BOT}) /reset"
    assert strip("", BOT) == ""
