"""实时读触摸屏原始事件：找设备节点 + 把多点触摸（协议 B）事件流解析成 down/move/up。

本模块只做"读"，不管坐标换算——最小版只用它取**内核时间戳**。
真机 digitizer（如 goodix_ts）的坐标单位是 0.01 像素；我们自己用 UHID 造的
虚拟触摸屏则约等于面板宽（见 find_touch_device 的判别）。
"""

from __future__ import annotations

import re
import subprocess
import threading

from .adb import AdbDevice

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


def find_touch_device(device: AdbDevice, panel_w: int | None = None):
    """找**真实**多点触摸屏，返回 ``(节点, 坐标比例)``。

    要点：排除我们自己用 UHID 造出来的虚拟屏（名字里带 autoplay/uhid），
    并从 ABS_MT_POSITION_X 的 max 反推坐标单位——真机 digitizer 用「0.01 像素」，
    max 会接近面板宽的 100 倍；UHID 那块则约等于面板宽。
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
        cands.sort(key=lambda b: abs(b["xmax"] / float(panel_w) - 100.0))
    best = cands[0]
    scale = (best["xmax"] / float(panel_w)) if panel_w else 100.0
    return best["node"], scale


class TouchReader(threading.Thread):
    """后台跑 ``adb shell getevent``，把多点触摸协议 B 的事件流解析成动作。"""

    def __init__(self, device: AdbDevice, node: str, panel_w: int, panel_h: int,
                 log=None, scale: float | None = None):
        super().__init__(daemon=True)
        self.device = device
        self.node = node
        self.panel_w = panel_w
        self.scale = float(scale) if scale else 100.0
        self.log = log or (lambda *a: None)
        self.raw: list[tuple[float, str, int, int, int]] = []   # t_dev, kind, finger, x, y
        self._proc: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._slots: dict[int, tuple[int, int, int]] = {}
        self._cur_slot = 0
        self._pending: dict[int, tuple[int, int, int]] = {}

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
                if value == 0xFFFFFFFF:
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
