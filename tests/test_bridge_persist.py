"""Unit tests for the state-dump filesystem persistence."""

import json
import os

from game_mcp.bridge import persist
from game_mcp.bridge.__main__ import run_dump
from game_mcp.bridge.protocol import SnapshotStore


def _snap(state):
    return SnapshotStore().record(state)


def test_atomic_write_produces_valid_json_and_no_temp_files(tmp_path):
    path = str(tmp_path / "state" / "latest.json")
    snap = _snap({"floor": 7, "gold": 99})
    assert persist.atomic_write_json(path, persist.latest_payload(snap)) is True
    with open(path, encoding="utf-8") as fh:
        loaded = json.load(fh)
    assert loaded["seq"] == 1 and loaded["state"] == {"floor": 7, "gold": 99}
    assert loaded["received_wall"] == snap.received_wall_iso
    leftovers = [n for n in os.listdir(tmp_path / "state") if n.endswith(".tmp")]
    assert leftovers == []


def test_write_latest_writes_state_and_meta(tmp_path):
    out = str(tmp_path)
    snap = _snap({"floor": 4})
    assert persist.write_latest(out, snap) is True
    with open(os.path.join(out, persist.LATEST_NAME), encoding="utf-8") as fh:
        latest = json.load(fh)
    with open(os.path.join(out, persist.META_NAME), encoding="utf-8") as fh:
        meta = json.load(fh)
    assert latest["state"] == {"floor": 4} and latest["seq"] == 1
    assert meta == {"seq": 1, "received_wall": snap.received_wall_iso, "received_unix": snap.received_unix}
    assert "state" not in meta  # meta is the cheap change-detection sidecar


def test_record_samples_writes_one_file_per_push(tmp_path):
    directory = str(tmp_path / "samples")
    store = SnapshotStore()
    paths = [persist.record_sample(directory, store.record({"i": i})) for i in range(3)]
    assert all(p is not None for p in paths)
    files = sorted(os.listdir(directory))
    assert len(files) == 3 and all(name.endswith(".json") for name in files)
    assert any("_000002.json" in name for name in files)
    with open(paths[1], encoding="utf-8") as fh:
        assert json.load(fh)["seq"] == 2


def test_run_dump_persists_meta_and_samples(tmp_path):
    out = str(tmp_path / "out")
    samples = str(tmp_path / "samples")
    store = SnapshotStore()
    lines = ['{"floor": 1}\n', '{"floor": 2}\n']
    import io

    run_dump(store, io.StringIO(), iter(lines), out_dir=out, record_samples=samples)
    with open(os.path.join(out, persist.META_NAME), encoding="utf-8") as fh:
        assert json.load(fh)["seq"] == 2
    assert len(os.listdir(samples)) == 2
    assert [n for n in os.listdir(out) if n.endswith(".tmp")] == []


def test_default_state_dir_lives_under_local_appdata(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert persist.default_state_dir() == os.path.join(str(tmp_path), "sts-game-mcp", "state")
