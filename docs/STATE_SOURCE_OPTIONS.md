# 遊戲狀態來源的選項評估（供決策用）

日期：2026-09-07。狀態：討論中，尚未實作任何一項。

## 1. 問題

目前 AI 取得遊戲狀態的唯一方式是 `get_game_screen` 回傳整張 1600×900 截圖，由 AI 的視覺能力判讀。每一步約 1.5–2k token 的影像，且座標由 AI 自己估。問題：能否只傳「關鍵狀態的 JSON」，或至少把截圖降到輔助角色？

限制條件（第一階段原則）：不讀記憶體、不注入遊戲、不修改存檔、不使用遊戲內部 API。其中「不注入、不用內部 API」是方案 D／E 會碰到的線。

## 2. 方案總覽

| 代號 | 做法 | 狀態權威性 | 每步 payload | 動作路徑 | 是否碰到限制 |
|---|---|---|---|---|---|
| A（現況） | 截圖 → AI 視覺 | 中（AI 判讀可能錯） | 大（整張圖） | 桌面模擬點擊 | 否 |
| B | 工具端 OCR／樣板比對截圖 → JSON | 中低（啟發式解析） | 小 | 桌面模擬點擊 | 否 |
| C | 只讀 `saves/*.autosave` | 高，但**戰鬥中不更新** | 小 | 桌面模擬點擊 | 「不修改存檔」不受影響；是否允許「讀」請確認 |
| D（混合） | CommunicationMod 只當感測器輸出 JSON；動作仍走桌面模擬點擊 | 高 | 小（可另附縮圖） | 桌面模擬點擊 | **注入遊戲、用內部 API** |
| E | CommunicationMod 全接管：狀態與動作都走模組指令 | 高 | 小 | 模組指令（PLAY／END／CHOOSE） | **注入遊戲、用內部 API** |

A 與 B 可以疊加：B 只是把解析工作從 AI 搬到工具端，畫面仍是唯一資料來源。C 適合戰鬥外（牌組、遺物、地圖、金幣），戰鬥內拿不到手牌與敵人狀態。真正能「戰鬥中只傳 JSON」的只有 D 和 E。

## 3. CommunicationMod 的事實（依 2026-09-07 讀取的 README）

- 依賴：ModTheSpire + **BaseMod**。README 沒有寫明支援的遊戲版本；本機是 v2.3.4（2022-12-18），需實裝確認。
- 啟動方式：模組讀 SpireConfig 設定檔裡的 `command=...`，**由遊戲程序啟動這個外部程式**，用它的 stdin／stdout 交換一行一則的 JSON。外部程式先在 stdout 印 `ready`，之後每當遊戲狀態穩定（沒有動畫、沒有待處理輸入）模組就把整包狀態推到它的 stdin。
- 指令集：`START`、`PLAY 手牌序號 [目標序號]`、`POTION`、`END`、`CHOOSE`、`PROCEED/CONFIRM`、`RETURN/SKIP/CANCEL/LEAVE`、`KEY 鍵名`、`CLICK Left|Right X Y`（1920×1080 標準化座標，走遊戲內部輸入）、`WAIT`、`STATE`（立即回傳目前狀態）。
- 狀態欄位：頂層 `available_commands`、`ready_for_command`、`in_game`、`screen_type`、`screen_state`、`seed`、`act`、`floor`、`ascension_level`、`class`、`gold`、`room_type`、`deck`、`relics`、`potions`、`map`（節點有 x/y，是地圖格座標不是螢幕座標）。戰鬥中另有 `combat_state`：`player`（hp、block、energy、powers、orbs）、`hand`（每張牌有 name、id、cost、type、has_target、is_playable、exhausts、upgrades、uuid）、`draw_pile`、`discard_pile`、`exhaust_pile`、`monsters`（id、current_hp、max_hp、block、intent、move_base_damage、move_adjusted_damage、powers、is_gone）、`turn`。
- **沒有的東西**：手牌與怪物的螢幕座標。手牌只有陣列順序，怪物只有陣列順序。
- 已知限制：Match and Keep 事件狀態不完整；藥水欄滿時無回饋；不支援取消已選的手牌；部分手動操作不會觸發狀態推送；README 說主要在「快速模式」開啟下測試。
- README 沒有提到成就。ModTheSpire 是否影響 Steam 成就，需裝好後在其設定畫面確認。

## 4. 方案 D（混合）的設計草案

核心想法：模組只做「看」，不做「動」。我們的橋接程式永遠不送 `PLAY`／`END`／`CHOOSE`／`CLICK`／`KEY`，只送 `ready`、`STATE`、`WAIT`。所有動作仍由現有的 `click`／`move_mouse`／`press_key` 走桌面注入，現有的綁定、焦點、遮擋、觀察 ID、停止閂鎖全部保留。

### 4.1 程序關係

```
MCP 客戶端 ──stdio──> sts-game-mcp（現有伺服器）
                          │ named pipe（本機、僅本使用者）
                          ▼
                     state-bridge.exe / .py  <──stdin/stdout──  CommunicationMod（在遊戲程序內）
```

- 遊戲啟動時由模組拉起 `state-bridge`；它把每次推送的狀態 JSON 存成「最新狀態」，並在本機 named pipe 上提供讀取。
- `sts-game-mcp` 啟動時連 pipe；連不上就退回純截圖模式，工具照常可用，只是 `state` 欄位缺席並在回傳裡標明 `state_source: "screenshot_only"`。
- 這是我們自己兩個程序之間的本機通道，不是對外 HTTP 服務，但與原本「不啟動 HTTP 服務」的精神有擦邊，明列在此。

### 4.2 `get_game_screen` 的回傳變化

- 新增 `state` 欄位：模組的最新狀態 JSON（原樣或精簡），加 `state_age_ms` 與 `state_seq`。
- 新增 `state_consistency`：把 JSON 與畫面做幾個便宜的交叉檢查（例如手牌張數、能量、我方 HP 的 OCR 對照）。不一致就標 `mismatch`，AI 應以畫面為準並重新擷取。
- 影像預設改為縮圖（`GAME_MCP_IMAGE_SCALE=0.5`，約 400×225 仍看得出布局），AI 需要時再要求全尺寸。這一項可獨立於模組先做。

### 4.3 座標映射（模組沒有螢幕座標，這是混合方案的主要工程）

- 手牌：StS 的手牌排列是固定扇形，只取決於張數（1–10）。先用這次 `captures/` 的截圖量測各張數下每張牌的中心點，做成查表；再用畫面上牌框的亮青色邊緣做偵測校正。回傳給 AI 的每張手牌就會帶 `screen_point`。
- 怪物：`monsters` 陣列順序與畫面左到右順序一致（含 `is_gone` 的空位）。位置用畫面上血條（紅色長條加 `xx/yy` 文字）偵測取得，對應到陣列索引。
- 按鈕（結束回合、略過獎勵、繼續）：位置固定，查表即可。
- 這些映射都附 `confidence`；低於門檻就不給座標，逼 AI 回到看圖。

### 4.4 動作驗證（混合方案的額外好處）

每個動作之後，等待模組的下一次狀態推送，比對預期變化：打出第 2 張牌後手牌少一張、能量減少對應費用、目標怪物 HP 下降。這比現在只看「焦點還在、游標在目標」強得多，可以直接回報 `effect_confirmed: true/false`。

### 4.5 安全機制對應

| 現有機制 | 在 D 下 |
|---|---|
| 綁定 HWND/PID | 保留，另外要求模組回報的 `seed` 與存檔一致 |
| 前景／遮擋／觀察 ID | 保留不變 |
| 按鍵白名單 | 保留不變 |
| 停止閂鎖 | 保留；橋接程式本身不會送任何動作指令，所以模組側不需要停止機制 |
| 遙測／網路 | 模組與橋接程式都不連網；named pipe 只在本機 |

## 5. 方案 E 與 D 的差別

E 把動作也交給模組：`PLAY 2 1` 直接打第 2 張牌到第 1 隻怪，不需要座標、不需要焦點、不需要遮擋檢查，AI 完全碰不到桌面，是最收斂的動作空間。代價是現有這套桌面安全機制幾乎整套作廢，要改成在指令層做白名單與停止；而且模組的 `CLICK`／`KEY` 指令也在裡面，仍要擋。若最終目標是「讓 AI 穩定打通遊戲」，E 的工程量與可靠度都優於 D；若目標是「驗證受限桌面代理」，D 保留了這個題目。

## 6. 風險與未確認事項

1. 版本相容：CommunicationMod 與 v2.3.4 是否可用，只有實裝才知道。
2. 成就：未確認。
3. 存檔：CommunicationMod 與 BaseMod 不新增內容，存檔應可在有／無模組間切換；仍應先備份 `saves/` 與 `preferences/`。目前有一局停在獎勵畫面的存檔在裡面。
4. 執行面：模組會依設定檔啟動任意命令。設定檔內容與檔案權限要由我們控制；模組只從 Workshop 或作者 GitHub release 取得。
5. 「快速模式」：README 表示主要在快速模式下測試。開快速模式會讓動畫變短，對截圖式操作也是好事，但要重新量一次動作後的等待時間。
6. 推送時機：模組只在「狀態穩定」時推送，動畫中不會有新狀態；橋接程式需要處理「舊狀態」與「等待中」的區分，回傳給 AI 的 `state_age_ms` 就是為此。

## 7. 建議

- 若限制不放寬：做 B 的戰鬥 HUD 子集（HP、格擋、能量、手牌張數與位置框、敵人血條與意圖數字）加 C（戰鬥外讀存檔），影像改縮圖，AI 需要時再要全圖。能省一半以上 token，但狀態仍是啟發式。
- 若限制可放寬：走 D。模組只當感測器，動作與安全機制全部沿用現況，改動集中在「多一個狀態來源」與「索引到座標的映射」，風險最小，且能立刻得到動作效果驗證。之後若要追求通關率，再評估升級到 E。

## 8. 若採 D 的實作順序

前置（不改遊戲）：
1. 備份 `saves/`、`preferences/`。
2. 從 Workshop 裝 ModTheSpire、BaseMod、CommunicationMod，確認能載入 v2.3.4，確認成就設定。
3. 寫最小橋接程式：只印 `ready`、把推送的狀態落地為 JSON、支援 `STATE`，不送任何動作。跑一局手動遊玩，收集狀態樣本。

實作：
4. `sts-game-mcp` 連 pipe，`get_game_screen` 加 `state`、`state_age_ms`、`state_consistency`。
5. 手牌與怪物的索引到座標映射，含信心值。
6. 動作後的效果驗證（`effect_confirmed`）。
7. 用同一場 Ascension 0 第一戰重跑流程 D，比較每步 token 與成功率。
