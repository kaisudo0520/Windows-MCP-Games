"""Unit tests for the state-dump core: allowlist, snapshot store, read loop."""

import dataclasses
import io

import pytest

from game_mcp.bridge.__main__ import run_dump
from game_mcp.bridge.protocol import SnapshotStore, parse_state_line, write_line


def test_allowlist_permits_only_ready():
    out = io.StringIO()
    write_line(out, "ready")
    assert out.getvalue() == "ready\n"


@pytest.mark.parametrize(
    "forbidden",
    ["state", "STATE 1", "PLAY 1", "END", "CHOOSE 0", "CLICK Left 1 2", "KEY 5", "POTION", "START", "PROCEED", "ready ", "Ready"],
)
def test_allowlist_rejects_everything_else(forbidden):
    with pytest.raises(RuntimeError):
        write_line(io.StringIO(), forbidden)


def test_parse_skips_blank_malformed_and_non_objects():
    assert parse_state_line("") is None
    assert parse_state_line("   \n") is None
    assert parse_state_line("{not json}") is None
    assert parse_state_line("[1, 2, 3]") is None  # JSON array is not a state object
    assert parse_state_line('{"floor": 3}') == {"floor": 3}


def test_seq_increments_from_one_per_recorded_state():
    store = SnapshotStore()
    assert store.current_seq() == 0
    first = store.record({"a": 1})
    second = store.record({"a": 2})
    assert (first.seq, second.seq) == (1, 2)
    assert store.latest() is second
    assert store.current_seq() == 2


def test_snapshot_is_immutable():
    snap = SnapshotStore().record({"hp": 50})
    with pytest.raises(dataclasses.FrozenInstanceError):
        snap.seq = 99  # type: ignore[misc]


class _AssertReadyFirst:
    """Iterable of stdin lines that asserts `ready` was written before the first read."""

    def __init__(self, stdout: io.StringIO, lines: list[str]) -> None:
        self._stdout = stdout
        self._lines = iter(lines)

    def __iter__(self):
        return self

    def __next__(self) -> str:
        assert self._stdout.getvalue().startswith("ready\n")
        return next(self._lines)


def test_ready_is_written_exactly_once_before_any_read(tmp_path):
    stdout = io.StringIO()
    store = SnapshotStore()
    lines = ['{"floor": 1}\n', "garbage\n", '{"floor": 2}\n']
    run_dump(store, stdout, _AssertReadyFirst(stdout, lines), out_dir=str(tmp_path), record_samples=None)
    assert stdout.getvalue() == "ready\n"  # one-way: only the handshake is ever written
    assert store.current_seq() == 2  # the two valid lines; the garbage skipped
    assert store.latest().state == {"floor": 2}
