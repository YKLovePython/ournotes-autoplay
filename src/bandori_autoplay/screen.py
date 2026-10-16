"""取一帧无损整屏图（``exec-out screencap -p``），带硬超时看门狗。

为什么不用实时视频流：最小版只在"认 LIVE START"这一步需要画面，
静态一帧就够；而且 PNG 是无损的、原分辨率的，没有视频压缩带来的误差。

为什么要看门狗：有的机器/有的界面下 ``screencap`` 会**吊住不返回**
（实测遇到过：进程一直在，60 秒的 subprocess 超时也没能把它收掉）。
这里自己管进程：超时就杀掉、收尸，再返回 None。
"""

from __future__ import annotations

import subprocess
import time

import numpy as np


def wake(device) -> None:
    """把屏幕点亮（只是发一个唤醒键，不改任何设置）。"""
    try:
        device.shell("input keyevent KEYCODE_WAKEUP")
    except Exception:  # noqa: BLE001
        pass


def grab(device, timeout: float = 15.0, log=None):
    """抓一帧，返回 BGR 图（numpy）或 None。超时/失败返回 None。"""
    say = log or (lambda *a: None)
    cmd = device._cmd(["exec-out", "screencap", "-p"])
    t0 = time.perf_counter()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.communicate(timeout=3)          # 收尸；仍吊着就算了，别拖住主流程
        except subprocess.TimeoutExpired:
            pass
        say(f"    截图超时（{timeout:.0f}s），已放弃这一帧"
            f"（耗时 {time.perf_counter() - t0:.1f}s）")
        return None
    if not out or not out.startswith(b"\x89PNG"):
        say("    截图返回的不是 PNG（屏幕被保护内容挡住？）")
        return None
    import cv2

    img = cv2.imdecode(np.frombuffer(out, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        say("    截图解码失败")
    return img
