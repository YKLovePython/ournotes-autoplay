"""你按第 1 个音符，脚本从第 2 个音符接着打。

    python tools/human_play.py              # 选歌 → 运行
    python tools/human_play.py --observe    # 只看不动手（先拿它验证一次）

就这么几步，没有别的机关：

1. 选歌（回车 = 影色舞 EASY）；
2. 脚本替你点 LIVE START，趁加载页自动量一次往返延迟；
3. 演奏画面出来后，你**按第 1 个音符**；
4. 脚本拿你那一下当基准，**从第 2 个音符开始接管**，你松手看它打完。

基准来自你自己的落指：脚本取的是那条触摸事件的**内核时间戳**，所以 adb 读得
慢只会让我们晚点开打，不会让落点偏。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bandori_autoplay.adb import AdbDevice, AdbError  # noqa: E402
from bandori_autoplay.capture import ScreenStream  # noqa: E402
from bandori_autoplay.chartdb import FieldGeometry, load_events  # noqa: E402
from bandori_autoplay.config import load_config  # noqa: E402
from bandori_autoplay.human_anchor import (  # noqa: E402
    HumanAnchor,
    find_uhid_node,
    match_tap,
    measure_path_latency,
)
from bandori_autoplay.liveui import wait_confirm_page  # noqa: E402
from bandori_autoplay.performer import sleep_until  # noqa: E402
from bandori_autoplay.touch import make_touch  # noqa: E402
from bandori_autoplay.touchlog import find_touch_node  # noqa: E402


# 加载页大概要 9~10 秒；这个下限只是挡掉「还没进演奏画面就误触」
ARM_FLOOR_S = 4.5
# 接管最小提前量：比这更近的音符来不及发，直接跳过
HANDOVER_LEAD_S = 0.15
# 量不到往返延迟时用的缺省值（实测量级 20~50 ms）
DEFAULT_PATH_LATENCY = 0.035

# ---- 手势参数 ----
TAP_MS = 0.040           # 点按按住多久
HOLD_TAIL_PAD_S = 0.060  # 长条到点后再多按 60ms 才松手（松早了尾判容易掉）
FLICK_LEAD_S = 0.020     # 轻扫：提前按下
FLICK_SPAN_S = 0.050     # 轻扫划动的耗时
FLICK_DIST = 120         # 轻扫距离（设备像素）
SLOTS = 9                # 可用的手指编号
RELEASE_GAP_S = 0.030    # 松开后隔这么久才允许这根手指接下一个音符
# 长条跟随的节奏。实测横移长条中位 1258 px/s、90 分位 4346 px/s，
# 按「每个谱面节点动一次」（约 157ms）单步位移中位 197px——和音符条容差同量级，
# 手指有一半时间在条外，这就是"有概率 miss"的来源。
# 改成**按位移采样**：走够 MOVE_TARGET_PX 才发一次，同一根手指最快 MOVE_MIN_S 一次。
MOVE_TARGET_PX = 50      # 每次划动至少走这么多像素
MOVE_MIN_S = 0.025       # 同一根手指最快 40Hz


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
        """代理给真正的流——选歌界面靠它决定要不要清屏。"""
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


def _flick_vector(direction: str, dist: int = FLICK_DIST) -> tuple[int, int]:
    """轻扫方向 -> 屏幕位移。谱面里只有 up / left / right（没有 down）。"""
    d = (direction or "up").lower()
    if d == "left":
        return -dist, 0
    if d == "right":
        return dist, 0
    return 0, -dist          # up 是默认，也是谱面里最多的


def _resample_path(path, t0: float, to_px, t_from: float, t_to: float,
                   target_px: float = MOVE_TARGET_PX, min_s: float = MOVE_MIN_S):
    """把长条轨迹重采样成 ``[(绝对时刻, 屏幕 x), ...]``，让手指贴着条子连续走。

    谱面节点大约 77~157ms 一个，照抄就是"一格一跳"（单步中位 197px，条子容差
    同量级，所以有概率掉）。这里按**位移**采样：走够 ``target_px`` 就发一次，
    同时同一根手指最快 ``min_s`` 一次——慢条子不会被无谓地喂一堆动作，
    快条子也不会跳步。
    """
    pts = [(t0 + t_ms / 1000.0, float(to_px(c))) for t_ms, c in (path or ())]
    if len(pts) < 2 or t_to <= t_from:
        return []
    out: list[tuple[float, float]] = []
    last_t = last_px = None
    for i in range(1, len(pts)):
        ta, pa = pts[i - 1]
        tb, pb = pts[i]
        if tb <= ta:
            continue
        n = max(1, int(abs(pb - pa) / (target_px / 2.0) + 0.999))
        for k in range(1, n + 1):
            r = k / n
            t = ta + (tb - ta) * r
            px = pa + (pb - pa) * r
            if t < t_from - 1e-9 or t > t_to + 1e-9:
                continue
            if last_t is not None and (t - last_t) < min_s:
                continue
            if last_px is not None and abs(px - last_px) < target_px:
                continue
            out.append((t, px))
            last_t, last_px = t, px
    return out


def build_schedule(events, t0: float, geom: FieldGeometry, start_index: int,
                   tap_ms: float = TAP_MS, hold_pad_s: float = HOLD_TAIL_PAD_S):
    """把谱面事件摊成动作表（down / move / up），按时间排序。

    三类手势：
      * **tap**   —— 按下、按 40ms、抬起
      * **long**  —— 按住，并**沿着节点轨迹移动**，到点后再多按一会儿才松手
      * **flick** —— 按下、朝谱面给的方向划出去、抬起

    同一时刻的动作按「先按/移动、后松开」排，免得游戏把松手和新按下并成一拍。
    """
    acts: list[tuple[float, str, int, int, int]] = []
    y = geom.device_judge_y
    busy = [0.0] * (SLOTS + 1)      # busy[s] = 这根手指到什么时候才空出来

    def take_slot(t: float) -> int:
        free = [s for s in range(1, SLOTS + 1) if busy[s] <= t]
        if free:
            return min(free)
        return min(range(1, SLOTS + 1), key=lambda s: busy[s])

    for i in range(start_index, len(events)):
        e = events[i]
        t_hit = t0 + e.time_ms / 1000.0
        x = geom.device_x(e.center)

        # ---- 轻扫 ----
        if e.kind == "flick" and not e.is_hold:
            dx, dy = _flick_vector(e.direction)
            t_down = t_hit - FLICK_LEAD_S
            slot = take_slot(t_down)
            t_up = t_hit + FLICK_SPAN_S
            busy[slot] = t_up + RELEASE_GAP_S
            acts.append((t_down, "down", slot, x, y))
            acts.append((t_hit, "move", slot, x + dx // 2, y + dy // 2))
            acts.append((t_hit + FLICK_SPAN_S / 2, "move", slot, x + dx, y + dy))
            acts.append((t_up, "up", slot, x + dx, y + dy))
            continue

        # ---- 长条：连续跟着轨迹走；头/尾可能是轻扫 ----
        if e.is_hold:
            head_f = getattr(e, "head_flick", "") or (e.direction if e.kind == "flick" else "")
            tail_f = getattr(e, "tail_flick", "")
            t_end = t0 + e.end_ms / 1000.0
            t_down = t_hit - (FLICK_LEAD_S if head_f else 0.0)
            follow_to = t_end - (FLICK_SPAN_S if tail_f else 0.0)
            t_up = t_end + (FLICK_SPAN_S + 0.02 if tail_f else hold_pad_s)
            slot = take_slot(t_down)
            busy[slot] = t_up + RELEASE_GAP_S
            fx, fy = x, y
            if head_f:
                dx, dy = _flick_vector(head_f)
                acts.append((t_down, "down", slot, fx, fy))
                acts.append((t_hit, "move", slot, fx + dx // 2, fy + dy // 2))
                acts.append((t_hit + FLICK_SPAN_S / 2, "move", slot, fx + dx, fy + dy))
                acts.append((t_hit + FLICK_SPAN_S, "move", slot, x, y))
                fx, fy = x, y
            else:
                acts.append((t_down, "down", slot, fx, fy))
            start_follow = t_hit + (FLICK_SPAN_S if head_f else 0.0)
            for t_abs, px in _resample_path(e.path, t0, geom.device_x,
                                            start_follow, follow_to):
                nx = int(round(px))
                if nx == fx:        # 位置没变就不发（原地不动的长条一个划动都不发）
                    continue
                acts.append((t_abs, "move", slot, nx, fy))
                fx = nx
            if tail_f:
                # 先确保手指已经在条子尾巴上，再划出去
                tail_px = int(round(geom.device_x(e.path[-1][1] if e.path else e.center)))
                if tail_px != fx:
                    acts.append((follow_to, "move", slot, tail_px, fy))
                    fx = tail_px
                dx, dy = _flick_vector(tail_f)
                acts.append((t_end, "move", slot, fx + dx // 2, fy + dy // 2))
                acts.append((t_end + FLICK_SPAN_S / 2, "move", slot, fx + dx, fy + dy))
                fx, fy = fx + dx, fy + dy
            acts.append((t_up, "up", slot, fx, fy))
            continue

        # ---- 普通点击 ----
        slot = take_slot(t_hit)
        t_up = t_hit + tap_ms
        busy[slot] = t_up + RELEASE_GAP_S
        acts.append((t_hit, "down", slot, x, y))
        acts.append((t_up, "up", slot, x, y))

    acts.sort(key=lambda a: (a[0], 1 if a[1] == "up" else 0))
    return acts


# ------------------------------------------------------------------ 选歌
def song_catalog(chart_dir: Path) -> list[tuple[int, str, list[str], str]]:
    """曲目列表 —— **名称与游戏内一致**。

    游戏里显示的是**日文原名**（不做翻译），所以这里用 ``MasterText._japanese``；
    其它语言只留作搜索别名（想用简中名找也行）。顺序仍按曲目号。

    返回 ``(曲目号, 日文名, 难度列表, 搜索别名)``。
    """
    index = chart_dir / "index.json"
    if not index.exists():
        return []
    charts = json.loads(index.read_text(encoding="utf-8")).get("charts", [])
    by_music: dict[int, set[str]] = {}
    for c in charts:
        by_music.setdefault(int(c["musicId"]), set()).add(str(c["difficulty"]))

    titles: dict[str, dict] = {}
    text = ROOT / "work" / "master" / "MasterText.json"
    if text.exists():
        for row in json.loads(text.read_text(encoding="utf-8"))["_allData"]:
            titles[row["_id"]] = row
    music: dict[int, dict] = {}
    mfile = ROOT / "work" / "master" / "MasterLiveMusic.json"
    if mfile.exists():
        for row in json.loads(mfile.read_text(encoding="utf-8"))["_allData"]:
            music[int(row["_id"])] = row

    order = ["easy", "normal", "hard", "expert"]
    now = time.time()
    entries = []
    for mid in sorted(by_music):
        row = music.get(mid, {})
        t = (titles.get(str(row.get("_titleTextID") or ""))
             or titles.get(f"Music_Tilte_{mid}") or titles.get(f"Music_Title_{mid}") or {})
        # 游戏里显示的是日文原名；其它语言只用来做搜索别名
        name = (t.get("_japanese") or t.get("_traditionalChinese")
                or t.get("_simplifiedChinese") or t.get("_english") or "")
        alias = " ".join(str(t.get(k) or "") for k in
                         ("_simplifiedChinese", "_traditionalChinese", "_japanese", "_english"))
        note = ""
        start = _parse_start(row.get("_startAt"))
        if start and start > now:
            note = time.strftime("  预载 %m-%d", time.localtime(start))
        diffs = [d for d in order if d in by_music[mid]]
        entries.append((mid, (name or f"(未命名 {mid})") + note, diffs, alias))
    return entries


def _parse_start(value) -> float | None:
    """把 master 里的 ``_startAt`` 解析成时间戳（格式是 ``2026/10/08 21:00:00``）。"""
    s = str(value or "").strip()
    if not s:
        return None
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d", "%Y-%m-%d"):
        try:
            return time.mktime(time.strptime(s, fmt))
        except ValueError:
            continue
    return None


PAGE = 12
LAST_CHOICE = ROOT / "work" / "last_choice.json"


def _clear() -> None:
    if sys.stdout.isatty():
        os.system("cls" if os.name == "nt" else "clear")
    else:
        print()


def _ask(prompt: str):
    """问一句；要是没有输入（管道/后台跑）就返回 None。"""
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None


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


def pick_song(catalog: list[tuple[int, str, list[str]]], log=print):
    """选曲界面：分页列表 + 关键字搜索 + 上次记忆。

    想最快开跑就一路回车——回车 = 上次打的那首（第一次是影色舞 EASY）。
    """
    last = _load_last()
    fallback = (next((s for s in catalog if s[0] == last.get("music")), None)
                or next((s for s in catalog if s[0] == 100004), None)
                or catalog[0])
    page, query = 0, ""
    while True:
        q = query.lower()
        view = ([s for s in catalog if q in s[1].lower() or q in s[3].lower()]
                if query else list(catalog))
        if query and not view:
            print(f"    没有匹配「{query}」的曲目，换一个词")
            query = ""
            continue
        pages = max(1, (len(view) + PAGE - 1) // PAGE)
        page = max(0, min(page, pages - 1))
        chunk = view[page * PAGE:(page + 1) * PAGE]
        _clear()
        head = "  选择曲目  （名称同游戏）"
        if query:
            head += f"   搜索「{query}」"
        print("=" * 64)
        print(f"{head}    第 {page+1}/{pages} 页 · 共 {len(view)} 首")
        print("=" * 64)
        for i, (mid, name, _d, _alias) in enumerate(chunk, 1):
            star = "    ←上次" if mid == last.get("music") else ""
            print(f"   [{i:>2}] {mid:>7}  {name}{star}")
        print("-" * 64)
        print(f"   序号选曲 · 关键字搜索 · n 下页 · p 上页 · 回车 = {fallback[1]}")
        raw = _ask("\n  选曲 > ")
        if raw is None or raw == "":
            song = fallback
            break
        low = raw.lower()
        if low in ("n", "p"):
            page += 1 if low == "n" else -1
            continue
        if low in ("q", "quit", "exit"):
            return None, None, None
        if raw.isdigit():
            n = int(raw)
            if 1 <= n <= len(chunk):
                song = chunk[n - 1]
                break
            exact = next((s for s in catalog if s[0] == n), None)
            if exact:
                song = exact
                break
        query, page = raw, 0        # 当成关键字搜

    music, name, diffs = song[0], song[1], song[2]
    default_diff = (last.get("difficulty")
                    if last.get("music") == music and last.get("difficulty") in diffs
                    else ("easy" if "easy" in diffs else (diffs[0] if diffs else "easy")))
    _clear()
    print("=" * 64)
    print(f"  选择难度    {name}（{music}）")
    print("=" * 64)
    for i, d in enumerate(diffs, 1):
        star = "    ←上次" if d == default_diff else ""
        print(f"   [{i}] {d}{star}")
    print("-" * 64)
    print(f"   序号或难度名 · 回车 = {default_diff}")
    raw = _ask("\n  难度 > ")
    difficulty = default_diff
    if raw:
        low = raw.lower()
        if low.isdigit() and 1 <= int(low) <= len(diffs):
            difficulty = diffs[int(low) - 1]
        elif low in diffs:
            difficulty = low
    _save_last(music, difficulty)
    return music, difficulty, name


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--music", type=int, default=None, help="曲目号（不给就进选歌）")
    ap.add_argument("--difficulty", default=None, help="easy/normal/hard/expert")
    ap.add_argument("--offset-ms", type=float, default=0.0,
                    help="整体微调（正=提前，负=延后）")
    ap.add_argument("--observe", action="store_true",
                    help="只观察不注入：脚本一下都不碰手机")
    ap.add_argument("--no-calibrate", action="store_true",
                    help="跳过加载页里的往返延迟标定")
    ap.add_argument("--start-button", default="", help="覆盖 LIVE START 坐标，如 2284,1037")
    args = ap.parse_args()

    logdir = ROOT / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    logpath = logdir / time.strftime("human_play_%m%d_%H%M%S.log")
    logfile = open(logpath, "w", encoding="utf-8", buffering=1)   # 行缓冲，被中断也留得下
    sys.stdout = _Tee(sys.__stdout__, logfile)
    print(f"日志 → {logpath}")

    cfg = load_config()
    chart_dir = ROOT / cfg.get("chartdb", {}).get("dir", "work/chartdb/out")

    name = ""
    if args.music is None:
        catalog = song_catalog(chart_dir)
        if not catalog:
            print(f"读不到谱面索引：{chart_dir / 'index.json'}")
            return 2
        picked = pick_song(catalog)
        if picked[0] is None:
            print("没选歌，结束")
            return 0
        music, difficulty, name = picked
    else:
        music = args.music
        difficulty = args.difficulty or "easy"

    chart = chart_dir / "charts" / f"{music:06d}" / f"{difficulty}.json"
    if not chart.exists():
        print(f"没有这张谱面：{chart}")
        return 2
    events = load_events(chart)
    if not events:
        print("谱面里没有落指事件")
        return 2
    first = events[0]
    print(f"谱面 {name}（{music} {difficulty}）：{len(events)} 个落指事件，"
          f"第 1 个在 {first.time_ms/1000:.2f}s，最后一个在 {events[-1].time_ms/1000:.1f}s")

    try:
        dev = AdbDevice(serial=cfg["device"].get("serial"),
                        adb_path=cfg["device"].get("adb_path"))
        info = dev.info()
    except Exception as exc:  # noqa: BLE001
        print(f"连不上手机：{exc}")
        print("   按顺序检查这几项（大多数情况是手机端的事）：")
        print("   1) 数据线插好（Windows 能看到「REDMI K90」这类设备名就算插好了）")
        print("   2) 手机：设置 → 更多设置 → 开发者选项 → **USB 调试** 打开")
        print("      （本来就开着的话，关掉再打开一次）")
        print("   3) 下拉通知栏 → 点「USB 连接方式」→ 选「传输文件 (MTP)」")
        print("   4) 手机上若弹出「允许 USB 调试吗？」，勾「始终允许」再点确定")
        print("   5) 改完在命令行敲一次：adb devices —— 看到一串设备号就成了")
        return 2
    panel_w, panel_h = info.width, info.height       # 自然方向（竖屏）
    screen_w, screen_h = info.height, info.width     # 游戏横屏逻辑尺寸
    stream_size = cfg["vision"].get("stream_size", "1280x592")
    sw, sh = (int(v) for v in stream_size.lower().split("x"))
    geom = FieldGeometry((sw, sh), screen_w, screen_h)
    print(f"手机 {info.model}  面板 {panel_w}x{panel_h}  横屏 {screen_w}x{screen_h}")

    node = find_touch_node(dev, panel_w)
    print(f"真实触摸屏节点 {node}")

    touch = None
    if not args.observe:
        touch = make_touch(cfg["touch"], dev, screen_w=screen_w, screen_h=screen_h,
                           jar=cfg["touch"].get("scrcpy_jar"))
        touch.start()
        print(f"触控后端 {type(touch).__name__}")

    stream = ScreenStream(dev, size=stream_size, time_limit=300)
    stream.start()
    anchor = None
    try:
        time.sleep(1.0)
        if args.start_button:
            btn = tuple(int(v) for v in args.start_button.split(","))
        else:
            btn = wait_confirm_page(dev, stream, (screen_w, screen_h), (sw, sh), log=print)
        if btn is None:
            print("等不到「乐队确认页」——请把手机停在那首歌的确认页上再跑")
            return 2
        print(f"开演键 {btn}，点它")
        dev.input_tap(*btn)
        t_start = time.perf_counter()

        path_latency = None
        if touch is not None and not args.no_calibrate:
            time.sleep(3.0)                      # 等加载页出来（这期间触摸没人管）
            uhid_node = find_uhid_node(dev, panel_w)
            if uhid_node:
                print(f"UHID 节点 {uhid_node}，在加载页量一次往返延迟…")
                path_latency = measure_path_latency(
                    dev, uhid_node, touch, panel_w, panel_h,
                    spot=(geom.device_x(first.center), geom.device_judge_y), log=print)
            else:
                print("没找到 UHID 节点，跳过标定")
        if path_latency is None:
            path_latency = DEFAULT_PATH_LATENCY
            print(f"  用缺省往返延迟 {path_latency*1000:.0f} ms")
        else:
            print(f"  往返延迟（读取 + 注入）≈ {path_latency*1000:.0f} ms")

        anchor = HumanAnchor(dev, node, panel_w, panel_h, geom, log=lambda *a: None)
        anchor.start()
        print("=" * 62)
        print("  等你的第 1 个音符 —— 演奏画面一出来就正常按下去，按完松手。")
        if args.observe:
            print("  （观察模式：脚本不会碰你的手机，只记录）")
        print("=" * 62)
        tap = anchor.wait_first(not_before=t_start + ARM_FLOOR_S, timeout=60.0)
        if tap is None:
            print("没等到演奏区里的落指，结束")
            return 2

        t0 = tap.t_host - path_latency - first.time_ms / 1000.0 + args.offset_ms / 1000.0
        print(f"锚点：内核 {tap.t_dev:.3f}s / 本机 {tap.t_host:.3f}s，"
              f"落点 ({tap.x}, {tap.y})，音心 {tap.lane:.2f}"
              f"（第 1 个音符音心 {first.center:.1f}）")

        if args.observe:
            print()
            print("观察模式：继续按你自己的节奏打，脚本只对时间。按 Ctrl+C 结束。")
            n = len(anchor.taps)
            while True:
                for t in anchor.poll():
                    i, res = match_tap(t, events, t0)
                    n += 1
                    if i is None:
                        print(f"  第 {n} 次落指 音心 {t.lane:5.2f} —— 对不上谱面"
                              f"（超出 160 ms，或轨道不对）")
                    else:
                        print(f"  第 {n} 次落指 音心 {t.lane:5.2f} → 谱面第 {i+1} 个音符 "
                              f"{events[i].time_ms/1000:6.2f}s，残差 {res*1000:+6.0f} ms")
                time.sleep(0.01)

        now = time.perf_counter()
        start_index = 0
        while (start_index < len(events)
               and t0 + events[start_index].time_ms / 1000.0 < now + HANDOVER_LEAD_S):
            start_index += 1
        if start_index >= len(events):
            print("锚点太晚，没有剩下可打的音符")
            return 2
        print(f"从第 {start_index+1} 个音符（{events[start_index].time_ms/1000:.2f}s）"
              f"开始接管，跳过前面 {start_index} 个")

        acts = build_schedule(events, t0, geom, start_index)
        seg = events[start_index:]
        n_hold = sum(1 for e in seg if e.is_hold)
        n_move = sum(1 for e in seg
                     if e.is_hold and len({c for _t, c in (e.path or ())}) > 1)
        n_flick = sum(1 for e in seg if e.kind == "flick" and not e.is_hold)
        n_tailf = sum(1 for e in seg if getattr(e, "tail_flick", ""))
        if n_hold or n_flick:
            extra = f"，其中尾巴要上/侧滑的 {n_tailf} 个" if n_tailf else ""
            print(f"  这一段：长条 {n_hold} 个（会横移的 {n_move} 个{extra}）、"
                  f"轻扫 {n_flick} 个")
        mix: dict[str, int] = {}
        for _t, k, _s, _x, _y in acts:
            mix[k] = mix.get(k, 0) + 1
        end_limit = t0 + events[-1].time_ms / 1000.0 + 12.0
        late = 0
        done = 0
        for at, kind, slot, x, y in acts:
            if time.perf_counter() > end_limit:
                break
            if sleep_until(at) > 0.05:
                late += 1
            try:
                if kind == "down":
                    touch.down(slot, x, y)
                elif kind == "move":
                    touch.move(slot, x, y)
                else:
                    touch.up(slot)
                done += 1
            except Exception as exc:  # noqa: BLE001
                print(f"  触控异常：{exc}")
        print(f"注入完成：{done}/{len(acts)} 个动作"
              f"（按下 {mix.get('down', 0)} · 划动 {mix.get('move', 0)}"
              f" · 抬起 {mix.get('up', 0)}）"
              + (f"，{late} 个迟到 >50ms" if late else ""))
        left = end_limit - time.perf_counter()
        if left > 0:
            time.sleep(left)
    except KeyboardInterrupt:
        print("\n手动中断")
    finally:
        if anchor is not None:
            anchor.stop()
        stream.stop()
        if touch is not None:
            touch.close()
        sys.stdout = sys.__stdout__
        try:
            logfile.flush()
            logfile.close()
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
