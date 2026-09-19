"""進入點：``sts-bot run``（由 CommunicationMod 啟動）與輔助子命令。

啟動順序很重要：``ready`` 握手要在任何慢的東西（tk、監看執行緒）之前送出，
才不會撞到模組的啟動逾時。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
from pathlib import Path

from game_mcp.config import load_settings
from game_mcp.safety import HeldInputs, OperatorWatcher, StopLatch
from sts_bot import __version__
from sts_bot.bridge import Bridge
from sts_bot.protocol import CommLink
from sts_bot.recorder import RunRecorder
from sts_bot.view import HeadlessView

logger = logging.getLogger("sts_bot")


def default_run_dir() -> Path:
    base = os.getenv("STS_BOT_RUN_DIR")
    if base:
        return Path(base)
    local = os.getenv("LOCALAPPDATA") or str(Path.home())
    return Path(local) / "sts-bot" / "runs"


def _setup_logging(level: str, run_dir: Path | None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if run_dir is not None:
        handlers.append(logging.FileHandler(run_dir / "bridge.log", encoding="utf-8"))
    logging.basicConfig(level=getattr(logging, level), format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)


def cmd_run(args: argparse.Namespace) -> int:
    link = CommLink(sys.stdin.buffer, sys.stdout.buffer, encoding=args.encoding)
    link.handshake()  # 第一件事就是上線，之後才做可能慢的初始化

    recorder = RunRecorder(args.run_dir)
    _setup_logging(args.log_level, recorder.dir)
    logger.info("sts-bot %s 啟動；紀錄目錄 %s", __version__, recorder.dir)

    settings = load_settings()
    held = HeldInputs()
    latch = StopLatch(held, settings)
    try:
        watcher = OperatorWatcher(latch, settings)
    except ValueError as exc:
        logger.error("%s", exc)
        return 2
    watcher.start()

    view = HeadlessView() if args.ui == "none" else _make_tk_view()
    bridge = Bridge(link, latch, recorder=recorder, view=view)
    try:
        if args.ui == "none":
            bridge.run_headless()
        else:
            view.attach(bridge)  # type: ignore[union-attr]
            view.mainloop()  # type: ignore[union-attr]
    finally:
        watcher.stop()
        recorder.close()
        logger.info("橋接結束：收到訊息 %d 則，送出指令 %d 個", bridge.messages_seen, link.commands_sent)
    return 0


def _make_tk_view():
    from sts_bot.ui import TkView

    return TkView()


class _FixtureStream:
    """``demo`` 用的 stdin 替身：先吐出 fixture 的行，然後像閒置的模組一樣阻塞。"""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)
        self._closed = threading.Event()

    def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        self._closed.wait()
        return b""

    def close(self) -> None:
        self._closed.set()


def cmd_demo(args: argparse.Namespace) -> int:
    """用預錄的狀態開視窗，沒有遊戲也能操作介面。

    在視窗打的指令會原樣寫到 stdout，也就是實機時會送給模組的那一行，
    所以主控台看得到「真的跑會送出什麼」。
    """
    fixture = Path(args.fixture)
    raw = json.loads(fixture.read_text(encoding="utf-8"))
    stream = _FixtureStream([(json.dumps(raw, ensure_ascii=False) + "\n").encode("utf-8")])
    link = CommLink(stream, sys.stdout.buffer)  # type: ignore[arg-type]
    _setup_logging(args.log_level, None)
    latch = StopLatch(HeldInputs(), load_settings())
    view = _make_tk_view()
    bridge = Bridge(link, latch, view=view)
    view.note(f"示範模式：狀態來自 {fixture.name}；指令會印到主控台，不會送進遊戲")
    view.attach(bridge)
    try:
        view.mainloop()
    finally:
        stream.close()
    return 0


def cmd_print_config(args: argparse.Namespace) -> int:
    """印出要貼進 CommunicationMod config.properties 的 ``command=`` 行。"""
    python = Path(sys.executable).resolve()
    launcher = Path(__file__).resolve().parents[2] / "sts_bot_launch.py"
    if launcher.is_file():
        # 原始碼目錄：遊戲那台電腦不需要安裝套件，也不需要 PYTHONPATH。
        parts = [str(python), "-u", str(launcher), "--ui", args.ui]
    else:
        parts = [str(python), "-u", "-m", "sts_bot", "run", "--ui", args.ui]
    if args.run_dir is not None:
        parts += ["--run-dir", str(Path(args.run_dir).resolve())]
    # Java .properties 的跳脫規則：反斜線與冒號都要跳脫。
    escaped = " ".join(p.replace("\\", "\\\\").replace(":", "\\:") for p in parts)
    sys.stdout.write(
        "# CommunicationMod config.properties（通常在 %LOCALAPPDATA%\\ModTheSpire\\CommunicationMod\\config.properties）\n"
        f"command={escaped}\n"
        "runAtGameStart=true\n"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sts-bot", description="殺戮尖塔 CommunicationMod 橋接程式。")
    parser.add_argument("--version", action="version", version=f"sts-bot {__version__}")
    sub = parser.add_subparsers(dest="cmd")

    run = sub.add_parser("run", help="執行橋接（由 CommunicationMod 啟動）")
    run.add_argument("--ui", choices=["tk", "none"], default="tk", help="tk 開視窗；none 只寫日誌")
    run.add_argument("--run-dir", type=Path, default=default_run_dir(), help="events.jsonl 與 bridge.log 的存放目錄")
    run.add_argument("--encoding", default=None, help="強制 stdin/stdout 編碼（預設先試 utf-8 再試系統地區編碼）")
    run.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    run.set_defaults(func=cmd_run)

    demo = sub.add_parser("demo", help="用預錄狀態開視窗，不需要遊戲")
    demo.add_argument("--fixture", type=Path, default=Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "commmod_combat_state.json")
    demo.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    demo.set_defaults(func=cmd_demo)

    cfg = sub.add_parser("print-config", help="印出模組 config.properties 用的 command= 行")
    cfg.add_argument("--ui", choices=["tk", "none"], default="tk")
    cfg.add_argument("--run-dir", type=Path, default=None)
    cfg.set_defaults(func=cmd_print_config)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd is None:
        args = parser.parse_args(["run", *(argv or [])])
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
