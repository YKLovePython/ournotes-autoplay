"""配置加载。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "default.yaml"


def load_config(path: str | Path | None = None, overrides: dict[str, Any] | None = None) -> dict:
    p = Path(path) if path else DEFAULT_PATH
    cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if overrides:
        cfg = deep_merge(cfg, overrides)
    return cfg


def deep_merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def lane_x(cfg: dict) -> list[int]:
    lanes = cfg.get("lanes", {})
    xs = lanes.get("x") or []
    count = int(lanes.get("count", 7))
    if len(xs) != count:
        raise ValueError(f"lanes.x 数量({len(xs)})与 lanes.count({count}) 不一致")
    return [int(v) for v in xs]


def save_config(cfg: dict, path: str | Path) -> None:
    Path(path).write_text(
        yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
