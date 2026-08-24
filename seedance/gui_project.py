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
from tkinter import filedialog, messagebox, ttk

from . import cost as cost_module
from .capabilities import ModelCapabilities
from .config import get_api_key
from .errors import SeedanceError
from .project import ProjectState, load_project, write_template
from .project_runner import plan_project, run_project

STATUS_MARK = {"done": "✓ 完成", "failed": "✗ 失敗", "running": "… 進行中", "pending": "待生成"}


class ProjectTab(ttk.Frame):
    def __init__(self, parent, caps_provider):
        super().__init__(parent, padding=10)
        self.caps_provider = caps_provider          # 回傳 ModelCapabilities 或 None
        self.project = None
        self.state: ProjectState | None = None
        self.scenes: list[dict] = []                # 原始 dict，存檔時直接寫回
        self.current_index: int | None = None
        self.cast_vars: dict[str, tk.BooleanVar] = {}
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
        self.prompt_text = tk.Text(editor, height=7, wrap="word", font=("Microsoft JhengHei UI", 10))
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

        row += 1
        self.audio_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(editor, text="生成音訊", variable=self.audio_var).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=3)

        row += 1
        ttk.Label(editor, text="出場角色").grid(row=row, column=0, sticky="nw", pady=3)
        self.cast_frame = ttk.Frame(editor)
        self.cast_frame.grid(row=row, column=1, sticky="ew", pady=3)

        row += 1
        ttk.Button(editor, text="套用到此列", command=self._apply_editor).grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=(10, 0))

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

    def _open(self, path: str | None = None) -> None:
        path = path or filedialog.askopenfilename(
            title="開啟專案檔", filetypes=[("專案 JSON", "*.json"), ("所有檔案", "*.*")]
        )
        if not path:
            return
        try:
            self.project = load_project(path)
        except SeedanceError as exc:
            messagebox.showerror("專案檔有問題", str(exc))
            return

        self.state = ProjectState.load(self.project)
        self.scenes = [dict(s) for s in (self.project.raw.get("scenes") or [])]
        self.current_index = None
        self.path_var.set(str(self.project.path))
        self._build_cast_checkboxes()
        self._refresh_options()
        self._refresh_table()
        self.run_button.configure(state="normal")
        self._log("已開啟 %s（%d 鏡）" % (self.project.path.name, len(self.scenes)))

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

    def _save(self) -> None:
        if not self.project:
            return
        self._apply_editor(silent=True)
        data = dict(self.project.raw)
        data["scenes"] = self.scenes
        self.project.path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self._log("已儲存 %s" % self.project.path.name)
        self._open(str(self.project.path))   # 重新載入以套用驗證與估價

    # --- 表格 ---------------------------------------------------------

    def _refresh_options(self) -> None:
        caps: ModelCapabilities | None = self.caps_provider()
        if caps:
            self.duration_combo["values"] = [str(d) for d in caps.supported_durations]
            self.size_combo["values"] = caps.sorted_sizes()
        self.chain_combo["values"] = ["（不接續）"] + [s.get("id", "") for s in self.scenes]

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
        selection = self.tree.selection()
        if not selection:
            return
        index = int(selection[0])
        if index == self.current_index:
            return
        self._apply_editor(silent=True)     # 先把上一列的編輯寫回去
        self.current_index = index
        self._load_editor(self.scenes[index])

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

    def _apply_editor(self, silent: bool = False) -> None:
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

        if not silent:
            self._log("已套用到 %s" % scene["id"])
        self._refresh_options()
        self._refresh_table()
        self.tree.selection_set(str(self.current_index))

    def _add_scene(self) -> None:
        if not self.project:
            return
        self._apply_editor(silent=True)
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
        self._apply_editor(silent=True)
        self.scenes[self.current_index], self.scenes[target] = (
            self.scenes[target], self.scenes[self.current_index])
        self.current_index = target
        self._refresh_table()
        self.tree.selection_set(str(target))

    # --- 執行 ---------------------------------------------------------

    def _run(self) -> None:
        if not self.project or (self.worker and self.worker.is_alive()):
            return
        self._apply_editor(silent=True)
        self._save()
        if not self.project:
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
