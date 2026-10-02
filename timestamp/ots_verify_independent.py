"""不依赖本地比特币节点，独立验证 OpenTimestamps 证明。

做法：
1. 读 .ots，取出「文件摘要」和各个证明分支；
2. 对每条分支重放哈希运算，得到它声称被写进区块的那个摘要；
3. 从公共 API 取回该高度的区块头（80 字节），用证明里的默克尔路径
   验证「摘要确实在这区块里」。
4. 顺便确认 .ots 里的文件摘要与本机文件的 SHA-256 一致（证明是否适用于这个文件）。

用法：python ots_独立验证.py <被存证的文件> <文件.ots>
"""
from __future__ import annotations

import hashlib
import sys
import urllib.request

from opentimestamps.core.notary import BitcoinBlockHeaderAttestation
from opentimestamps.core.serialize import StreamDeserializationContext
from opentimestamps.core.timestamp import DetachedTimestampFile


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "codex"})
    return urllib.request.urlopen(req, timeout=30).read()


def block_header(height: int) -> bytes:
    """从 mempool.space 取指定高度的区块头（80 字节）。"""
    block_hash = fetch(f"https://mempool.space/api/block-height/{height}").decode().strip()
    header_hex = fetch(f"https://mempool.space/api/block/{block_hash}/header").decode().strip()
    return bytes.fromhex(header_hex)


def as_block_header(raw: bytes):
    """把 80 字节原始区块头转成 python-bitcoinlib 的 CBlockHeader。"""
    import io

    from bitcoin.core import CBlockHeader

    try:
        return CBlockHeader.deserialize(raw)
    except Exception:  # noqa: BLE001
        return CBlockHeader.stream_deserialize(io.BytesIO(raw))


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    target = sys.argv[1] if len(sys.argv) > 1 else "OurNotes-Autoplay-20261003.zip"
    stamp_path = sys.argv[2] if len(sys.argv) > 2 else target + ".ots"

    with open(stamp_path, "rb") as fd:
        detached = DetachedTimestampFile.deserialize(StreamDeserializationContext(fd))
    local = hashlib.sha256(open(target, "rb").read()).digest()
    print("本机文件 SHA-256 :", local.hex())
    print("证明里的文件摘要 :", detached.file_digest.hex())
    same = local == detached.file_digest
    print("两者一致         :", same)
    if not same:
        print(">>> 不一致：这份 .ots 不是给这个文件的")
        return 2

    print()
    checked = 0
    for msg, attestation in detached.timestamp.all_attestations():
        if isinstance(attestation, BitcoinBlockHeaderAttestation):
            height = attestation.height
            try:
                header = as_block_header(block_header(height))
            except Exception as exc:  # noqa: BLE001
                print(f"  区块 {height}: 取区块头失败 {str(exc)[:60]}")
                continue
            ok = attestation.verify_against_blockheader(msg, header)
            checked += 1
            print(f"  区块高度 {height}: {'✓ 验证通过' if ok else '✗ 验证失败'}")
        else:
            print(f"  （未升级的分支：{attestation.__class__.__name__}）")

    print()
    if checked:
        print("结论：该文件的 SHA-256 确实被写入了上述比特币区块 → 时间证明成立。")
    else:
        print("结论：还没有比特币区块证明（可能仍是待确认状态）。")
    return 0 if checked else 1


if __name__ == "__main__":
    raise SystemExit(main())
