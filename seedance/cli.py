"""命令列介面。

    python -m seedance gen "提示詞" [選項]
    python -m seedance batch scenes.json [--concurrency 3] [--concat final.mp4]
    python -m seedance resume <job_id>
    python -m seedance models [--refresh]
    python -m seedance gui
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .capabilities import fetch_models, get_capabilities
from .client import GenerationSpec
from .config import DEFAULT_MODEL, api_key_source, cost_limit_usd, get_api_key
from .errors import SeedanceError
from .runner import concat_videos, generate, generate_batch, load_scenes, prepare, resume


def _fix_console_encoding() -> None:
    """Windows 終端機預設可能是 cp950，中文與 emoji 會炸掉。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="seedance",
        description="用 OpenRouter 的 Seedance 模型生成影片（CLI 與 GUI 共用同一套核心）。",
    )
    sub = parser.add_subparsers(dest="command")

    gen = sub.add_parser("gen", help="生成單支影片")
    gen.add_argument("prompt", nargs="?", default="", help="影片提示詞")
    gen.add_argument("--model", default=DEFAULT_MODEL)
    gen.add_argument("--duration", type=int, default=None, help="秒數，預設 5")
    gen.add_argument("--size", default=None, help="像素尺寸，例如 480x854；預設為最低解析度手機直式")
    gen.add_argument("--resolution", default=None, help="480p / 720p（與 --size 擇一）")
    gen.add_argument("--aspect-ratio", default=None, help="9:16 / 16:9 …（與 --size 擇一）")
    gen.add_argument("--audio", action="store_true", help="同時生成音訊（預設關閉，較省錢）")
    gen.add_argument("--seed", type=int, default=None)
    gen.add_argument("--ref", action="append", default=[], metavar="檔案或網址",
                     help="參考素材，可重複；圖片／影片／音訊皆可")
    gen.add_argument("--first-frame", default=None, help="首影格圖片")
    gen.add_argument("--last-frame", default=None, help="尾影格圖片")
    gen.add_argument("--name", default="", help="輸出檔名標籤")
    gen.add_argument("--out", default=None, help="輸出資料夾，預設 outputs/")
    gen.add_argument("--dry-run", action="store_true", help="只驗參數與估價，不送單、不花錢")
    gen.add_argument("--yes", "-y", action="store_true", help="略過成本護欄確認")

    batch = sub.add_parser("batch", help="依分鏡檔批次生成")
    batch.add_argument("scenes", help="分鏡 JSON 檔")
    batch.add_argument("--model", default=DEFAULT_MODEL)
    batch.add_argument("--concurrency", type=int, default=3)
    batch.add_argument("--concat", default=None, help="全部完成後用 ffmpeg 串成一支，指定輸出檔名")
    batch.add_argument("--out", default=None)
    batch.add_argument("--dry-run", action="store_true")
    batch.add_argument("--yes", "-y", action="store_true")

    resume_cmd = sub.add_parser("resume", help="用 job id 取回先前送出的任務")
    resume_cmd.add_argument("job_id")
    resume_cmd.add_argument("--out", default=None)

    models = sub.add_parser("models", help="列出可用影片模型與能力")
    models.add_argument("--refresh", action="store_true", help="忽略本機快取重新抓取")
    models.add_argument("--model", default=None, help="只看單一型號的詳細能力")

    sub.add_parser("gui", help="開啟圖形介面")
    return parser


def cmd_gen(args) -> int:
    spec = GenerationSpec(
        prompt=args.prompt,
        model=args.model,
        duration=args.duration or 0,
        size=args.size,
        resolution=args.resolution,
        aspect_ratio=args.aspect_ratio,
        generate_audio=args.audio,
        seed=args.seed,
        references=list(args.ref),
        first_frame=args.first_frame,
        last_frame=args.last_frame,
        name=args.name,
    )

    caps = get_capabilities(spec.model)
    if not spec.duration:
        spec.duration = caps.default_duration()
    if not (spec.size or spec.resolution or spec.aspect_ratio):
        spec.size = caps.default_size()

    if args.dry_run:
        _, body, estimate = prepare(spec, caps=caps, log=lambda m: print("  %s" % m))
        print("[dry-run] 參數驗證通過")
        print(estimate.format())
        print("請求體欄位：%s" % ", ".join(sorted(body)))
        if estimate.usd > cost_limit_usd():
            print("注意：超過成本護欄 US$%.2f，實際執行時需要加 --yes" % cost_limit_usd())
        return 0

    result = generate(
        spec,
        api_key=get_api_key(),
        caps=caps,
        approved=args.yes,
        output_dir=Path(args.out) if args.out else None,
    )
    print("影片：%s" % result.video_path)
    print("記錄：%s" % result.record_path)
    return 0


def cmd_batch(args) -> int:
    specs = load_scenes(Path(args.scenes), model=args.model)

    if args.dry_run:
        total = 0.0
        for spec in specs:
            _, _, estimate = prepare(spec, build_body=False)
            total += estimate.usd
            print("[dry-run] %-16s %s" % (spec.name, estimate.format()))
        print("[dry-run] 合計預估 US$%.3f" % total)
        return 0

    results = generate_batch(
        specs,
        api_key=get_api_key(),
        concurrency=args.concurrency,
        approved=args.yes,
        output_dir=Path(args.out) if args.out else None,
    )

    succeeded = [r for r in results if not isinstance(r, Exception)]
    failed = [r for r in results if isinstance(r, Exception)]
    print("\n完成 %d / %d 支" % (len(succeeded), len(results)))
    for result in succeeded:
        print("  ✓ %s" % result.summary())
    for error in failed:
        print("  ✗ %s" % error)

    if args.concat and succeeded:
        concat_videos([r.video_path for r in succeeded], Path(args.concat))

    return 0 if not failed else 1


def cmd_resume(args) -> int:
    path = resume(args.job_id, api_key=get_api_key(), output_dir=Path(args.out) if args.out else None)
    print("影片：%s" % path)
    return 0


def cmd_models(args) -> int:
    entries = fetch_models(force_refresh=args.refresh)
    if args.model:
        caps = get_capabilities(args.model, force_refresh=args.refresh)
        print("%s（%s）" % (caps.name or caps.id, caps.id))
        print("  解析度    ：%s" % ", ".join(caps.supported_resolutions))
        print("  長寬比    ：%s" % ", ".join(caps.supported_aspect_ratios))
        print("  尺寸      ：%s" % ", ".join(caps.sorted_sizes()))
        print("  秒數      ：%s" % ", ".join(str(d) for d in caps.supported_durations))
        print("  首尾影格  ：%s" % (", ".join(caps.supported_frame_images) or "不支援"))
        print("  生成音訊  ：%s" % ("支援" if caps.generate_audio else "不支援"))
        print("  seed      ：%s" % ("支援" if caps.seed else "不支援"))
        print("  計價      ：%s" % caps.pricing_skus)
        print("  預設尺寸  ：%s（本工具挑的最低解析度手機直式）" % caps.default_size())
        return 0

    print("共 %d 個影片模型：" % len(entries))
    for entry in entries:
        print("  %-34s %s" % (entry.get("id"), ", ".join(entry.get("supported_resolutions") or [])))
    print("\n看單一型號細節：python -m seedance models --model %s" % DEFAULT_MODEL)
    return 0


def cmd_gui(_args) -> int:
    from .gui import main as gui_main

    gui_main()
    return 0


def main(argv: list[str] | None = None) -> int:
    _fix_console_encoding()
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        print("\n金鑰來源：%s ／ 成本護欄：US$%.2f" % (api_key_source(), cost_limit_usd()))
        return 0

    handlers = {
        "gen": cmd_gen,
        "batch": cmd_batch,
        "resume": cmd_resume,
        "models": cmd_models,
        "gui": cmd_gui,
    }
    try:
        return handlers[args.command](args)
    except SeedanceError as exc:
        print("\n錯誤：%s" % exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已中斷。若任務已送出，可用 python -m seedance resume <job_id> 取件。", file=sys.stderr)
        return 130
