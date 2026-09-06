"""Discover candidate game windows and bind to exactly one of them.

A binding records the HWND *and* the process identity (PID, executable name,
process creation time). Re-validation compares all of them, so a recycled PID
or a new window with the same title cannot silently inherit the binding.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import psutil

from game_mcp import win32
from game_mcp.config import Settings
from game_mcp.errors import ErrorCode, GameMcpError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Candidate:
    hwnd: int
    pid: int
    exe_name: str
    exe_path: str
    title: str
    class_name: str
    window_rect: win32.ScreenRect
    minimized: bool
    visible: bool
    is_foreground: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "hwnd": self.hwnd,
            "pid": self.pid,
            "exe_name": self.exe_name,
            "exe_path": self.exe_path,
            "title": self.title,
            "class": self.class_name,
            "window_rect": self.window_rect.to_dict(),
            "minimized": self.minimized,
            "visible": self.visible,
            "is_foreground": self.is_foreground,
        }


@dataclass(frozen=True)
class Binding:
    hwnd: int
    pid: int
    exe_name: str
    exe_path: str
    process_create_time: float
    title_at_bind: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "hwnd": self.hwnd,
            "pid": self.pid,
            "exe_name": self.exe_name,
            "exe_path": self.exe_path,
            "process_create_time": self.process_create_time,
            "title_at_bind": self.title_at_bind,
        }


def _process_info(pid: int) -> tuple[str, str, float] | None:
    try:
        proc = psutil.Process(pid)
        with proc.oneshot():
            name = proc.name()
            try:
                path = proc.exe()
            except (psutil.AccessDenied, psutil.ZombieProcess):
                path = ""
            return name, path, proc.create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return None


def _matches(settings: Settings, exe_name: str, exe_path: str, title: str) -> bool:
    if exe_name.lower() not in settings.process_names:
        return False
    title_l = title.lower()
    path_l = exe_path.lower()
    if any(sub in title_l for sub in settings.title_substrings):
        return True
    return any(sub in path_l for sub in settings.path_substrings)


def list_candidates(settings: Settings) -> list[Candidate]:
    """Enumerate top-level windows that look like the configured game."""
    foreground = win32.foreground_window()
    found: list[Candidate] = []
    for hwnd in win32.enum_top_level_windows():
        if not win32.is_visible(hwnd):
            continue
        title = win32.window_title(hwnd)
        if not title:
            continue
        pid = win32.window_pid(hwnd)
        if pid <= 0:
            continue
        info = _process_info(pid)
        if info is None:
            continue
        exe_name, exe_path, _create_time = info
        if not _matches(settings, exe_name, exe_path, title):
            continue
        try:
            rect = win32.window_rect(hwnd)
        except Exception:
            continue
        found.append(
            Candidate(
                hwnd=hwnd,
                pid=pid,
                exe_name=exe_name,
                exe_path=exe_path,
                title=title,
                class_name=win32.window_class(hwnd),
                window_rect=rect,
                minimized=win32.is_minimized(hwnd),
                visible=True,
                is_foreground=(hwnd == foreground),
            )
        )
    return found


def bind(settings: Settings, hwnd: int | None) -> Binding:
    """Bind to the single matching window, or to *hwnd* when several match."""
    candidates = list_candidates(settings)
    if not candidates:
        raise GameMcpError(
            ErrorCode.NO_CANDIDATES,
            "No window matches the configured game process/title filters.",
            {"process_names": settings.process_names, "title_substrings": settings.title_substrings},
        )
    if hwnd is None:
        if len(candidates) > 1:
            raise GameMcpError(
                ErrorCode.AMBIGUOUS_CANDIDATES,
                "Several windows match; call bind_game again with an explicit hwnd.",
                {"candidates": [c.to_dict() for c in candidates]},
            )
        chosen = candidates[0]
    else:
        matching = [c for c in candidates if c.hwnd == hwnd]
        if not matching:
            raise GameMcpError(
                ErrorCode.CANDIDATE_NOT_FOUND,
                f"hwnd {hwnd} is not among the current candidates.",
                {"candidates": [c.to_dict() for c in candidates]},
            )
        chosen = matching[0]
    info = _process_info(chosen.pid)
    if info is None:
        raise GameMcpError(ErrorCode.PROCESS_GONE, f"Process {chosen.pid} disappeared during bind.")
    exe_name, exe_path, create_time = info
    binding = Binding(
        hwnd=chosen.hwnd,
        pid=chosen.pid,
        exe_name=exe_name,
        exe_path=exe_path,
        process_create_time=create_time,
        title_at_bind=chosen.title,
    )
    logger.info("Bound to hwnd=%s pid=%s exe=%s title=%r", binding.hwnd, binding.pid, exe_name, chosen.title)
    return binding


def check_binding_alive(binding: Binding) -> None:
    """Raise WINDOW_GONE / PROCESS_GONE if the bound window or process is no longer the same."""
    if not win32.is_window(binding.hwnd):
        raise GameMcpError(ErrorCode.WINDOW_GONE, "Bound window handle is no longer valid.", binding.to_dict())
    pid = win32.window_pid(binding.hwnd)
    if pid != binding.pid:
        raise GameMcpError(
            ErrorCode.WINDOW_GONE,
            "Bound window handle now belongs to a different process.",
            {"expected_pid": binding.pid, "actual_pid": pid},
        )
    info = _process_info(binding.pid)
    if info is None:
        raise GameMcpError(ErrorCode.PROCESS_GONE, "Bound process has exited.", binding.to_dict())
    exe_name, _path, create_time = info
    if exe_name.lower() != binding.exe_name.lower() or abs(create_time - binding.process_create_time) > 1.0:
        raise GameMcpError(
            ErrorCode.PROCESS_GONE,
            "PID was reused by a different process.",
            {"expected": binding.to_dict(), "actual_exe": exe_name, "actual_create_time": create_time},
        )
