"""录 / 放**你自己的真实触摸**。

思路（比让脚本自己算时间靠谱得多）：你亲手打一遍，脚本用 ``getevent``
把真实触摸屏的原始事件记下来，之后每次开演都用 UHID 虚拟触摸屏
把这些事件**按同样的相对时间**重放一遍。时间对不对由你的手负责，
脚本只负责复制。

坐标：本机触摸屏 ``goodix_ts``（``/dev/input/event7``）报的是
**面板自然方向（竖屏）**、单位为 0.01 像素：X 0..115599、Y 0..250999。
换算到横屏逻辑坐标（0..2510, 0..1156，也就是 UHID 后端用的那套）：

    横屏 x = 竖屏 y          （0..2510）
    横屏 y = 面板宽 - 竖屏 x   （0..1156）

参考点：两次运行都用「演奏画面出现」的那一帧做 0 点（同一套检测），
所以取流链路那几十毫秒的延迟在录制和回放里自动抵消。
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .adb import AdbDevice
from .chartdb import FAN

EV_SYN, EV_KEY, EV_ABS = 0x00, 0x01, 0x03
SYN_REPORT = 0x00
ABS_MT_SLOT = 0x2F
ABS_MT_POSITION_X = 0x35
ABS_MT_POSITION_Y = 0x36
ABS_MT_TRACKING_ID = 0x39

# 只给一个设备时，getevent 不打印 "/dev/input/eventN:" 前缀，所以设备名可选
_LINE = re.compile(
    r"^\[\s*([0-9]+\.[0-9]+)\]\s+(?:\S+:\s+)?([0-9a-fA-F]+)\s+"
    r"([0-9a-fA-F]+)\s+([0-9a-fA-F]+)\s*$"
)


@dataclass
class TouchAction:
    """一次落指状态变化（坐标已是横屏逻辑坐标）。"""

    t_ms: float          # 相对参考点
    kind: str            # down | move | up
    finger: int          # 0..9
    x: int
    y: int


def find_touch_device(device: AdbDevice, panel_w: int | None = None):
    """找**真实**多点触摸屏，返回 ``(节点, 坐标比例)``。

    要点：排除我们自己用 UHID 造出来的虚拟屏（名字里带 autoplay/uhid），
    并从 ``ABS_MT_POSITION_X`` 的 max 反推坐标单位——真机 digitizer 用
    「0.01 像素」，max 会是面板宽度的约 100 倍；UHID 那块则约等于面板宽。
    """
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

    cands = [b for b in blocks
             if b["xmax"] > 0
             and "autoplay" not in b["name"].lower()
             and "uhid" not in b["name"].lower()]
    if not cands:
        raise RuntimeError("没找到真实多点触摸设备（getevent -pl 里没有 ABS_MT_POSITION_X）")
    if panel_w:
        # 越接近「0.01 像素」这个约定（xmax ≈ 100×面板宽）越像真触摸屏
        cands.sort(key=lambda b: abs(b["xmax"] / float(panel_w) - 100.0))
    best = cands[0]
    scale = (best["xmax"] / float(panel_w)) if panel_w else 100.0
    return best["node"], scale


def find_touch_node(device: AdbDevice, panel_w: int | None = None) -> str:
    return find_touch_device(device, panel_w)[0]


def device_clock_offset(device: AdbDevice, shell=None, samples: int = 3) -> float:
    """返回「设备单调时钟 - 本机 perf_counter」的偏移（秒）。

    用来把「我们在流里看到演奏画面」的本机时刻换算到 getevent 的时间轴上。
    绕行一次 adb shell 会引入往返延迟，所以取往返最短的那次。
    """
    best: tuple[float, float] | None = None
    for _ in range(max(1, samples)):
        t0 = time.perf_counter()
        out = (shell.run("cat /proc/uptime") if shell is not None
               else device.shell("cat /proc/uptime")) or ""
        t1 = time.perf_counter()
        try:
            dev_t = float(out.split()[0])
        except (ValueError, IndexError):
            continue
        lat = t1 - t0
        if best is None or lat < best[0]:
            best = (lat, dev_t - (t0 + t1) / 2.0)
    if best is None:
        raise RuntimeError("读不到设备时钟(/proc/uptime)")
    return best[1]


class TouchReader(threading.Thread):
    """后台跑 ``adb shell getevent``，把多点触摸协议 B 的事件流解析成动作。"""

    def __init__(self, device: AdbDevice, node: str, panel_w: int, panel_h: int,
                 log=None, scale: float | None = None):
        super().__init__(daemon=True)
        self.device = device
        self.node = node
        self.panel_w = panel_w
        self.scale = float(scale) if scale else 100.0   # goodix_ts 的单位是 0.01 像素
        self.log = log or (lambda *a: None)
        self.raw: list[tuple[float, str, int, int, int]] = []   # t_dev, kind, finger, x, y(竖屏像素)
        self._proc: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._slots: dict[int, tuple[int, int, int]] = {}       # slot -> (id, x, y)
        self._cur_slot = 0
        self._pending: dict[int, tuple[int, int, int]] = {}

    # ------------------------------------------------------------------
    def start(self) -> None:
        self._proc = subprocess.Popen(
            self.device._cmd(["shell", "getevent", "-t", self.node]),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=1,
            universal_newlines=True,
        )
        super().start()

    def stop(self) -> None:
        self._stop.set()
        if self._proc is not None:
            try:
                self._proc.terminate()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    def run(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                if self._stop.is_set():
                    break
                m = _LINE.match(line.strip())
                if not m:
                    continue
                ts = float(m.group(1))
                etype, code, value = (int(m.group(i), 16) for i in (2, 3, 4))
                self._feed(ts, etype, code, value)
        except Exception as exc:  # noqa: BLE001
            self.log(f"getevent 读取结束: {exc}")

    def _feed(self, ts: float, etype: int, code: int, value: int) -> None:
        if etype == EV_ABS:
            if code == ABS_MT_SLOT:
                self._cur_slot = value
            elif code == ABS_MT_TRACKING_ID:
                if value == 0xFFFFFFFF:              # 抬手
                    s = self._pending.get(self._cur_slot, (0, 0, 0))
                    self._pending[self._cur_slot] = (0xFFFFFFFF, s[1], s[2])
                else:
                    self._pending[self._cur_slot] = (value, 0, 0)
            elif code in (ABS_MT_POSITION_X, ABS_MT_POSITION_Y):
                eid, x, y = self._pending.get(self._cur_slot, (0, 0, 0))
                if code == ABS_MT_POSITION_X:
                    x = value
                else:
                    y = value
                self._pending[self._cur_slot] = (eid, x, y)
        elif etype == EV_SYN and code == SYN_REPORT:
            self._commit(ts)

    def _commit(self, ts: float) -> None:
        """一次 SYN_REPORT = 一帧触摸状态；和上一帧比较，产出 down/move/up。"""
        for slot, (eid, x, y) in self._pending.items():
            old = self._slots.get(slot)
            if eid == 0xFFFFFFFF:
                if old is not None:
                    self.raw.append((ts, "up", slot, old[1], old[2]))
                    del self._slots[slot]
                continue
            if eid == 0 and x == 0 and y == 0:
                continue
            if old is None:
                self._slots[slot] = (eid, x, y)
                self.raw.append((ts, "down", slot, x, y))
            elif (x, y) != (old[1], old[2]):
                self._slots[slot] = (eid, x, y)
                self.raw.append((ts, "move", slot, x, y))
        self._pending = dict(self._slots)

    # ------------------------------------------------------------------
    def actions(self, origin_dev_time: float | None = None) -> list[TouchAction]:
        """换算成横屏逻辑坐标；``origin_dev_time`` 为 0 点（默认=第一次触摸）。"""
        if origin_dev_time is None:
            if not self.raw:
                return []
            origin_dev_time = self.raw[0][0]
        out: list[TouchAction] = []
        for t, kind, slot, x, y in self.raw:
            xp = x / self.scale                      # 竖屏像素
            yp = y / self.scale
            out.append(TouchAction(
                t_ms=(t - origin_dev_time) * 1000.0,
                kind=kind,
                finger=slot & 0x0F,
                x=int(round(yp)),                    # 横屏 x = 竖屏 y
                y=int(round(self.panel_w - xp)),     # 横屏 y = 面板宽 - 竖屏 x
            ))
        return out


def in_playfield(x_dev: float, y_dev: float, geom, y_lo: float = 360.0,
                 y_hi: float = 1125.0) -> bool:
    """这个落点是不是在演奏区里（扇形轨道 + 判定线上下那一片）。

    用来剔掉「手先碰了 LIVE START 按钮」「碰到屏幕别处」这类噪声事件。
    """
    if not (y_lo <= y_dev <= y_hi):
        return False
    y_stream = y_dev * geom.scale_y
    half = FAN * (y_stream - geom.ay)
    if half <= 1.0:
        return False
    return abs(x_dev * geom.scale_x - geom.ax) <= half * 1.05


def filter_actions(actions: list[TouchAction], geom) -> list[TouchAction]:
    """丢掉不在演奏区里的那几根手指的全部动作（按下不在区里的整段都丢）。"""
    out: list[TouchAction] = []
    live: dict[int, bool] = {}
    for a in actions:
        if a.kind == "down":
            ok = in_playfield(a.x, a.y, geom)
            live[a.finger] = ok
            if ok:
                out.append(a)
        elif live.get(a.finger, False):
            out.append(a)
    return out


def align_to_chart(actions: list[TouchAction], events, geom,
                   lo: float = -20.0, hi: float = 20.0, step: float = 0.005,
                   time_tol: float = 0.12, lane_tol: float = 1.6):
    """把「相对第一次落指」的时间轴挪到谱面时间轴上。

    返回 ``(shift_seconds, hits, total)``：``t_ms/1000 + shift`` ≈ 谱面时间。
    只用按下事件，同时要求轨道对得上，所以镜像/换算错误会直接体现为命中率极低。
    """
    # 只信落在演奏区里的按下：录的时候手可能先碰到「LIVE START」按钮，
    # 或者碰到屏幕别的地方，这些不是打歌。
    downs = [a for a in actions if a.kind == "down" and in_playfield(a.x, a.y, geom)]
    if not downs or not events:
        return 0.0, 0, len(downs)
    et = np.array([e.time_ms / 1000.0 for e in events])
    ec = np.array([e.center for e in events], dtype=float)
    at = np.array([a.t_ms / 1000.0 for a in downs])
    al = np.array([geom.stream_x_to_lane(a.x * geom.scale_x, geom.judge_y) for a in downs])
    best = None
    plateau: list[float] = []
    for shift in np.arange(lo, hi, step):
        target = at + shift
        idx = np.searchsorted(et, target)
        hit = 0
        for j, (tt, ll) in enumerate(zip(target, al)):
            for k in (idx[j] - 1, idx[j], idx[j] + 1):
                if 0 <= k < len(et) and abs(et[k] - tt) <= time_tol and abs(ec[k] - ll) <= lane_tol:
                    hit += 1
                    break
        rate = hit / len(downs)
        if best is None or rate > best[0]:
            best = (rate, float(shift), hit)
            plateau = [float(shift)]
        elif rate == best[0]:
            plateau.append(float(shift))
    # 命中率往往在一个区间里并列，取中位数避免系统性偏早/偏晚
    if len(plateau) > 2:
        best = (best[0], float(np.median(plateau)), best[2])
    # 再用「匹配上的对子」直接估一个中位数偏移，去掉平台带来的偏差
    shift = best[1]
    residuals: list[float] = []
    for tt, ll in zip(at + shift, al):
        cand = None
        for e in events:
            if abs(e.center - ll) > lane_tol:
                continue
            d = e.time_ms / 1000.0 - tt
            if abs(d) <= time_tol * 1.5 and (cand is None or abs(d) < abs(cand)):
                cand = d
        if cand is not None:
            residuals.append(cand)
    if len(residuals) >= 5:
        shift = float(np.median(residuals) + shift)
        best = (best[0], shift, best[2])
    return best[1], best[2], len(downs)


def save_recording(path: str | Path, actions: list[TouchAction], meta: dict) -> None:
    doc = {
        "version": 1,
        "meta": meta,
        "actions": [
            {"t": round(a.t_ms, 1), "k": a.kind, "f": a.finger, "x": a.x, "y": a.y}
            for a in actions
        ],
    }
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")


def load_recording(path: str | Path) -> tuple[list[TouchAction], dict]:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    acts = [TouchAction(a["t"], a["k"], a["f"], a["x"], a["y"]) for a in doc["actions"]]
    return acts, doc.get("meta", {})
