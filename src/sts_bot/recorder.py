"""只追加不修改的 JSONL 紀錄，記下所有經過協定的東西。

每個橋接程序一個目錄（``<base>/<時間戳>/``），裡面一個 ``events.jsonl``。
每行是 ``{"t": unix時間, "seq": 序號, "kind": 種類, "data": {...}}``。
目前用到的 kind：``state``（含原始 JSON）、``error``、``command``、``rejected``、``note``。
要回放一局就是讀這個檔；除此之外不落地任何東西。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any


class RunRecorder:
    def __init__(self, base_dir: Path, run_id: str | None = None) -> None:
        self.run_id = run_id or time.strftime("%Y%m%d-%H%M%S")
        self.dir = Path(base_dir) / self.run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "events.jsonl"
        self._lock = threading.Lock()
        self._fh = self.path.open("a", encoding="utf-8")
        self._seq = 0

    def record(self, kind: str, data: dict[str, Any]) -> int:
        line = {"t": time.time(), "seq": self._next_seq(), "kind": kind, "data": data}
        text = json.dumps(line, ensure_ascii=False, default=str)
        with self._lock:
            self._fh.write(text + "\n")
            self._fh.flush()
        return line["seq"]

    def _next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    def close(self) -> None:
        with self._lock:
            if not self._fh.closed:
                self._fh.close()


def read_events(path: Path) -> list[dict[str, Any]]:
    """讀回一個 events.jsonl（測試與離線回放用）。"""
    with Path(path).open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]
