"""判断当前画面是不是「正在演奏」。

依据：演奏画面有两条横贯整个轨道的线 —— 上方一条亮白线（y≈449/592）
与下方一条品红判定线（y≈492/592）。这两条线在选曲/编队/结算页都不存在，
所以很适合当作「是否已经进曲」的判据。
"""

from __future__ import annotations

import cv2
import numpy as np


def judgment_line_rows(image: np.ndarray) -> np.ndarray:
    """返回每一行「品红色像素」的数量。"""
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    h = hsv[:, :, 0].astype(np.int16)
    s = hsv[:, :, 1].astype(np.int16)
    v = hsv[:, :, 2].astype(np.int16)
    magenta = (h >= 138) & (h <= 178) & (s > 110) & (v > 150)
    return magenta.sum(axis=1)


def is_live_frame(image: np.ndarray, band: tuple[float, float] = (0.80, 0.88),
                  min_ratio: float = 0.45) -> bool:
    """判断这一帧是不是演奏画面。

    演奏画面有**两条**横贯轨道的长线：上方一条亮白线（y≈0.75h）和
    下方一条品红判定线（y≈0.83h）。要求两条同时成立，才能把
    编队页（只有零碎的粉色）和加载页（只有一条进度条）都排除掉——
    只要求一条时，实测会被加载页的进度条骗到，基准偏早 40 多秒。
    """
    h, w = image.shape[:2]
    magenta = judgment_line_rows(image)
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    white = ((hsv[:, :, 2] >= 200) & (hsv[:, :, 1] <= 70)).sum(axis=1)

    lo, hi = int(h * band[0]), int(h * band[1])
    if not bool((magenta[lo:hi] >= w * min_ratio).any()):
        return False
    wlo, whi = int(h * 0.73), int(h * 0.79)
    return bool((white[wlo:whi] >= w * 0.30).any())
