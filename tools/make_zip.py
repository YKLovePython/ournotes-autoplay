"""把便携版打成一个 zip：解压后就是一个 ``谛听\\`` 文件夹（exe + 若干文件夹）。

    python tools/make_zip.py [输出.zip]

为什么不用 Compress-Archive：谱面文件的时间戳是 1970 年，早于 zip 格式的下限
（1980），PowerShell 会直接报错。这里统一写一个固定时间戳。
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "dist" / "谛听"
DT = (2026, 10, 16, 0, 0, 0)          # zip 里统一的时间戳
SKIP_DIRS = {"__pycache__", "logs", ".pytest_cache", ".git"}
SKIP_SUFFIX = {".pyc", ".log"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out", nargs="?", default=str(Path.home() / "Desktop" / "谛听_便携版.zip"))
    ap.add_argument("--src", default=str(SRC))
    ap.add_argument("--arcroot", default=None, help="zip 里的顶层文件夹名（默认用源目录名）")
    args = ap.parse_args()

    src = Path(args.src)
    out = Path(args.out)
    arcroot = args.arcroot or src.name
    if not src.exists():
        print(f"找不到源目录 {src}")
        return 2

    files = [f for f in sorted(src.rglob("*"))
             if f.is_file()
             and not any(p in SKIP_DIRS for p in f.parts)
             and f.suffix.lower() not in SKIP_SUFFIX]
    print(f"打包 {len(files)} 个文件 → {out}")
    total = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for i, f in enumerate(files, 1):
            rel = f.relative_to(src).as_posix()
            info = zipfile.ZipInfo(f"{arcroot}/{rel}", date_time=DT)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            data = f.read_bytes()
            zf.writestr(info, data)
            total += len(data)
            if i % 50 == 0:
                print(f"  {i}/{len(files)} …", flush=True)
    print(f"\n完成：{out}  {out.stat().st_size / 1048576:.1f} MB"
          f"（原始 {total / 1048576:.1f} MB）")
    print("解压后是一个 谛听\\ 文件夹：里面只有 谛听.exe 和几个文件夹。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
