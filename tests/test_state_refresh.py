"""STATE re-send requests: allowlist, the dump's request watcher, and the MCP server's
automatic refresh when monster intents are still the DEBUG placeholder."""

import io
import os
import threading
import time

import pytest

from game_mcp.bridge import persist, request
from game_mcp.bridge.__main__ import run_dump
from game_mcp.bridge.mcp_server import get_state, needs_intent_refresh, refresh_state
from game_mcp.bridge.protocol import SnapshotStore, write_line
from game_mcp.bridge.request import RequestWatcher


def _combat(intent, gone=False):
    return {"game_state": {"combat_state": {"monsters": [{"id": "Cultist", "intent": intent, "is_gone": gone}]}}}


# ---------------------------------------------------------------- allowlist

def test_allowlist_permits_state_and_ready_only():
    out = io.StringIO()
    write_line(out, "ready")
    write_line(out, "STATE")
    assert out.getvalue() == "ready\nSTATE\n"


@pytest.mark.parametrize("forbidden", ["state", "STATE 1", "PLAY 1 0", "END", "CHOOSE 0", "CLICK Left 1 2", "KEY 5"])
def test_allowlist_still_rejects_actions(forbidden):
    with pytest.raises(RuntimeError):
        write_line(io.StringIO(), forbidden)


# ---------------------------------------------------------------- watcher

def test_watcher_sends_state_once_per_request_and_consumes_marker(tmp_path):
    out = io.StringIO()
    watcher = RequestWatcher(str(tmp_path), out, poll_s=0.02, min_interval_s=0.0)
    watcher.start()
    try:
        assert request.place_request(str(tmp_path))
        deadline = time.monotonic() + 2
        while watcher.sent < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert watcher.sent == 1
        assert not os.path.exists(request.request_path(str(tmp_path)))
        time.sleep(0.1)
        assert watcher.sent == 1  # no marker, no further STATE
    finally:
        watcher.stop()
    assert out.getvalue() == "STATE\n"


def test_watcher_never_writes_anything_else(tmp_path, monkeypatch):
    """Even a hostile marker file content cannot change what is sent."""
    out = io.StringIO()
    watcher = RequestWatcher(str(tmp_path), out, poll_s=0.02, min_interval_s=0.0)
    with open(request.request_path(str(tmp_path)), "w", encoding="utf-8") as fh:
        fh.write("PLAY 1 0\nEND\n")
    watcher.start()
    try:
        deadline = time.monotonic() + 2
        while watcher.sent < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        watcher.stop()
    assert out.getvalue() == "STATE\n"


def test_run_dump_with_watcher_answers_request(tmp_path):
    out = str(tmp_path)
    stdout = io.StringIO()
    gate = threading.Event()

    def lines():
        yield '{"floor": 1}\n'
        request.place_request(out)
        gate.wait(2)  # give the watcher time to answer before EOF
        yield '{"floor": 2}\n'

    def release():
        deadline = time.monotonic() + 2
        while "STATE" not in stdout.getvalue() and time.monotonic() < deadline:
            time.sleep(0.01)
        gate.set()

    threading.Thread(target=release, daemon=True).start()
    run_dump(SnapshotStore(), stdout, lines(), out_dir=out, record_samples=None, watch_requests=True)
    assert stdout.getvalue() == "ready\nSTATE\n"


# ---------------------------------------------------------------- server side

def test_needs_intent_refresh_detects_debug_only_for_live_monsters():
    assert needs_intent_refresh(_combat("DEBUG")) is True
    assert needs_intent_refresh(_combat("ATTACK")) is False
    assert needs_intent_refresh(_combat("DEBUG", gone=True)) is False
    assert needs_intent_refresh({"game_state": {"screen_type": "MAP"}}) is False
    assert needs_intent_refresh(None) is False


def test_get_state_auto_refreshes_debug_intent(tmp_path):
    out = str(tmp_path)
    store = SnapshotStore()
    persist.write_latest(out, store.record(_combat("DEBUG")))

    def fake_dump():
        deadline = time.monotonic() + 2
        while not os.path.exists(request.request_path(out)) and time.monotonic() < deadline:
            time.sleep(0.01)
        request.clear_request(out)
        persist.write_latest(out, store.record(_combat("ATTACK")))

    thread = threading.Thread(target=fake_dump)
    thread.start()
    try:
        result = get_state(out)
    finally:
        thread.join()
    assert result["refresh"] == {"requested": True, "reason": "intent_debug", "resolved": True}
    assert result["seq"] == 2
    assert result["view"] == "compact"
    assert result["state"]["combat"]["monsters"][0]["intent"] == "ATTACK"


def test_get_state_without_debug_does_not_request(tmp_path):
    out = str(tmp_path)
    persist.write_latest(out, SnapshotStore().record(_combat("ATTACK")))
    result = get_state(out)
    assert "refresh" not in result
    assert not os.path.exists(request.request_path(out))


def test_refresh_times_out_and_clears_marker_when_no_dump(tmp_path):
    out = str(tmp_path)
    persist.write_latest(out, SnapshotStore().record(_combat("DEBUG")))
    start = time.monotonic()
    result = refresh_state(out, reason="explicit", timeout_s=0.3)
    assert time.monotonic() - start < 1.5
    assert result["refresh"] == {"requested": True, "reason": "explicit", "resolved": False}
    assert result["available"] is True and result["seq"] == 1  # still serves the last state
    assert not os.path.exists(request.request_path(out))


def test_explicit_refresh_wakes_on_timestamp_change_after_dump_restart(tmp_path):
    out = str(tmp_path)
    persist.write_latest(out, SnapshotStore().record({"floor": 1}))  # seq 1

    def fake_dump():
        while not os.path.exists(request.request_path(out)):
            time.sleep(0.01)
        request.clear_request(out)
        persist.write_latest(out, SnapshotStore().record({"floor": 1}))  # restarted dump: seq 1 again

    thread = threading.Thread(target=fake_dump)
    thread.start()
    try:
        result = get_state(out, refresh=True)
    finally:
        thread.join()
    assert result["refresh"]["resolved"] is True
