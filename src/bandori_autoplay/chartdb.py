"""谱面读取：把 our-notes-chartdb 的 ``chartdoc/1`` 展平成按时间排序的落指事件。

位置空间是 24 格（``laneCount = 24``）：``position`` 是起始格、``size`` 是跨格宽度，
``中心 = position + size/2``。换算成屏幕像素由 geometry 负责，这里只管解析。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ChartEvent:
    """一次需要落指的事件（长条只留头，释放时间放 end_ms）。"""

    kind: str
    time_ms: int
    end_ms: int | None
    center: float
    size: float
    direction: str = "normal"
    path: tuple = ()
    tail_flick: str = ""
    head_flick: str = ""

    @property
    def is_hold(self) -> bool:
        return self.end_ms is not None and self.end_ms > self.time_ms


def _center(n: dict) -> float:
    pos = n.get("position")
    if pos is None:
        return float(n.get("size") or 0.0) / 2.0
    return float(pos) + float(n.get("size") or 0.0) / 2.0


def _path_of(inner: list[dict]) -> tuple:
    """长条节点轨迹：中间刻度缺 position 时，按时间在前后的已知位置间线性插值。"""
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
                out.append(ChartEvent(kind=kind, time_ms=int(head["timeMs"]),
                                      end_ms=int(tail["timeMs"]), center=_center(head),
                                      size=float(head.get("size") or 0.0),
                                      direction=str(head.get("direction") or "normal"),
                                      path=_path_of(inner), tail_flick=tail_flick,
                                      head_flick=head_flick))
                continue
            if n.get("timeMs") is None:
                continue
            kind = str(n.get("type") or "tap")
            if kind not in ("tap", "flick", "long"):
                continue
            out.append(ChartEvent(kind=kind, time_ms=int(n["timeMs"]), end_ms=None,
                                  center=_center(n), size=float(n.get("size") or 0.0),
                                  direction=str(n.get("direction") or "normal")))

    walk(doc["notes"])
    out.sort(key=lambda e: e.time_ms)
    return out


def load_title_map(master_dir: str | Path) -> dict[int, str]:
    """曲目号 → 游戏里显示的日文原名（选歌列表用）。"""
    master_dir = Path(master_dir)
    titles: dict[str, dict] = {}
    text = master_dir / "MasterText.json"
    if text.exists():
        for row in json.loads(text.read_text(encoding="utf-8"))["_allData"]:
            titles[row["_id"]] = row
    music: dict[int, dict] = {}
    mfile = master_dir / "MasterLiveMusic.json"
    if mfile.exists():
        for row in json.loads(mfile.read_text(encoding="utf-8"))["_allData"]:
            music[int(row["_id"])] = row
    out: dict[int, str] = {}
    for mid, row in music.items():
        t = (titles.get(str(row.get("_titleTextID") or ""))
             or titles.get(f"Music_Tilte_{mid}") or titles.get(f"Music_Title_{mid}") or {})
        name = (t.get("_japanese") or t.get("_traditionalChinese")
                or t.get("_simplifiedChinese") or t.get("_english") or "")
        out[mid] = name or f"(未命名 {mid})"
    return out
