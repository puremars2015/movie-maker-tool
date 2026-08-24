"""成本估算與護欄。

不同供應商的計價方式不一樣，而且差很多：

  * ByteDance Seedance 以 video token 計價：
        tokens = (寬 × 高 × 秒數 × 24) / 1024
    所以尺寸與秒數都會影響費用。
  * MiniMax H3 以秒計價（`duration_seconds`），另外按參考圖張數加價
    （`reference_images`），跟畫面大小無關。

單價一律取自 `/videos/models` 的 `pricing_skus`。**認不出計價方式時要直接報錯**，
絕不能回 0——那會讓成本護欄失效，使用者以為免費卻被扣款。
"""

from __future__ import annotations

from dataclasses import dataclass

from .capabilities import ModelCapabilities, derive_dimensions
from .errors import CostGuardError, ValidationError

FRAMES_PER_SECOND = 24
PIXELS_PER_TOKEN = 1024

TOKEN_SKUS = ("video_tokens", "video_tokens_without_audio", "video_tokens_with_video_input")
SECONDS_SKU = "duration_seconds"
REFERENCE_SKU = "reference_images"

# 實測「實際扣款 ÷ 牌價」的比例。只放真的量過的模型；沒量過的一律當 1.0，
# 寧可高估也不要讓使用者以為比較便宜。
OBSERVED_DISCOUNT = {
    "bytedance/seedance-2.0-mini": 0.416,
}


@dataclass
class CostEstimate:
    usd: float
    basis: str                  # "tokens" 或 "seconds"
    sku: str
    duration: int
    model: str = ""
    tokens: int = 0
    price_per_token: float = 0.0
    width: int = 0
    height: int = 0
    reference_count: int = 0
    detail: str = ""

    @property
    def discount(self) -> float:
        return OBSERVED_DISCOUNT.get(self.model, 1.0)

    @property
    def expected_usd(self) -> float:
        """依實測折扣推估的實際扣款；沒量過折扣的模型就等於牌價。"""
        return self.usd * self.discount

    @property
    def spec_label(self) -> str:
        if self.width and self.height:
            return "%dx%d" % (self.width, self.height)
        return "—"

    def format(self) -> str:
        if self.discount < 1.0:
            tail = "（牌價上限，依實測折扣實際約 US$%.3f）" % self.expected_usd
        else:
            tail = "（牌價）"
        return "預估 %s ≈ US$%.3f %s" % (self.detail, self.usd, tail)


def parse_size(size: str) -> tuple[int, int]:
    try:
        width, height = str(size).lower().split("x")
        return int(width), int(height)
    except (ValueError, AttributeError) as exc:
        raise ValidationError(f"尺寸格式錯誤：{size!r}，應為 寬x高，例如 480x854") from exc


def estimate_tokens(width: int, height: int, duration: int) -> int:
    return int(width * height * duration * FRAMES_PER_SECOND / PIXELS_PER_TOKEN)


def pricing_basis(caps: ModelCapabilities) -> str:
    skus = caps.pricing_skus or {}
    if any(name in skus for name in TOKEN_SKUS):
        return "tokens"
    if SECONDS_SKU in skus:
        return "seconds"
    return "unknown"


def pick_token_sku(caps: ModelCapabilities, *, generate_audio: bool, has_video_input: bool) -> tuple[str, float]:
    """挑對應的 token 計價 SKU。有影片參考輸入時單價較低。"""
    skus = caps.pricing_skus or {}
    order = (
        ["video_tokens_with_video_input", "video_tokens", "video_tokens_without_audio"]
        if has_video_input
        else (
            ["video_tokens", "video_tokens_without_audio"]
            if generate_audio
            else ["video_tokens_without_audio", "video_tokens"]
        )
    )
    for name in order:
        if name in skus:
            try:
                return name, float(skus[name])
            except (TypeError, ValueError):
                continue
    raise ValidationError("模型 %s 的 token 單價無法解析：%s" % (caps.id, skus))


def estimate(
    caps: ModelCapabilities,
    *,
    duration: int,
    size: str | None = None,
    resolution: str | None = None,
    aspect_ratio: str | None = None,
    generate_audio: bool = False,
    has_video_input: bool = False,
    reference_count: int = 0,
) -> CostEstimate:
    basis = pricing_basis(caps)

    if basis == "seconds":
        return _estimate_by_seconds(
            caps, duration, reference_count, size, resolution, aspect_ratio
        )
    if basis == "tokens":
        return _estimate_by_tokens(
            caps, duration, size, resolution, aspect_ratio,
            generate_audio, has_video_input, reference_count,
        )

    raise ValidationError(
        "無法判斷模型 %s 的計價方式，因此拒絕估價。\n"
        "pricing_skus = %s\n"
        "認得的欄位有 %s（依 token）與 %s（依秒）。\n"
        "在能正確估價之前不會送出請求——否則成本護欄會形同虛設。"
        % (caps.id, caps.pricing_skus, ", ".join(TOKEN_SKUS), SECONDS_SKU)
    )


def _estimate_by_seconds(
    caps: ModelCapabilities,
    duration: int,
    reference_count: int,
    size: str | None = None,
    resolution: str | None = None,
    aspect_ratio: str | None = None,
) -> CostEstimate:
    skus = caps.pricing_skus or {}
    try:
        per_second = float(skus[SECONDS_SKU])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValidationError("模型 %s 的每秒單價無法解析：%s" % (caps.id, skus)) from exc

    usd = duration * per_second
    detail = "%d 秒 × $%s" % (duration, _trim(per_second))

    try:
        per_reference = float(skus.get(REFERENCE_SKU, 0) or 0)
    except (TypeError, ValueError):
        per_reference = 0.0
    if per_reference and reference_count:
        usd += reference_count * per_reference
        detail += " + %d 張參考圖 × $%s" % (reference_count, _trim(per_reference))

    # 畫面大小不影響費用，但介面與檔名還是要顯示，所以一併推算出來。
    if size:
        width, height = parse_size(size)
    elif resolution:
        width, height = derive_dimensions(resolution, aspect_ratio or "16:9")
    else:
        width = height = 0

    return CostEstimate(
        usd=usd,
        basis="seconds",
        sku=SECONDS_SKU,
        duration=duration,
        model=caps.id,
        width=width,
        height=height,
        reference_count=reference_count,
        detail=detail,
    )


def _estimate_by_tokens(
    caps: ModelCapabilities,
    duration: int,
    size: str | None,
    resolution: str | None,
    aspect_ratio: str | None,
    generate_audio: bool,
    has_video_input: bool,
    reference_count: int,
) -> CostEstimate:
    if size:
        width, height = parse_size(size)
    elif resolution:
        width, height = derive_dimensions(resolution, aspect_ratio or "16:9")
    else:
        width = height = 0

    if not width or not height:
        raise ValidationError(
            "模型 %s 依畫面大小計價，但無法從目前的設定推算尺寸"
            "（size=%r, resolution=%r, aspect_ratio=%r）。" % (caps.id, size, resolution, aspect_ratio)
        )

    tokens = estimate_tokens(width, height, duration)
    sku, price = pick_token_sku(caps, generate_audio=generate_audio, has_video_input=has_video_input)

    return CostEstimate(
        usd=tokens * price,
        basis="tokens",
        sku=sku,
        duration=duration,
        model=caps.id,
        tokens=tokens,
        price_per_token=price,
        width=width,
        height=height,
        reference_count=reference_count,
        detail="%dx%d × %d 秒 = %s tokens × $%s"
               % (width, height, duration, format(tokens, ","), _trim(price)),
    )


def _trim(value: float) -> str:
    return ("%.7f" % value).rstrip("0").rstrip(".")


def check_guard(cost: CostEstimate, limit_usd: float, *, approved: bool) -> None:
    """超過門檻且未經確認就擋下來。approved 由 CLI 的 --yes 或 GUI 的確認對話框帶入。"""
    if approved or cost.usd <= limit_usd:
        return
    raise CostGuardError(
        f"預估花費 US${cost.usd:.3f} 超過門檻 US${limit_usd:.2f}。\n"
        f"CLI 請加 --yes 確認，或用 SEEDANCE_COST_LIMIT 調整門檻。"
    )


def expected_total(model: str, list_usd: float) -> float:
    """一批分鏡的預期實際花費。未量測過折扣的模型直接回牌價。"""
    return list_usd * OBSERVED_DISCOUNT.get(model, 1.0)


def selectable_models() -> list[tuple[str, str]]:
    """介面可選的模型：只列出計價方式認得出來的。

    算不出價錢的模型不該出現在選單裡——那等於請使用者在不知道要花多少錢的
    情況下按下送出。CLI 仍可用 --model 指定任何型號，只是會在估價階段報錯。
    """
    from .capabilities import ModelCapabilities, fetch_models
    from .config import DEFAULT_MODEL

    entries = []
    for raw in fetch_models():
        caps = ModelCapabilities.from_dict(raw)
        basis = pricing_basis(caps)
        if basis == "unknown":
            continue
        label = "%s ｜ %s" % (caps.name or caps.id, "依秒計價" if basis == "seconds" else "依畫面大小計價")
        entries.append((caps.id, label))

    entries.sort(key=lambda item: (item[0] != DEFAULT_MODEL, item[0]))
    return entries
