"""演奏区几何：谱面格位 → 设备像素。

这里的每个数都来自游戏自己的数据（见 geometry/notes_geometry.json 的出处），
并用实机无损截图复核过。**不是**从画面里量出来的近似值，也不需要任何标定。

一句话公式（横屏，W x H 设备像素）：

    1 平面单位 = H / 10.8 像素
    x(格位 c) = W/2 + (c - 12) * 0.64237 * H / 10.8        c 取 0..24
    判定线 y = H * 0.79259

为什么是"按屏高"：判定平面挂在相机前 10.6078 处，而该处视锥半高正好是 5.4048，
等于平面自身的半高 5.4 —— 也就是平面高度 1:1 铺在屏幕高度上；屏幕更宽只是两侧留白。
（另一路佐证：Live/LiveMainCamera 是正交相机，size 也正好是 5.4。）
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

LANE_COUNT = 24.0
PLANE_HEIGHT = 10.8          # 平面高（单位 = 100 设计像素）
PLANE_HALF_HEIGHT = 5.4
JUDGE_PLANE_Y = -3.16        # 判定线在平面上的 y
APEX_PLANE_Y = 5.9684        # 两侧边交点（透视顶点）
LANE_PITCH = 0.64237         # 一格的平面宽度
HALF_WIDTH = 7.7085          # 判定线处的半宽（= LANE_PITCH * 12）

def app_root() -> Path:
    """程序根目录：打包后是 exe 所在目录，源码运行时是仓库根。

    几何数据放在**exe 旁边**（不是 _internal 里），这样游戏更新后重新导出、
    直接替换那个 json 就行，不用重打包。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


GEOMETRY_JSON = app_root() / "geometry" / "notes_geometry.json"


def load_constants(path: Path | None = None) -> dict:
    """读导出物里的常量；读不到就用模块里的内置值（两者应一致）。"""
    p = path or GEOMETRY_JSON
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
        c = doc.get("常量") or {}
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    mapping = {
        "lane_count": "lane_count", "plane_height": "plane_height",
        "judge_plane_y": "judge_plane_y", "apex_plane_y": "apex_plane_y",
        "lane_pitch_plane": "lane_pitch", "half_width_plane": "half_width",
    }
    for k, attr in mapping.items():
        if k in c:
            out[attr] = float(c[k])
    return out


@dataclass(frozen=True)
class Field:
    """一台设备上的演奏区。"""

    screen_w: int
    screen_h: int

    @property
    def scale(self) -> float:
        """1 平面单位 = 多少设备像素。"""
        return self.screen_h / PLANE_HEIGHT

    @property
    def judge_y(self) -> int:
        """判定线的设备 y。"""
        return int(round((PLANE_HALF_HEIGHT - JUDGE_PLANE_Y) / PLANE_HEIGHT * self.screen_h))

    @property
    def center_x(self) -> float:
        return self.screen_w / 2.0

    @property
    def unit_px(self) -> float:
        """谱面一格的像素宽（24 格制）。"""
        return LANE_PITCH * self.scale

    @property
    def playfield_half_px(self) -> float:
        """判定线处场地半宽（像素）。"""
        return HALF_WIDTH * self.scale

    def x_of(self, center: float) -> int:
        """谱面格位（position + size/2，0..24）→ 设备 x。"""
        return int(round(self.center_x + (center - LANE_COUNT / 2.0) * self.unit_px))

    def describe(self) -> str:
        return (f"{self.screen_w}x{self.screen_h}  判定线 y={self.judge_y} "
                f"({self.judge_y / self.screen_h * 100:.2f}% 屏高)  "
                f"一格 {self.unit_px:.1f}px  场地半宽 ±{self.playfield_half_px:.0f}px")


def field_for(screen_w: int, screen_h: int) -> Field:
    """按屏幕尺寸给出演奏区（横向居中、按屏高缩放）。"""
    return Field(int(screen_w), int(screen_h))
