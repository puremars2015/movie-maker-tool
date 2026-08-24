"""透過 OpenRouter 製作影片的小工具，支援多個模型，CLI 與 GUI 共用同一套核心。"""

from .client import GenerationSpec, VideoClient
from .runner import generate, generate_batch, resume

__all__ = ["GenerationSpec", "VideoClient", "generate", "generate_batch", "resume"]
__version__ = "0.1.0"
