import pytest

from game_mcp.errors import ErrorCode, GameMcpError
from game_mcp.observation import ObservationStore
from game_mcp.win32 import ScreenRect

RECT = ScreenRect(100, 100, 1700, 1000)


def _issue(store: ObservationStore, ttl: float = 30.0, rect: ScreenRect = RECT):
    return store.issue(hwnd=1, client_rect=rect, dpi=96, image_size=(1600, 900), scale=1.0, ttl_s=ttl)


def _code(exc_info) -> tuple[ErrorCode, str]:
    exc: GameMcpError = exc_info.value
    return exc.code, exc.details.get("reason", "")


def test_require_returns_current_observation():
    store = ObservationStore()
    obs = _issue(store)
    assert store.require(obs.id, client_rect_now=RECT) is obs


def test_missing_id_is_stale():
    store = ObservationStore()
    _issue(store)
    with pytest.raises(GameMcpError) as exc:
        store.require("", client_rect_now=RECT)
    assert _code(exc) == (ErrorCode.OBSERVATION_STALE, "missing")


def test_no_observation_issued_yet():
    store = ObservationStore()
    with pytest.raises(GameMcpError) as exc:
        store.require("abc", client_rect_now=RECT)
    assert _code(exc) == (ErrorCode.OBSERVATION_STALE, "none_issued")


def test_older_observation_is_superseded():
    store = ObservationStore()
    first = _issue(store)
    second = _issue(store)
    with pytest.raises(GameMcpError) as exc:
        store.require(first.id, client_rect_now=RECT)
    assert _code(exc) == (ErrorCode.OBSERVATION_STALE, "superseded")
    assert exc.value.details["current"] == second.id


def test_expired_observation(monkeypatch):
    store = ObservationStore()
    obs = _issue(store, ttl=1.0)
    import game_mcp.observation as mod

    real = mod.time.monotonic
    monkeypatch.setattr(mod.time, "monotonic", lambda: real() + 5.0)
    with pytest.raises(GameMcpError) as exc:
        store.require(obs.id, client_rect_now=RECT)
    assert _code(exc) == (ErrorCode.OBSERVATION_STALE, "expired")
    assert store.current() is None


def test_window_moved_invalidates():
    store = ObservationStore()
    obs = _issue(store)
    moved = ScreenRect(110, 100, 1710, 1000)
    with pytest.raises(GameMcpError) as exc:
        store.require(obs.id, client_rect_now=moved)
    assert _code(exc) == (ErrorCode.OBSERVATION_STALE, "window_moved_or_resized")
    assert store.current() is None


def test_explicit_invalidate_reports_reason():
    store = ObservationStore()
    obs = _issue(store)
    store.invalidate("input_performed")
    with pytest.raises(GameMcpError) as exc:
        store.require(obs.id, client_rect_now=RECT)
    assert _code(exc) == (ErrorCode.OBSERVATION_STALE, "input_performed")
