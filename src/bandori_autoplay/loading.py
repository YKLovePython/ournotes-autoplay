"""抓「加载完成」那一刻 —— 目前最准的时间锚点。

实机录到的顺序（点完 LIVE START，1280x592 流，亮度=整帧均值）::

    0.00s  确认页        亮度 114
    0.83s  黑屏          亮度 0.2
    1.25s  加载页        亮度 203~204   ← 页面上直接写着 NOW LOADING xx.x%
    3.81s  （加载页最后一帧）
    4.22s  黑屏          亮度 0.7
    5.45s  开场标题卡    亮度 71        ← 开场动画开始
    9.69s  判定线出现    亮度 ~40

**加载页结束 = 开场动画起点（F0）**。开场动画是游戏原生 Timeline，
固定 355 帧 @60fps = 5.9167 秒（见 ournotes-player/src/live/intro.js），
音乐在它播完那一刻开始，所以::

    谱面时间 0 = 加载页结束 + 5.9167s

核对：3.9 + 5.917 = 9.82s；判定线 9.69s + 实测 0.16s = 9.85s —— 差 30ms，互相印证。

比「等判定线出现」好在两点：信号又大又干脆（亮度 204 → 0.7），
而且早 5.7 秒拿到，第一个音符（谱面 2.891s）之前有充足余量。
"""

from __future__ import annotations

import time

INTRO_SECONDS = 355.0 / 60.0      # 开场动画固定长度（351~355 帧 @60fps）
BRIGHT_ON = 170.0                 # 加载页亮度阈值（实测 203~204）
DARK_OFF = 70.0                   # 「结束」的亮度阈值（紧随其后是黑屏 0.7）


def wait_loading_end(stream, timeout: float = 35.0, log=None,
                     bright_on: float = BRIGHT_ON, dark_off: float = DARK_OFF,
                     min_bright: int = 2, min_dark: int = 2):
    """等「加载页 → 黑屏」。返回 ``(t_end, t_first_bright, frames)``；没等到返回 ``(None, ...)``。

    ``t_end`` 取「加载页最后一帧」与「第一帧黑屏」的中点，抵消采样粒度。
    """
    say = log or (lambda *a: None)
    deadline = time.perf_counter() + timeout
    last = -1
    bright_frames = 0
    t_first_bright = None
    t_last_bright = None
    dark_frames = 0
    t_first_dark = None
    frames = 0
    while time.perf_counter() < deadline:
        fr = stream.latest(timeout=0.5, newer_than=last)
        if fr is None or fr.index == last:
            continue
        last = fr.index
        frames += 1
        b = float(fr.image.mean())
        if b >= bright_on:
            if bright_frames == 0:
                t_first_bright = fr.ts
            bright_frames += 1
            t_last_bright = fr.ts
            dark_frames = 0
            t_first_dark = None
            continue
        if bright_frames >= min_bright and b <= dark_off:
            if dark_frames == 0:
                t_first_dark = fr.ts
            dark_frames += 1
            if dark_frames >= min_dark:
                t_end = (t_last_bright + t_first_dark) / 2.0
                say(f"  加载页结束（亮度 {bright_on:.0f} → {b:.0f}，"
                    f"持续 {bright_frames} 帧）")
                return t_end, t_first_bright, frames
        else:
            dark_frames = 0
            t_first_dark = None
    return None, t_first_bright, frames


def wait_chart_zero(stream, timeout: float = 35.0, log=None, latency_s: float = 0.030):
    """等加载结束并换算成「谱面时间 0」的真机时刻。

    扣掉取流延迟（实测 25~40ms，用 show_touches 白点量的）。
    """
    t_end, _t_first, frames = wait_loading_end(stream, timeout=timeout, log=log)
    if t_end is None:
        return None
    return t_end + INTRO_SECONDS - latency_s
