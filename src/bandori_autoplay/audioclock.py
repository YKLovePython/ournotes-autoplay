"""用游戏自己的音频播放位置当歌曲时钟。

MIUI 会在 logcat 里打游戏音频轨的播放进度：

    D/AudioTrackImpl( 3302): [audioTrackData][fine] 50s(f:50000 m:0 s:0 k:0 z:0) : pid 3302 ...

其中 ``f`` 是**已播放毫秒数**——就是谱面用的那条时间轴。歌曲刚开始时计数器会
从小值重新计数，那一行就能反推出「谱面时间 0」的真实时刻：

    t0 = 日志到达时刻 - f/1000

刷新间隔约 5 秒（静音段是 1 秒），所以它赶不上第一个音符；
定位是**校正源**：画面先起跑，音频一到就把基准修正过来。
"""

from __future__ import annotations

import re
import subprocess
import threading
import time

from .adb import AdbDevice

LINE = re.compile(r"\[audioTrackData\]\[(\w+)\]\s+(\d+)s\(f:(\d+)")


class AudioClock:
    def __init__(self, device: AdbDevice, tag: str = "AudioTrackImpl", log=None):
        self.device = device
        self.tag = tag
        self.log = log or (lambda *a: None)
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.samples: list[tuple[float, int]] = []      # (主机时刻, 已播放毫秒)
        self.error: str | None = None

    # ------------------------------------------------------------------
    def start(self) -> None:
        cmd = self.device._cmd(["logcat", "-T", "1", "-s", f"{self.tag}:D"])
        self._proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            encoding="utf-8", errors="replace",
        )
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            for line in self._proc.stdout:
                if self._stop.is_set():
                    break
                now = time.perf_counter()
                m = LINE.search(line)
                if not m:
                    continue
                ms = int(m.group(3))
                with self._lock:
                    self.samples.append((now, ms))
                    if len(self.samples) > 400:
                        del self.samples[:200]
        except Exception as exc:  # noqa: BLE001
            self.error = f"{type(exc).__name__}: {exc}"

    # ------------------------------------------------------------------
    def snapshot(self) -> list[tuple[float, int]]:
        with self._lock:
            return list(self.samples)

    def estimate_t0(self, since: float, min_ms: int = 0, max_ms: int = 12000) -> tuple[float, int] | None:
        """从 ``since`` 之后的样本里找"新开的音轨"，反推谱面时间 0。

        新音轨的特征：已播放毫秒数明显小于前一条（计数器重置）。
        返回 (t0, 该样本的毫秒数)。
        """
        prev_ms = None
        best = None
        for ts, ms in self.snapshot():
            if ts < since:
                continue
            if prev_ms is not None and ms < prev_ms - 300 and ms <= max_ms:
                best = (ts - ms / 1000.0, ms)
            prev_ms = ms
            if ms <= max_ms and ms >= min_ms:
                best = (ts - ms / 1000.0, ms)
        return best

    def close(self) -> None:
        self._stop.set()
        if self._proc is not None:
            try:
                self._proc.terminate()
            except Exception:  # noqa: BLE001
                pass
            self._proc = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
