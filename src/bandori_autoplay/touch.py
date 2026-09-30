"""低延迟多点触控注入。

支持四种后端：
* ``uhid``：借 scrcpy-server 注册一块**虚拟触摸屏**（系统认成真外设），
  毫秒级、真多指、游戏无法区分（本机实测可用，推荐）
* ``sendevent``：直接写 /dev/input/eventX，延迟最低，但本机 SELinux 拒绝（不可用）
* ``minitouch``：需要推 minitouch 二进制（部分设备可用）
* ``input``：adb shell input，只用于兜底（不支持真正多指、延迟高）

设备坐标系：ABS_MT_POSITION_X/Y，最大值 = 屏幕像素 * coord_scale（本机为 100）。
"""

from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass

from .adb import AdbDevice

# Linux input 事件编码
EV_SYN = 0x00
EV_KEY = 0x01
EV_ABS = 0x03

SYN_REPORT = 0x00
BTN_TOUCH = 0x14A

ABS_MT_SLOT = 0x2F
ABS_MT_TOUCH_MAJOR = 0x30
ABS_MT_POSITION_X = 0x35
ABS_MT_POSITION_Y = 0x36
ABS_MT_TRACKING_ID = 0x39


def _event(etype: int, code: int, value: int) -> str:
    return f"sendevent {{node}} {etype} {code} {value}"


class TouchBackend:
    # 注入命令从发出到落点生效的固定提前量（毫秒）；排程时会提前这么久发出
    pre_issue_ms: float = 0.0

    def perform(self, presses: list[tuple[int, int]], y: int) -> None:
        """在 y 这一行同时按下若干位置；每项是 (x, 按住毫秒数)。默认实现走 down/up。"""
        longest = max((ms for _x, ms in presses), default=0)
        slots = []
        for i, (x, _ms) in enumerate(presses):
            slot = i + 1
            self.down(slot, x, y)
            slots.append(slot)
        time.sleep(longest / 1000.0)
        for slot in slots:
            self.up(slot)

    def down(self, slot: int, x: int, y: int) -> None:  # pragma: no cover
        raise NotImplementedError

    def move(self, slot: int, x: int, y: int) -> None:  # pragma: no cover
        raise NotImplementedError

    def up(self, slot: int) -> None:  # pragma: no cover
        raise NotImplementedError

    def tap(self, x: int, y: int, duration_ms: int = 40, slot: int = 0) -> None:
        self.down(slot, x, y)
        time.sleep(duration_ms / 1000.0)
        self.up(slot)

    def hold(self, x: int, y: int, duration_ms: int, slot: int = 0) -> None:
        self.down(slot, x, y)
        time.sleep(max(duration_ms, 1) / 1000.0)
        self.up(slot)

    def swipe(self, points: list[tuple[int, int]], duration_ms: int, slot: int = 0) -> None:
        if not points:
            return
        self.down(slot, *points[0])
        if len(points) > 1:
            step = duration_ms / max(len(points) - 1, 1) / 1000.0
            for p in points[1:]:
                time.sleep(step)
                self.move(slot, *p)
        time.sleep(0.02)
        self.up(slot)

    def close(self) -> None:
        pass


class SendEventBackend(TouchBackend):
    """通过 adb shell 批量 sendevent 注入（一次 shell 调用写多条事件，降低延迟）。"""

    def __init__(
        self,
        device: AdbDevice,
        node: str,
        scale: int = 100,
        max_slots: int = 10,
        persistent: bool = True,
    ):
        self.device = device
        self.node = node
        self.scale = scale
        self.max_slots = max_slots
        self._next_id = 1000
        self._lock = threading.Lock()
        self._base = f"sendevent {node}"
        self._shell = device.persistent_shell() if persistent else None

    def _run(self, lines: list[str]) -> None:
        if not lines:
            return
        script = "; ".join(lines)
        if self._shell is not None:
            self._shell.run(script)
            return
        subprocess.run(
            self.device._cmd(["shell", script]),
            capture_output=True,
            timeout=20,
        )

    def close(self) -> None:
        if self._shell is not None:
            self._shell.close()
            self._shell = None

    def _shift(self, value: int) -> int:
        return int(value) * self.scale

    def down(self, slot: int, x: int, y: int) -> None:
        with self._lock:
            tid = self._next_id
            self._next_id += 1
        px, py = self._shift(x), self._shift(y)
        self._run(
            [
                f"{self._base} {EV_ABS} {ABS_MT_SLOT} {slot}",
                f"{self._base} {EV_ABS} {ABS_MT_TRACKING_ID} {tid}",
                f"{self._base} {EV_ABS} {ABS_MT_POSITION_X} {px}",
                f"{self._base} {EV_ABS} {ABS_MT_POSITION_Y} {py}",
                f"{self._base} {EV_ABS} {ABS_MT_TOUCH_MAJOR} 200",
                f"{self._base} {EV_KEY} {BTN_TOUCH} 1",
                f"{self._base} {EV_SYN} {SYN_REPORT} 0",
            ]
        )

    def move(self, slot: int, x: int, y: int) -> None:
        px, py = self._shift(x), self._shift(y)
        self._run(
            [
                f"{self._base} {EV_ABS} {ABS_MT_SLOT} {slot}",
                f"{self._base} {EV_ABS} {ABS_MT_POSITION_X} {px}",
                f"{self._base} {EV_ABS} {ABS_MT_POSITION_Y} {py}",
                f"{self._base} {EV_SYN} {SYN_REPORT} 0",
            ]
        )

    def up(self, slot: int) -> None:
        self._run(
            [
                f"{self._base} {EV_ABS} {ABS_MT_SLOT} {slot}",
                f"{self._base} {EV_ABS} {ABS_MT_TRACKING_ID} -1",
                f"{self._base} {EV_KEY} {BTN_TOUCH} 0",
                f"{self._base} {EV_SYN} {SYN_REPORT} 0",
            ]
        )


class InputBackend(TouchBackend):
    """兜底：adb shell input（单指、高延迟）。"""

    def __init__(self, device: AdbDevice):
        self.device = device
        self._held: dict[int, tuple[int, int]] = {}

    def down(self, slot: int, x: int, y: int) -> None:
        self._held[slot] = (x, y)
        self.device.shell(f"input motionevent DOWN {int(x)} {int(y)}")

    def move(self, slot: int, x: int, y: int) -> None:
        self.device.shell(f"input motionevent MOVE {int(x)} {int(y)}")

    def up(self, slot: int) -> None:
        x, y = self._held.pop(slot, (0, 0))
        self.device.shell(f"input motionevent UP {int(x)} {int(y)}")


def make_touch(
    cfg: dict,
    device: AdbDevice,
    *,
    screen_w: int | None = None,
    screen_h: int | None = None,
    jar: str | None = None,
) -> TouchBackend:
    backend = cfg.get("backend", "sendevent")
    if backend in ("input", "input-gesture"):
        from .input_touch import InputGestureBackend

        return InputGestureBackend(device, int(cfg.get("tap_duration_ms", 45)))
    if backend in ("uhid", "scrcpy-uhid"):
        from .scrcpy_touch import ScrcpyTouch
        from .uhid_touch import UhidBackend

        info = device.info()
        if screen_w is None or screen_h is None:
            screen_w, screen_h = info.height, info.width      # 横屏
        panel_w, panel_h = info.width, info.height            # 面板自然方向（竖屏）
        transport = ScrcpyTouch(
            device, jar or cfg.get("scrcpy_jar", ""), screen_w, screen_h
        )
        return UhidBackend(transport, panel_w, panel_h, screen_w, screen_h)
    if backend == "scrcpy":
        from .scrcpy_touch import ScrcpyTouch

        if screen_w is None or screen_h is None:
            info = device.info()
            screen_w, screen_h = info.height, info.width      # 横屏
        return ScrcpyTouch(device, jar or cfg.get("scrcpy_jar", ""), screen_w, screen_h)
    if backend == "sendevent":
        return SendEventBackend(
            device,
            cfg.get("event_node", "/dev/input/event7"),
            int(cfg.get("coord_scale", 100)),
            int(cfg.get("max_slots", 10)),
        )
    return InputBackend(device)


@dataclass
class TouchPoint:
    lane: int
    x: int
    y: int
    slot: int
