# movie-maker-tool

透過 OpenRouter 生成影片，支援 ByteDance Seedance 2.0 Mini 與 MiniMax H3 等多個模型，
CLI 與 GUI 兩種介面共用同一套核心邏輯。所有模型參數都是從 API 讀回來的，換模型時選項會跟著變。

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
python -m movie_maker_tool gui
```

介面提供：模型選擇、提示詞輸入、輸出規格（預設最低解析度的手機直式）、長度秒數、參考素材多選上傳（圖片／影片／音訊）、首尾影格、seed、音訊開關、即時費用預估、執行紀錄、播放與開啟輸出資料夾。模型不支援的欄位（例如 H3 的 seed）會自動變灰。

## CLI

```bash
python -m movie_maker_tool gen "夕陽下的玻璃溫室，霧氣繚繞，鏡頭緩慢推軌向前"
```

```bash
python -m movie_maker_tool gen "一家人在客廳玩耍" --ref 爸爸.png --ref 媽媽.png --duration 8 --size 720x1280
```

```bash
python -m movie_maker_tool gen "測試" --dry-run
```

`--dry-run` 只做參數驗證與估價，不送單、不花錢。其他常用選項：`--duration` `--size` `--resolution` `--aspect-ratio` `--audio` `--seed` `--first-frame` `--last-frame` `--out` `--yes`。

## 選擇模型

工具不綁單一模型。所有參數都是從 OpenRouter 的 `/videos/models` 讀回來的，換模型時
GUI 與 CLI 的選項會跟著變。

```bash
python -m movie_maker_tool models --model minimax/hailuo-3
```

目前主要用的兩個差異不小：

| | ByteDance Seedance 2.0 Mini | MiniMax H3 |
|---|---|---|
| 解析度 | 480p / 720p | 2K only |
| 輸出規格 | 13 種明確像素尺寸 | 不接受 `size`，只吃解析度＋長寬比 |
| 秒數 | 4–15 | 5–15 |
| seed | 支援 | **不支援** |
| 音訊 | 支援 | 支援 |
| 計價 | 依畫面大小（token） | **依秒 $0.13＋每張參考圖 $0.04** |
| 5 秒一鏡（480x854 / 2K 9:16） | 牌價 $0.168、實際約 $0.070 | $0.650（無折扣） |

**H3 大約貴一個數量級**，因為它固定 2K 而且依秒計價。同一份 12 鏡分鏡，seedance 牌價
約 $2.5（實際約 $1.05），H3 約 $12.7。

GUI 的模型下拉選單只列出**算得出價錢的模型**（目前 11 個）。算不出價的不該讓人按下送出，
所以刻意排除；CLI 仍可用 `--model` 指定任何型號，只是會在估價階段擋下並說明原因。

切換模型時會連帶處理：

- **輸出規格清單重新產生** —— seedance 顯示 `480x854` 這種像素尺寸，H3 顯示 `2K 9:16`
- **秒數清單重新產生**，不支援的秒數會自動貼到最接近的合法值
- **不支援的欄位直接鎖住** —— 例如 H3 的 seed 欄位會變灰
- **專案模式會先列出所有要調整的分鏡再問你**，例如「s02：秒數 4 不支援，改為 5」

## 費用怎麼算

不同供應商計價方式不同，程式依 `pricing_skus` 自動判斷：

- **依畫面大小（token）** — Seedance 系列：`tokens = 寬 × 高 × 秒數 × 24 / 1024`
- **依秒** — H3、Kling、Hailuo 等：`每秒單價 × 秒數`，H3 另加每張參考圖 $0.04

**認不出計價方式的模型會直接報錯，不會估成 $0。** 這是刻意的：估成 0 會讓成本護欄
形同虛設，使用者以為免費卻被扣款。

估的一律是**牌價**。只有實測量過折扣的模型才會另外顯示預期金額（目前只有
seedance-2.0-mini，實測為牌價的 41.6%）。沒量過的模型直接顯示牌價，寧可高估。
實際扣款一律以任務回傳的 `usage.cost` 為準。

## 專案模式：一次轉出整部動畫

多鏡動畫用專案模式。它與舊的 `batch` 最大的差別是**有狀態**：每一鏡的結果都記在狀態檔裡，重跑時自動跳過已完成的鏡頭。十鏡跑到第七鏡失敗，重跑只會補那三鏡，不會把前六鏡重新生成一遍再收一次錢。

```bash
python -m movie_maker_tool project init project.json
```

會產生骨架，並把目錄裡現成的圖片自動填進 `cast`。專案檔長這樣：

```json
{
  "title": "家族小劇場 EP1",
  "cast": { "爸爸": "爸爸.png", "媽媽": "媽媽.png", "弟弟": "弟弟.png" },
  "defaults": { "size": "480x854", "duration": 5, "generate_audio": false },
  "scenes": [
    { "id": "s01", "prompt": "客廳，爸爸正要帶弟弟出門", "cast": ["爸爸", "弟弟"] },
    { "id": "s02", "prompt": "媽媽從廚房探頭叮嚀", "cast": ["媽媽"], "duration": 8 },
    { "id": "s03", "prompt": "父子在玄關相視而笑", "cast": ["爸爸", "弟弟"], "continue_from": "s01" }
  ],
  "output": { "concat": "outputs/EP1.mp4" }
}
```

- **`cast` 只定義一次**，各鏡用名字引用，改角色圖時只要改一處。
- **`continue_from`** 會用 ffmpeg 抽出前一鏡的最後一格當這一鏡的首影格，讓畫面接得起來。標了它的鏡頭必須等前一鏡完成，其餘鏡頭仍然並行 —— 同一個專案裡可以混用。
- **`id` 是狀態檔的對應鍵**，改了等於變成新的一鏡、會重新生成也重新收費。

```bash
python -m movie_maker_tool project check project.json
```

免費。驗證每一鏡、檢查接續有沒有斷鏈或循環，並列出每鏡估價與總價。確認後：

```bash
python -m movie_maker_tool project run project.json --yes
```

有鏡頭失敗時會**立即停止**，不再送出新的鏡頭；已經在跑的會等它完成，因為那筆已經計費，硬中斷只是讓錢白花。修正後直接重跑即可，已完成的不會重做。`--only s03,s07` 可只重跑特定幾鏡，`--force` 才會重做已完成的（等於再付一次錢）。

```bash
python -m movie_maker_tool project status project.json
```

隨時查每鏡狀態與累計花費。全部完成後會自動合併成 `output.concat` 指定的檔案。

GUI 的「專案批次」分頁可以開啟專案檔逐列編輯，不必手改 JSON：

- **角色圖**：按「編輯角色表…」新增角色、換圖、重新命名或移除。角色表是整個專案共用的，換一次圖，所有用到該角色的分鏡都跟著換。重新命名會自動搬移各鏡的引用。
- **分鏡圖**：每一鏡有自己的「本鏡參考素材」（場景圖、道具、風格參考）與「首影格／尾影格」。設了「接續自」時首影格欄位會鎖住，因為那格會自動取自前一鏡。
- 其餘欄位：提示詞、編號、秒數、長寬、生成音訊、出場角色勾選，並即時顯示總價。
- **只轉出某幾鏡**：點表格最左邊的「轉出」欄可勾選分鏡，總價與按鈕會跟著變成勾選的範圍，其餘分鏡完全不會動到。都不勾就是全部未完成的。勾選的分鏡若要接續某個還沒完成的鏡頭，會先問你要不要一併轉出。等同 CLI 的 `--only`。

選檔案時路徑會自動相對化到專案資料夾，整包搬移或分享時仍然有效。典型分工是 AI agent 產生初版 `project.json`，人在 GUI 裡微調。

### 舊的批次指令

```bash
python -m movie_maker_tool batch scenes.example.json --concurrency 3 --concat outputs/final.mp4
```

`batch` 仍然可用，但它沒有狀態，重跑會全部重新生成也重新收費。多鏡的情況建議用專案模式。

### 其他

```bash
python -m movie_maker_tool models --model bytedance/seedance-2.0-mini
```

```bash
python -m movie_maker_tool resume <job_id>
```

## 給 AI Agent 使用（Skill）

除了人用的 CLI 與 GUI，本專案也包成了 skill，讓 AI agent 能可靠地呼叫。

Skill 放在 `.claude/skills/movie-maker-tool/`，在這個專案目錄下工作的 agent 會自動看到它。要讓所有專案都能用，複製到使用者層級即可：

```bash
cp -r .claude/skills/movie-maker-tool ~/.claude/skills/
```

另有打包好的 `movie-maker-tool.skill`（就是上面那個資料夾的壓縮檔），可用於安裝到其他環境或分享。

### `--json` 模式

Skill 之所以能可靠運作，靠的是 CLI 的機器可讀輸出。`gen`、`batch`、`resume`、`models` 都支援 `--json`：

```bash
python -m movie_maker_tool gen "提示詞" --duration 5 --json --dry-run
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

各自產出 `dist/MovieMakerTool.app`（macOS）或 `dist/MovieMakerTool.exe`（Windows）。**打包只能在對應系統上做**，這台機器做出的 App 不能拿去另一個作業系統用，Windows 版要在 Windows 機器上另外跑一次 `build_windows.bat`。

執行檔會把 `.env`、`outputs/`、`jobs/`、`.cache/` 放在**執行檔所在的資料夾**（而不是專案原始碼目錄），所以散布時把 `.env`（填好金鑰）跟執行檔放在同一個資料夾即可。

macOS 因為 App 未經 Apple 簽章，首次雙擊會被 Gatekeeper 擋下，對 App 按右鍵→「開啟」一次即可（之後就能正常雙擊）。

## 運作方式

OpenRouter 的影片生成是非同步任務：

1. `POST /api/v1/videos` → 回 `202` 與 `{id, polling_url, status}`
2. 輪詢 `polling_url` 直到 `completed`（其餘終態 `failed` / `cancelled` / `expired` 視為錯誤）
3. `GET /api/v1/videos/{id}/content?index=0` 下載 MP4

送單成功就會計費，所以程式在收到 job id 的當下立刻把任務寫進 `jobs/<id>.json`。若中途斷線或關掉程式，用 `resume <job_id>` 就能把影片取回來。

## 成本護欄

預設單次 US$0.50，超過時 CLI 需加 `--yes`、GUI 會跳出確認框。門檻可用 `.env` 裡的
`MOVIE_MAKER_COST_LIMIT` 調整。計價方式見上面「費用怎麼算」。

## 產出

- `outputs/日期-時間_標籤_寬x高_秒數.mp4` — 影片
- 同名 `.json` — 參數、seed、job id、實際費用，用來重現與對帳
- `jobs/<job_id>.json` — 任務狀態記錄，`resume` 會讀它

## 檔案結構

| 檔案 | 用途 |
|---|---|
| `movie_maker_tool/config.py` | `.env` 解析、金鑰查找、路徑與門檻 |
| `movie_maker_tool/capabilities.py` | 抓 `/videos/models`、快取、送單前驗參數 |
| `movie_maker_tool/cost.py` | token 估算、計價 SKU、成本護欄 |
| `movie_maker_tool/media.py` | 參考素材轉 content part、自動縮圖、大小上限 |
| `movie_maker_tool/client.py` | 送單 / 輪詢 / 下載、任務記錄 |
| `movie_maker_tool/runner.py` | 流程編排、批次、ffmpeg 合併 |
| `movie_maker_tool/cli.py` | 命令列介面 |
| `movie_maker_tool/gui.py` | Tkinter 圖形介面 |

## 已知限制

- 參考素材走 base64 內嵌，單一請求總量上限 24 MB；素材很多時建議改放公開 HTTPS 網址。
- 影片與音訊參考只有 Seedance 2 代以上的模型會實際採用，其他模型會忽略。
- 影片生成不支援 Zero Data Retention（非同步取件必須暫存）。
