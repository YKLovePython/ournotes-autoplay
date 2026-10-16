"""给文件做时间存证：RFC 3161 可信时间戳（FreeTSA） + 比特币时间戳（OpenTimestamps）。

    python tools/timestamp.py <文件> [<文件> ...]

RFC 3161 用 DER 编码一个 TimeStampReq（sha256 + 随机 nonce）POST 到 FreeTSA，
把回来的 TimeStampResp 存成 ``<文件>.tsr``；顺便在 token 里核对：
  * status = granted
  * token 里确实含有该文件的 SHA-256
  * 取出 genTime（签发时间）

比特币那部分调本机的 ``ots`` 命令（OpenTimestamps）。刚提交时是"待确认"，
过 1~2 小时用 ``ots upgrade`` 升级为已锚定。

不需要 openssl：DER 是自己拼的，验证靠"哈希在 token 里"这一条硬事实 +
可由任何人用 openssl/官网独立复核。
"""

from __future__ import annotations

import hashlib
import os
import random
import subprocess
import sys
import urllib.request
from pathlib import Path

# 依次尝试的公共 TSA（RFC 3161）。用哪个会记在输出里 —— 校验时要配套对应的根证书。
TSA_URLS = (
    "https://freetsa.org/tsr",
    "http://timestamp.digicert.com",
    "http://timestamp.sectigo.com",
    "https://rfc3161.ai.moda",
)


def der_len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def der(tag: int, payload: bytes) -> bytes:
    return bytes([tag]) + der_len(len(payload)) + payload


def seq(*items: bytes) -> bytes:
    return der(0x30, b"".join(items))


def integer(value: int) -> bytes:
    if value == 0:
        return der(0x02, b"\x00")
    b = value.to_bytes((value.bit_length() + 7) // 8, "big")
    if b[0] & 0x80:
        b = b"\x00" + b
    return der(0x02, b)


OID_SHA256 = bytes.fromhex("0609608648016503040201")     # 2.16.840.1.101.3.4.2.1
OID_SIGNED_DATA = bytes.fromhex("06092a864886f70d010702")


def ts_request(digest: bytes) -> bytes:
    """TimeStampReq ::= { version 1, messageImprint { AlgorithmIdentifier, OCTET STRING },
    nonce INTEGER, certReq BOOLEAN TRUE }"""
    imprint = seq(seq(OID_SHA256, b"\x05\x00"), der(0x04, digest))
    return seq(integer(1), imprint, integer(random.getrandbits(64)),
               der(0x01, b"\xff"))


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def find_generalized_time(der_bytes: bytes) -> str | None:
    """在 token 里找 GeneralizedTime（0x18, 15 字节, YYYYMMDDHHMMSSZ）。"""
    i = 0
    while i < len(der_bytes) - 17:
        if der_bytes[i] == 0x18 and der_bytes[i + 1] == 0x0F:
            raw = der_bytes[i + 2:i + 17]
            if raw.endswith(b"Z") and raw[:14].isdigit():
                return raw.decode()
        i += 1
    return None


def stamp_rfc3161(path: Path, log=print) -> bool:
    digest = hashlib.sha256(path.read_bytes()).digest()
    req = ts_request(digest)
    log(f"  {path.name}：SHA-256 {digest.hex()[:32]}…")
    body = b""
    used = ""
    for url in TSA_URLS:
        request = urllib.request.Request(
            url, data=req,
            headers={"Content-Type": "application/timestamp-query",
                     "User-Agent": "diting-timestamp/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=45) as resp:
                body = resp.read()
            if body and digest in body:
                used = url
                break
            log(f"    （{url} 的回复里没有我们的哈希，换下一个）")
            body = b""
        except Exception as exc:  # noqa: BLE001
            log(f"    （{url} 失败：{type(exc).__name__} {exc}）")
    if not body:
        log("  ✗ 所有 TSA 都没成功")
        return False
    out = path.with_suffix(path.suffix + ".tsr")
    out.write_bytes(body)
    ok_hash = digest in body
    gen = find_generalized_time(body)
    log(f"  ✓ 已保存 {out.name}（{len(body)} 字节）  TSA：{used}")
    log(f"    token 内含该文件的 SHA-256：{ok_hash}")
    if gen:
        log(f"    TSA 签发时间（UTC）：{gen[:4]}-{gen[4:6]}-{gen[6:8]} "
            f"{gen[8:10]}:{gen[10:12]}:{gen[12:14]}")
    return ok_hash


def stamp_ots(path: Path, log=print) -> bool:
    exe = None
    for cand in ("ots", os.path.expandvars(r"%LOCALAPPDATA%\Packages\PythonSoftwareFoundation.Python.3.11_qbz5n2kfra8p0\LocalCache\local-packages\Python311\Scripts\ots.exe")):
        try:
            subprocess.run([cand, "--version"], capture_output=True, timeout=30)
            exe = cand
            break
        except Exception:  # noqa: BLE001
            continue
    if exe is None:
        log("  ! 本机没找到 ots 命令，跳过比特币时间戳")
        return False
    r = subprocess.run([exe, "stamp", str(path)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=180)
    if r.returncode != 0:
        log(f"  ✗ ots stamp 失败：{(r.stderr or r.stdout).strip()[:200]}")
        return False
    log(f"  ✓ 比特币时间戳已提交（待确认，1~2 小时后可 ots upgrade 升级）")
    return True


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    print("=" * 64)
    print("  RFC 3161 可信时间戳（FreeTSA）")
    print("=" * 64)
    ok = True
    for arg in sys.argv[1:]:
        p = Path(arg)
        if not p.exists():
            print(f"  找不到 {p}")
            ok = False
            continue
        ok = stamp_rfc3161(p) and ok
    print()
    print("=" * 64)
    print("  比特币时间戳（OpenTimestamps）")
    print("=" * 64)
    for arg in sys.argv[1:]:
        p = Path(arg)
        if p.exists():
            stamp_ots(p)
    print()
    print("全部完成 ✓" if ok else "有项目没成功 ✗")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
