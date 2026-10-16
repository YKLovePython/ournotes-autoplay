"""独立核对时间存证：RFC 3161（.tsr）与比特币（.ots）。

    python tools/verify_timestamp.py [存证目录，默认 ./timestamp]

.tsr 的核对是"自证"的：token 里必须含有被存证文件的 SHA-256（本脚本直接比对），
并打印 TSA 的签发时间。要验证签名链本身，用 openssl（见 timestamp/验证方法.md）。
.ots 用本机的 ots 命令核对（没有就只列出状态）。
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> bytes:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.digest()


def gen_time(blob: bytes) -> str | None:
    i = 0
    while i < len(blob) - 17:
        if blob[i] == 0x18 and blob[i + 1] == 0x0F:
            raw = blob[i + 2:i + 17]
            if raw.endswith(b"Z") and raw[:14].isdigit():
                return raw.decode()
        i += 1
    return None


def main() -> int:
    d = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "timestamp"
    if not d.exists():
        print(f"找不到目录 {d}")
        return 2
    print(f"核对目录：{d}\n")
    bad = 0
    pairs = []
    for tsr in sorted(d.glob("*.tsr")):
        target = tsr.with_name(tsr.name[:-4])
        if not target.exists():
            print(f"  ! {tsr.name}：找不到对应的 {target.name}")
            bad += 1
            continue
        digest = sha256(target)
        blob = tsr.read_bytes()
        ok = digest in blob
        t = gen_time(blob)
        pairs.append((target.name, ok, t))
        print(f"  {'✓' if ok else '✗'} {target.name}")
        print(f"      SHA-256 {digest.hex()[:40]}…   .tsr {len(blob)} 字节"
              + (f"   签发（UTC）{t[:4]}-{t[4:6]}-{t[6:8]} {t[8:10]}:{t[10:12]}:{t[12:14]}"
                 if t else ""))
        bad += 0 if ok else 1
    if not pairs:
        print("  （没有 .tsr）")
    print()
    ots_files = sorted(d.glob("*.ots"))
    if ots_files:
        print("比特币时间戳（.ots）：")
        for o in ots_files:
            target = o.with_name(o.name[:-4])
            try:
                r = subprocess.run(["ots", "info", str(o)], capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=60)
                info = (r.stdout or r.stderr).strip().splitlines()
                first = info[1].strip() if len(info) > 1 else (info[0] if info else "")
                print(f"  · {target.name:<46} {first[:60]}")
            except FileNotFoundError:
                print(f"  · {target.name:<46}（本机没装 ots，跳过）")
            except Exception as exc:  # noqa: BLE001
                print(f"  · {target.name:<46}（ots 报错：{type(exc).__name__}）")
    print()
    print("结论：" + ("时间戳与文件一致 ✓" if bad == 0 else f"有 {bad} 项对不上 ✗"))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
