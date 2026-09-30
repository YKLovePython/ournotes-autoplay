"""实时视觉自动演奏（无谱面模式）。

流程::

    screenrecord(H264 58fps) -> 解码 -> 音符检测 -> 轨迹跟踪
        -> 预测命中时刻/位置 -> sendevent 触控

所有检测在视频流坐标系（默认 1280x592）里做，输出时再换算回设备像素。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import cv2
import numpy as np

from .adb import AdbDevice
from .capture import ScreenStream
from .touch import TouchBackend
from .tracker import LaneGrid, NoteDetector, NoteDetectorConfig, NoteTracker


@dataclass
class AutoplayStats:
    frames: int = 0
    notes_seen: int = 0
    taps: int = 0
    holds: int = 0
    flicks: int = 0
    skipped_late: int = 0
    errors: int = 0
    started: float = field(default_factory=time.perf_counter)

    def report(self) -> str:
        dt = time.perf_counter() - self.started
        return (
            f"运行 {dt:.1f}s, 处理 {self.frames} 帧 ({self.frames/max(dt,1e-6):.1f} fps)\n"
            f"发现音符 {self.notes_seen}, 触发 点击={self.taps} 长按={self.holds} 轻扫={self.flicks}\n"
            f"丢弃(已过线) {self.skipped_late}, 触控异常 {self.errors}"
        )


class VisionAutoplay:
    def __init__(
        self,
        device: AdbDevice,
        touch: TouchBackend,
        screen_w: int,
        screen_h: int,
        judge_y: int,
        stream_size: str = "1280x592",
        lead_ms: float = 45.0,
        hold_tail_ms: int = 20,
        assist_mode: bool = True,
        tap_ms: int = 45,
        lane_center_x: float = 1253.0,
        lane_pitch: float = 295.0,
        lane_count: int = 7,
        apex_y: float = -72.0,
        snap: bool = True,
        debug_dir: str | None = None,
        logger=print,
    ):
        self.device = device
        self.touch = touch
        self.screen_w, self.screen_h = screen_w, screen_h
        self.judge_y = judge_y
        self.stream_size = stream_size
        self.lead_ms = lead_ms
        self.hold_tail_ms = hold_tail_ms
        self.assist_mode = assist_mode
        self.tap_ms = tap_ms
        self.lane_center_x = lane_center_x
        self.lane_pitch = lane_pitch
        self.lane_count = lane_count
        self.apex_y = apex_y
        self.snap = snap
        self.debug_dir = debug_dir
        self.log = logger
        self._slot_of: dict[int, int] = {}
        self._free_slots = list(range(1, 9))

    # ------------------------------------------------------------------
    def _slot(self, track_id: int) -> int:
        if track_id in self._slot_of:
            return self._slot_of[track_id]
        slot = self._free_slots.pop(0) if self._free_slots else 0
        self._slot_of[track_id] = slot
        return slot

    def _release(self, track_id: int) -> None:
        slot = self._slot_of.pop(track_id, None)
        if slot is None:
            return
        try:
            self.touch.up(slot)
        except Exception:  # noqa: BLE001
            pass
        if slot and slot not in self._free_slots:
            self._free_slots.append(slot)
            self._free_slots.sort()

    # ------------------------------------------------------------------
    def run(self, seconds: float = 60.0, dry_run: bool = False, save_debug_every: int = 0) -> AutoplayStats:
        cfg = NoteDetectorConfig()
        detector = NoteDetector(cfg)
        stats = AutoplayStats()
        stream = ScreenStream(self.device, size=self.stream_size, time_limit=int(seconds) + 30)
        stream.start()
        try:
            first = stream.latest(timeout=6.0)
            if first is None:
                raise RuntimeError(f"无法获取屏幕流: {stream.error}")
            sh, sw = first.image.shape[:2]
            scale_x = sh / self.screen_w if False else sw / self.screen_w
            scale_y = sh / self.screen_h
            judge_y_stream = self.judge_y * scale_y
            tracker = NoteTracker(judge_y_stream)
            grid = LaneGrid(
                center_x=self.lane_center_x * scale_x,
                pitch=self.lane_pitch * scale_x,
                count=self.lane_count,
                judge_y=judge_y_stream,
                apex_y=self.apex_y * scale_y,
            )
            self.log(
                f"流 {sw}x{sh}（设备 {self.screen_w}x{self.screen_h}），"
                f"判定线 y={self.judge_y} -> 流内 {judge_y_stream:.1f}"
            )
            t_end = time.perf_counter() + seconds
            last_index = -1
            hold_check: list[tuple[int, float]] = []
            while time.perf_counter() < t_end:
                frame = stream.latest(timeout=1.0, newer_than=last_index)
                if frame is None:
                    continue
                last_index = frame.index
                stats.frames += 1
                blobs = detector.detect(frame.image)
                stats.notes_seen += len(blobs)
                tracker.update(frame.ts, blobs)
                pending = tracker.pending_hits(frame.ts, horizon_s=0.6)
                for tr, x_judge, t_hit, vy in pending:
                    if tr.state != "active":
                        continue
                    if len(tr) < 4 and tr.obs[-1].y < judge_y_stream * 0.5:
                        continue
                    fire_at = t_hit - self.lead_ms / 1000.0
                    if frame.ts < fire_at - 0.05:
                        continue
                    if frame.ts < fire_at:
                        time.sleep(max(0.0, fire_at - frame.ts))
                    w_stream = tr.last.w
                    if self.snap:
                        x_judge = grid.snap(x_judge, width_px=w_stream * (1 / scale_x) * 0.0 + w_stream, unit=393.0 * scale_x, y=judge_y_stream)
                    x_screen = int(x_judge / scale_x) if scale_x else int(x_judge)
                    x_screen = max(0, min(self.screen_w - 1, x_screen))
                    kind = tr.last.kind
                    slot = self._slot(tr.id)
                    if not dry_run:
                        try:
                            if kind == "hold":
                                self.touch.down(slot, x_screen, self.judge_y)
                                hold_check.append((tr.id, time.perf_counter() + 0.35))
                                stats.holds += 1
                            elif kind == "flick" and not self.assist_mode:
                                dx = 120 if tr.last.x < 0 else 120
                                self.touch.swipe(
                                    [(x_screen, self.judge_y + 30), (x_screen + dx, self.judge_y - 10)],
                                    80,
                                    slot,
                                )
                                self._release(tr.id)
                                stats.flicks += 1
                            else:
                                self.touch.tap(x_screen, self.judge_y, self.tap_ms, slot)
                                self._release(tr.id)
                                stats.taps += 1
                        except Exception as exc:  # noqa: BLE001
                            stats.errors += 1
                            self.log(f"触控失败: {exc}")
                    else:
                        stats.taps += 1
                    tr.state = "fired"
                    tr.fired_at = frame.ts
                    tr.fired_x = x_judge
                    tracker.suppress_neighbours(tr, x_judge, window=260.0 * scale_x)
                # 长按释放
                now = time.perf_counter()
                for tid, until in list(hold_check):
                    if now >= until:
                        self._release(tid)
                        hold_check.remove((tid, until))
                if save_debug_every and stats.frames % save_debug_every == 0 and self.debug_dir:
                    self._dump_debug(frame.image, blobs, tracker, judge_y_stream, stats.frames)
        finally:
            for tid in list(self._slot_of):
                self._release(tid)
            stream.stop()
        self.log(stats.report())
        return stats

    # ------------------------------------------------------------------
    def _dump_debug(self, image, blobs, tracker, judge_y_stream, index: int) -> None:
        vis = image.copy()
        cv2.line(vis, (0, int(judge_y_stream)), (vis.shape[1], int(judge_y_stream)), (0, 255, 0), 1)
        for b in blobs:
            cv2.rectangle(vis, (int(b.x - b.w / 2), int(b.y - b.h / 2)), (int(b.x + b.w / 2), int(b.y + b.h / 2)), (0, 255, 255), 1)
        for tr in tracker.tracks:
            if not tr.obs:
                continue
            pts = np.array([[int(o.x), int(o.y)] for o in tr.obs], np.int32)
            cv2.polylines(vis, [pts], False, (255, 0, 255), 1)
            pr = tracker.predict(tr)
            if pr:
                xj, th, vy = pr
                cv2.circle(vis, (int(xj), int(judge_y_stream)), 10, (0, 128, 255), 2)
        cv2.imwrite(f"{self.debug_dir}/auto_{index:05d}.png", vis)
