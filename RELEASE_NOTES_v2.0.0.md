## 谛听 v2.0.0 —— 零标定版本

**你按第 1 个音符，它把整首打完。** 落点全部由**游戏自己的数据**算出来，跟你的手指点在哪无关；
你的第一下只提供**时间**（内核时间戳）。

### 这一版最大的变化：把"要标定的"变成"要算的"

演奏区几何不再靠量画面，而是从游戏场景 `EmbScene/Live` 里读出来：

| 从游戏数据读到的 | 值 |
|---|---|
| 判定平面（`judgementScreenBottomPosition = 2.24`，平面半高 ±5.4） | y = −3.16 → **屏高的 79.26%** |
| 谱面 24 格（`laneCount = 24`） | 格距 0.64237 平面单位 |
| 演奏相机 fov / 判定平面到相机的距离 | 54° / 10.6078（该处视锥半高 5.4048 ≈ 平面半高 5.4） |

⇒ **平面 1:1 铺在屏幕高度上、横向居中**，于是：

```
1 平面单位 = 屏高 H / 10.8 像素
x(格位 c)  = W/2 + (c − 12) × 0.64237 × H / 10.8
判定线 y   = H × 0.79259
```

**换手机、换分辨率都不需要标定。** 实测复核（31 张实机 2510×1156 无损截图）：
车道线间距残差中位 **−1.1%**。

### 顺带修掉的老问题（这就是重做的原因）

| | 旧实现 | 真值 | 后果 |
|---|---|---|---|
| 判定线 y | 83.04%（取了下面那条装饰紫线） | 79.26% | 偏低 44 px |
| 每格宽度 | 71.7 px | 68.8 px | 偏宽 5.5% |
| 最外侧格落点 | 393 px | 430 px | **偏 37 px，掉到最外侧车道命中区之外** |

### 用法（四步）

1. 下载下面这个 zip，**解压**；
2. 手机打开 **USB 调试**、插线、弹窗勾"始终允许"；
3. 游戏里选好歌和难度，停在有 **LIVE START** 的那一页；
4. 双击 `谛听.exe` → 回车选歌、回车选难度 → 演奏画面出来后**按第 1 个音符**。

启动会自动跑只读自检；选歌界面按 `c` = 完整自检（含试建虚拟触摸屏）。
便携版**自带运行库，不用装 Python**（Windows 10/11 x64 + 安卓手机）。

### 校验与时间存证

* 发布包 SHA-256：`4220681dd8f44072d791e3f9bd1d2dbdd533cc95d94c6ead2e9ecb7adfb49311`
* RFC 3161 可信时间戳（FreeTSA）：`timestamp/Diting-OurNotes-Autoplay-v2.0.0-win64.zip.tsr`
* 比特币时间戳（OpenTimestamps）：`timestamp/Diting-OurNotes-Autoplay-v2.0.0-win64.zip.ots`
* 独立验证方法：[timestamp/验证方法_v2.0.0.md](timestamp/验证方法_v2.0.0.md)

交流群 **227897560** ｜ B站 **作业快没了**

---

## Diting v2.0.0 — zero-calibration release

**You tap the first note; it plays the rest.** Every touch position is computed from the game's own
scene data — nothing to calibrate, nothing to measure by eye. Your first tap only supplies a
*timestamp* (its kernel timestamp).

Geometry is now read out of the game's `EmbScene/Live` scene (judgement line at **79.26 % of screen
height**; 24-unit lane grid, 0.64237 units apart; lane camera `fov = 54°`; judgement plane 10.6078
units from the camera, where the frustum half-height 5.4048 equals the plane's half-height 5.4),
which yields a one-line device rule: **the plane maps 1:1 onto the screen height, centred
horizontally** → `1 plane unit = H / 10.8 px`. **No calibration on a new phone.**
Verified against 31 full-resolution screenshots taken on the device (lane-line spacing residual
**−1.1 % median**).

### Usage

1. Download the zip below and extract it.
2. Enable **USB debugging**, connect the phone, accept the prompt (“always allow”).
3. Pick a song and difficulty in the game and stop on the page with **LIVE START**.
4. Double-click `谛听.exe`, press Enter twice, then **tap the first note** when the play screen appears.

A read-only self-check runs at startup; press `c` in the song list for the full check.
The portable build bundles its own runtime — **no Python needed** (Windows 10/11 x64 + Android).

Release SHA-256: `4220681dd8f44072d791e3f9bd1d2dbdd533cc95d94c6ead2e9ecb7adfb49311`
(RFC 3161 + Bitcoin timestamp proofs under `timestamp/`.)

For study and technical research only. Automation may violate the game's terms of service —
use at your own risk, never on your main account. MIT licensed.
