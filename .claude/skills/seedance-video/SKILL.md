---
name: seedance-video
description: 用 OpenRouter 的 Seedance 模型生成影片，支援純文字生成、角色參考圖生成、首尾影格控制與批次分鏡。當使用者提到生成影片、做動畫、AI 影片、短片、text-to-video、image-to-video、文生視頻、seedance，或想把腳本／分鏡變成影片、用人物照片做動畫時，都要使用這個 skill。每次生成都會實際扣款，這個 skill 內建先試算費用再取得同意的流程，所以即使使用者只是問「這樣要多少錢」也應該用它。
---

# Seedance 影片生成

包裝本專案的 `seedance` CLI，透過 OpenRouter 呼叫 `bytedance/seedance-2.0-mini` 生成影片。

## 最重要的一件事：這會花使用者的錢

送單成功的那一刻就開始計費，**任務無法取消，也無法退費**。一支 15 秒有音訊的影片實測扣款約 US$0.21。

所以流程永遠是「先免費試算 → 告訴使用者要花多少 → 得到同意 → 才真的送單」。`--dry-run` 完全不接觸 API，是免費的，用它來驗證參數與估價不會有任何代價。

除非使用者已經明確說了「直接做」「不用問我」之類的話，否則不要跳過確認這一步。使用者說「幫我生成一支影片」是在描述目標，不等於授權你在沒看到價格前就扣款。

## 標準流程

### 1. 確認環境

```bash
python -m seedance models --model bytedance/seedance-2.0-mini --json
```

這個指令不需要金鑰、不花錢，回傳模型當下真正支援的尺寸、秒數與能力。**參數要以它的回傳為準，不要憑記憶填**，供應商會調整支援清單。

若這步失敗，多半是不在專案目錄下執行。專案根目錄需含 `seedance/` 套件，預設位置：

```
C:\Users\purem\OneDrive\Desktop\趣味動畫製作
```

金鑰讀取順序是 `.env` 優先、系統環境變數次之。若回報缺少金鑰，請使用者自行在專案根目錄的 `.env` 填入 `OPENROUTER_API_KEY=...` —— 不要代為輸入或代為取得金鑰。

### 2. 免費試算

```bash
python -m seedance gen "你的提示詞" --duration 5 --size 480x854 --json --dry-run
```

回傳 `estimate.list_price_usd` 與 `exceeds_cost_limit`。這步會驗證所有參數，錯的秒數或尺寸會在這裡就被擋下並列出合法值。

### 3. 告訴使用者價格，等同意

報價時要說清楚估值是**牌價上限**。實測顯示實際扣款約為估值的 41.5%（OpenRouter 目前有促銷折扣）：

| 設定 | 估價（牌價） | 實際扣款 |
|---|---|---|
| 480x854 / 5 秒 / 無音訊 / 4 張參考圖 | US$0.168 | US$0.070 |
| 480x854 / 15 秒 / 有音訊 / 4 張參考圖 | US$0.504 | US$0.209 |

合理的說法是：「估價上限 US$0.17，依目前折扣實際大約 US$0.07，要生成嗎？」

### 4. 生成

```bash
python -m seedance gen "你的提示詞" --duration 5 --size 480x854 --json
```

生成要 30 秒到數分鐘，程式會自己輪詢到完成並下載。回傳含 `video_path`、`job_id`、`actual_cost_usd`。

若估價超過成本護欄（預設單次 US$0.50），指令會失敗並回傳 `CostGuardError`。這是刻意的擋門 —— 這時要回頭問使用者，得到同意後才加 `--yes` 重跑。不要看到這個錯誤就自動加 `--yes` 重試，那等於繞過使用者的授權。

## `--json` 的輸出約定

加了 `--json` 之後，**stdout 只有一個 JSON 物件**，進度訊息全部走 stderr。可以直接對 stdout 做 `json.loads`，不必剖析中文輸出。

成功時 `ok: true`；失敗時 `ok: false` 且 `error.type` 是錯誤類別名稱，離開碼為 1。依 `error.type` 決定怎麼處理：

| `error.type` | 意義 | 該怎麼做 |
|---|---|---|
| `ValidationError` | 參數不合法 | 訊息裡有合法值清單，改正後重試（未計費） |
| `ConfigError` | 找不到金鑰 | 請使用者填 `.env`（未計費） |
| `CostGuardError` | 超過成本門檻 | 回去問使用者，同意後才加 `--yes`（未計費） |
| `ApiError` | OpenRouter 回非 2xx | 看 message；401 是金鑰無效，402 是餘額不足 |
| `JobFailedError` | 供應商回報失敗 | **已計費**，據實告知使用者 |
| `JobTimeoutError` | 等待逾時 | **已計費**，用回傳的 `error.job_id` 執行 resume 取件 |

## 中斷了怎麼辦

只要任務送出去就已經計費，程式當掉、網路斷線都不會讓那筆錢回來。所以送單成功的當下，任務就被寫進 `jobs/<job_id>.json`。

```bash
python -m seedance resume <job_id> --json
```

看到 `JobTimeoutError` 或使用者說「剛剛那支不見了」時，先去 `jobs/` 找最近的記錄再 resume，而不是重新生成一支 —— 重生成等於再付一次錢。

## 參數

以 `models --json` 的回傳為準，以下是目前的值：

- **`--size`**：`480x480` `480x640` `640x480` `480x854` `854x480` `720x720` `1120x480` `720x960` `960x720` `720x1280` `1280x720` `720x1680` `1680x720`（寬x高）。預設 `480x854`，即最低解析度的手機直式。也可改用 `--resolution` + `--aspect-ratio`。
- **`--duration`**：4 到 15 的整數秒。
- **`--audio`**：預設關閉。做多鏡頭動畫時通常整支片最後統一配音比較省，不必每個分鏡都生。
- **`--seed`**：整數，同參數同 seed 可重現（供應商不保證絕對一致）。
- **`--ref`**：參考素材，可重複多次。
- **`--first-frame` / `--last-frame`**：指定首尾影格圖片。

費用只跟「寬 × 高 × 秒數」有關（`tokens = 寬 × 高 × 秒數 × 24 / 1024`），跟提示詞長短、參考圖數量無關。想省錢就降尺寸或縮秒數。

## 參考素材

```bash
python -m seedance gen "一家人在客廳" --ref 爸爸.png --ref 媽媽.png --json --dry-run
```

`--ref` 用於角色一致性或畫風參考，不會強制成為畫面的第一格。要精確控制開場畫面才用 `--first-frame`。

本機檔案會自動轉成 base64 內嵌，超過 1.5 MB 的圖片會先縮到長邊 1536 px（需要 Pillow）。也可以直接給公開 HTTPS 網址。影片與音訊參考只有 Seedance 2 代以上會實際採用。

## 批次分鏡

多個鏡頭時用批次，比逐支呼叫好：只需一次總價確認，而且可以並行。

分鏡檔格式（`defaults` 是共同參數，各 scene 可覆寫）：

```json
{
  "defaults": { "size": "480x854", "duration": 5, "generate_audio": false },
  "scenes": [
    { "name": "01-開場", "prompt": "清晨的老街，鏡頭緩慢上升" },
    { "name": "02-登場", "prompt": "橘貓從巷口走出來", "duration": 6 }
  ]
}
```

```bash
python -m seedance batch scenes.json --concurrency 3 --json --dry-run
```

確認總價後拿掉 `--dry-run`。加 `--concat outputs/final.mp4` 會在全部完成後用 ffmpeg 串成一支；沒裝 ffmpeg 就只跳過合併，個別影片照常產出。

批次的護欄是看**總價**，所以多鏡頭很容易超過 US$0.50 —— 這時更要把總價講清楚再問。

## 產出

- `outputs/日期-時間_標籤_寬x高_秒數.mp4` — 影片
- 同名 `.json` — 參數、seed、job id、實際費用，可用來重現或對帳
- `jobs/<job_id>.json` — 任務狀態，resume 會讀它

回報給使用者時給出完整路徑與實際費用（`actual_cost_usd`），不要只說「完成了」。

## 寫提示詞

Seedance 吃畫面描述，不是抽象指令。有效的組成是：**主體 + 動作 + 場景 + 鏡頭運動 + 光線氛圍**。

- 較弱：`一隻貓`
- 較好：`橘貓從巷口走出來，停下回頭看鏡頭，清晨逆光，淺景深，鏡頭緩慢推近`

要角色在多個鏡頭間長得一樣，靠的是 `--ref` 帶同一組參考圖，而不是在提示詞裡反覆描述長相。

更完整的旗標與 JSON 欄位說明見 `references/cli-reference.md`；只有在需要查完整參數表或 JSON 結構時才需要讀它。
