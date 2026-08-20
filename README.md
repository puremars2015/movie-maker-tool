# 趣味動畫製作 — Seedance 影片生成工具

用 OpenRouter 的 `bytedance/seedance-2.0-mini` 生成影片，CLI 與 GUI 兩種介面共用同一套核心邏輯。

## 安裝

```bash
pip install -r requirements.txt
```

只有 `requests` 是必要的。另外選裝 `Pillow`（自動把過大的參考圖縮小，省流量也避免請求被拒）與系統的 `ffmpeg`（批次模式合併影片用）。

## 設定金鑰

在專案根目錄建立 `.env`（可從 `.env.example` 複製）：

```
OPENROUTER_API_KEY=sk-or-v1-你的金鑰
```

查找順序是 **`.env` 優先，找不到才用系統環境變數**。`.env` 已列入 `.gitignore`。

## GUI

雙擊 `啟動GUI.bat`，或執行：

```bash
python -m seedance gui
```

介面提供：提示詞輸入、長寬（預設 `480x854`，最低解析度手機直式）、長度秒數、參考素材多選上傳（圖片／影片／音訊）、首尾影格、seed、音訊開關、即時費用預估、執行紀錄、播放與開啟輸出資料夾。

## CLI

```bash
python -m seedance gen "夕陽下的玻璃溫室，霧氣繚繞，鏡頭緩慢推軌向前"
```

```bash
python -m seedance gen "一家人在客廳玩耍" --ref 爸爸.png --ref 媽媽.png --duration 8 --size 720x1280
```

```bash
python -m seedance gen "測試" --dry-run
```

`--dry-run` 只做參數驗證與估價，不送單、不花錢。其他常用選項：`--duration` `--size` `--resolution` `--aspect-ratio` `--audio` `--seed` `--first-frame` `--last-frame` `--out` `--yes`。

### 批次分鏡

```bash
python -m seedance batch scenes.example.json --concurrency 3 --concat outputs/final.mp4
```

分鏡檔用 `defaults` 設共同參數、`scenes` 列各鏡，單鏡的設定會蓋過 defaults。`--concat` 需要 ffmpeg；沒裝就只跳過合併，個別影片照樣產出。

### 其他

```bash
python -m seedance models --model bytedance/seedance-2.0-mini
```

```bash
python -m seedance resume <job_id>
```

## 給 AI Agent 使用（Skill）

除了人用的 CLI 與 GUI，本專案也包成了 skill，讓 AI agent 能可靠地呼叫。

Skill 放在 `.claude/skills/seedance-video/`，在這個專案目錄下工作的 agent 會自動看到它。要讓所有專案都能用，複製到使用者層級即可：

```bash
cp -r .claude/skills/seedance-video ~/.claude/skills/
```

另有打包好的 `seedance-video.skill`（就是上面那個資料夾的壓縮檔），可用於安裝到其他環境或分享。

### `--json` 模式

Skill 之所以能可靠運作，靠的是 CLI 的機器可讀輸出。`gen`、`batch`、`resume`、`models` 都支援 `--json`：

```bash
python -m seedance gen "提示詞" --duration 5 --json --dry-run
```

約定是 **stdout 只有一個 JSON 物件，進度訊息全部走 stderr**，呼叫端可以直接 `json.loads(stdout)`。成功時 `ok: true`；失敗時 `ok: false`、`error.type` 是錯誤類別名稱、離開碼為 1。逾時錯誤還會附上 `job_id` 與 `recoverable_with: "resume"`，讓呼叫端知道那筆已計費、可以取件而不必重新生成。

Skill 本身最重要的規則是：**先 `--dry-run` 免費試算、把價格告訴使用者、得到同意才送單**。成本護欄擋下來時不會自動加 `--yes` 重試，那等於繞過使用者授權。

## 打包成執行檔（雙擊開啟，不用裝 Python）

macOS：

```bash
./build_macos.sh
```

Windows（需先裝好 Python 3.9+，安裝時勾選 "Add python.exe to PATH"）：

```
build_windows.bat
```

各自產出 `dist/Seedance.app`（macOS）或 `dist/Seedance.exe`（Windows）。**打包只能在對應系統上做**，這台機器做出的 App 不能拿去另一個作業系統用，Windows 版要在 Windows 機器上另外跑一次 `build_windows.bat`。

執行檔會把 `.env`、`outputs/`、`jobs/`、`.cache/` 放在**執行檔所在的資料夾**（而不是專案原始碼目錄），所以散布時把 `.env`（填好金鑰）跟執行檔放在同一個資料夾即可。

macOS 因為 App 未經 Apple 簽章，首次雙擊會被 Gatekeeper 擋下，對 App 按右鍵→「開啟」一次即可（之後就能正常雙擊）。

## 運作方式

OpenRouter 的影片生成是非同步任務：

1. `POST /api/v1/videos` → 回 `202` 與 `{id, polling_url, status}`
2. 輪詢 `polling_url` 直到 `completed`（其餘終態 `failed` / `cancelled` / `expired` 視為錯誤）
3. `GET /api/v1/videos/{id}/content?index=0` 下載 MP4

送單成功就會計費，所以程式在收到 job id 的當下立刻把任務寫進 `jobs/<id>.json`。若中途斷線或關掉程式，用 `resume <job_id>` 就能把影片取回來。

## 費用

Seedance 以 video token 計價：`tokens = (寬 × 高 × 秒數 × 24) / 1024`，單價取自 `/videos/models` 的 `pricing_skus`。

程式估的是**牌價上限**，不含 OpenRouter 頁面上的促銷折扣，實際扣款以任務回傳的 `usage.cost` 為準（會寫進影片旁的 `.json`）。參考值：480x854 五秒約 US$0.17，1280x720 十五秒約 US$1.13。

預設成本護欄為單次 US$0.50，超過時 CLI 需加 `--yes`、GUI 會跳出確認框。門檻可用 `.env` 裡的 `SEEDANCE_COST_LIMIT` 調整。

## 產出

- `outputs/日期-時間_標籤_寬x高_秒數.mp4` — 影片
- 同名 `.json` — 參數、seed、job id、實際費用，用來重現與對帳
- `jobs/<job_id>.json` — 任務狀態記錄，`resume` 會讀它

## 檔案結構

| 檔案 | 用途 |
|---|---|
| `seedance/config.py` | `.env` 解析、金鑰查找、路徑與門檻 |
| `seedance/capabilities.py` | 抓 `/videos/models`、快取、送單前驗參數 |
| `seedance/cost.py` | token 估算、計價 SKU、成本護欄 |
| `seedance/media.py` | 參考素材轉 content part、自動縮圖、大小上限 |
| `seedance/client.py` | 送單 / 輪詢 / 下載、任務記錄 |
| `seedance/runner.py` | 流程編排、批次、ffmpeg 合併 |
| `seedance/cli.py` | 命令列介面 |
| `seedance/gui.py` | Tkinter 圖形介面 |

## 已知限制

- 參考素材走 base64 內嵌，單一請求總量上限 24 MB；素材很多時建議改放公開 HTTPS 網址。
- 影片與音訊參考只有 Seedance 2 代以上的模型會實際採用，其他模型會忽略。
- 影片生成不支援 Zero Data Retention（非同步取件必須暫存）。
