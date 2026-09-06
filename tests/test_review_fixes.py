"""Regression tests for the findings of the first review round."""

import pytest

from game_mcp import win32
from game_mcp.config import Settings
from game_mcp.errors import ErrorCode, GameMcpError
from game_mcp.observation import ObservationStore
from game_mcp.safety import HeldInputs, OperatorWatcher, StopLatch
from game_mcp.vendor import win32_input as w32

RECT = win32.ScreenRect(100, 100, 700, 500)


@pytest.fixture
def quiet_release(monkeypatch):
    monkeypatch.setattr(w32, "ReleaseMouse", lambda button, waitTime=0: None)
    monkeypatch.setattr(w32, "ReleaseKey", lambda vk, waitTime=0: None)


def test_operator_stop_upgrades_a_pending_tool_stop(monkeypatch, quiet_release):
    """Review finding 1: Pause/corner during stop_input must not be swallowed."""
    settings = Settings(emergency_key="scrolllock", corner_failsafe=False)
    latch = StopLatch(HeldInputs(), settings)
    watcher = OperatorWatcher(latch, settings)
    latch.engage("tool", "ai asked")
    monkeypatch.setattr(w32, "KeyWasPressed", lambda vk: vk == int(w32.Keys.VK_SCROLL))
    assert watcher.poll() is True
    assert latch.state().origin == "operator"
    with pytest.raises(GameMcpError) as exc:
        latch.resume()
    assert exc.value.code == ErrorCode.STOPPED
    # a second poll while already operator-stopped is a no-op
    assert watcher.poll() is False
    assert watcher.triggered_count == 1


def test_release_all_keeps_entries_whose_release_failed(monkeypatch):
    held = HeldInputs()
    held.note_key_down(0x45)
    held.note_button_down("left")

    def boom(*_a, **_k):
        raise OSError("keybd_event failed")

    monkeypatch.setattr(w32, "ReleaseKey", boom)
    monkeypatch.setattr(w32, "ReleaseMouse", lambda button, waitTime=0: None)
    result = held.release_all()
    assert result["buttons"] == ["left"]
    assert result["failed"] == [0x45]
    assert held.snapshot() == {"buttons": [], "keys": [0x45]}


def test_unknown_emergency_key_is_rejected_at_startup():
    settings = Settings(emergency_key="f11")
    with pytest.raises(ValueError) as exc:
        OperatorWatcher(StopLatch(HeldInputs(), settings), settings)
    assert "f11" in str(exc.value)


def test_dead_handle_raises_oserror_not_pywintypes():
    with pytest.raises(OSError):
        win32.client_rect_on_screen(0x1)
    with pytest.raises(OSError):
        win32.window_rect(0x1)


def test_observation_for_another_window_is_stale():
    store = ObservationStore()
    obs = store.issue(hwnd=11, client_rect=RECT, dpi=96, image_size=(600, 400), scale=1.0, ttl_s=30)
    with pytest.raises(GameMcpError) as exc:
        store.require(obs.id, client_rect_now=RECT, hwnd_now=22)
    assert exc.value.code == ErrorCode.OBSERVATION_STALE
    assert exc.value.details["reason"] == "rebound"
    assert store.current() is None


def test_key_was_pressed_reads_bit0_and_bit15(monkeypatch):
    calls = []

    def fake_state(vk):
        calls.append(vk)
        return 0x0001  # pressed since last call, not currently down

    monkeypatch.setattr(w32._user32, "GetAsyncKeyState", fake_state)
    assert w32.KeyWasPressed(0x91) is True
    assert w32.IsKeyPressed(0x91) is False
    assert calls == [0x91, 0x91]
