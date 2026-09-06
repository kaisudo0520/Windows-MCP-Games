"""Boundary tests against a controllable tkinter window standing in for the game.

These move the real cursor and click inside the test window, so they are marked
``integration``. They never touch any other application. Run with
``uv run pytest -m integration``.
"""

from __future__ import annotations

import time
import tkinter as tk

import pytest
import win32gui

from game_mcp import win32
from game_mcp.binding import bind, list_candidates
from game_mcp.capture import capture_game_screen
from game_mcp.config import Settings
from game_mcp.errors import ErrorCode, GameMcpError
from game_mcp.input_service import InputService
from game_mcp.observation import ObservationStore
from game_mcp.safety import HeldInputs, StopLatch
from game_mcp.validation import validate_window

pytestmark = pytest.mark.integration

TITLE = "gamemcp-test-window"
W, H = 640, 360


def _settings(**overrides) -> Settings:
    base = dict(
        process_names=["python.exe", "pythonw.exe", "python3.12.exe"],
        title_substrings=[TITLE],
        path_substrings=["__never__"],
        key_whitelist=["e", "1", "enter"],
        observation_ttl_s=20.0,
        occlusion_grid_px=20,
        corner_failsafe=False,
    )
    base.update(overrides)
    return Settings(**base)


class ProbeWindow:
    def __init__(self, title: str = TITLE, x: int = 300, y: int = 200):
        self.root = tk.Tk()
        self.root.title(title)
        self.root.geometry(f"{W}x{H}+{x}+{y}")
        self.root.configure(bg="#3050a0")
        self.clicks: list[tuple[int, int, int]] = []
        self.keys: list[tuple[str, int]] = []
        self.root.bind("<Button-1>", lambda e: self.clicks.append((1, e.x, e.y)))
        self.root.bind("<Button-3>", lambda e: self.clicks.append((3, e.x, e.y)))
        # keycode 0xE5 (VK_PROCESSKEY) means an IME swallowed the translation but the
        # keystroke did reach the window; zh-TW layouts do this to plain Tk windows.
        self.root.bind("<Key>", lambda e: self.keys.append((e.keysym.lower(), e.keycode)))
        self.pump(0.3)
        self.hwnd = int(self.root.winfo_id())
        self.hwnd = win32.root_window(self.hwnd)
        # Detach the IME from the probe window: on zh-TW systems a fresh Tk window starts
        # in native mode and would swallow injected keys (see win32.ime_state).
        import ctypes

        imm = ctypes.windll.imm32
        imm.ImmGetContext.restype = ctypes.c_void_p
        himc = imm.ImmGetContext(ctypes.c_void_p(self.hwnd))
        if himc:
            imm.ImmSetOpenStatus(ctypes.c_void_p(himc), 0)
            imm.ImmReleaseContext(ctypes.c_void_p(self.hwnd), ctypes.c_void_p(himc))

    def pump(self, seconds: float = 0.1) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.root.update()
            time.sleep(0.01)

    def bring_to_front(self) -> bool:
        self.root.lift()
        self.root.focus_force()
        self.pump(0.1)
        try:
            win32gui.SetForegroundWindow(self.hwnd)
        except Exception:
            pass
        self.pump(0.2)
        return win32.foreground_window() == self.hwnd

    def close(self) -> None:
        try:
            self.root.destroy()
        except tk.TclError:
            pass


@pytest.fixture
def window():
    win = ProbeWindow()
    yield win
    win.close()


@pytest.fixture
def focused_window(window):
    if not window.bring_to_front():
        pytest.skip("could not bring the test window to the foreground (foreground lock)")
    return window


def _bound(window: ProbeWindow, settings: Settings):
    binding = bind(settings, None)
    assert binding.hwnd == window.hwnd
    return binding


def _service(settings: Settings):
    store = ObservationStore()
    held = HeldInputs()
    latch = StopLatch(held, settings)
    return InputService(settings, store, held, latch), store, latch


# --------------------------------------------------------------------- binding


def test_candidate_discovery_uses_process_identity(window):
    settings = _settings()
    cands = list_candidates(settings)
    assert [c.hwnd for c in cands] == [window.hwnd]
    assert cands[0].exe_name.lower() in settings.process_names
    binding = _bound(window, settings)
    assert binding.pid == win32.window_pid(window.hwnd)


def test_ambiguous_candidates_require_explicit_hwnd(window):
    other = ProbeWindow(title=TITLE + "-2", x=1000, y=200)
    try:
        settings = _settings()
        with pytest.raises(GameMcpError) as exc:
            bind(settings, None)
        assert exc.value.code == ErrorCode.AMBIGUOUS_CANDIDATES
        assert len(exc.value.details["candidates"]) == 2
        assert bind(settings, other.hwnd).hwnd == other.hwnd
        with pytest.raises(GameMcpError) as exc:
            bind(settings, 12345)
        assert exc.value.code == ErrorCode.CANDIDATE_NOT_FOUND
    finally:
        other.close()


def test_no_candidates():
    with pytest.raises(GameMcpError) as exc:
        bind(_settings(title_substrings=["definitely-not-a-window"]), None)
    assert exc.value.code == ErrorCode.NO_CANDIDATES


def test_window_gone_after_close(window):
    settings = _settings()
    binding = _bound(window, settings)
    window.close()
    time.sleep(0.2)
    with pytest.raises(GameMcpError) as exc:
        validate_window(binding, settings, require_foreground=False)
    assert exc.value.code == ErrorCode.WINDOW_GONE


# --------------------------------------------------------------------- capture


def test_capture_matches_client_area(focused_window):
    settings = _settings()
    binding = _bound(focused_window, settings)
    store = ObservationStore()
    result = capture_game_screen(binding, settings, store)
    rect = win32.client_rect_on_screen(focused_window.hwnd)
    assert result.observation.image_size == (rect.width, rect.height)
    assert result.png[:8] == b"\x89PNG\r\n\x1a\n"
    assert store.current().id == result.observation.id
    assert set(result.timing_ms) >= {"validate", "occlusion_check", "capture", "encode", "total"}


def test_capture_refused_when_not_foreground(window):
    settings = _settings()
    binding = _bound(window, settings)
    other = ProbeWindow(title="unrelated-window", x=1000, y=200)
    try:
        if not other.bring_to_front():
            pytest.skip("could not move focus to the second window")
        with pytest.raises(GameMcpError) as exc:
            capture_game_screen(binding, settings, ObservationStore())
        assert exc.value.code == ErrorCode.NOT_FOREGROUND
        assert exc.value.details["foreground_hwnd"] == other.hwnd
    finally:
        other.close()


def test_capture_refused_when_occluded(focused_window):
    settings = _settings()
    binding = _bound(focused_window, settings)
    cover = tk.Toplevel(focused_window.root)
    cover.title("cover")
    cover.geometry(f"200x120+{300 + 100}+{200 + 100}")
    cover.attributes("-topmost", True)
    focused_window.pump(0.3)
    try:
        # the cover is owned by the test window and may have taken focus; put focus back
        win32gui.SetForegroundWindow(focused_window.hwnd)
        focused_window.pump(0.2)
        if win32.foreground_window() != focused_window.hwnd:
            pytest.skip("cover stole focus and it could not be returned")
        with pytest.raises(GameMcpError) as exc:
            capture_game_screen(binding, settings, ObservationStore())
        assert exc.value.code == ErrorCode.OCCLUDED
        assert exc.value.details["occluder"]["hwnd"] == win32.root_window(int(cover.winfo_id()))
    finally:
        cover.destroy()


def test_capture_refused_when_minimized(focused_window):
    settings = _settings()
    binding = _bound(focused_window, settings)
    focused_window.root.iconify()
    focused_window.pump(0.3)
    try:
        with pytest.raises(GameMcpError) as exc:
            capture_game_screen(binding, settings, ObservationStore())
        assert exc.value.code == ErrorCode.MINIMIZED
    finally:
        focused_window.root.deiconify()
        focused_window.pump(0.2)


def test_capture_refused_when_partly_offscreen(focused_window):
    settings = _settings()
    binding = _bound(focused_window, settings)
    vs = win32.virtual_screen()
    focused_window.root.geometry(f"{W}x{H}+{vs.right - W // 2}+{200}")
    focused_window.pump(0.3)
    try:
        with pytest.raises(GameMcpError) as exc:
            capture_game_screen(binding, settings, ObservationStore())
        assert exc.value.code == ErrorCode.OFFSCREEN
    finally:
        focused_window.root.geometry(f"{W}x{H}+300+200")
        focused_window.pump(0.2)


# ----------------------------------------------------------------------- input


def test_click_is_delivered_to_the_window_and_consumes_observation(focused_window):
    settings = _settings()
    binding = _bound(focused_window, settings)
    svc, store, _latch = _service(settings)
    obs = capture_game_screen(binding, settings, store).observation
    result = svc.click(binding, 50, 40, obs.id)
    focused_window.pump(0.3)
    assert result["ok"] is True
    assert result["post_action"]["focus_kept"] is True
    assert result["post_action"]["cursor_at_target"] is True
    assert focused_window.clicks[-1] == (1, 50, 40)
    assert set(result["timing_ms"]) == {
        "validate", "precheck_to_input_gap", "injection_window", "input", "post_verify", "total"
    }
    # the same observation cannot be reused
    with pytest.raises(GameMcpError) as exc:
        svc.click(binding, 50, 40, obs.id)
    assert exc.value.code == ErrorCode.OBSERVATION_STALE
    assert exc.value.details["reason"] == "input_performed"


def test_right_click(focused_window):
    settings = _settings()
    binding = _bound(focused_window, settings)
    svc, store, _ = _service(settings)
    obs = capture_game_screen(binding, settings, store).observation
    svc.click(binding, 60, 70, obs.id, button="right")
    focused_window.pump(0.3)
    assert focused_window.clicks[-1] == (3, 60, 70)


def test_move_mouse_hover(focused_window):
    settings = _settings()
    binding = _bound(focused_window, settings)
    svc, store, _ = _service(settings)
    obs = capture_game_screen(binding, settings, store).observation
    result = svc.move_mouse(binding, 100, 100, obs.id)
    rect = win32.client_rect_on_screen(focused_window.hwnd)
    assert result["screen_point"] == [rect.left + 100, rect.top + 100]
    assert result["post_action"]["cursor_at_target"] is True
    assert focused_window.clicks == []


def test_press_whitelisted_key_reaches_window(focused_window):
    settings = _settings()
    binding = _bound(focused_window, settings)
    svc, store, _ = _service(settings)
    obs = capture_game_screen(binding, settings, store).observation
    result = svc.press_key(binding, "e", obs.id)
    focused_window.pump(0.3)
    assert result["ok"] and result["vk"] == 0x45
    assert focused_window.keys, "no key event reached the window"
    keysym, keycode = focused_window.keys[-1]
    assert keysym == "e" or keycode in (0x45, 0xE5)


def test_click_out_of_bounds_sends_nothing(focused_window):
    settings = _settings()
    binding = _bound(focused_window, settings)
    svc, store, _ = _service(settings)
    obs = capture_game_screen(binding, settings, store).observation
    with pytest.raises(GameMcpError) as exc:
        svc.click(binding, W + 5, 10, obs.id)
    assert exc.value.code == ErrorCode.INVALID_COORDINATES
    focused_window.pump(0.2)
    assert focused_window.clicks == []
    # a rejected action does not consume the observation
    assert store.current().id == obs.id


def test_click_refused_after_window_moved(focused_window):
    settings = _settings()
    binding = _bound(focused_window, settings)
    svc, store, _ = _service(settings)
    obs = capture_game_screen(binding, settings, store).observation
    focused_window.root.geometry(f"{W}x{H}+320+200")
    focused_window.pump(0.3)
    with pytest.raises(GameMcpError) as exc:
        svc.click(binding, 10, 10, obs.id)
    assert exc.value.code == ErrorCode.OBSERVATION_STALE
    assert exc.value.details["reason"] == "window_moved_or_resized"
    assert focused_window.clicks == []


def test_click_refused_when_focus_lost(focused_window):
    settings = _settings()
    binding = _bound(focused_window, settings)
    svc, store, _ = _service(settings)
    obs = capture_game_screen(binding, settings, store).observation
    other = ProbeWindow(title="unrelated-window", x=1000, y=200)
    try:
        if not other.bring_to_front():
            pytest.skip("could not move focus away")
        with pytest.raises(GameMcpError) as exc:
            svc.click(binding, 10, 10, obs.id)
        assert exc.value.code == ErrorCode.NOT_FOREGROUND
        focused_window.pump(0.2)
        assert focused_window.clicks == []
    finally:
        other.close()


def test_click_refused_when_target_point_is_covered(focused_window):
    settings = _settings(occlusion_grid_px=400)  # coarse grid so only the point check can catch it
    binding = _bound(focused_window, settings)
    svc, store, _ = _service(settings)
    obs = capture_game_screen(binding, settings, store).observation
    rect = win32.client_rect_on_screen(focused_window.hwnd)
    cover = tk.Toplevel(focused_window.root)
    cover.geometry(f"60x60+{rect.left + 30}+{rect.top + 30}")  # covers client (30..90, 30..90)
    cover.attributes("-topmost", True)
    cover.overrideredirect(True)  # no focus change
    focused_window.pump(0.3)
    try:
        if win32.foreground_window() != focused_window.hwnd:
            pytest.skip("cover stole focus")
        with pytest.raises(GameMcpError) as exc:
            svc.click(binding, 60, 60, obs.id)
        assert exc.value.code == ErrorCode.OCCLUDED
        assert focused_window.clicks == []
    finally:
        cover.destroy()


def test_capture_pixels_come_from_the_probe_window(focused_window):
    import io

    from PIL import Image

    settings = _settings()
    binding = _bound(focused_window, settings)
    result = capture_game_screen(binding, settings, ObservationStore())
    image = Image.open(io.BytesIO(result.png)).convert("RGB")
    w, h = image.size
    # background is #3050a0 = (48, 80, 160); sample the center and a point near each corner
    for px in [(w // 2, h // 2), (5, 5), (w - 6, 5), (5, h - 6), (w - 6, h - 6)]:
        assert image.getpixel(px) == (48, 80, 160), (px, image.getpixel(px))
    assert result.warnings == []


def test_capture_refused_by_zorder_when_cover_is_disabled(focused_window):
    """A disabled window is invisible to WindowFromPoint; the z-order walk must catch it."""
    settings = _settings()
    binding = _bound(focused_window, settings)
    rect = win32.client_rect_on_screen(focused_window.hwnd)
    cover = tk.Toplevel(focused_window.root)
    cover.overrideredirect(True)
    cover.attributes("-topmost", True)
    cover.geometry(f"100x100+{rect.left + 200}+{rect.top + 100}")
    focused_window.pump(0.2)
    cover_hwnd = win32.root_window(int(cover.winfo_id()))
    win32gui.EnableWindow(cover_hwnd, False)
    focused_window.pump(0.2)
    try:
        if win32.foreground_window() != focused_window.hwnd:
            pytest.skip("cover stole focus")
        with pytest.raises(GameMcpError) as exc:
            capture_game_screen(binding, settings, ObservationStore())
        assert exc.value.code == ErrorCode.OCCLUDED
        assert exc.value.details["method"] == "z_order"
        assert exc.value.details["occluder"]["hwnd"] == cover_hwnd
        assert exc.value.details["occluder"]["enabled"] is False
    finally:
        cover.destroy()


def test_transparent_overlay_is_reported_not_blocking(focused_window):
    import win32con

    settings = _settings()
    binding = _bound(focused_window, settings)
    rect = win32.client_rect_on_screen(focused_window.hwnd)
    overlay = tk.Toplevel(focused_window.root)
    overlay.overrideredirect(True)
    overlay.attributes("-topmost", True)
    overlay.geometry(f"{rect.width}x{rect.height}+{rect.left}+{rect.top}")
    focused_window.pump(0.2)
    ov_hwnd = win32.root_window(int(overlay.winfo_id()))
    ex = win32gui.GetWindowLong(ov_hwnd, win32con.GWL_EXSTYLE)
    win32gui.SetWindowLong(ov_hwnd, win32con.GWL_EXSTYLE, ex | win32con.WS_EX_LAYERED | win32con.WS_EX_TRANSPARENT)
    win32gui.SetLayeredWindowAttributes(ov_hwnd, 0, 1, win32con.LWA_ALPHA)  # practically invisible
    focused_window.pump(0.2)
    try:
        if win32.foreground_window() != focused_window.hwnd:
            pytest.skip("overlay stole focus")
        result = capture_game_screen(binding, settings, ObservationStore())
        assert [o.hwnd for o in result.window.overlays] == [ov_hwnd]
        assert result.warnings and "overlay" in result.warnings[0].lower()
        assert result.metadata()["window"]["overlays"][0]["hit_test_transparent"] is True
    finally:
        overlay.destroy()


def test_capture_refused_when_cover_appears_during_grab(focused_window, monkeypatch):
    from game_mcp import capture as capture_mod

    settings = _settings()
    binding = _bound(focused_window, settings)
    rect = win32.client_rect_on_screen(focused_window.hwnd)
    holder: dict[str, tk.Toplevel] = {}
    real_capture = capture_mod.backend.capture

    def grab_then_cover(capture_rect, backend_name):
        image = real_capture(capture_rect, backend_name)
        cover = tk.Toplevel(focused_window.root)
        cover.overrideredirect(True)
        cover.attributes("-topmost", True)
        cover.geometry(f"80x80+{rect.left + 50}+{rect.top + 50}")
        holder["cover"] = cover
        focused_window.pump(0.2)
        return image

    monkeypatch.setattr(capture_mod.backend, "capture", grab_then_cover)
    try:
        with pytest.raises(GameMcpError) as exc:
            capture_game_screen(binding, settings, ObservationStore())
        assert exc.value.code == ErrorCode.OCCLUDED
        assert exc.value.details["phase"] == "post_capture"
    finally:
        if "cover" in holder:
            holder["cover"].destroy()


def test_capture_is_busy_while_an_action_holds_the_lock(focused_window):
    from game_mcp.server import GameSession

    settings = _settings()
    session = GameSession(settings)
    session.binding = _bound(focused_window, settings)
    assert session.op_lock.acquire(blocking=False)
    try:
        with pytest.raises(GameMcpError) as exc:
            session.run_exclusive(lambda: capture_game_screen(session.binding, settings, session.store))
        assert exc.value.code == ErrorCode.BUSY
    finally:
        session.op_lock.release()
    result = session.run_exclusive(lambda: capture_game_screen(session.binding, settings, session.store))
    assert result.observation.id == session.store.current().id


def test_click_reports_target_ownership_and_trust(focused_window):
    settings = _settings()
    binding = _bound(focused_window, settings)
    svc, store, _ = _service(settings)
    obs = capture_game_screen(binding, settings, store).observation
    result = svc.click(binding, 30, 30, obs.id)
    post = result["post_action"]
    assert post["target_still_ours"] is True
    assert post["stopped_during_action"] is False
    assert post["trustworthy"] is True
    assert result["timing_ms"]["injection_window"] >= result["timing_ms"]["input"]


def test_stop_latch_blocks_input_and_operator_stop_is_sticky(focused_window):
    settings = _settings()
    binding = _bound(focused_window, settings)
    svc, store, latch = _service(settings)
    obs = capture_game_screen(binding, settings, store).observation
    latch.engage("tool", "test")
    with pytest.raises(GameMcpError) as exc:
        svc.click(binding, 10, 10, obs.id)
    assert exc.value.code == ErrorCode.STOPPED
    latch.resume()
    latch.engage("operator", "pause")
    with pytest.raises(GameMcpError) as exc:
        latch.resume()
    assert exc.value.code == ErrorCode.STOPPED
    focused_window.pump(0.2)
    assert focused_window.clicks == []
