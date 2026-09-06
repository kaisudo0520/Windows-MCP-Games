"""Screen-rectangle capture backends (MSS / Pillow).

Vendored from Windows-MCP ``src/windows_mcp/desktop/screenshot.py`` at commit
08ddee78c26182b103d62c1c84c1fbec82a280b2 (MIT, Copyright (c) 2025 JEOMON GEORGE).

Modifications for sts-game-mcp:
  * The dxcam (DXGI) backend was removed; only ``mss`` and ``pillow`` remain.
  * ``windows_mcp.uia.Rect`` / ``GetVirtualScreenRect`` were replaced by a local
    ``Rect`` dataclass and a ctypes ``GetSystemMetrics`` call so that the
    UIAutomation / comtypes stack is not imported.
  * The env var was renamed to ``GAME_MCP_SCREENSHOT_BACKEND``.
  * ``mss.MSS`` is used when available (``mss.mss`` is deprecated in mss 10).

IMPORTANT: these backends capture a rectangle of the *screen*. They are not a
window-isolated capture. Whatever is drawn inside that rectangle (overlays,
popups, other windows on top) ends up in the image. The caller is responsible
for deciding whether the rectangle is safe to capture (see game_mcp.capture).
"""

from dataclasses import dataclass
import ctypes
import logging
import os

from PIL import Image, ImageGrab

try:
    import mss
except ImportError:  # pragma: no cover - exercised only when mss is missing
    mss = None

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


def GetVirtualScreenRect() -> tuple[int, int, int, int]:
    """Return (left, top, right, bottom) of the virtual screen in physical pixels."""
    SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
    SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
    user32 = ctypes.windll.user32
    left = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    top = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    width = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    height = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    return left, top, left + width, top + height


# ---------------------------------------------------------------------------
# Shared utilities (upstream)
# ---------------------------------------------------------------------------


def _build_crop_box(capture_rect: Rect, padding: int = 0) -> tuple[int, int, int, int]:
    left_offset, top_offset, _, _ = GetVirtualScreenRect()
    return (
        capture_rect.left - left_offset + padding,
        capture_rect.top - top_offset + padding,
        capture_rect.right - left_offset + padding,
        capture_rect.bottom - top_offset + padding,
    )


def _crop_screenshot(screenshot: Image.Image, capture_rect: Rect | None) -> Image.Image:
    if capture_rect is None:
        return screenshot
    return screenshot.crop(_build_crop_box(capture_rect))


def get_screenshot_backend() -> str:
    """Read the preferred backend from the environment variable."""
    value = os.getenv("GAME_MCP_SCREENSHOT_BACKEND", "auto")
    normalized = value.strip().lower()
    valid = _ScreenshotBackend.registry.keys() | {"auto"}
    if normalized in valid:
        return normalized
    logger.warning(
        "Unknown screenshot backend '%s'; falling back to auto",
        value,
    )
    return "auto"


# ---------------------------------------------------------------------------
# Backend framework (upstream)
# ---------------------------------------------------------------------------


class _ScreenshotBackend:
    """Base class for screenshot capture backends.

    Subclasses **must** define two class attributes:

    * ``name: str`` - unique key such as ``"mss"``.
    * ``priority: int`` - lower numbers are tried first in the *auto* chain.

    Defining both attributes automatically registers the subclass via
    ``__init_subclass__``.
    """

    name: str
    priority: int

    registry: dict[str, type["_ScreenshotBackend"]] = {}

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if "name" in cls.__dict__ and "priority" in cls.__dict__:
            existing = _ScreenshotBackend.registry.get(cls.name)
            if existing is not None and existing is not cls:
                raise ValueError(f"Duplicate screenshot backend name: {cls.name!r}")
            _ScreenshotBackend.registry[cls.name] = cls

    def is_available(self, capture_rect: Rect | None) -> bool:
        """Return ``True`` if this backend can service the request."""
        return True

    def capture(self, capture_rect: Rect | None) -> Image.Image:
        """Capture a screenshot.  Subclasses must override."""
        raise NotImplementedError


class _PillowBackend(_ScreenshotBackend):
    """Capture via PIL *ImageGrab* (always available)."""

    name = "pillow"
    priority = 100

    def capture(self, capture_rect: Rect | None) -> Image.Image:
        grab_kwargs: dict[str, object] = {"all_screens": True}
        if capture_rect is not None:
            grab_kwargs["bbox"] = (
                capture_rect.left,
                capture_rect.top,
                capture_rect.right,
                capture_rect.bottom,
            )
        try:
            screenshot = ImageGrab.grab(**grab_kwargs)
        except (OSError, RuntimeError, ValueError):
            if capture_rect is not None:
                logger.warning(
                    "Failed to capture selected region directly, "
                    "falling back to virtual screen crop"
                )
                # Fallback: grab full virtual screen then crop to the requested region.
                return _crop_screenshot(ImageGrab.grab(all_screens=True), capture_rect)
            logger.warning("Failed to capture virtual screen, using primary screen")
            screenshot = ImageGrab.grab()
        # Success path: ImageGrab.grab(bbox=...) already returned the exact region,
        # so no further cropping is needed.
        return screenshot


class _MssBackend(_ScreenshotBackend):
    """Capture via the *mss* library."""

    name = "mss"
    priority = 20

    def is_available(self, capture_rect: Rect | None) -> bool:
        return mss is not None

    def capture(self, capture_rect: Rect | None) -> Image.Image:
        if mss is None:
            raise RuntimeError("mss is not available")
        # mss 10 renamed the factory to ``MSS``; fall back for older releases.
        factory = getattr(mss, "MSS", None) or mss.mss
        with factory() as sct:
            if capture_rect is None:
                monitor = sct.monitors[0]
            else:
                monitor = {
                    "left": capture_rect.left,
                    "top": capture_rect.top,
                    "width": capture_rect.right - capture_rect.left,
                    "height": capture_rect.bottom - capture_rect.top,
                }
            raw = sct.grab(monitor)
            image = Image.frombytes("RGB", raw.size, raw.rgb)
        # mss.grab(monitor) already captures exactly the requested region,
        # so no further cropping is needed.
        return image


# ---------------------------------------------------------------------------
# Instance management (upstream)
# ---------------------------------------------------------------------------

_backend_instances: dict[str, _ScreenshotBackend] = {}

#: Backends that have handed back an unusable frame in this process.
_degraded_backends: set[str] = set()


def _get_backend(name: str) -> _ScreenshotBackend:
    """Return a cached singleton instance for the given backend *name*."""
    if name not in _backend_instances:
        cls = _ScreenshotBackend.registry.get(name)
        if cls is None:
            raise ValueError(f"Unknown screenshot backend: {name!r}")
        _backend_instances[name] = cls()
    return _backend_instances[name]


def _is_usable_capture(image: Image.Image | None) -> bool:
    """Return True if a captured frame is structurally sound enough to encode.

    Only structure is checked, never content -- a legitimately black screen is a
    perfectly valid screenshot.
    """
    if image is None:
        return False
    if image.width <= 0 or image.height <= 0:
        return False
    try:
        image.load()
    except (OSError, ValueError):
        return False
    return True


# ---------------------------------------------------------------------------
# Public API (upstream)
# ---------------------------------------------------------------------------


def capture(
    capture_rect: Rect | None,
    backend: str | None = None,
) -> tuple[Image.Image, str]:
    """Capture a screenshot and return ``(image, backend_name_used)``."""
    selected = backend or get_screenshot_backend()

    # Build the candidate chain: all registered backends sorted by priority, or a single one.
    if selected == "auto":
        chain = sorted(_ScreenshotBackend.registry.values(), key=lambda c: c.priority)
    else:
        cls = _ScreenshotBackend.registry.get(selected)
        if cls is None:
            raise ValueError(f"Unknown screenshot backend: {selected!r}")
        chain = [cls]

    # Try each candidate: skip unavailable ones, catch failures and fall through.
    for backend_cls in chain:
        if backend_cls.name in _degraded_backends:
            continue
        inst = _get_backend(backend_cls.name)
        if not inst.is_available(capture_rect):
            continue
        try:
            image = inst.capture(capture_rect)
        except (OSError, RuntimeError, ValueError, IndexError):
            logger.warning(
                "Screenshot backend '%s' failed; trying next backend",
                inst.name,
                exc_info=selected != "auto",
            )
            continue

        if not _is_usable_capture(image):
            _degraded_backends.add(inst.name)
            logger.warning(
                "Screenshot backend '%s' returned an unusable frame; disabling it "
                "for this process and trying the next backend",
                inst.name,
            )
            continue

        return image, inst.name

    # All candidates exhausted; pillow is always present as the last resort.
    return _get_backend("pillow").capture(capture_rect), "pillow"
