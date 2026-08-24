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
from .capabilities import ModelCapabilities
from .config import get_api_key
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

        columns = ("status", "id", "dur", "cast", "chain", "cost", "prompt")
        self.tree = ttk.Treeview(left, columns=columns, show="headings", selectmode="browse")
        headings = {
            "status": ("狀態", 70), "id": ("編號", 55), "dur": ("秒", 35),
            "cast": ("角色", 90), "chain": ("接續", 55), "cost": ("估價", 60),
            "prompt": ("提示詞", 240),
        }
        for key, (label, width) in headings.items():
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, anchor="w")
        self.tree.grid(row=0, column=0, sticky="nsew")

        scroll = ttk.Scrollbar(left, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        buttons = ttk.Frame(left)
        buttons.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        for text, command in (
            ("新增分鏡", self._add_scene), ("刪除", self._delete_scene),
            ("上移", lambda: self._move(-1)), ("下移", lambda: self._move(1)),
        ):
            ttk.Button(buttons, text=text, width=9, command=command).pack(side="left", padx=2)

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
        ttk.Label(editor, text="長寬（像素）").grid(row=row, column=0, sticky="w", pady=3)
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
        ttk.Checkbutton(editor, text="生成音訊", variable=self.audio_var).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=3)

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

    def _refresh_options(self) -> None:
        caps: ModelCapabilities | None = self.caps_provider()
        if caps:
            self.duration_combo["values"] = [str(d) for d in caps.supported_durations]
            self.size_combo["values"] = caps.sorted_sizes()

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
        total = 0.0
        todo = 0
        for index, scene in enumerate(self.scenes):
            scene_id = scene.get("id", "s%02d" % (index + 1))
            status = self.state.status_of(scene_id) if self.state else "pending"
            if self.state and self.state.is_done(scene_id):
                status = "done"
            else:
                todo += 1
                total += estimates.get(scene_id, 0.0)

            self.tree.insert("", "end", iid=str(index), values=(
                STATUS_MARK.get(status, status),
                scene_id,
                scene.get("duration", self.project.defaults.get("duration", "")),
                "、".join(scene.get("cast") or []),
                scene.get("continue_from") or "",
                "US$%.3f" % estimates.get(scene_id, 0.0) if scene_id in estimates else "—",
                (scene.get("prompt") or "")[:60],
            ))

        spent = self.state.total_cost() if self.state else 0.0
        self.summary_var.set(
            "待生成 %d 鏡，預估上限 US$%.3f（實際約 US$%.3f）｜已花費 US$%.4f"
            % (todo, total, total * 0.416, spent)
        )

        # 重建表格會清掉選取，補回來並保持與 current_index 一致；不一致的話
        # 佇列裡的 <<TreeviewSelect>> 會被當成「使用者換了一列」而互相打架。
        if self.current_index is not None and 0 <= self.current_index < len(self.scenes):
            self.tree.selection_set(str(self.current_index))

    def _estimates(self) -> dict[str, float]:
        """逐鏡估價。

        刻意直接從編輯中的 self.scenes 算，而不是從已載入的 Project 物件——
        使用者改了秒數就該立刻看到價格變動，不必先存檔重載。也因此這裡要容錯：
        編輯到一半的分鏡不該讓整個表格畫不出來。
        """
        if not self.project:
            return {}
        caps = self.caps_provider()
        if not caps:
            return {}

        results = {}
        for index, scene in enumerate(self.scenes):
            scene_id = scene.get("id", "s%02d" % (index + 1))
            merged = {**self.project.defaults, **scene}
            try:
                results[scene_id] = cost_module.estimate(
                    caps,
                    size=merged.get("size") or caps.default_size(),
                    duration=int(merged.get("duration") or caps.default_duration()),
                    generate_audio=bool(merged.get("generate_audio")),
                ).usd
            except (SeedanceError, ValueError, TypeError):
                continue
        return results

    # --- 編輯 ---------------------------------------------------------

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
        self.size_var.set(scene.get("size") or defaults.get("size", ""))
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
        scene["id"] = self.id_var.get().strip() or scene.get("id", "")
        scene["prompt"] = self.prompt_text.get("1.0", "end").strip()

        if self.duration_var.get().isdigit():
            scene["duration"] = int(self.duration_var.get())
        if self.size_var.get():
            scene["size"] = self.size_var.get()
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

        try:
            plan = plan_project(self.project, self.state)
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
            "本次要生成 %d 鏡（%s）。\n\n預估上限 US$%.3f，依目前折扣實際約 US$%.3f。\n\n"
            "送出後即計費且無法取消，要開始嗎？"
            % (len(plan.todo), "、".join(plan.todo), plan.todo_cost, plan.todo_cost * 0.416),
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
