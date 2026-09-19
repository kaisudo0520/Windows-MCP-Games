"""每個指令送進遊戲前必經的唯一關卡。

檢查順序，第一個不過就擋下：
1. 動詞是模組認得的；
2. 停止閂鎖（工具或操作者）沒有啟動，唯讀動詞除外；
3. 最新狀態說 ``ready_for_command``（唯讀動詞除外）；
4. 動詞在該狀態的 ``available_commands`` 內（唯讀動詞除外）。

每次送出與每次拒絕都會記錄。停止閂鎖就是 MCP 分支用在桌面輸入的那個類別，
所以 Scroll Lock／滑鼠左上角的緊急停止原樣有效。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from game_mcp.errors import GameMcpError
from game_mcp.safety import StopLatch
from sts_bot.protocol import READ_ONLY_COMMANDS, CommLink, ProtocolError, normalize_command
from sts_bot.recorder import RunRecorder
from sts_bot.state import Snapshot

logger = logging.getLogger(__name__)

_ORIGIN_LABEL = {"tool": "按鈕／工具", "operator": "操作者"}


@dataclass(frozen=True)
class DispatchResult:
    ok: bool
    command: str
    origin: str
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "command": self.command, "origin": self.origin, "reason": self.reason}


class CommandDispatcher:
    def __init__(self, link: CommLink, latch: StopLatch, recorder: RunRecorder | None = None) -> None:
        self._link = link
        self._latch = latch
        self._recorder = recorder

    def check(self, command: str, snapshot: Snapshot | None) -> tuple[str, str | None]:
        """回傳 (正規化後的指令, 拒絕原因或 None)。"""
        try:
            line = normalize_command(command)
        except ProtocolError as exc:
            return command, str(exc)
        verb = line.split(" ", 1)[0].lower()
        if verb in READ_ONLY_COMMANDS:
            return line, None
        try:
            self._latch.require_running()
        except GameMcpError as exc:
            origin = _ORIGIN_LABEL.get(str(exc.details.get("origin")), str(exc.details.get("origin")))
            return line, f"輸入已停止（{origin}）：{exc.details.get('reason')}"
        if snapshot is None:
            return line, "還沒收到任何遊戲狀態"
        if not snapshot.ready_for_command:
            return line, "遊戲尚未就緒，不接受指令"
        if verb not in snapshot.available_commands:
            return line, f"'{verb}' 不在目前可用指令內 {list(snapshot.available_commands)}"
        return line, None

    def send(self, command: str, snapshot: Snapshot | None, origin: str = "policy") -> DispatchResult:
        line, reason = self.check(command, snapshot)
        if reason is not None:
            result = DispatchResult(False, line, origin, reason)
            logger.warning("擋下 %s 的指令 %r：%s", origin, line, reason)
            self._record("rejected", result)
            return result
        try:
            sent = self._link.send(line)
        except (OSError, ProtocolError) as exc:
            result = DispatchResult(False, line, origin, f"寫入失敗：{exc}")
            logger.error("送出 %r 失敗：%s", line, exc)
            self._record("rejected", result)
            return result
        result = DispatchResult(True, sent, origin)
        logger.info("送出 %s 的指令：%s", origin, sent)
        self._record("command", result)
        return result

    def _record(self, kind: str, result: DispatchResult) -> None:
        if self._recorder is not None:
            self._recorder.record(kind, result.to_dict())
