"""人手锚点：只取「你第一次按下」的内核时间戳，不碰位置。

位置由 geometry 从游戏数据算，跟你点在哪无关。所以这里不需要坐标单位、
不需要横屏正反（A/B）、不需要落点过滤 —— 那些适配项在"只当钟用"之后全部消失。

时间轴：
    F = 你手指真正碰到面板的内核时刻（getevent 的 [ 123.456 ]）
    L = 事件从内核读出来、经 adb 到我们手里的延迟
    λ = 我们发一条注入、到内核收到它的延迟
    我们看见你第一次落指的时刻 = F + L
    ⇒ 谱面 0 点（本机时刻） t0 = 观测时刻 - (L + λ) - 第 1 个音符的谱面时间
"""

from __future__ import annotations

import re
import time

from .touchlog import TouchReader


class TouchClock(TouchReader):
    """TouchReader 的实时版：额外记下每一行到达本机的时刻。

    默认走 pty（``adb shell -t``）：管道里的 stdout 是块缓冲，getevent 可能攒够
    4KB 才吐出来，那就没法实时接管了。
    """

    def __init__(self, *args, use_pty: bool = True, **kw):
        super().__init__(*args, **kw)
        self.use_pty = use_pty
        self.host_stamps: list[float] = []   # 与 raw 一一对应
        self._last_stamp = 0.0

    def start(self) -> None:
        if not self.use_pty:
            return super().start()
        import subprocess
        import threading

        self._proc = subprocess.Popen(
            self.device._cmd(["shell", "-t", "getevent", "-t", self.node]),
            # stdin 必须接 /dev/null：`adb shell -t` 会把本进程的 stdin 转发给手机端，
            # 不给它 /dev/null 的话键盘输入会被偷偷吃掉（表现为"卡在按回车那一步"）。
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=1,
            universal_newlines=True,
        )
        threading.Thread.start(self)

    def _feed(self, ts: float, etype: int, code: int, value: int) -> None:
        self._last_stamp = time.perf_counter()
        super()._feed(ts, etype, code, value)

    def _commit(self, ts: float) -> None:
        before = len(self.raw)
        super()._commit(ts)
        extra = len(self.raw) - before
        if extra > 0:
            self.host_stamps.extend([self._last_stamp or time.perf_counter()] * extra)

    def wait_down(self, *, timeout: float = 90.0, start: int = 0, log=None):
        """等下一次真实落指，返回 ``(下标, 内核时刻, 本机时刻)``；超时返回 None。"""
        say = log or (lambda *a: None)
        deadline = time.perf_counter() + timeout
        i = max(0, int(start))
        beat = time.perf_counter() + 15.0
        while time.perf_counter() < deadline:
            while i < len(self.raw):
                idx = i
                i += 1
                t_dev, kind, _slot, _x, _y = self.raw[idx]
                if kind != "down":
                    continue
                host = (self.host_stamps[idx] if idx < len(self.host_stamps)
                        else time.perf_counter())
                return idx, t_dev, host
            if time.perf_counter() >= beat:
                say("    还没收到你的落指…（脚本读不到触摸屏的话这里会一直空着）")
                beat = time.perf_counter() + 15.0
            time.sleep(0.004)
        return None


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


def find_uhid_node(device, panel_w: int, name_hint: str = "autoplay") -> str | None:
    """找我们自己那块 UHID 虚拟触摸屏的节点（标定往返延迟用）。

    UHID 描述符里的 Logical Maximum 用的是面板尺寸，所以它的
    ABS_MT_POSITION_X max ≈ 面板宽；真机 digitizer 则是 ≈ 面板宽 × 100。
    """
    cands = [b for b in input_devices(device) if b["xmax"] > 0]
    if not cands:
        return None
    named = [b for b in cands
             if name_hint in b["name"].lower() or "uhid" in b["name"].lower()]
    pool = named or [b for b in cands if abs(b["xmax"] - panel_w) < panel_w * 0.25]
    if not pool:
        return None
    pool.sort(key=lambda b: abs(b["xmax"] - panel_w))
    return pool[0]["node"]
