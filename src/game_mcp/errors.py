"""Error codes shared by every tool. Each failure is reported with one code so the
calling AI can branch on it instead of parsing prose."""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    NOT_BOUND = "NOT_BOUND"
    NO_CANDIDATES = "NO_CANDIDATES"
    AMBIGUOUS_CANDIDATES = "AMBIGUOUS_CANDIDATES"
    CANDIDATE_NOT_FOUND = "CANDIDATE_NOT_FOUND"
    WINDOW_GONE = "WINDOW_GONE"
    PROCESS_GONE = "PROCESS_GONE"
    MINIMIZED = "MINIMIZED"
    NOT_FOREGROUND = "NOT_FOREGROUND"
    CLOAKED = "CLOAKED"
    OFFSCREEN = "OFFSCREEN"
    OCCLUDED = "OCCLUDED"
    INVALID_COORDINATES = "INVALID_COORDINATES"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    OBSERVATION_STALE = "OBSERVATION_STALE"
    CAPTURE_FAILED = "CAPTURE_FAILED"
    STOPPED = "STOPPED"
    KEY_NOT_ALLOWED = "KEY_NOT_ALLOWED"
    IME_ACTIVE = "IME_ACTIVE"
    INPUT_FAILED = "INPUT_FAILED"
    BUSY = "BUSY"


class GameMcpError(Exception):
    """Structured error carrying a code plus optional machine-readable details."""

    def __init__(self, code: ErrorCode, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"code": str(self.code), "message": self.message, "details": self.details}


def error_response(exc: GameMcpError, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": False, "error": exc.to_dict()}
    payload.update(extra)
    return payload
