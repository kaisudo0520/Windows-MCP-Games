"""tkinter 視窗：上半是即時遊戲狀態，下半是動作日誌與手動控制。

在主執行緒執行。``attach(bridge)`` 用 ``after`` 每 50 ms 呼叫一次
``bridge.process_pending``，讀取執行緒永遠不會碰到 tk。
"""

from __future__ import annotations

import time
import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING, Any

from sts_bot.dispatch import DispatchResult
from sts_bot.state import Snapshot

if TYPE_CHECKING:
    from sts_bot.bridge import Bridge

PUMP_MS = 50
LOG_MAX_LINES = 2000
_ORIGIN_LABEL = {"tool": "按鈕", "operator": "操作者（Scroll Lock／左上角）"}


class TkView:
    def __init__(self, title: str = "sts-bot 橋接") -> None:
        self.root = tk.Tk()
        self.root.title(title)
        self.root.geometry("780x660")
        self._bridge: Bridge | None = None
        self._build()

    # ----------------------------------------------------------------- 建構

    def _build(self) -> None:
        root = self.root
        self.headline = tk.StringVar(value="等待遊戲送來狀態…")
        self.stop_text = tk.StringVar(value="輸入：可用")
        ttk.Label(root, textvariable=self.headline, font=("Microsoft JhengHei", 11, "bold")).pack(fill="x", padx=8, pady=(8, 2))
        self.stop_label = ttk.Label(root, textvariable=self.stop_text, foreground="green")
        self.stop_label.pack(fill="x", padx=8)

        panes = ttk.Frame(root)
        panes.pack(fill="x", padx=8, pady=4)
        self.hand = self._listbox(panes, "手牌（[n] = PLAY n）", 0)
        self.enemies = self._listbox(panes, "敵人（[n] = 目標序號）", 1)
        self.choices = self._listbox(panes, "選項（[n] = CHOOSE n）", 2)
        for col in range(3):
            panes.columnconfigure(col, weight=1)

        self.commands = tk.StringVar(value="可用指令：無")
        ttk.Label(root, textvariable=self.commands, wraplength=760).pack(fill="x", padx=8)

        self.log = tk.Text(root, height=18, state="disabled", wrap="word", font=("Microsoft JhengHei", 9))
        self.log.pack(fill="both", expand=True, padx=8, pady=4)
        self.log.tag_configure("sent", foreground="#0a6")
        self.log.tag_configure("rejected", foreground="#c00")
        self.log.tag_configure("note", foreground="#555")

        self._build_controls(root)

    def _listbox(self, parent: ttk.Frame, title: str, col: int) -> tk.Listbox:
        frame = ttk.LabelFrame(parent, text=title)
        frame.grid(row=0, column=col, sticky="nsew", padx=2)
        box = tk.Listbox(frame, height=8, font=("Microsoft JhengHei", 9), exportselection=False)
        box.pack(fill="both", expand=True)
        return box

    def _build_controls(self, root: tk.Tk) -> None:
        bar = ttk.Frame(root)
        bar.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Label(bar, text="指令：").pack(side="left")
        self.entry = ttk.Entry(bar)
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Return>", lambda _e: self._send_manual())
        ttk.Button(bar, text="送出", command=self._send_manual).pack(side="left", padx=2)
        ttk.Button(bar, text="要求狀態", command=self._request_state).pack(side="left", padx=2)
        ttk.Button(bar, text="停止", command=self._stop).pack(side="left", padx=(12, 2))
        ttk.Button(bar, text="恢復", command=self._resume).pack(side="left", padx=2)

    # ------------------------------------------------------------ 操作者控制

    def attach(self, bridge: Bridge) -> None:
        self._bridge = bridge
        bridge.start_reader()
        self.show_stop_state(bridge.latch.state().to_dict())
        self.root.after(PUMP_MS, self._pump)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _pump(self) -> None:
        if self._bridge is None:
            return
        alive = self._bridge.process_pending(block=False)
        if not alive:
            self.note("橋接結束，3 秒後關閉視窗")
            self.root.after(3000, self.root.destroy)
            return
        self.show_stop_state(self._bridge.latch.state().to_dict())
        self.root.after(PUMP_MS, self._pump)

    def _send_manual(self) -> None:
        if self._bridge is None:
            return
        text = self.entry.get()
        if text.strip():
            self._bridge.dispatch(text, origin="操作者")
            self.entry.delete(0, "end")

    def _request_state(self) -> None:
        if self._bridge is not None:
            self._bridge.request_state()

    def _stop(self) -> None:
        if self._bridge is not None:
            self._bridge.stop("停止按鈕")

    def _resume(self) -> None:
        if self._bridge is not None:
            self._bridge.resume()

    def _on_close(self) -> None:
        # 關窗前先進入停止狀態，避免留下模糊的閂鎖狀態。
        if self._bridge is not None:
            self._bridge.stop("視窗關閉")
        self.root.destroy()

    def mainloop(self) -> None:
        self.root.mainloop()

    # ------------------------------------------------------------------ 顯示

    def show_snapshot(self, snapshot: Snapshot) -> None:
        self.headline.set(snapshot.headline())
        self._fill(self.hand, [c.label() for c in snapshot.combat.hand] if snapshot.combat else [])
        self._fill(self.enemies, [m.label() for m in snapshot.combat.monsters] if snapshot.combat else [])
        self._fill(self.choices, [f"[{i}] {c}" for i, c in enumerate(snapshot.choices)])
        ready = "" if snapshot.ready_for_command else "（尚未就緒）"
        self.commands.set("可用指令：" + ("、".join(snapshot.available_commands) or "無") + ready)
        self._log(snapshot.headline(), "note")
        if snapshot.error:
            self._log(f"模組錯誤：{snapshot.error}", "rejected")

    def show_dispatch(self, result: DispatchResult) -> None:
        if result.ok:
            self._log(f"{result.origin} → 送出 {result.command}", "sent")
        else:
            self._log(f"{result.origin} → {result.command}  被擋：{result.reason}", "rejected")

    def show_stop_state(self, state: dict[str, Any]) -> None:
        if state.get("stopped"):
            origin = _ORIGIN_LABEL.get(str(state.get("origin")), str(state.get("origin")))
            self.stop_text.set(f"輸入：已停止，來源 {origin}（{state.get('reason')}）")
            self.stop_label.configure(foreground="red")
        else:
            self.stop_text.set("輸入：可用")
            self.stop_label.configure(foreground="green")

    def note(self, text: str) -> None:
        self._log(text, "note")

    @staticmethod
    def _fill(box: tk.Listbox, items: list[str]) -> None:
        box.delete(0, "end")
        for item in items:
            box.insert("end", item)

    def _log(self, text: str, tag: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.log.configure(state="normal")
        self.log.insert("end", f"{stamp} {text}\n", tag)
        excess = int(self.log.index("end-1c").split(".")[0]) - LOG_MAX_LINES
        if excess > 0:
            self.log.delete("1.0", f"{excess + 1}.0")
        self.log.see("end")
        self.log.configure(state="disabled")
