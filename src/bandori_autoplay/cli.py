"""命令行入口。

常用命令::

    python -m bandori_autoplay.cli recon            # 设备侦察
    python -m bandori_autoplay.cli shot out.png     # 截图
    python -m bandori_autoplay.cli calibrate        # 自动标定轨道
    python -m bandori_autoplay.cli touch-selftest    # 触控层自检（不碰游戏）
    python -m bandori_autoplay.cli demo-chart        # 生成示例谱面
    python -m bandori_autoplay.cli play charts/x.json --dry-run
    python -m bandori_autoplay.cli vision --seconds 20
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .adb import PACKAGE, AdbDevice
from .chart import load_chart, make_demo_chart
from .config import lane_x, load_config, save_config
from .logging_utils import setup_logging
from .player import ChartPlayer, VisionPlayer
from .autoplay import VisionAutoplay
from .touch import make_touch
from .vision import LaneGeometry, VisionConfig, auto_lane_positions


def _device(cfg: dict) -> AdbDevice:
    return AdbDevice(
        serial=cfg.get("device", {}).get("serial"),
        adb_path=cfg.get("device", {}).get("adb_path"),
    )


# --------------------------------------------------------------------------


def cmd_recon(args) -> int:
    cfg = load_config(args.config)
    dev = _device(cfg)
    info = dev.info()
    print("=== 设备侦察 ===")
    print(f"序列号 : {info.serial}")
    print(f"型号   : {info.brand} {info.model}")
    print(f"Android: {info.android} (SDK {info.sdk})")
    print(f"分辨率 : {info.width}x{info.height} @ {info.density}dpi")
    print()
    print("设备列表:")
    for serial, state in dev.devices():
        print(f"  {serial}  {state}")
    print()
    installed = dev.shell(f"pm list packages | grep -i {PACKAGE}")
    print(f"游戏安装: {'是' if PACKAGE in installed else '否'} ({PACKAGE})")
    focus = dev.current_focus()
    print(f"当前前台: {focus}")
    print()
    print("触控设备:")
    out = dev.shell("getevent -pl")
    cur = None
    for line in out.splitlines():
        if "add device" in line:
            cur = line.strip()
        elif "name:" in line and cur:
            print(f"  {cur}  {line.strip()}")
            cur = None
    print()
    print("游戏数据目录:")
    base = f"/sdcard/Android/data/{PACKAGE}/files"
    for line in dev.shell(f"ls -la {base} 2>/dev/null").splitlines():
        print(f"  {line}")
    return 0


def cmd_shot(args) -> int:
    cfg = load_config(args.config)
    dev = _device(cfg)
    path = dev.screenshot(args.path)
    print(f"已保存: {path.resolve()} ({path.stat().st_size} bytes)")
    return 0


def cmd_calibrate(args) -> int:
    cfg = load_config(args.config)
    dev = _device(cfg)
    from .adb import AdbError

    if args.image:
        import cv2

        frame = cv2.imread(args.image)
        if frame is None:
            print(f"无法读取图片: {args.image}")
            return 1
    else:
        frame = dev.screenshot_array()

    lane_count = int(cfg["lanes"]["count"])
    line_y = args.line_y or cfg["judgement"].get("line_y") or int(frame.shape[0] * 0.82)
    xs = auto_lane_positions(frame, lane_count, line_y)
    print(f"分辨率 {frame.shape[1]}x{frame.shape[0]}")
    print(f"判定线 y = {line_y}")
    print(f"轨道 x 坐标 = {xs}")
    if args.save:
        cfg["lanes"]["x"] = xs
        cfg["judgement"]["line_y"] = line_y
        save_config(cfg, args.config or Path("config/default.yaml"))
        print(f"已写入配置: {args.config or 'config/default.yaml'}")
    if args.visualize:
        import cv2

        vis = frame.copy()
        for i, x in enumerate(xs):
            cv2.line(vis, (x, 0), (x, vis.shape[0]), (0, 255, 0), 2)
            cv2.putText(vis, str(i), (x - 8, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 255), 2)
        cv2.line(vis, (0, line_y), (vis.shape[1], line_y), (0, 0, 255), 3)
        out = args.visualize
        cv2.imwrite(out, vis)
        print(f"可视化输出: {out}")
    return 0


def cmd_touch_selftest(args) -> int:
    """不触碰游戏：打开系统的「指针位置」叠加层，注入多点触控并截图验证。"""
    cfg = load_config(args.config)
    dev = _device(cfg)
    touch = make_touch(cfg["touch"], dev)
    info = dev.info()
    w, h = info.height, info.width  # 横屏
    print("开启系统指针位置叠加层（settings put system pointer_location 1）")
    dev.shell("settings put system pointer_location 1")
    time.sleep(1.0)
    try:
        print("注入单点触摸 ...")
        touch.down(0, w // 2, h // 2)
        time.sleep(0.6)
        shot1 = Path("debug/shots/touch_tap.png")
        dev.screenshot(shot1)
        touch.up(0)
        print(f"  截图: {shot1.resolve()}")

        print("注入三指同时按下 ...")
        for i, frac in enumerate((0.3, 0.5, 0.7)):
            touch.down(i, int(w * frac), int(h * 0.4))
        time.sleep(0.6)
        shot2 = Path("debug/shots/touch_three.png")
        dev.screenshot(shot2)
        for i in range(3):
            touch.up(i)
        print(f"  截图: {shot2.resolve()}")
    finally:
        touch.close()
        dev.shell("settings put system pointer_location 0")
    print("自检结束（叠加层已关闭）")
    return 0


def cmd_demo_chart(args) -> int:
    chart = make_demo_chart()
    path = chart.save(args.out)
    print(f"已生成示例谱面: {path.resolve()} ({len(chart.notes)} 个音符)")
    return 0


def cmd_play(args) -> int:
    cfg = load_config(args.config)
    log = setup_logging(cfg["logging"]["level"], cfg["logging"]["dir"])
    dev = _device(cfg)
    chart = load_chart(args.chart)
    if args.speed:
        for n in chart.notes:
            n.time_ms = int(n.time_ms / args.speed)
            n.duration_ms = int(n.duration_ms / args.speed)
    xs = lane_x(cfg)
    touch = make_touch(cfg["touch"], dev)
    player = ChartPlayer(
        dev,
        touch,
        xs,
        int(cfg["judgement"]["line_y"]),
        global_offset_ms=int(args.offset if args.offset is not None else cfg["judgement"]["global_offset_ms"]),
        tap_duration_ms=int(cfg["touch"]["tap_duration_ms"]),
        drop_hold_tail=bool(cfg["judgement"]["drop_hold_tail"]),
        assist_mode=bool(cfg["judgement"]["assist_mode"]),
        logger=log.info,
    )
    try:
        player.play(chart, lead_in_ms=args.lead_in, dry_run=args.dry_run)
    finally:
        touch.close()
    return 0


def cmd_vision(args) -> int:
    cfg = load_config(args.config)
    log = setup_logging(cfg["logging"]["level"], cfg["logging"]["dir"])
    dev = _device(cfg)
    xs = lane_x(cfg)
    geo = LaneGeometry(int(cfg["lanes"]["count"]), xs, int(cfg["judgement"]["line_y"]))
    vcfg = VisionConfig(
        detect_roi_top=int(cfg["vision"]["detect_roi_top"]),
        detect_roi_bottom=int(cfg["vision"]["detect_roi_bottom"]),
        min_area=float(cfg["vision"]["min_area"]),
        lane_tolerance=float(cfg["vision"]["lane_tolerance"]),
    )
    touch = make_touch(cfg["touch"], dev)
    debug_dir = None
    if args.save_debug:
        debug_dir = args.save_debug
        Path(debug_dir).mkdir(parents=True, exist_ok=True)
    player = VisionPlayer(
        dev,
        touch,
        geo,
        vcfg,
        trigger_offset_px=args.trigger,
        debug_dir=debug_dir,
        logger=log.info,
    )
    try:
        stats = player.run(args.seconds, dry_run=args.dry_run, save_debug_every=args.every)
    finally:
        touch.close()
    print(json.dumps(stats, ensure_ascii=False, indent=1))
    return 0


def cmd_stream_test(args) -> int:
    """只测实时流帧率，不触控。"""
    cfg = load_config(args.config)
    dev = _device(cfg)
    from .capture import ScreenStream

    stream = ScreenStream(dev, size=args.size, time_limit=int(args.seconds) + 10)
    stream.start()
    try:
        time.sleep(args.seconds)
        frame = stream.latest(timeout=2.0)
        print(f"帧率 ≈ {stream.fps():.1f} fps")
        print(f"最新帧: {None if frame is None else frame.image.shape}")
        if frame is not None and args.save:
            import cv2

            cv2.imwrite(args.save, frame.image)
            print(f"已保存 {args.save}")
    finally:
        stream.stop()
    return 0


def cmd_auto(args) -> int:
    """实时视觉自动演奏（不需要谱面）。"""
    cfg = load_config(args.config)
    log = setup_logging(cfg["logging"]["level"], cfg["logging"]["dir"])
    dev = _device(cfg)
    info = dev.info()
    screen_w, screen_h = info.height, info.width  # 横屏
    touch = make_touch(cfg["touch"], dev)
    debug_dir = args.save_debug
    if debug_dir:
        Path(debug_dir).mkdir(parents=True, exist_ok=True)
    auto = VisionAutoplay(
        dev,
        touch,
        screen_w=screen_w,
        screen_h=screen_h,
        judge_y=int(args.line_y if args.line_y is not None else cfg["judgement"]["line_y"]),
        stream_size=cfg["vision"].get("stream_size", "1280x592"),
        lead_ms=float(args.lead if args.lead is not None else cfg["vision"].get("lead_ms", 45)),
        assist_mode=bool(cfg["judgement"]["assist_mode"]),
        tap_ms=int(cfg["touch"]["tap_duration_ms"]),
        lane_center_x=float(cfg["lanes"].get("center_x", 1253.0)),
        lane_pitch=float(cfg["lanes"].get("pitch", 295.0)),
        lane_count=int(cfg["lanes"]["count"]),
        snap=not args.no_snap,
        debug_dir=debug_dir,
        logger=log.info,
    )
    try:
        auto.run(args.seconds, dry_run=args.dry_run, save_debug_every=args.every)
    finally:
        touch.close()
    return 0


def _chart_path(cfg: dict, music: int | None, difficulty: str) -> Path:
    root = Path(cfg.get("chartdb", {}).get("dir", "work/chartdb/out"))
    return root / "charts" / str(music) / f"{difficulty}.json"


def cmd_play(args) -> int:
    """谱面驱动的精确演奏（chartdb 谱面 + scrcpy 低延迟触控）。"""
    from .chartdb import FieldGeometry, load_events
    from .performer import ChartPerformer

    cfg = load_config(args.config)
    log = setup_logging(cfg["logging"]["level"], cfg["logging"]["dir"])
    dev = _device(cfg)
    info = dev.info()
    screen_w, screen_h = info.height, info.width      # 横屏

    chart = Path(args.chart) if args.chart else _chart_path(cfg, args.music, args.difficulty)
    if not chart.exists():
        log.info(f"找不到谱面文件: {chart}")
        return 2
    events = load_events(chart)
    log.info(f"谱面 {chart}：{len(events)} 个落指事件")

    if args.delay is None:
        args.delay = float(cfg.get("game", {}).get("start_delay_s", 10.0))

    stream_size = cfg["vision"].get("stream_size", "1280x592")
    sw, sh = (int(v) for v in stream_size.lower().split("x"))
    geom = FieldGeometry((sw, sh), screen_w, screen_h)
    log.info(f"几何：判定线 y_stream={geom.judge_y:.1f} -> 设备 y={geom.device_judge_y}；"
             f"中心轨 x={geom.device_x(12)}")

    touch = make_touch(
        cfg["touch"], dev, screen_w=screen_w, screen_h=screen_h,
        jar=cfg["touch"].get("scrcpy_jar"),
    )
    if hasattr(touch, "start"):
        if not args.dry_run:
            touch.start()
    auto_start = None
    start_spec = args.start
    if start_spec is None:
        btn = cfg.get("game", {}).get("live_start_button")
        if btn:
            start_spec = f"{int(btn[0])},{int(btn[1])}"
    if start_spec:
        auto_start = tuple(int(v) for v in start_spec.split(","))  # type: ignore[assignment]
    if auto_start:
        log.info(f"会自动点击开演按钮 {auto_start}")

    performer = ChartPerformer(
        dev, touch, events, geom,
        screen_w=screen_w, screen_h=screen_h, stream_size=stream_size,
        offset_ms=args.offset, tap_ms=int(cfg["touch"]["tap_duration_ms"]),
        live_appear_offset_ms=float(cfg.get("game", {}).get("live_appear_offset_s", 2.1)) * 1000.0,
        hold_pad_ms=int(cfg["touch"].get("hold_release_pad_ms", 25)),
        dry_run=args.dry_run, logger=log.info,
    )
    try:
        performer.run(
            timeout=args.timeout, start_button=auto_start,
            start_delay=args.delay,
            sync={"manual": "manual", "auto": "auto", "fixed": False}[args.sync_mode],
            sync_timeout=args.sync_timeout, min_votes=args.min_votes,
        )
    finally:
        touch.close()
    return 0


def cmd_songs(args) -> int:
    """列出可用曲目与难度，方便在脚本里挑 --music / --difficulty。"""
    import json

    cfg = load_config(args.config)
    root = Path(cfg.get("chartdb", {}).get("dir", "work/chartdb/out"))
    master = root.parent.parent / "master"
    music_p = master / "MasterLiveMusic.json"
    text_p = master / "MasterText.json"
    if not music_p.exists():
        print(f"找不到 masterdata：{music_p}")
        return 2
    music = {r["_id"]: r for r in json.loads(music_p.read_text(encoding="utf-8"))["_allData"]}
    text = {}
    if text_p.exists():
        for r in json.loads(text_p.read_text(encoding="utf-8"))["_allData"]:
            text[r["_id"]] = r
    have = {}
    for p in sorted((root / "charts").glob("*/*.json")):
        have.setdefault(int(p.parent.name), []).append(p.stem)
    order = {"easy": 0, "normal": 1, "hard": 2, "expert": 3}
    print(f"{'musicId':>8}  {'难度':<28} 标题")
    for mid, diffs in sorted(have.items()):
        row = music.get(mid, {})
        t = text.get(row.get("_titleTextID", ""), {})
        title = t.get("_traditionalChinese") or t.get("_japanese") or ""
        diffs = sorted(diffs, key=lambda d: order.get(d, 9))
        print(f"{mid:>8}  {','.join(diffs):<28} {title}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="bandori-autoplay", description="BanG Dream! Our Notes 自动演奏")
    ap.add_argument("--config", default=None, help="配置文件路径")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("recon", help="设备与游戏侦察")
    p.set_defaults(func=cmd_recon)

    p = sub.add_parser("shot", help="截图")
    p.add_argument("path", nargs="?", default="debug/shots/shot.png")
    p.set_defaults(func=cmd_shot)

    p = sub.add_parser("calibrate", help="标定轨道与判定线")
    p.add_argument("--image", help="使用已有截图而不是实时截图")
    p.add_argument("--line-y", type=int, default=None)
    p.add_argument("--save", action="store_true", help="写回配置文件")
    p.add_argument("--visualize", default=None, help="输出可视化图片路径")
    p.set_defaults(func=cmd_calibrate)

    p = sub.add_parser("touch-selftest", help="触控层自检（指针位置叠加层）")
    p.set_defaults(func=cmd_touch_selftest)

    p = sub.add_parser("demo-chart", help="生成示例谱面")
    p.add_argument("--out", default="charts/sample_demo.json")
    p.set_defaults(func=cmd_demo_chart)

    p = sub.add_parser("play", help="按谱面演奏")
    p.add_argument("chart")
    p.add_argument("--dry-run", action="store_true", help="只走时间轴不触控")
    p.add_argument("--lead-in", type=int, default=3000)
    p.add_argument("--offset", type=int, default=None, help="覆盖全局偏移(ms)")
    p.add_argument("--speed", type=float, default=None, help="时间轴倍速（调试用）")
    p.set_defaults(func=cmd_play)

    p = sub.add_parser("vision", help="视觉驱动演奏")
    p.add_argument("--seconds", type=float, default=20.0)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--trigger", type=int, default=60, help="在判定线上方多少像素触发")
    p.add_argument("--save-debug", default=None, help="保存可视化帧的目录")
    p.add_argument("--every", type=int, default=0, help="每 N 帧保存一帧")
    p.set_defaults(func=cmd_vision)

    p = sub.add_parser("stream-test", help="测试实时屏幕流帧率")
    p.add_argument("--seconds", type=float, default=5.0)
    p.add_argument("--size", default="1280x592")
    p.add_argument("--save", default=None, help="保存最新一帧到文件")
    p.set_defaults(func=cmd_stream_test)

    p = sub.add_parser("auto", help="实时视觉自动演奏")
    p.add_argument("--seconds", type=float, default=60.0)
    p.add_argument("--dry-run", action="store_true", help="只预测不触控")
    p.add_argument("--lead", type=float, default=None, help="提前量(ms)")
    p.add_argument("--line-y", type=int, default=None, help="覆盖判定线 y")
    p.add_argument("--save-debug", default=None)
    p.add_argument("--every", type=int, default=0, help="每 N 帧存一张可视化")
    p.add_argument("--no-snap", action="store_true", help="关闭轨中心吸附")
    p.set_defaults(func=cmd_auto)

    p = sub.add_parser("perform", help="谱面驱动的精确演奏（chartdb + scrcpy 触控）")
    p.add_argument("chart", nargs="?", default=None, help="chartdb 谱面 JSON 路径")
    p.add_argument("--music", type=int, default=100004, help="曲目 ID（未给 chart 时使用）")
    p.add_argument("--difficulty", default="easy", help="easy|normal|hard|expert")
    p.add_argument("--start", default=None, help="开始键坐标 x,y（默认读配置 game.live_start_button）")
    p.add_argument("--delay", type=float, default=None, help="点完开始键后等多少秒才开始落指")
    p.add_argument("--offset", type=float, default=0.0, help="整体提前/延后 ms（正=提前）")
    p.add_argument("--sync-mode", choices=["auto", "manual", "fixed"], default="auto",
                   help="时间基准：auto=自动检测演奏画面（默认，失败转手动）"
                        "| manual=按回车打拍 | fixed=固定等待")
    p.add_argument("--timeout", type=float, default=120.0, help="演奏总时长上限（秒）")
    p.add_argument("--sync-timeout", type=float, default=25.0, help="同步超时（秒）")
    p.add_argument("--min-votes", type=int, default=4, help="同步所需最少票数")
    p.add_argument("--dry-run", action="store_true", help="只排程不触控")
    p.set_defaults(func=cmd_play)

    p = sub.add_parser("songs", help="列出可用曲目与难度")
    p.set_defaults(func=cmd_songs)
    return ap


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
