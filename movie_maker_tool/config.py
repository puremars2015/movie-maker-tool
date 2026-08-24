"""設定：.env 解析、API 金鑰取得、專案路徑。

金鑰查找順序（依使用者指定）：
    1. .env 檔內的 OPENROUTER_API_KEY  ← 優先
    2. 系統環境變數 OPENROUTER_API_KEY  ← 回退
注意這與一般「環境變數蓋過 .env」的慣例相反，是刻意為之，改動前請先確認。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .errors import ConfigError


def _detect_project_root() -> Path:
    """一般執行時是原始碼所在的專案根目錄。

    打包成 PyInstaller 執行檔後 __file__ 會落在解壓縮出來的暫存目錄，
    不是使用者能看到、能編輯 .env 的地方，所以改用「執行檔旁邊」的目錄：
        - macOS .app：Contents/MacOS/MovieMakerTool → 回推到 .app 外層的資料夾
        - Windows/單一執行檔：執行檔所在資料夾
    """
    if getattr(sys, "frozen", False):
        exe_path = Path(sys.executable).resolve()
        if sys.platform == "darwin" and ".app/Contents/MacOS" in str(exe_path):
            # .../SomeDir/MovieMakerTool.app/Contents/MacOS/MovieMakerTool
            return exe_path.parents[3]
        return exe_path.parent
    return Path(__file__).resolve().parent.parent


PROJECT_ROOT = _detect_project_root()
ENV_FILE = PROJECT_ROOT / ".env"
OUTPUT_DIR = PROJECT_ROOT / "outputs"
JOBS_DIR = PROJECT_ROOT / "jobs"
CACHE_DIR = PROJECT_ROOT / ".cache"

API_BASE = "https://openrouter.ai/api/v1"
SITE_URL = "https://openrouter.ai"
API_KEY_NAME = "OPENROUTER_API_KEY"

DEFAULT_MODEL = "bytedance/seedance-2.0-mini"
DEFAULT_DURATION = 5
DEFAULT_COST_LIMIT_USD = 0.50


def parse_env_file(path: Path | None = None) -> dict[str, str]:
    """讀 .env 成 dict。格式寬鬆：允許 export 前綴、# 註解、單雙引號。"""
    path = Path(path) if path else ENV_FILE
    if not path.is_file():
        return {}

    values: dict[str, str] = {}
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ConfigError(f"無法讀取 {path}：{exc}") from exc

    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def get_api_key(explicit: str | None = None, env_file: Path | None = None) -> str:
    """取得 API 金鑰。順序：參數 > .env > 系統環境變數。"""
    if explicit:
        return explicit.strip()

    from_file = parse_env_file(env_file).get(API_KEY_NAME, "").strip()
    if from_file:
        return from_file

    from_environ = os.environ.get(API_KEY_NAME, "").strip()
    if from_environ:
        return from_environ

    raise ConfigError(
        f"找不到 {API_KEY_NAME}。請在 {ENV_FILE} 寫入一行：\n"
        f"    {API_KEY_NAME}=sk-or-v1-你的金鑰\n"
        f"或設定同名的系統環境變數。金鑰可於 https://openrouter.ai/keys 取得。"
    )


def api_key_source(env_file: Path | None = None) -> str:
    """回報金鑰來源，只用於顯示，不會洩漏金鑰內容。"""
    if parse_env_file(env_file).get(API_KEY_NAME, "").strip():
        return ".env"
    if os.environ.get(API_KEY_NAME, "").strip():
        return "環境變數"
    return "未設定"


COST_LIMIT_NAMES = ("MOVIE_MAKER_COST_LIMIT", "SEEDANCE_COST_LIMIT")


def cost_limit_usd() -> float:
    """成本護欄門檻，可用 MOVIE_MAKER_COST_LIMIT 覆寫。

    工具改名前叫 SEEDANCE_COST_LIMIT，這裡仍然接受舊名。門檻若因為改名而
    悄悄退回預設值，使用者可能在不知情的情況下被擋下或放行，所以寧可多讀一個鍵。
    """
    env_file = parse_env_file()
    for name in COST_LIMIT_NAMES:
        raw = env_file.get(name) or os.environ.get(name)
        if raw:
            try:
                return float(raw)
            except ValueError:
                continue
    return DEFAULT_COST_LIMIT_USD


def ensure_dirs() -> None:
    for directory in (OUTPUT_DIR, JOBS_DIR, CACHE_DIR):
        directory.mkdir(parents=True, exist_ok=True)
