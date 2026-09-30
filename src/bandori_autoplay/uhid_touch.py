"""用 UHID 虚拟触摸屏注入触摸（非 root，系统认成真实外设）。

原理：scrcpy-server 支持在设备上创建 HID 设备（``/dev/uhid``）——

    UHID_CREATE(12)  注册一块「触摸屏」，附 HID 报告描述符
    UHID_INPUT(13)   发触摸报告（10 指同时上报状态）
    UHID_DESTROY(14) 注销

实测（本机 Redmi / Android 16，无 root）：

    dumpsys input:  9: autoplay-touch
        Classes: TOUCH | TOUCH_MT
        Path: /dev/input/event8

系统把它当成真正的多点触摸屏，游戏无法区分——比 InputManager 注入可靠得多。

坐标：报告描述符里的 Logical Maximum 要用**竖屏（自然方向）尺寸**
（本机 1156x2510），Android 才会 1:1 映射；横屏游戏坐标 (x, y) 转成
HID 坐标是 ``(Wp - y, x)``，与 phisap 的 ``x, y = device_width - y, x`` 一致。
"""

from __future__ import annotations

import struct
import threading
import time

from .touch import TouchBackend

MSG_UHID_CREATE = 12
MSG_UHID_INPUT = 13
MSG_UHID_DESTROY = 14

FINGERS = 10
REPORT_LEN = FINGERS * 5          # 每指 1 字节状态 + 2 字节 X + 2 字节 Y

_HEAD = bytes([0x05, 0x0D, 0x09, 0x04, 0xA1, 0x01, 0x15, 0x00])
_P1 = bytes([
    0x09, 0x22, 0xA1, 0x02, 0x09, 0x51, 0x75, 0x04, 0x95, 0x01, 0x25, 0x09,
    0x81, 0x02, 0x09, 0x42, 0x25, 0x01, 0x75, 0x01, 0x81, 0x02, 0x09, 0x32,
    0x25, 0x01, 0x81, 0x02, 0x75, 0x02, 0x81, 0x01, 0x05, 0x01, 0x09, 0x30,
    0x26,
])
_P2 = bytes([0x75, 0x10, 0x81, 0x02, 0x09, 0x31, 0x26])
_P3 = bytes([0x81, 0x02, 0x05, 0x0D, 0xC0])
_TAIL = bytes([0xC0])


def make_descriptor(width: int, height: int) -> bytes:
    body = _P1 + struct.pack("H", width) + _P2 + struct.pack("H", height) + _P3
    return _HEAD + body * FINGERS + _TAIL


class UhidTouch(TouchBackend):
    """触摸屏后端：状态式（每发一次报告就是全部手指的状态）。"""

    pre_issue_ms = 6.0

    def __init__(self, transport, panel_w: int, panel_h: int, screen_w: int, screen_h: int,
                 uid: int = 7, name: str = "autoplay-touch", log=None):
        """
        transport: 已连接 scrcpy-server 的对象（提供 ``_send``）
        panel_w/h: 面板自然方向尺寸（竖屏，如 1156x2510）
        screen_w/h: 游戏横屏逻辑尺寸（如 2510x1156）
        """
        self.t = transport
        self.panel_w, self.panel_h = panel_w, panel_h
        self.screen_w, self.screen_h = screen_w, screen_h
        self.uid = uid
        self.name = name
        self.log = log or (lambda *a: None)
        self._fingers: dict[int, tuple[int, int]] = {}
        self._lock = threading.Lock()
        self._next_slot = 1
        self._timers: list[threading.Timer] = []

    # ------------------------------------------------------------------
    def start(self) -> None:
        desc = make_descriptor(self.panel_w, self.panel_h)
        nm = self.name.encode()
        self.t._send(
            bytes([MSG_UHID_CREATE]) + struct.pack(">HHH", self.uid, 0, 0)
            + bytes([len(nm)]) + nm + struct.pack(">H", len(desc)) + desc
        )
        time.sleep(0.3)
        self.log(f"UHID 触摸屏已注册（{self.panel_w}x{self.panel_h}，{len(desc)} 字节描述符）")

    def _to_hid(self, x: int, y: int) -> tuple[int, int]:
        """横屏逻辑坐标 -> HID 坐标。"""
        return int(self.panel_w - y), int(x)

    def _report(self) -> bytes:
        out = bytearray()
        for i in range(FINGERS):
            if i in self._fingers:
                x, y = self._fingers[i]
                out += bytes([(i & 0x0F) | 0x30]) + struct.pack("<HH", x, y)
            else:
                out += bytes([i & 0x0F]) + struct.pack("<HH", 0, 0)
        return bytes(out)

    def _flush(self) -> None:
        data = self._report()
        self.t._send(bytes([MSG_UHID_INPUT]) + struct.pack(">HH", self.uid, len(data)) + data)

    # ------------------------------------------------------------------
    def down(self, slot: int, x: int, y: int) -> None:
        hx, hy = self._to_hid(x, y)
        with self._lock:
            self._fingers[slot & 0x0F] = (hx, hy)
            self._flush()

    def move(self, slot: int, x: int, y: int) -> None:
        self.down(slot, x, y)

    def up(self, slot: int) -> None:
        with self._lock:
            self._fingers.pop(slot & 0x0F, None)
            self._flush()

    def perform(self, presses: list[tuple[int, int]], y: int) -> None:
        """在 y 这一行同时按下若干位置；每项是 (x, 按住毫秒数)。"""
        if not presses:
            return
        slots: list[tuple[int, int]] = []
        with self._lock:
            for x, ms in presses:
                slot = self._next_slot
                self._next_slot = self._next_slot % 9 + 1
                hx, hy = self._to_hid(x, y)
                self._fingers[slot] = (hx, hy)
                slots.append((slot, ms))
            self._flush()

        def release() -> None:
            with self._lock:
                for slot, _ms in slots:
                    self._fingers.pop(slot, None)
                self._flush()

        longest = max(ms for _x, ms in presses)
        timer = threading.Timer(longest / 1000.0, release)
        timer.daemon = True
        timer.start()
        self._timers.append(timer)

    def close(self) -> None:
        for timer in self._timers:
            timer.cancel()
        self._timers.clear()
        try:
            with self._lock:
                self._fingers.clear()
                self._flush()
            self.t._send(bytes([MSG_UHID_DESTROY]) + struct.pack(">H", self.uid))
        except Exception:  # noqa: BLE001
            pass


class UhidBackend(UhidTouch):
    """把「scrcpy-server 传输 + UHID 虚拟触摸屏」打包成普通后端。

    ``make_touch`` 返回的就是它；``start`` / ``close`` 会连带管理
    scrcpy-server 常驻进程，调用方（演奏器、菜单）不需要知道底下有两层。
    """

    def __init__(self, transport, panel_w: int, panel_h: int,
                 screen_w: int, screen_h: int, **kw):
        super().__init__(transport, panel_w, panel_h, screen_w, screen_h, **kw)
        self._started = False

    def start(self) -> None:
        self.t.start()
        super().start()
        self._started = True

    def close(self) -> None:
        try:
            if self._started:
                super().close()
        finally:
            self._started = False
            self.t.close()
