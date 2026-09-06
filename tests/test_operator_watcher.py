import time

import pytest

from game_mcp.config import Settings
from game_mcp.errors import ErrorCode, GameMcpError
from game_mcp.safety import HeldInputs, OperatorWatcher, StopLatch
from game_mcp.vendor import win32_input as w32


@pytest.fixture
def quiet_release(monkeypatch):
    monkeypatch.setattr(w32, "ReleaseMouse", lambda button, waitTime=0: None)
    monkeypatch.setattr(w32, "ReleaseKey", lambda vk, waitTime=0: None)


def test_emergency_key_engages_operator_stop(monkeypatch, quiet_release):
    settings = Settings(emergency_key="pause", corner_failsafe=False)
    latch = StopLatch(HeldInputs(), settings)
    watcher = OperatorWatcher(latch, settings)
    monkeypatch.setattr(w32, "KeyWasPressed", lambda vk: vk == int(w32.Keys.VK_PAUSE))
    assert watcher.poll() is True
    state = latch.state()
    assert state.stopped and state.origin == "operator"
    with pytest.raises(GameMcpError) as exc:
        latch.resume()
    assert exc.value.code == ErrorCode.STOPPED


def test_corner_failsafe_detects_top_left(monkeypatch, quiet_release):
    settings = Settings(emergency_key="pause", corner_failsafe=True)
    watcher = OperatorWatcher(StopLatch(HeldInputs(), settings), settings)
    monkeypatch.setattr(w32, "KeyWasPressed", lambda vk: False)
    monkeypatch.setattr(w32, "GetCursorPos", lambda: (0, 0))
    assert "corner" in watcher._check_once()
    monkeypatch.setattr(w32, "GetCursorPos", lambda: (5, 0))
    assert watcher._check_once() is None


def test_corner_failsafe_disabled(monkeypatch, quiet_release):
    settings = Settings(emergency_key="pause", corner_failsafe=False)
    watcher = OperatorWatcher(StopLatch(HeldInputs(), settings), settings)
    monkeypatch.setattr(w32, "KeyWasPressed", lambda vk: False)
    monkeypatch.setattr(w32, "GetCursorPos", lambda: (0, 0))
    assert watcher._check_once() is None


@pytest.mark.integration
def test_watcher_thread_latches_on_real_corner_cursor(quiet_release):
    """Moves the physical cursor to (0,0); the running watcher must latch within ~200 ms."""
    settings = Settings(emergency_key="pause", corner_failsafe=True)
    latch = StopLatch(HeldInputs(), settings)
    watcher = OperatorWatcher(latch, settings)
    before = w32.GetCursorPos()
    watcher.start()
    try:
        w32.SetCursorPos(0, 0)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not latch.state().stopped:
            time.sleep(0.02)
        assert latch.state().stopped and latch.state().origin == "operator"
        assert watcher.triggered_count == 1
    finally:
        watcher.stop()
        w32.SetCursorPos(*before)
