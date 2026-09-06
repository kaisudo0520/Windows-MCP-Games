"""Debug-image retention and IME auto-toggle."""

import os
import time

import pytest

from game_mcp import win32
from game_mcp.capture import prune_debug_dir
from game_mcp.config import Settings
from game_mcp.errors import ErrorCode, GameMcpError
from game_mcp.input_service import InputService
from game_mcp.observation import ObservationStore
from game_mcp.safety import HeldInputs, StopLatch
from game_mcp.validation import WindowState
from game_mcp.vendor import win32_input as w32


def _touch(path: str, age_s: float) -> None:
    with open(path, "wb") as fh:
        fh.write(b"x")
    stamp = time.time() - age_s
    os.utime(path, (stamp, stamp))


def test_prune_by_age_and_count(tmp_path):
    d = str(tmp_path)
    for i in range(6):
        _touch(os.path.join(d, f"old{i}.png"), age_s=3 * 3600)
    for i in range(4):
        _touch(os.path.join(d, f"new{i}.png"), age_s=i)  # newest last
    _touch(os.path.join(d, "notes.txt"), age_s=9 * 3600)
    settings = Settings(debug_dir=d, debug_keep_files=3, debug_max_age_h=1.0)
    removed = prune_debug_dir(settings, by_age=True, by_count=False)
    assert removed == 6
    assert sorted(os.listdir(d)) == ["new0.png", "new1.png", "new2.png", "new3.png", "notes.txt"]
    removed = prune_debug_dir(settings, by_age=False, by_count=True)
    assert removed == 1
    assert "new3.png" not in os.listdir(d)  # the oldest of the four is gone
    assert "notes.txt" in os.listdir(d)  # non-images are never touched


def test_prune_is_noop_without_debug_dir(tmp_path):
    assert prune_debug_dir(Settings(debug_dir=None), by_age=True, by_count=True) == 0
    assert prune_debug_dir(Settings(debug_dir=str(tmp_path / "missing")), by_age=True, by_count=True) == 0


class _FakeIme:
    def __init__(self, states):
        self.states = list(states)
        self.calls = 0

    def __call__(self, hwnd):
        self.calls += 1
        native = self.states.pop(0) if self.states else False
        return win32.ImeState(1, True, 1 if native else 0, "0x4040404")


@pytest.fixture
def press_env(monkeypatch):
    """Stub everything around press_key so only the IME logic runs."""
    taps: list[int] = []
    monkeypatch.setattr(w32, "PressKey", lambda vk, waitTime=0: taps.append(vk))
    monkeypatch.setattr(w32, "ReleaseKey", lambda vk, waitTime=0: None)
    monkeypatch.setattr(win32, "foreground_window", lambda: 42)
    state = WindowState(42, win32.ScreenRect(0, 0, 100, 100), 96, 42, 0.0, True, 0.0)
    import game_mcp.input_service as mod

    monkeypatch.setattr(mod, "validate_window", lambda *a, **k: state)
    return taps


def _svc(settings: Settings):
    store = ObservationStore()
    obs = store.issue(hwnd=42, client_rect=win32.ScreenRect(0, 0, 100, 100), dpi=96, image_size=(100, 100), scale=1.0, ttl_s=30)
    held = HeldInputs()
    return InputService(settings, store, held, StopLatch(held, settings)), obs, held


def test_ime_native_triggers_shift_tap_then_sends_key(press_env, monkeypatch):
    fake = _FakeIme([True, False])  # native before the tap, alphanumeric after
    monkeypatch.setattr(win32, "ime_state", fake)
    svc, obs, held = _svc(Settings(key_whitelist=["e"]))
    from game_mcp.binding import Binding

    binding = Binding(42, 1, "x.exe", "", 0.0, "t")
    result = svc.press_key(binding, "e", obs.id)
    assert press_env == [int(w32.Keys.VK_SHIFT), 0x45]
    assert result["ime"]["shift_tapped"] is True and result["ime"]["native_open"] is False
    assert fake.calls == 2
    assert held.snapshot() == {"buttons": [], "keys": []}


def test_ime_alphanumeric_sends_key_without_shift(press_env, monkeypatch):
    monkeypatch.setattr(win32, "ime_state", _FakeIme([False]))
    svc, obs, _ = _svc(Settings(key_whitelist=["e"]))
    from game_mcp.binding import Binding

    result = svc.press_key(Binding(42, 1, "x.exe", "", 0.0, "t"), "e", obs.id)
    assert press_env == [0x45]
    assert result["ime"]["shift_tapped"] is False


def test_ime_still_native_after_tap_is_reported(press_env, monkeypatch):
    monkeypatch.setattr(win32, "ime_state", _FakeIme([True, True]))
    svc, obs, _ = _svc(Settings(key_whitelist=["e"]))
    from game_mcp.binding import Binding

    with pytest.raises(GameMcpError) as exc:
        svc.press_key(Binding(42, 1, "x.exe", "", 0.0, "t"), "e", obs.id)
    assert exc.value.code == ErrorCode.IME_ACTIVE
    assert exc.value.details["shift_tapped"] is True
    assert press_env == [int(w32.Keys.VK_SHIFT)]  # the game key itself was never sent


def test_ime_autotoggle_disabled(press_env, monkeypatch):
    monkeypatch.setattr(win32, "ime_state", _FakeIme([True]))
    svc, obs, _ = _svc(Settings(key_whitelist=["e"], ime_autotoggle=False))
    from game_mcp.binding import Binding

    with pytest.raises(GameMcpError) as exc:
        svc.press_key(Binding(42, 1, "x.exe", "", 0.0, "t"), "e", obs.id)
    assert exc.value.code == ErrorCode.IME_ACTIVE
    assert press_env == []
