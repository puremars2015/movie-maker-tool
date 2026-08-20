"""OpenRouter 影片生成 API 客戶端。

流程是三段式的非同步任務：
    POST /videos                      → 202 {id, polling_url, status}
    GET  {polling_url}                → 輪詢到 completed
    GET  /videos/{id}/content?index=0 → 下載 MP4

送單成功後會立刻把任務記錄寫進 jobs/，因為任務一旦建立就在雲端跑、也已經計費；
程式中斷不該讓那支影片消失。之後可用 resume 憑 job id 回頭取件。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin

import requests

from .config import API_BASE, JOBS_DIR, SITE_URL
from .errors import ApiError, JobFailedError, JobTimeoutError

TERMINAL_FAILURES = {"failed", "cancelled", "expired"}
POLL_INTERVAL_START = 10
POLL_INTERVAL_MAX = 30
DEFAULT_JOB_TIMEOUT = 30 * 60


@dataclass
class GenerationSpec:
    """一支影片的完整生成參數，CLI 與 GUI 都組這個物件。"""

    prompt: str
    model: str
    duration: int
    size: str | None = None
    resolution: str | None = None
    aspect_ratio: str | None = None
    generate_audio: bool = False
    seed: int | None = None
    references: list[str] = field(default_factory=list)
    first_frame: str | None = None
    last_frame: str | None = None
    name: str = ""

    def frame_types(self) -> list[str]:
        types = []
        if self.first_frame:
            types.append("first_frame")
        if self.last_frame:
            types.append("last_frame")
        return types


def build_request(spec: GenerationSpec, log=None) -> dict:
    """把 GenerationSpec 轉成 API 請求體。只放有值的欄位，避免蓋掉供應商預設值。"""
    from .media import build_references, check_total_size, to_content_part

    body: dict = {"model": spec.model}

    if spec.prompt:
        body["prompt"] = spec.prompt
    if spec.duration:
        body["duration"] = int(spec.duration)
    if spec.size:
        body["size"] = spec.size
    if spec.resolution:
        body["resolution"] = spec.resolution
    if spec.aspect_ratio:
        body["aspect_ratio"] = spec.aspect_ratio
    body["generate_audio"] = bool(spec.generate_audio)
    if spec.seed is not None:
        body["seed"] = int(spec.seed)

    frames = []
    if spec.first_frame:
        frames.append(to_content_part(spec.first_frame, frame_type="first_frame", log=log))
    if spec.last_frame:
        frames.append(to_content_part(spec.last_frame, frame_type="last_frame", log=log))
    if frames:
        body["frame_images"] = frames

    references = build_references(spec.references, log=log)
    if references:
        body["input_references"] = references

    check_total_size(frames + references)
    return body


def redact(body: dict) -> dict:
    """把 base64 素材換成摘要，讓記錄檔與日誌不會被幾 MB 的字串塞爆。"""

    def shrink(part: dict) -> dict:
        clone = dict(part)
        for key in ("image_url", "video_url", "audio_url"):
            value = clone.get(key)
            if isinstance(value, dict):
                url = str(value.get("url", ""))
                if url.startswith("data:"):
                    header = url.split(",", 1)[0]
                    clone[key] = {"url": "<%s, %d KB>" % (header, len(url) // 1024)}
        return clone

    out = dict(body)
    for key in ("frame_images", "input_references"):
        if key in out:
            out[key] = [shrink(part) for part in out[key]]
    return out


class SeedanceClient:
    def __init__(self, api_key: str, *, base_url: str = API_BASE, timeout: int = 120):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": "Bearer " + api_key,
                "HTTP-Referer": SITE_URL,
                "X-Title": "seedance-video-tool",
            }
        )

    # --- 送單 ---------------------------------------------------------

    def submit(self, body: dict) -> dict:
        try:
            response = self.session.post(
                self.base_url + "/videos",
                json=body,
                timeout=self.timeout,
                headers={"Content-Type": "application/json"},
            )
        except requests.RequestException as exc:
            raise ApiError(0, "送出任務時連線失敗：%s" % exc) from exc

        if response.status_code not in (200, 201, 202):
            raise ApiError(response.status_code, _error_message(response), _safe_json(response))

        job = _safe_json(response) or {}
        if not job.get("id"):
            raise ApiError(response.status_code, "回應中沒有任務 id", job)
        return job

    # --- 輪詢 ---------------------------------------------------------

    def poll(self, job: dict | str) -> dict:
        url = self._polling_url(job)
        try:
            response = self.session.get(url, timeout=self.timeout)
        except requests.RequestException as exc:
            raise ApiError(0, "輪詢時連線失敗：%s" % exc) from exc

        if response.status_code != 200:
            raise ApiError(response.status_code, _error_message(response), _safe_json(response))
        return _safe_json(response) or {}

    def wait(
        self,
        job: dict | str,
        *,
        on_update: Callable[[dict, int], None] | None = None,
        timeout_s: int = DEFAULT_JOB_TIMEOUT,
        should_cancel: Callable[[], bool] | None = None,
    ) -> dict:
        """輪詢到終態。網路瞬斷會重試，不讓已計費的任務因一次逾時就丟掉。"""
        job_id = job.get("id") if isinstance(job, dict) else str(job)
        status = job if isinstance(job, dict) else {"id": job_id, "status": "pending"}
        deadline = time.monotonic() + timeout_s
        interval = POLL_INTERVAL_START
        attempt = 0
        consecutive_errors = 0

        while True:
            state = str(status.get("status", "")).lower()
            if state == "completed":
                return status
            if state in TERMINAL_FAILURES:
                raise JobFailedError(state, status.get("error") or "供應商未提供原因", status)

            if should_cancel and should_cancel():
                raise JobTimeoutError(
                    job_id,
                    "已停止等待。任務 %s 仍在雲端執行，可稍後用 resume 取件。" % job_id,
                )

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise JobTimeoutError(
                    job_id,
                    "等待逾時（%d 分鐘）。任務 %s 可能仍在執行，"
                    "請用 python -m seedance resume %s 取件。" % (timeout_s // 60, job_id, job_id),
                )

            # 分段睡，GUI 按下「停止等待」才不用等滿一輪 30 秒才有反應。
            slept = 0.0
            nap = max(1.0, min(interval, remaining))
            while slept < nap:
                if should_cancel and should_cancel():
                    break
                time.sleep(min(1.0, nap - slept))
                slept += 1.0
            interval = min(interval + 5, POLL_INTERVAL_MAX)
            if should_cancel and should_cancel():
                continue
            attempt += 1

            try:
                status = self.poll(status if status.get("polling_url") else job_id)
                consecutive_errors = 0
            except ApiError as exc:
                consecutive_errors += 1
                if consecutive_errors >= 5:
                    raise
                if on_update:
                    on_update(
                        {
                            "id": job_id,
                            "status": "輪詢失敗（第 %d 次，將重試）：%s" % (consecutive_errors, exc.message),
                        },
                        attempt,
                    )
                status = {"id": job_id, "status": "in_progress", "polling_url": _job_path(job_id)}
                continue

            if on_update:
                on_update(status, attempt)

    # --- 下載 ---------------------------------------------------------

    def download(self, job: dict, dest: Path, *, index: int = 0) -> Path:
        job_id = job.get("id")
        urls = job.get("unsigned_urls") or []
        if index < len(urls):
            url = urls[index]
        else:
            url = "%s/videos/%s/content?index=%d" % (self.base_url, job_id, index)
        url = urljoin(SITE_URL, url)

        # 只有打回 OpenRouter 自家 API 才需要帶金鑰；簽名網址不需要，也不該外洩。
        headers = dict(self.session.headers) if url.startswith(SITE_URL + "/api/") else {}

        try:
            with requests.get(url, headers=headers, stream=True, timeout=self.timeout) as response:
                if response.status_code != 200:
                    raise ApiError(response.status_code, _error_message(response), _safe_json(response))
                dest.parent.mkdir(parents=True, exist_ok=True)
                with dest.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=1 << 20):
                        if chunk:
                            handle.write(chunk)
        except requests.RequestException as exc:
            raise ApiError(0, "下載影片失敗：%s" % exc) from exc

        if dest.stat().st_size == 0:
            dest.unlink(missing_ok=True)
            raise ApiError(0, "下載到 0 位元組的檔案，已刪除")
        return dest

    def _polling_url(self, job: dict | str) -> str:
        if isinstance(job, dict):
            raw = job.get("polling_url") or _job_path(job.get("id"))
        else:
            raw = _job_path(job)
        return urljoin(SITE_URL, raw)


def _job_path(job_id) -> str:
    return "/api/v1/videos/%s" % job_id


def _error_message(response) -> str:
    payload = _safe_json(response)
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if isinstance(error, str) and error:
            return error
    text = (response.text or "").strip()
    return text[:400] or response.reason or "未知錯誤"


def _safe_json(response):
    try:
        return response.json()
    except ValueError:
        return None


# --- 任務記錄 ---------------------------------------------------------


def save_job_record(job: dict, body: dict | None = None, *, extra: dict | None = None) -> Path:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    path = JOBS_DIR / ("%s.json" % job.get("id"))
    record = _load_record_or_empty(path)
    record.update(
        {
            "id": job.get("id"),
            "polling_url": job.get("polling_url") or record.get("polling_url"),
            "status": job.get("status"),
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    )
    if body is not None:
        record["request"] = redact(body)
    record.setdefault("created_at", record["updated_at"])
    if extra:
        record.update(extra)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_job_record(job_id: str) -> dict:
    path = JOBS_DIR / ("%s.json" % job_id)
    if not path.is_file():
        return {"id": job_id}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"id": job_id}


def _load_record_or_empty(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
