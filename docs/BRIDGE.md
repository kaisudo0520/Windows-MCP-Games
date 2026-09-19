# sts-bot 橋接程式（第 1 階段：只看不動）

`sts_bot` 是給 CommunicationMod 啟動的外部程式。遊戲每次狀態穩定就把一行 JSON 推到它的 stdin，
它把狀態顯示在一個小視窗、寫進 `events.jsonl`，並讓操作者手動送指令。**目前沒有任何策略會自動出招**
（`NullPolicy`），Jev 與規則策略是後面的階段。

舊分支的 `game_mcp` 套件原樣保留在這條分支上；`sts_bot` 直接重用它的 `StopLatch`／`OperatorWatcher`，
所以 **Scroll Lock 與滑鼠推到左上角的緊急停止照舊有效**，而且操作者停止一樣不能用按鈕解除。

## 沒有遊戲也能看視窗：demo

```powershell
$env:PYTHONPATH="src"; python -m sts_bot demo
```

用 `tests/fixtures/commmod_combat_state.json` 餵一個戰鬥狀態，視窗會一直開著；在輸入框打 `play 1 0`、`end`、
`state`，指令會印到啟動它的主控台（就是實機時會送給模組的那一行），被擋掉的指令會紅字顯示原因。
可用 `--fixture 檔案` 換別的狀態，例如從實機錄下的 `events.jsonl` 裡抽一筆 `state` 的 `data.raw`。

## 部署到有遊戲的電腦

橋接程式只用標準庫（`tkinter` 也在標準庫內；`game_mcp.safety` 只用 ctypes），不需要 pywin32、fastmcp 或 uv。

1. 那台電腦安裝 Python 3.12 以上（勾選 tcl/tk）。
2. 把這個 repo 複製過去（`git clone` 或整包 zip；至少要 `src/game_mcp`、`src/sts_bot`、`tests/fixtures`）。
3. 在 repo 目錄：
   ```powershell
   $env:PYTHONPATH="src"; python -m sts_bot demo           # 先確認視窗能開
   $env:PYTHONPATH="src"; python -m sts_bot print-config   # 產生 command= 行
   ```
   `print-config` 在原始碼目錄下會產生 `command=<python> -u <repo>\sts_bot_launch.py --ui tk`。
   這個啟動腳本自己把 `src/` 加進 `sys.path`，所以模組啟動子程序時**不需要**安裝套件、也不需要 PYTHONPATH。
   `sts_bot_launch.py` 的其他參數會原樣傳給 `sts-bot run`（例如 `--ui none --run-dir E:\sts-runs`）。
4. 依下一節設定模組。

## 前置（不改遊戲本體）

1. 備份 `SlayTheSpire/saves/` 與 `SlayTheSpire/preferences/`。
2. Steam Workshop 訂閱 **ModTheSpire**、**BaseMod**、**CommunicationMod**；用 ModTheSpire 啟動一次遊戲確認三者載入。
3. 讓 CommunicationMod 產生設定檔（第一次載入後會出現），路徑通常是
   `%LOCALAPPDATA%\ModTheSpire\CommunicationMod\config.properties`。

## 設定 command

```powershell
uv run sts-bot print-config            # 用 .venv 的 python
# 或沒有 uv 時：
$env:PYTHONPATH="src"; python -m sts_bot print-config
```

輸出類似：

```
command=C\:\\...\\python.exe -u E\:\\project\\Windows-MCP-Games\\sts_bot_launch.py --ui tk
runAtGameStart=true
```

把這兩行貼進 `config.properties`（Java properties 格式，反斜線與冒號必須跳脫，`print-config` 已處理）。
`-u` 是必要的：stdout 是協定通道，不能有緩衝。若要指定紀錄目錄或關掉視窗：

```
sts-bot print-config --ui none --run-dir E:\sts-runs
```

## 執行時

- 遊戲啟動（或在 ModTheSpire 的 CommunicationMod 設定裡按 start process）後會跳出「sts-bot 橋接」視窗。
- 上半：目前層數／房間／HP／金幣／回合／能量；手牌（`[n]` 是 `PLAY n` 用的 1-based 序號）、
  敵人（`[n]` 是目標用的 0-based 序號）、選項（`CHOOSE n`）。
- 下半：動作日誌。綠色是送出的指令，紅色是被擋掉的指令與原因，灰色是狀態摘要與備註。
- 輸入框可以手動送指令（`play 1 0`、`end`、`choose 0`、`state`），送出前會檢查：
  動詞合法 → 沒有停止 → 遊戲 `ready_for_command` → 動詞在 `available_commands` 內。
  `state`／`wait` 是唯讀，停止中也能送。
- **停止** 按鈕等同舊的 `stop_input`；**恢復** 只能解除按鈕造成的停止，解不開 Scroll Lock／左上角造成的停止。**要求狀態** 送 `STATE` 讓模組立刻回傳目前狀態。
- 關閉視窗會先進入停止狀態再結束程序。

## 紀錄

預設在 `%LOCALAPPDATA%\sts-bot\runs\<時間戳>\`（可用 `STS_BOT_RUN_DIR` 或 `--run-dir` 改）：

- `events.jsonl`：每行 `{"t","seq","kind","data"}`，`kind` ∈ `state`（含原始 JSON）／`command`／`rejected`／`error`／`note`。
- `bridge.log`：程式日誌（同時也送到 stderr，CommunicationMod 會存進 `communication_mod_errors.log`）。

第 2 階段之後的離線回放與 Jev payload 調參都以 `events.jsonl` 為輸入。

## 編碼

CommunicationMod 用 JVM 預設字集寫 stdin。程式對每一行先試 UTF-8，失敗再試系統地區編碼（zh-TW 是 cp950），
並用同一種編碼回覆。若中文卡牌名稱出現亂碼，用 `--encoding cp950` 或 `--encoding utf-8` 強制指定。

## 測試

```powershell
python -m pytest tests/test_bot_*.py -q     # 不需要 pywin32，純記憶體串流
```

## 程式結構

| 檔案 | 責任 |
|---|---|
| `protocol.py` | `ready` 握手、逐行讀 JSON、編碼偵測、指令動詞驗證與送出 |
| `state.py` | JSON → `Snapshot`（手牌／敵人／選項／可用指令），給 UI 與未來的 Jev payload |
| `dispatch.py` | 唯一的送指令關卡：動詞、停止閂鎖、`ready_for_command`、`available_commands` |
| `policy.py` | `Policy` 介面；目前只有 `NullPolicy` |
| `bridge.py` | 讀取執行緒 + 事件佇列 + 主執行緒處理；操作者控制（送指令／要求狀態／停止／恢復） |
| `view.py` / `ui.py` | 顯示抽象與 tkinter 實作 |
| `recorder.py` | `events.jsonl` |
| `__main__.py` | `run`、`demo`、`print-config` |
| `../sts_bot_launch.py` | 給模組 `command=` 用的免安裝啟動腳本 |
