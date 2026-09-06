# sts-game-mcp

一個精簡的本機 stdio MCP 伺服器，只會擷取與操作**一個已綁定的 Slay the Spire 視窗**。
以 [CursorTouch/Windows-MCP](https://github.com/CursorTouch/Windows-MCP)（commit `08ddee78`）的擷取與輸入元件為基礎重製，
其餘通用桌面能力（PowerShell、檔案系統、登錄、剪貼簿、程序管理、瀏覽器、HTTP 傳輸、遙測）一概未納入。
來源與授權見 `THIRD_PARTY_NOTICES.md`。

目標環境：Windows 10 22H2、遊戲視窗模式 1600×900、DPI 100%（其他 DPI 亦處理，見下）。

## 安裝

需要 [uv](https://docs.astral.sh/uv/) 與 Python 3.12（uv 會自動使用或下載）。

```powershell
cd N:\projects\github\Windows-MCP-Game
uv sync --extra dev          # 建立 .venv 並依 uv.lock 安裝鎖定版本
uv run pytest -m "not integration"   # 單元測試（不碰桌面）
uv run pytest -m integration          # 整合測試：會開一個小型 tkinter 測試視窗並在裡面點擊
```

安裝依賴時需要網路；執行期不會主動連線任何第三方服務。畫面仍會經 MCP 客戶端送給 AI，因此**不是**完全離線。

## 啟動與停止

```powershell
uv run sts-game-mcp                 # stdio，由 MCP 客戶端以子程序方式啟動
uv run sts-game-mcp --log-level DEBUG
```

- 停止伺服器：MCP 客戶端關閉連線即結束；或直接結束該子程序。正常結束（stdio 關閉）時會釋放本工具按下但尚未放開的滑鼠鍵／按鍵；若被 `TerminateProcess` 強制終止則不會執行，理論上有一個約 50 ms 的視窗可能留下按住的鍵（下次實體按一下該鍵即解除）。
- 停止輸入（不關伺服器）：
  - 工具層：`stop_input` → 之後必須呼叫 `resume_input` 才會恢復。
  - 操作者緊急停止：**按一下 Scroll Lock 鍵**（預設；Pause 鍵在 PC 鍵盤上按下即同時送出按下／放開，沒有持續按住狀態，所以不當預設），或把滑鼠**推到主螢幕左上角 (0,0)**。操作者停止**永遠覆蓋工具停止**（即使 AI 先呼叫了 `stop_input`），而且**無法用工具呼叫解除**，只能重啟伺服器（可用 `GAME_MCP_ALLOW_TOOL_RESUME_AFTER_OPERATOR_STOP=1` 放寬）。
  - 偵測方式是每 30 ms 輪詢 `GetAsyncKeyState`（含「自上次呼叫後曾按下」位元），不是鍵盤鉤子。
- 停止時仍可 `get_game_screen`（只讀）。

MCP 客戶端設定範例見 `docs/mcp-config.example.json`。不會自行修改任何全域設定，也不會登錄開機自啟動。

## 工作流程

1. `list_game_windows` → `bind_game`（候選不唯一時必須傳 `hwnd`）。
2. `get_game_screen` → 取得影像 + `observation_id`。
3. 一個動作（`click` / `move_mouse` / `press_key`），必須帶剛取得的 `observation_id`。
4. 回到 2。每個 `observation_id` 只能用一次；動作後、逾時、視窗移動／縮放、重新綁定都會使其失效。

## 工具

| 工具 | 參數 | 回傳 |
|---|---|---|
| `list_game_windows` | 無 | `{ok, count, candidates:[{hwnd,pid,exe_name,exe_path,title,class,window_rect,minimized,visible,is_foreground}], filters}` |
| `bind_game` | `hwnd?: int` | `{ok, binding:{hwnd,pid,exe_name,exe_path,process_create_time,title_at_bind}, window}` |
| `get_game_screen` | 無 | 內容區塊 `[ImageContent(png/jpeg), TextContent(JSON)]`；JSON 同時放在 `structuredContent`：`{ok, observation_id, image_size, client_size, scale, dpi, captured_at_unix, valid_for_s, backend, window:{…, overlays:[…]}, timing_ms:{validate,occlusion_check,capture,post_validate,encode,total}, image_format, image_bytes, warnings:[…]}` |
| `click` | `x:int, y:int, observation_id:str, button:"left"\|"right"="left"` | `{ok, action, image_point, client_point, screen_point, observation_id_used, observation_invalidated, post_action:{window_alive,focus_kept,geometry_unchanged,stopped_during_action,cursor_screen,cursor_at_target,target_still_ours,trustworthy}, timing_ms:{validate,precheck_to_input_gap,injection_window,input,post_verify,total}, next}` |
| `move_mouse` | `x:int, y:int, observation_id:str` | 同上（`action:"move_mouse"`） |
| `press_key` | `key:str, observation_id:str` | `{ok, action, key, vk, ime:{open,conversion_mode,native_open,keyboard_layout,shift_tapped}, post_action, timing_ms, next}` |
| `stop_input` | `reason?: str` | `{ok, stop:{stopped,origin,reason,since_unix}, held_after_release}` |
| `resume_input` | 無 | `{ok, stop}` 或錯誤 `STOPPED`（操作者停止） |
| `get_status` | 無 | `{ok, version, dpi_awareness, binding, window, window_error, stop, operator_stop, held_inputs, current_observation, key_whitelist, observation_ttl_s, screenshot_backend, debug_dir, uptime_s}` |

座標：`x, y` 是**回傳影像的像素座標**（左上角為 0,0）。伺服器負責換算成客戶區與螢幕實體像素（程序為 Per-Monitor V2 DPI aware，`scale` 欄位記錄影像縮放比）。

失敗時回 `{ok:false, error:{code, message, details}}`。`code` 一覽：

| code | 含義 |
|---|---|
| `NOT_BOUND` / `NO_CANDIDATES` / `AMBIGUOUS_CANDIDATES` / `CANDIDATE_NOT_FOUND` | 綁定問題 |
| `WINDOW_GONE` / `PROCESS_GONE` | 視窗控制代碼失效、PID 被重用或程序已結束 |
| `MINIMIZED` / `CLOAKED` / `NOT_FOREGROUND` / `OFFSCREEN` | 視窗狀態不允許擷取或輸入 |
| `OCCLUDED` | 客戶區（或點擊目標點）被其他視窗覆蓋；`details.method` 為 `z_order` 或 `hit_test`，`details.occluder` 指出是誰，`details.phase` 為 `post_capture` 表示抓圖期間才出現 |
| `OBSERVATION_STALE` | `details.reason` ∈ `missing / none_issued / superseded / expired / window_moved_or_resized / input_performed / stopped / rebound` |
| `INVALID_COORDINATES` / `INVALID_ARGUMENT` / `KEY_NOT_ALLOWED` | 座標越界、參數無效（如 `button`）、按鍵不在白名單 |
| `IME_ACTIVE` | 遊戲視窗的輸入法在中文模式，按鍵會被輸入法吃掉（自動點 Shift 後仍未切換） |
| `STOPPED` / `BUSY` / `CAPTURE_FAILED` / `INPUT_FAILED` | 停止中、另一動作進行中、擷取或注入失敗 |

## 設定（環境變數）

| 變數 | 預設 | 說明 |
|---|---|---|
| `GAME_MCP_PROCESS_NAMES` | `slaythespire.exe,javaw.exe,java.exe` | 候選視窗的程序名（Steam 版實際為 `javaw.exe`） |
| `GAME_MCP_TITLE_SUBSTRINGS` | `slay the spire` | 標題子字串（不分大小寫）；與 `GAME_MCP_PATH_SUBSTRINGS`（`slaythespire`）擇一符合即可 |
| `GAME_MCP_KEY_WHITELIST` | `1..9,0,e,enter,space` | 可按的鍵；不支援組合鍵。`escape` 預設不開放 |
| `GAME_MCP_OBSERVATION_TTL_S` | `20` | 觀察有效秒數 |
| `GAME_MCP_OCCLUSION_GRID_PX` | `40` | 遮擋取樣格距（像素） |
| `GAME_MCP_IMAGE_FORMAT` | `png` | `png`（無損，約 1.2 MB）或 `jpeg`（4:4:4，`GAME_MCP_JPEG_QUALITY` 預設 90，約 230 KB）。Claude Desktop 等有 1 MB 圖片上限的客戶端請用 `jpeg` |
| `GAME_MCP_IMAGE_SCALE` | `1.0` | 額外縮放；卡牌文字建議 ≥ 0.75 |
| `GAME_MCP_SCREENSHOT_BACKEND` | `auto` | `auto` / `mss` / `pillow` |
| `GAME_MCP_EMERGENCY_KEY` | `scrolllock` | 操作者緊急停止鍵：`scrolllock` / `pause` / `f12`；不認得的名稱會讓伺服器啟動失敗（exit 2） |
| `GAME_MCP_CORNER_FAILSAFE` | `1` | 滑鼠推到主螢幕左上角即停止 |
| `GAME_MCP_ALLOW_TOOL_RESUME_AFTER_OPERATOR_STOP` | `0` | 是否允許 `resume_input` 解除操作者停止 |
| `GAME_MCP_DEBUG_DIR` | 未設 | 設定後每次擷取另存一份影像到該目錄（建議用專案內的 `captures/`，已在 .gitignore）；未設定時不落地 |
| `GAME_MCP_DEBUG_KEEP_FILES` / `GAME_MCP_DEBUG_MAX_AGE_H` | `300` / `24` | 偵錯影像清理：每次寫入後只保留最新 N 張；伺服器啟動時刪除超過 N 小時的檔案（固定清理時點） |
| `GAME_MCP_CLICK_SETTLE_MS` | `60` | 游標放到目標後、按下前的停頓，讓遊戲先在一個影格內看到懸停（LibGDX 的 hitbox 需要） |
| `GAME_MCP_IME_AUTOTOGGLE` | `1` | `press_key` 前若偵測到遊戲視窗的輸入法在中文模式，工具自己點一下 Shift 切成英數再送鍵；仍是中文則回 `IME_ACTIVE`（`GAME_MCP_ALLOW_IME_NATIVE=1` 可強制送出） |

## 安全邊界

- **綁定**：以 HWND + PID + 程序名 + 程序建立時間比對；任一不符即 `WINDOW_GONE` / `PROCESS_GONE`。不啟動任何程式、不切換視窗、不搶焦點。
- **擷取**：MSS／Pillow 是「螢幕矩形」擷取，不是視窗隔離擷取。因此擷取前檢查：未最小化、未被 DWM cloak、是前景視窗、客戶區四角都在某個螢幕內、z-order 往上走沒有不透明視窗與客戶區相交、格狀取樣 `WindowFromPoint` 找不到其他視窗；擷取後把幾何、前景、遮擋**全部再驗一次**。任一不成立即拒絕，不回傳影像。對點擊測試透明的覆蓋層（`WS_EX_LAYERED|WS_EX_TRANSPARENT`，例如 Discord Overlay）不擋，但列在 `window.overlays` 與 `warnings` 讓 AI 知道影像可能混入它們的內容。
- **輸入**：每個動作重新做上述全部檢查，再驗 `observation_id`（唯一、未逾時、同一 HWND、幾何未變），再對目標點做點級命中檢查；先移動游標並確認真的落在目標（ClipCursor 或 SetCursorPos 失敗就中止），注入前最後一次檢查停止閂鎖、前景與目標點歸屬。鍵盤只允許白名單單鍵。動作後回報焦點／幾何是否保持、游標是否在目標、目標點是否仍屬遊戲、動作期間是否觸發停止，並彙整成 `post_action.trustworthy`。
- **併發**：擷取、綁定、輸入共用一把鎖，同時只做一件事（其餘回 `BUSY`），不會為動作進行中抓到的像素發出觀察 ID。
- **停止**：見「啟動與停止」。停止或例外時釋放本工具按下的鍵／鈕。
- 沒有拖曳工具：Slay the Spire 以「點卡→點目標」即可出牌；確有需要再加受限拖曳。

## 已實測 / 未實測

已實測（本機，Windows 10 19045、雙螢幕 3840×1080、DPI 100%、遊戲 javaw.exe 視窗模式 1600×900）：

- 真實遊戲只讀擷取：影像 1600×900、僅客戶區、mss 後端；穩態工具端耗時約 **80–150 ms**（validate 4–10 ms、capture 20–80 ms、擷取後再驗證 4 ms、PNG 編碼 55–75 ms；JPEG q90 編碼 8 ms、總計約 50 ms）。首次呼叫約 570 ms（暖機）。
- 真實遊戲邊界：置頂視窗覆蓋 → `OCCLUDED`（`method: z_order`，指出遮擋 hwnd）；其他視窗取得焦點 → `NOT_FOREGROUND`；移除後恢復。本機的 Discord Overlay 被正確辨識為透明覆蓋層並回報在 `warnings`。
- tkinter 測試視窗（25 項整合測試）：綁定唯一／多候選／不存在、視窗關閉、擷取尺寸與像素內容、失焦、遮擋（命中測試與 z-order 停用視窗）、透明覆蓋層只警告、抓圖期間出現遮擋 → 拒絕、最小化、部分超出螢幕、鎖被占用回 `BUSY`、左鍵／右鍵點擊送達且座標正確、`trustworthy` 判定、懸停、白名單按鍵送達、越界不送出、視窗移動後舊觀察失效、失焦拒絕、目標點被覆蓋拒絕、停止閂鎖與操作者停止不可由工具解除、實體游標推到左上角觸發停止。
- stdio 子程序端到端：只暴露 9 個工具。
- 單元：觀察生命週期（含換綁 HWND）、停止閂鎖與操作者升級、釋放失敗保留紀錄、緊急鍵名驗證、`pywintypes.error` 轉 `WINDOW_GONE`、按鍵映射與白名單、座標換算。

已實測（流程 D，真實遊戲輸入）：主選單懸停與點擊、模式／角色選擇、啟程、地圖節點、教學翻頁、戰鬥中「點卡→點敵人」出牌、非目標牌「點卡→點場地」、E 鍵結束回合；每步 `post_action.trustworthy=true`，工具端點擊約 90 ms、按鍵約 55 ms。發現並修正兩件事：LibGDX 需要先看到懸停再點擊（加 `GAME_MCP_CLICK_SETTLE_MS`）；zh-TW 輸入法在中文模式會吃掉按鍵（加 IME 偵測與 Shift 自動切換）。

**未實測**：非 100% DPI 環境；Scroll Lock 在遊戲前景時的實際觸發（單元測試以替身驗證，左上角模式已實測）；動作進行中觸發停止的路徑（單元以替身驗證）。

## 已知限制

- 遮擋偵測靠 z-order 交集 + `WindowFromPoint` 取樣，都不是像素級。透明覆蓋層（Discord／Steam overlay、標註工具）只會被回報不會被擋；同一程序自己的彈出視窗不會被視為遮擋；系統游標本身不會被擷取。
- 「最後一次檢查 → 注入完成」之間有競態窗口：回傳 `precheck_to_input_gap`（檢查到第一個事件，通常 <0.1 ms）與 `injection_window`（到最後一個事件，點擊約 50–70 ms）。無法由使用者模式消除；若 `post_action.trustworthy=false`，該輸入可能已送到別的視窗，必須重新擷取確認。
- 操作者緊急停止是輪詢（30 ms），不是鍵盤鉤子；其他程式若也在查同一個鍵的 `GetAsyncKeyState`，「曾按下」位元可能被它們清掉。
- zh-TW 輸入法作用中時，一般視窗收到的是 `VK_PROCESSKEY`（整合測試已容許）；LWJGL 遊戲視窗依實測時再確認。
- 觀察 ID 只保留最新一個；沒有歷史回放。
- 未支援拖曳、滾輪、多鍵、文字輸入。
