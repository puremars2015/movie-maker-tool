"""專案模式：一次定義所有分鏡，一次轉出全部。

與 batch 的差別在於「狀態」。batch 是一次性的：跑到第七鏡失敗，重跑會把前六鏡
重新生成一遍，等於再付一次錢。專案模式把每一鏡的結果寫進狀態檔，重跑時預設只
處理還沒完成的，所以重跑一百次也只會為失敗的鏡頭付費。

專案檔長這樣：

    {
      "title": "家族小劇場 EP1",
      "cast": { "爸爸": "爸爸.png", "弟弟": "弟弟.png" },
      "defaults": { "size": "480x854", "duration": 5 },
      "scenes": [
        { "id": "s01", "prompt": "客廳，爸爸正要帶弟弟出門", "cast": ["爸爸", "弟弟"] },
        { "id": "s02", "prompt": "玄關相視而笑", "continue_from": "s01" }
      ],
      "output": { "concat": "outputs/EP1.mp4" }
    }

cast 只定義一次、各鏡以名字引用，改角色圖時只要改一處。continue_from 會取前一鏡
的最後一格當這一鏡的首影格——那張圖在寫專案檔時還不存在，只能由程式在跑的當下填。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from .client import GenerationSpec
from .config import DEFAULT_MODEL
from .errors import ValidationError

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"


@dataclass
class Scene:
    """一個分鏡。references 已經把 cast 名字解析成實際路徑。"""

    id: str
    prompt: str = ""
    duration: int | None = None
    size: str | None = None
    resolution: str | None = None
    aspect_ratio: str | None = None
    generate_audio: bool = False
    seed: int | None = None
    model: str = DEFAULT_MODEL
    cast: list[str] = field(default_factory=list)
    references: list[str] = field(default_factory=list)
    first_frame: str | None = None
    last_frame: str | None = None
    continue_from: str | None = None

    def to_spec(self) -> GenerationSpec:
        return GenerationSpec(
            prompt=self.prompt,
            model=self.model,
            duration=self.duration or 0,
            size=self.size,
            resolution=self.resolution,
            aspect_ratio=self.aspect_ratio,
            generate_audio=self.generate_audio,
            seed=self.seed,
            references=list(self.references),
            first_frame=self.first_frame,
            last_frame=self.last_frame,
            name=self.id,
        )


@dataclass
class Project:
    path: Path
    title: str = ""
    cast: dict[str, str] = field(default_factory=dict)
    defaults: dict = field(default_factory=dict)
    scenes: list[Scene] = field(default_factory=list)
    concat_output: str | None = None
    raw: dict = field(default_factory=dict)

    @property
    def root(self) -> Path:
        """專案檔所在目錄。所有相對路徑以它為基準，不是以 cwd。"""
        return self.path.parent

    def scene_by_id(self, scene_id: str) -> Scene | None:
        for scene in self.scenes:
            if scene.id == scene_id:
                return scene
        return None

    @property
    def state_path(self) -> Path:
        return self.path.with_suffix(".state.json")

    @property
    def frames_dir(self) -> Path:
        return self.root / ".frames"


# --- 載入 -------------------------------------------------------------


def load_project(path: str | Path) -> Project:
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise ValidationError("找不到專案檔：%s" % path)

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError("專案檔不是合法的 JSON：%s（第 %d 行）" % (exc.msg, exc.lineno)) from exc

    if not isinstance(data, dict) or "scenes" not in data:
        raise ValidationError("專案檔必須是物件，且要有 scenes 陣列。")

    defaults = data.get("defaults") or {}
    cast_table = data.get("cast") or {}
    project = Project(
        path=path,
        title=data.get("title", ""),
        cast=dict(cast_table),
        defaults=dict(defaults),
        concat_output=(data.get("output") or {}).get("concat"),
        raw=data,
    )

    seen: set[str] = set()
    for index, entry in enumerate(data["scenes"], start=1):
        scene = _build_scene(entry, index, defaults, cast_table, project.root)
        if scene.id in seen:
            raise ValidationError("分鏡 id 重複：%s。id 是狀態檔的對應鍵，必須唯一。" % scene.id)
        seen.add(scene.id)
        project.scenes.append(scene)

    _validate_graph(project)
    return project


def _build_scene(entry: dict, index: int, defaults: dict, cast_table: dict, root: Path) -> Scene:
    merged = {**defaults, **entry}
    scene_id = str(merged.get("id") or "s%02d" % index)

    names = list(merged.get("cast") or [])
    references: list[str] = []
    for name in names:
        if name not in cast_table:
            raise ValidationError(
                "分鏡 %s 的角色「%s」不在 cast 名單裡。已定義的角色：%s"
                % (scene_id, name, ", ".join(cast_table) or "（無）")
            )
        references.append(_resolve(cast_table[name], root))

    for extra in merged.get("references") or []:
        resolved = _resolve(extra, root)
        if resolved not in references:
            references.append(resolved)

    continue_from = merged.get("continue_from")
    first_frame = merged.get("first_frame")
    if continue_from and first_frame:
        raise ValidationError(
            "分鏡 %s 同時指定了 continue_from 與 first_frame，兩者衝突。"
            "continue_from 會自動填入首影格，請擇一。" % scene_id
        )

    return Scene(
        id=scene_id,
        prompt=merged.get("prompt", ""),
        duration=int(merged["duration"]) if merged.get("duration") else None,
        size=merged.get("size"),
        resolution=merged.get("resolution"),
        aspect_ratio=merged.get("aspect_ratio"),
        generate_audio=bool(merged.get("generate_audio", False)),
        seed=merged.get("seed"),
        model=merged.get("model", DEFAULT_MODEL),
        cast=names,
        references=references,
        first_frame=_resolve(first_frame, root) if first_frame else None,
        last_frame=_resolve(merged["last_frame"], root) if merged.get("last_frame") else None,
        continue_from=continue_from,
    )


def _resolve(value: str, root: Path) -> str:
    """相對路徑以專案檔所在目錄為基準；網址原樣保留。"""
    text = str(value)
    if text.lower().startswith(("http://", "https://", "data:")):
        return text
    candidate = Path(text).expanduser()
    return str(candidate if candidate.is_absolute() else (root / candidate).resolve())


def _validate_graph(project: Project) -> None:
    """continue_from 形成的依賴要指得到、不能指自己、不能成環。"""
    ids = {scene.id for scene in project.scenes}

    for scene in project.scenes:
        if not scene.continue_from:
            continue
        if scene.continue_from == scene.id:
            raise ValidationError("分鏡 %s 的 continue_from 指向自己。" % scene.id)
        if scene.continue_from not in ids:
            raise ValidationError(
                "分鏡 %s 的 continue_from 指向不存在的 %s。" % (scene.id, scene.continue_from)
            )

    # 走訪每條鏈找環；資料量小，直接每個節點往回追即可。
    for scene in project.scenes:
        seen = {scene.id}
        chain = [scene.id]
        cursor = scene.continue_from
        while cursor:
            if cursor in seen:
                raise ValidationError(
                    "continue_from 形成循環：%s。鏡頭接續必須是有方向的鏈。"
                    % " → ".join(chain + [cursor])
                )
            seen.add(cursor)
            chain.append(cursor)
            node = project.scene_by_id(cursor)
            cursor = node.continue_from if node else None


# --- 狀態檔 -----------------------------------------------------------


class ProjectState:
    """每一鏡的結果。存在的意義是重跑時不要為已完成的鏡頭再付一次錢。"""

    def __init__(self, path: Path, data: dict | None = None):
        self.path = path
        self.data = data or {"scenes": {}, "total_cost": 0.0}
        self.data.setdefault("scenes", {})

    @classmethod
    def load(cls, project: Project) -> "ProjectState":
        path = project.state_path
        if not path.is_file():
            return cls(path)
        try:
            return cls(path, json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            # 狀態檔壞掉不該讓整個專案打不開，但要讓使用者知道會重跑。
            return cls(path)

    def save(self) -> None:
        self.data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self.data["total_cost"] = round(self.total_cost(), 6)
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def get(self, scene_id: str) -> dict:
        return self.data["scenes"].get(scene_id, {"status": STATUS_PENDING})

    def status_of(self, scene_id: str) -> str:
        return self.get(scene_id).get("status", STATUS_PENDING)

    def is_done(self, scene_id: str) -> bool:
        entry = self.get(scene_id)
        if entry.get("status") != STATUS_DONE:
            return False
        # 影片被刪掉的話不算完成，否則 concat 會找不到檔案。
        video = entry.get("video_path")
        return bool(video and Path(video).is_file())

    def mark(self, scene_id: str, **fields) -> None:
        entry = self.data["scenes"].setdefault(scene_id, {})
        entry.update(fields)
        # 重跑成功的鏡頭不該還掛著上一次的錯誤訊息，否則 status 會誤導人。
        if fields.get("status") in (STATUS_DONE, STATUS_RUNNING):
            entry.pop("error", None)
        self.save()

    def video_path(self, scene_id: str) -> Path | None:
        raw = self.get(scene_id).get("video_path")
        return Path(raw) if raw else None

    def last_frame(self, scene_id: str) -> Path | None:
        raw = self.get(scene_id).get("last_frame")
        return Path(raw) if raw else None

    def total_cost(self) -> float:
        total = 0.0
        for entry in self.data["scenes"].values():
            cost = entry.get("cost")
            if isinstance(cost, (int, float)):
                total += cost
        return total

    def done_ids(self) -> list[str]:
        return [sid for sid in self.data["scenes"] if self.is_done(sid)]


# --- 範本 -------------------------------------------------------------


def write_template(path: Path, *, cast_images: list[Path] | None = None) -> Path:
    """產生專案骨架。若目錄裡有圖片就先填進 cast，省得使用者自己打路徑。"""
    cast = {img.stem: img.name for img in (cast_images or [])[:8]}
    names = list(cast)

    template = {
        "title": path.stem,
        "cast": cast or {"角色名": "角色圖.png"},
        "defaults": {
            "model": DEFAULT_MODEL,
            "size": "480x854",
            "duration": 5,
            "generate_audio": False,
        },
        "scenes": [
            {
                "id": "s01",
                "prompt": "第一個鏡頭的畫面描述：主體 + 動作 + 場景 + 鏡頭運動 + 光線",
                "cast": names[:2],
            },
            {
                "id": "s02",
                "prompt": "第二個鏡頭。加上 continue_from 就會接續前一鏡的最後一格。",
                "cast": names[:1],
                "continue_from": "s01",
            },
        ],
        "output": {"concat": "outputs/%s.mp4" % path.stem},
    }
    path.write_text(json.dumps(template, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def save_project(project: Project, scenes_data: list[dict] | None = None) -> Path:
    """把專案寫回檔案。GUI 編輯後存檔用。

    以 raw 為底更新，未知欄位才不會在存檔時被吃掉——使用者可能手工加了註解性質
    的欄位，工具不該擅自刪除。
    """
    data = dict(project.raw)
    data["title"] = project.title
    data["cast"] = project.cast
    data["defaults"] = project.defaults
    if scenes_data is not None:
        data["scenes"] = scenes_data
    if project.concat_output:
        data.setdefault("output", {})["concat"] = project.concat_output
    project.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return project.path
