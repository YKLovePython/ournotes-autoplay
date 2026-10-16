"""往返延迟（读取 + 注入 = L + λ）的标定。

在**加载页**做：那时游戏不响应触摸，随手点几下没有副作用。

两个讲究（旧实现吃过亏）：
  1. 先"静默"再发：等到一段时间内没有新事件，免得把上一条的尾巴算成本条，
     旧实现因此量出过 1 ms 这种不可能的值；
  2. 报中位数 + 全部分布，不只报最小值。
"""

from __future__ import annotations

import time

from .anchor import TouchClock


def _quiet(clock: TouchClock, quiet_s: float = 0.25, timeout: float = 2.0) -> None:
    """等到连续 ``quiet_s`` 秒没有新事件。"""
    deadline = time.perf_counter() + timeout
    last = len(clock.raw)
    stamp = time.perf_counter()
    while time.perf_counter() < deadline:
        if len(clock.raw) != last:
            last = len(clock.raw)
            stamp = time.perf_counter()
        elif time.perf_counter() - stamp >= quiet_s:
            return
        time.sleep(0.004)


def measure_roundtrip(device, uhid_node: str, touch, spot: tuple[int, int],
                      samples: int = 8, log=print) -> float | None:
    """量「发一条 UHID 触摸 → getevent 读到它」的往返时间（秒）。"""
    clock = TouchClock(device, uhid_node, 1, 1, log=lambda *a: None, scale=1.0)
    clock.start()
    time.sleep(0.6)
    lats: list[float] = []
    for _ in range(max(1, samples)):
        _quiet(clock, 0.25, 1.5)
        n0 = len(clock.raw)
        h0 = time.perf_counter()
        try:
            touch.down(0, spot[0], spot[1])
            time.sleep(0.03)
            touch.up(0)
        except Exception as exc:  # noqa: BLE001
            log(f"    标定注入失败：{exc}")
            break
        h1 = None
        deadline = h0 + 0.6
        while time.perf_counter() < deadline:
            for idx in range(n0, len(clock.raw)):
                if clock.raw[idx][1] == "down":
                    h1 = (clock.host_stamps[idx] if idx < len(clock.host_stamps)
                          else time.perf_counter())
                    break
            if h1 is not None:
                break
            time.sleep(0.002)
        if h1 is not None:
            lats.append(h1 - h0)
    clock.stop()
    if not lats:
        log("    没量到往返延迟（getevent 读不到 UHID 的事件？）")
        return None
    lats.sort()
    mid = lats[len(lats) // 2]
    log("    往返延迟样本（毫秒）：" + " ".join(f"{v * 1000:.1f}" for v in lats))
    log(f"    取中位 {mid * 1000:.1f} ms（最小 {lats[0] * 1000:.1f} / 最大 {lats[-1] * 1000:.1f}）")
    return mid
