"""流程編排：驗參數 → 估價 → 送單 → 輪詢 → 下載 → 寫記錄。

CLI 與 GUI 都呼叫這裡，兩個介面才不會各自長出一套略有差異的邏輯。
所有進度訊息都走 log callback，呼叫端自己決定要 print 還是塞進 GUI 的文字框。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from . import cost as cost_module
from .capabilities import ModelCapabilities, get_capabilities
from .client import GenerationSpec, SeedanceClient, build_request, load_job_record, redact, save_job_record
from .config import DEFAULT_MODEL, OUTPUT_DIR, cost_limit_usd, ensure_dirs
from .errors import SeedanceError, ValidationError
from .media import has_video_reference

Logger = Callable[[str], None]


@dataclass
class GenerationResult:
    spec: GenerationSpec
    job: dict
    video_path: Path
    record_path: Path
    estimate: cost_module.CostEstimate
    elapsed_s: float

    @property
    def actual_cost(self):
        usage = self.job.get("usage") or {}
        return usage.get("cost")

    def summary(self) -> str:
        actual = self.actual_cost
        billed = ("實際 US$%.4f" % actual) if isinstance(actual, (int, float)) else "實際費用未回報"
        return "%s（耗時 %.0f 秒，%s）" % (self.video_path.name, self.elapsed_s, billed)


def prepare(
    spec: GenerationSpec,
    *,
    caps: ModelCapabilities | None = None,
    log: Logger | None = None,
    build_body: bool = True,
) -> tuple[ModelCapabilities, dict, cost_module.CostEstimate]:
    """送單前的所有本地檢查，集中在這裡；任何一步失敗都不會花到錢。

    build_body=False 只做驗證與估價，跳過素材編碼——批次模式的預估階段用得到，
    免得每張參考圖被 base64 兩次。
    """
    caps = caps or get_capabilities(spec.model)

    if not spec.prompt and not (spec.first_frame or spec.references):
        raise ValidationError("請輸入 prompt，或至少提供一張首影格／參考素材。")

    if not spec.duration:
        spec.duration = caps.default_duration()
    if not (spec.size or spec.resolution or spec.aspect_ratio):
        # 明確帶上尺寸，估價與實際輸出才會一致，不會落到供應商的預設值。
        spec.size = caps.default_size()

    caps.validate(
        duration=spec.duration,
        size=spec.size,
        resolution=spec.resolution,
        aspect_ratio=spec.aspect_ratio,
        generate_audio=spec.generate_audio,
        seed=spec.seed,
        frame_types=spec.frame_types(),
    )

    size = spec.size or _size_from(caps, spec.resolution, spec.aspect_ratio)
    estimate = cost_module.estimate(
        caps,
        size=size,
        duration=spec.duration,
        generate_audio=spec.generate_audio,
        has_video_input=has_video_reference(spec.references),
    )
    body = build_request(spec, log=log) if build_body else {}
    return caps, body, estimate


def generate(
    spec: GenerationSpec,
    *,
    api_key: str,
    caps: ModelCapabilities | None = None,
    approved: bool = False,
    log: Logger = print,
    output_dir: Path | None = None,
    should_cancel: Callable[[], bool] | None = None,
    timeout_s: int | None = None,
) -> GenerationResult:
    """生成單支影片，回傳結果。呼叫這個函式會實際扣款。"""
    ensure_dirs()
    output_dir = Path(output_dir or OUTPUT_DIR)
    started = time.monotonic()

    caps, body, estimate = prepare(spec, caps=caps, log=log)
    log(estimate.format())
    cost_module.check_guard(estimate, cost_limit_usd(), approved=approved)

    client = SeedanceClient(api_key)
    log("送出任務…")
    job = client.submit(body)
    job_id = job.get("id")
    save_job_record(job, body, extra={"spec": _spec_to_dict(spec), "estimate_usd": estimate.usd})
    log("任務已建立：%s（已計費，中斷後可用 resume %s 取件）" % (job_id, job_id))

    def on_update(status: dict, attempt: int) -> None:
        log("  第 %d 次輪詢：%s" % (attempt, status.get("status")))

    final = client.wait(
        job,
        on_update=on_update,
        should_cancel=should_cancel,
        timeout_s=timeout_s or 30 * 60,
    )
    save_job_record(final, extra={"usage": final.get("usage")})

    dest = output_dir / _filename(spec, job_id, estimate)
    log("下載中…")
    video_path = client.download(final, dest)

    record_path = _write_sidecar(video_path, spec, body, final, estimate)
    save_job_record(final, extra={"video_path": str(video_path)})

    result = GenerationResult(
        spec=spec,
        job=final,
        video_path=video_path,
        record_path=record_path,
        estimate=estimate,
        elapsed_s=time.monotonic() - started,
    )
    log("完成：%s" % result.summary())
    return result


def resume(
    job_id: str,
    *,
    api_key: str,
    log: Logger = print,
    output_dir: Path | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> Path:
    """憑 job id 把已經送出（也已計費）的任務取回來。"""
    ensure_dirs()
    output_dir = Path(output_dir or OUTPUT_DIR)
    record = load_job_record(job_id)
    client = SeedanceClient(api_key)

    log("查詢任務 %s…" % job_id)
    status = client.poll(record if record.get("polling_url") else job_id)

    if str(status.get("status")).lower() != "completed":
        status = client.wait(
            status,
            on_update=lambda s, i: log("  第 %d 次輪詢：%s" % (i, s.get("status"))),
            should_cancel=should_cancel,
        )

    spec_data = record.get("spec") or {}
    name = spec_data.get("name") or "resumed"
    dest = output_dir / ("%s_%s_%s.mp4" % (time.strftime("%Y%m%d-%H%M%S"), _slug(name), job_id[:8]))
    video_path = client.download(status, dest)
    save_job_record(status, extra={"video_path": str(video_path), "usage": status.get("usage")})
    log("完成：%s" % video_path.name)
    return video_path


# --- 批次 -------------------------------------------------------------


def load_scenes(path: Path, *, model: str = DEFAULT_MODEL) -> list[GenerationSpec]:
    """讀分鏡檔。支援 {defaults, scenes:[...]} 或直接一個 scene 陣列。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, list):
        defaults, scenes = {}, data
    else:
        defaults = data.get("defaults") or {}
        scenes = data.get("scenes") or []

    if not scenes:
        raise ValidationError("分鏡檔裡沒有任何 scene。")

    specs = []
    for index, scene in enumerate(scenes, start=1):
        merged = {**defaults, **scene}
        specs.append(
            GenerationSpec(
                prompt=merged.get("prompt", ""),
                model=merged.get("model", model),
                duration=int(merged.get("duration", 5)),
                size=merged.get("size"),
                resolution=merged.get("resolution"),
                aspect_ratio=merged.get("aspect_ratio"),
                generate_audio=bool(merged.get("generate_audio", False)),
                seed=merged.get("seed"),
                references=list(merged.get("references") or []),
                first_frame=merged.get("first_frame"),
                last_frame=merged.get("last_frame"),
                name=merged.get("name") or ("scene%02d" % index),
            )
        )
    return specs


def generate_batch(
    specs: Iterable[GenerationSpec],
    *,
    api_key: str,
    concurrency: int = 3,
    approved: bool = False,
    log: Logger = print,
    output_dir: Path | None = None,
) -> list[GenerationResult | Exception]:
    """並行跑多支。單支失敗不會拖垮其他支，錯誤原樣放在結果清單裡。"""
    specs = list(specs)
    caps_cache: dict[str, ModelCapabilities] = {}
    total = 0.0
    for spec in specs:
        caps = caps_cache.setdefault(spec.model, get_capabilities(spec.model))
        _, _, estimate = prepare(spec, caps=caps, build_body=False)
        total += estimate.usd
    log("共 %d 支，預估總計 US$%.3f（牌價上限）" % (len(specs), total))
    if not approved and total > cost_limit_usd():
        raise cost_module.CostGuardError(
            "批次預估 US$%.3f 超過門檻 US$%.2f，請加 --yes 確認。" % (total, cost_limit_usd())
        )

    results: list[GenerationResult | Exception] = [None] * len(specs)  # type: ignore[list-item]

    def run(index_spec):
        index, spec = index_spec
        prefix = "[%s]" % (spec.name or ("#%d" % (index + 1)))
        try:
            return index, generate(
                spec,
                api_key=api_key,
                caps=caps_cache.get(spec.model),
                approved=True,  # 總額已在上面一次確認過
                log=lambda message: log("%s %s" % (prefix, message)),
                output_dir=output_dir,
            )
        except SeedanceError as exc:
            log("%s 失敗：%s" % (prefix, exc))
            return index, exc

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        for index, outcome in pool.map(run, enumerate(specs)):
            results[index] = outcome
    return results


def concat_videos(paths: list[Path], dest: Path, *, log: Logger = print) -> Path | None:
    """用 ffmpeg 把多支片串成一支。沒裝 ffmpeg 就跳過，不影響已產出的分鏡。"""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        log("找不到 ffmpeg，略過合併。各分鏡影片仍在 outputs/。")
        return None
    if len(paths) < 2:
        log("少於兩支影片，不需要合併。")
        return None

    dest.parent.mkdir(parents=True, exist_ok=True)
    list_file = dest.with_suffix(".concat.txt")
    list_file.write_text(
        "\n".join("file '%s'" % p.resolve().as_posix() for p in paths),
        encoding="utf-8",
    )

    command = [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy", str(dest)]
    log("合併 %d 支影片…" % len(paths))
    process = subprocess.run(command, capture_output=True, text=True, errors="replace")

    if process.returncode != 0:
        # 直接 copy 失敗多半是編碼參數不一致，重新編碼再試一次。
        log("串流複製失敗，改為重新編碼…")
        command = [
            ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p", str(dest),
        ]
        process = subprocess.run(command, capture_output=True, text=True, errors="replace")

    list_file.unlink(missing_ok=True)
    if process.returncode != 0:
        log("ffmpeg 合併失敗：%s" % (process.stderr or "").strip()[-500:])
        return None
    log("已合併：%s" % dest)
    return dest


# --- 小工具 -----------------------------------------------------------


def _size_from(caps: ModelCapabilities, resolution: str | None, aspect_ratio: str | None) -> str:
    """沒給 size 時，從 resolution + aspect_ratio 推回實際像素，好估價。"""
    if resolution and aspect_ratio:
        try:
            ratio_w, ratio_h = (int(v) for v in aspect_ratio.split(":"))
        except ValueError:
            ratio_w, ratio_h = 16, 9
        for size in caps.supported_sizes:
            width, height = (int(v) for v in size.lower().split("x"))
            if abs((width / height) - (ratio_w / ratio_h)) < 0.02 and _matches_resolution(width, height, resolution):
                return size
    return caps.default_size()


def _matches_resolution(width: int, height: int, resolution: str) -> bool:
    target = re.sub(r"[^0-9]", "", resolution)
    return bool(target) and min(width, height) == int(target)


def _filename(spec: GenerationSpec, job_id: str, estimate: cost_module.CostEstimate) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    label = _slug(spec.name or spec.prompt or "video")
    return "%s_%s_%dx%d_%ds.mp4" % (stamp, label, estimate.width, estimate.height, spec.duration)


def _slug(text: str, limit: int = 32) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\s]+", "-", str(text)).strip("-")
    return (cleaned[:limit] or "video").rstrip("-")


def _spec_to_dict(spec: GenerationSpec) -> dict:
    return {
        "name": spec.name,
        "prompt": spec.prompt,
        "model": spec.model,
        "duration": spec.duration,
        "size": spec.size,
        "resolution": spec.resolution,
        "aspect_ratio": spec.aspect_ratio,
        "generate_audio": spec.generate_audio,
        "seed": spec.seed,
        "references": list(spec.references),
        "first_frame": spec.first_frame,
        "last_frame": spec.last_frame,
    }


def _write_sidecar(
    video_path: Path,
    spec: GenerationSpec,
    body: dict,
    job: dict,
    estimate: cost_module.CostEstimate,
) -> Path:
    """影片旁邊放一份 JSON：參數、seed、實際費用。重現與對帳都靠它。"""
    record = {
        "video": video_path.name,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "job_id": job.get("id"),
        "generation_id": job.get("generation_id"),
        "spec": _spec_to_dict(spec),
        "request": redact(body),
        "estimate": {"tokens": estimate.tokens, "usd": estimate.usd, "sku": estimate.sku},
        "usage": job.get("usage"),
    }
    path = video_path.with_suffix(".json")
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
