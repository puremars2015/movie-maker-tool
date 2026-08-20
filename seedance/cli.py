"""命令列介面。

    python -m seedance gen "提示詞" [選項]
    python -m seedance batch scenes.json [--concurrency 3] [--concat final.mp4]
    python -m seedance resume <job_id>
    python -m seedance models [--refresh]
    python -m seedance gui
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .capabilities import fetch_models, get_capabilities
from .client import GenerationSpec, load_job_record
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


# --- --json 模式 ------------------------------------------------------
#
# 給 AI agent 或其他程式呼叫用。約定很簡單：stdout 只有一個 JSON 物件，
# 所有進度訊息改走 stderr。這樣呼叫端可以直接 json.loads(stdout)，
# 不必去剖析人類看的中文輸出。


def _wants_json(args) -> bool:
    return bool(getattr(args, "json", False))


def _emit(args, payload: dict) -> int:
    """輸出結果。JSON 模式回傳單一物件，否則什麼都不做（由呼叫端自行印人類格式）。"""
    if _wants_json(args):
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0 if payload.get("ok", True) else 1


def _logger(args):
    """JSON 模式下把進度訊息導到 stderr，保持 stdout 乾淨。"""
    stream = sys.stderr if _wants_json(args) else sys.stdout
    return lambda message: print(message, file=stream)


def _estimate_payload(estimate) -> dict:
    return {
        "tokens": estimate.tokens,
        "price_per_token": estimate.price_per_token,
        "list_price_usd": round(estimate.usd, 6),
        "sku": estimate.sku,
        "width": estimate.width,
        "height": estimate.height,
        "duration": estimate.duration,
        "note": "牌價上限，未計促銷折扣；實際扣款以 usage.cost 為準",
    }


def _add_json_flag(parser) -> None:
    parser.add_argument(
        "--json",
        action="store_true",
        help="輸出機器可讀的 JSON 到 stdout，進度訊息改走 stderr（供程式或 AI agent 呼叫）",
    )


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

    for command in (gen, batch, resume_cmd, models):
        _add_json_flag(command)
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

    log = _logger(args)
    over_guard_note = "超過成本護欄 US$%.2f，實際執行時需要加 --yes" % cost_limit_usd()

    if args.dry_run:
        _, body, estimate = prepare(spec, caps=caps, log=lambda m: log("  %s" % m))
        exceeds_guard = estimate.usd > cost_limit_usd()

        if _wants_json(args):
            return _emit(args, {
                "ok": True,
                "command": "gen",
                "dry_run": True,
                "validated": True,
                "model": spec.model,
                "size": spec.size,
                "duration": spec.duration,
                "generate_audio": spec.generate_audio,
                "reference_count": len(spec.references),
                "estimate": _estimate_payload(estimate),
                "cost_limit_usd": cost_limit_usd(),
                "exceeds_cost_limit": exceeds_guard,
                "request_fields": sorted(body),
            })

        print("[dry-run] 參數驗證通過")
        print(estimate.format())
        print("請求體欄位：%s" % ", ".join(sorted(body)))
        if exceeds_guard:
            print("注意：%s" % over_guard_note)
        return 0

    result = generate(
        spec,
        api_key=get_api_key(),
        caps=caps,
        approved=args.yes,
        output_dir=Path(args.out) if args.out else None,
        log=log,
    )

    if _wants_json(args):
        return _emit(args, {
            "ok": True,
            "command": "gen",
            "dry_run": False,
            "job_id": result.job.get("id"),
            "video_path": str(result.video_path),
            "record_path": str(result.record_path),
            "elapsed_seconds": round(result.elapsed_s, 1),
            "estimate": _estimate_payload(result.estimate),
            "actual_cost_usd": result.actual_cost,
            "prompt": spec.prompt,
        })

    print("影片：%s" % result.video_path)
    print("記錄：%s" % result.record_path)
    return 0


def cmd_batch(args) -> int:
    specs = load_scenes(Path(args.scenes), model=args.model)
    log = _logger(args)

    if args.dry_run:
        total = 0.0
        scenes = []
        for spec in specs:
            _, _, estimate = prepare(spec, build_body=False)
            total += estimate.usd
            scenes.append({"name": spec.name, "estimate": _estimate_payload(estimate)})
            if not _wants_json(args):
                print("[dry-run] %-16s %s" % (spec.name, estimate.format()))

        if _wants_json(args):
            return _emit(args, {
                "ok": True,
                "command": "batch",
                "dry_run": True,
                "scene_count": len(specs),
                "scenes": scenes,
                "total_list_price_usd": round(total, 6),
                "cost_limit_usd": cost_limit_usd(),
                "exceeds_cost_limit": total > cost_limit_usd(),
            })

        print("[dry-run] 合計預估 US$%.3f" % total)
        return 0

    results = generate_batch(
        specs,
        api_key=get_api_key(),
        concurrency=args.concurrency,
        approved=args.yes,
        output_dir=Path(args.out) if args.out else None,
        log=log,
    )

    succeeded = [r for r in results if not isinstance(r, Exception)]
    failed = [r for r in results if isinstance(r, Exception)]

    concat_path = None
    if args.concat and succeeded:
        concat_path = concat_videos([r.video_path for r in succeeded], Path(args.concat), log=log)

    if _wants_json(args):
        return _emit(args, {
            "ok": not failed,
            "command": "batch",
            "dry_run": False,
            "succeeded": len(succeeded),
            "total": len(results),
            "videos": [
                {
                    "name": r.spec.name,
                    "job_id": r.job.get("id"),
                    "video_path": str(r.video_path),
                    "actual_cost_usd": r.actual_cost,
                    "elapsed_seconds": round(r.elapsed_s, 1),
                }
                for r in succeeded
            ],
            "failures": [str(e) for e in failed],
            "concat_path": str(concat_path) if concat_path else None,
        })

    print("\n完成 %d / %d 支" % (len(succeeded), len(results)))
    for result in succeeded:
        print("  ✓ %s" % result.summary())
    for error in failed:
        print("  ✗ %s" % error)

    return 0 if not failed else 1


def cmd_resume(args) -> int:
    path = resume(
        args.job_id,
        api_key=get_api_key(),
        output_dir=Path(args.out) if args.out else None,
        log=_logger(args),
    )

    if _wants_json(args):
        record = load_job_record(args.job_id)
        return _emit(args, {
            "ok": True,
            "command": "resume",
            "job_id": args.job_id,
            "video_path": str(path),
            "actual_cost_usd": (record.get("usage") or {}).get("cost"),
        })

    print("影片：%s" % path)
    return 0


def cmd_models(args) -> int:
    entries = fetch_models(force_refresh=args.refresh)

    if _wants_json(args):
        if args.model:
            caps = get_capabilities(args.model, force_refresh=args.refresh)
            return _emit(args, {
                "ok": True,
                "command": "models",
                "model": caps.id,
                "name": caps.name,
                "supported_resolutions": caps.supported_resolutions,
                "supported_aspect_ratios": caps.supported_aspect_ratios,
                "supported_sizes": caps.sorted_sizes(),
                "supported_durations": caps.supported_durations,
                "supported_frame_images": caps.supported_frame_images,
                "generate_audio": caps.generate_audio,
                "seed": caps.seed,
                "pricing_skus": caps.pricing_skus,
                "default_size": caps.default_size(),
                "default_duration": caps.default_duration(),
            })
        return _emit(args, {
            "ok": True,
            "command": "models",
            "count": len(entries),
            "models": [
                {"id": e.get("id"), "resolutions": e.get("supported_resolutions") or []}
                for e in entries
            ],
        })

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
        if _wants_json(args):
            payload = {
                "ok": False,
                "command": args.command,
                "error": {"type": type(exc).__name__, "message": str(exc)},
            }
            # 逾時的話把 job id 帶出來，呼叫端才知道可以用 resume 取件（影片已計費）。
            job_id = getattr(exc, "job_id", None)
            if job_id:
                payload["error"]["job_id"] = job_id
                payload["error"]["recoverable_with"] = "resume"
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 1
        print("\n錯誤：%s" % exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已中斷。若任務已送出，可用 python -m seedance resume <job_id> 取件。", file=sys.stderr)
        return 130
