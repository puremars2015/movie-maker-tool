"""用 OpenRouter 的 Seedance 模型製作影片的小工具，CLI 與 GUI 共用同一套核心。"""

from .client import GenerationSpec, SeedanceClient
from .runner import generate, generate_batch, resume

__all__ = ["GenerationSpec", "SeedanceClient", "generate", "generate_batch", "resume"]
__version__ = "0.1.0"
