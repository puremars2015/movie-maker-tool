"""GUI 的「專案批次」分頁：載入分鏡、逐列編輯、一次轉出。

典型用法是 AI agent 先產生一版 project.json，再到這裡逐列微調提示詞、秒數與出場
角色，確認總價後一次轉出。所以這個分頁的重點是「看得到每一鏡的狀態與價格」，
而不是把所有參數都塞進畫面。

編輯採自動套用：切換選取列、存檔、開始轉出之前都會把編輯區的內容寫回該列，
使用者不必記得按「套用」。
"""

from __future__ import annotations

import json
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from . import cost as cost_module
from .capabilities import ModelCapabilities, get_capabilities
from .config import DEFAULT_MODEL, get_api_key
from .errors import SeedanceError
from .media import FILE_DIALOG_TYPES, is_url
from .project import ProjectState, load_project, write_template
from .project_runner import plan_project, run_project

STATUS_MARK = {"done": "✓ 完成", "failed": "✗ 失敗", "running": "… 進行中", "pending": "待生成"}
IMAGE_TYPES = [("圖片", "*.png *.jpg *.jpeg *.webp *.gif *.bmp"), ("所有檔案", "*.*")]


class ProjectTab(ttk.Frame):
    def __init__(self, parent, caps_provider):
        super().__init__(parent, padding=10)
        self.caps_provider = caps_provider          # 回傳 ModelCapabilities 或 None
        self.project = None
        self.state: ProjectState | None = None
        self.scenes: list[dict] = []                # 原始 dict，存檔時直接寫回
        self.current_index: int | None = None
        self.cast_vars: dict[str, tk.BooleanVar] = {}
        self.scene_refs: list[str] = []          # 目前這一鏡的專屬素材
        self.picked_ids: set[str] = set()        # 勾選要轉出的分鏡；空的代表全部未完成
        self.model_choices: list[tuple[str, str]] = []
        self.project_caps = None                 # 依專案自己的模型載入，不跟單支分頁共用
        self.output_options: list = []
        self.messages: queue.Queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None

        self._build()
        self.after(150, self._drain)

    # --- 版面 ---------------------------------------------------------

    def _build(self) -> None:
        top = ttk.Frame(self)
        top.pack(fill="x", pady=(0, 8))
        ttk.Button(top, text="開啟專案…", command=self._open).pack(side="left")
        ttk.Button(top, text="新建…", command=self._new).pack(side="left", padx=4)
        ttk.Button(top, text="儲存", command=self._save).pack(side="left")
        self.path_var = tk.StringVar(value="尚未開啟專案")
        ttk.Label(top, textvariable=self.path_var, foreground="#666").pack(side="left", padx=10)

        # 專案有自己的模型，不跟單支分頁共用——兩邊可以同時在做不同的事。
        self.model_var = tk.StringVar()
        self.model_combo = ttk.Combobox(top, textvariable=self.model_var, state="readonly", width=30)
        self.model_combo.pack(side="right")
        self.model_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_model_change())
        ttk.Label(top, text="模型").pack(side="right", padx=(0, 4))

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=3)
        body.columnconfigure(1, weight=2)
        body.rowconfigure(0, weight=1)

        self._build_table(body)
        self._build_editor(body)
        self._build_bottom()

    def _build_table(self, parent) -> None:
        left = ttk.Frame(parent)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)

        columns = ("pick", "status", "id", "dur", "cast", "chain", "cost", "prompt")
        self.tree = ttk.Treeview(left, columns=columns, show="headings", selectmode="browse")
        headings = {
            "pick": ("轉出", 40), "status": ("狀態", 70), "id": ("編號", 55), "dur": ("秒", 35),
            "cast": ("角色", 90), "chain": ("接續", 55), "cost": ("估價", 60),
            "prompt": ("提示詞", 220),
        }
        for key, (label, width) in headings.items():
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, anchor="center" if key == "pick" else "w")
        self.tree.grid(row=0, column=0, sticky="nsew")

        scroll = ttk.Scrollbar(left, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Button-1>", self._on_tree_click)

        buttons = ttk.Frame(left)
        buttons.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        for text, command in (
            ("新增分鏡", self._add_scene), ("刪除", self._delete_scene),
            ("上移", lambda: self._move(-1)), ("下移", lambda: self._move(1)),
        ):
            ttk.Button(buttons, text=text, width=9, command=command).pack(side="left", padx=2)

        picks = ttk.Frame(left)
        picks.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        ttk.Label(picks, text="點「轉出」欄可勾選要生成哪幾鏡；都不勾就是全部未完成的。",
                  foreground="#888", font=("Microsoft JhengHei UI", 8)).pack(side="left")
        ttk.Button(picks, text="清除勾選", width=9, command=self._clear_picks).pack(side="right", padx=2)
        ttk.Button(picks, text="勾選未完成", width=11, command=self._pick_pending).pack(side="right")

    def _build_editor(self, parent) -> None:
        editor = ttk.LabelFrame(parent, text="編輯選取的分鏡", padding=10)
        editor.grid(row=0, column=1, sticky="nsew")
        editor.columnconfigure(1, weight=1)
        editor.rowconfigure(1, weight=1)

        ttk.Label(editor, text="提示詞").grid(row=0, column=0, sticky="nw", pady=2)
        self.prompt_text = tk.Text(editor, height=5, wrap="word", font=("Microsoft JhengHei UI", 10))
        self.prompt_text.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(2, 8))

        row = 2
        ttk.Label(editor, text="編號").grid(row=row, column=0, sticky="w", pady=3)
        self.id_var = tk.StringVar()
        ttk.Entry(editor, textvariable=self.id_var).grid(row=row, column=1, sticky="ew", pady=3)

        row += 1
        ttk.Label(editor, text="長度（秒）").grid(row=row, column=0, sticky="w", pady=3)
        self.duration_var = tk.StringVar()
        self.duration_combo = ttk.Combobox(editor, textvariable=self.duration_var, state="readonly")
        self.duration_combo.grid(row=row, column=1, sticky="ew", pady=3)

        row += 1
        ttk.Label(editor, text="輸出規格").grid(row=row, column=0, sticky="w", pady=3)
        self.size_var = tk.StringVar()
        self.size_combo = ttk.Combobox(editor, textvariable=self.size_var, state="readonly")
        self.size_combo.grid(row=row, column=1, sticky="ew", pady=3)

        row += 1
        ttk.Label(editor, text="接續自").grid(row=row, column=0, sticky="w", pady=3)
        self.chain_var = tk.StringVar()
        self.chain_combo = ttk.Combobox(editor, textvariable=self.chain_var, state="readonly")
        self.chain_combo.grid(row=row, column=1, sticky="ew", pady=3)
        self.chain_combo.bind("<<ComboboxSelected>>", lambda _e: self._sync_frame_widgets())

        row += 1
        self.audio_var = tk.BooleanVar(value=False)
        self.audio_check = ttk.Checkbutton(editor, text="生成音訊", variable=self.audio_var)
        self.audio_check.grid(row=row, column=0, columnspan=2, sticky="w", pady=3)

        row += 1
        cast_header = ttk.Frame(editor)
        cast_header.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Label(cast_header, text="出場角色").pack(side="left")
        ttk.Button(cast_header, text="編輯角色表…", width=11, command=self._edit_cast).pack(side="right")

        row += 1
        self.cast_frame = ttk.Frame(editor)
        self.cast_frame.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(2, 6))

        # 這一鏡專屬的素材：與角色表無關，適合放場景圖、道具參考或風格圖。
        row += 1
        ref_header = ttk.Frame(editor)
        ref_header.grid(row=row, column=0, columnspan=2, sticky="ew")
        ttk.Label(ref_header, text="本鏡參考素材").pack(side="left")
        ttk.Button(ref_header, text="移除", width=6, command=self._remove_scene_ref).pack(side="right", padx=2)
        ttk.Button(ref_header, text="新增…", width=7, command=self._add_scene_refs).pack(side="right")

        row += 1
        self.ref_list = tk.Listbox(editor, height=3, activestyle="none", exportselection=False)
        self.ref_list.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(2, 6))

        row += 1
        ttk.Label(editor, text="首影格").grid(row=row, column=0, sticky="w", pady=3)
        self.first_frame_var = tk.StringVar()
        self.first_frame_widgets = self._image_picker(editor, row, self.first_frame_var)

        row += 1
        ttk.Label(editor, text="尾影格").grid(row=row, column=0, sticky="w", pady=3)
        self.last_frame_var = tk.StringVar()
        self._image_picker(editor, row, self.last_frame_var)

        row += 1
        self.frame_hint = ttk.Label(editor, text="", foreground="#888",
                                    font=("Microsoft JhengHei UI", 8), wraplength=300)
        self.frame_hint.grid(row=row, column=0, columnspan=2, sticky="w")

        row += 1
        ttk.Button(editor, text="套用到此列", command=self._apply_editor).grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=(8, 0))

    def _image_picker(self, parent, row: int, var: tk.StringVar) -> tuple:
        frame = ttk.Frame(parent)
        frame.grid(row=row, column=1, sticky="ew", pady=3)
        frame.columnconfigure(0, weight=1)
        entry = ttk.Entry(frame, textvariable=var)
        entry.grid(row=0, column=0, sticky="ew")

        def choose() -> None:
            path = filedialog.askopenfilename(title="選擇圖片", filetypes=IMAGE_TYPES)
            if path:
                var.set(self._relative_to_project(path))

        def clear() -> None:
            var.set("")

        browse = ttk.Button(frame, text="…", width=3, command=choose)
        browse.grid(row=0, column=1, padx=(4, 0))
        remove = ttk.Button(frame, text="✕", width=3, command=clear)
        remove.grid(row=0, column=2, padx=(2, 0))
        return entry, browse, remove

    def _build_bottom(self) -> None:
        bottom = ttk.Frame(self)
        bottom.pack(fill="x", pady=(10, 0))

        self.summary_var = tk.StringVar(value="—")
        ttk.Label(bottom, textvariable=self.summary_var, foreground="#0a6").pack(side="left")

        self.run_button = ttk.Button(bottom, text="開始轉出", command=self._run, state="disabled")
        self.run_button.pack(side="right")
        self.stop_button = ttk.Button(bottom, text="停止", command=self._stop, state="disabled")
        self.stop_button.pack(side="right", padx=6)

        self.progress = ttk.Progressbar(self, mode="indeterminate")
        self.progress.pack(fill="x", pady=(6, 6))

        log_frame = ttk.LabelFrame(self, text="執行紀錄", padding=6)
        log_frame.pack(fill="both", expand=True)
        self.log_text = tk.Text(log_frame, height=7, wrap="word", state="disabled", font=("Consolas", 9))
        scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    # --- 專案檔 -------------------------------------------------------

    def _open(self, path: str | None = None) -> bool:
        """載入專案檔。回傳是否成功——呼叫端要靠它判斷能不能繼續。"""
        path = path or filedialog.askopenfilename(
            title="開啟專案檔", filetypes=[("專案 JSON", "*.json"), ("所有檔案", "*.*")]
        )
        if not path:
            return False
        try:
            self.project = load_project(path)
        except SeedanceError as exc:
            messagebox.showerror("專案檔有問題", str(exc))
            return False

        self.state = ProjectState.load(self.project)
        self.scenes = [dict(s) for s in (self.project.raw.get("scenes") or [])]
        self.current_index = None
        self.path_var.set(str(self.project.path))
        self.project_caps = None
        self._show_current_model()
        self._load_project_caps_async(self._project_model())
        self._build_cast_checkboxes()
        self._refresh_options()
        self._refresh_table()
        self.run_button.configure(state="normal")
        self._log("已開啟 %s（%d 鏡）" % (self.project.path.name, len(self.scenes)))
        return True

    def _new(self) -> None:
        path = filedialog.asksaveasfilename(
            title="新建專案檔", defaultextension=".json",
            filetypes=[("專案 JSON", "*.json")], initialfile="project.json",
        )
        if not path:
            return
        target = Path(path)
        images = sorted(target.parent.glob("*.png"))
        write_template(target, cast_images=images)
        self._open(str(target))

    def _save(self) -> bool:
        if not self.project:
            return False
        self._flush_editor()
        data = dict(self.project.raw)
        data["scenes"] = self.scenes
        self.project.path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self._log("已儲存 %s" % self.project.path.name)
        # 重新載入以套用驗證與估價。若載入失敗，記憶體裡還是舊的專案內容，
        # 這時絕不能繼續往下轉出——那會拿過期的分鏡去生成，而且是要花錢的。
        return self._open(str(self.project.path))

    # --- 表格 ---------------------------------------------------------

    def _caps(self):
        """優先用專案自己的模型能力；還沒載入時退回單支分頁的，至少讓畫面有東西。"""
        return self.project_caps or self.caps_provider()

    def set_model_choices(self, choices) -> None:
        self.model_choices = list(choices)
        self.model_combo["values"] = [label for _, label in self.model_choices]
        self._show_current_model()

    def _show_current_model(self) -> None:
        current = self._project_model()
        for model_id, label in self.model_choices:
            if model_id == current:
                self.model_var.set(label)
                return
        self.model_var.set(current or "")

    def _project_model(self) -> str:
        if not self.project:
            return ""
        return (self.project.defaults or {}).get("model") or DEFAULT_MODEL

    def _selected_model_id(self):
        label = self.model_var.get()
        for model_id, text in self.model_choices:
            if text == label:
                return model_id
        return None

    def _load_project_caps_async(self, model: str) -> None:
        def work() -> None:
            try:
                self.messages.put(("project_caps", get_capabilities(model)))
            except SeedanceError as exc:
                self.messages.put(("log", "載入 %s 的能力失敗：%s" % (model, exc)))

        threading.Thread(target=work, daemon=True).start()

    def _on_model_change(self) -> None:
        model_id = self._selected_model_id()
        if not self.project or not model_id or model_id == self._project_model():
            return

        try:
            caps = get_capabilities(model_id)
        except SeedanceError as exc:
            messagebox.showerror("載入模型失敗", str(exc))
            self._show_current_model()
            return

        # 換模型會讓既有分鏡的尺寸、秒數等設定失效，先算出要改什麼再問。
        self._flush_editor()
        changes = self._plan_model_migration(caps)
        if changes and not messagebox.askyesno(
            "換模型需要調整分鏡",
            "改用 %s 之後，這些設定必須跟著調整：\n\n%s\n\n要套用嗎？"
            % (caps.name or caps.id, "\n".join(changes[:12])
               + ("\n…共 %d 項" % len(changes) if len(changes) > 12 else "")),
        ):
            self._show_current_model()
            return

        self._apply_model_migration(caps)
        self.project.defaults["model"] = caps.id
        self.project.raw.setdefault("defaults", {})["model"] = caps.id
        self.project_caps = caps
        for line in changes:
            self._log("  " + line)
        self._log("已改用 %s，記得按儲存。" % caps.id)
        self._refresh_options()
        self._refresh_table()
        if self.current_index is not None and self.current_index < len(self.scenes):
            self._load_editor(self.scenes[self.current_index])

    def _plan_model_migration(self, caps) -> list[str]:
        """列出換成 caps 之後必須調整的設定。只描述，不動資料。"""
        changes: list[str] = []
        default_option = caps.default_output()
        blocks = [("defaults", self.project.defaults or {})]
        blocks += [(s.get("id", "?"), s) for s in self.scenes]

        for name, block in blocks:
            size = block.get("size")
            resolution = block.get("resolution")
            fallback = default_option.label if default_option else "預設"

            if caps.uses_explicit_sizes:
                # 目標模型吃明確尺寸：清掉另一種寫法的殘骸，否則兩者會同時存在而互相矛盾。
                if resolution or block.get("aspect_ratio"):
                    changes.append("%s：移除解析度 %s，改用尺寸 %s"
                                   % (name, resolution or block.get("aspect_ratio"), fallback))
                if size and size not in caps.supported_sizes:
                    changes.append("%s：size %s 不支援，改為 %s" % (name, size, fallback))
            else:
                if size:
                    changes.append("%s：移除 size %s，改用 %s" % (name, size, fallback))
                if resolution and resolution not in caps.supported_resolutions:
                    changes.append("%s：解析度 %s 不支援，改為 %s" % (name, resolution, fallback))

            duration = block.get("duration")
            if duration and caps.supported_durations and int(duration) not in caps.supported_durations:
                changes.append("%s：秒數 %s 不支援，改為 %d" % (name, duration, _nearest(int(duration), caps.supported_durations)))

            if block.get("generate_audio") and not caps.generate_audio:
                changes.append("%s：關閉音訊（此模型不支援）" % name)
            if block.get("seed") is not None and not caps.seed:
                changes.append("%s：移除 seed（此模型不支援）" % name)
        return changes

    def _apply_model_migration(self, caps) -> None:
        default_option = caps.default_output()
        for block in [self.project.defaults or {}] + self.scenes:
            size = block.get("size")
            resolution = block.get("resolution")

            # 先判斷這一塊的規格在新模型下還合不合用，再決定要不要換掉。
            if caps.uses_explicit_sizes:
                stale = bool(resolution) or bool(block.get("aspect_ratio"))
                invalid = bool(size) and size not in caps.supported_sizes
            else:
                stale = bool(size)
                invalid = bool(resolution) and resolution not in caps.supported_resolutions

            if stale or invalid or not (size or resolution):
                # 三個欄位一起清掉再重寫，避免留下另一種寫法的殘骸——這正是
                # 「切成 H3 再切回 seedance」會讓 defaults 同時有 size 與 2K 的原因。
                for key in ("size", "resolution", "aspect_ratio"):
                    block.pop(key, None)
                if default_option:
                    block.update(default_option.as_request_fields())

            duration = block.get("duration")
            if duration and caps.supported_durations and int(duration) not in caps.supported_durations:
                block["duration"] = _nearest(int(duration), caps.supported_durations)

            if block.get("generate_audio") and not caps.generate_audio:
                block["generate_audio"] = False
            if block.get("seed") is not None and not caps.seed:
                block.pop("seed", None)

        self.project.raw["defaults"] = self.project.defaults

    def _refresh_options(self) -> None:
        caps: ModelCapabilities | None = self._caps()
        if caps:
            self.duration_combo["values"] = [str(d) for d in caps.supported_durations]
            self.output_options = caps.output_options()
            self.size_combo["values"] = [o.label for o in self.output_options]
            self.audio_check.configure(state="normal" if caps.generate_audio else "disabled")
            if not caps.generate_audio:
                self.audio_var.set(False)

        # 不要把自己列進「接續自」——選了會被忽略，使用者只會看到沒反應。
        current_id = None
        if self.current_index is not None and self.current_index < len(self.scenes):
            current_id = self.scenes[self.current_index].get("id")
        others = [s.get("id", "") for s in self.scenes if s.get("id") != current_id]
        self.chain_combo["values"] = ["（不接續）"] + others

    def _refresh_table(self) -> None:
        self.tree.delete(*self.tree.get_children())
        if not self.project:
            return

        estimates = self._estimates()
        self._prune_picks()
        targets = self._target_ids()
        total = 0.0
        expected = 0.0

        for index, scene in enumerate(self.scenes):
            scene_id = scene.get("id", "s%02d" % (index + 1))
            done = bool(self.state and self.state.is_done(scene_id))
            status = "done" if done else (self.state.status_of(scene_id) if self.state else "pending")
            estimate = estimates.get(scene_id)
            if scene_id in targets and estimate:
                total += estimate.usd
                expected += estimate.expected_usd

            self.tree.insert("", "end", iid=str(index), values=(
                "☑" if scene_id in self.picked_ids else "☐",
                STATUS_MARK.get(status, status),
                scene_id,
                scene.get("duration", self.project.defaults.get("duration", "")),
                "、".join(scene.get("cast") or []),
                scene.get("continue_from") or "",
                ("US$%.3f" % estimate.usd) if estimate else "—",
                (scene.get("prompt") or "")[:60],
            ))

        spent = self.state.total_cost() if self.state else 0.0
        scope = "勾選 %d 鏡" % len(targets) if self.picked_ids else "待生成 %d 鏡" % len(targets)
        # 有實測折扣的模型才顯示預期金額；沒量過的直接顯示牌價，不要讓人以為比較便宜。
        if abs(expected - total) > 1e-9:
            money = "牌價 US$%.3f（依實測折扣預期約 US$%.3f）" % (total, expected)
        else:
            money = "預估 US$%.3f" % total
        self.summary_var.set("%s，%s｜已花費 US$%.4f" % (scope, money, spent))
        if getattr(self, "run_button", None):
            self.run_button.configure(
                text="轉出勾選的 %d 鏡" % len(targets) if self.picked_ids else "開始轉出"
            )

        # 重建表格會清掉選取，補回來並保持與 current_index 一致；不一致的話
        # 佇列裡的 <<TreeviewSelect>> 會被當成「使用者換了一列」而互相打架。
        if self.current_index is not None and 0 <= self.current_index < len(self.scenes):
            self.tree.selection_set(str(self.current_index))

    # --- 勾選要轉出的分鏡 ---------------------------------------------

    def _on_tree_click(self, event):
        """點「轉出」欄切換勾選。回傳 break 讓這一下不要順便改變選取列，
        否則勾一個框就會把右邊的編輯區也切走，很干擾。"""
        if self.tree.identify_region(event.x, event.y) != "cell":
            return None
        if self.tree.identify_column(event.x) != "#1":
            return None
        row = self.tree.identify_row(event.y)
        if not row:
            return None

        scene_id = self.scenes[int(row)].get("id")
        if not scene_id:
            return "break"
        if scene_id in self.picked_ids:
            self.picked_ids.discard(scene_id)
        else:
            self.picked_ids.add(scene_id)
        self._refresh_table()
        return "break"

    def _target_ids(self) -> list[str]:
        """這次實際會生成的分鏡：有勾選就只算勾的，並一律排除已完成的。"""
        ids = [s.get("id") for s in self.scenes if s.get("id")]
        if self.picked_ids:
            ids = [i for i in ids if i in self.picked_ids]
        return [i for i in ids if not (self.state and self.state.is_done(i))]

    def _prune_picks(self) -> None:
        self.picked_ids &= {s.get("id") for s in self.scenes if s.get("id")}

    def _pick_pending(self) -> None:
        self.picked_ids = {s.get("id") for s in self.scenes
                           if s.get("id") and not (self.state and self.state.is_done(s["id"]))}
        self._refresh_table()

    def _clear_picks(self) -> None:
        self.picked_ids.clear()
        self._refresh_table()

    def _missing_dependencies(self, targets: list[str]) -> list[str]:
        """勾選的分鏡若要接續某鏡，而那鏡既沒完成也沒被勾，就得補進來，
        否則它永遠等不到前一鏡，plan_project 會直接擋下整批。"""
        by_id = {s.get("id"): s for s in self.scenes}
        needed: list[str] = []
        queue_ = list(targets)
        seen = set(targets)
        while queue_:
            scene = by_id.get(queue_.pop())
            dep = scene.get("continue_from") if scene else None
            if not dep or dep in seen:
                continue
            if self.state and self.state.is_done(dep):
                continue
            seen.add(dep)
            needed.append(dep)
            queue_.append(dep)
        return needed

    def _estimates(self) -> dict:
        """逐鏡估價。

        刻意直接從編輯中的 self.scenes 算，而不是從已載入的 Project 物件——
        使用者改了秒數就該立刻看到價格變動，不必先存檔重載。也因此這裡要容錯：
        編輯到一半的分鏡不該讓整個表格畫不出來。
        """
        if not self.project:
            return {}
        caps = self._caps()
        if not caps:
            return {}

        results = {}
        for index, scene in enumerate(self.scenes):
            scene_id = scene.get("id", "s%02d" % (index + 1))
            merged = {**self.project.defaults, **scene}
            try:
                option = caps.default_output()
                results[scene_id] = cost_module.estimate(
                    caps,
                    duration=int(merged.get("duration") or caps.default_duration()),
                    size=merged.get("size") or (option.size if option else None),
                    resolution=merged.get("resolution") or (option.resolution if option else None),
                    aspect_ratio=merged.get("aspect_ratio") or (option.aspect_ratio if option else None),
                    generate_audio=bool(merged.get("generate_audio")),
                    reference_count=len(merged.get("references") or [])
                    + bool(merged.get("first_frame")) + bool(merged.get("last_frame")),
                )
            except (SeedanceError, ValueError, TypeError):
                continue
        return results

    # --- 編輯 ---------------------------------------------------------

    def _option_by_label(self, label: str):
        for option in self.output_options:
            if option.label == label:
                return option
        return None

    def _option_label(self, scene: dict, defaults: dict) -> str:
        """把分鏡目前的規格對回下拉選單的標籤。"""
        size = scene.get("size") or defaults.get("size")
        if size:
            for option in self.output_options:
                if option.size == size:
                    return option.label
        resolution = scene.get("resolution") or defaults.get("resolution")
        aspect = scene.get("aspect_ratio") or defaults.get("aspect_ratio")
        if resolution:
            for option in self.output_options:
                if option.resolution == resolution and option.aspect_ratio == aspect:
                    return option.label
        return self.output_options[0].label if self.output_options else ""

    def _build_cast_checkboxes(self) -> None:
        for child in self.cast_frame.winfo_children():
            child.destroy()
        self.cast_vars = {}
        for name in (self.project.cast if self.project else {}):
            var = tk.BooleanVar(value=False)
            self.cast_vars[name] = var
            ttk.Checkbutton(self.cast_frame, text=name, variable=var).pack(side="left", padx=2)

    def _on_select(self, _event=None) -> None:
        """切換選取列：先把上一列的編輯寫回資料，再載入新的一列。

        這裡只能呼叫 _flush_editor（純資料），不能呼叫 _apply_editor。
        _apply_editor 會改動選取狀態，而 Tk 的 <<TreeviewSelect>> 是排進佇列
        非同步送達的——若在這裡把選取設回舊的一列，下一輪事件又會發現不一致，
        兩者就會無限來回把事件迴圈塞爆，UI 直接凍結。
        """
        selection = self.tree.selection()
        if not selection:
            return
        index = int(selection[0])
        if index == self.current_index or index >= len(self.scenes):
            return

        self._flush_editor()
        self.current_index = index
        self._refresh_options()          # 先更新「接續自」清單，才排得掉新選這一鏡自己
        self._load_editor(self.scenes[index])
        self._refresh_table()   # 結束時選取列會等於 current_index，巢狀事件一比就返回

    def _load_editor(self, scene: dict) -> None:
        defaults = self.project.defaults if self.project else {}
        self.prompt_text.delete("1.0", "end")
        self.prompt_text.insert("1.0", scene.get("prompt", ""))
        self.id_var.set(scene.get("id", ""))
        self.duration_var.set(str(scene.get("duration") or defaults.get("duration", 5)))
        self.size_var.set(self._option_label(scene, defaults))
        self.chain_var.set(scene.get("continue_from") or "（不接續）")
        self.audio_var.set(bool(scene.get("generate_audio", defaults.get("generate_audio", False))))
        selected = set(scene.get("cast") or [])
        for name, var in self.cast_vars.items():
            var.set(name in selected)

        self.scene_refs = list(scene.get("references") or [])
        self._refresh_ref_list()
        self.first_frame_var.set(scene.get("first_frame") or "")
        self.last_frame_var.set(scene.get("last_frame") or "")
        self._sync_frame_widgets()

    def _flush_editor(self) -> None:
        """把編輯區的欄位寫回目前這一列的資料。純資料操作，不碰任何元件狀態。"""
        if self.current_index is None or self.current_index >= len(self.scenes):
            return
        scene = self.scenes[self.current_index]
        previous_id = scene.get("id", "")
        scene["id"] = self.id_var.get().strip() or previous_id
        if previous_id != scene["id"] and previous_id in self.picked_ids:
            self.picked_ids.discard(previous_id)
            self.picked_ids.add(scene["id"])
        scene["prompt"] = self.prompt_text.get("1.0", "end").strip()

        if self.duration_var.get().isdigit():
            scene["duration"] = int(self.duration_var.get())
        option = self._option_by_label(self.size_var.get())
        if option:
            # 依模型還原：有明確尺寸就寫 size，否則寫 resolution + aspect_ratio。
            for key in ("size", "resolution", "aspect_ratio"):
                scene.pop(key, None)
            scene.update(option.as_request_fields())
        scene["generate_audio"] = bool(self.audio_var.get())

        chain = self.chain_var.get()
        if chain and chain != "（不接續）" and chain != scene["id"]:
            scene["continue_from"] = chain
            scene.pop("first_frame", None)   # 兩者衝突，接續優先
        else:
            scene.pop("continue_from", None)

        cast = [name for name, var in self.cast_vars.items() if var.get()]
        if cast:
            scene["cast"] = cast
        else:
            scene.pop("cast", None)

        if self.scene_refs:
            scene["references"] = list(self.scene_refs)
        else:
            scene.pop("references", None)

        # 接續會自動填首影格，兩者互斥，所以有接續時就不寫 first_frame。
        first = self.first_frame_var.get().strip()
        if first and not scene.get("continue_from"):
            scene["first_frame"] = first
        else:
            scene.pop("first_frame", None)

        last = self.last_frame_var.get().strip()
        if last:
            scene["last_frame"] = last
        else:
            scene.pop("last_frame", None)

    # --- 素材 ---------------------------------------------------------

    def _relative_to_project(self, path: str) -> str:
        """能相對就相對，讓專案資料夾整包搬移或分享時路徑仍然有效。"""
        if not self.project or is_url(path):
            return path
        try:
            return Path(path).resolve().relative_to(self.project.root).as_posix()
        except ValueError:
            return str(Path(path))

    def _add_scene_refs(self) -> None:
        if self.current_index is None:
            messagebox.showinfo("先選一個分鏡", "請先在左邊選取要加素材的分鏡。")
            return
        paths = filedialog.askopenfilenames(title="選擇本鏡參考素材", filetypes=FILE_DIALOG_TYPES)
        for path in paths:
            relative = self._relative_to_project(path)
            if relative not in self.scene_refs:
                self.scene_refs.append(relative)
        self._refresh_ref_list()

    def _remove_scene_ref(self) -> None:
        for index in reversed(self.ref_list.curselection()):
            del self.scene_refs[index]
        self._refresh_ref_list()

    def _refresh_ref_list(self) -> None:
        self.ref_list.delete(0, "end")
        for source in self.scene_refs:
            self.ref_list.insert("end", source)

    def _sync_frame_widgets(self) -> None:
        """有接續時鎖住首影格欄位——由程式抽前一鏡的最後一格填入，手動指定會衝突。"""
        chained = bool(self.chain_var.get()) and self.chain_var.get() != "（不接續）"
        state = "disabled" if chained else "normal"
        for widget in getattr(self, "first_frame_widgets", ()):
            widget.configure(state=state)
        if chained:
            self.first_frame_var.set("")
            self.frame_hint.configure(text="首影格會自動取自接續的前一鏡，不需要也不能手動指定。")
        else:
            self.frame_hint.configure(text="首尾影格用來精確控制開頭／結尾畫面；只想維持角色一致就用出場角色即可。")

    def _edit_cast(self) -> None:
        if not self.project:
            return
        CastDialog(self, self.project)

    def _on_cast_changed(self, renames: dict[str, str] | None = None) -> None:
        """角色表改完後：套用改名、重建勾選框、把已不存在的角色從各鏡移除。

        改名一定要連帶搬移各鏡的引用，否則那個角色會從所有分鏡裡靜靜消失，
        使用者要等到影片生出來才發現人不見了。
        """
        renames = renames or {}
        names = set(self.project.cast)
        for scene in self.scenes:
            current = [renames.get(n, n) for n in (scene.get("cast") or [])]
            kept = [n for n in current if n in names]
            if kept:
                scene["cast"] = kept
            else:
                scene.pop("cast", None)

        self._build_cast_checkboxes()
        if self.current_index is not None and self.current_index < len(self.scenes):
            self._load_editor(self.scenes[self.current_index])
        self._refresh_table()
        self._log("角色表已更新：%s" % ("、".join(self.project.cast) or "（空）"))

    def _apply_editor(self, silent: bool = False) -> None:
        """寫回資料並刷新畫面。給「套用到此列」按鈕與存檔／執行前呼叫。"""
        self._flush_editor()
        if self.current_index is None or self.current_index >= len(self.scenes):
            return
        if not silent:
            self._log("已套用到 %s" % self.scenes[self.current_index].get("id", ""))
        self._refresh_options()
        self._refresh_table()

    def _add_scene(self) -> None:
        if not self.project:
            return
        self._flush_editor()
        existing = {s.get("id") for s in self.scenes}
        index = len(self.scenes) + 1
        while ("s%02d" % index) in existing:
            index += 1
        self.scenes.append({"id": "s%02d" % index, "prompt": ""})
        self.current_index = None
        self._refresh_options()
        self._refresh_table()
        self.tree.selection_set(str(len(self.scenes) - 1))

    def _delete_scene(self) -> None:
        if self.current_index is None:
            return
        scene = self.scenes[self.current_index]
        dependents = [s.get("id") for s in self.scenes if s.get("continue_from") == scene.get("id")]
        if dependents and not messagebox.askyesno(
            "確認刪除",
            "%s 被 %s 接續，刪掉後那些鏡頭的接續會一併移除。要繼續嗎？"
            % (scene.get("id"), "、".join(dependents)),
        ):
            return

        for other in self.scenes:
            if other.get("continue_from") == scene.get("id"):
                other.pop("continue_from", None)
        del self.scenes[self.current_index]
        self.current_index = None
        self._refresh_options()
        self._refresh_table()

    def _move(self, delta: int) -> None:
        if self.current_index is None:
            return
        target = self.current_index + delta
        if not 0 <= target < len(self.scenes):
            return
        self._flush_editor()
        self.scenes[self.current_index], self.scenes[target] = (
            self.scenes[target], self.scenes[self.current_index])
        self.current_index = target
        self._refresh_table()
        self.tree.selection_set(str(target))

    # --- 執行 ---------------------------------------------------------

    def _run(self) -> None:
        if not self.project or (self.worker and self.worker.is_alive()):
            return
        self._flush_editor()
        if not self._save():
            self._log("專案未能重新載入，已中止轉出（沒有送出任何請求）。")
            return

        targets = self._target_ids()
        if not targets:
            messagebox.showinfo(
                "沒有待生成的鏡頭",
                "勾選的分鏡都已完成。" if self.picked_ids else "全部分鏡都已完成。",
            )
            return

        # 勾選時可能漏掉被接續的前一鏡，先問要不要補進來
        missing = self._missing_dependencies(targets) if self.picked_ids else []
        if missing:
            if not messagebox.askyesno(
                "需要一併轉出",
                "勾選的分鏡要接續 %s，但它還沒完成。\n\n要一併轉出嗎？（不加的話無法生成）"
                % "、".join(missing),
            ):
                self._log("已取消（未送出，不計費）。")
                return
            self.picked_ids.update(missing)
            self._refresh_table()
            targets = self._target_ids()

        only = targets if self.picked_ids else None
        try:
            plan = plan_project(self.project, self.state, only=only)
        except SeedanceError as exc:
            messagebox.showerror("專案檢查未通過", str(exc))
            return

        if not plan.todo:
            messagebox.showinfo("沒有待生成的鏡頭", "全部分鏡都已完成。")
            return

        try:
            api_key = get_api_key()
        except SeedanceError as exc:
            messagebox.showerror("缺少金鑰", str(exc))
            return

        if not messagebox.askyesno(
            "確認費用",
            "本次要生成 %d 鏡（%s）%s。\n\n%s\n\n"
            "送出後即計費且無法取消，要開始嗎？"
            % (len(plan.todo), "、".join(plan.todo),
               "，其餘分鏡不會動到" if self.picked_ids else "",
               ("牌價 US$%.3f，依實測折扣預期約 US$%.3f"
                % (plan.todo_cost, plan.todo_expected))
               if abs(plan.todo_expected - plan.todo_cost) > 1e-9
               else ("預估 US$%.3f" % plan.todo_cost)),
        ):
            self._log("已取消（未送出，不計費）。")
            return

        self.cancel_event.clear()
        self.run_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.progress.start(12)
        self._log("—" * 28)

        project, state = self.project, self.state

        def work() -> None:
            try:
                result = run_project(
                    project, state,
                    api_key=api_key,
                    concurrency=3,
                    approved=True,          # 上面已確認
                    only=only,
                    log=lambda m: self.messages.put(("log", m)),
                    should_cancel=self.cancel_event.is_set,
                )
                self.messages.put(("done", result))
            except SeedanceError as exc:
                self.messages.put(("error", str(exc)))
            except Exception as exc:
                self.messages.put(("error", "未預期的錯誤：%r" % exc))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _stop(self) -> None:
        self.cancel_event.set()
        self._log("已要求停止：不再送出新的鏡頭，在途的會等它完成（已經計費）。")

    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self.messages.get_nowait()
                if kind == "log":
                    self._log(str(payload))
                elif kind == "project_caps":
                    self.project_caps = payload
                    self._refresh_options()
                    self._refresh_table()
                    self._log("專案模型：%s" % getattr(payload, "id", payload))
                elif kind == "done":
                    self._finish(payload)
                elif kind == "error":
                    self._finish(None, error=str(payload))
        except queue.Empty:
            pass
        self.after(150, self._drain)

    def _finish(self, result, error: str | None = None) -> None:
        self.progress.stop()
        self.run_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        if self.project:
            self.state = ProjectState.load(self.project)
            self._refresh_table()

        if error:
            self._log("錯誤：%s" % error)
            messagebox.showerror("轉出失敗", error)
            return
        if result is None:
            return

        if result.failed:
            scene_id, message = result.failed[0]
            messagebox.showwarning(
                "已停止",
                "%s 失敗，已依設定立即停止。\n\n%s\n\n未開始：%s\n\n"
                "修正後再按開始，已完成的鏡頭不會重跑，也不會重複計費。"
                % (scene_id, message, "、".join(result.not_started) or "無"),
            )
        elif result.concat_path:
            messagebox.showinfo("完成", "全部鏡頭完成，已合併：\n%s" % result.concat_path)
        else:
            messagebox.showinfo("完成", "本次完成 %d 鏡。" % len(result.completed))

    def _log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")


class CastDialog(tk.Toplevel):
    """角色表編輯：名稱 ↔ 圖片。

    角色表是整個專案共用的，各鏡只引用名字。所以這裡改一次圖，所有用到該角色的
    分鏡都會跟著換——這正是 cast 存在的理由，不必逐鏡重貼路徑。
    """

    def __init__(self, parent: ProjectTab, project):
        super().__init__(parent)
        self.parent_tab = parent
        self.project = project
        self.entries: list[list[str]] = [[name, path] for name, path in project.cast.items()]
        self.renames: dict[str, str] = {}

        self.title("編輯角色表")
        self.geometry("560x360")
        self.transient(parent.winfo_toplevel())
        self.grab_set()

        self._build()
        self._refresh()

    def _build(self) -> None:
        frame = ttk.Frame(self, padding=10)
        frame.pack(fill="both", expand=True)
        frame.rowconfigure(1, weight=1)
        frame.columnconfigure(0, weight=1)

        ttk.Label(
            frame,
            text="各分鏡以名稱引用角色。換圖只要改這裡，所有用到的分鏡都會跟著換。",
            foreground="#666",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))

        self.tree = ttk.Treeview(frame, columns=("name", "path"), show="headings", selectmode="browse")
        self.tree.heading("name", text="角色名")
        self.tree.heading("path", text="圖片")
        self.tree.column("name", width=120)
        self.tree.column("path", width=380)
        self.tree.grid(row=1, column=0, sticky="nsew")

        scroll = ttk.Scrollbar(frame, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.grid(row=1, column=1, sticky="ns")

        buttons = ttk.Frame(frame)
        buttons.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        for text, command in (
            ("新增角色…", self._add), ("換圖…", self._replace),
            ("重新命名", self._rename), ("移除", self._remove),
        ):
            ttk.Button(buttons, text=text, width=11, command=command).pack(side="left", padx=2)

        confirm = ttk.Frame(frame)
        confirm.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Button(confirm, text="確定", width=10, command=self._confirm).pack(side="right")
        ttk.Button(confirm, text="取消", width=10, command=self.destroy).pack(side="right", padx=6)

    def _refresh(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for index, (name, path) in enumerate(self.entries):
            self.tree.insert("", "end", iid=str(index), values=(name, path))

    def _selected(self) -> int | None:
        selection = self.tree.selection()
        return int(selection[0]) if selection else None

    def _add(self) -> None:
        paths = filedialog.askopenfilenames(title="選擇角色圖", filetypes=IMAGE_TYPES)
        taken = {name for name, _ in self.entries}
        for path in paths:
            relative = self.parent_tab._relative_to_project(path)
            name = Path(path).stem
            suffix = 2
            while name in taken:            # 同名會蓋掉別人，自動加序號避開
                name = "%s%d" % (Path(path).stem, suffix)
                suffix += 1
            taken.add(name)
            self.entries.append([name, relative])
        self._refresh()

    def _replace(self) -> None:
        index = self._selected()
        if index is None:
            return
        path = filedialog.askopenfilename(title="選擇新的角色圖", filetypes=IMAGE_TYPES)
        if path:
            self.entries[index][1] = self.parent_tab._relative_to_project(path)
            self._refresh()

    def _rename(self) -> None:
        index = self._selected()
        if index is None:
            return
        old = self.entries[index][0]
        new = simpledialog.askstring("重新命名", "新的角色名：", initialvalue=old, parent=self)
        if not new or new == old:
            return
        if any(name == new for i, (name, _) in enumerate(self.entries) if i != index):
            messagebox.showwarning("名稱重複", "已經有叫「%s」的角色了。" % new, parent=self)
            return
        self.entries[index][0] = new
        # 記錄原始名字 → 新名字，確定時才用它搬移各鏡的引用
        original = next((o for o, c in self.renames.items() if c == old), old)
        self.renames[original] = new
        self._refresh()

    def _remove(self) -> None:
        index = self._selected()
        if index is None:
            return
        name = self.entries[index][0]
        users = [s.get("id") for s in self.parent_tab.scenes if name in (s.get("cast") or [])]
        if users and not messagebox.askyesno(
            "確認移除",
            "「%s」正被 %s 使用，移除後那些分鏡會少掉這個角色。要繼續嗎？"
            % (name, "、".join(users)),
            parent=self,
        ):
            return
        del self.entries[index]
        self._refresh()

    def _confirm(self) -> None:
        missing = [name for name, path in self.entries
                   if not is_url(path) and not (self.project.root / path).is_file()
                   and not Path(path).is_file()]
        if missing and not messagebox.askyesno(
            "找不到圖片",
            "這些角色的圖片找不到：%s\n\n仍要儲存嗎？（轉出前的檢查會再擋一次）" % "、".join(missing),
            parent=self,
        ):
            return

        cast = {name: path for name, path in self.entries}
        self.project.cast = cast
        self.project.raw["cast"] = cast     # 存檔時是從 raw 寫回去的
        self.parent_tab._on_cast_changed(self.renames)
        self.destroy()


def _nearest(value: int, options) -> int:
    """挑最接近的合法秒數。換模型時把不支援的秒數自動貼過去，比直接報錯好用。"""
    return min(options, key=lambda o: (abs(o - value), o))
