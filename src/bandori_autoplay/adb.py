"""ADB 设备封装：截图、shell、应用控制、输入事件。

全部通过 subprocess 调用 adb，二进制安全（避免 PowerShell 重定向损坏 PNG）。
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

PACKAGE = "com.bilibili.sirius.official"
ACTIVITY = "com.bangdream.sirius.MainActivity"


class AdbError(RuntimeError):
    pass


def _default_adb() -> str:
    found = shutil.which("adb")
    if found:
        return found
    # 本机已知的 platform-tools 位置
    candidates = [
        r"C:\Users\67063\Desktop\_桌面整理_20260730\04_其他项目与代码\platform-tools\adb.exe",
        r"C:\Users\67063\AppData\Local\Android\Sdk\platform-tools\adb.exe",
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    return "adb"


@dataclass
class DeviceInfo:
    serial: str
    model: str
    brand: str
    android: str
    sdk: str
    width: int
    height: int
    density: int


class AdbDevice:
    def __init__(self, serial: str | None = None, adb_path: str | None = None, timeout: int = 60):
        self.adb = adb_path or _default_adb()
        self.serial = serial
        self.timeout = timeout
        if not self.serial:
            self.serial = self._first_device()

    # ---------- 基础 ----------
    def _cmd(self, args: list[str]) -> list[str]:
        cmd = [self.adb]
        if self.serial:
            cmd += ["-s", self.serial]
        return cmd + args

    def shell(self, command: str, timeout: int | None = None, binary: bool = False):
        proc = subprocess.run(
            self._cmd(["shell", command]),
            capture_output=True,
            timeout=timeout or self.timeout,
        )
        if binary:
            return proc.stdout
        out = proc.stdout.decode("utf-8", "replace")
        err = proc.stderr.decode("utf-8", "replace")
        if proc.returncode != 0 and not out:
            raise AdbError(err.strip() or f"adb shell 失败: {command}")
        return out

    def _first_device(self) -> str:
        proc = subprocess.run([self.adb, "devices"], capture_output=True, timeout=30)
        for line in proc.stdout.decode("utf-8", "replace").splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                return parts[0]
        raise AdbError("没有在线的 ADB 设备")

    def devices(self) -> list[tuple[str, str]]:
        proc = subprocess.run([self.adb, "devices"], capture_output=True, timeout=30)
        out = []
        for line in proc.stdout.decode("utf-8", "replace").splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2:
                out.append((parts[0], parts[1]))
        return out

    # ---------- 信息 ----------
    def prop(self, name: str) -> str:
        return self.shell(f"getprop {name}").strip()

    def info(self) -> DeviceInfo:
        size = self.shell("wm size").strip()
        density = self.shell("wm density").strip()
        w = h = 0
        for token in size.replace("\r", " ").split():
            if "x" in token and token[0].isdigit():
                a, _, b = token.partition("x")
                w, h = int(a), int(b)
        dens = 0
        for token in density.split():
            if token.isdigit():
                dens = int(token)
        return DeviceInfo(
            serial=self.serial,
            model=self.prop("ro.product.model"),
            brand=self.prop("ro.product.brand"),
            android=self.prop("ro.build.version.release"),
            sdk=self.prop("ro.build.version.sdk"),
            width=w,
            height=h,
            density=dens,
        )

    # ---------- 截图 ----------
    def screenshot(self, path: str | Path, remote: str = "/sdcard/_bd_shot.png") -> Path:
        """截图并保存到本地（二进制安全）。"""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = subprocess.run(
            self._cmd(["exec-out", "screencap", "-p"]),
            capture_output=True,
            timeout=self.timeout,
        ).stdout
        if not data.startswith(b"\x89PNG"):
            # 退化方案：先写设备再 pull
            self.shell(f"screencap -p {remote}")
            subprocess.run(
                self._cmd(["pull", remote, str(path)]),
                capture_output=True,
                timeout=self.timeout,
            )
        else:
            path.write_bytes(data)
        return path

    def screenshot_array(self):
        """返回 BGR numpy 数组（用于实时识别）。"""
        import cv2
        import numpy as np

        data = subprocess.run(
            self._cmd(["exec-out", "screencap", "-p"]),
            capture_output=True,
            timeout=self.timeout,
        ).stdout
        if not data.startswith(b"\x89PNG"):
            raise AdbError("screencap 返回数据异常")
        arr = np.frombuffer(data, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            raise AdbError("PNG 解码失败")
        return img

    # ---------- 应用 ----------
    def current_focus(self) -> str:
        out = self.shell("dumpsys window | grep -E 'mCurrentFocus'")
        return out.strip()

    def is_game_foreground(self) -> bool:
        return PACKAGE in self.current_focus()

    def launch(self, wait: float = 0.0) -> None:
        self.shell(f"monkey -p {PACKAGE} -c android.intent.category.LAUNCHER 1")
        if wait:
            time.sleep(wait)

    def start_activity(self, component: str = f"{PACKAGE}/{ACTIVITY}") -> None:
        self.shell(f"am start -n {component}")

    def stop(self) -> None:
        self.shell(f"am force-stop {PACKAGE}")

    def screen_on(self) -> None:
        self.shell("input keyevent KEYCODE_WAKEUP")

    def keep_awake(self) -> None:
        self.shell("svc power stayon true")

    # ---------- 输入（低精度，仅用于 UI 导航） ----------
    def input_tap(self, x: int, y: int) -> None:
        self.shell(f"input tap {int(x)} {int(y)}")

    def input_swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int = 200) -> None:
        self.shell(f"input swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)} {int(ms)}")

    def input_key(self, key: str) -> None:
        self.shell(f"input keyevent {key}")

    # ---------- 调试 ----------
    def logcat_dump(self, lines: int = 500, grep: str | None = None) -> str:
        cmd = f"logcat -d -t {int(lines)}"
        if grep:
            cmd += f" | grep -iE '{grep}'"
        return self.shell(cmd)

    def list_encrypted_bundles(self) -> list[str]:
        base = f"/sdcard/Android/data/{PACKAGE}/files/EncryptedBundles"
        out = self.shell(f"ls {base} 2>/dev/null")
        return [line.strip() for line in out.splitlines() if line.strip()]

    def pull(self, remote: str, local: str) -> None:
        subprocess.run(self._cmd(["pull", remote, local]), capture_output=True, timeout=600)

    def push(self, local: str, remote: str) -> None:
        subprocess.run(self._cmd(["push", local, remote]), capture_output=True, timeout=600)

    def persistent_shell(self) -> "PersistentShell":
        return PersistentShell(self)


class PersistentShell:
    """常驻 ``adb shell`` 进程：把命令写进 stdin，避免反复 fork 造成的 30~80ms 延迟。

    这是音游自动演奏的关键优化点——每个音符的触控都要在 ~5ms 内送达设备。
    """

    def __init__(self, device: AdbDevice):
        self.device = device
        self._lock = threading.Lock()
        self._proc = subprocess.Popen(
            device._cmd(["shell"]),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )

    def run(self, command: str) -> None:
        with self._lock:
            if self._proc.poll() is not None:
                self._proc = subprocess.Popen(
                    self.device._cmd(["shell"]),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    bufsize=0,
                )
            assert self._proc.stdin is not None
            self._proc.stdin.write((command + "\n").encode("utf-8"))
            self._proc.stdin.flush()

    def close(self) -> None:
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
            self._proc.terminate()
        except Exception:  # noqa: BLE001
            pass
