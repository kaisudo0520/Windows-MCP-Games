"""Shared fixtures. Adds src/ to sys.path so tests run without an editable install."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def combat_state_dict() -> dict:
    with (FIXTURES / "commmod_combat_state.json").open(encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture
def combat_state_line(combat_state_dict) -> bytes:
    return (json.dumps(combat_state_dict, ensure_ascii=False) + "\n").encode("utf-8")


@pytest.fixture
def quiet_release(monkeypatch):
    """Stop latch tests must not touch the real keyboard/mouse."""
    from game_mcp.vendor import win32_input as w32

    monkeypatch.setattr(w32, "ReleaseMouse", lambda button, waitTime=0: None)
    monkeypatch.setattr(w32, "ReleaseKey", lambda vk, waitTime=0: None)
