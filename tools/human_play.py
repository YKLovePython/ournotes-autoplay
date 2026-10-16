"""谛听：你按第 1 个音符，脚本从第 2 个音符起把整首打完。

双击就是打歌；启动时会自己先跑一遍只读自检（不碰手机画面）。
把装有整屏截图的文件夹**拖到 exe 上**，就是核对几何。

    python tools/human_play.py                 # 选歌 → 开演
    python tools/human_play.py --check         # 完整自检（含试建虚拟触摸屏）
    python tools/human_play.py --music 100004 --difficulty easy
    python tools/human_play.py <截图文件夹>     # 核对几何
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


ROOT = app_root()
sys.path.insert(0, str(ROOT / "src"))

from bandori_autoplay import (  # noqa: E402
    chartdb, geometry, liveui, schedule, screen, selfcheck, verify,
)
from bandori_autoplay.adb import AdbDevice  # noqa: E402
from bandori_autoplay.anchor import TouchClock, find_uhid_node  # noqa: E402
from bandori_autoplay.config import find_resource, load as load_config, resolve_paths  # noqa: E402
from bandori_autoplay.latency import measure_roundtrip  # noqa: E402
from bandori_autoplay.touch import make_touch  # noqa: E402
from bandori_autoplay.touchlog import find_touch_device  # noqa: E402

STEP = "=" * 62


def setup_console() -> None:
    """双击 exe 直接运行时，把控制台切到 UTF-8，中文才不乱码。"""
    try:
        if os.name == "nt":
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:  # noqa: BLE001
        pass
    for stream in (sys.stdout, sys.stdin):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


class _Tee:
    """同时写到屏幕和日志文件，方便事后核对。"""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, s: str) -> int:
        for st in self.streams:
            try:
                st.write(s)
            except Exception:  # noqa: BLE001
                pass
        return len(s)

    def flush(self) -> None:
        for st in self.streams:
            try:
                st.flush()
            except Exception:  # noqa: BLE001
                pass

    def isatty(self) -> bool:
        for st in self.streams:
            try:
                if st.isatty():
                    return True
            except Exception:  # noqa: BLE001
                pass
        return False

    def fileno(self) -> int:
        for st in self.streams:
            try:
                return st.fileno()
            except Exception:  # noqa: BLE001
                continue
        raise OSError("_Tee 没有可用的 fileno")


def sleep_until(deadline: float, spin: float = 0.0015) -> float:
    """等到 ``perf_counter() >= deadline``，返回迟到量（秒）。"""
    while True:
        left = deadline - time.perf_counter()
        if left <= 0:
            return -left
        if left > spin:
            time.sleep(left - spin)


def _ask(prompt: str):
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None


LAST_CHOICE = ROOT / "work" / "last_choice.json"
PAGE = 12


def _load_last() -> dict:
    try:
        return json.loads(LAST_CHOICE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save_last(music: int, difficulty: str) -> None:
    try:
        LAST_CHOICE.parent.mkdir(parents=True, exist_ok=True)
        LAST_CHOICE.write_text(json.dumps({"music": music, "difficulty": difficulty},
                                          ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def song_catalog(chart_dir: Path, master_dir: Path):
    """可选曲目列表：``[(曲目号, 名字, [难度...]), ...]``（名字同游戏）。"""
    index = chart_dir / "index.json"
    if not index.exists():
        return []
    charts = json.loads(index.read_text(encoding="utf-8")).get("charts", [])
    by_music: dict[int, set[str]] = {}
    for c in charts:
        by_music.setdefault(int(c["musicId"]), set()).add(str(c["difficulty"]))
    titles = chartdb.load_title_map(master_dir)
    order = ["easy", "normal", "hard", "expert"]
    out = []
    for mid in sorted(by_music):
        diffs = [d for d in order if d in by_music[mid]]
        out.append((mid, titles.get(mid) or f"(未命名 {mid})", diffs))
    return out


def pick_song(catalog, cfg: dict):
    """选曲 + 选难度。回车一路走 = 上次那首 / EASY；c = 完整自检。"""
    last = _load_last()
    fallback = (next((s for s in catalog if s[0] == last.get("music")), None)
                or next((s for s in catalog if s[0] == 100004), None) or catalog[0])
    page, query = 0, ""
    while True:
        q = query.lower()
        view = [s for s in catalog if q in s[1].lower()] if query else list(catalog)
        if query and not view:
            print(f"    没有匹配「{query}」的曲目")
            query = ""
            continue
        pages = max(1, (len(view) + PAGE - 1) // PAGE)
        page = max(0, min(page, pages - 1))
        chunk = view[page * PAGE:(page + 1) * PAGE]
        print(STEP)
        print(f"  选择曲目{('   搜索「' + query + '」') if query else ''}"
              f"    第 {page + 1}/{pages} 页 · 共 {len(view)} 首")
        print(STEP)
        for i, (mid, name, _d) in enumerate(chunk, 1):
            star = "    ←上次" if mid == last.get("music") else ""
            print(f"   [{i:>2}] {mid:>7}  {name}{star}")
        print("-" * 62)
        print(f"   序号选曲 · 关键字搜索 · n 下页 · p 上页 · c 自检 · 回车 = {fallback[1]}")
        raw = _ask("\n  选曲 > ")
        if raw is None or raw == "":
            song = fallback
            break
        low = raw.lower()
        if low == "n":
            page += 1
            continue
        if low == "p":
            page -= 1
            continue
        if low in ("c", "check"):
            return ("check", None, None)
        if low in ("q", "quit", "exit"):
            return None
        if raw.isdigit():
            n = int(raw)
            if 1 <= n <= len(chunk):
                song = chunk[n - 1]
                break
            exact = next((s for s in catalog if s[0] == n), None)
            if exact:
                song = exact
                break
        query, page = raw, 0

    music, name, diffs = song
    default = (last.get("difficulty") if last.get("music") == music
               and last.get("difficulty") in diffs
               else ("easy" if "easy" in diffs else diffs[0]))
    print(STEP)
    print(f"  选择难度    {name}（{music}）")
    print(STEP)
    for i, d in enumerate(diffs, 1):
        print(f"   [{i}] {d}" + ("    ←上次" if d == default else ""))
    raw = _ask(f"\n  难度（回车 = {default}）> ")
    difficulty = default
    if raw:
        low = raw.lower()
        if low.isdigit() and 1 <= int(low) <= len(diffs):
            difficulty = diffs[int(low) - 1]
        elif low in diffs:
            difficulty = low
    _save_last(music, difficulty)
    return music, difficulty, name


def _banner() -> None:
    print(STEP)
    print("  谛听   —— 你按第 1 个音符，脚本从第 2 个音符起把整首打完")
    print(STEP)
    print("  1) 手机：进游戏 → 选好歌和难度 → 停在「乐队确认」页（右下角有 LIVE START）")
    print("  2) 电脑：这里回车选歌、再回车选难度")
    print("  3) 演奏画面一出来，按第 1 个音符，按完松手")
    print()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("target", nargs="*", default=None,
                    help="把一个截图文件夹（或几张图）拖进来 = 核对几何")
    ap.add_argument("--music", type=int, default=None, help="曲目号（不给就进选歌）")
    ap.add_argument("--difficulty", default=None, help="easy/normal/hard/expert")
    ap.add_argument("--offset-ms", type=float, default=None,
                    help="整体微调（正 = 提前）；不给就用配置里的值")
    ap.add_argument("--no-calibrate", action="store_true", help="跳过加载页的往返延迟标定")
    ap.add_argument("--check", action="store_true", help="完整自检后就退出")
    args = ap.parse_args()

    setup_console()
    cfg = resolve_paths(load_config(), ROOT)

    # 拖一个文件夹进来 = 核对几何
    if args.target:
        return verify.main(list(args.target))

    logdir = Path(cfg["paths"]["logs"])
    logdir.mkdir(parents=True, exist_ok=True)
    logpath = logdir / time.strftime("human_play_%m%d_%H%M%S.log")
    logfile = open(logpath, "w", encoding="utf-8", buffering=1)   # 行缓冲，被中断也留得下
    sys.stdout = _Tee(sys.__stdout__, logfile)

    if args.check:
        return selfcheck.deep(cfg, ROOT, log=print)

    _banner()
    problems = selfcheck.quick(cfg, log=print)
    if problems:
        print()
        print("  ⚠ 自检没过，先解决上面那些再开演：")
        for p in problems:
            print("    · " + p)
        return 2
    print()

    chart_dir = Path(cfg["paths"]["chartdb"])
    while True:
        if args.music is None:
            catalog = song_catalog(chart_dir, Path(cfg["paths"]["master"]))
            if not catalog:
                print(f"读不到谱面索引：{chart_dir / 'index.json'}")
                return 2
            picked = pick_song(catalog, cfg)
            if picked is None:
                print("没选歌，结束")
                return 0
            if picked[0] == "check":
                print()
                selfcheck.deep(cfg, ROOT, log=print)
                print()
                continue
            music, difficulty, name = picked
        else:
            music = args.music
            difficulty = args.difficulty or "easy"
            name = ""
        break

    chart = chart_dir / "charts" / f"{music:06d}" / f"{difficulty}.json"
    if not chart.exists():
        print(f"没有这张谱面：{chart}")
        return 2
    events = chartdb.load_events(chart)
    if not events:
        print("谱面里没有落指事件")
        return 2
    first = events[0]

    try:
        dev = AdbDevice(serial=cfg["device"].get("serial"),
                        adb_path=cfg["device"].get("adb_path"))
        info = dev.info()
    except Exception as exc:  # noqa: BLE001
        print(f"连不上手机：{exc}")
        print("   检查：数据线插好 / 开发者选项里打开「USB 调试」/ 手机上允许调试")
        return 2
    panel_w, panel_h = info.width, info.height
    screen_w, screen_h = info.height, info.width            # 游戏是横屏
    field = geometry.field_for(screen_w, screen_h)
    offset_ms = (cfg["play"].get("offset_ms", 0.0) if args.offset_ms is None
                 else args.offset_ms)
    print(f"手机 {info.model}  Android {info.android}  面板 {panel_w}x{panel_h}")
    print(f"几何 {field.describe()}    偏移 {offset_ms:+.0f} ms")
    print(f"谱面 {name}（{music} {difficulty}）：{len(events)} 个落指事件，"
          f"第 1 个在 {first.time_ms / 1000:.2f}s，最后一个在 {events[-1].time_ms / 1000:.1f}s")

    # 先把动作表算好（按下之后只做一次"平移起跑"）
    t_build = time.perf_counter()
    acts = schedule.build_actions(events, field)
    n_hold = sum(1 for e in events if e.is_hold)
    n_flick = sum(1 for e in events if e.kind == "flick" and not e.is_hold)
    print(f"动作表已就绪：{len(acts)} 个动作（长条 {n_hold} · 轻扫 {n_flick}），"
          f"耗时 {(time.perf_counter() - t_build) * 1000:.0f} ms")

    # 先认确认页（只看画面，不碰触摸）
    node, unit = find_touch_device(dev, panel_w)
    print(f"真实触摸屏节点 {node}")
    tpl = find_resource(cfg["paths"]["live_start_template"])
    screen.wake(dev)
    print()
    print("  请在手机上把歌停在「乐队确认」页（右下角有 LIVE START 的那一页）。")
    print("  认不到就会停下——不会乱点别的坐标。")
    touch = None
    try:
        btn = liveui.wait_confirm_page(lambda: screen.grab(dev, log=print), tpl,
                                       timeout=float(cfg["play"].get("confirm_timeout_s", 120.0)),
                                       min_score=float(cfg["play"].get("template_score", 0.62)),
                                       log=print)
        if btn is None:
            print("  等不到「乐队确认页」——请把手机停在那首歌的确认页上再跑一次")
            return 2
        print(f"  点 LIVE START {btn}")
        dev.input_tap(*btn)
        t_start = time.perf_counter()

        # 这时才建 UHID 虚拟触摸屏（趁加载页建，~3 秒）
        touch = make_touch(cfg["touch"], dev, screen_w=screen_w, screen_h=screen_h,
                           jar=cfg["touch"].get("scrcpy_jar"))
        touch.start()
        print(f"  触控后端 {type(touch).__name__}")

        # 加载页里量往返延迟（这时点屏幕没有副作用）
        latency = None
        if not args.no_calibrate:
            time.sleep(2.5)
            uhid_node = find_uhid_node(dev, panel_w)
            if uhid_node:
                print(f"  UHID 节点 {uhid_node}，在加载页量一次往返延迟…")
                latency = measure_roundtrip(dev, uhid_node, touch,
                                            (field.center_x, field.judge_y), log=print)
            else:
                print("  没找到 UHID 节点，跳过标定")
        if latency is None:
            latency = float(cfg["play"].get("default_latency_ms", 35.0)) / 1000.0
            print(f"  用缺省往返延迟 {latency * 1000:.0f} ms")

        # 等你的第一下（只取时间戳）
        clock = TouchClock(dev, node, panel_w, panel_h, log=lambda *a: None, scale=unit)
        clock.start()
        print(STEP)
        print("  等你的第 1 个音符 —— 演奏画面一出来就正常按下去，按完松手。")
        print("  （只取你那一下的内核时间戳当基准；你按在哪不影响落点）")
        print(STEP)
        got = clock.wait_down(timeout=180.0, log=print)
        if got is None:
            print("没等到落指，结束")
            return 2
        _idx, t_dev, t_host = got
        t0 = t_host - latency - first.time_ms / 1000.0 + offset_ms / 1000.0
        print(f"锚点：内核 {t_dev:.3f}s / 本机 {t_host:.3f}s，往返延迟 {latency * 1000:.0f} ms")
        print(f"      ⇒ 谱面 0 点 = 本机 {t0:.3f}s")

        # 从还没过期的那个音符起接管
        now_rel = time.perf_counter() - t0
        lead = float(cfg["play"].get("handover_lead_s", 0.015))
        start_index = 0
        while (start_index < len(events)
               and events[start_index].time_ms / 1000.0 < now_rel + lead):
            start_index += 1
        if start_index >= len(events):
            print("锚点太晚，没有剩下可打的音符")
            return 2
        print(f"从第 {start_index + 1} 个音符（{events[start_index].time_ms / 1000:.2f}s）"
              f"开始接管，跳过前面 {start_index} 个")

        # 用预计算好的动作表：丢掉已经过去的（保留刚过去 0.25s 内的 down，长条还接得上）
        t_cut = now_rel - 0.25
        live_slots: set[int] = set()
        plan = []
        for t_rel, kind, slot, x, y in acts:
            if t_rel < t_cut:
                if not (kind == "down" and slot not in live_slots):
                    continue
            if kind == "down":
                live_slots.add(slot)
            elif slot not in live_slots:
                continue
            plan.append((t_rel, kind, slot, x, y))
        end_rel = events[-1].time_ms / 1000.0 + 12.0
        late = 0
        done = 0
        mix: dict[str, int] = {}
        for t_rel, kind, slot, x, y in plan:
            if time.perf_counter() - t0 > end_rel:
                break
            if sleep_until(t0 + t_rel) > 0.05:
                late += 1
            try:
                if kind == "down":
                    touch.down(slot, x, y)
                elif kind == "move":
                    touch.move(slot, x, y)
                else:
                    touch.up(slot)
                done += 1
                mix[kind] = mix.get(kind, 0) + 1
            except Exception as exc:  # noqa: BLE001
                print(f"  触控异常：{exc}")
        print(f"注入完成：{done}/{len(plan)} 个动作"
              f"（按下 {mix.get('down', 0)} · 划动 {mix.get('move', 0)}"
              f" · 抬起 {mix.get('up', 0)}）" + (f"，{late} 个迟到 >50ms" if late else ""))
        left = t0 + end_rel - time.perf_counter()
        if left > 0:
            time.sleep(left)
    except KeyboardInterrupt:
        print("\n手动中断")
    finally:
        try:
            if touch is not None:
                touch.close()
        except Exception:  # noqa: BLE001
            pass
        sys.stdout = sys.__stdout__
        try:
            logfile.flush()
            logfile.close()
        except Exception:  # noqa: BLE001
            pass
    print()
    print("打完了。去结算页看命中率：整体偏早/偏晚就调 config\\default.yaml 里的 offset_ms")
    print(f"日志：{logpath}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
