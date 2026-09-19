"""CommunicationMod line protocol: handshake, framing, encoding, command validation."""

import io
import json

import pytest

from sts_bot.protocol import (
    READY,
    CommLink,
    ProtocolError,
    candidate_encodings,
    decode_line,
    normalize_command,
    parse_message,
)


def _link(stdin_bytes: bytes = b"", encoding: str | None = None) -> tuple[CommLink, io.BytesIO]:
    out = io.BytesIO()
    return CommLink(io.BytesIO(stdin_bytes), out, encoding=encoding), out


def test_handshake_writes_ready_line_and_flushes():
    link, out = _link()
    link.handshake()
    assert out.getvalue() == READY


def test_read_message_skips_blank_lines_and_returns_none_on_eof(combat_state_line):
    link, _ = _link(b"\n   \n" + combat_state_line)
    msg = link.read_message()
    assert msg is not None and msg.in_game and msg.ready_for_command
    assert msg.available_commands == ("play", "end", "potion", "key", "click", "wait", "state")
    assert link.read_message() is None
    assert link.lines_read == 1


def test_error_reply_is_a_message_with_error_field():
    line = b'{"error":"Invalid command","ready_for_command":true}\n'
    link, _ = _link(line)
    msg = link.read_message()
    assert msg is not None
    assert msg.error == "Invalid command"
    assert msg.game_state is None
    assert not msg.in_game


def test_non_json_line_raises_protocol_error_but_link_stays_usable(combat_state_line):
    link, _ = _link(b"garbage\n" + combat_state_line)
    with pytest.raises(ProtocolError):
        link.read_message()
    assert link.read_message() is not None


def test_decode_prefers_utf8_then_falls_back_to_locale_codepage():
    text = '{"name":"打擊"}'
    assert decode_line(text.encode("utf-8"), ["utf-8", "cp950"]) == (text, "utf-8")
    assert decode_line(text.encode("cp950"), ["utf-8", "cp950"]) == (text, "cp950")
    lossy, enc = decode_line(b"\xff\xfe\xfd", ["utf-8"])
    assert enc == "utf-8+replace" and "�" in lossy


def test_candidate_encodings_dedupes_and_honours_override():
    encs = candidate_encodings("cp950")
    assert encs[0] == "cp950"
    assert len(encs) == len({e.lower() for e in encs})


def test_send_normalizes_whitespace_and_replies_in_detected_encoding():
    payload = ('{"in_game":false,"ready_for_command":true,"available_commands":["start"]}\n').encode("cp950")
    link, out = _link(payload)
    link.read_message()
    assert link.last_encoding in {"utf-8", "cp950"}  # ASCII payload decodes as utf-8 first
    sent = link.send("  PLAY   1  0 ")
    assert sent == "PLAY 1 0"
    assert out.getvalue() == b"PLAY 1 0\n"
    assert link.commands_sent == 1


@pytest.mark.parametrize("bad", ["", "   ", "explode", "run end"])
def test_normalize_rejects_unknown_or_empty_verbs(bad):
    with pytest.raises(ProtocolError):
        normalize_command(bad)


def test_normalize_collapses_embedded_newlines_into_one_line():
    # A newline inside a command would otherwise be two commands on the wire.
    assert normalize_command("choose\n0") == "choose 0"


def test_parse_message_rejects_non_object_json():
    with pytest.raises(ProtocolError):
        parse_message(json.dumps([1, 2, 3]))
