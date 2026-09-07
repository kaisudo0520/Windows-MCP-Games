"""Tests for the advisor-mode review fixes: stdin decoding, Windows replace retry,
meta/state write ordering, seq continuity across dump restarts, torn/transient reads,
and wait_for_state waking on a timestamp change."""

import io
import json
import logging
import os
import threading
import time

import pytest

from game_mcp.bridge import mcp_server, persist
from game_mcp.bridge.__main__ import SampleRetention, _parse_args, run_dump
from game_mcp.bridge.mcp_server import read_state, wait_for_state
from game_mcp.bridge.protocol import LineDecoder, SnapshotStore

CJK_TEXT = "打擊"  # encodable in cp950 and utf-8, with different bytes


def _load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------- decoding

def test_decoder_prefers_utf8():
    assert LineDecoder().decode(CJK_TEXT.encode("utf-8")) == CJK_TEXT


def test_decoder_falls_back_without_raising_and_warns_once(caplog):
    decoder = LineDecoder()
    raw = CJK_TEXT.encode("cp950")
    with caplog.at_level(logging.WARNING, logger="game_mcp.bridge.protocol"):
        out = decoder.decode(raw)
        decoder.decode(raw)  # second time must not warn again
    assert isinstance(out, str)
    if decoder.fallback.lower() in {"cp950", "big5"}:
        assert out == CJK_TEXT
    warnings = [r for r in caplog.records if "stdin line is not valid UTF-8" in r.getMessage()]
    assert len(warnings) == 1
    assert raw[:2].hex(" ") in warnings[0].getMessage()  # raw bytes are logged for diagnosis


def test_decoder_keeps_utf8_names_when_one_stray_byte_breaks_the_line():
    # Mostly UTF-8 (CJK card names) with one invalid byte elsewhere: names must survive.
    raw = ('{"hand":[{"name":"' + CJK_TEXT + '"}],"junk":"').encode("utf-8") + b"\xff" + b'"}\n'
    out = LineDecoder().decode(raw)
    assert CJK_TEXT in out
    assert out.count("\ufffd") == 1


def test_decoder_forced_encoding_replaces_bad_bytes():
    assert LineDecoder("ascii").decode(b"\xff{}") == "�{}"


def test_run_dump_survives_undecodable_bytes(tmp_path):
    store = SnapshotStore()
    lines = [b'{"floor": 1}\n', b"\xff\xfe\xfd not json\n", b'{"floor": 2}\n']
    run_dump(store, io.StringIO(), iter(lines), out_dir=str(tmp_path), record_samples=None)
    assert store.current_seq() == 2 and store.latest().state == {"floor": 2}


# ---------------------------------------------------------------- persistence

def test_replace_is_retried_on_transient_permission_error(monkeypatch, tmp_path):
    real_replace = os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError(5, "sharing violation")
        real_replace(src, dst)

    monkeypatch.setattr(persist.os, "replace", flaky)
    monkeypatch.setattr(persist, "_REPLACE_BACKOFF_S", 0.001)
    path = str(tmp_path / "latest.json")
    assert persist.atomic_write_json(path, {"seq": 1}) is True
    assert calls["n"] == 3
    assert _load(path) == {"seq": 1}
    assert [n for n in os.listdir(tmp_path) if n.endswith(".tmp")] == []


def test_meta_is_not_written_when_latest_write_fails(monkeypatch, tmp_path):
    out = str(tmp_path)
    store = SnapshotStore()
    assert persist.write_latest(out, store.record({"floor": 1})) is True

    def fail_latest(tmp, path):
        if path.endswith(persist.LATEST_NAME):
            os.remove(tmp)
            return False
        os.replace(tmp, path)
        return True

    monkeypatch.setattr(persist, "_replace_with_retry", fail_latest)
    assert persist.write_latest(out, store.record({"floor": 2})) is False
    meta = _load(os.path.join(out, persist.META_NAME))
    latest = _load(os.path.join(out, persist.LATEST_NAME))
    assert meta["seq"] == 1 == latest["seq"]  # meta never leads the state on disk
    assert read_state(out)["state"] == {"floor": 1}


def test_seq_continues_across_dump_restart(tmp_path):
    out = str(tmp_path)
    first = SnapshotStore(start_seq=persist.read_last_seq(out))
    lines = ['{"floor": 1}\n', '{"floor": 2}\n']
    run_dump(first, io.StringIO(), iter(lines), out_dir=out, record_samples=None)
    assert persist.read_last_seq(out) == 2
    second = SnapshotStore(start_seq=persist.read_last_seq(out))
    run_dump(second, io.StringIO(), iter(['{"floor": 3}\n']), out_dir=out, record_samples=None)
    result = read_state(out)
    assert result["seq"] == 3 and result["state"] == {"floor": 3}


def test_read_last_seq_tolerates_garbage(tmp_path):
    (tmp_path / persist.META_NAME).write_text("not json", encoding="utf-8")
    assert persist.read_last_seq(str(tmp_path)) == 0
    assert persist.read_last_seq(str(tmp_path / "missing")) == 0


def test_sweep_removes_stale_temp_files(tmp_path):
    (tmp_path / ".dump-abc.tmp").write_text("x")
    (tmp_path / "keep.json").write_text("{}")
    assert persist.sweep_temp_files(str(tmp_path)) == 1
    assert sorted(os.listdir(tmp_path)) == ["keep.json"]


# ---------------------------------------------------------------- reader

def test_reader_serves_last_good_state_when_meta_leads_latest(tmp_path):
    out = str(tmp_path)
    persist.write_latest(out, SnapshotStore().record({"floor": 1}))
    meta_path = os.path.join(out, persist.META_NAME)
    meta = _load(meta_path)
    with open(meta_path, "w", encoding="utf-8") as fh:
        json.dump({**meta, "seq": 2}, fh)
    result = read_state(out)
    assert result["available"] is True and result["seq"] == 1 and result["state"] == {"floor": 1}


def test_reader_reports_unreadable_for_persistently_corrupt_latest(monkeypatch, tmp_path):
    (tmp_path / persist.LATEST_NAME).write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(mcp_server, "_TORN_READ_SLEEP_S", 0.001)
    assert read_state(str(tmp_path)) == {"available": False, "reason": "unreadable"}


def test_reader_retries_transient_open_failure(monkeypatch, tmp_path):
    out = str(tmp_path)
    persist.write_latest(out, SnapshotStore().record({"floor": 5}))
    real_open = open
    failures = {"n": 0}

    def flaky_open(path, *args, **kwargs):
        if str(path).endswith(persist.LATEST_NAME) and failures["n"] < 1:
            failures["n"] += 1
            raise PermissionError(5, "sharing violation")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", flaky_open)
    monkeypatch.setattr(mcp_server, "_TORN_READ_SLEEP_S", 0.001)
    result = read_state(out)
    assert result["available"] is True and result["state"] == {"floor": 5}
    assert failures["n"] == 1


def test_concurrent_writer_never_yields_torn_state(tmp_path):
    out = str(tmp_path)
    store = SnapshotStore()
    payload = {"payload": "x" * 20000}
    stop = threading.Event()

    def writer():
        while not stop.is_set():
            persist.write_latest(out, store.record(payload))

    thread = threading.Thread(target=writer)
    thread.start()
    deadline = time.monotonic() + 1.5
    seen_available = 0
    try:
        while time.monotonic() < deadline:
            result = read_state(out)
            if result["available"]:
                seen_available += 1
                assert result["state"] == payload
            else:
                assert result["reason"] in {"no_state_yet", "unreadable"}
    finally:
        stop.set()
        thread.join()
    assert seen_available > 0
    assert read_state(out)["seq"] == store.current_seq()


def test_wait_for_state_wakes_on_timestamp_change_with_same_seq(tmp_path):
    out = str(tmp_path)
    persist.write_latest(out, SnapshotStore().record({"floor": 1}))  # seq 1

    def restart_and_push():
        time.sleep(0.2)
        persist.write_latest(out, SnapshotStore().record({"floor": 9}))  # fresh dump, seq 1 again

    thread = threading.Thread(target=restart_and_push)
    thread.start()
    try:
        result = wait_for_state(out, since_seq=1, timeout_s=5, poll_s=0.05)
    finally:
        thread.join()
    assert result["timed_out"] is False and result["state"] == {"floor": 9}
    assert result["received_unix"] is not None


@pytest.mark.parametrize("since_seq", [0, 3])
def test_wait_for_state_resolves_immediately_when_seq_already_ahead(tmp_path, since_seq):
    out = str(tmp_path)
    store = SnapshotStore(start_seq=3)
    persist.write_latest(out, store.record({"floor": 4}))  # seq 4
    start = time.monotonic()
    result = wait_for_state(out, since_seq=since_seq, timeout_s=5, poll_s=0.05)
    assert result["timed_out"] is False and result["seq"] == 4
    assert time.monotonic() - start < 1.0


# ---------------------------------------------------------------- sample retention

def test_prune_samples_keeps_newest_and_drops_old(tmp_path):
    for i in range(6):
        f = tmp_path / f"s_{i:06d}.json"
        f.write_text("{}")
        os.utime(f, (1_000_000 + i, 1_000_000 + i))  # ancient mtimes, ascending
    (tmp_path / "notes.txt").write_text("keep me")
    removed = persist.prune_samples(str(tmp_path), keep_files=4, max_age_h=1.0, by_age=False)
    assert removed == 2
    assert sorted(p.name for p in tmp_path.glob("*.json")) == [f"s_{i:06d}.json" for i in range(2, 6)]
    removed = persist.prune_samples(str(tmp_path), keep_files=100, max_age_h=1.0, by_age=True)
    assert removed == 4 and list(tmp_path.glob("*.json")) == []
    assert (tmp_path / "notes.txt").exists()
    assert persist.prune_samples(None, keep_files=1, max_age_h=1.0, by_age=True) == 0
    assert persist.prune_samples(str(tmp_path / "missing"), keep_files=1, max_age_h=1.0, by_age=True) == 0


def test_run_dump_prunes_samples_by_count(tmp_path):
    out = str(tmp_path / "out")
    samples = str(tmp_path / "samples")
    lines = ['{"floor": %d}' % i + chr(10) for i in range(5)]
    run_dump(
        SnapshotStore(), io.StringIO(), iter(lines), out_dir=out, record_samples=samples,
        retention=SampleRetention(keep_files=2, max_age_h=72),
    )
    names = sorted(os.listdir(samples))
    assert len(names) == 2 and names[-1].endswith("_000005.json")


def test_parse_args_retention_defaults_and_validation(tmp_path):
    args = _parse_args(["--out", str(tmp_path)])
    assert args.samples_keep == 500 and args.samples_max_age_h == 72.0
    args = _parse_args(["--out", str(tmp_path), "--samples-keep", "10", "--samples-max-age-h", "1.5"])
    assert args.samples_keep == 10 and args.samples_max_age_h == 1.5
    with pytest.raises(SystemExit):
        _parse_args(["--out", str(tmp_path), "--samples-keep", "0"])
