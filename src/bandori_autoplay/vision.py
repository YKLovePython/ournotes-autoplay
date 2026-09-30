"""实时视觉识别：轨道标定 + 音符检测。

设计目标
--------
* 输入一帧横屏游戏截图（BGR），输出 ``Detection`` 列表（轨道、x、y、类型、置信度）。
* 轨道几何（七轨 x 坐标、判定线 y）可以自动标定或从配置读取。
* 支持辅助模式：轻扫/滑条退化为点按。

检测思路（对占位/半透明音符皮肤都相对鲁棒）：
1. 截取判定线上方 ROI；
2. 用「亮 + 饱和」阈值得到候选像素（音符明显比轨道背景亮/艳）；
3. 形态学去噪 + 轮廓提取；
4. 按轨道中心归类；
5. 依据长宽比与面积区分 tap / hold / slide / flick。
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class LaneGeometry:
    count: int
    xs: list[int]
    line_y: int
    width: int = 0

    def nearest_lane(self, x: float, tolerance: float | None = None) -> int | None:
        if not self.xs:
            return None
        dists = [abs(x - lx) for lx in self.xs]
        idx = int(np.argmin(dists))
        if tolerance is not None and dists[idx] > tolerance:
            return None
        return idx


@dataclass
class Detection:
    lane: int
    x: int
    y: int
    kind: str = "tap"          # tap | hold | slide | flick
    confidence: float = 1.0
    height: int = 0
    width: int = 0
    area: float = 0.0
    swipe_direction: str = "none"

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{self.kind} lane={self.lane} x={self.x} y={self.y} conf={self.confidence:.2f}>"


@dataclass
class VisionConfig:
    detect_roi_top: int = 100
    detect_roi_bottom: int = 30
    min_area: float = 120
    lane_tolerance: float = 90
    value_min: int = 190
    saturation_min: int = 40
    dilate: int = 2
    max_detections_per_lane: int = 3


class NoteDetector:
    def __init__(self, geometry: LaneGeometry, cfg: VisionConfig | None = None):
        self.geo = geometry
        self.cfg = cfg or VisionConfig()
        self._kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))

    def build_mask(self, frame: np.ndarray) -> tuple[np.ndarray, int]:
        h, w = frame.shape[:2]
        y0 = max(0, int(self.cfg.detect_roi_top))
        y1 = min(h, int(self.geo.line_y + self.cfg.detect_roi_bottom))
        roi = frame[y0:y1, :]
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        v = hsv[:, :, 2]
        s = hsv[:, :, 1]
        mask = ((v >= self.cfg.value_min) | ((s >= 120) & (v >= 140))).astype(np.uint8) * 255
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, self._kernel)
        if self.cfg.dilate:
            mask = cv2.dilate(mask, self._kernel, iterations=self.cfg.dilate)
        return mask, y0

    def detect(self, frame: np.ndarray) -> list[Detection]:
        mask, y0 = self.build_mask(frame)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        per_lane: dict[int, list[Detection]] = {}
        for c in contours:
            area = cv2.contourArea(c)
            if area < self.cfg.min_area:
                continue
            x, y, bw, bh = cv2.boundingRect(c)
            cx = x + bw / 2
            cy = y + bh / 2 + y0
            lane = self.geo.nearest_lane(cx, self.cfg.lane_tolerance)
            if lane is None:
                continue
            kind, direction = self._classify(c, bw, bh)
            conf = min(1.0, area / (self.cfg.min_area * 6))
            det = Detection(
                lane=lane,
                x=int(cx),
                y=int(cy),
                kind=kind,
                confidence=conf,
                height=bh,
                width=bw,
                area=area,
                swipe_direction=direction,
            )
            per_lane.setdefault(lane, []).append(det)
        out: list[Detection] = []
        for lane, dets in per_lane.items():
            dets.sort(key=lambda d: d.y)
            out.extend(dets[: self.cfg.max_detections_per_lane])
        return out

    @staticmethod
    def _classify(contour, bw: int, bh: int) -> tuple[str, str]:
        if bh >= bw * 1.6:
            return "hold", "none"
        pts = contour.reshape(-1, 2)
        if len(pts) < 3:
            return "tap", "none"
        mask = np.zeros((bh, bw), np.uint8)
        shift = pts - [pts[:, 0].min(), pts[:, 1].min()]
        cv2.drawContours(mask, [shift.reshape(-1, 1, 2)], -1, 255, -1)
        top = mask[: max(1, bh // 3), :]
        cols = np.where(top.sum(axis=0) > 0)[0]
        if len(cols) > 2:
            center = cols.mean()
            if center < bw * 0.35:
                return "flick", "right"
            if center > bw * 0.65:
                return "flick", "left"
        ratio = bw / max(bh, 1)
        if 0.7 <= ratio <= 1.4:
            return "tap", "none"
        return "slide", "none"

    def draw(self, frame: np.ndarray, detections: list[Detection]) -> np.ndarray:
        vis = frame.copy()
        for x in self.geo.xs:
            cv2.line(vis, (x, 0), (x, vis.shape[0]), (60, 60, 60), 1)
        cv2.line(vis, (0, self.geo.line_y), (vis.shape[1], self.geo.line_y), (0, 255, 0), 2)
        colors = {
            "tap": (0, 255, 255),
            "hold": (255, 128, 0),
            "slide": (255, 0, 255),
            "flick": (0, 128, 255),
        }
        for d in detections:
            c = colors.get(d.kind, (255, 255, 255))
            cv2.circle(vis, (d.x, d.y), 14, c, 2)
            cv2.putText(
                vis,
                f"{d.kind}:{d.lane}",
                (max(0, d.x - 30), max(20, d.y - 18)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                c,
                2,
            )
        return vis


def auto_lane_positions(
    frame: np.ndarray,
    count: int = 7,
    line_y: int | None = None,
    search: tuple[int, int] | None = None,
) -> list[int]:
    """从游戏截图里估计轨道中心 x 坐标。

    做法：在判定线上方取一条水平带，计算每列的垂直边缘能量，在等距先验附近
    搜索最强边缘，作为轨道中心。
    """
    h, w = frame.shape[:2]
    y1 = line_y if line_y is not None else int(h * 0.82)
    band = frame[max(0, y1 - 220) : y1, :]
    gray = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY).astype(np.float32)
    grad = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    energy = np.abs(grad).mean(axis=0)
    energy = cv2.GaussianBlur(energy.reshape(1, -1), (1, 31), 0).ravel()

    if search:
        lo, hi = search
    else:
        lo, hi = int(w * 0.05), int(w * 0.95)
    span = hi - lo
    step = span / (count + 1)
    xs = []
    for i in range(1, count + 1):
        center = int(lo + step * i)
        win = max(12, int(step * 0.25))
        a, b = max(lo, center - win), min(hi, center + win)
        if b <= a:
            xs.append(center)
            continue
        local = energy[a:b]
        xs.append(int(a + int(np.argmax(local))))
    return xs


def estimate_line_y(frame: np.ndarray, ratio: float = 0.82) -> int:
    return int(frame.shape[0] * ratio)
