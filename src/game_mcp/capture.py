"""Capture the bound window's client area and issue an observation id.

The pixel source is the vendored MSS/Pillow screen-rectangle backend. Because
that reads the *screen*, the rectangle is only captured after
``validate_window`` confirmed the window is foreground, fully on a monitor and
free of hit-testable occluders. That is the isolation this tool offers; see
``win32.find_occluder`` for what it cannot detect.
"""

from __future__ import annotations

import io
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

from PIL import Image

from game_mcp.binding import Binding
from game_mcp.config import Settings
from game_mcp.errors import ErrorCode, GameMcpError
from game_mcp.observation import Observation, ObservationStore
from game_mcp.validation import WindowState, validate_window
from game_mcp.vendor import screenshot as backend

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CaptureResult:
    png: bytes  # encoded image bytes; format is in ``image_format``
    image_format: str
    observation: Observation
    window: WindowState
    backend_name: str
    timing_ms: dict[str, float]
    debug_path: str | None
    warnings: list[str]

    def metadata(self) -> dict[str, Any]:
        meta = self.observation.to_dict()
        meta.update(
            {
                "ok": True,
                "backend": self.backend_name,
                "window": self.window.to_dict(),
                "timing_ms": self.timing_ms,
                "image_format": self.image_format,
                "image_bytes": len(self.png),
                "warnings": self.warnings,
                "coordinate_space": (
                    "Input coordinates are client-relative pixels in the returned image's own "
                    "pixel grid (0,0 = top-left of the image)."
                ),
            }
        )
        if self.debug_path:
            meta["debug_path"] = self.debug_path
        return meta


def _fit(image: Image.Image, max_w: int, max_h: int, extra_scale: float = 1.0) -> tuple[Image.Image, float]:
    scale = min(1.0, max_w / image.width, max_h / image.height) * extra_scale
    if scale >= 1.0:
        return image, 1.0
    size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    return image.resize(size, Image.Resampling.LANCZOS), scale


def _encode(image: Image.Image, settings: Settings) -> tuple[bytes, str]:
    buffer = io.BytesIO()
    if settings.image_format == "jpeg":
        image.convert("RGB").save(buffer, format="JPEG", quality=settings.jpeg_quality, subsampling=0)
        return buffer.getvalue(), "jpeg"
    image.save(buffer, format="PNG", optimize=False, compress_level=3)
    return buffer.getvalue(), "png"


_DEBUG_EXTS = (".png", ".jpeg", ".jpg")


def _debug_files(directory: str) -> list[tuple[float, str]]:
    """(mtime, path) for every image in *directory*, oldest first."""
    entries: list[tuple[float, str]] = []
    try:
        for name in os.listdir(directory):
            if name.lower().endswith(_DEBUG_EXTS):
                path = os.path.join(directory, name)
                try:
                    entries.append((os.path.getmtime(path), path))
                except OSError:
                    continue
    except OSError:
        return []
    entries.sort()
    return entries


def prune_debug_dir(settings: Settings, *, by_age: bool, by_count: bool) -> int:
    """Delete debug images beyond the retention policy. Returns the number removed."""
    if not settings.debug_dir or not os.path.isdir(settings.debug_dir):
        return 0
    entries = _debug_files(settings.debug_dir)
    doomed: list[str] = []
    if by_age:
        cutoff = time.time() - settings.debug_max_age_h * 3600
        doomed.extend(path for mtime, path in entries if mtime < cutoff)
    if by_count and len(entries) > settings.debug_keep_files:
        doomed.extend(path for _mtime, path in entries[: len(entries) - settings.debug_keep_files])
    removed = 0
    for path in dict.fromkeys(doomed):
        try:
            os.remove(path)
            removed += 1
        except OSError:
            logger.warning("could not delete debug image %s", path)
    if removed:
        logger.info("pruned %d debug image(s) from %s", removed, settings.debug_dir)
    return removed


def _debug_dump(settings: Settings, png: bytes, observation_id: str, ext: str) -> str | None:
    if not settings.debug_dir:
        return None
    try:
        os.makedirs(settings.debug_dir, exist_ok=True)
        path = os.path.join(settings.debug_dir, f"{time.strftime('%Y%m%d-%H%M%S')}-{observation_id}.{ext}")
        with open(path, "wb") as fh:
            fh.write(png)
    except OSError:
        logger.exception("debug dump failed")
        return None
    prune_debug_dir(settings, by_age=False, by_count=True)
    return path


def capture_game_screen(
    binding: Binding | None,
    settings: Settings,
    store: ObservationStore,
) -> CaptureResult:
    total_started = time.perf_counter()
    timing: dict[str, float] = {}

    started = time.perf_counter()
    window = validate_window(binding, settings, require_foreground=True, check_occlusion=True)
    timing["validate"] = round((time.perf_counter() - started) * 1000, 2)
    timing["occlusion_check"] = round(window.occlusion_check_ms, 2)

    rect = window.client_rect
    started = time.perf_counter()
    try:
        image, backend_name = backend.capture(
            backend.Rect(rect.left, rect.top, rect.right, rect.bottom),
            None if settings.screenshot_backend == "auto" else settings.screenshot_backend,
        )
    except Exception as exc:
        raise GameMcpError(ErrorCode.CAPTURE_FAILED, f"Screen capture failed: {exc}") from exc
    timing["capture"] = round((time.perf_counter() - started) * 1000, 2)

    if image.size != (rect.width, rect.height):
        raise GameMcpError(
            ErrorCode.CAPTURE_FAILED,
            "Backend returned an image whose size differs from the client rect.",
            {"expected": [rect.width, rect.height], "actual": list(image.size), "backend": backend_name},
        )

    # Re-check geometry, foreground AND occlusion *after* the grab: a window that
    # moved, or an overlay that appeared while the pixels were read, taints the image.
    started = time.perf_counter()
    try:
        after = validate_window(binding, settings, require_foreground=True, check_occlusion=True)
    except GameMcpError as exc:
        exc.details["phase"] = "post_capture"
        raise
    if after.client_rect != rect:
        raise GameMcpError(
            ErrorCode.CAPTURE_FAILED,
            "The window moved or resized during capture.",
            {"before": rect.to_dict(), "after": after.client_rect.to_dict()},
        )
    timing["post_validate"] = round((time.perf_counter() - started) * 1000, 2)

    started = time.perf_counter()
    image, scale = _fit(image, settings.max_image_width, settings.max_image_height, settings.image_scale)
    png, image_format = _encode(image, settings)
    timing["encode"] = round((time.perf_counter() - started) * 1000, 2)

    observation = store.issue(
        hwnd=window.hwnd,
        client_rect=rect,
        dpi=window.dpi,
        image_size=image.size,
        scale=scale,
        ttl_s=settings.observation_ttl_s,
    )
    debug_path = _debug_dump(settings, png, observation.id, image_format)
    timing["total"] = round((time.perf_counter() - total_started) * 1000, 2)
    warnings: list[str] = []
    if window.overlays:
        names = ", ".join(f"{o.title or o.class_name} (pid {o.pid})" for o in window.overlays)
        warnings.append(
            f"Hit-test transparent overlay(s) sit above the game and may draw into the image: {names}"
        )
    return CaptureResult(
        png=png,
        image_format=image_format,
        observation=observation,
        window=window,
        backend_name=backend_name,
        timing_ms=timing,
        debug_path=debug_path,
        warnings=warnings,
    )
