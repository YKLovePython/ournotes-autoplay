"""低延迟实时画面采集。

链路：``adb exec-out screenrecord --output-format=h264 -`` → **独立进程的 ffmpeg**
解码成 bgr24 原始帧 → 主线程按固定大小读取。

为什么不直接用 PyAV：本机 PyAV 解 H.264 会在几秒后原生崩溃（访问违例，
0xC0000005），而且不留 Python 异常，整个脚本会静默死掉。
换成独立的 ffmpeg 进程后，解码崩溃最多只影响取流，主程序不受牵连。
"""

from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass

import numpy as np

from .adb import AdbDevice


@dataclass
class Frame:
    index: int
    ts: float          # time.perf_counter()
    image: np.ndarray  # BGR


def _ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("需要 ffmpeg：请先 pip install imageio-ffmpeg") from exc


class ScreenStream:
    """后台线程持续解码屏幕流，主线程通过 :meth:`latest` 取最新帧。"""

    def __init__(
        self,
        device: AdbDevice,
        size: str = "1280x592",
        bit_rate: int = 8_000_000,
        time_limit: int = 180,
        extra: list[str] | None = None,
    ):
        self.device = device
        self.size = size
        self.bit_rate = bit_rate
        self.time_limit = time_limit
        self.extra = extra or []
        self._adb: subprocess.Popen | None = None
        self._ff: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._frame: Frame | None = None
        self._stop = threading.Event()
        self._count = 0
        self._start_ts = 0.0
        self.error: str | None = None
        w, _, h = size.lower().partition("x")
        self.width, self.height = int(w), int(h)

    # ------------------------------------------------------------------
    def start(self) -> None:
        adb_cmd = self.device._cmd(
            [
                "exec-out",
                "screenrecord",
                "--output-format=h264",
                f"--time-limit={self.time_limit}",
                f"--size={self.size}",
                f"--bit-rate={self.bit_rate}",
                *self.extra,
                "-",
            ]
        )
        # 注意：这里不能加 bufsize=0。Windows 下无缓冲管道会让下游 ffmpeg
        # 立刻收到 EOF 而退出（表现为一帧都解不出来）。
        self._adb = subprocess.Popen(adb_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        ff = _ffmpeg_exe()
        self._ff = subprocess.Popen(
            [
                ff, "-hide_banner", "-loglevel", "warning",
                "-f", "h264", "-i", "pipe:0",
                "-f", "rawvideo", "-pix_fmt", "bgr24", "-",
            ],
            stdin=self._adb.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if self._adb.stdout is not None:
            self._adb.stdout.close()          # 交给 ffmpeg 持有
        self._err_thread = threading.Thread(target=self._drain_err, daemon=True)
        self._err_thread.start()
        self._start_ts = time.perf_counter()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _drain_err(self) -> None:
        if self._ff is None or self._ff.stderr is None:
            return
        try:
            for line in self._ff.stderr:
                self.error = line.decode("utf-8", "replace").strip() or self.error
        except Exception:  # noqa: BLE001
            pass

    def _run(self) -> None:
        assert self._ff is not None and self._ff.stdout is not None
        nbytes = self.width * self.height * 3
        try:
            while not self._stop.is_set():
                buf = self._ff.stdout.read(nbytes)
                if not buf or len(buf) < nbytes:
                    break
                img = np.frombuffer(buf, dtype=np.uint8).reshape((self.height, self.width, 3))
                with self._lock:
                    self._count += 1
                    self._frame = Frame(self._count, time.perf_counter(), img.copy())
        except Exception as exc:  # noqa: BLE001
            self.error = f"{type(exc).__name__}: {exc}"

    # ------------------------------------------------------------------
    def latest(self, timeout: float = 2.0, newer_than: int = -1) -> Frame | None:
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            with self._lock:
                frame = self._frame
            if frame is not None and frame.index > newer_than:
                return frame
            time.sleep(0.002)
        with self._lock:
            return self._frame

    def fps(self) -> float:
        if not self._start_ts:
            return 0.0
        return self._count / max(1e-6, time.perf_counter() - self._start_ts)

    def stop(self) -> None:
        self._stop.set()
        for proc in (self._ff, self._adb):
            if proc is not None:
                try:
                    proc.terminate()
                except Exception:  # noqa: BLE001
                    pass
        if self._thread:
            self._thread.join(timeout=2.0)

    def __enter__(self) -> "ScreenStream":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()


def wait_live_frame(stream: "ScreenStream", timeout: float = 25.0, log=None):
    """等「演奏画面出现」，返回 ``(本机时刻, 帧)``；超时返回 ``(None, None)``。"""
    from .livestate import is_live_frame

    say = log or (lambda *a: None)
    deadline = time.perf_counter() + timeout
    last = -1
    frames = 0
    while time.perf_counter() < deadline:
        fr = stream.latest(timeout=0.5, newer_than=last)
        if fr is None or fr.index == last:
            continue
        last = fr.index
        frames += 1
        if is_live_frame(fr.image):
            say(f"  演奏画面已出现（第 {frames} 帧）")
            return fr.ts, fr
    say(f"  超时：{frames} 帧内没等到演奏画面")
    return None, None
