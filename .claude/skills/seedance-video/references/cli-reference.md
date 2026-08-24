# CLI 完整參考

只有在需要查完整旗標或 JSON 結構時才需要讀這份。日常流程 SKILL.md 就夠了。

## 目錄

- [子指令](#子指令)
- [gen 的旗標](#gen-的旗標)
- [JSON 輸出結構](#json-輸出結構)
- [批次分鏡檔](#批次分鏡檔)
- [設定與路徑](#設定與路徑)
- [底層 API 行為](#底層-api-行為)

## 子指令

| 指令 | 用途 | 花錢？ |
|---|---|---|
| `gen` | 生成單支影片 | 是（除非加 `--dry-run`） |
| `project init` | 產生專案檔骨架 | 否 |
| `project check` | 驗證所有分鏡並估價 | 否 |
| `project run` | 轉出專案，跳過已完成的分鏡 | 是（僅未完成的） |
| `project status` | 查看各分鏡狀態與累計花費 | 否 |
| `batch` | 舊的一次性批次，沒有狀態 | 是（除非加 `--dry-run`） |
| `resume` | 用 job id 取回已送出的任務 | 否（該筆已計費） |
| `models` | 列出模型與能力 | 否 |
| `gui` | 開啟 Tkinter 圖形介面 | 否（介面內操作才會） |

`gen` `batch` `resume` `models` 與 `project check/run/status` 都支援 `--json`。

## project run 的旗標

| 旗標 | 說明 |
|---|---|
| `--concurrency` | 並行數，預設 3。標了 `continue_from` 的鏡頭必須等前一鏡，不受此值影響 |
| `--only s03,s07` | 只跑指定分鏡。若指定的鏡頭要接續某鏡而該鏡既不在範圍內也未完成，會直接報錯 |
| `--force` | 連已完成的也重新生成。**這是在付第二次錢**，需另外加 `--yes` |
| `--out` | 影片輸出資料夾 |
| `--yes` / `-y` | 略過成本護欄。多鏡專案幾乎一定會用到，所以更要先報價 |

### `project run --json` 的回傳

```json
{
  "ok": false,
  "command": "project run",
  "completed": ["s02"],
  "failed": [{ "id": "s01", "error": "HTTP 500: ..." }],
  "skipped": [],
  "not_started": ["s03", "s04"],
  "concat_path": null,
  "spent_usd": 0.07,
  "project_total_spent_usd": 0.07
}
```

`skipped` 是這次跳過的已完成分鏡（沒有再收費），`not_started` 是因為失敗即停而沒送出的。`concat_path` 只有在全部分鏡都完成時才會有值。

### 專案檔完整欄位

```json
{
  "title": "字串，僅用於顯示",
  "cast": { "角色名": "圖片路徑或 HTTPS 網址" },
  "defaults": { "model": "...", "size": "480x854", "duration": 5, "generate_audio": false },
  "scenes": [
    {
      "id": "s01",
      "prompt": "畫面描述",
      "cast": ["角色名"],
      "references": ["額外素材路徑"],
      "duration": 5,
      "size": "480x854",
      "generate_audio": false,
      "seed": 12345,
      "continue_from": "前一鏡的 id",
      "first_frame": "圖片路徑（與 continue_from 互斥）",
      "last_frame": "圖片路徑"
    }
  ],
  "output": { "concat": "outputs/final.mp4" }
}
```

各鏡的欄位覆寫 `defaults`。相對路徑以專案檔所在目錄為基準。

### 狀態檔

`project.json` 旁邊會自動產生 `project.state.json`：

```json
{
  "scenes": {
    "s01": { "status": "done", "job_id": "...", "video_path": "...",
             "cost": 0.07, "last_frame": ".frames/s01_last.png",
             "completed_at": "2026-08-21 01:20:11" }
  },
  "total_cost": 0.28,
  "updated_at": "..."
}
```

`status` 有 `pending` / `running` / `done` / `failed`。判定「已完成」時會一併確認影片檔還在，檔案被刪掉的鏡頭會重新生成。**不要手動編輯或刪除狀態檔**，刪掉等於讓整個專案重跑重收費。`.frames/` 放的是接續用的影格快取。

## gen 的旗標

| 旗標 | 型別 | 說明 |
|---|---|---|
| `prompt` | 位置參數 | 影片描述。若有 `--first-frame` 或 `--ref` 可省略 |
| `--model` | str | 預設 `bytedance/seedance-2.0-mini` |
| `--duration` | int | 秒數，預設 5 |
| `--size` | str | `寬x高`，預設 `480x854` |
| `--resolution` | str | `480p` / `720p`，與 `--size` 擇一 |
| `--aspect-ratio` | str | `9:16` / `16:9` 等，與 `--size` 擇一 |
| `--audio` | flag | 生成音訊，預設關 |
| `--seed` | int | 重現用 |
| `--ref` | str，可重複 | 參考素材，本機路徑或 HTTPS 網址 |
| `--first-frame` | str | 首影格圖片 |
| `--last-frame` | str | 尾影格圖片 |
| `--name` | str | 輸出檔名標籤 |
| `--out` | str | 輸出資料夾，預設 `outputs/` |
| `--dry-run` | flag | 只驗證與估價，不送單、不花錢 |
| `--yes` / `-y` | flag | 略過成本護欄。**只在使用者明確同意後才加** |
| `--json` | flag | stdout 輸出 JSON，進度走 stderr |

## JSON 輸出結構

### `gen --dry-run --json`

```json
{
  "ok": true,
  "command": "gen",
  "dry_run": true,
  "validated": true,
  "model": "bytedance/seedance-2.0-mini",
  "size": "480x854",
  "duration": 5,
  "generate_audio": false,
  "reference_count": 2,
  "estimate": {
    "tokens": 48037,
    "price_per_token": 3.5e-06,
    "list_price_usd": 0.168129,
    "sku": "video_tokens_without_audio",
    "width": 480, "height": 854, "duration": 5,
    "note": "牌價上限，未計促銷折扣；實際扣款以 usage.cost 為準"
  },
  "cost_limit_usd": 0.5,
  "exceeds_cost_limit": false,
  "request_fields": ["duration", "generate_audio", "model", "prompt", "size"]
}
```

### `gen --json`（實際生成）

```json
{
  "ok": true,
  "command": "gen",
  "dry_run": false,
  "job_id": "ZkLqzgpzmGz0Xc8aNG38",
  "video_path": "C:\\...\\outputs\\20260820-230139_..._480x854_15s.mp4",
  "record_path": "C:\\...\\outputs\\20260820-230139_..._480x854_15s.json",
  "elapsed_seconds": 197.2,
  "estimate": { "...": "同上" },
  "actual_cost_usd": 0.209394108,
  "prompt": "..."
}
```

`actual_cost_usd` 來自 OpenRouter 回報的 `usage.cost`，是真正的扣款金額。

### 錯誤

```json
{
  "ok": false,
  "command": "gen",
  "error": {
    "type": "JobTimeoutError",
    "message": "等待逾時（30 分鐘）...",
    "job_id": "ZkLqzgpzmGz0Xc8aNG38",
    "recoverable_with": "resume"
  }
}
```

離開碼為 1。`job_id` 與 `recoverable_with` 只在可以用 resume 救回時出現。

### `batch --json`

```json
{
  "ok": true, "command": "batch", "dry_run": false,
  "succeeded": 3, "total": 3,
  "videos": [
    { "name": "01-開場", "job_id": "...", "video_path": "...",
      "actual_cost_usd": 0.07, "elapsed_seconds": 88.4 }
  ],
  "failures": [],
  "concat_path": "outputs/final.mp4"
}
```

單支失敗不會中斷其他支：`ok` 為 false、`failures` 列出錯誤訊息，成功的仍在 `videos` 裡且已產出。

### `models --model X --json`

回傳 `supported_sizes`、`supported_durations`、`supported_aspect_ratios`、`supported_frame_images`、`generate_audio`、`seed`、`pricing_skus`、`default_size`、`default_duration`。

## 批次分鏡檔

```json
{
  "defaults": {
    "model": "bytedance/seedance-2.0-mini",
    "size": "480x854",
    "duration": 5,
    "generate_audio": false
  },
  "scenes": [
    {
      "name": "01-開場",
      "prompt": "清晨的老街，霧氣未散，鏡頭從地面緩慢上升",
      "duration": 6,
      "references": ["爸爸.png", "媽媽.png"],
      "first_frame": null,
      "last_frame": null,
      "seed": 12345
    }
  ]
}
```

單一 scene 的欄位會覆寫 `defaults`。也接受直接給一個 scene 陣列（沒有 `defaults`）。`name` 會變成輸出檔名的標籤，省略則自動編號。

`--concurrency` 預設 3。並行數開太大不會比較快，供應商端有自己的佇列。

## 設定與路徑

| 項目 | 位置 |
|---|---|
| 金鑰 | `.env` 的 `OPENROUTER_API_KEY`，找不到才用系統環境變數 |
| 成本護欄 | `.env` 的 `SEEDANCE_COST_LIMIT`，預設 0.50（美元） |
| 影片產出 | `outputs/` |
| 任務記錄 | `jobs/<job_id>.json` |
| 模型能力快取 | `.cache/videos_models.json`，24 小時 |

打包成執行檔後，上述路徑會落在**執行檔所在的資料夾**，而不是原始碼目錄。

## 底層 API 行為

OpenRouter 的影片生成是非同步任務：

1. `POST /api/v1/videos` → `202` 與 `{id, polling_url, status}`
2. 輪詢 `polling_url` 直到 `completed`
3. `GET /api/v1/videos/{id}/content?index=0` 下載 MP4

狀態機：`pending` → `in_progress` → `completed` / `failed` / `cancelled` / `expired`，後三者都是終態。

計價公式 `tokens = 寬 × 高 × 秒數 × 24 / 1024`，單價取自 `/videos/models` 的 `pricing_skus`：有影片輸入時單價較低（`video_tokens_with_video_input`）。

已知限制：

- 參考素材走 base64 內嵌，請求體總量上限 24 MB。素材多時改用公開 HTTPS 網址。
- 影片與音訊參考只有 Seedance 2 代以上的模型會採用，其他模型忽略。
- 影片生成不支援 Zero Data Retention。
