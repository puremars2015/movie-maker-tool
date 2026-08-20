"""集中定義例外，讓 CLI 與 GUI 能用同一套方式呈現錯誤訊息。"""

from __future__ import annotations


class SeedanceError(Exception):
    """本工具所有錯誤的基底類別。"""


class ConfigError(SeedanceError):
    """缺少 API 金鑰、.env 讀取失敗等設定問題。"""


class ValidationError(SeedanceError):
    """送單前的本地參數驗證失敗（例如秒數不在模型支援清單內）。"""


class CostGuardError(SeedanceError):
    """預估成本超過護欄門檻，且未經確認。"""


class ApiError(SeedanceError):
    """OpenRouter 回傳非 2xx。"""

    def __init__(self, status_code: int, message: str, payload=None):
        super().__init__(f"HTTP {status_code}: {message}")
        self.status_code = status_code
        self.message = message
        self.payload = payload


class JobFailedError(SeedanceError):
    """任務進入 failed / cancelled / expired 終態。"""

    def __init__(self, status: str, message: str, job: dict | None = None):
        super().__init__(f"任務 {status}：{message}")
        self.status = status
        self.message = message
        self.job = job or {}


class JobTimeoutError(SeedanceError):
    """輪詢超過時限仍未完成（任務可能仍在雲端執行，可用 resume 取件）。"""

    def __init__(self, job_id: str, message: str):
        super().__init__(message)
        self.job_id = job_id
