"""谱面数据库（chartdb）读取与落点几何。

谱面来自 `our-notes-chartdb` 发布的 `charts/<musicId>/<difficulty>.json`
（schema `chartdoc/1`）。位置空间是 **24 格**（见 ournotes-player 的
数据格式文档：`laneCount = 24`），`position` 是起始格、`size` 是跨格宽度。

画面是自顶部中心发散的扇形，音符沿扇形径向下落，落点公式：

    center = position + size / 2
    x(y)   = ax + (center / 12 - 1) * fan * (y - ay)

参数由实机标定（1280x592 流）：ax=639.5, ay=-36.5, fan=0.831,
判定线 y=491.6。用实录视频与谱面对时验证，命中率 96.9%。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

# 实机标定值（以 1280x592 的视频流为基准，换算成比例以便适配其它流尺寸）
AX_RATIO = 639.5 / 1280.0
AY_RATIO = -36.5 / 592.0
FAN = 0.831
JUDGE_RATIO = 491.6 / 592.0
LANES = 24.0


@dataclass(frozen=True)
class ChartEvent:
    """一次需要落指的事件（长条只保留头，释放时间放 end_ms）。"""

    kind: str                 # tap | flick | long
    time_ms: int              # 命中时刻（谱面时间）
    end_ms: int | None        # 长条释放时刻
    center: float             # 音心（0..24）
    size: float
    direction: str = "normal"
    # 长条的节点轨迹 ``((t_ms, center), ...)``（含头尾）。
    # 实测：全部谱面里 23073 个长条有 11986 个头尾位置不同——也就是一半以上的
    # 长条会横移，光在头部按住不跟手是会掉的。
    path: tuple = ()
    # 长条**尾部**要求轻扫时的方向（up/left/right）。全库 2512 个长条带这个尾巴。
    tail_flick: str = ""
    # 长条**头部**就是轻扫时的方向（少见，全库 200 个）。
    head_flick: str = ""

    @property
    def is_hold(self) -> bool:
        return self.end_ms is not None and self.end_ms > self.time_ms


def _center(n: dict) -> float:
    """节点音心。缺 ``position`` 的节点退化按宽度算——全库 2091 个这样的节点
    都是长条的中间刻度，真正的插值交给 :func:`_path_of`。"""
    pos = n.get("position")
    if pos is None:
        return float(n.get("size") or 0.0) / 2.0
    return float(pos) + float(n.get("size") or 0.0) / 2.0


def _path_of(inner: list[dict]) -> tuple:
    """长条节点轨迹 ``((t_ms, center), ...)``。

    中间刻度有 2091 个不带 ``position``（头尾从不缺），按时间在前后两个已知位置
    之间**线性插值**——长条是滑过去的，插值比"原地不动"更贴合实际位置。
    """
    known = [i for i, x in enumerate(inner) if x.get("position") is not None]
    if not known:
        return ()
    out: list[tuple[int, float]] = []
    for i, x in enumerate(inner):
        t = int(x["timeMs"])
        if x.get("position") is not None:
            out.append((t, _center(x)))
            continue
        prev = max((k for k in known if k < i), default=None)
        nxt = min((k for k in known if k > i), default=None)
        if prev is None:
            out.append((t, _center(inner[nxt])))
        elif nxt is None:
            out.append((t, _center(inner[prev])))
        else:
            a, b = inner[prev], inner[nxt]
            ta, tb = int(a["timeMs"]), int(b["timeMs"])
            r = 0.0 if tb == ta else (t - ta) / (tb - ta)
            ca, cb = _center(a), _center(b)
            out.append((t, ca + (cb - ca) * r))
    return tuple(out)


def load_events(path: str | Path) -> list[ChartEvent]:
    """把 chartdb 的 chartdoc/1 展平成按时间排序的落指事件。"""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    out: list[ChartEvent] = []

    def walk(nodes: list[dict]) -> None:
        for n in nodes:
            if n.get("container"):
                inner = [x for x in (n.get("node") or []) if x.get("timeMs") is not None]
                if not inner:
                    continue
                inner.sort(key=lambda x: x["timeMs"])
                head, tail = inner[0], inner[-1]
                kind = "long"
                head_flick = ""
                if str(head.get("type")) == "flick":
                    kind = "flick"
                    head_flick = str(head.get("direction") or "up")
                tail_flick = ""
                if str(tail.get("type")) == "flick":
                    tail_flick = str(tail.get("direction") or "up")
                out.append(
                    ChartEvent(
                        kind=kind,
                        time_ms=int(head["timeMs"]),
                        end_ms=int(tail["timeMs"]),
                        center=_center(head),
                        size=float(head.get("size") or 0.0),
                        direction=str(head.get("direction") or "normal"),
                        path=_path_of(inner),
                        tail_flick=tail_flick,
                        head_flick=head_flick,
                    )
                )
                continue
            if n.get("timeMs") is None:
                continue
            kind = str(n.get("type") or "tap")
            if kind not in ("tap", "flick", "long"):
                continue                 # guide 等只作视觉引导，不落指
            out.append(
                ChartEvent(
                    kind=kind,
                    time_ms=int(n["timeMs"]),
                    end_ms=None,
                    center=_center(n),
                    size=float(n.get("size") or 0.0),
                    direction=str(n.get("direction") or "normal"),
                )
            )

    walk(doc["notes"])
    out.sort(key=lambda e: e.time_ms)
    return out


class FieldGeometry:
    """把谱面位置换算成设备屏幕坐标。"""

    def __init__(self, stream_size: tuple[int, int], screen_w: int, screen_h: int):
        sw, sh = stream_size
        self.sw, self.sh = sw, sh
        self.ax = AX_RATIO * sw
        self.ay = AY_RATIO * sh
        self.judge_y = JUDGE_RATIO * sh
        self.half = FAN * (self.judge_y - self.ay)
        self.screen_w, self.screen_h = screen_w, screen_h
        self.scale_x = sw / screen_w
        self.scale_y = sh / screen_h

    def lane_to_stream_x(self, center: float, y: float | None = None) -> float:
        yy = self.judge_y if y is None else y
        return self.ax + (center / (LANES / 2.0) - 1.0) * FAN * (yy - self.ay)

    def stream_x_to_lane(self, x: float, y: float | None = None) -> float:
        yy = self.judge_y if y is None else y
        return ((x - self.ax) / (FAN * (yy - self.ay)) + 1.0) * (LANES / 2.0)

    def device_x(self, center: float) -> int:
        return int(round(self.lane_to_stream_x(center) / self.scale_x))

    @property
    def device_judge_y(self) -> int:
        return int(round(self.judge_y / self.scale_y))
