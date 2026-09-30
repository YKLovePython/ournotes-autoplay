"""演奏主循环：谱面驱动 / 视觉驱动 / 混合。"""

from __future__ import annotations

import time
from dataclasses import dataclass

import cv2
import numpy as np

from .adb import AdbDevice
from .chart import Chart
from .scheduler import Clock, ScheduleStats, TouchEvent, build_events, summarize
from .touch import TouchBackend
from .vision import Detection, LaneGeometry, NoteDetector, VisionConfig


class ChartPlayer:
    """按谱面时间轴触发触控。"""

    def __init__(
        self,
        device: AdbDevice,
        touch: TouchBackend,
        lane_x: list[int],
        line_y: int,
        global_offset_ms: int = 0,
        tap_duration_ms: int = 40,
        drop_hold_tail: bool = True,
        assist_mode: bool = True,
        logger=print,
    ):
        self.device = device
        self.touch = touch
        self.lane_x = lane_x
        self.line_y = line_y
        self.global_offset_ms = global_offset_ms
        self.tap_duration_ms = tap_duration_ms
        self.drop_hold_tail = drop_hold_tail
        self.assist_mode = assist_mode
        self.log = logger

    def play(self, chart: Chart, lead_in_ms: int = 3000, dry_run: bool = False) -> ScheduleStats:
        events = build_events(
            chart,
            self.lane_x,
            self.line_y,
            self.global_offset_ms,
            self.tap_duration_ms,
            self.drop_hold_tail,
            self.assist_mode,
        )
        stats = summarize(events)
        self.log(f"谱面 {chart.title or chart.source}: {stats.total} 个音符, 时长 {chart.duration_ms()} ms")
        if not events:
            return stats

        # 把所有按下/抬起合并成一条事件流，用单线程顺序执行（长按用独立 slot 保持）
        schedule: list[tuple[int, TouchEvent, str]] = []
        for e in events:
            schedule.append((e.down_ms, e, "down"))
            schedule.append((e.up_ms, e, "up"))
        schedule.sort(key=lambda t: (t[0], 0 if t[2] == "down" else 1))

        clock = Clock()
        clock.reset()
        base = events[0].down_ms - lead_in_ms
        slot_of: dict[int, int] = {}
        free_slots = list(range(1, 9))
        active: dict[int, TouchEvent] = {}

        self.log(f"等待前奏 {lead_in_ms} ms 后开始演奏（dry_run={dry_run}）")
        for t_ms, event, action in schedule:
            target = t_ms - base
            now = clock.sleep_until(target)
            lateness = now - target
            stats.lateness_sum += lateness
            stats.lateness_count += 1
            stats.max_lateness_ms = max(stats.max_lateness_ms, lateness)
            if lateness > 60:
                stats.late_events += 1
            if dry_run:
                stats.fired += 1
                continue
            try:
                key = id(event)
                if action == "down":
                    slot = next((s for s in free_slots if s not in slot_of.values()), 0)
                    slot_of[key] = slot
                    active[slot] = event
                    if event.kind == "hold":
                        self.touch.down(slot, event.x, event.y)
                    elif event.kind == "flick" and not self.assist_mode:
                        dx = 120 if event.swipe_direction == "right" else -120
                        self.touch.swipe(
                            [(event.x, event.y + 40), (event.x + dx, event.y - 20)], 90, slot
                        )
                    else:
                        self.touch.down(slot, event.x, event.y)
                else:
                    slot = slot_of.pop(key, 0)
                    active.pop(slot, None)
                    self.touch.up(slot)
                stats.fired += 1
            except Exception as exc:  # noqa: BLE001
                stats.errors += 1
                self.log(f"触控失败 @{t_ms}ms: {exc}")
        self.log(stats.report())
        return stats


class VisionPlayer:
    """实时视觉驱动：截图 → 识别 → 在判定线上方触发点击。"""

    def __init__(
        self,
        device: AdbDevice,
        touch: TouchBackend,
        geometry: LaneGeometry,
        vision_cfg: VisionConfig | None = None,
        trigger_offset_px: int = 60,
        cooldown_ms: int = 90,
        debug_dir: str | None = None,
        logger=print,
    ):
        self.device = device
        self.touch = touch
        self.geo = geometry
        self.detector = NoteDetector(geometry, vision_cfg)
        self.trigger_offset_px = trigger_offset_px
        self.cooldown_ms = cooldown_ms
        self.debug_dir = debug_dir
        self.log = logger
        self._last_hit: dict[int, float] = {}

    def run(self, seconds: float = 30.0, dry_run: bool = False, save_debug_every: int = 0):
        clock = Clock()
        frames = 0
        hits = 0
        held: dict[int, float] = {}
        t_end = time.perf_counter() + seconds
        while time.perf_counter() < t_end:
            frame = self.device.screenshot_array()
            detections = self.detector.detect(frame)
            now = clock.now_ms()
            for d in detections:
                trigger_y = self.geo.line_y - self.trigger_offset_px
                if d.y < trigger_y:
                    continue
                last = self._last_hit.get(d.lane, -1e9)
                if now - last < self.cooldown_ms:
                    continue
                self._last_hit[d.lane] = now
                hits += 1
                if not dry_run:
                    if d.kind == "hold":
                        self.touch.down(1 + d.lane, d.x, self.geo.line_y)
                        held[d.lane] = now
                    else:
                        self.touch.tap(d.x, self.geo.line_y, 40, slot=1 + d.lane)
            # 松开已经离开视野的长条
            for lane in list(held):
                if not any(d.lane == lane and d.kind == "hold" for d in detections):
                    if not dry_run:
                        self.touch.up(1 + lane)
                    held.pop(lane, None)
            frames += 1
            if save_debug_every and frames % save_debug_every == 0 and self.debug_dir:
                vis = self.detector.draw(frame, detections)
                cv2.imwrite(f"{self.debug_dir}/vision_{frames:05d}.png", vis)
        return {"frames": frames, "hits": hits, "fps": frames / max(seconds, 1e-6)}


class HybridPlayer:
    """谱面主导 + 视觉校正（先实现时间轴执行 + 在线统计）。"""

    def __init__(self, chart_player: ChartPlayer, geometry: LaneGeometry, logger=print):
        self.chart_player = chart_player
        self.geo = geometry
        self.log = logger

    def play(self, chart: Chart, lead_in_ms: int = 3000, dry_run: bool = False):
        return self.chart_player.play(chart, lead_in_ms=lead_in_ms, dry_run=dry_run)
