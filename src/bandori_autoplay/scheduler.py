"""时间轴调度：把谱面展开成触控事件并按时间精确触发。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .chart import Chart, Note


@dataclass
class TouchEvent:
    """一次完整触摸（按下→抬起）。"""

    lane: int
    down_ms: int
    up_ms: int
    x: int
    y: int
    kind: str = "tap"
    swipe_direction: str = "none"
    note: Note | None = None

    @property
    def duration_ms(self) -> int:
        return max(1, self.up_ms - self.down_ms)


@dataclass
class ScheduleStats:
    total: int = 0
    taps: int = 0
    holds: int = 0
    slides: int = 0
    flicks: int = 0
    late_events: int = 0
    fired: int = 0
    errors: int = 0
    max_lateness_ms: float = 0.0
    lateness_sum: float = 0.0
    lateness_count: int = 0

    def report(self) -> str:
        avg = self.lateness_sum / self.lateness_count if self.lateness_count else 0.0
        return (
            f"计划 {self.total} 个（tap={self.taps} hold={self.holds} "
            f"slide={self.slides} flick={self.flicks}）\n"
            f"触发 {self.fired} 个，失败 {self.errors} 个，迟到 {self.late_events} 个\n"
            f"平均延迟 {avg:.2f} ms，最大延迟 {self.max_lateness_ms:.2f} ms"
        )


def build_events(
    chart: Chart,
    lane_x: list[int],
    line_y: int,
    global_offset_ms: int = 0,
    tap_duration_ms: int = 40,
    drop_hold_tail: bool = True,
    assist_mode: bool = True,
) -> list[TouchEvent]:
    events: list[TouchEvent] = []
    for note in chart.sorted_notes():
        if note.lane >= len(lane_x):
            continue
        x = lane_x[note.lane]
        down = note.time_ms + chart.offset_ms - global_offset_ms
        if note.type == "hold":
            up = note.end_ms + chart.offset_ms - global_offset_ms
            if not drop_hold_tail:
                up += 40
        else:
            up = down + tap_duration_ms
        events.append(
            TouchEvent(
                lane=note.lane,
                down_ms=int(down),
                up_ms=int(up),
                x=int(x),
                y=int(line_y),
                kind=note.type,
                swipe_direction=note.swipe_direction,
                note=note,
            )
        )
    events.sort(key=lambda e: e.down_ms)
    return events


def summarize(events: list[TouchEvent]) -> ScheduleStats:
    st = ScheduleStats(total=len(events))
    for e in events:
        if e.kind == "tap":
            st.taps += 1
        elif e.kind == "hold":
            st.holds += 1
        elif e.kind == "slide":
            st.slides += 1
        elif e.kind == "flick":
            st.flicks += 1
    return st


class Clock:
    """高精度相对时钟：以某个 monotonic 起点为 0 ms。"""

    def __init__(self, start_ts: float | None = None):
        self.start_ts = start_ts if start_ts is not None else time.perf_counter()

    def now_ms(self) -> float:
        return (time.perf_counter() - self.start_ts) * 1000.0

    def reset(self) -> None:
        self.start_ts = time.perf_counter()

    def sleep_until(self, target_ms: float) -> float:
        """忙等到目标时刻，返回实际唤醒时的 now_ms。"""
        remaining = target_ms - self.now_ms()
        if remaining > 3:
            time.sleep(max(0.0, (remaining - 1.5) / 1000.0))
        while self.now_ms() < target_ms:
            pass
        return self.now_ms()
