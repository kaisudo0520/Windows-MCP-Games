"""橋接核心：讀狀態、顯示、記錄，讓策略或操作者一次送一個指令。

執行緒模型：``start_reader()`` 在背景執行緒做阻塞的 stdin 讀取，把訊息放進佇列。
擁有主執行緒的人（tk 的 mainloop 或無頭迴圈）呼叫 ``process_pending()`` 把佇列清掉，
所以所有狀態變更與畫面更新都發生在同一條執行緒上。
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Any

from game_mcp.errors import GameMcpError
from game_mcp.safety import StopLatch
from sts_bot.dispatch import CommandDispatcher, DispatchResult
from sts_bot.policy import NullPolicy, Policy
from sts_bot.protocol import CommLink, Message, ProtocolError
from sts_bot.recorder import RunRecorder
from sts_bot.state import Snapshot
from sts_bot.view import HeadlessView, View

logger = logging.getLogger(__name__)

_EOF = object()


class Bridge:
    def __init__(
        self,
        link: CommLink,
        latch: StopLatch,
        *,
        recorder: RunRecorder | None = None,
        policy: Policy | None = None,
        view: View | None = None,
    ) -> None:
        self.link = link
        self.latch = latch
        self.recorder = recorder
        self.policy: Policy = policy or NullPolicy()
        self.view: View = view or HeadlessView()
        self.dispatcher = CommandDispatcher(link, latch, recorder)
        self.snapshot: Snapshot | None = None
        self.messages_seen = 0
        self.eof = False
        self._events: queue.Queue[Any] = queue.Queue()
        self._reader: threading.Thread | None = None

    # ------------------------------------------------------------- 生命週期

    def start_reader(self) -> None:
        if self._reader is not None:
            return
        self._reader = threading.Thread(target=self._read_loop, name="commmod-reader", daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        while True:
            try:
                msg = self.link.read_message()
            except ProtocolError as exc:
                self._events.put(exc)
                continue
            except (OSError, ValueError) as exc:  # 串流在底下被關掉
                logger.error("讀取 stdin 失敗：%s", exc)
                msg = None
            if msg is None:
                self._events.put(_EOF)
                return
            self._events.put(msg)

    def process_pending(self, *, block: bool = False, timeout: float | None = None) -> bool:
        """處理佇列中的事件。EOF 處理完後回傳 False。"""
        try:
            event = self._events.get(block=block, timeout=timeout)
        except queue.Empty:
            return not self.eof
        while True:
            self._handle_event(event)
            if self.eof:
                return False
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                return True

    def _handle_event(self, event: Any) -> None:
        if event is _EOF:
            self.eof = True
            self.view.note("遊戲關閉了連線（stdin EOF）")
            self._record("note", {"text": "eof"})
        elif isinstance(event, ProtocolError):
            self.view.note(f"模組送來無法解析的行：{event}")
            self._record("error", {"text": str(event)})
        elif isinstance(event, Message):
            self.handle_message(event)

    def run_headless(self) -> None:
        self.start_reader()
        while self.process_pending(block=True, timeout=1.0):
            pass

    # ---------------------------------------------------------------- 訊息

    def handle_message(self, msg: Message) -> Snapshot:
        self.messages_seen += 1
        snapshot = Snapshot.from_message(msg)
        self.snapshot = snapshot
        self._record("state", {"raw": msg.raw, "encoding": msg.encoding})
        if snapshot.error:
            self._record("error", {"text": snapshot.error})
        self.view.show_snapshot(snapshot)
        if snapshot.ready_for_command:
            self._consult_policy(snapshot)
        return snapshot

    def _consult_policy(self, snapshot: Snapshot) -> None:
        try:
            command = self.policy.decide(snapshot)
        except Exception:  # 策略有 bug 不能把橋接程式弄死
            logger.exception("策略 %s 拋出例外", self.policy.name)
            self.view.note(f"策略 {self.policy.name} 拋出例外，詳見日誌")
            return
        if command:
            self.dispatch(command, origin=f"策略:{self.policy.name}")

    # ------------------------------------------------------------ 操作者控制

    def dispatch(self, command: str, origin: str = "操作者") -> DispatchResult:
        result = self.dispatcher.send(command, self.snapshot, origin)
        self.view.show_dispatch(result)
        return result

    def request_state(self) -> DispatchResult:
        return self.dispatch("STATE", origin="操作者")

    def stop(self, reason: str = "停止按鈕") -> dict[str, Any]:
        state = self.latch.engage("tool", reason).to_dict()
        self.view.show_stop_state(state)
        self._record("note", {"text": "stop", "state": state})
        return state

    def resume(self) -> dict[str, Any]:
        try:
            state = self.latch.resume().to_dict()
        except GameMcpError as exc:
            self.view.note("無法恢復：這是操作者觸發的停止，只能重啟橋接程式解除")
            logger.warning("%s", exc.message)
            return self.latch.state().to_dict()
        self.view.show_stop_state(state)
        self._record("note", {"text": "resume", "state": state})
        return state

    def _record(self, kind: str, data: dict[str, Any]) -> None:
        if self.recorder is not None:
            self.recorder.record(kind, data)
