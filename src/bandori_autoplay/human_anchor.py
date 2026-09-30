"""人手锚点：你按第 1 个音符，脚本从第 2 个音符接着打。

为什么不再让脚本自己找起播时刻
------------------------------
谱面文件里没有前摇时长，加载时间还不固定，所以「点完开始键后等 N 秒」这类
做法必然不稳。实机日志里同一套逻辑的标定结果是 2.12s / 9.53s / 10.05s /
11.52s——差 9 秒，够让整首歌全 miss。三个录制文件里也有同样的现象：
``yswt_1`` 的 0 点其实是 LIVE START 那一下，对齐偏移从 2.9s 变成 9.5s，
命中率从 64/74 掉到 16/75。

而**你自己按下的那一下**，就是游戏刚刚判定接受的一次谱面时刻。拿它当 0 点，
等于直接抄游戏自己的时间轴，不用猜任何常数。

时间轴怎么算
------------
设：

* ``F``    你的手指真正碰到面板的时刻（设备 MONOTONIC，也就是 getevent 的
   ``[ 123.456 ]`` 那个值）；
* ``L``    触摸事件从内核读出来、经 adb 到我们手里的延迟；
* ``λ``    我们发一条 UHID 触摸、到内核收到它的延迟。

我们「看见」你第一次落指的时刻是 ``h_obs = F + L``。要让脚本的第 n 个音符
被游戏在同样的时刻判定，内核必须在 ``F + (t_n - t_ref)`` 收到注入；也就是
我们要在

    H_n = h_obs - (L + λ) + (t_n - t_ref)

发出，其中 ``t_ref`` 是你按中的那个音符的谱面时间。

``L + λ`` 是一个常数，而且**可以量**：用 UHID 自己点一下，再看 getevent
什么时候读到这条事件，往返时间就是它（见 :func:`measure_path_latency`）。

注意 ``L`` 在这里只是延迟、不是误差：用内核时间戳当锚点，意味着「读得慢」
只会让我们晚一点开打，不会让落点偏。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from .chartdb import FieldGeometry
from .touchlog import TouchReader, in_playfield


class TouchReaderRT(TouchReader):
    """``TouchReader`` 的实时版。

    相比原版多了两件事：

    1. 记下**每一行到达本机的时刻**（``host_stamps`` 与 ``raw`` 一一对应），
       这样才能把内核时间戳换成我们这边的时钟；
    2. 默认走 pty（``adb shell -t``）——管道里的 stdout 是块缓冲，
       ``getevent`` 可能要攒够 4KB 才吐出来，那就没法实时接管了。
    """

    def __init__(self, *args, use_pty: bool = True, **kw):
        super().__init__(*args, **kw)
        self.use_pty = use_pty
        self.stamps: list[float] = []       # 每一行到达的时刻
        self.host_stamps: list[float] = []  # 与 raw 一一对应
        self._last_stamp = 0.0

    # ------------------------------------------------------------------
    def start(self) -> None:  # noqa: D102
        if not self.use_pty:
            return super().start()
        import subprocess
        import threading

        self._proc = subprocess.Popen(
            self.device._cmd(["shell", "-t", "getevent", "-t", self.node]),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=1,
            universal_newlines=True,
        )
        threading.Thread.start(self)

    def _feed(self, ts: float, etype: int, code: int, value: int) -> None:
        now = time.perf_counter()
        self.stamps.append(now)
        self._last_stamp = now
        super()._feed(ts, etype, code, value)

    def _commit(self, ts: float) -> None:
        before = len(self.raw)
        super()._commit(ts)
        extra = len(self.raw) - before
        if extra > 0:
            self.host_stamps.extend([self._last_stamp or time.perf_counter()] * extra)


# ---------------------------------------------------------------- getevent 信息
def input_devices(device) -> list[dict]:
    """解析 ``getevent -pl``，返回 ``[{node, name, xmax}, ...]``。"""
    out = device.shell("getevent -pl") or ""
    blocks: list[dict] = []
    cur: dict | None = None
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("add device"):
            if cur:
                blocks.append(cur)
            cur = {"node": s.rsplit(":", 1)[-1].strip(), "name": "", "xmax": 0}
        elif cur is not None:
            if "name:" in line and not cur["name"]:
                cur["name"] = line.split('"')[1] if '"' in line else ""
            elif "ABS_MT_POSITION_X" in line:
                m = re.search(r"max\s+(\d+)", line)
                if m:
                    cur["xmax"] = int(m.group(1))
    if cur:
        blocks.append(cur)
    return blocks


def find_uhid_node(device, panel_w: int | None = None, name_hint: str = "autoplay") -> str | None:
    """找我们自己那块 UHID 虚拟触摸屏的节点。

    UHID 描述符里的 Logical Maximum 用的是面板尺寸，所以它的
    ``ABS_MT_POSITION_X`` max ≈ 面板宽；真机 digitizer 则是 ≈ 面板宽 × 100。
    """
    cands = [b for b in input_devices(device) if b["xmax"] > 0]
    if not cands:
        return None
    named = [b for b in cands
             if name_hint in b["name"].lower() or "uhid" in b["name"].lower()]
    if panel_w:
        pool = named or [b for b in cands if abs(b["xmax"] - panel_w) < panel_w * 0.25]
        if pool:
            pool = sorted(pool, key=lambda b: abs(b["xmax"] - panel_w))
            return pool[0]["node"]
    return named[0]["node"] if named else None


def raw_to_landscape(x_raw: float, y_raw: float, scale: float, panel_w: int) -> tuple[float, float]:
    """getevent 原始值 -> 横屏逻辑设备坐标。

    goodix_ts 报的是**面板自然方向（竖屏）**、单位 0.01 像素：

        横屏 x = 竖屏 y
        横屏 y = 面板宽 - 竖屏 x
    """
    xp = x_raw / scale
    yp = y_raw / scale
    return yp, panel_w - xp


# ---------------------------------------------------------------- 往返延迟
def measure_path_latency(device, node: str, touch, panel_w: int, panel_h: int,
                         spot: tuple[int, int], samples: int = 6,
                         log=print) -> float | None:
    """量「发一条 UHID 触摸」到「getevent 读到它」的往返时间 = ``L + λ``。

    两个方向的延迟都是正的、而且总有一侧在排队，所以取多次里的最小值。
    这段要在**加载页**做：那时候游戏不响应触摸，随手点几下没有副作用。
    """
    reader = TouchReaderRT(device, node, panel_w, panel_h, log=lambda *a: None, scale=1.0)
    reader.start()
    time.sleep(0.6)
    lats: list[float] = []
    for _ in range(max(1, samples)):
        n0 = len(reader.stamps)
        h0 = time.perf_counter()
        try:
            touch.down(0, spot[0], spot[1])
            time.sleep(0.03)
            touch.up(0)
        except Exception as exc:  # noqa: BLE001
            log(f"    标定注入失败：{exc}")
            break
        h1 = None
        deadline = h0 + 0.7
        while time.perf_counter() < deadline:
            if len(reader.stamps) > n0:
                h1 = reader.stamps[n0]
                break
            time.sleep(0.0005)
        if h1 is not None:
            lats.append(h1 - h0)
        time.sleep(0.25)
    reader.stop()
    if not lats:
        log("    没量到往返延迟（getevent 可能是块缓冲，或 UHID 没生效）")
        return None
    lats.sort()
    return lats[0]


# ---------------------------------------------------------------- 锚点
@dataclass
class Tap:
    """一次落在演奏区里的真实落指。"""

    t_dev: float      # 内核时间戳（设备 MONOTONIC）
    t_host: float     # 我们读到它的本机时刻
    x: int            # 横屏设备坐标
    y: int
    lane: float       # 换算到谱面位置空间（0..24）


class HumanAnchor:
    """盯着真实触摸屏，等你按第一个音符。"""

    def __init__(self, device, node: str, panel_w: int, panel_h: int,
                 geom: FieldGeometry, *, scale: float = 100.0,
                 use_pty: bool = True, log=print):
        self.geom = geom
        self.panel_w = panel_w
        self.log = log
        self.reader = TouchReaderRT(device, node, panel_w, panel_h, log=log,
                                    scale=scale, use_pty=use_pty)
        self.taps: list[Tap] = []
        self._seen = 0

    # ------------------------------------------------------------------
    def start(self) -> None:
        self.reader.start()

    def stop(self) -> None:
        self.reader.stop()

    def poll(self) -> list[Tap]:
        """收下新到的落指（已经过演奏区过滤）。"""
        raw = self.reader.raw
        fresh: list[Tap] = []
        while self._seen < len(raw):
            idx = self._seen
            self._seen += 1
            t, kind, _slot, x_raw, y_raw = raw[idx]
            if kind != "down":
                continue
            dev_x, dev_y = raw_to_landscape(x_raw, y_raw, self.reader.scale, self.panel_w)
            if not in_playfield(dev_x, dev_y, self.geom):
                continue          # LIVE START 按钮、屏幕边缘等噪声
            lane = self.geom.stream_x_to_lane(dev_x * self.geom.scale_x,
                                              dev_y * self.geom.scale_y)
            host = (self.reader.host_stamps[idx]
                    if idx < len(self.reader.host_stamps) else time.perf_counter())
            tap = Tap(t_dev=t, t_host=host, x=int(round(dev_x)), y=int(round(dev_y)),
                      lane=float(lane))
            self.taps.append(tap)
            fresh.append(tap)
        return fresh

    def wait_first(self, *, not_before: float | None = None, timeout: float = 45.0):
        """等你第一次落指；``not_before`` 是本机时刻下限（挡掉加载期的误触）。"""
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            for tap in self.poll():
                if not_before is not None and tap.t_host < not_before:
                    continue
                return tap
            time.sleep(0.005)
        return None


def match_tap(tap: Tap, events, t0: float, *, time_tol: float = 0.16,
              lane_tol: float = 2.0, first: int = 0):
    """把一次落指对上谱面事件。返回 ``(下标, 残差秒)``，对不上返回 ``(None, None)``。"""
    best = (None, None)
    for i in range(first, len(events)):
        e = events[i]
        dt = (tap.t_host - t0) - e.time_ms / 1000.0
        if abs(dt) > time_tol:
            continue
        if abs(e.center - tap.lane) > lane_tol:
            continue
        if best[1] is None or abs(dt) < abs(best[1]):
            best = (i, dt)
    return best
