"""谱面数据模型与解析器。

统一输出结构（每个音符）::

    {"time_ms": int, "lane": int, "type": "tap|hold|slide|flick",
     "duration_ms": int, "swipe_direction": "left|right|none"}

支持格式：
* ``json``  —— 本项目标准谱面（charts/*.json）
* ``sus``   —— BanG Dream 系（Sliding Universal Score）文本谱面
* ``csv``   —— 简易表格，便于人工录入
"""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

NOTE_TYPES = {"tap", "hold", "slide", "flick"}
DIRECTIONS = {"left", "right", "none"}


@dataclass
class Note:
    time_ms: int
    lane: int
    type: str = "tap"
    duration_ms: int = 0
    swipe_direction: str = "none"

    def __post_init__(self) -> None:
        if self.type not in NOTE_TYPES:
            raise ValueError(f"未知音符类型: {self.type}")
        if self.swipe_direction not in DIRECTIONS:
            raise ValueError(f"未知滑动方向: {self.swipe_direction}")
        if self.duration_ms < 0:
            raise ValueError("duration_ms 不能为负")

    @property
    def end_ms(self) -> int:
        return self.time_ms + max(self.duration_ms, 0)

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Chart:
    title: str = ""
    artist: str = ""
    difficulty: str = "normal"
    offset_ms: int = 0
    lane_count: int = 7
    notes: list[Note] = field(default_factory=list)
    source: str = ""

    def sorted_notes(self) -> list[Note]:
        return sorted(self.notes, key=lambda n: (n.time_ms, n.lane))

    def duration_ms(self) -> int:
        return max((n.end_ms for n in self.notes), default=0)

    def as_dict(self) -> dict:
        return {
            "title": self.title,
            "artist": self.artist,
            "difficulty": self.difficulty,
            "offset_ms": self.offset_ms,
            "lane_count": self.lane_count,
            "source": self.source,
            "notes": [n.as_dict() for n in self.sorted_notes()],
        }

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.as_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
        return p


# --------------------------------------------------------------------------
# 解析器
# --------------------------------------------------------------------------


def parse_json(path: str | Path) -> Chart:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    notes = [Note(**{k: v for k, v in n.items() if k in Note.__dataclass_fields__}) for n in data.get("notes", [])]
    return Chart(
        title=data.get("title", ""),
        artist=data.get("artist", ""),
        difficulty=data.get("difficulty", "normal"),
        offset_ms=int(data.get("offset_ms", 0)),
        lane_count=int(data.get("lane_count", 7)),
        notes=notes,
        source=str(path),
    )


def parse_csv(path: str | Path) -> Chart:
    notes: list[Note] = []
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            notes.append(
                Note(
                    time_ms=int(float(row.get("time_ms", 0))),
                    lane=int(row.get("lane", 0)),
                    type=row.get("type", "tap") or "tap",
                    duration_ms=int(float(row.get("duration_ms", 0) or 0)),
                    swipe_direction=row.get("swipe_direction", "none") or "none",
                )
            )
    return Chart(notes=notes, source=str(path))


def parse_sus(path: str | Path, lane_count: int = 7) -> Chart:
    """解析 SUS 文本谱面（BanG Dream 系通用交换格式）。

    关键指令::

        #BPM01: 120
        #00002: 01
        0003: 1

    其中第 4 位表示类型：1=Tap 2=Hold(链) 3=Slide …（各实现略有差异），
    这里给出通用实现，允许通过 ``LANE_TYPE`` 表调整。
    """
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    bpm_changes: dict[int, float] = {}
    bpm = 120.0
    raw_notes: list[tuple[int, int, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        if line.startswith("#BPM"):
            _, value = line.split(":", 1)
            bpm_changes[len(bpm_changes) + 1] = float(value)
            continue
        if line.startswith("#"):
            continue
        if ":" not in line:
            continue
        measure_part, value = line.split(":", 1)
        if not measure_part.isdigit() or len(measure_part) < 4:
            continue
        measure = int(measure_part[:3])
        split = int(measure_part[3:])
        raw_notes.append((measure, split, value.strip()))

    # 简化版：假设 4/4 拍、BPM 不变（如需精确请扩展 bpm_changes 与拍号处理）
    beat_ms = 60000.0 / bpm
    measure_ms = beat_ms * 4
    notes: list[Note] = []
    for measure, split, value in raw_notes:
        t = int(measure * measure_ms + (split / 16.0) * measure_ms)
        for lane in range(lane_count):
            if len(value) <= lane or value[lane] == "0":
                continue
            kind = value[lane]
            ntype = "tap"
            direction = "none"
            if kind in ("2", "5"):  # 长条
                ntype = "hold"
            elif kind in ("3", "4"):
                ntype = "slide"
            notes.append(Note(time_ms=t, lane=lane, type=ntype, swipe_direction=direction))
    return Chart(notes=notes, source=str(path), lane_count=lane_count)


PARSERS = {
    ".json": parse_json,
    ".csv": parse_csv,
    ".sus": parse_sus,
    ".txt": parse_sus,
}


def load_chart(path: str | Path) -> Chart:
    ext = Path(path).suffix.lower()
    if ext not in PARSERS:
        raise ValueError(f"不支持的谱面格式: {ext}")
    return PARSERS[ext](path)


def make_demo_chart(lane_count: int = 7) -> Chart:
    """生成一段演示谱面：单点 + 长按 + 滑动，用于跑通链路。"""
    notes: list[Note] = []
    t = 1000
    for i in range(24):
        lane = i % lane_count
        notes.append(Note(time_ms=t, lane=lane, type="tap"))
        t += 220
    t += 400
    notes.append(Note(time_ms=t, lane=3, type="hold", duration_ms=1200))
    t += 1600
    notes.append(Note(time_ms=t, lane=2, type="flick", swipe_direction="right"))
    notes.append(Note(time_ms=t + 120, lane=4, type="flick", swipe_direction="left"))
    return Chart(title="demo", artist="internal", difficulty="easy", notes=notes)
