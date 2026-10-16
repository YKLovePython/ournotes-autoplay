"""核对几何：拿整屏无损截图，让程序自己量车道线和判定线，跟模型对残差。

exe 里也能用：把装有截图的文件夹**拖到 谛听.exe 上**即可。
不需要手机、不需要人看图。判定标准是数字：
  * 车道线间距：模型按屏高（2510x1156 上是 137.5px）
  * 判定线：模型 79.26% 屏高（应落在实测两条紫线正中间）
"""

from __future__ import annotations

import argparse
from pathlib import Path

import cv2
import numpy as np

from .geometry import Field


def imread_unicode(path) -> np.ndarray | None:
    """中文路径也能读（cv2.imread 在 Windows 上会静默失败）。"""
    try:
        buf = np.fromfile(str(path), dtype=np.uint8)
    except OSError:
        return None
    return cv2.imdecode(buf, cv2.IMREAD_COLOR) if buf.size else None


def magenta_rows(img: np.ndarray) -> tuple[int, int, float]:
    """返回（最强品红行 y, 该行像素数, 占宽比例）。两条紫线是判定带的上下边。"""
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    hue, sat, val = (hsv[:, :, i].astype(np.int16) for i in range(3))
    m = (hue >= 138) & (hue <= 178) & (sat > 90) & (val > 120)
    rows = m.sum(axis=1)
    y = int(np.argmax(rows))
    return y, int(rows[y]), float(rows[y]) / img.shape[1]


def lane_lines(img: np.ndarray, y: int, half_band: int = 10) -> list[int]:
    """在 y 附近一条水平带里找车道线（细的亮列）。"""
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
    y0, y1 = max(0, y - half_band), min(img.shape[0], y + half_band + 1)
    prof = np.median(g[y0:y1], axis=0)
    base = np.convolve(prof, np.ones(81) / 81, mode="same")
    d = prof - base
    thr = max(3.0, float(d.std()) * 1.2)
    xs: list[int] = []
    i = 1
    while i < len(d) - 1:
        if d[i] > thr and d[i] >= d[i - 1] and d[i] >= d[i + 1]:
            j = i
            while j < len(d) - 1 and d[j + 1] > thr:
                j += 1
            xs.append(int(round(i + int(np.argmax(d[i:j + 1])) + (j - i) / 2)))
            i = j + 1
        else:
            i += 1
    return xs


def main(argv: list[str] | None = None, log=print) -> int:
    ap = argparse.ArgumentParser(prog="谛听 --verify")
    ap.add_argument("path", nargs="+", help="截图目录或若干 png")
    ap.add_argument("--limit", type=int, default=8, help="最多看几张")
    ap.add_argument("--band", type=int, default=10, help="取带半高（像素）")
    ap.add_argument("--any-size", action="store_true",
                    help="连裁切过的局部图也看（默认只看整屏原图）")
    args = ap.parse_args(argv)

    files: list[Path] = []
    for a in args.path:
        p = Path(a)
        files += sorted(p.glob("*.png")) if p.is_dir() else [p]
    if not args.any_size:
        keep = []
        for f in files:
            img = imread_unicode(f)
            if img is not None and img.shape[1] >= 2000 and img.shape[1] > img.shape[0]:
                keep.append(f)
        files = keep
    files = files[: args.limit]
    if not files:
        log("没有整屏原图（把 2510x1156 的整屏截图放进文件夹再拖进来）")
        return 2

    log("模型：判定线 79.26% 屏高；车道线间距 = 一格的 2 倍（随屏幕尺寸自动变）")
    log("（只对能量出 ≥4 处车道线间距、且间距自洽的整屏演奏画面判定）")
    log()
    usable: list[tuple[float, float]] = []
    for f in files:
        img = imread_unicode(f)
        if img is None:
            log(f"{f.name:<26} 读不了")
            continue
        h, w = img.shape[:2]
        field = Field(w, h)
        my, _cnt, _frac = magenta_rows(img)
        xs = lane_lines(img, field.judge_y, args.band)
        inplay = [x for x in xs if abs(x - field.center_x) < field.playfield_half_px * 1.06]
        diffs = [inplay[i + 1] - inplay[i] for i in range(len(inplay) - 1)]
        near = [d for d in diffs if 0.6 * 2 * field.unit_px <= d <= 1.5 * 2 * field.unit_px]
        if len(near) < 4:
            log(f"{f.name:<26} 量不到足够的车道线（不像演奏画面），跳过")
            continue
        med = float(np.median(near))
        spread = (float(np.percentile(near, 75)) - float(np.percentile(near, 25))) / med
        if spread > 0.15:
            log(f"{f.name:<26} 间距不自洽（{len(near)} 处，四分位距 {spread * 100:.0f}%），"
                f"多半被音符盖住，跳过")
            continue
        model_pitch = 2 * field.unit_px
        outer = min(inplay) if inplay else 0
        model_outer = field.center_x - field.playfield_half_px
        usable.append((med, model_pitch))
        log(f"{f.name:<26} {w}x{h}  品红行 {my}（{my / h * 100:.1f}%）  "
            f"间距 实测 {med:.1f} / 模型 {model_pitch:.1f} px"
            f"（{(med / model_pitch - 1) * 100:+.1f}%）  "
            f"最外侧 实测 {outer:.0f} / 模型 {model_outer:.0f}")

    if len(usable) < 3:
        log()
        log(f"结论：只有 {len(usable)} 张可用，样本太少 —— 再多放几张整屏演奏画面截图"
            f"（判定线附近别挤着一大堆音符的帧最好）")
        return 2
    res = np.array([(a - b) / b for a, b in usable]) * 100.0
    log()
    log(f"结论：{len(usable)} 张可用，车道线间距残差 中位 {np.median(res):+.1f}%"
        f"（范围 {res.min():+.1f}% ~ {res.max():+.1f}%）")
    ok = abs(np.median(res)) <= 2.0
    log("      " + ("几何对得上 ✓（±2% 以内算通过）" if ok
                    else "对不上 ✗ —— 请把这份输出发出来"))
    return 0 if ok else 1
