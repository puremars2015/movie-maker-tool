"""模型能力表：抓 /api/v1/videos/models，快取，並在送單前做本地驗證。

先驗證再送單有兩個好處：錯誤訊息比 API 的 400 具體，而且不會有「參數打錯卻已
建立任務」的風險。這個端點是公開的，不需要金鑰。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

from .config import API_BASE, CACHE_DIR
from .errors import ApiError, ValidationError

CACHE_FILE = CACHE_DIR / "videos_models.json"
CACHE_TTL_SECONDS = 24 * 60 * 60


@dataclass
class ModelCapabilities:
    """單一影片模型的能力宣告，欄位對應 /videos/models 的回應。"""

    id: str
    name: str = ""
    supported_resolutions: list[str] = field(default_factory=list)
    supported_aspect_ratios: list[str] = field(default_factory=list)
    supported_sizes: list[str] = field(default_factory=list)
    supported_durations: list[int] = field(default_factory=list)
    supported_frame_images: list[str] = field(default_factory=list)
    generate_audio: bool = False
    seed: bool = False
    pricing_skus: dict[str, str] = field(default_factory=dict)
    allowed_passthrough_parameters: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> "ModelCapabilities":
        return cls(
            id=data.get("id", ""),
            name=data.get("name", ""),
            supported_resolutions=list(data.get("supported_resolutions") or []),
            supported_aspect_ratios=list(data.get("supported_aspect_ratios") or []),
            supported_sizes=list(data.get("supported_sizes") or []),
            supported_durations=[int(d) for d in (data.get("supported_durations") or [])],
            supported_frame_images=list(data.get("supported_frame_images") or []),
            generate_audio=bool(data.get("generate_audio")),
            seed=bool(data.get("seed")),
            pricing_skus=dict(data.get("pricing_skus") or {}),
            allowed_passthrough_parameters=list(data.get("allowed_passthrough_parameters") or []),
            raw=data,
        )

    # --- 預設值 -------------------------------------------------------

    def default_size(self) -> str:
        """預設輸出尺寸：最低解析度的手機直式版面（9:16 優先）。"""
        portrait = [s for s in self.supported_sizes if _area(s) and _is_portrait(s)]
        if not portrait:
            return self.supported_sizes[0] if self.supported_sizes else "480x854"

        phone = [s for s in portrait if _is_nine_sixteen(s)]
        pool = phone or portrait
        return min(pool, key=_area)

    def default_duration(self, preferred: int = 5) -> int:
        if not self.supported_durations:
            return preferred
        if preferred in self.supported_durations:
            return preferred
        return min(self.supported_durations)

    def sorted_sizes(self) -> list[str]:
        """依面積排序，讓下拉選單由小到大，最便宜的在最上面。"""
        return sorted([s for s in self.supported_sizes if _area(s)], key=_area)

    # --- 驗證 ---------------------------------------------------------

    def validate(
        self,
        *,
        duration: int | None = None,
        size: str | None = None,
        resolution: str | None = None,
        aspect_ratio: str | None = None,
        generate_audio: bool = False,
        seed: int | None = None,
        frame_types: list[str] | None = None,
    ) -> None:
        """不合法就丟 ValidationError，訊息裡附上合法值清單。"""
        problems: list[str] = []

        if duration is not None and self.supported_durations and duration not in self.supported_durations:
            problems.append(
                f"秒數 {duration} 不支援，可用：{_join(self.supported_durations)}"
            )
        if size and self.supported_sizes and size not in self.supported_sizes:
            problems.append(f"尺寸 {size} 不支援，可用：{_join(self.supported_sizes)}")
        if resolution and self.supported_resolutions and resolution not in self.supported_resolutions:
            problems.append(
                f"解析度 {resolution} 不支援，可用：{_join(self.supported_resolutions)}"
            )
        if aspect_ratio and self.supported_aspect_ratios and aspect_ratio not in self.supported_aspect_ratios:
            problems.append(
                f"長寬比 {aspect_ratio} 不支援，可用：{_join(self.supported_aspect_ratios)}"
            )
        if generate_audio and not self.generate_audio:
            problems.append(f"模型 {self.id} 不支援生成音訊")
        if seed is not None and not self.seed:
            problems.append(f"模型 {self.id} 不支援 seed")
        for frame_type in frame_types or []:
            if self.supported_frame_images and frame_type not in self.supported_frame_images:
                problems.append(
                    f"影格類型 {frame_type} 不支援，可用：{_join(self.supported_frame_images)}"
                )

        if problems:
            raise ValidationError("參數不符合模型能力：\n  - " + "\n  - ".join(problems))


def _area(size: str) -> int:
    try:
        width, height = size.lower().split("x")
        return int(width) * int(height)
    except (ValueError, AttributeError):
        return 0


def _dims(size: str) -> tuple[int, int]:
    width, height = size.lower().split("x")
    return int(width), int(height)


def _is_portrait(size: str) -> bool:
    width, height = _dims(size)
    return height > width


def _is_nine_sixteen(size: str) -> bool:
    width, height = _dims(size)
    return abs(height / width - 16 / 9) < 0.02


def _join(values) -> str:
    return ", ".join(str(v) for v in values)


# --- 取得資料 ---------------------------------------------------------


def fetch_models(*, force_refresh: bool = False, timeout: int = 30) -> list[dict]:
    """回傳所有影片模型。優先用快取；抓不到時退回過期快取而不是直接失敗。"""
    cached = _read_cache()
    if cached and not force_refresh and not _cache_expired(cached):
        return cached["data"]

    try:
        response = requests.get(f"{API_BASE}/videos/models", timeout=timeout)
    except requests.RequestException as exc:
        if cached:
            return cached["data"]
        raise ApiError(0, f"無法連線到 OpenRouter 取得模型清單：{exc}") from exc

    if response.status_code != 200:
        if cached:
            return cached["data"]
        raise ApiError(response.status_code, "取得模型清單失敗", _safe_json(response))

    data = (_safe_json(response) or {}).get("data") or []
    _write_cache(data)
    return data


def get_capabilities(model: str, *, force_refresh: bool = False) -> ModelCapabilities:
    for entry in fetch_models(force_refresh=force_refresh):
        if entry.get("id") == model:
            return ModelCapabilities.from_dict(entry)
    raise ValidationError(
        f"在 OpenRouter 的影片模型清單中找不到 {model}。"
        f"可用 `python -m seedance models` 列出目前支援的型號。"
    )


def _read_cache() -> dict | None:
    if not CACHE_FILE.is_file():
        return None
    try:
        payload = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload.get("data"), list) else None


def _cache_expired(cached: dict) -> bool:
    return time.time() - float(cached.get("fetched_at", 0)) > CACHE_TTL_SECONDS


def _write_cache(data: list[dict]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"fetched_at": time.time(), "data": data}
    CACHE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _safe_json(response) -> dict | None:
    try:
        return response.json()
    except ValueError:
        return None
