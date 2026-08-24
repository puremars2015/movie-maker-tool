"""專案執行：排程、接續、失敗即停。

排程要同時處理兩種鏡頭：標了 continue_from 的必須等前一鏡完成（要拿它的最後一格
當首影格），其餘可以並行。所以這裡不是單純的 thread pool map，而是一個「誰的依賴
好了就送誰進池子」的迴圈。

失敗即停的語意要小心：停的是「不再送出新的鏡頭」，而不是丟掉正在跑的。任務一旦
送出就已經計費，硬中斷只會讓那筆錢白花，所以在途的會等它完成並下載完再收工。
"""

from __future__ import annotations

import shutil
import subprocess
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import cost as cost_module
from .capabilities import ModelCapabilities, get_capabilities
from .config import cost_limit_usd, ensure_dirs
from .errors import SeedanceError, ValidationError
from .project import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    Project,
    ProjectState,
    Scene,
)
from .runner import concat_videos, generate, prepare

Logger = Callable[[str], None]

# 估價階段用的佔位字串。build_body=False 時不會去讀檔，所以不會有副作用；
# 它只是讓「這一鏡待會兒會有首影格」這件事在驗證時成立。
PENDING_FRAME = "<continue_from>"


@dataclass
class SceneEstimate:
    scene_id: str
    estimate: cost_module.CostEstimate
    already_done: bool = False


@dataclass
class ProjectPlan:
    """check 與 run 共用的前置計算結果。全程免費，不碰生成 API。"""

    estimates: list[SceneEstimate] = field(default_factory=list)
    todo: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def todo_cost(self) -> float:
        done = set(self.skipped)
        return sum(e.estimate.usd for e in self.estimates if e.scene_id not in done)

    @property
    def todo_expected(self) -> float:
        """預期實際扣款。逐鏡套各自模型的實測折扣——專案可以混用不同模型，
        整批乘同一個係數會算錯。"""
        done = set(self.skipped)
        return sum(e.estimate.expected_usd for e in self.estimates if e.scene_id not in done)

    @property
    def total_cost(self) -> float:
        return sum(e.estimate.usd for e in self.estimates)


@dataclass
class ProjectRunResult:
    completed: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    not_started: list[str] = field(default_factory=list)
    concat_path: Path | None = None
    spent_usd: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.failed and not self.not_started


# --- 規劃（免費） -----------------------------------------------------


def plan_project(
    project: Project,
    state: ProjectState,
    *,
    only: list[str] | None = None,
    force: bool = False,
) -> ProjectPlan:
    """驗證每一鏡並估價。任何錯誤都在這裡就爆出來，不會花到錢。"""
    plan = ProjectPlan()
    caps_cache: dict[str, ModelCapabilities] = {}

    selected = _select(project, only)

    for scene in project.scenes:
        if scene.id not in selected:
            continue

        _check_assets_exist(scene)

        caps = caps_cache.setdefault(scene.model, get_capabilities(scene.model))
        spec = scene.to_spec()
        if scene.continue_from and not spec.first_frame:
            spec.first_frame = PENDING_FRAME

        try:
            _, _, estimate = prepare(spec, caps=caps, build_body=False)
        except SeedanceError as exc:
            raise ValidationError("分鏡 %s：%s" % (scene.id, exc)) from exc

        # prepare 會把預設值補進 spec，寫回 scene 讓後續執行沿用同一組參數。
        scene.duration = spec.duration
        scene.size = spec.size

        done = state.is_done(scene.id) and not force
        plan.estimates.append(SceneEstimate(scene.id, estimate, already_done=done))
        (plan.skipped if done else plan.todo).append(scene.id)

    _check_dependencies_runnable(project, state, plan, force=force)
    return plan


def _check_assets_exist(scene: Scene) -> None:
    """確認本機素材真的在。

    check 的意義就是把問題全部攔在花錢之前，所以路徑打錯不能等到 run 才發現。
    網址不在這裡驗證——連線失敗與檔案不存在是不同的問題，而且驗證網址要發請求。
    """
    from .media import is_url

    missing = []
    for source in list(scene.references) + [scene.first_frame, scene.last_frame]:
        if not source or is_url(source):
            continue
        if not Path(source).is_file():
            missing.append(source)

    if missing:
        raise ValidationError(
            "分鏡 %s 找不到這些素材：\n  - %s\n"
            "路徑以專案檔所在目錄為基準，不是你執行指令的目錄。"
            % (scene.id, "\n  - ".join(missing))
        )


def _select(project: Project, only: list[str] | None) -> set[str]:
    ids = {scene.id for scene in project.scenes}
    if not only:
        return ids
    unknown = [sid for sid in only if sid not in ids]
    if unknown:
        raise ValidationError(
            "專案裡沒有這些分鏡：%s。現有的是：%s" % (", ".join(unknown), ", ".join(sorted(ids)))
        )
    return set(only)


def _check_dependencies_runnable(
    project: Project, state: ProjectState, plan: ProjectPlan, *, force: bool
) -> None:
    """接續的前一鏡若既不在待辦、也還沒完成，這一鏡永遠等不到，先擋下來。"""
    runnable = set(plan.todo) | set(plan.skipped)
    for scene_id in plan.todo:
        scene = project.scene_by_id(scene_id)
        dep = scene.continue_from if scene else None
        if not dep:
            continue
        if dep in runnable or state.is_done(dep):
            continue
        raise ValidationError(
            "分鏡 %s 要接續 %s，但 %s 既不在這次的執行範圍、也還沒完成。\n"
            "請把 %s 一起納入（例如 --only %s,%s），或先單獨跑它。"
            % (scene_id, dep, dep, dep, dep, scene_id)
        )


# --- 執行 -------------------------------------------------------------


def run_project(
    project: Project,
    state: ProjectState,
    *,
    api_key: str,
    concurrency: int = 3,
    approved: bool = False,
    only: list[str] | None = None,
    force: bool = False,
    log: Logger = print,
    should_cancel: Callable[[], bool] | None = None,
    output_dir: Path | None = None,
) -> ProjectRunResult:
    """轉出專案。預設只處理未完成的鏡頭，所以重跑不會重複計費。"""
    ensure_dirs()
    plan = plan_project(project, state, only=only, force=force)
    result = ProjectRunResult(skipped=list(plan.skipped))

    if plan.skipped:
        log("跳過已完成的 %d 鏡：%s" % (len(plan.skipped), ", ".join(plan.skipped)))
    if not plan.todo:
        log("沒有需要生成的鏡頭。")
        result.concat_path = _maybe_concat(project, state, log=log)
        return result

    if abs(plan.todo_expected - plan.todo_cost) > 1e-9:
        log("本次要生成 %d 鏡，牌價 US$%.3f，依實測折扣預期約 US$%.3f"
            % (len(plan.todo), plan.todo_cost, plan.todo_expected))
    else:
        log("本次要生成 %d 鏡，預估 US$%.3f" % (len(plan.todo), plan.todo_cost))
    limit = cost_limit_usd()
    if not approved and plan.todo_cost > limit:
        raise cost_module.CostGuardError(
            "本次預估 US$%.3f 超過門檻 US$%.2f，請確認後再執行。" % (plan.todo_cost, limit)
        )

    caps_cache: dict[str, ModelCapabilities] = {}
    todo = list(plan.todo)
    remaining = {sid for sid in todo}
    running: dict = {}
    stop = False

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        while remaining or running:
            if not stop and should_cancel and should_cancel():
                stop = True
                log("收到停止指示，不再送出新的鏡頭（在途的會等它完成，因為已經計費）。")

            # 依賴滿足且尚未送出的，補進池子。
            if not stop:
                for scene_id in _ready(project, state, remaining, running):
                    if len(running) >= max(1, concurrency):
                        break
                    scene = project.scene_by_id(scene_id)
                    remaining.discard(scene_id)
                    caps = caps_cache.setdefault(scene.model, get_capabilities(scene.model))
                    state.mark(scene_id, status=STATUS_RUNNING)
                    future = pool.submit(
                        _run_scene, project, state, scene, caps, api_key, log, output_dir
                    )
                    running[future] = scene_id

            if not running:
                # 沒有在途、也沒有可送出的：不是全部做完，就是被 fail-fast 擋住。
                break

            done_futures, _ = wait(list(running), return_when=FIRST_COMPLETED)
            for future in done_futures:
                scene_id = running.pop(future)
                try:
                    outcome = future.result()
                except SeedanceError as exc:
                    stop = True
                    state.mark(scene_id, status=STATUS_FAILED, error=str(exc))
                    result.failed.append((scene_id, str(exc)))
                    log("[%s] 失敗：%s" % (scene_id, exc))
                    log("依設定立即停止，不再送出新的鏡頭。修正後重跑會自動跳過已完成的。")
                else:
                    result.completed.append(scene_id)
                    result.spent_usd += outcome or 0.0

    result.not_started = sorted(remaining)
    if result.not_started and not result.failed:
        log("尚未開始：%s" % ", ".join(result.not_started))

    if result.failed:
        log("本次完成 %d 鏡、失敗 1 鏡、未開始 %d 鏡。" % (len(result.completed), len(result.not_started)))
    else:
        log("本次完成 %d 鏡。" % len(result.completed))

    result.concat_path = _maybe_concat(project, state, log=log)
    return result


def _ready(project: Project, state: ProjectState, remaining: set[str], running: dict) -> list[str]:
    """回傳依賴已滿足、可以立刻送出的鏡頭，維持專案檔的順序。"""
    in_flight = set(running.values())
    ready = []
    for scene in project.scenes:
        if scene.id not in remaining or scene.id in in_flight:
            continue
        dep = scene.continue_from
        if dep and not state.is_done(dep):
            continue
        ready.append(scene.id)
    return ready


def _run_scene(
    project: Project,
    state: ProjectState,
    scene: Scene,
    caps: ModelCapabilities,
    api_key: str,
    log: Logger,
    output_dir: Path | None,
) -> float:
    """生成單一鏡頭，成功後抽出最後一格供後續接續使用。回傳實際花費。"""
    prefix = "[%s]" % scene.id
    spec = scene.to_spec()

    if scene.continue_from:
        frame = ensure_last_frame(project, state, scene.continue_from, log=log)
        spec.first_frame = str(frame)
        log("%s 接續 %s 的最後一格" % (prefix, scene.continue_from))

    result = generate(
        spec,
        api_key=api_key,
        caps=caps,
        approved=True,  # 總額已在 run_project 開頭一次確認
        log=lambda message: log("%s %s" % (prefix, message)),
        output_dir=output_dir,
    )

    cost = result.actual_cost if isinstance(result.actual_cost, (int, float)) else None
    state.mark(
        scene.id,
        status=STATUS_DONE,
        job_id=result.job.get("id"),
        video_path=str(result.video_path),
        cost=cost,
        completed_at=time.strftime("%Y-%m-%d %H:%M:%S"),
    )

    # 只有真的有人要接續才抽格，省下不必要的 ffmpeg 呼叫。
    if any(other.continue_from == scene.id for other in project.scenes):
        ensure_last_frame(project, state, scene.id, log=log)

    return cost or 0.0


def _maybe_concat(project: Project, state: ProjectState, *, log: Logger) -> Path | None:
    """全部鏡頭都完成才合併；缺鏡時合出來的片是錯的，不如不合。"""
    if not project.concat_output:
        return None

    missing = [scene.id for scene in project.scenes if not state.is_done(scene.id)]
    if missing:
        log("尚有 %d 鏡未完成（%s），暫不合併。" % (len(missing), ", ".join(missing)))
        return None

    paths = [state.video_path(scene.id) for scene in project.scenes]
    dest = Path(project.concat_output)
    if not dest.is_absolute():
        dest = project.root / dest
    return concat_videos([p for p in paths if p], dest, log=log)


# --- 影格抽取 ---------------------------------------------------------


def ensure_last_frame(project: Project, state: ProjectState, scene_id: str, *, log: Logger) -> Path:
    """取得某一鏡的最後一格，必要時用 ffmpeg 抽出來並快取。"""
    cached = state.last_frame(scene_id)
    if cached and cached.is_file():
        return cached

    video = state.video_path(scene_id)
    if not video or not video.is_file():
        raise ValidationError(
            "要接續 %s，但找不到它的影片檔。請先完成該鏡，或移除 continue_from。" % scene_id
        )

    project.frames_dir.mkdir(parents=True, exist_ok=True)
    dest = project.frames_dir / ("%s_last.png" % scene_id)
    extract_last_frame(video, dest, log=log)
    state.mark(scene_id, last_frame=str(dest))
    return dest


def extract_last_frame(video: Path, dest: Path, *, log: Logger = print) -> Path:
    """抽出影片的最後一格。

    -sseof -1 先跳到結尾前一秒，再用 -update 1 讓每張後續影格覆寫同一個檔案，
    迴圈跑完留下的就是最後一格。比先用 ffprobe 問長度再 seek 少一次呼叫，
    也不會因為時間軸有些微誤差而抽到黑畫面。
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ValidationError(
            "鏡頭接續（continue_from）需要 ffmpeg 才能抽出前一鏡的最後一格，"
            "但系統找不到 ffmpeg。請安裝，或移除專案檔裡的 continue_from。"
        )

    dest.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-sseof", "-1", "-i", str(video),
        "-update", "1", "-q:v", "2", str(dest),
    ]
    process = subprocess.run(command, capture_output=True, text=True, errors="replace")

    if process.returncode != 0 or not dest.is_file():
        raise ValidationError(
            "抽取 %s 的最後一格失敗：%s" % (video.name, (process.stderr or "").strip()[-300:])
        )
    log("已抽出 %s 的最後一格" % video.name)
    return dest


# --- 狀態摘要 ---------------------------------------------------------


def summarize(project: Project, state: ProjectState) -> dict:
    scenes = []
    for scene in project.scenes:
        entry = state.get(scene.id)
        scenes.append({
            "id": scene.id,
            "status": entry.get("status", STATUS_PENDING),
            "duration": scene.duration,
            "size": scene.size,
            "cast": scene.cast,
            "continue_from": scene.continue_from,
            "prompt": scene.prompt,
            "video_path": entry.get("video_path"),
            "cost": entry.get("cost"),
            "error": entry.get("error"),
        })

    counts = {STATUS_DONE: 0, STATUS_FAILED: 0, STATUS_PENDING: 0, STATUS_RUNNING: 0}
    for item in scenes:
        counts[item["status"]] = counts.get(item["status"], 0) + 1

    return {
        "title": project.title,
        "project_path": str(project.path),
        "scene_count": len(project.scenes),
        "counts": counts,
        "spent_usd": round(state.total_cost(), 6),
        "scenes": scenes,
    }
