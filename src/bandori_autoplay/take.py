"""录一遍：等确认页 → 点开始键 → 记录真实触摸 → 存档并对齐到谱面。

``tools/record_taps.py``（录一遍）和 ``tools/double_take.py``（连着录两遍）
共用这一份逻辑。
"""

from __future__ import annotations

import time
from pathlib import Path

from .adb import AdbDevice
from .capture import ScreenStream, wait_live_frame
from .chartdb import FieldGeometry, load_events
from .livestate import is_live_frame
from .liveui import wait_confirm_page
from .touchlog import TouchReader, align_to_chart, find_touch_node, save_recording

REC_DIR = Path(__file__).resolve().parents[2] / "work" / "recordings"


def record_one(
    cfg: dict,
    *,
    name: str,
    music: int = 100004,
    difficulty: str = "easy",
    need_confirm: bool = True,
    start_btn: tuple[int, int] | None = None,
    log=print,
) -> Path | None:
    """录一遍，返回存好的 json 路径；失败返回 None。"""
    dev = AdbDevice(serial=cfg["device"].get("serial"), adb_path=cfg["device"].get("adb_path"))
    info = dev.info()
    node = find_touch_node(dev)
    screen = (info.height, info.width)          # 横屏
    stream_size = cfg["vision"].get("stream_size", "1280x592")
    sw, sh = (int(v) for v in stream_size.lower().split("x"))
    btn = start_btn or tuple(int(v) for v in cfg["game"]["live_start_button"])

    stream = ScreenStream(dev, size=stream_size, time_limit=240)
    reader = TouchReader(dev, node, info.width, info.height, log=log)
    t_tap = None
    stop_reason = "超时"
    t_live = None
    stream.start()
    reader.start()
    try:
        time.sleep(1.0)
        if need_confirm:
            hit = wait_confirm_page(dev, stream, screen, (sw, sh), log=log)
            if hit is None:
                log("    等不到确认页，放弃")
                return None
            btn = hit
        log(f"    开演键 {btn}，点它")
        dev.input_tap(*btn)
        t_tap = time.perf_counter()
        t_live, _fr = wait_live_frame(stream, timeout=45.0, log=log)
        if t_live is None:
            log("    没等到演奏画面")
            return None
        log("    开始记录你的触摸…")
        last_live = time.perf_counter()
        seen_live = 0
        last = -1
        deadline = t_tap + 220.0
        while time.perf_counter() < deadline:
            fr = stream.latest(timeout=0.5, newer_than=last)
            if fr is None or fr.index == last:
                continue
            last = fr.index
            now = time.perf_counter()
            if is_live_frame(fr.image):
                seen_live += 1
                last_live = now
            elif seen_live > 20 and now - last_live > 5.0:
                stop_reason = "演奏画面结束"
                break
    except KeyboardInterrupt:
        stop_reason = "手动中断"
    finally:
        stream.stop()
        reader.stop()
        time.sleep(0.4)

    actions = reader.actions()               # 0 点 = 第一次落指
    if not actions:
        log("    一个触摸事件都没录到")
        return None

    chart_dir = cfg.get("chartdb", {}).get("dir", "work/chartdb/out")
    chart = (Path(__file__).resolve().parents[2] / chart_dir
             / "charts" / f"{music:06d}" / f"{difficulty}.json")
    events = load_events(chart) if chart.exists() else []
    geom = FieldGeometry((sw, sh), screen[0], screen[1])
    shift, hits, total = align_to_chart(actions, events, geom) if events else (0.0, 0, 0)

    meta = {
        "music": music,
        "difficulty": difficulty,
        "shift_ms": shift * 1000.0,
        "align_hits": hits,
        "align_total": total,
        "live_start": list(btn),
        "taps": sum(1 for a in actions if a.kind == "down"),
        "panel": [info.width, info.height],
        "stop_reason": stop_reason,
    }
    path = REC_DIR / f"{name}.json"
    save_recording(path, actions, meta)
    downs = [a for a in actions if a.kind == "down"]
    log(f"    {len(actions)} 个事件、{len(downs)} 次按下、跨度 "
        f"{actions[-1].t_ms/1000:.1f}s；和谱面对齐 {hits}/{total}"
        f"（偏移 {shift*1000:+.0f} ms）→ {path.name}")
    return path
