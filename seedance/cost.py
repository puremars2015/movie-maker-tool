"""成本估算與護欄。

Seedance 以 video token 計價，官方公式：
    tokens = (寬 × 高 × 秒數 × 24) / 1024
單價來自 /videos/models 的 pricing_skus。注意那是牌價，模型頁面上的促銷折扣
不會反映在這裡，所以估出來的數字是「上限」，實際扣款以任務回傳的 usage.cost 為準。
"""

from __future__ import annotations

from dataclasses import dataclass

from .capabilities import ModelCapabilities
from .errors import CostGuardError, ValidationError

FRAMES_PER_SECOND = 24
PIXELS_PER_TOKEN = 1024


@dataclass
class CostEstimate:
    tokens: int
    price_per_token: float
    usd: float
    sku: str
    width: int
    height: int
    duration: int

    def format(self) -> str:
        return (
            f"預估 {self.width}x{self.height} × {self.duration} 秒 = "
            f"{self.tokens:,} tokens × ${self.price_per_token:.7f} "
            f"≈ US${self.usd:.3f}（牌價上限，未計促銷折扣）"
        )


def parse_size(size: str) -> tuple[int, int]:
    try:
        width, height = str(size).lower().split("x")
        return int(width), int(height)
    except (ValueError, AttributeError) as exc:
        raise ValidationError(f"尺寸格式錯誤：{size!r}，應為 寬x高，例如 480x854") from exc


def estimate_tokens(width: int, height: int, duration: int) -> int:
    return int(width * height * duration * FRAMES_PER_SECOND / PIXELS_PER_TOKEN)


def pick_sku(caps: ModelCapabilities, *, generate_audio: bool, has_video_input: bool) -> tuple[str, float]:
    """挑對應的計價 SKU。有影片參考輸入時單價較低。"""
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
    return "unknown", 0.0


def estimate(
    caps: ModelCapabilities,
    *,
    size: str,
    duration: int,
    generate_audio: bool = False,
    has_video_input: bool = False,
) -> CostEstimate:
    width, height = parse_size(size)
    tokens = estimate_tokens(width, height, duration)
    sku, price = pick_sku(caps, generate_audio=generate_audio, has_video_input=has_video_input)
    return CostEstimate(
        tokens=tokens,
        price_per_token=price,
        usd=tokens * price,
        sku=sku,
        width=width,
        height=height,
        duration=duration,
    )


def check_guard(cost: CostEstimate, limit_usd: float, *, approved: bool) -> None:
    """超過門檻且未經確認就擋下來。approved 由 CLI 的 --yes 或 GUI 的確認對話框帶入。"""
    if approved or cost.usd <= limit_usd:
        return
    raise CostGuardError(
        f"預估花費 US${cost.usd:.3f} 超過門檻 US${limit_usd:.2f}。\n"
        f"CLI 請加 --yes 確認，或用 SEEDANCE_COST_LIMIT 調整門檻。"
    )
