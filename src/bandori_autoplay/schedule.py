"""把谱面事件摊成动作表（down / move / up）。

时间一律**相对谱面 0 点**——所以在你按下之前就能全部算好，按下之后只做一次
"加上 t0、从还没过期的音符开始发"，接管点能提前到几乎不丢音符。

三类手势：
  tap   按下、按 40ms、抬起
  long  按住并沿轨迹移动，到点后再多按一会儿才松手；头/尾可能是轻扫
  flick 按下、朝谱面给的方向划出去、抬起
"""

from __future__ import annotations

TAP_MS = 0.040
HOLD_TAIL_PAD_S = 0.060
FLICK_LEAD_S = 0.020
FLICK_SPAN_S = 0.050
FLICK_DIST = 120
SLOTS = 9
RELEASE_GAP_S = 0.030
MOVE_TARGET_PX = 50
MOVE_MIN_S = 0.025


def flick_vector(direction: str, dist: int = FLICK_DIST) -> tuple[int, int]:
    """轻扫方向 -> 屏幕位移。谱面里只有 up / left / right。"""
    d = (direction or "up").lower()
    if d == "left":
        return -dist, 0
    if d == "right":
        return dist, 0
    return 0, -dist


def _resample_path(path, to_px, t_from: float, t_to: float,
                   target_px: float = MOVE_TARGET_PX, min_s: float = MOVE_MIN_S):
    """长条轨迹重采样成 ``[(t_rel, 屏幕 x), ...]``，让手指贴着条子连续走。

    谱面节点约 77~157ms 一个，照抄就是"一格一跳"（单步中位 197px，和条子容差同量级，
    所以有概率掉）。按**位移**采样：走够 target_px 发一次，同一根手指最快 min_s 一次。
    """
    pts = [(t_ms / 1000.0, float(to_px(c))) for t_ms, c in (path or ())]
    if len(pts) < 2 or t_to <= t_from:
        return []
    out: list[tuple[float, float]] = []
    last_t = last_px = None
    for i in range(1, len(pts)):
        ta, pa = pts[i - 1]
        tb, pb = pts[i]
        if tb <= ta:
            continue
        n = max(1, int(abs(pb - pa) / (target_px / 2.0) + 0.999))
        for k in range(1, n + 1):
            r = k / n
            t = ta + (tb - ta) * r
            px = pa + (pb - pa) * r
            if t < t_from - 1e-9 or t > t_to + 1e-9:
                continue
            if last_t is not None and (t - last_t) < min_s:
                continue
            if last_px is not None and abs(px - last_px) < target_px:
                continue
            out.append((t, px))
            last_t, last_px = t, px
    return out


def build_actions(events, field, start_index: int = 0,
                  tap_ms: float = TAP_MS, hold_pad_s: float = HOLD_TAIL_PAD_S):
    """返回按时间排序的 ``[(t_rel, kind, slot, x, y), ...]``，t_rel 相对谱面 0 点。"""
    acts: list[tuple[float, str, int, int, int]] = []
    y = field.judge_y
    busy = [0.0] * (SLOTS + 1)

    def take_slot(t: float) -> int:
        free = [s for s in range(1, SLOTS + 1) if busy[s] <= t]
        if free:
            return min(free)
        return min(range(1, SLOTS + 1), key=lambda s: busy[s])

    for i in range(start_index, len(events)):
        e = events[i]
        t_hit = e.time_ms / 1000.0
        x = field.x_of(e.center)

        if e.kind == "flick" and not e.is_hold:
            dx, dy = flick_vector(e.direction)
            t_down = t_hit - FLICK_LEAD_S
            slot = take_slot(t_down)
            t_up = t_hit + FLICK_SPAN_S
            busy[slot] = t_up + RELEASE_GAP_S
            acts.append((t_down, "down", slot, x, y))
            acts.append((t_hit, "move", slot, x + dx // 2, y + dy // 2))
            acts.append((t_hit + FLICK_SPAN_S / 2, "move", slot, x + dx, y + dy))
            acts.append((t_up, "up", slot, x + dx, y + dy))
            continue

        if e.is_hold:
            head_f = getattr(e, "head_flick", "") or (e.direction if e.kind == "flick" else "")
            tail_f = getattr(e, "tail_flick", "")
            t_end = (e.end_ms or e.time_ms) / 1000.0
            t_down = t_hit - (FLICK_LEAD_S if head_f else 0.0)
            follow_to = t_end - (FLICK_SPAN_S if tail_f else 0.0)
            t_up = t_end + (FLICK_SPAN_S + 0.02 if tail_f else hold_pad_s)
            slot = take_slot(t_down)
            busy[slot] = t_up + RELEASE_GAP_S
            fx, fy = x, y
            if head_f:
                dx, dy = flick_vector(head_f)
                acts.append((t_down, "down", slot, fx, fy))
                acts.append((t_hit, "move", slot, fx + dx // 2, fy + dy // 2))
                acts.append((t_hit + FLICK_SPAN_S / 2, "move", slot, fx + dx, fy + dy))
                acts.append((t_hit + FLICK_SPAN_S, "move", slot, x, y))
                fx, fy = x, y
            else:
                acts.append((t_down, "down", slot, fx, fy))
            start_follow = t_hit + (FLICK_SPAN_S if head_f else 0.0)
            for t_abs, px in _resample_path(e.path, field.x_of, start_follow, follow_to):
                nx = int(round(px))
                if nx == fx:
                    continue
                acts.append((t_abs, "move", slot, nx, fy))
                fx = nx
            if tail_f:
                tail_px = int(round(field.x_of(e.path[-1][1] if e.path else e.center)))
                if tail_px != fx:
                    acts.append((follow_to, "move", slot, tail_px, fy))
                    fx = tail_px
                dx, dy = flick_vector(tail_f)
                acts.append((t_end, "move", slot, fx + dx // 2, fy + dy // 2))
                acts.append((t_end + FLICK_SPAN_S / 2, "move", slot, fx + dx, fy + dy))
                fx, fy = fx + dx, fy + dy
            acts.append((t_up, "up", slot, fx, fy))
            continue

        slot = take_slot(t_hit)
        t_up = t_hit + tap_ms
        busy[slot] = t_up + RELEASE_GAP_S
        acts.append((t_hit, "down", slot, x, y))
        acts.append((t_up, "up", slot, x, y))

    acts.sort(key=lambda a: (a[0], 1 if a[1] == "up" else 0))
    return acts
