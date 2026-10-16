"""认「乐队确认页」的 LIVE START 按钮。

只做这一件事：告诉你现在能不能开演、按钮在哪。
**不点结算页、不猜坐标**——认不到就返回 None，由主流程停下来提示你。

模板按**屏幕高度**缩放（按钮贴屏幕右下角，跟高度一起缩放；旧版按宽度缩放，
换设备就会失灵）。模板取自 1156 高的实机截图。
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

TEMPLATE_REF_HEIGHT = 1156.0
_tpl: np.ndarray | None = None
_tpl_path: Path | None = None


def imread_unicode(path) -> np.ndarray | None:
    """读图，**路径里有中文也能读**（cv2.imread 在 Windows 上会静默失败）。"""
    try:
        buf = np.fromfile(str(path), dtype=np.uint8)
    except OSError:
        return None
    return cv2.imdecode(buf, cv2.IMREAD_COLOR) if buf.size else None


def template(path: str | Path) -> np.ndarray | None:
    global _tpl, _tpl_path
    p = Path(path)
    if _tpl is None or _tpl_path != p:
        _tpl = imread_unicode(p)
        _tpl_path = p
    return _tpl


def find_live_start(image: np.ndarray, tpl_path: str | Path, min_score: float = 0.62):
    """返回 ``(x, y, 匹配度)``（屏幕像素坐标）；没找到返回 None。"""
    tpl = template(tpl_path)
    if tpl is None or image is None:
        return None
    scale = image.shape[0] / TEMPLATE_REF_HEIGHT
    if abs(scale - 1.0) > 0.01:
        tpl = cv2.resize(tpl, (max(2, int(round(tpl.shape[1] * scale))),
                               max(2, int(round(tpl.shape[0] * scale)))))
    if tpl.shape[0] > image.shape[0] or tpl.shape[1] > image.shape[1]:
        return None
    res = cv2.matchTemplate(image, tpl, cv2.TM_CCOEFF_NORMED)
    _mn, mx, _ml, loc = cv2.minMaxLoc(res)
    if mx < min_score:
        return None
    return (loc[0] + tpl.shape[1] // 2, loc[1] + tpl.shape[0] // 2, float(mx))


def wait_confirm_page(grab, tpl_path: str | Path, timeout: float = 60.0,
                      min_score: float = 0.62, log=print):
    """反复抓一帧看是不是确认页；是就返回 LIVE START 坐标，超时返回 None。

    ``grab`` 是无参函数，返回 BGR 图像（例如 ``dev.screenshot_array``）。
    """
    import time

    deadline = time.perf_counter() + timeout
    n = 0
    while time.perf_counter() < deadline:
        n += 1
        try:
            img = grab()
        except Exception as exc:  # noqa: BLE001
            log(f"    抓图失败：{exc}")
            time.sleep(0.5)
            continue
        hit = find_live_start(img, tpl_path, min_score)
        if hit:
            log(f"    第 {n} 张认出确认页：LIVE START ({hit[0]}, {hit[1]})，匹配 {hit[2]:.3f}")
            return int(hit[0]), int(hit[1])
        time.sleep(0.2)
    return None
