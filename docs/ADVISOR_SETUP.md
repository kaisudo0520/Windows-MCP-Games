# 顧問模式安裝與試玩步驟

日期：2026-09-07。對象：本機（Steam 版 Slay the Spire v2.3.4，`E:\STEAM\steamapps\common\SlayTheSpire`）。

流程：遊戲內的 CommunicationMod 把狀態推給 `sts-state-dump` → 寫成 `latest.json` → AI 透過
`sts-state-mcp` 讀取並用文字告訴你該怎麼打 → 你手動操作。整條路線**不會**對遊戲送任何指令。

## 0. 已經做好的事

- 存檔與偏好設定已備份到 `E:\STEAM\steamapps\common\SlayTheSpire_backup_2026-09-07`
  （`saves/`、`preferences/`、`betaPreferences/`）。
- 兩支程式已安裝在專案 venv：`.venv\Scripts\sts-state-dump.exe`、`.venv\Scripts\sts-state-mcp.exe`。
- Codex CLI 的 `~/.codex/config.toml` 與 Claude Desktop 的 `%APPDATA%\Claude\claude_desktop_config.json`
  都已加入 `sts-state` MCP 伺服器（原檔備份為 `*.bak-2026-09-07`）。

## 1. 安裝模組（你來做）

1. Steam Workshop 訂閱三個項目：**ModTheSpire**、**BaseMod**、**CommunicationMod**。
2. 在 Steam 啟動 Slay the Spire，選「Play With Mods」（ModTheSpire 啟動器）。
3. 啟動器裡勾選 BaseMod 與 CommunicationMod，先按 Play 一次確認遊戲能正常進主選單（v2.3.4 相容性只有實裝才知道）。
4. 關掉遊戲。此時 ModTheSpire 應已建立設定目錄 `%LOCALAPPDATA%\ModTheSpire\`。

## 2. 設定 CommunicationMod 啟動我們的 dump

建立（或編輯）`%LOCALAPPDATA%\ModTheSpire\CommunicationMod\config.properties`，內容：

```properties
command=N\:\\projects\\github\\Windows-MCP-Game\\.venv\\Scripts\\sts-state-dump.exe --record-samples N\:\\projects\\github\\Windows-MCP-Game\\captures\\state_samples
runAtGameStart=true
```

注意 `.properties` 檔的跳脫：冒號寫 `\:`，反斜線寫 `\\`。完整說明見 `docs/COMMUNICATIONMOD_PROTOCOL.md`。
若目錄名稱與此不同（模組版本差異），以啟動器實際建立的為準。

## 3. 收樣本（第一次試玩）

1. 用 ModTheSpire 啟動遊戲，進主選單後開始一局，手動打一場戰鬥。
2. 看兩個地方是否有東西：
   - `%LOCALAPPDATA%\sts-game-mcp\state\latest.json` 與 `latest.meta.json`（`seq` 應隨每次畫面穩定而增加）。
   - `captures\state_samples\` 每次推送一個檔案（自動清理：只留最新 500 個、72 小時以上的在下次啟動時刪除；
     要改就在 `command=` 加 `--samples-keep N` / `--samples-max-age-h H`）。
3. `%LOCALAPPDATA%\sts-game-mcp\state\bridge.log` 是 dump 的日誌。第一次推送會出現一行
   `stdin line is not valid UTF-8 ... decoded with cp950` 的 WARNING：這是正常的，本機遊戲的 JRE 用 MS950
   送字串，dump 會自動用 cp950 解，卡名已驗證正確。只有在 AI 回報卡名是問號或方塊時才需要在 `command=`
   加 `--stdin-encoding cp950`。
4. 沒有任何檔案時：檢查 `%LOCALAPPDATA%\ModTheSpire\CommunicationMod\` 下模組自己的錯誤日誌
   （它把子程序的 stderr 導到那裡），常見原因是 `command=` 路徑跳脫錯誤。

## 4. 讓 AI 讀狀態

**Claude Desktop**（改完設定要重開 Desktop）：開一個 Project，專案指示建議：

```
你是 Slay the Spire 顧問。我輸入「1」時，呼叫 get_game_state 讀取最新狀態，然後只回：
1. 一行局勢摘要（我方 HP/格擋/能量、每隻敵人的 HP 與意圖）
2. 建議動作，依順序條列，手牌用「第 N 張（牌名）」（左起 1 起算）、敵人用「左起第 N 隻」
3. 一句理由
不要重述整包 JSON。若回傳 available 為 false，告訴我模組沒有推送，請我確認遊戲畫面。
```

**省 token 的要點**（Codex 每回合會吃掉不少額度時看這裡）：
- `get_game_state` 預設就是精簡視圖（實測約原始的 5–10%），不要叫 AI 用 `view="full"`，除非它抱怨缺欄位。
- 指示裡明講「每個決策只呼叫一次 `get_game_state`，不要輪詢、不要重複呼叫確認」。
- 指示裡明講「不要複述狀態，只回摘要一行 + 建議 + 一句理由」。
- Codex 的 `model_reasoning_effort` 用 `low` 就夠打牌了；`~/.codex/config.toml` 目前就是 low。

**Codex CLI**：同樣的指示放在提示詞或 `AGENTS.md`。Codex 也可以改用 `wait_for_state(since_seq)`
自己等你操作完（逾時就用同一個 `since_seq` 再呼叫），不需要你每次打字。

## 5. 已知限制

- **觀者的姿態模組不會送**（player 只有 HP／格擋／能量／狀態／充能球；已對照模組原始碼）。精簡視圖在觀者戰鬥中
  會固定放 `stance: unknown` 提醒 AI 問你。玩觀者時打「1」順便講姿態最省事，例如「1 憤怒」。真言是狀態，
  會出現在 `powers`。缺陷的充能球、任何角色的進階等級都有送，A10 以上照玩。

- 每場戰鬥剛開始、你還沒動作時，模組推的第一包敵人意圖是 `DEBUG`。`get_game_state` 會自動請模組重送一次
  再回給 AI（回傳裡 `refresh.resolved` 為 true 表示拿到新包）。若 AI 仍回報 DEBUG，看 `bridge.log` 有沒有
  `sent STATE re-send request` 這行：沒有代表遊戲跑的是舊版 dump，重開遊戲即可。

- 模組 README：Match and Keep 事件狀態不完整；部分手動操作不會觸發推送。遇到 AI 讀到舊狀態時，
  先看 `age_ms` 再決定是否要重新操作一下畫面讓模組推送。
- 模組只在畫面「穩定」時推送，動畫中不會有新狀態。
- 索引：JSON 陣列是 0 起算，但給你的指示統一用 1 起算（左起第 N）。
