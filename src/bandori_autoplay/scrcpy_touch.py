"""通过 scrcpy-server 的 control 通道注入触控。

为什么要绕这一圈：
* ``sendevent /dev/input/eventX`` 需要写输入设备节点，本机 SELinux 对
  shell 域直接拒绝（Permission denied），且没有 root 无法绕过。
* ``adb shell input`` 每次都要新起一个 app_process，实测 60~80ms，
  且只能单指，七轨音游的和弦根本来不及。
* scrcpy-server 是一个常驻 Java 进程，以 shell 身份（持有 INJECT_EVENTS）
  调用 Android 官方的 ``InputManager.injectInputEvent``，
  毫秒级延迟、支持真多指，是当前唯一可行的方案。

协议（scrcpy v4.1，32 字节定长）::

    0        u8   type = 2 (INJECT_TOUCH_EVENT)
    1        u8   action (0=DOWN 1=UP 2=MOVE)
    2..10    u64  pointer_id  (big endian)
    10..14   i32  x
    14..18   i32  y
    18..20   u16  screen width
    20..22   u16  screen height
    22..24   u16  pressure (16.16 定点)
    24..28   u32  action_button
    28..32   u32  buttons
"""

from __future__ import annotations

import socket
import struct
import subprocess
import threading
import time
import random
from pathlib import Path

from .adb import AdbDevice
from .touch import TouchBackend

MSG_INJECT_TOUCH = 2
ACTION_DOWN, ACTION_UP, ACTION_MOVE = 0, 1, 2

REMOTE_JAR = "/data/local/tmp/scrcpy-server.jar"
SERVER_CLASS = "com.genymobile.scrcpy.Server"
SERVER_VERSION = "4.1"


def _touch_msg(action: int, pointer_id: int, w: int, h: int, x: int, y: int, pressure: float,
               action_button: int = 0, buttons: int = 0) -> bytes:
    return struct.pack(
        ">BBQiiHHHII",
        MSG_INJECT_TOUCH,
        action,
        pointer_id,
        w,
        h,
        int(x),
        int(y),
        min(65535, int(max(0.0, min(1.0, pressure)) * 65536)),
        action_button,
        buttons,
    )


class ScrcpyTouch(TouchBackend):
    """把 scrcpy-server 当成一个可复用的多点触控注射器。"""

    pre_issue_ms = 3.0

    def __init__(
        self,
        device: AdbDevice,
        jar: str | Path,
        screen_w: int,
        screen_h: int,
        port: int = 27183,
        log=None,
    ):
        self.device = device
        self.jar = Path(jar)
        self.screen_w = int(screen_w)
        self.screen_h = int(screen_h)
        self.port = int(port)
        # 用随机 scid 给抽象 socket 起唯一名字，避免上次残留的服务器占着同名 socket
        self.scid = random.randrange(1, 0x7FFFFFFF)
        self.socket_name = f"scrcpy_{self.scid:08x}"
        self.log = log or (lambda *a: None)
        self._proc: subprocess.Popen | None = None
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        self._down: set[int] = set()
        # 抬起时必须沿用按下时的坐标：否则 UP 与 DOWN 位移过大，
        # 系统会判定成滑动而不是点击，按钮就不响应。
        self._pos: dict[int, tuple[int, int]] = {}

    # ------------------------------------------------------------------
    def start(self, timeout: float = 12.0, connect_delay: float = 2.5, attempts: int = 3) -> None:
        """启动服务器并建立 control 通道。

        踩过的坑：scrcpy-server 起来后需要约 1 秒完成初始化，
        如果立刻连上去，连接会在 ~1.5 秒后被对端掐断。
        所以这里「等一会儿再连 + 连上后探活 + 失败就整只重启」。
        """
        if not self.jar.exists():
            raise FileNotFoundError(f"缺少 scrcpy-server: {self.jar}")
        self.device.push(str(self.jar), REMOTE_JAR)
        self.device.shell(f"chmod 644 {REMOTE_JAR}")
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                self._spawn_server(connect_delay + attempt * 0.8)
                if self._connect_and_verify(timeout):
                    self.log(f"scrcpy-server 就绪 (port {self.port}, attempt {attempt + 1})")
                    return
                last = RuntimeError("连接后立刻被断开")
            except Exception as exc:  # noqa: BLE001
                last = exc
            self._kill_server()
        raise RuntimeError(f"启动 scrcpy-server 失败: {last}")

    # ------------------------------------------------------------------
    def _spawn_server(self, wait: float) -> None:
        subprocess.run(
            self.device._cmd(["forward", "--remove", f"tcp:{self.port}"]),
            capture_output=True,
            timeout=15,
        )
        subprocess.run(
            self.device._cmd(["forward", f"tcp:{self.port}", f"localabstract:{self.socket_name}"]),
            capture_output=True,
            timeout=15,
        )
        args = " ".join(
            [
                f"CLASSPATH={REMOTE_JAR}",
                "app_process",
                "/",
                SERVER_CLASS,
                SERVER_VERSION,
                f"scid={self.scid:x}",
                "log_level=info",
                "video=false",
                "audio=false",
                "control=true",
                "tunnel_forward=true",
                "send_dummy_byte=true",
            ]
        )
        self._proc = subprocess.Popen(
            self.device._cmd(["shell", args]),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
        )
        self._log_lines: list[str] = []
        self._pump = threading.Thread(target=self._pump_output, daemon=True)
        self._pump.start()
        time.sleep(max(0.0, wait))

    def _connect_and_verify(self, timeout: float) -> bool:
        deadline = time.perf_counter() + timeout
        sock: socket.socket | None = None
        while time.perf_counter() < deadline:
            try:
                s = socket.create_connection(("127.0.0.1", self.port), timeout=2.0)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                s.settimeout(2.0)
                try:
                    s.recv(1)          # 服务器连上后会先发一个探活字节
                except socket.timeout:
                    pass
                s.settimeout(None)
                sock = s
                break
            except OSError as exc:      # noqa: PERF203
                time.sleep(0.25)
        if sock is None:
            self.log("  connect 失败：一直连不上 forward 端口")
            return False
        # 探活：连上后若对端马上关掉，recv 会返回空字节
        time.sleep(1.0)
        sock.settimeout(1.0)
        try:
            alive = sock.recv(1, socket.MSG_PEEK) != b""
        except socket.timeout:
            alive = True
        except OSError:
            alive = False
        finally:
            try:
                sock.settimeout(None)
            except OSError:
                pass
        if not alive:
            self.log("  连上了但随即被对端关闭（服务器初始化未完成）")
            try:
                sock.close()
            except OSError:
                pass
            return False
        self._sock = sock
        return True

    def _kill_server(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=3)
            except Exception:  # noqa: BLE001
                pass
            self._proc = None
        subprocess.run(
            self.device._cmd(["shell", "pkill -f scrcpy-server.jar"]),
            capture_output=True,
            timeout=15,
        )
        subprocess.run(
            self.device._cmd(["forward", "--remove", f"tcp:{self.port}"]),
            capture_output=True,
            timeout=10,
        )

    def _pump_output(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").rstrip()
                if line:
                    self._log_lines.append(line)
                    if len(self._log_lines) > 200:
                        del self._log_lines[:100]
        except Exception:  # noqa: BLE001
            pass

    def server_log(self) -> str:
        return "\n".join(self._log_lines[-40:])

    # ------------------------------------------------------------------
    def _send(self, payload: bytes) -> None:
        if self._sock is None:
            raise RuntimeError("scrcpy-server 未启动")
        with self._lock:
            self._sock.sendall(payload)

    def down(self, slot: int, x: int, y: int) -> None:
        self._down.add(slot)
        self._pos[slot] = (int(x), int(y))
        self._send(_touch_msg(ACTION_DOWN, slot + 1, self.screen_w, self.screen_h, x, y, 1.0))

    def move(self, slot: int, x: int, y: int) -> None:
        self._pos[slot] = (int(x), int(y))
        self._send(_touch_msg(ACTION_MOVE, slot + 1, self.screen_w, self.screen_h, x, y, 1.0))

    def up(self, slot: int) -> None:
        x, y = self._pos.pop(slot, (0, 0))
        self._down.discard(slot)
        self._send(_touch_msg(ACTION_UP, slot + 1, self.screen_w, self.screen_h, x, y, 0.0))

    def close(self) -> None:
        for slot in list(self._down):
            try:
                self.up(slot)
            except Exception:  # noqa: BLE001
                pass
        self._kill_server()
