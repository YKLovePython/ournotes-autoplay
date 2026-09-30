"""认「乐队确认页」上的 LIVE START 按钮。

用来判断现在到底能不能开演（连刷时每轮都要等结算流程走完），
以及按钮当前在哪（模板匹配得到的坐标比写死的坐标稳）。

模板是 ``tools/live_start_template.png``（从实机截图裁的按钮局部），
同时支持整屏截图（2510x1156）和取流帧（1280x592）——按宽度自动缩放。
"""

from __future__ import annotations

import time
from pathlib import Path

import cv2
import numpy as np

_TPL_PATH = Path(__file__).resolve().parents[2] / "tools" / "live_start_template.png"
_tpl: np.ndarray | None = None


def imread_unicode(path) -> np.ndarray | None:
    """读图片，**路径里有中文也能读**。

    ``cv2.imread`` 在 Windows 上走的是本地代码页 API，路径里只要有一个非 ASCII
    字符就直接返回 None（不报错，只是"读不到"）。用 ``imdecode`` + ``np.fromfile``
    绕开这一层。
    """
    try:
        buf = np.fromfile(str(path), dtype=np.uint8)
    except OSError:
        return None
    if buf.size == 0:
        return None
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


def template() -> np.ndarray | None:
    global _tpl
    if _tpl is None and _TPL_PATH.exists():
        _tpl = imread_unicode(_TPL_PATH)
    return _tpl


def find_live_start(image: np.ndarray, min_score: float = 0.62):
    """返回 ``(x, y, 匹配度)``（屏幕像素坐标），没找到返回 None。"""
    tpl = template()
    if tpl is None or image is None:
        return None
    scale = image.shape[1] / 2510.0
    if abs(scale - 1.0) > 0.01:
        tpl = cv2.resize(tpl, (int(tpl.shape[1] * scale), int(tpl.shape[0] * scale)))
    if tpl.shape[0] > image.shape[0] or tpl.shape[1] > image.shape[1]:
        return None
    res = cv2.matchTemplate(image, tpl, cv2.TM_CCOEFF_NORMED)
    _min_v, max_v, _min_l, max_loc = cv2.minMaxLoc(res)
    if max_v < min_score:
        return None
    return (max_loc[0] + tpl.shape[1] // 2,
            max_loc[1] + tpl.shape[0] // 2,
            float(max_v))


# 结算流程里「再次演出 / 下一步 / 确定 / 编入」都在右下角这一带
# （实机验证：结算页点「再次演出」(2210,1040) 直接回到乐队确认页）
ADVANCE_SPOTS = [(2210, 1040), (2196, 1058), (2260, 986), (2060, 1016)]


def locate_live_start(stream, screen: tuple[int, int], stream_size: tuple[int, int],
                      look: float = 1.6):
    """看一眼流里现在是不是确认页；是就返回 LIVE START 的设备坐标。"""
    sx = screen[0] / stream_size[0]
    sy = screen[1] / stream_size[1]
    deadline = time.perf_counter() + look
    last = -1
    while time.perf_counter() < deadline:
        fr = stream.latest(timeout=0.4, newer_than=last)
        if fr is None or fr.index == last:
            continue
        last = fr.index
        hit = find_live_start(fr.image)
        if hit:
            return int(round(hit[0] * sx)), int(round(hit[1] * sy))
    return None


def wait_confirm_page(dev, stream, screen: tuple[int, int], stream_size: tuple[int, int],
                      timeout: float = 60.0, log=print):
    """等到确认页；期间点右下角把结算流程走完（再次演出 → 确认页）。"""
    deadline = time.perf_counter() + timeout
    k = 0
    while time.perf_counter() < deadline:
        hit = locate_live_start(stream, screen, stream_size)
        if hit is not None:
            return hit
        spot = ADVANCE_SPOTS[k % len(ADVANCE_SPOTS)]
        k += 1
        log(f"    还在结算流程里 → 点 {spot}")
        dev.input_tap(*spot)
        time.sleep(1.8)
    return None
