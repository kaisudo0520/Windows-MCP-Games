"""顯示層抽象，讓橋接核心不依賴 tkinter。

``ui.py`` 的 ``TkView`` 是真正的視窗；``HeadlessView`` 改成寫日誌，
給測試與 ``--ui none`` 使用。
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from sts_bot.dispatch import DispatchResult
from sts_bot.state import Snapshot

logger = logging.getLogger(__name__)


class View(Protocol):
    def show_snapshot(self, snapshot: Snapshot) -> None: ...

    def show_dispatch(self, result: DispatchResult) -> None: ...

    def show_stop_state(self, state: dict[str, Any]) -> None: ...

    def note(self, text: str) -> None: ...


class HeadlessView:
    def show_snapshot(self, snapshot: Snapshot) -> None:
        for line in snapshot.lines():
            logger.info("狀態｜%s", line)

    def show_dispatch(self, result: DispatchResult) -> None:
        if result.ok:
            logger.info("動作｜%s → %s", result.origin, result.command)
        else:
            logger.warning("動作｜%s → %s 被擋：%s", result.origin, result.command, result.reason)

    def show_stop_state(self, state: dict[str, Any]) -> None:
        logger.info("停止｜%s", state)

    def note(self, text: str) -> None:
        logger.info("備註｜%s", text)
