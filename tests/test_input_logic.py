import pytest

from game_mcp.config import Settings
from game_mcp.errors import ErrorCode, GameMcpError
from game_mcp.input_service import InputService, key_to_vk
from game_mcp.observation import Observation, ObservationStore
from game_mcp.safety import HeldInputs, StopLatch
from game_mcp.win32 import ScreenRect


def _service(settings: Settings | None = None) -> InputService:
    settings = settings or Settings()
    held = HeldInputs()
    return InputService(settings, ObservationStore(), held, StopLatch(held, settings))


def _obs(scale: float = 1.0, image_size=(1600, 900)) -> Observation:
    return Observation(
        id="obs1",
        hwnd=1,
        client_rect=ScreenRect(100, 50, 1700, 950),
        dpi=96,
        image_size=image_size,
        scale=scale,
        created_monotonic=0.0,
        created_wall=0.0,
        ttl_s=30.0,
    )


@pytest.mark.parametrize(
    "name, vk",
    [("e", 0x45), ("E", 0x45), ("1", 0x31), ("0", 0x30), ("enter", 0x0D), ("space", 0x20), ("escape", 0x1B)],
)
def test_key_to_vk_known(name, vk):
    assert key_to_vk(name) == vk


@pytest.mark.parametrize("name", ["ctrl+c", "alt", "f4", "", "win", "é"])
def test_key_to_vk_unknown(name):
    assert key_to_vk(name) is None


def test_press_key_rejects_non_whitelisted_before_any_window_check():
    svc = _service(Settings(key_whitelist=["e"]))
    with pytest.raises(GameMcpError) as exc:
        svc.press_key(None, "escape", "whatever")
    assert exc.value.code == ErrorCode.KEY_NOT_ALLOWED
    assert exc.value.details["allowed"] == ["e"]


def test_press_key_rejects_combinations_even_if_listed():
    svc = _service(Settings(key_whitelist=["ctrl+c"]))
    with pytest.raises(GameMcpError) as exc:
        svc.press_key(None, "ctrl+c", "x")
    assert exc.value.code == ErrorCode.KEY_NOT_ALLOWED


def test_click_requires_binding():
    svc = _service()
    with pytest.raises(GameMcpError) as exc:
        svc.click(None, 10, 10, "obs")
    assert exc.value.code == ErrorCode.NOT_BOUND


def test_click_refused_when_stopped():
    settings = Settings()
    held = HeldInputs()
    latch = StopLatch(held, settings)
    svc = InputService(settings, ObservationStore(), held, latch)
    latch.engage("tool", "t")
    with pytest.raises(GameMcpError) as exc:
        svc.click(None, 10, 10, "obs")
    assert exc.value.code == ErrorCode.STOPPED


def test_invalid_button():
    svc = _service()
    with pytest.raises(GameMcpError) as exc:
        svc.click(None, 1, 1, "obs", button="middle")  # type: ignore[arg-type]
    assert exc.value.code == ErrorCode.INVALID_ARGUMENT


@pytest.mark.parametrize("x, y", [(-1, 0), (0, -1), (1600, 0), (0, 900), (99999, 5)])
def test_map_point_out_of_bounds(x, y):
    svc = _service()
    with pytest.raises(GameMcpError) as exc:
        svc._map_point(_obs(), x, y)
    assert exc.value.code == ErrorCode.INVALID_COORDINATES


def test_map_point_rejects_bool_and_float():
    svc = _service()
    with pytest.raises(GameMcpError):
        svc._map_point(_obs(), True, 1)  # type: ignore[arg-type]
    with pytest.raises(GameMcpError):
        svc._map_point(_obs(), 1.5, 1)  # type: ignore[arg-type]


def test_map_point_identity_scale():
    svc = _service()
    client, screen = svc._map_point(_obs(), 0, 0)
    assert client == (0, 0) and screen == (100, 50)
    client, screen = svc._map_point(_obs(), 1599, 899)
    assert client == (1599, 899) and screen == (1699, 949)


def test_map_point_scaled_image():
    svc = _service()
    obs = _obs(scale=0.5, image_size=(800, 450))
    client, screen = svc._map_point(obs, 799, 449)
    assert client == (1598, 898) and screen == (1698, 948)
    with pytest.raises(GameMcpError):
        svc._map_point(obs, 800, 0)
