"""在演奏画面里盯判定线：量出「我们计划的落指」比「音符真正压线」早还是晚。

为什么不靠开局那一下定死时间：

* 实机取流有固定延迟（实测 25~40 ms，用 show_touches 白点量出来的），
* 开局阶段游戏自己会慢一拍（加载 MV/背景/Live2D），
* 之后还可能有缓慢漂移。

所以除了开局给一个基准，还要在演奏中用画面里的音符持续反推偏差，
把基准一点点拉回正轨。这里的检测只看**判定线正上方那一条窄带**——
实机上这一带几乎不会误检（画面正中那个常驻亮块在更高的位置）。
"""

from __future__ import annotations

import collections
import statistics
import threading

import cv2
import numpy as np

from .chartdb import FAN, FieldGeometry

VY = 800.0          # 音符下落速度（流像素/秒，实机标定）


def note_samples(image: np.ndarray, ts: float, geom: FieldGeometry,
                 y_lo: float = 428.0, y_hi: float = 492.0
                 ) -> list[tuple[float, float]]:
    """一帧里判定线附近的音符 → ``(压线时刻, 音心)``，同一时钟。"""
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    mask = ((hsv[:, :, 2] >= 180) & (hsv[:, :, 1] <= 90)).astype(np.uint8) * 255
    n, _l, st, ct = cv2.connectedComponentsWithStats(mask, 8)
    out: list[tuple[float, float]] = []
    for j in range(1, n):
        _x, _y, w, h, a = st[j]
        if a < 250 or w < 60 or h > 60 or a / (w * h) < 0.70:
            continue
        cy, cx = float(ct[j][1]), float(ct[j][0])
        if not (y_lo <= cy <= y_hi):
            continue
        if abs(cx - geom.ax) > 1.05 * FAN * (cy - geom.ay):
            continue
        out.append((ts + (geom.judge_y - cy) / VY,
                    geom.stream_x_to_lane(cx, cy)))
    return out


class NoteWatcher(threading.Thread):
    """演奏过程中持续取流，把「计划落指时刻」与「音符压线时刻」的差攒起来。"""

    def __init__(self, stream, geom: FieldGeometry, events, t0: float, log,
                 latency_ms: float = 30.0, window: int = 6, max_err: float = 0.75,
                 stride: int = 3, apply: bool = False):
        super().__init__(daemon=True)
        self.stream = stream
        self.geom = geom
        self.events = events
        self.t0 = t0
        self.log = log
        self.latency = latency_ms / 1000.0
        self.max_err = max_err
        # 检测是重活，隔几帧算一次就够（音符在判定线附近停留约 80 ms）
        self.stride = max(1, stride)
        # 默认只观测不修正：实测误检会把整段落指推偏几百毫秒。
        self.apply = apply
        self._res = collections.deque(maxlen=window)
        self._stop = threading.Event()
        self._last = -1
        self.samples = 0
        self.frames = 0

    # ------------------------------------------------------------------
    def stop(self) -> None:
        self._stop.set()

    @property
    def shift(self) -> float:
        """要加到 t0 上的修正（正 = 整体延后）。"""
        if not self.apply:
            return 0.0
        if len(self._res) < 2:
            return 0.0
        return -statistics.median(self._res)

    def run(self) -> None:
        while not self._stop.is_set():
            fr = self.stream.latest(timeout=0.2, newer_than=self._last)
            if fr is None or fr.index == self._last:
                continue
            self._last = fr.index
            self.frames += 1
            if fr.index % self.stride:
                continue
            for tcross, lane in note_samples(fr.image, fr.ts, self.geom):
                self._push(tcross, lane)

    # ------------------------------------------------------------------
    def _push(self, tcross: float, lane: float) -> None:
        close = tcross - self.latency          # 音符真正压线的时刻（真机时钟）
        best = None
        for e in self.events:
            if abs(e.center - lane) > 1.6:
                continue
            err = (self.t0 + e.time_ms / 1000.0) - close   # 正 = 我们晚了
            if abs(err) > self.max_err:
                continue
            if best is None or abs(err) < abs(best[0]):
                best = (err, e)
        if best is None:
            return
        self.samples += 1
        self._res.append(best[0])
        if self.samples <= 3 or self.samples % 5 == 0:
            self.log(f"  对时观测：第 {best[1].time_ms/1000:5.2f}s 音符 偏差 "
                     f"{best[0]*1000:+5.0f} ms（样本 {self.samples}，"
                     f"当前修正 {self.shift*1000:+.0f} ms）")
