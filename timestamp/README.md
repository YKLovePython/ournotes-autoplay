# 时间戳存证 —— 验证方法

> ⚠️ **这个文件夹里的 `OurNotes-Autoplay-20261003/` 是封包内容，已经参与哈希计算，不要再改它**。
> 要加东西就加在这个文件夹（外层），或者另建文件夹。

## 文件清单

| 文件 | 是什么 |
| --- | --- |
| `OurNotes-Autoplay-20261003.zip` | **被封存的原始包**（代码 + 文档，38 个文件 + 作者声明 + 包内哈希清单） |
| `OurNotes-Autoplay-20261003.zip.ots` | **比特币时间戳证明**（OpenTimestamps） |
| `OurNotes-Autoplay-20261003.zip.tsr` | **RFC 3161 可信时间戳**（FreeTSA 签发） |
| `存档SHA256.txt` | zip 的 SHA-256 与大小 |
| `时间戳说明_TSA.txt` | TSA 地址、状态、签发时间 |
| `freetsa_cacert.pem` / `freetsa_tsa.crt` | FreeTSA 的根证书与 TSA 证书（校验 `.tsr` 时要配套） |

**zip 的 SHA-256：**
```
80109e206e2d2562bbea5443ea6307c114765c76d3dce334114827b85215b6af
```

---

## 1. 先验证"文件没被动过"

```powershell
certutil -hashfile OurNotes-Autoplay-20261003.zip SHA256
```

输出应等于上面那串哈希。相等 → 文件与存档时完全一致。

（这是所有时间证明的前提：证明只对"这一串哈希"有效，所以 **zip 不要改**。）

---

## 2. 验证比特币时间戳（`.ots`）

**当晚生成的是"待确认"状态**，需要等 1~2 小时（日历把哈希写进比特币交易、交易被打包）。之后再执行：

```powershell
ots upgrade OurNotes-Autoplay-20261003.zip     # 把待确认升级为已锚定
ots verify  OurNotes-Autoplay-20261003.zip     # 校验：会显示区块高度与区块时间
ots info    OurNotes-Autoplay-20261003.zip.ots # 只看证明内容
```

不想装工具的话，把 **zip 和 `.ots` 两个文件**上传到 https://opentimestamps.org 也能在线校验。

> `ots` 本机已装好（v0.7.2）。本机缺 OpenSSL 导致 `python-bitcoinlib` 导入失败的问题，已通过在
> `bitcoin/core/key.py` 里加一段"找不到 OpenSSL 就退化成占位对象"的兜底修好——只影响密钥/钱包功能，
> 存证、升级、校验都不受影响。

---

## 3. 验证 RFC 3161 时间戳（`.tsr`）

需要 openssl（本机没装；可从 https://slproweb.com/products/Win32OpenSSL.html 装，或用 Git Bash 自带版本）：

```bash
openssl ts -verify -in OurNotes-Autoplay-20261003.zip.tsr \
                   -data OurNotes-Autoplay-20261003.zip \
                   -CAfile freetsa_cacert.pem -untrusted freetsa_tsa.crt
```

看到 `Verification: OK` 即通过。也可以只做粗验：时间戳文件里应当**包含 zip 的 SHA-256 原文**
（本机已比对，结果为 `True`）。

> FreeTSA 是免费的公共 TSA。若需要**法律效力更强**的签名时间戳，见下面「还可以加什么」。

---

## 4. 还可以加什么（按权威性）

| 方式 | 说明 | 成本 |
| --- | --- | --- |
| **国内版权登记**（中国版权保护中心 / 各省版权局） | 拿到《作品登记证书》，是国内维权最常见、法院最认的形式 | 免费或几十元，周期 1~2 个月 |
| **可信时间戳**（联合信任 tsa.cn 等） | 国内法院普遍认可的第三方时间戳服务，可对源代码/文档出证 | 按次收费，几分钟出证 |
| **Zenodo DOI** | 把 GitHub release 同步到 Zenodo，获得一个学术界的唯一 DOI 与"发布日期"，第三方托管 | 免费 |
| **Software Heritage** | 提交仓库归档，获得永久 SWHID 与归档时间 | 免费 |
| **eIDAS 合格时间戳**（欧盟） | 欧盟法律意义上的合格电子时间戳 | 收费 |

建议：**国内版权登记 + 可信时间戳**（法律用）＋ **本文件夹这套**（免费、可独立验证、随时可做）。

---

## 5. 保存建议

这套东西的价值在于**长期可验证**，所以要：

1. **三处存放**：本机、云盘（如网盘/OneDrive）、离线介质（U 盘/移动硬盘各一份）
2. **zip 永远不要再改**；要出新版本就重新打包 + 重新做时间戳，旧包保持原样
3. `.ots` / `.tsr` 必须与 zip **一起**保存——它们只对这一个哈希有效
