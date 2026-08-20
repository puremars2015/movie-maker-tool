"""參考素材處理：把本機檔案或網址轉成 OpenRouter 的 content part。

OpenRouter 接受兩種來源：
  * 公開 HTTPS 網址
  * data URL（base64 內嵌）
本機檔案走 base64。base64 會膨脹約 33%，所以設了大小上限，免得送出一個註定
逾時或被拒的請求。影片／音訊參考只有 Seedance 2 代以上會實際採用。
"""

from __future__ import annotations

import base64
import io
import mimetypes
from pathlib import Path

from .errors import ValidationError

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".m4v", ".avi", ".mkv"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}

# 內嵌上限（原始檔案大小，非 base64 後）
MAX_INLINE_BYTES = {"image": 8 * 1024 * 1024, "video": 24 * 1024 * 1024, "audio": 12 * 1024 * 1024}

# 超過這個大小的圖片會先縮圖再內嵌（需要 Pillow）
SHRINK_THRESHOLD_BYTES = 1536 * 1024
MAX_IMAGE_EDGE = 1536

# 整個請求體的 base64 總量上限；超過多半會逾時或被拒
MAX_TOTAL_INLINE_BYTES = 24 * 1024 * 1024

FILE_DIALOG_TYPES = [
    ("所有支援的素材", "*.png *.jpg *.jpeg *.webp *.gif *.bmp *.mp4 *.mov *.webm *.m4v *.mp3 *.wav *.m4a *.aac"),
    ("圖片", "*.png *.jpg *.jpeg *.webp *.gif *.bmp"),
    ("影片", "*.mp4 *.mov *.webm *.m4v"),
    ("音訊", "*.mp3 *.wav *.m4a *.aac *.ogg *.flac"),
    ("所有檔案", "*.*"),
]


def is_url(value: str) -> bool:
    return str(value).lower().startswith(("http://", "https://", "data:"))


def kind_of(source: str) -> str:
    """判斷素材類型，回傳 image / video / audio。"""
    suffix = Path(str(source).split("?")[0]).suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    if suffix in AUDIO_EXTENSIONS:
        return "audio"

    guessed, _ = mimetypes.guess_type(str(source))
    if guessed:
        for prefix in ("image", "video", "audio"):
            if guessed.startswith(prefix):
                return prefix
    raise ValidationError(
        f"無法判斷素材類型：{source}\n支援副檔名："
        f"{', '.join(sorted(IMAGE_EXTENSIONS | VIDEO_EXTENSIONS | AUDIO_EXTENSIONS))}"
    )


def shrink_image(path: Path) -> tuple[bytes, str] | None:
    """把過大的參考圖縮到 MAX_IMAGE_EDGE 內並轉 JPEG。

    手機或 AI 生成的 PNG 動輒兩三 MB，base64 後再膨脹 33%，四張就逼近請求體上限。
    縮到 1536 px 對「角色長相／畫風」這類參考用途完全足夠。
    需要 Pillow；沒裝就回傳 None，改用原檔（由大小上限把關）。
    """
    if path.stat().st_size <= SHRINK_THRESHOLD_BYTES:
        return None
    try:
        from PIL import Image
    except ImportError:
        return None

    try:
        with Image.open(path) as image:
            image = image.convert("RGB")
            image.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE))
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=88, optimize=True)
    except OSError:
        return None
    return buffer.getvalue(), "image/jpeg"


def to_data_url(path: Path, kind: str, log=None) -> str:
    payload = None
    mime = None

    if kind == "image":
        shrunk = shrink_image(path)
        if shrunk:
            payload, mime = shrunk
            if log:
                log(
                    "已縮圖 %s：%.1f MB → %.1f MB"
                    % (path.name, path.stat().st_size / 1024 / 1024, len(payload) / 1024 / 1024)
                )

    if payload is None:
        size = path.stat().st_size
        limit = MAX_INLINE_BYTES[kind]
        if size > limit:
            raise ValidationError(
                f"{path.name} 有 {size / 1024 / 1024:.1f} MB，超過內嵌上限 "
                f"{limit / 1024 / 1024:.0f} MB。請壓縮、安裝 Pillow 讓程式自動縮圖，"
                f"或改用公開 HTTPS 網址。"
            )
        payload = path.read_bytes()

    if not mime:
        mime, _ = mimetypes.guess_type(str(path))
    if not mime:
        mime = {"image": "image/png", "video": "video/mp4", "audio": "audio/mpeg"}[kind]

    encoded = base64.b64encode(payload).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def to_content_part(source: str, *, frame_type: str | None = None, log=None) -> dict:
    """把一個素材來源轉成 API 的 content part；frame_type 只用於首尾影格。"""
    source = str(source).strip()
    if not source:
        raise ValidationError("素材路徑為空")

    if is_url(source):
        kind = "image" if source.startswith("data:image") else kind_of(source)
        url = source
    else:
        path = Path(source).expanduser()
        if not path.is_file():
            raise ValidationError(f"找不到檔案：{path}")
        kind = kind_of(path)
        url = to_data_url(path, kind, log=log)

    if frame_type:
        if kind != "image":
            raise ValidationError(f"首尾影格只能用圖片，{source} 是 {kind}")
        return {"type": "image_url", "image_url": {"url": url}, "frame_type": frame_type}

    key = f"{kind}_url"
    return {"type": key, key: {"url": url}}


def build_references(sources, log=None) -> list[dict]:
    return [to_content_part(item, log=log) for item in sources or []]


def check_total_size(parts: list[dict]) -> None:
    """把所有內嵌素材加總，超量就在送單前擋下來。"""
    total = 0
    for part in parts:
        for key in ("image_url", "video_url", "audio_url"):
            value = part.get(key)
            if isinstance(value, dict):
                url = str(value.get("url", ""))
                if url.startswith("data:"):
                    total += len(url)
    if total > MAX_TOTAL_INLINE_BYTES:
        raise ValidationError(
            f"內嵌素材合計約 {total / 1024 / 1024:.1f} MB，超過 "
            f"{MAX_TOTAL_INLINE_BYTES / 1024 / 1024:.0f} MB。請減少參考素材數量、"
            f"先把圖片縮小，或改用公開 HTTPS 網址。"
        )


def has_video_reference(sources) -> bool:
    for item in sources or []:
        try:
            if kind_of(item) == "video":
                return True
        except ValidationError:
            continue
    return False


def describe(source: str) -> str:
    """在介面上顯示用的短描述。"""
    if is_url(source):
        return f"[網址] {source[:60]}"
    path = Path(source)
    try:
        size_mb = path.stat().st_size / 1024 / 1024
        return f"[{kind_of(path)}] {path.name}（{size_mb:.1f} MB）"
    except (OSError, ValidationError):
        return path.name
