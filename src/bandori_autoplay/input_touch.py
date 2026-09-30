"""用 adb 的 ``input`` 命令注入触控。

为什么不用 scrcpy 通道：实测在《Our Notes》里 **scrcpy 注入的触摸不被游戏接受**
（点开始键、点判定线都没有任何反应），而 ``input`` 系列游戏是认的。

每次调用要新起一个 app_process，实测中位 61ms、抖动约 ±10ms，所以：

* 提前 ``PRE_ISSUE_MS`` 就把命令发出去，让落点回到目标时刻；
* **全部异步发起**（不等待命令返回）。长按如果同步等待，会把后面几秒的音符
  全顶掉——早期版本就是这么漏掉长按的；
* 和弦用设备端 ``&`` 并行发起，几根手指几乎同时落下；
* 长按用 ``input motionevent DOWN`` + 延时 ``UP``，按住时长由我们控制。
"""

from __future__ import annotations

import subprocess
import threading

from .adb import AdbDevice
from .touch import TouchBackend

PRE_ISSUE_MS = 61.0
HOLD_THRESHOLD_MS = 90          # 超过这个时长按长按处理


class InputGestureBackend(TouchBackend):
    pre_issue_ms = PRE_ISSUE_MS

    def __init__(self, device: AdbDevice, tap_ms: int = 45, log=None):
        self.device = device
        self.tap_ms = int(tap_ms)
        self.log = log or (lambda *a: None)
        self._pending: list[subprocess.Popen] = []

    # ------------------------------------------------------------------
    def _spawn(self, script: str) -> None:
        """发起一条设备端 shell 脚本，立刻返回（不等它跑完）。"""
        try:
            p = subprocess.Popen(
                self.device._cmd(["shell", script]),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception as exc:  # noqa: BLE001
            self.log(f"input 注入异常: {exc}")
            return
        self._pending.append(p)
        if len(self._pending) > 24:
            self._pending = [q for q in self._pending if q.poll() is None]

    def perform(self, presses: list[tuple[int, int]], y: int) -> None:
        """在 y 这一行同时按下若干位置；每项是 (x, 按住毫秒数)。"""
        if not presses:
            return
        taps = [x for x, ms in presses if ms <= HOLD_THRESHOLD_MS]
        holds = [(x, ms) for x, ms in presses if ms > HOLD_THRESHOLD_MS]

        if taps:
            cmds = [f"input tap {int(x)} {int(y)}" for x in taps]
            self._spawn(" & ".join(c + " &" for c in cmds) if len(cmds) > 1 else cmds[0])
        for x, ms in holds:
            self._spawn(f"input motionevent DOWN {int(x)} {int(y)}")
            threading.Timer(ms / 1000.0, self._release, args=(x, y)).start()

    def _release(self, x: int, y: int) -> None:
        self._spawn(f"input motionevent UP {int(x)} {int(y)}")

    # 兼容 TouchBackend 的单指接口
    def down(self, slot: int, x: int, y: int) -> None:  # pragma: no cover
        self._spawn(f"input motionevent DOWN {int(x)} {int(y)}")

    def up(self, slot: int) -> None:  # pragma: no cover
        pass

    def close(self) -> None:
        for p in self._pending:
            try:
                p.wait(timeout=1)
            except Exception:  # noqa: BLE001
                pass
        self._pending.clear()
