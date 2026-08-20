"""Tkinter 圖形介面。

用標準庫的 tkinter 而非 gradio，是為了不必額外安裝任何套件、雙擊就能跑。
所有實際工作都委派給 runner，這個檔案只負責畫面與執行緒。

生成在背景執行緒跑，訊息丟進 queue，由主執行緒的 after 迴圈取出更新畫面——
tkinter 的元件只能在主執行緒碰。
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import cost as cost_module
from .capabilities import ModelCapabilities, get_capabilities
from .client import GenerationSpec
from .config import DEFAULT_MODEL, OUTPUT_DIR, api_key_source, cost_limit_usd, get_api_key
from .errors import SeedanceError
from .media import FILE_DIALOG_TYPES, describe, has_video_reference
from .runner import generate

IMAGE_DIALOG_TYPES = [("圖片", "*.png *.jpg *.jpeg *.webp *.gif *.bmp"), ("所有檔案", "*.*")]


class SeedanceApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("趣味動畫製作 — Seedance 影片生成")
        self.root.geometry("980x760")
        self.root.minsize(880, 680)

        self.caps: ModelCapabilities | None = None
        self.references: list[str] = []
        self.messages: queue.Queue[tuple[str, object]] = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.last_video: Path | None = None

        self._build_ui()
        self._drain_queue()
        self._load_capabilities_async()

    # --- 版面 ---------------------------------------------------------

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 8))
        ttk.Label(header, text="Seedance 影片生成", font=("Microsoft JhengHei UI", 14, "bold")).pack(side="left")
        self.status_var = tk.StringVar(value="載入模型能力中…")
        ttk.Label(header, textvariable=self.status_var, foreground="#666").pack(side="right")

        body = ttk.Frame(outer)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=3)
        body.columnconfigure(1, weight=2)
        body.rowconfigure(0, weight=1)

        self._build_left(body)
        self._build_right(body)
        self._build_bottom(outer)

    def _build_left(self, parent: ttk.Frame) -> None:
        left = ttk.Frame(parent)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        left.rowconfigure(1, weight=2)
        left.rowconfigure(4, weight=1)
        left.columnconfigure(0, weight=1)

        ttk.Label(left, text="影片提示詞（prompt）").grid(row=0, column=0, sticky="w")
        self.prompt_text = tk.Text(left, height=9, wrap="word", font=("Microsoft JhengHei UI", 10))
        self.prompt_text.grid(row=1, column=0, sticky="nsew", pady=(4, 10))
        self.prompt_text.insert(
            "1.0",
            "描述畫面、鏡頭運動與光線，例如：\n夕陽下的玻璃溫室，霧氣繚繞，鏡頭緩慢推軌向前",
        )
        self.prompt_text.bind("<FocusIn>", self._clear_placeholder_once)
        self._placeholder_cleared = False

        ref_header = ttk.Frame(left)
        ref_header.grid(row=2, column=0, sticky="ew")
        ttk.Label(ref_header, text="參考素材（圖片／影片／音訊，可多選）").pack(side="left")
        ttk.Button(ref_header, text="新增…", width=8, command=self._add_references).pack(side="right")
        ttk.Button(ref_header, text="移除", width=8, command=self._remove_reference).pack(side="right", padx=4)

        hint = "圖片可做角色／風格參考；影片與音訊只有 Seedance 2 代以上會採用"
        ttk.Label(left, text=hint, foreground="#888", font=("Microsoft JhengHei UI", 8)).grid(
            row=3, column=0, sticky="w", pady=(2, 4)
        )

        self.ref_list = tk.Listbox(left, height=5, activestyle="none")
        self.ref_list.grid(row=4, column=0, sticky="nsew")

    def _build_right(self, parent: ttk.Frame) -> None:
        right = ttk.LabelFrame(parent, text="輸出設定", padding=10)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(1, weight=1)

        row = 0
        ttk.Label(right, text="長寬（像素）").grid(row=row, column=0, sticky="w", pady=4)
        self.size_var = tk.StringVar()
        self.size_combo = ttk.Combobox(right, textvariable=self.size_var, state="readonly", width=18)
        self.size_combo.grid(row=row, column=1, sticky="ew", pady=4)
        self.size_combo.bind("<<ComboboxSelected>>", lambda _e: self._update_estimate())

        row += 1
        ttk.Label(right, text="長度（秒）").grid(row=row, column=0, sticky="w", pady=4)
        self.duration_var = tk.StringVar()
        self.duration_combo = ttk.Combobox(right, textvariable=self.duration_var, state="readonly", width=18)
        self.duration_combo.grid(row=row, column=1, sticky="ew", pady=4)
        self.duration_combo.bind("<<ComboboxSelected>>", lambda _e: self._update_estimate())

        row += 1
        self.audio_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            right, text="同時生成音訊（會增加費用）", variable=self.audio_var, command=self._update_estimate
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=4)

        row += 1
        ttk.Label(right, text="seed（選填）").grid(row=row, column=0, sticky="w", pady=4)
        self.seed_var = tk.StringVar()
        ttk.Entry(right, textvariable=self.seed_var, width=20).grid(row=row, column=1, sticky="ew", pady=4)

        row += 1
        ttk.Separator(right).grid(row=row, column=0, columnspan=2, sticky="ew", pady=8)

        row += 1
        ttk.Label(right, text="首影格（選填）").grid(row=row, column=0, sticky="w", pady=4)
        self.first_frame_var = tk.StringVar()
        self._file_picker(right, row, self.first_frame_var)

        row += 1
        ttk.Label(right, text="尾影格（選填）").grid(row=row, column=0, sticky="w", pady=4)
        self.last_frame_var = tk.StringVar()
        self._file_picker(right, row, self.last_frame_var)

        row += 1
        ttk.Separator(right).grid(row=row, column=0, columnspan=2, sticky="ew", pady=8)

        row += 1
        self.estimate_var = tk.StringVar(value="—")
        ttk.Label(right, textvariable=self.estimate_var, wraplength=280, foreground="#0a6").grid(
            row=row, column=0, columnspan=2, sticky="w"
        )

        row += 1
        ttk.Label(
            right,
            text="估價為牌價上限，未計促銷折扣；\n實際扣款以任務回報的 usage.cost 為準。",
            foreground="#888",
            font=("Microsoft JhengHei UI", 8),
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(4, 0))

    def _file_picker(self, parent: ttk.Frame, row: int, var: tk.StringVar) -> None:
        frame = ttk.Frame(parent)
        frame.grid(row=row, column=1, sticky="ew", pady=4)
        frame.columnconfigure(0, weight=1)
        entry = ttk.Entry(frame, textvariable=var)
        entry.grid(row=0, column=0, sticky="ew")

        def choose() -> None:
            path = filedialog.askopenfilename(title="選擇圖片", filetypes=IMAGE_DIALOG_TYPES)
            if path:
                var.set(path)

        ttk.Button(frame, text="…", width=3, command=choose).grid(row=0, column=1, padx=(4, 0))

    def _build_bottom(self, parent: ttk.Frame) -> None:
        controls = ttk.Frame(parent)
        controls.pack(fill="x", pady=(10, 6))

        self.generate_button = ttk.Button(controls, text="生成影片", command=self._on_generate, state="disabled")
        self.generate_button.pack(side="left")
        self.stop_button = ttk.Button(controls, text="停止等待", command=self._on_stop, state="disabled")
        self.stop_button.pack(side="left", padx=6)

        self.play_button = ttk.Button(controls, text="播放", command=self._play_last, state="disabled")
        self.play_button.pack(side="right")
        ttk.Button(controls, text="開啟輸出資料夾", command=self._open_output_dir).pack(side="right", padx=6)

        self.progress = ttk.Progressbar(parent, mode="indeterminate")
        self.progress.pack(fill="x", pady=(0, 6))

        log_frame = ttk.LabelFrame(parent, text="執行紀錄", padding=6)
        log_frame.pack(fill="both", expand=True)
        self.log_text = tk.Text(log_frame, height=9, wrap="word", state="disabled", font=("Consolas", 9))
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    # --- 能力載入 -----------------------------------------------------

    def _load_capabilities_async(self) -> None:
        def work() -> None:
            try:
                caps = get_capabilities(DEFAULT_MODEL)
                self.messages.put(("caps", caps))
            except SeedanceError as exc:
                self.messages.put(("caps_error", str(exc)))

        threading.Thread(target=work, daemon=True).start()

    def _apply_capabilities(self, caps: ModelCapabilities) -> None:
        self.caps = caps
        sizes = caps.sorted_sizes()
        self.size_combo["values"] = sizes
        self.size_var.set(caps.default_size())

        durations = [str(d) for d in caps.supported_durations]
        self.duration_combo["values"] = durations
        self.duration_var.set(str(caps.default_duration()))

        if not caps.generate_audio:
            self.audio_var.set(False)

        source = api_key_source()
        self.status_var.set("%s ｜ 金鑰來源：%s" % (caps.name or caps.id, source))
        self.generate_button.configure(state="normal")
        self._log("模型能力載入完成：%s" % caps.id)
        self._log("預設 %s（最低解析度手機直式）、%s 秒" % (self.size_var.get(), self.duration_var.get()))
        if source == "未設定":
            self._log("警告：找不到 OPENROUTER_API_KEY，請在專案根目錄的 .env 填入後重開。")
        self._update_estimate()

    # --- 事件 ---------------------------------------------------------

    def _clear_placeholder_once(self, _event=None) -> None:
        if not self._placeholder_cleared:
            self.prompt_text.delete("1.0", "end")
            self._placeholder_cleared = True

    def _add_references(self) -> None:
        paths = filedialog.askopenfilenames(title="選擇參考素材", filetypes=FILE_DIALOG_TYPES)
        for path in paths:
            if path not in self.references:
                self.references.append(path)
                self.ref_list.insert("end", describe(path))
        self._update_estimate()

    def _remove_reference(self) -> None:
        for index in reversed(self.ref_list.curselection()):
            self.ref_list.delete(index)
            del self.references[index]
        self._update_estimate()

    def _update_estimate(self) -> None:
        if not self.caps or not self.size_var.get() or not self.duration_var.get():
            return
        try:
            estimate = cost_module.estimate(
                self.caps,
                size=self.size_var.get(),
                duration=int(self.duration_var.get()),
                generate_audio=self.audio_var.get(),
                has_video_input=has_video_reference(self.references),
            )
        except SeedanceError as exc:
            self.estimate_var.set(str(exc))
            return
        self.estimate_var.set(estimate.format())

    def _on_generate(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        if not self.caps:
            return

        self._clear_placeholder_once()
        prompt = self.prompt_text.get("1.0", "end").strip()
        if not prompt and not (self.first_frame_var.get() or self.references):
            messagebox.showwarning("缺少輸入", "請輸入提示詞，或至少提供一張首影格／參考素材。")
            return

        seed_raw = self.seed_var.get().strip()
        if seed_raw and not seed_raw.lstrip("-").isdigit():
            messagebox.showwarning("seed 格式錯誤", "seed 必須是整數，或留空。")
            return

        spec = GenerationSpec(
            prompt=prompt,
            model=self.caps.id,
            duration=int(self.duration_var.get()),
            size=self.size_var.get(),
            generate_audio=self.audio_var.get(),
            seed=int(seed_raw) if seed_raw else None,
            references=list(self.references),
            first_frame=self.first_frame_var.get().strip() or None,
            last_frame=self.last_frame_var.get().strip() or None,
        )

        try:
            api_key = get_api_key()
        except SeedanceError as exc:
            messagebox.showerror("缺少金鑰", str(exc))
            return

        estimate = cost_module.estimate(
            self.caps,
            size=spec.size,
            duration=spec.duration,
            generate_audio=spec.generate_audio,
            has_video_input=has_video_reference(spec.references),
        )
        limit = cost_limit_usd()
        if estimate.usd > limit:
            confirmed = messagebox.askyesno(
                "確認費用",
                "這次生成預估 US$%.3f，超過護欄門檻 US$%.2f。\n\n%s\n\n仍要送出嗎？"
                % (estimate.usd, limit, estimate.format()),
            )
            if not confirmed:
                self._log("已取消（未送出，不計費）。")
                return

        self.cancel_event.clear()
        self.generate_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.play_button.configure(state="disabled")
        self.progress.start(12)
        self._log("—" * 30)

        def work() -> None:
            try:
                result = generate(
                    spec,
                    api_key=api_key,
                    caps=self.caps,
                    approved=True,  # 上面已確認過
                    log=lambda message: self.messages.put(("log", message)),
                    should_cancel=self.cancel_event.is_set,
                )
                self.messages.put(("done", result.video_path))
            except SeedanceError as exc:
                self.messages.put(("error", str(exc)))
            except Exception as exc:  # 未預期錯誤也要讓使用者看到，而不是靜靜卡住
                self.messages.put(("error", "未預期的錯誤：%r" % exc))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _on_stop(self) -> None:
        self.cancel_event.set()
        self._log("已要求停止等待（任務仍在雲端執行，可用 CLI 的 resume 取件）。")

    def _play_last(self) -> None:
        if self.last_video and self.last_video.is_file():
            _open_path(self.last_video)

    def _open_output_dir(self) -> None:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        _open_path(OUTPUT_DIR)

    # --- 訊息迴圈 -----------------------------------------------------

    def _drain_queue(self) -> None:
        try:
            while True:
                kind, payload = self.messages.get_nowait()
                if kind == "log":
                    self._log(str(payload))
                elif kind == "caps":
                    self._apply_capabilities(payload)  # type: ignore[arg-type]
                elif kind == "caps_error":
                    self.status_var.set("模型能力載入失敗")
                    self._log("錯誤：%s" % payload)
                    messagebox.showerror("載入失敗", str(payload))
                elif kind == "done":
                    self._finish(Path(str(payload)))
                elif kind == "error":
                    self._finish(None, error=str(payload))
        except queue.Empty:
            pass
        self.root.after(120, self._drain_queue)

    def _finish(self, video_path: Path | None, error: str | None = None) -> None:
        self.progress.stop()
        self.generate_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        if error:
            self._log("錯誤：%s" % error)
            messagebox.showerror("生成失敗", error)
            return
        if video_path:
            self.last_video = video_path
            self.play_button.configure(state="normal")
            self._log("影片已存到：%s" % video_path)

    def _log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")


def _open_path(path: Path) -> None:
    """用系統預設程式開啟檔案或資料夾。"""
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # noqa: S606  # Windows 專用
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
    except OSError as exc:
        messagebox.showerror("開啟失敗", str(exc))


def main() -> None:
    root = tk.Tk()
    try:
        # Windows 高 DPI 下字會糊，這行讓畫面銳利；失敗就算了。
        from ctypes import windll

        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    SeedanceApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
