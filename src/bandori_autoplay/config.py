"""配置：一个 yaml + 一份几何数据。"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

DEFAULTS = {
    "device": {
        "serial": None,
        "adb_path": None,
    },
    "touch": {
        "backend": "uhid",
        "scrcpy_jar": "tools/scrcpy/scrcpy-server-v4.1",
    },
    "play": {
        "offset_ms": 0.0,
        "default_latency_ms": 35.0,
        "handover_lead_s": 0.015,
        "template_score": 0.62,
        "confirm_timeout_s": 120.0,
    },
    "paths": {
        "chartdb": "work/chartdb/out",
        "master": "work/master",
        "logs": "logs",
        "live_start_template": "tools/live_start_template.png",
    },
}


def app_root() -> Path:
    """程序根目录：打包成 exe 后是 exe 所在目录，源码运行时是仓库根。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def resource_root() -> Path:
    """随程序走的资源（模板、scrcpy-server）：打包后在 _internal 里，源码运行时是仓库根。"""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    if getattr(sys, "frozen", False):
        internal = app_root() / "_internal"
        if internal.exists():
            return internal
    return app_root()


def find_resource(rel: str | Path) -> str:
    """先找 exe 旁边（方便你自己替换），再找打包进去的那份。"""
    p = Path(rel)
    if p.is_absolute():
        return str(p)
    for base in (app_root(), resource_root()):
        cand = base / p
        if cand.exists():
            return str(cand)
    return str(resource_root() / p)


def deep_merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load(path: str | Path | None = None) -> dict:
    """读配置；文件不存在就用内置默认值。"""
    p = Path(path) if path else app_root() / "config" / "default.yaml"
    cfg = dict(DEFAULTS)
    if p.exists():
        cfg = deep_merge(cfg, yaml.safe_load(p.read_text(encoding="utf-8")) or {})
    return cfg


def resolve_paths(cfg: dict, root: Path | None = None) -> dict:
    """把配置里的相对路径按程序目录解析成绝对路径。"""
    root = root or app_root()
    paths = cfg.setdefault("paths", {})
    # 注意：live_start_template 不在这里解析 —— 它可能被打包进 _internal，
    # 交给 find_resource 先找 exe 旁边、再找打包里那份。
    for key in ("chartdb", "master", "logs"):
        v = paths.get(key)
        if v:
            q = Path(v)
            paths[key] = str(q if q.is_absolute() else (root / q))
    jar = cfg.get("touch", {}).get("scrcpy_jar")
    if jar:
        cfg["touch"]["scrcpy_jar"] = find_resource(jar)
    tpl = paths.get("live_start_template")
    if tpl:
        paths["live_start_template"] = find_resource(tpl)
    # adb：优先用随包的那份（换电脑/换目录都能用）；配置里写了别的就先看它在不在
    adb = cfg.setdefault("device", {}).get("adb_path")
    bundled = root / "platform-tools" / ("adb.exe" if sys.platform == "win32" else "adb")
    if not adb:
        if bundled.exists():
            cfg["device"]["adb_path"] = str(bundled)
    elif not Path(str(adb)).exists():
        if bundled.exists():
            print(f"  注意：配置里的 adb 不存在（{adb}），改用随包的 platform-tools\\adb.exe")
            cfg["device"]["adb_path"] = str(bundled)
        else:
            print(f"  注意：配置里的 adb 不存在（{adb}），改用系统里能找到的 adb")
            cfg["device"]["adb_path"] = None
    return cfg
