"""CommandDispatcher gating: verb, stop latch, readiness, available_commands, recording."""

import io

import pytest

from game_mcp.config import Settings
from game_mcp.safety import HeldInputs, StopLatch
from sts_bot.dispatch import CommandDispatcher
from sts_bot.protocol import CommLink, Message
from sts_bot.recorder import RunRecorder, read_events
from sts_bot.state import Snapshot


@pytest.fixture
def setup(tmp_path, combat_state_dict, quiet_release):
    out = io.BytesIO()
    link = CommLink(io.BytesIO(), out)
    latch = StopLatch(HeldInputs(), Settings())
    recorder = RunRecorder(tmp_path, run_id="t")
    snap = Snapshot.from_message(Message(combat_state_dict, "utf-8"))
    return CommandDispatcher(link, latch, recorder), out, latch, recorder, snap


def test_allowed_command_is_written_and_recorded(setup):
    dispatcher, out, _, recorder, snap = setup
    result = dispatcher.send("play 1 0", snap, origin="test")
    assert result.ok and result.command == "play 1 0"
    assert out.getvalue() == b"play 1 0\n"
    events = read_events(recorder.path)
    assert events[-1]["kind"] == "command" and events[-1]["data"]["origin"] == "test"


def test_unknown_verb_is_rejected_without_writing(setup):
    dispatcher, out, _, recorder, snap = setup
    result = dispatcher.send("fly away", snap)
    assert not result.ok and "未知的指令動詞" in result.reason
    assert out.getvalue() == b""
    assert read_events(recorder.path)[-1]["kind"] == "rejected"


def test_command_outside_available_commands_is_rejected(setup):
    dispatcher, out, _, _, snap = setup
    result = dispatcher.send("choose 0", snap)
    assert not result.ok and "不在目前可用指令內" in result.reason
    assert out.getvalue() == b""


def test_not_ready_state_blocks_actions_but_not_state_query(setup, combat_state_dict):
    dispatcher, out, _, _, _ = setup
    combat_state_dict["ready_for_command"] = False
    snap = Snapshot.from_message(Message(combat_state_dict, "utf-8"))
    assert not dispatcher.send("end", snap).ok
    assert dispatcher.send("state", snap).ok
    assert out.getvalue() == b"state\n"


def test_no_snapshot_yet_blocks_actions(setup):
    dispatcher, out, _, _, _ = setup
    result = dispatcher.send("end", None)
    assert not result.ok and "還沒收到任何遊戲狀態" in result.reason
    assert dispatcher.send("wait 30", None).ok


def test_stop_latch_blocks_actions_and_operator_stop_survives_resume(setup):
    dispatcher, out, latch, _, snap = setup
    latch.engage("tool", "test")
    result = dispatcher.send("end", snap)
    assert not result.ok and "輸入已停止（按鈕／工具）" in result.reason
    assert dispatcher.send("state", snap).ok, "read-only verbs pass while stopped"
    latch.resume()
    assert dispatcher.send("end", snap).ok

    latch.engage("operator", "scroll lock")
    assert not dispatcher.send("end", snap).ok
    with pytest.raises(Exception):
        latch.resume()
    assert out.getvalue() == b"state\nend\n"


def test_write_failure_is_reported_not_raised(setup, monkeypatch):
    dispatcher, _, _, _, snap = setup

    def boom(_line):
        raise OSError("pipe closed")

    monkeypatch.setattr(dispatcher._link, "send", boom)
    result = dispatcher.send("end", snap)
    assert not result.ok and "寫入失敗" in result.reason
