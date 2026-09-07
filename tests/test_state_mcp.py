"""Tests for the read-only advisor MCP server (game_mcp.bridge.mcp_server)."""

import asyncio
import threading
import time

from fastmcp import Client

from game_mcp.bridge import persist
from game_mcp.bridge.mcp_server import build_server, read_state, wait_for_state
from game_mcp.bridge.protocol import SnapshotStore


def _write(out_dir, state):
    return persist.write_latest(out_dir, SnapshotStore().record(state))


def test_read_state_unavailable_on_empty_dir(tmp_path):
    result = read_state(str(tmp_path))
    assert result["available"] is False and "reason" in result


def test_read_state_unavailable_on_missing_dir(tmp_path):
    result = read_state(str(tmp_path / "nope"))
    assert result == {"available": False, "reason": "no_state_dir"}


def test_read_state_returns_fields_after_write(tmp_path):
    out = str(tmp_path)
    _write(out, {"floor": 6, "class": "IRONCLAD"})
    result = read_state(out)
    assert result["available"] is True
    assert result["seq"] == 1
    assert result["state"] == {"floor": 6, "class": "IRONCLAD"}
    assert result["received_wall"] is not None
    assert isinstance(result["age_ms"], float) and result["age_ms"] >= 0.0


def test_wait_for_state_returns_when_seq_bumped(tmp_path):
    out = str(tmp_path)
    store = SnapshotStore()
    persist.write_latest(out, store.record({"floor": 1}))  # seq 1 present

    def bump():
        time.sleep(0.2)
        persist.write_latest(out, store.record({"floor": 2}))  # seq 2

    thread = threading.Thread(target=bump)
    thread.start()
    try:
        result = wait_for_state(out, since_seq=1, timeout_s=5, poll_s=0.05)
    finally:
        thread.join()
    assert result["timed_out"] is False
    assert result["seq"] == 2 and result["state"] == {"floor": 2}


def test_wait_for_state_times_out(tmp_path):
    out = str(tmp_path)
    _write(out, {"floor": 1})  # seq 1
    start = time.monotonic()
    result = wait_for_state(out, since_seq=1, timeout_s=1, poll_s=0.05)  # clamp floor is 1s
    assert result["timed_out"] is True
    assert result["seq"] == 1  # falls back to the current state
    assert time.monotonic() - start >= 1.0


def test_server_exposes_two_read_only_tools(tmp_path):
    mcp = build_server(str(tmp_path))

    async def run():
        async with Client(mcp) as client:
            tools = await client.list_tools()
            names = {t.name for t in tools}
            assert names == {"get_game_state", "wait_for_state"}
            for tool in tools:
                ann = tool.annotations
                ro = getattr(ann, "readOnlyHint", None)
                if ro is None and isinstance(ann, dict):
                    ro = ann.get("readOnlyHint")
                assert ro is True
            result = await client.call_tool("get_game_state")
            assert result.data["available"] is False  # empty dir

    asyncio.run(run())
