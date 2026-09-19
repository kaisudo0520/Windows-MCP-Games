"""決策策略。策略看到一個 Snapshot，回傳一個指令或 None。

第 1 階段只有 ``NullPolicy``（永遠不出招）。之後依序加入：規則式基準策略、
Jev 負責戰鬥外選擇的策略、再到戰鬥策略。
"""

from __future__ import annotations

from typing import Protocol

from sts_bot.state import Snapshot


class Policy(Protocol):
    name: str

    def decide(self, snapshot: Snapshot) -> str | None:
        """回傳要送給模組的指令行；回傳 None 表示這個狀態不做事。"""


class NullPolicy:
    name = "sensor-only"

    def decide(self, snapshot: Snapshot) -> str | None:
        return None
