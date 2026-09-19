"""CommunicationMod 的逐行協定。

模組會把我們當子程序啟動，透過我們的 stdio 溝通：

* 啟動後必須很快在 stdout 印出 ``ready\\n``，否則模組會把我們殺掉；
* 每當遊戲狀態穩定（或回應 ``STATE``／``WAIT``／錯誤指令）時，模組往我們的 stdin
  寫一行 JSON 物件；
* 我們往 stdout 寫一行一個指令。

因此 stdout 是協定通道，本套件任何地方都不可以 print；日誌走 stderr，
模組會把 stderr 抄到 ``communication_mod_errors.log``。

編碼：模組是 Java，用 JVM 預設字集寫入。zh-TW 的 Windows 上可能是 cp950 而不是 UTF-8。
每一行先試 UTF-8，再試系統地區編碼，並用最後成功的那種編碼回覆。
"""

from __future__ import annotations

import json
import locale
import logging
from dataclasses import dataclass
from typing import Any, BinaryIO

logger = logging.getLogger(__name__)

READY = b"ready\n"
REPLACE_MARKER = "utf-8+replace"

#: 模組認得的所有指令動詞（線上傳輸不分大小寫）。
KNOWN_COMMANDS = frozenset(
    {
        "start", "potion", "play", "end", "choose", "proceed", "confirm",
        "return", "skip", "cancel", "leave", "key", "click", "wait", "state",
    }
)
#: 不會改變遊戲的動詞；輸入停止中也允許送出。
READ_ONLY_COMMANDS = frozenset({"state", "wait"})


class ProtocolError(ValueError):
    """模組送來的行（或我們要送的指令）不符合協定。"""


@dataclass(frozen=True)
class Message:
    """模組送來的一行 JSON，附上型別化的欄位存取。"""

    raw: dict[str, Any]
    encoding: str

    @property
    def error(self) -> str | None:
        err = self.raw.get("error")
        return str(err) if err else None

    @property
    def ready_for_command(self) -> bool:
        return bool(self.raw.get("ready_for_command", False))

    @property
    def in_game(self) -> bool:
        return bool(self.raw.get("in_game", False))

    @property
    def available_commands(self) -> tuple[str, ...]:
        cmds = self.raw.get("available_commands") or []
        return tuple(str(c).lower() for c in cmds)

    @property
    def game_state(self) -> dict[str, Any] | None:
        state = self.raw.get("game_state")
        return state if isinstance(state, dict) else None


def candidate_encodings(preferred: str | None = None) -> list[str]:
    """解碼順序：明確指定的 → UTF-8 → Windows 地區碼頁。"""
    ordered = [preferred, "utf-8", locale.getpreferredencoding(False)]
    result: list[str] = []
    for enc in ordered:
        if enc and enc.lower() not in {r.lower() for r in result}:
            result.append(enc)
    return result


def decode_line(data: bytes, encodings: list[str]) -> tuple[str, str]:
    """回傳 (文字, 使用的編碼)。全部失敗時退回有損的 UTF-8 而不是拋錯。"""
    for enc in encodings:
        try:
            return data.decode(enc), enc
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace"), REPLACE_MARKER


def parse_message(text: str, encoding: str = "utf-8") -> Message:
    text = text.strip()
    if not text:
        raise ProtocolError("空行")
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"不是 JSON：{exc}") from exc
    if not isinstance(obj, dict):
        raise ProtocolError("JSON 最外層不是物件")
    return Message(obj, encoding)


def normalize_command(command: str) -> str:
    """壓平空白並驗證動詞。不合法時拋 ProtocolError。"""
    line = " ".join(command.split())
    if not line:
        raise ProtocolError("空指令")
    verb = line.split(" ", 1)[0].lower()
    if verb not in KNOWN_COMMANDS:
        raise ProtocolError(f"未知的指令動詞 {verb!r}")
    return line


class CommLink:
    """對模組給我們的兩條位元組串流做阻塞式讀寫。"""

    def __init__(self, stdin: BinaryIO, stdout: BinaryIO, *, encoding: str | None = None) -> None:
        self._in = stdin
        self._out = stdout
        self._encodings = candidate_encodings(encoding)
        self.last_encoding: str = self._encodings[0]
        self.lines_read = 0
        self.commands_sent = 0

    def handshake(self) -> None:
        self._out.write(READY)
        self._out.flush()

    def read_message(self) -> Message | None:
        """下一則 JSON 訊息，跳過空行。EOF（遊戲關閉）時回傳 None。"""
        while True:
            data = self._in.readline()
            if not data:
                return None
            if not data.strip():
                continue
            text, enc = decode_line(data, self._encodings)
            if enc != self.last_encoding:
                logger.info("偵測到 stdin 編碼：%s", enc)
                self.last_encoding = enc
            self.lines_read += 1
            return parse_message(text, enc)

    def send(self, command: str) -> str:
        """寫出一行指令並 flush。回傳實際送出的正規化字串。"""
        line = normalize_command(command)
        enc = "utf-8" if self.last_encoding == REPLACE_MARKER else self.last_encoding
        self._out.write((line + "\n").encode(enc))
        self._out.flush()
        self.commands_sent += 1
        return line
