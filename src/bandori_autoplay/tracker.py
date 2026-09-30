"""音符检测 + 轨迹跟踪 + 命中点预测（不依赖谱面）。

核心思想：不猜轨道网格，而是**跟踪每个音符自己的轨迹**：

1. 检测亮色音符条 -> 得到 (x, y)；
2. 用 x = a*y + b 拟合（轨道是从顶点放射的直线，所以 x 关于 y 线性）；
3. 由同一束轨迹估计 y 方向速度，预测到达判定线的时间；
4. 到点就按下 —— 位置直接取 a*judge_y + b。

这样即使不知道游戏到底几轨、轨道在哪，也能打准。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import cv2
import numpy as np


@dataclass
class Blob:
    """一帧里的一个音符候选（坐标为画面坐标）。"""

    x: float
    y: float
    w: int
    h: int
    area: float
    kind: str = "tap"
    direction: str = "none"
    color: str = "unknown"


@dataclass
class Observation:
    ts: float
    x: float
    y: float
    w: int
    h: int
    kind: str


@dataclass
class Track:
    id: int
    obs: list[Observation] = field(default_factory=list)
    state: str = "active"        # active | fired | dropped
    fired_at: float = 0.0
    fired_x: float = 0.0
    predicted_hit_ts: float = 0.0
    last_w: float = 0.0

    @property
    def last(self) -> Observation:
        return self.obs[-1]

    def __len__(self) -> int:
        return len(self.obs)


class NoteDetectorConfig:
    def __init__(self, **kw):
        self.value_min = kw.get("value_min", 185)
        self.sat_max = kw.get("sat_max", 120)
        self.min_width = kw.get("min_width", 26)
        self.min_area = kw.get("min_area", 160)
        self.max_height_ratio = kw.get("max_height_ratio", 0.55)
        # 填充率：音符是实心横条（≈0.7~1.0）；轨道边框/判定线连成的
        # 细线结构填充率极低（≈0.02），必须排除，否则会被当成一个巨大的"音符"。
        self.min_fill = kw.get("min_fill", 0.45)
        self.roi_top = kw.get("roi_top", 40)
        self.roi_bottom = kw.get("roi_bottom", 500)
        self.track_half_width_ratio = kw.get("track_half_width_ratio", 0.84)
        self.apex_x = kw.get("apex_x", 0.5)
        self.apex_y = kw.get("apex_y", -0.06)      # 相对画面高度
        self.line_ratio = kw.get("line_ratio", 0.83)

    def apex(self, shape) -> tuple[float, float]:
        h, w = shape[:2]
        return self.apex_x * w, self.apex_y * h

    def half_width(self, y: float, shape) -> float:
        ax, ay = self.apex(shape)
        return self.track_half_width_ratio * (y - ay)


class NoteDetector:
    def __init__(self, cfg: NoteDetectorConfig | None = None):
        self.cfg = cfg or NoteDetectorConfig()
        self._k = cv2.getStructuringElement(cv2.MORPH_RECT, (9, 3))

    def detect(self, frame: np.ndarray) -> list[Blob]:
        cfg = self.cfg
        h, w = frame.shape[:2]
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        v = hsv[:, :, 2].astype(np.int16)
        s = hsv[:, :, 1].astype(np.int16)
        hue = hsv[:, :, 0].astype(np.int16)
        mask = ((v >= cfg.value_min) & (s <= cfg.sat_max)).astype(np.uint8) * 255
        mask[: cfg.roi_top, :] = 0
        mask[cfg.roi_bottom :, :] = 0
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, self._k)
        n, _lab, stats, cent = cv2.connectedComponentsWithStats(mask, 8)
        ax, _ay = cfg.apex(frame.shape)
        out: list[Blob] = []
        for i in range(1, n):
            x, y, bw, bh, area = stats[i]
            if bw < cfg.min_width or area < cfg.min_area:
                continue
            if bh > bw * cfg.max_height_ratio:
                continue
            if area < cfg.min_fill * bw * bh:
                continue
            cx, cy = float(cent[i][0]), float(cent[i][1])
            if abs(cx - ax) > cfg.half_width(cy, frame.shape) * 1.02:
                continue
            # 颜色分类（用于区分音符类型，阈值只做粗分类）
            patch = frame[max(0, int(cy) - 4) : int(cy) + 5, max(0, int(cx) - 6) : int(cx) + 7]
            kind, color, direction = "tap", "white", "none"
            if patch.size:
                hh = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV).reshape(-1, 3).mean(axis=0)
                hue_mean = float(hh[0])
                if 15 <= hue_mean <= 40:
                    kind, color = "flick", "yellow"
                    direction = "right" if cx < ax else "right"
                elif 40 < hue_mean <= 90:
                    kind, color = "slide", "green"
                elif hue_mean > 140:
                    kind, color = "slide", "pink"
                elif 90 <= hue_mean <= 140:
                    kind, color = "tap", "blue"
            out.append(Blob(cx, cy, int(bw), int(bh), float(area), kind, direction, color))
        return out


class NoteTracker:
    """把逐帧检测结果关联成轨迹，并预测命中时刻/位置。"""

    def __init__(
        self,
        judge_y: float,
        gate_x: float = 120.0,
        gate_y: float = 200.0,
        max_gap_s: float = 0.12,
        min_obs: int = 3,
        max_obs: int = 40,
    ):
        self.judge_y = judge_y
        self.gate_x = gate_x
        self.gate_y = gate_y
        self.max_gap_s = max_gap_s
        self.min_obs = min_obs
        self.max_obs = max_obs
        self.tracks: list[Track] = []
        self._next_id = 1

    # ------------------------------------------------------------------
    def update(self, ts: float, blobs: list[Blob]) -> None:
        used: set[int] = set()
        for tr in self.tracks:
            if tr.state != "active" or not tr.obs:
                continue
            last = tr.last
            if ts - last.ts > self.max_gap_s:
                tr.state = "dropped"
                continue
            # 预测下一帧位置（用最近两点的线性外推）
            px, py = last.x, last.y
            if len(tr) >= 2:
                p0, p1 = tr.obs[-2], tr.obs[-1]
                dt = max(1e-6, p1.ts - p0.ts)
                k = min((ts - p1.ts) / dt, 3.0)
                px = p1.x + (p1.x - p0.x) * k
                py = p1.y + (p1.y - p0.y) * k
            best = None
            best_d = 1e9
            for idx, b in enumerate(blobs):
                if idx in used:
                    continue
                dx, dy = abs(b.x - px), abs(b.y - py)
                if dx > self.gate_x or dy > self.gate_y:
                    continue
                # 宽度突变说明是另一个音符（宽条/窄条差别很大）
                if last.w and b.w:
                    wr = max(last.w, b.w) / max(1, min(last.w, b.w))
                    if wr > 2.2:
                        continue
                    dw = abs(b.w - last.w) * 0.6
                else:
                    dw = 0.0
                d = dx + dy * 0.8 + dw
                if d < best_d:
                    best_d, best = d, idx
            if best is None:
                continue
            used.add(best)
            b = blobs[best]
            tr.obs.append(Observation(ts, b.x, b.y, b.w, b.h, b.kind))
            tr.last_w = b.w
            if len(tr.obs) > self.max_obs:
                tr.obs = tr.obs[-self.max_obs :]
        for idx, b in enumerate(blobs):
            if idx in used:
                continue
            self.tracks.append(
                Track(self._next_id, [Observation(ts, b.x, b.y, b.w, b.h, b.kind)])
            )
            self._next_id += 1
        # 清理
        self.tracks = [t for t in self.tracks if t.state != "dropped" or len(t) >= self.min_obs]
        if len(self.tracks) > 80:
            self.tracks = self.tracks[-80:]

    # ------------------------------------------------------------------
    def predict(self, tr: Track) -> tuple[float, float, float] | None:
        """返回 (x_at_judge, t_hit, vy)。"""
        if len(tr) < self.min_obs:
            return None
        ys = np.array([o.y for o in tr.obs], dtype=np.float64)
        xs = np.array([o.x for o in tr.obs], dtype=np.float64)
        ts = np.array([o.ts for o in tr.obs], dtype=np.float64)
        if ys[-1] - ys[0] < 8:
            return None
        a, b = np.polyfit(ys, xs, 1)
        x_judge = a * self.judge_y + b
        # y 方向速度（用最近若干点线性拟合，透视加速度在最后阶段影响较小）
        n = min(len(tr), 6)
        t_rel = ts[-n:] - ts[-n]
        y_rel = ys[-n:]
        if t_rel[-1] <= 0:
            return None
        vy, c = np.polyfit(t_rel, y_rel, 1)
        if vy <= 1e-3:
            return None
        t_hit = ts[-1] + (self.judge_y - ys[-1]) / vy
        return float(x_judge), float(t_hit), float(vy)

    def pending_hits(self, now: float, horizon_s: float) -> list[tuple[Track, float, float, float]]:
        """返回 horizon 内即将命中、且尚未触发的轨迹。"""
        out = []
        for tr in self.tracks:
            if tr.state != "active":
                continue
            pr = self.predict(tr)
            if pr is None:
                continue
            x_judge, t_hit, vy = pr
            if t_hit < now - 0.05:
                tr.state = "dropped"     # 已经过线但没触发
                continue
            if t_hit - now <= horizon_s:
                tr.predicted_hit_ts = t_hit
            out.append((tr, x_judge, t_hit, vy))
        out.sort(key=lambda item: item[2])
        return out

    # ------------------------------------------------------------------
    def suppress_neighbours(self, track: Track, x_judge: float, window: float = 260.0) -> None:
        """同一个音符可能被拆成多条轨迹，触发时把附近的重复轨迹一起作废。"""
        tr_last = track.last
        for other in self.tracks:
            if other is track or other.state != "active" or not other.obs:
                continue
            if abs(other.last.y - tr_last.y) > 90:
                continue
            if abs(other.last.x - tr_last.x) > window:
                continue
            other.state = "dropped"


class LaneGrid:
    """7 轨网格（用于把预测点吸附到合法落点）。"""

    def __init__(
        self,
        center_x: float,
        pitch: float,
        count: int = 7,
        judge_y: float = 0.0,
        apex_y: float = 0.0,
    ):
        self.center_x = center_x
        self.pitch = pitch
        self.count = count
        self.judge_y = judge_y
        self.apex_y = apex_y

    def centers_at(self, y: float | None = None) -> list[float]:
        k = 1.0
        if y is not None and self.judge_y and y > self.apex_y:
            k = (y - self.apex_y) / (self.judge_y - self.apex_y)
        mid = (self.count - 1) / 2.0
        return [self.center_x + (i - mid) * self.pitch * k for i in range(self.count)]

    def snap(self, x: float, width_px: float = 0.0, unit: float = 393.0, y: float | None = None) -> float:
        """把预测落点吸附到最近的合法中心。

        宽音符条跨 n 轨时，中心应落在 lane_i + (n-1)*pitch/2 上。
        """
        centers = self.centers_at(y)
        n = max(1, int(round(width_px / unit))) if width_px else 1
        k = 1.0
        if y is not None and self.judge_y and y > self.apex_y:
            k = (y - self.apex_y) / (self.judge_y - self.apex_y)
        cands = []
        for i in range(self.count):
            if i + n - 1 >= self.count and n > 1:
                continue
            cands.append(centers[i] + (n - 1) * self.pitch * k / 2.0)
        if not cands:
            cands = centers
        return min(cands, key=lambda c: abs(c - x))
