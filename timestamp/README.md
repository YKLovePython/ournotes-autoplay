# Timestamp proof / 时间戳存证

**English** · [中文](#中文)

> **Status: complete.** The hash is now committed into **Bitcoin block 969627**
> (block time 2026-10-02 19:37:33 UTC = 2026-10-03 03:37:33 Beijing; submitted 03:30,
> mined 7 minutes later). Verified independently against the block header with
> `ots_verify_independent.py`.

This folder is a **public time commitment**. The implementation itself is *not*
published — but the hash of the sealed source archive is, together with independent
third-party timestamps that prove the archive already existed on **2026-10-03**.

That is the standard way to claim priority without publishing the code: anyone can later
be shown the archive, and it either matches this hash or it does not.

```
Archive : OurNotes-Autoplay-20261003.zip   (149 KB, 40 files: source + docs, kept private)
SHA-256 : 80109e206e2d2562bbea5443ea6307c114765c76d3dce334114827b85215b6af
```

## Three independent proofs

| Proof | File | What it is |
| --- | --- | --- |
| Bitcoin (OpenTimestamps) | `OurNotes-Autoplay-20261003.zip.ots` | the hash is committed into the Bitcoin blockchain via public calendars |
| RFC 3161 timestamp | `OurNotes-Autoplay-20261003.zip.tsr` | issued by FreeTSA, `granted`, genTime **2026-10-02 19:30:36 UTC** (2026-10-03 03:30 Beijing) |
| Source history | private repo commit history | commits dated from **2026-09-30** onward |

`freetsa_cacert.pem` / `freetsa_tsa.crt` are the FreeTSA certificates needed to verify
the `.tsr` token.

## How to verify

**1. Check what the timestamps are about**

```bash
sha256sum OurNotes-Autoplay-20261003.zip      # must equal the hash above
```

**2. Bitcoin proof**

```bash
ots upgrade OurNotes-Autoplay-20261003.zip    # if still "pending"
ots verify  OurNotes-Autoplay-20261003.zip    # prints the Bitcoin block height + time
```

No tools at hand? Upload the archive and the `.ots` file to https://opentimestamps.org —
it verifies in the browser.

**3. RFC 3161 token**

```bash
openssl ts -verify -in OurNotes-Autoplay-20261003.zip.tsr \
                   -data OurNotes-Autoplay-20261003.zip \
                   -CAfile freetsa_cacert.pem -untrusted freetsa_tsa.crt
```

`Verification: OK` means the token is genuine and was issued over exactly this archive.

> The archive itself is kept private. To substantiate the claim, the author can release it
> at any time; if its SHA-256 matches the value recorded here, the timestamps above apply
> to it — no trust in the author or in GitHub is required, only in Bitcoin and in the TSA.

---

## 中文

> **状态：已完成。** 该哈希已写入 **比特币区块 969627**
> （区块时间 2026‑10‑02 19:37:33 UTC = 北京时间 2026‑10‑03 03:37:33；
> 03:30 提交，7 分钟后被打包）。已用区块头独立验证通过：`ots_verify_independent.py`。

这个目录是一份**公开的时间承诺**：**代码本体不公开**，公开的是封存包的哈希，以及能证明
"这个哈希在 **2026‑10‑03** 就已经存在"的第三方时间戳。

这是"不公开代码也能主张先后"的标准做法：将来任何时候把封存包拿出来，对得上哈希就成立，
对不上就说明不是这份。

```
封存包 : OurNotes-Autoplay-20261003.zip   （149 KB，40 个文件：源码 + 文档，未公开）
SHA-256: 80109e206e2d2562bbea5443ea6307c114765c76d3dce334114827b85215b6af
```

### 三重证据

| 证据 | 文件 | 说明 |
| --- | --- | --- |
| 比特币（OpenTimestamps） | `OurNotes-Autoplay-20261003.zip.ots` | 哈希经公共日历写入比特币区块链 |
| RFC 3161 可信时间戳 | `OurNotes-Autoplay-20261003.zip.tsr` | FreeTSA 签发，状态 granted，时间 **2026‑10‑02 19:30:36 UTC**（北京时间 10‑03 03:30） |
| 提交历史 | 私有仓库 | 提交自 **2026‑09‑30** 起 |

`freetsa_cacert.pem` / `freetsa_tsa.crt` 是校验 `.tsr` 需要用到的 FreeTSA 证书。

### 怎么验证

**① 确认存证对象**

```bash
sha256sum OurNotes-Autoplay-20261003.zip     # 必须等于上面那串哈希
```

**② 比特币那条**

```bash
ots upgrade OurNotes-Autoplay-20261003.zip   # 若显示 pending，先升级
ots verify  OurNotes-Autoplay-20261003.zip   # 打印出所在的比特币区块高度与时间
```

手上没工具也可以：把封存包和 `.ots` 两个文件传到 https://opentimestamps.org 在线验证。

**③ 时间戳令牌**

```bash
openssl ts -verify -in OurNotes-Autoplay-20261003.zip.tsr \
                   -data OurNotes-Autoplay-20261003.zip \
                   -CAfile freetsa_cacert.pem -untrusted freetsa_tsa.crt
```

输出 `Verification: OK` 即通过。

> 封存包由作者自己保管。需要主张时把它公开即可：只要它的 SHA‑256 与本目录记录的一致，
> 上面这些时间戳就直接适用于它——**不需要信任作者本人，也不需要信任 GitHub**，
> 只需要信任比特币和 TSA。
