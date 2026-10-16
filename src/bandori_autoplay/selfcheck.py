"""自检：开机自动跑一遍（只读，不碰手机画面）。

分两级：
  * :func:`quick`  —— 每次启动都跑：谱面、模板、手机、触摸屏节点、几何。约 1 秒。
  * :func:`deep`   —— 额外试着建一次虚拟触摸屏（探注入通道）再销毁。
返回问题列表；空列表 = 全通过。
"""

from __future__ import annotations

from pathlib import Path

from . import chartdb, geometry
from .adb import AdbDevice
from .config import find_resource
from .touchlog import find_touch_device


def _line(ok: bool, name: str, detail: str = "") -> str:
    return f"  {name:<10}{'✓' if ok else '✗'} {detail}".rstrip()


def quick(cfg: dict, log=print) -> list[str]:
    """开机自检。返回问题列表（每项是一句人能看懂的话）。"""
    problems: list[str] = []
    log("自检：")

    # 谱面索引
    chart_dir = Path(cfg["paths"]["chartdb"])
    index = chart_dir / "index.json"
    master = Path(cfg["paths"]["master"])
    ok = index.exists() and (master / "MasterText.json").exists()
    log(_line(ok, "谱面", str(chart_dir) if ok else f"缺少 {index}"))
    if not ok:
        problems.append("谱面数据不全（work\\chartdb 或 work\\master 缺东西）")
    elif not chartdb.load_title_map(master):
        problems.append("曲名表读不出来（work\\master\\MasterText.json 是不是坏了）")

    # 模板（顺便验证 cv2 能用 + 中文路径能读）
    tpl = find_resource(cfg["paths"]["live_start_template"])
    from .liveui import imread_unicode

    img = imread_unicode(tpl) if Path(tpl).exists() else None
    log(_line(img is not None, "模板", Path(tpl).name if img is not None
              else f"读不到 {tpl}"))
    if img is None:
        problems.append("认不出 LIVE START 的模板图（tools\\live_start_template.png）")

    # 手机
    dev = None
    info = None
    try:
        dev = AdbDevice(serial=cfg["device"].get("serial"),
                        adb_path=cfg["device"].get("adb_path"))
        info = dev.info()
    except Exception as exc:  # noqa: BLE001
        log(_line(False, "手机", f"连不上：{exc}"))
        problems.append("连不上手机：先插好数据线、打开「USB 调试」、手机上点「始终允许」")
    if info is not None:
        log(_line(True, "手机", f"{info.model}  Android {info.android}  "
                            f"面板 {info.width}x{info.height}"))
        if min(info.width, info.height) <= 0:
            problems.append("读不到屏幕尺寸（手机上 `wm size` 是空的）")
        # 触摸屏节点
        try:
            node, unit = find_touch_device(dev, info.width)
            log(_line(True, "触摸屏", f"{node}（坐标单位 {unit:g}）"))
        except Exception as exc:  # noqa: BLE001
            log(_line(False, "触摸屏", str(exc)))
            problems.append("找不到真实触摸屏节点（换根线/换个 USB 口再试）")
        # 几何
        f = geometry.field_for(info.height, info.width)
        log(_line(True, "几何", f"判定线 y={f.judge_y}（{f.judge_y / f.screen_h * 100:.2f}% 屏高）"
                                f"  一格 {f.unit_px:.1f}px"))
    return problems


def deep(cfg: dict, root: Path, log=print) -> int:
    """完整自检：quick + 试建一次虚拟触摸屏（探注入通道）。返回退出码。"""
    log("=" * 62)
    log("  完整自检")
    log("=" * 62)
    problems = quick(cfg, log)
    if not problems:
        from .adb import AdbDevice
        from .touch import make_touch

        try:
            dev = AdbDevice(serial=cfg["device"].get("serial"),
                            adb_path=cfg["device"].get("adb_path"))
            info = dev.info()
            touch = make_touch(cfg["touch"], dev, screen_w=info.height, screen_h=info.width,
                               jar=cfg["touch"].get("scrcpy_jar"))
            touch.start()
            log(_line(True, "注入通道", f"{type(touch).__name__} 建起来了"))
            touch.close()
            log(_line(True, "清理", "已销毁虚拟触摸屏"))
        except Exception as exc:  # noqa: BLE001
            log(_line(False, "注入通道", f"{exc}"))
            problems.append("建不起虚拟触摸屏（注入通道不可用）：换根数据线、别用 USB 集线器")
    log()
    if problems:
        log("自检没过：")
        for p in problems:
            log("  · " + p)
        return 1
    log("自检通过 ✓")
    return 0
