"""谱面驱动的演奏器。

用法（简单模式，推荐）：

1. 在手机游戏里选好曲目与难度，停在「乐队确认」页；
2. 脚本里也用同样的曲目 + 难度（它会去加载对应的谱面 JSON）；
3. 运行脚本 —— 它在固定的坐标点一下「LIVE START」，
   等固定秒数之后，严格按谱面的 `timeMs` 逐个落指。

时间对不上时只改两个旋钮：

* ``--delay``  点完开始键后，等多少秒才开始按谱面落指（粗调）；
* ``--offset`` 整体提前/延后多少毫秒（细调，正数=提前）。

默认不取流、不做任何画面校验，所以 CPU 很空，落指时刻更准。
如果哪天起播延迟不稳定，可以加 ``--sync`` 让脚本取流并与谱面对时。
"""

from __future__ import annotations

import collections
import statistics
import time
from dataclasses import dataclass, field

from .capture import ScreenStream
from .chartdb import ChartEvent, FieldGeometry
from .touch import TouchBackend


@dataclass
class PlayStats:
    taps: int = 0
    holds: int = 0
    flicks: int = 0
    late: int = 0
    errors: int = 0
    events: int = 0
    t0: float = 0.0
    first_fire: float = 0.0
    last_fire: float = 0.0
    sync_votes: int = 0
    sync_spread_ms: float = 0.0
    started: float = field(default_factory=time.perf_counter)

    def report(self) -> str:
        return (
            f"谱面事件 {self.events}；落指 tap={self.taps} hold={self.holds} "
            f"flick={self.flicks}；迟到 {self.late}，异常 {self.errors}"
        )


def sleep_until(t: float, spin: float = 0.002) -> float:
    """睡到指定时刻。返回实际迟到的秒数（正数表示晚于计划）。"""
    while True:
        d = t - time.perf_counter()
        if d <= 0:
            return -d
        if d > spin + 0.001:
            time.sleep(d - spin)
        else:
            time.sleep(0)


class ChartPerformer:
    def __init__(
        self,
        device,
        touch: TouchBackend,
        events: list[ChartEvent],
        geom: FieldGeometry,
        *,
        screen_w: int,
        screen_h: int,
        stream_size: str = "1280x592",
        offset_ms: float = 0.0,
        live_appear_offset_ms: float = 180.0,
        tap_ms: int = 45,
        hold_pad_ms: int = 25,
        max_slots: int = 8,
        dry_run: bool = False,
        logger=print,
    ):
        self.device = device
        self.touch = touch
        self.events = events
        self.geom = geom
        self.screen_w, self.screen_h = screen_w, screen_h
        self.stream_size = stream_size
        self.offset_ms = offset_ms
        self.live_appear_offset_ms = live_appear_offset_ms
        self.tap_ms = tap_ms
        self.hold_pad_ms = hold_pad_ms
        self.max_slots = max_slots
        self.dry_run = dry_run
        self.log = logger
        self.stats = PlayStats(events=len(events))
        self._audio = None
        self._audio_since = 0.0
        self._audio_done = False
        # 取流链路本身的延迟：屏幕上的变化 → 我们在帧里看到它。
        # 用 show_touches 白点实测（tools/measure_latency.py）：25~40 ms。
        # 注意别拿「点开始键后画面多久变」去量——那里面主要是游戏自己的响应时间。
        self.pipeline_latency_ms = 30.0
        self._pipeline_latency_ms = 0.0
        # 画面闭环修正默认关闭（实测误检会把落指推偏几百毫秒），只保留观测日志
        self.note_watch = False

    # ------------------------------------------------------------------ 演奏
    def run(
        self,
        *,
        timeout: float = 150.0,
        start_button: tuple[int, int] | None = None,
        start_delay: float = 10.0,
        sync: str | bool = "manual",
        sync_timeout: float = 25.0,
        min_votes: int = 3,
    ) -> PlayStats:
        """三种定基准的方式：

        * ``"manual"``（默认）：点开始键 → 你在手机上盯着，第一个音符快压线时按一下回车。
          这是社区里 Phigros 自动演奏项目采用的标准做法：谱面里没有"前摇时长"，
          加载时间又是变的，人眼直接看真机屏幕（没有链路延迟）反而最准。
        * ``"auto"``：取流检测演奏画面出现的瞬间，自动推算（受画面延迟影响，需探针校正）。
        * ``False`` / ``"fixed"``：点完开始键等固定秒数（``start_delay``）。
        """
        if sync == "manual":
            return self._run_manual(timeout, start_button)

        if sync == "auto":
            # 自动检测演奏画面；万一没检测到，退回手动打拍，绝不空跑
            return self._run_auto_then_manual(timeout, start_button, sync_timeout)

        if sync is False or sync == "fixed":
            t0 = time.perf_counter()
            self._start_audio_clock()
            if start_button is not None:
                self.log(f"点击开始键 {start_button}"
                         + ("（dry-run，不真的点）" if self.dry_run else ""))
                self._press_start(start_button[0], start_button[1])
                t0 = time.perf_counter() + start_delay
            self.log(
                f"谱面时间 0 定在 {start_delay:.2f}s 后开始；"
                f"落指整体偏移 {self.offset_ms:+.0f} ms（正=提前）"
            )
            self.stats.t0 = t0
            try:
                return self._execute(t0, timeout)
            finally:
                self._stop_audio_clock()

        stream = ScreenStream(self.device, size=self.stream_size, time_limit=int(timeout) + 60)
        stream.start()
        start_tap_ts = None
        self._start_audio_clock()
        try:
            first = stream.latest(timeout=8.0)
            if first is None:
                raise RuntimeError(f"取流失败: {stream.error}")
            sh, sw = first.image.shape[:2]
            if (sw, sh) != (self.geom.sw, self.geom.sh):
                self.log("流尺寸与标定不一致，按实际流重算几何")
                self.geom = FieldGeometry((sw, sh), self.screen_w, self.screen_h)
            if start_button is not None:
                self.log(f"点击开始键 {start_button}")
                start_tap_ts = time.perf_counter()
                ref_frame = stream.latest(timeout=1.0)
                self._press_start(start_button[0], start_button[1])
                if ref_frame is not None:
                    lat = self._measure_pipeline_latency(stream, ref_frame)
                    if lat is not None:
                        self._pipeline_latency_ms = lat
            t0 = self._sync(stream, sync_timeout, min_votes)
            if t0 is None:
                self.log("同步失败：没能在超时内对齐谱面")
                return self.stats
            self.stats.t0 = t0
            if start_tap_ts is not None:
                self.log(f"标定结果：点完开始键到谱面时间 0 相隔 {t0 - start_tap_ts:.2f} 秒")
            return self._play_with_watcher(stream, t0, timeout)
        finally:
            stream.stop()
            self._stop_audio_clock()

    # ---------------------------------------------------------- 音频时钟
    def _run_manual(self, timeout: float, start_button: tuple[int, int] | None) -> PlayStats:
        """手动同步：由你在真机上打一下节拍，作为谱面时间基准。

        社区（Phigros 自动演奏）的标准做法：谱面不含前摇时长、加载时间还不固定，
        而人眼看的是真实屏幕（没有采集链路延迟），所以手动打拍最准。
        """
        if start_button is not None:
            self.log(f"点击开始键 {start_button}")
            self._press_start(start_button[0], start_button[1])
        first = self.events[0]
        lead = first.time_ms / 1000.0
        print()
        print("=" * 62)
        print("  【手动同步】现在盯着手机屏幕：")
        print(f"  第 1 个音符快要压到判定线的瞬间，按一下回车键。")
        print("  （提前一点点按最好；按早/按晚都不要紧，")
        print("    之后用菜单 [4] 落指偏移 微调，正数=提前，负数=延后）")
        print("=" * 62)
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            self.log("没有收到回车，放弃")
            return self.stats
        t_enter = time.perf_counter()
        t0 = t_enter - lead + self.offset_ms / 1000.0
        self.stats.t0 = t0
        self.log(f"手动同步完成：基准 t0 = 按键时刻 - {lead:.3f}s "
                 f"（偏移 {self.offset_ms:+.0f} ms）")
        return self._execute(t0, timeout, already_adjusted=True)

    def _run_auto_then_manual(self, timeout: float, start_button: tuple[int, int] | None,
                              sync_timeout: float) -> PlayStats:
        """先自动检测演奏画面；没测到就转手动打拍。"""
        stream = ScreenStream(self.device, size=self.stream_size, time_limit=int(timeout) + 60)
        stream.start()
        try:
            first = stream.latest(timeout=8.0)
            if first is None:
                raise RuntimeError(f"取流失败: {stream.error}")
            sh, sw = first.image.shape[:2]
            if (sw, sh) != (self.geom.sw, self.geom.sh):
                self.geom = FieldGeometry((sw, sh), self.screen_w, self.screen_h)
            if start_button is not None:
                self.log(f"点击开始键 {start_button}")
                ref_frame = stream.latest(timeout=1.0)
                self._press_start(start_button[0], start_button[1])
                if ref_frame is not None:
                    lat = self._measure_pipeline_latency(stream, ref_frame)
                    if lat is not None:
                        self._pipeline_latency_ms = lat
            t0 = self._sync(stream, sync_timeout, 3)
        except Exception:
            stream.stop()
            raise
        if t0 is None:
            stream.stop()
            self.log("自动检测没成功，转为手动打拍同步")
            return self._run_manual(timeout, None)
        self.stats.t0 = t0
        try:
            return self._play_with_watcher(stream, t0, timeout)
        finally:
            stream.stop()

    def _play_with_watcher(self, stream, t0: float, timeout: float) -> PlayStats:
        """按谱面落指，同时盯着画面里的音符做闭环校正。"""
        from .notesight import NoteWatcher

        watcher = NoteWatcher(stream, self.geom, self.events, t0, self.log,
                              latency_ms=self.pipeline_latency_ms,
                              apply=self.note_watch)
        watcher.start()
        self.log("画面闭环对时已开启（会持续修正落指）" if self.note_watch
                 else "对时观测已开启（只记录偏差，不自动修正）")
        try:
            stats = self._execute(t0, timeout, watcher=watcher)
        finally:
            watcher.stop()
            self.log(f"  对时观测共 {watcher.samples} 个样本 / {watcher.frames} 帧")
        return stats

    def _start_audio_clock(self) -> None:
        # 实测：日志里那条音轨是主界面 BGM（会循环重置），不是 Live 的歌曲，
        # 用它校正会把整场推迟几十秒。所以默认关闭，只保留代码备用。
        if not getattr(self, "use_audio_clock", False):
            return
        try:
            from .audioclock import AudioClock

            self._audio = AudioClock(self.device, log=self.log)
            self._audio_since = time.perf_counter()
            self._audio.start()
            self.log("已启用音频时钟（读游戏自己的播放位置做校正）")
        except Exception as exc:  # noqa: BLE001
            self._audio = None
            self.log(f"音频时钟不可用（{exc}），仅用画面基准")

    def _stop_audio_clock(self) -> None:
        if self._audio is not None:
            try:
                self._audio.close()
            except Exception:  # noqa: BLE001
                pass
            self._audio = None

    def _press_start(self, x: int, y: int) -> None:
        """点「LIVE START」。

        这里**不用 scrcpy 触控通道**，改用 adb 的 ``input tap``：
        点开始键对延迟毫无要求（后面还要等固定秒数），
        而实测游戏的开始键用 ``input tap`` 能稳点进去、scrcpy 通道偶尔不被接受。
        """
        if self.dry_run:
            return
        try:
            self.device.input_tap(x, y)
            self.log("  已用 input tap 发送")
            return
        except Exception as exc:  # noqa: BLE001
            self.log(f"  input tap 失败（{exc}），退回触控通道")
        self.touch.tap(x, y, 70, slot=0)

    # ---------------------------------------------------------- 画面延迟
    def _measure_pipeline_latency(self, stream: ScreenStream, ref) -> float | None:
        """量「点开始键 → 画面真的变了」用了多久（毫秒）——游戏自己的响应时间。

        **这个数不能拿来扣时间基准**：它里面主要是游戏切场景的开销，不是取流延迟
        （取流延迟用 tools/measure_latency.py 的 show_touches 白点量，只有 25~40 ms）。
        这里只当参考打出来，方便判断手机当时卡不卡。
        """
        import cv2

        t_issue = time.perf_counter()
        deadline = t_issue + 3.0
        last = ref.index
        ref_img = ref.image
        while time.perf_counter() < deadline:
            fr = stream.latest(timeout=0.3, newer_than=last)
            if fr is None or fr.index == last:
                continue
            last = fr.index
            d = cv2.absdiff(fr.image, ref_img).max(axis=2)
            if float((d >= 40).sum()) > 0.05 * d.size:
                raw = (fr.ts - t_issue) * 1000.0
                self.log(f"  参考：点开始键后 {raw:.0f} ms 画面才切换（游戏自己的响应，"
                         f"不参与对时）")
                return max(0.0, raw)
        self.log("  参考：3 秒内没看到画面切换")
        return None

    def _execute(self, t0: float, timeout: float, already_adjusted: bool = False,
                 watcher=None) -> PlayStats:
        offset = 0.0 if already_adjusted else self.offset_ms
        pre = (offset + getattr(self.touch, "pre_issue_ms", 0.0)) / 1000.0
        judge_y = self.geom.device_judge_y

        # 把音符摊成「谱面时刻 + 位置 + 按住时长」，同一时刻的合并成一个手势
        items: list[tuple[float, int, int, ChartEvent]] = []
        for e in self.events:
            dur = self.tap_ms
            if e.is_hold:
                dur = max(self.tap_ms, (e.end_ms or e.time_ms) - e.time_ms - self.hold_pad_ms)
            items.append((e.time_ms / 1000.0, self.geom.device_x(e.center), dur, e))
        items.sort(key=lambda a: a[0])

        groups: list[tuple[float, list[tuple[int, int]], list[ChartEvent]]] = []
        for at, x, dur, e in items:
            if groups and at - groups[-1][0] <= 0.030:
                groups[-1][1].append((x, dur))
                groups[-1][2].append(e)
            else:
                groups.append((at, [(x, dur)], [e]))

        shift = 0.0
        end_limit = time.perf_counter() + timeout
        self.log(
            f"开始演奏：{len(self.events)} 个音符 → {len(groups)} 个手势；"
            f"最后一个音符在谱面 {self.events[-1].time_ms/1000:.1f}s"
            f"（提前 {pre*1000:.0f} ms 发出）。"
        )
        for at, presses, evs in groups:
            # 音频时钟一到就校正时间基准（游戏自己打的播放位置最可信）
            if self._audio is not None and not self._audio_done:
                est = self._audio.estimate_t0(self._audio_since, max_ms=9000)
                if est is not None:
                    t0_audio, ms = est
                    corr = t0_audio - t0
                    self._audio_done = True
                    self.log(f"音频时钟校正：已播 {ms} ms，t0 修正 {corr*1000:+.0f} ms")
                    if abs(corr) > 0.02:
                        shift = corr
            # 画面闭环：用刚看到的音符压线时刻，修正后面所有落指
            if watcher is not None:
                shift = watcher.shift
            target = t0 + shift + at - pre
            if time.perf_counter() > end_limit:
                self.log("超出时长上限，提前结束")
                break
            late = sleep_until(target)
            if late > 0.05:
                self.stats.late += 1
            try:
                if not self.dry_run:
                    self.touch.perform(presses, judge_y)
                for e in evs:
                    if e.is_hold:
                        self.stats.holds += 1
                    elif e.kind == "flick":
                        self.stats.flicks += 1
                    else:
                        self.stats.taps += 1
                if not self.stats.first_fire:
                    self.stats.first_fire = time.perf_counter()
                self.stats.last_fire = time.perf_counter()
            except Exception as exc:  # noqa: BLE001
                self.stats.errors += 1
                self.log(f"触控异常: {exc}")
        self.log(self.stats.report())
        return self.stats

    # ------------------------------------------------------ 可选：画面同步
    @staticmethod
    def _fit_fall_curve(samples: list[tuple[float, float]]) -> tuple[float, float]:
        """拟合「到判定线距离 d → 剩余时间 dt」为 dt = A*d + B*d²（过原点）。"""
        if len(samples) < 6:
            return 1.0 / 780.0, 0.0
        ds = [d for d, _ in samples]
        ts = [t for _, t in samples]
        s11 = sum(d * d for d in ds)
        s12 = sum(d ** 3 for d in ds)
        s22 = sum(d ** 4 for d in ds)
        y1 = sum(d * t for d, t in zip(ds, ts))
        y2 = sum(d * d * t for d, t in zip(ds, ts))
        det = s11 * s22 - s12 * s12
        if abs(det) < 1e-12:
            return 1.0 / 780.0, 0.0
        return (y1 * s22 - y2 * s12) / det, (s11 * y2 - s12 * y1) / det

    def resolve_offset(self, points: list[tuple[float, float, float]], min_votes: int,
                       iterations: int = 4) -> float | None:
        judge = self.geom.judge_y
        a_coef, b_coef = 1.0 / 780.0, 0.0

        def to_time(y: float) -> float:
            d = judge - y
            return a_coef * d + b_coef * d * d

        votes: collections.Counter[int] = collections.Counter()
        for t, x, y in points:
            if not (200.0 <= y <= 430.0):
                continue
            tcross = t + to_time(y)
            lane = self.geom.stream_x_to_lane(x, y)
            for e in self.events:
                if abs(e.center - lane) > 1.6:
                    continue
                off = tcross - e.time_ms / 1000.0
                if -1.0 < off < 60.0:
                    votes[round(off / 0.05)] += 1
        if not votes:
            return None
        bin_, count = votes.most_common(1)[0]
        if count < min_votes:
            return None
        off = bin_ * 0.05

        samples: list[float] = []
        spread = 0.0
        for _ in range(iterations):
            pairs: list[tuple[float, float]] = []
            for t, x, y in points:
                if not (200.0 <= y <= 430.0):
                    continue
                lane = self.geom.stream_x_to_lane(x, y)
                target = t + to_time(y)
                best = None
                for e in self.events:
                    if abs(e.center - lane) > 1.6:
                        continue
                    err = target - (off + e.time_ms / 1000.0)
                    if abs(err) <= 0.20 and (best is None or abs(err) < abs(best[0])):
                        best = (err, e)
                if best is not None:
                    pairs.append((judge - y, (off + best[1].time_ms / 1000.0) - t))
            if len(pairs) < 6:
                break
            a_coef, b_coef = self._fit_fall_curve(pairs)
            samples = []
            for t, x, y in points:
                if not (200.0 <= y <= 430.0):
                    continue
                lane = self.geom.stream_x_to_lane(x, y)
                tcross = t + to_time(y)
                for e in self.events:
                    if abs(e.center - lane) > 1.6:
                        continue
                    cand = tcross - e.time_ms / 1000.0
                    if abs(cand - off) <= 0.20:
                        samples.append(cand)
            if not samples:
                break
            off = statistics.median(samples)
            spread = statistics.pstdev(samples) * 1000 if len(samples) > 1 else 0.0
        self.stats.sync_votes = len(samples) if samples else count
        self.stats.sync_spread_ms = spread
        return off

    def _sync(self, stream: ScreenStream, timeout: float, min_votes: int) -> float | None:
        """定时间基准。首选「加载页结束」，不行再退回「判定线出现」。

        **首选：加载页结束 + 开场动画 5.9167s。** 点完开始键后画面上会出现
        ``NOW LOADING xx.x%`` 的加载页（整帧亮度 ~204），它结束的瞬间就是
        开场动画的起点；开场是游戏原生 Timeline，固定 355 帧 @60fps，
        音乐在它播完时开始。实测两条路互相印证（差 30ms），
        而加载页结束这个信号又大又干脆，还早 5.7 秒拿到。

        回退：等「判定线出现」。
        实测（拿录像里音符真正压线的时刻反推）：

        * 判定线在视频 2.43s 出现；
        * 谱面第 1 个音符（lane 6，2.891s）在视频 5.50s 压线；
        * 谱面第 2 个音符（lane 18，5.783s）在视频 8.36s 压线；

        两点都吻合，故 **谱面时间 0 = 判定线出现 + 0.18s**。
        这样在第一个音符前约 2.7 秒就锁定了，开头不会漏。
        （注意别拿录像里的音频去推：那段视频的音轨与画面不同步。）
        """
        from .loading import INTRO_SECONDS, wait_chart_zero
        from .livestate import is_live_frame

        self.log("等待加载页结束…")
        t0 = wait_chart_zero(stream, timeout=min(timeout, 12.0), log=self.log,
                             latency_s=self.pipeline_latency_ms / 1000.0)
        if t0 is not None:
            self.log(f"  基准：加载结束 + 开场 {INTRO_SECONDS:.3f}s，"
                     f"推测谱面 0 点（已扣取流延迟 {self.pipeline_latency_ms:.0f} ms）")
            return t0
        self.log("  没看到加载页（可能资源已缓存），改用判定线出现来定基准")

        deadline = time.perf_counter() + timeout
        last = -1
        frames = 0
        self.log("等待演奏画面出现…")
        while time.perf_counter() < deadline:
            fr = stream.latest(timeout=0.5, newer_than=last)
            if fr is None or fr.index == last:
                continue
            last = fr.index
            frames += 1
            if is_live_frame(fr.image):
                t0 = fr.ts + (self.live_appear_offset_ms - self.pipeline_latency_ms) / 1000.0
                self.log(f"  演奏画面已出现（第 {frames} 帧），"
                         f"据此推算音乐起点 "
                         f"{(self.live_appear_offset_ms - self.pipeline_latency_ms):.0f} ms 后"
                         f"（已扣取流延迟 {self.pipeline_latency_ms:.0f} ms）")
                return t0
        self.log(f"  超时：{frames} 帧内没等到演奏画面")
        return None
