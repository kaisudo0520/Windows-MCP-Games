"""Bridge loop end-to-end over in-memory streams: handshake, states, policy, EOF."""

import io
import json

from game_mcp.config import Settings
from game_mcp.safety import HeldInputs, StopLatch
from sts_bot.bridge import Bridge
from sts_bot.protocol import CommLink, parse_message
from sts_bot.recorder import RunRecorder, read_events
from sts_bot.state import Snapshot


class RecordingView:
    def __init__(self):
        self.snapshots: list[Snapshot] = []
        self.dispatches = []
        self.notes: list[str] = []
        self.stops = []

    def show_snapshot(self, snapshot):
        self.snapshots.append(snapshot)

    def show_dispatch(self, result):
        self.dispatches.append(result)

    def show_stop_state(self, state):
        self.stops.append(state)

    def note(self, text):
        self.notes.append(text)


class EndTurnPolicy:
    name = "end-turn"

    def decide(self, snapshot):
        return "END" if snapshot.in_combat and "end" in snapshot.available_commands else None


class ExplodingPolicy:
    name = "boom"

    def decide(self, snapshot):
        raise RuntimeError("bug")


def _bridge(tmp_path, stdin: bytes, policy=None):
    out = io.BytesIO()
    link = CommLink(io.BytesIO(stdin), out)
    latch = StopLatch(HeldInputs(), Settings())
    view = RecordingView()
    recorder = RunRecorder(tmp_path, run_id="run")
    bridge = Bridge(link, latch, recorder=recorder, policy=policy, view=view)
    return bridge, out, view, recorder


def test_headless_run_records_states_and_stops_at_eof(tmp_path, combat_state_line, quiet_release):
    bridge, out, view, recorder = _bridge(tmp_path, combat_state_line * 2)
    bridge.link.handshake()
    bridge.run_headless()
    assert bridge.eof and bridge.messages_seen == 2
    assert len(view.snapshots) == 2
    assert out.getvalue() == b"ready\n", "sensor-only bridge sends nothing"
    kinds = [e["kind"] for e in read_events(recorder.path)]
    assert kinds == ["state", "state", "note"]
    assert any("EOF" in n for n in view.notes)


def test_policy_command_is_dispatched_once_per_ready_state(tmp_path, combat_state_dict, quiet_release):
    ready = (json.dumps(combat_state_dict) + "\n").encode()
    combat_state_dict["ready_for_command"] = False
    busy = (json.dumps(combat_state_dict) + "\n").encode()
    bridge, out, view, _ = _bridge(tmp_path, ready + busy + ready, policy=EndTurnPolicy())
    bridge.run_headless()
    assert out.getvalue() == b"END\nEND\n"
    assert [d.origin for d in view.dispatches] == ["策略:end-turn"] * 2


def test_stopped_latch_turns_policy_commands_into_rejections(tmp_path, combat_state_line, quiet_release):
    bridge, out, view, _ = _bridge(tmp_path, combat_state_line, policy=EndTurnPolicy())
    bridge.stop("test")
    bridge.run_headless()
    assert out.getvalue() == b""
    assert len(view.dispatches) == 1 and not view.dispatches[0].ok
    assert view.stops and view.stops[0]["stopped"]


def test_exploding_policy_does_not_kill_the_loop(tmp_path, combat_state_line, quiet_release):
    bridge, out, view, _ = _bridge(tmp_path, combat_state_line * 2, policy=ExplodingPolicy())
    bridge.run_headless()
    assert bridge.messages_seen == 2 and out.getvalue() == b""
    assert any("拋出例外" in n for n in view.notes)


def test_bad_lines_are_noted_and_skipped(tmp_path, combat_state_line, quiet_release):
    bridge, _, view, recorder = _bridge(tmp_path, b"not json\n" + combat_state_line)
    bridge.run_headless()
    assert bridge.messages_seen == 1
    assert any("無法解析" in n for n in view.notes)
    assert read_events(recorder.path)[0]["kind"] == "error"


def test_operator_controls(tmp_path, combat_state_line, quiet_release):
    # Feed one state directly so the stream never reaches EOF while we drive the controls.
    bridge, out, view, _ = _bridge(tmp_path, b"")
    bridge.handle_message(parse_message(combat_state_line.decode("utf-8")))
    assert bridge.dispatch("end", origin="operator").ok
    assert bridge.request_state().ok
    bridge.stop("button")
    assert not bridge.dispatch("end").ok
    assert not bridge.resume()["stopped"]
    assert bridge.dispatch("end").ok
    assert out.getvalue() == b"end\nSTATE\nend\n"
