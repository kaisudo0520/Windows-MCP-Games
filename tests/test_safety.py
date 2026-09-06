import pytest

from game_mcp.config import Settings
from game_mcp.errors import ErrorCode, GameMcpError
from game_mcp.safety import HeldInputs, StopLatch
from game_mcp.vendor import win32_input as w32


@pytest.fixture
def released(monkeypatch):
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(w32, "ReleaseMouse", lambda button, waitTime=0: calls.append(("mouse", button)))
    monkeypatch.setattr(w32, "ReleaseKey", lambda vk, waitTime=0: calls.append(("key", vk)))
    return calls


def test_release_all_releases_only_what_was_held(released):
    held = HeldInputs()
    held.note_button_down("left")
    held.note_key_down(0x45)
    held.note_key_down(0x31)
    held.note_key_up(0x31)
    result = held.release_all()
    assert result == {"buttons": ["left"], "keys": [0x45], "failed": []}
    assert sorted(released) == [("key", 0x45), ("mouse", "left")]
    assert held.snapshot() == {"buttons": [], "keys": []}


def test_tool_stop_and_resume(released):
    latch = StopLatch(HeldInputs(), Settings())
    assert not latch.state().stopped
    state = latch.engage("tool", "test")
    assert state.stopped and state.origin == "tool"
    with pytest.raises(GameMcpError) as exc:
        latch.require_running()
    assert exc.value.code == ErrorCode.STOPPED
    assert not latch.resume().stopped
    latch.require_running()


def test_engage_releases_held_inputs(released):
    held = HeldInputs()
    held.note_button_down("right")
    latch = StopLatch(held, Settings())
    latch.engage("tool", "test")
    assert ("mouse", "right") in released
    assert held.snapshot()["buttons"] == []


def test_operator_stop_cannot_be_resumed_by_tool_by_default(released):
    latch = StopLatch(HeldInputs(), Settings(allow_tool_resume_after_operator_stop=False))
    latch.engage("operator", "pause key")
    with pytest.raises(GameMcpError) as exc:
        latch.resume()
    assert exc.value.code == ErrorCode.STOPPED
    assert latch.state().stopped and latch.state().origin == "operator"


def test_operator_stop_resume_when_explicitly_allowed(released):
    latch = StopLatch(HeldInputs(), Settings(allow_tool_resume_after_operator_stop=True))
    latch.engage("operator", "pause key")
    assert not latch.resume().stopped


def test_tool_stop_does_not_downgrade_operator_stop(released):
    latch = StopLatch(HeldInputs(), Settings())
    latch.engage("operator", "corner")
    state = latch.engage("tool", "later")
    assert state.origin == "operator"
