"""Runtime configuration, read once from environment variables.

Every knob has a conservative default so the server is usable with no
configuration at all. Nothing here reaches out to the network.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return max(lo, min(hi, value))


def _env_list(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return list(default)
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


# Keys the AI may press. Slay the Spire defaults: 1-9/0 select hand cards,
# E ends the turn, Enter/Space confirm "proceed" style prompts.
DEFAULT_KEY_WHITELIST = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "e", "enter", "space"]

DEFAULT_PROCESS_NAMES = ["slaythespire.exe", "javaw.exe", "java.exe"]
DEFAULT_TITLE_SUBSTRINGS = ["slay the spire"]
DEFAULT_PATH_SUBSTRINGS = ["slaythespire"]


@dataclass(frozen=True)
class Settings:
    process_names: list[str] = field(default_factory=lambda: _env_list("GAME_MCP_PROCESS_NAMES", DEFAULT_PROCESS_NAMES))
    title_substrings: list[str] = field(default_factory=lambda: _env_list("GAME_MCP_TITLE_SUBSTRINGS", DEFAULT_TITLE_SUBSTRINGS))
    path_substrings: list[str] = field(default_factory=lambda: _env_list("GAME_MCP_PATH_SUBSTRINGS", DEFAULT_PATH_SUBSTRINGS))
    key_whitelist: list[str] = field(default_factory=lambda: _env_list("GAME_MCP_KEY_WHITELIST", DEFAULT_KEY_WHITELIST))
    #: Seconds an observation stays valid for input if nothing else invalidates it.
    observation_ttl_s: float = field(default_factory=lambda: _env_float("GAME_MCP_OBSERVATION_TTL_S", 20.0, 1.0, 300.0))
    #: Grid step (pixels) used when sampling the client area for occluding windows.
    occlusion_grid_px: int = field(default_factory=lambda: int(_env_float("GAME_MCP_OCCLUSION_GRID_PX", 40, 8, 400)))
    #: Directory for debug image dumps. Unset = never write screenshots to disk.
    debug_dir: str | None = field(default_factory=lambda: os.getenv("GAME_MCP_DEBUG_DIR") or None)
    #: Retention for the debug directory: newest N files are kept after every dump, and files
    #: older than this many hours are deleted at server start.
    debug_keep_files: int = field(default_factory=lambda: int(_env_float("GAME_MCP_DEBUG_KEEP_FILES", 300, 1, 100000)))
    debug_max_age_h: float = field(default_factory=lambda: _env_float("GAME_MCP_DEBUG_MAX_AGE_H", 24, 0.01, 8760))
    #: Operator emergency stop key: scrolllock | pause | f12. Scroll Lock is the default because
    #: the Pause key on PC keyboards emits make+break together, so it has no sustained "down" state.
    emergency_key: str = field(default_factory=lambda: os.getenv("GAME_MCP_EMERGENCY_KEY", "scrolllock").strip().lower())
    #: Moving the physical cursor into the primary screen's top-left corner also stops input.
    corner_failsafe: bool = field(default_factory=lambda: _env_bool("GAME_MCP_CORNER_FAILSAFE", True))
    #: Whether `resume_input` may clear a stop that the operator triggered.
    allow_tool_resume_after_operator_stop: bool = field(default_factory=lambda: _env_bool("GAME_MCP_ALLOW_TOOL_RESUME_AFTER_OPERATOR_STOP", False))
    #: Pause between placing the cursor and pressing, so the game sees the hover on a frame
    #: before the press (LibGDX hitboxes evaluate hover and click per frame).
    click_settle_s: float = field(default_factory=lambda: _env_float("GAME_MCP_CLICK_SETTLE_MS", 60, 0, 500) / 1000)
    #: When the game window's IME is open in native (e.g. Chinese) mode, tap Shift once to
    #: switch it to alphanumeric before pressing the key (Microsoft Bopomofo/Quick toggle on Shift).
    ime_autotoggle: bool = field(default_factory=lambda: _env_bool("GAME_MCP_IME_AUTOTOGGLE", True))
    #: If the IME is still in native mode after the toggle, send the key anyway instead of IME_ACTIVE.
    allow_ime_native: bool = field(default_factory=lambda: _env_bool("GAME_MCP_ALLOW_IME_NATIVE", False))
    #: Screenshot backend: auto | mss | pillow.
    screenshot_backend: str = field(default_factory=lambda: os.getenv("GAME_MCP_SCREENSHOT_BACKEND", "auto").strip().lower())
    #: Longest side of the returned image. 1600x900 fits under this untouched.
    max_image_width: int = 1920
    max_image_height: int = 1080
    #: Extra downscale applied before encoding (1.0 = none). Card text needs >= 0.75.
    image_scale: float = field(default_factory=lambda: _env_float("GAME_MCP_IMAGE_SCALE", 1.0, 0.25, 1.0))
    #: "png" (lossless, ~1.2 MB at 1600x900) or "jpeg" (4:4:4, ~230 KB at quality 90).
    image_format: str = field(default_factory=lambda: (os.getenv("GAME_MCP_IMAGE_FORMAT", "png").strip().lower() if os.getenv("GAME_MCP_IMAGE_FORMAT", "png").strip().lower() in {"png", "jpeg"} else "png"))
    jpeg_quality: int = field(default_factory=lambda: int(_env_float("GAME_MCP_JPEG_QUALITY", 90, 50, 100)))


def load_settings() -> Settings:
    return Settings()
