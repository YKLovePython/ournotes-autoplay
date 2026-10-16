# 谛听 · BanG Dream! Our Notes 自动演奏（真机 · 免 root · 零标定）

**你按第 1 个音符，它把整首打完。**
落点**全部由游戏自己的数据算出来**，跟你的手指点在哪无关；你的第一下只提供**时间**（内核时间戳）。

**Diting — autoplay for BanG Dream! Our Notes on a real, unrooted Android phone.**
You tap the first note; it plays the rest. Every touch position is **computed from the game's own
LiveScene data**, so there is nothing to calibrate and nothing to measure by eye — your first tap
only supplies a *timestamp* (the kernel timestamp of that touch).

[下载便携版（免安装、不用 Python）](../../releases/latest) ｜ 交流群 **227897560** ｜ B站：**作业快没了**

---

## 一、这个项目的独特之处（四条，逐条可验证）

### 1. 演奏区几何来自游戏自己的数据，不是量出来的，也不是标定出来的

我们从游戏的 `EmbScene/Live` 场景里读出了判定平面与相机：

| 从游戏数据里读到的 | 值 |
|---|---|
| 判定平面（`judgementScreenBottomPosition = 2.24`，平面半高 ±5.4） | y = −3.16 → **屏高的 79.26%** |
| 谱面 24 格布局（`laneCount = 24`） | 格距 0.64237 平面单位 |
| 演奏相机 `fov` / 判定平面到相机的距离 `screen_root.z` | 54° / 10.6078（该处视锥半高 5.4048 ≈ 平面半高 5.4） |
| 轨道线样式 `_splitCountType = 2` | 6 主线 + 12 副线 → **12 条车道**（不是 7 条） |

由此得到一条**一行就能写下的换设备规则**：平面 1:1 铺在屏幕高度上，横向居中 ——

```
1 平面单位 = 屏高 H / 10.8 像素
x(格位 c)  = W/2 + (c − 12) × 0.64237 × H / 10.8      c 取 0..24
判定线 y   = H × 0.79259
```

**换手机、换分辨率都不用标定**：程序按屏幕尺寸自己算。
实测复核：31 张实机 2510×1156 无损截图，车道线间距残差 **中位 −1.1%**（见 [docs/03_验证记录.md](docs/03_验证记录.md)）。

### 2. 免 root、真机（不是模拟器）

借 `scrcpy-server` 注册一块 **UHID 虚拟触摸屏**（`/dev/uhid`）——系统把它当成真外设，
毫秒级、真多指，游戏分不出真假。不 root、不 hook、不碰游戏内存、不改游戏文件。

### 3. 运行时不靠视觉

全程只在开头认一次「乐队确认页」（模板匹配，按屏高缩放）。
之后是**纯谱面驱动** —— 没有 CV 误检把落点推偏的风险，也不需要实时取流（连 ffmpeg 都不需要）。

### 4. 锚点只负责时间

你按下的那一下，取它的**内核时间戳**当整首歌的 0 点（"当钟用"，不是校准）。
于是横屏正反（A/B）、手指那侧的坐标单位…… 这些适配项**全部消失**：位置由程序算，手指只提供时间。

---

## 二、快速开始

1. 到 [Releases](../../releases/latest) 下载 `Diting-OurNotes-Autoplay-*.zip`，**解压**（一定要解压）；
2. 手机：设置 → 开发者选项 → 打开 **USB 调试**；数据线接电脑；
   弹窗「允许 USB 调试吗」→ 勾**始终允许** → 确定（小米/红米可拔插一次）；
3. 游戏里选好歌和难度，**停在有 LIVE START 的那一页**；
4. 双击解压出来的 `谛听.exe` → 回车选歌 → 回车选难度；
5. 演奏画面出来后，**按第 1 个音符**，按完松手 —— 后面交给它。

启动时会自动跑一遍只读自检（谱面 / 模板 / 手机 / 触摸屏 / 几何），哪一项不对都会直接告诉你怎么办。
选歌界面按 `c` = 完整自检（额外试建一次虚拟触摸屏）。

**环境**：Windows 10/11 + 安卓手机。便携版**自带运行库，不需要装 Python**；
只支持 Windows 电脑 + 安卓手机（macOS / Linux / iPhone 用不了）。

---

## 三、和同类方案比

| | 本项目 | 模拟器方案（如 ChissaQAQ/OurNotes-Auto） |
|---|---|---|
| 设备 | **真机**，免 root | 必须 MuMu 模拟器 |
| 分辨率 | **任意**（按屏高自适应） | 必须 16:9，非 16:9 直接拒绝启动 |
| 几何来源 | **游戏场景数据 + 一行公式**（零标定） | 固定 16:9 基准平面；换机器靠逐局自动学偏移 |
| 同步方式 | 人手锚点（内核时间戳） | 逐帧拟合首音符运动 + 结算页 FAST/SLOW 闭环 |
| 运行时视觉 | 只在开头认一次确认页 | 持续取流（截图 + 渲染网格对齐） |
| 代价 | 需要你按一下第 1 个音符 | 可无人值守连续打 |

**一句话**：我们不是功能最多的，但在「真机 + 免 root + 零标定 + 只用手按一下当钟」这条路上，
目前是走得最远的 —— 而且"零标定"这件事是可复现、可验证的，不是宣传语。
（我们正是先踩过"从画面量几何"的坑 —— 判定线取错 44 px、横向偏宽 5.5%、最外侧格偏 37 px ——
才转向"从游戏数据算"的。这段过程记在 [docs/01_独特思路.md](docs/01_独特思路.md)。）

---

## 四、验证与时间存证

* **几何**：`tools/verify_geometry.py`，或者把装有整屏截图的文件夹**拖到 `谛听.exe` 上**。
  程序自己量车道线间距和判定线，输出数字残差（±2% 以内算通过），不需要人看图。
* **时间存证**：`timestamp/` 里有便携包的 SHA-256、RFC 3161 可信时间戳（FreeTSA）与
  比特币时间戳（OpenTimestamps），以及独立验证方法（[timestamp/验证方法.md](timestamp/验证方法.md)）。

## 五、目录结构

便携版（解压后）：

```
谛听.exe            主程序（双击即用；自检、几何核对都内置）
_internal\          运行库（Python + numpy + OpenCV，全部随包）
config\             开关：offset_ms（整体微调）
geometry\           演奏区几何常数（来自游戏数据；游戏更新后换它，不用重打包）
work\               谱面 + 曲名表
platform-tools\     adb
```

本仓库：`src/bandori_autoplay/`（10 个模块）、`tools/`（入口 + 自检 + 打包 + 几何核对）、
`docs/`（思路 / 技术细节 / 验证记录 / 时间线）、`timestamp/`（时间存证）。

## 六、免责声明

本项目仅供学习与技术研究，与 BanG Dream!、Bushiroad、Craft Egg、bilibili 及游戏运营方无关，
也未获其授权或认可。使用自动化工具可能违反游戏服务条款，可能导致账号被限制或封禁，
后果由使用者自行承担。请勿用于活动排名、竞技等影响其他玩家的场合，也不要拿主力账号冒险。

## 七、鸣谢

谱面数据来自社区项目 [our-notes-chartdb](https://github.com/ryougishiiki/our-notes-chartdb)；
"从游戏数据算几何"这条路能走通，前提是社区已经把游戏的渲染链逐行镜像出来 ——
[nnnotes](https://github.com/MetaSekaiLab/nnnotes) 与
[ournotes-player](https://github.com/empty-sekai/ournotes-player)；
触控注入基于 [scrcpy](https://github.com/Genymobile/scrcpy)。MIT License。

---

# English

**Diting — a human-anchored autoplay for *BanG Dream! Our Notes*, on a real unrooted phone.**

You tap the **first** note; it plays everything after it. The point of this project is what it does
*not* need: no root, no emulator, no per-device calibration, and no computer vision while playing.

## Why it is different

1. **The playfield geometry comes from the game's own data, not from measurements.** We read the
   judgement plane and the lane camera out of the game's `EmbScene/Live` scene: the judgement line
   sits at **79.26 % of the screen height**, lanes are laid out on a 24-unit grid (0.64237 units
   apart), the lane camera has `fov = 54°`, and the judgement plane sits 10.6078 units in front of
   it — where the frustum half-height (5.4048) equals the plane's own half-height (5.4). That gives
   a one-line device rule: **the plane is mapped 1:1 onto the screen *height*, centred
   horizontally** → `1 plane unit = H / 10.8 px`, `x(c) = W/2 + (c − 12)·0.64237·H/10.8`,
   `judgement y = H·0.79259`. **Nothing to calibrate on a new phone.** Verified against 31
   full-resolution screenshots taken on the device: lane-line spacing residual **−1.1 % median**.
2. **Real phone, no root.** A virtual HID touchscreen (UHID) is registered through `scrcpy-server`,
   so Android sees a real external touch device — millisecond latency, true multi-touch.
3. **No vision at runtime.** The screen is used once, to recognise the band-confirm page; everything
   after that is chart-driven, so no CV mis-detection can push a note off target.
4. **The human anchor carries time only.** Your first tap is used as a *clock* (its kernel
   timestamp), never as a position — landscape orientation and touch coordinate units stop mattering.

## Quick start

1. Download the zip from [Releases](../../releases/latest) and extract it (extracting is required).
2. Enable **USB debugging** on the phone, connect it, and accept the prompt ("always allow").
3. In the game, pick a song and difficulty and stop on the page with the **LIVE START** button.
4. Double-click `谛听.exe`, press Enter twice (song, difficulty).
5. When the play screen appears, **tap the first note** and let go.

Windows 10/11 + Android only. The portable build bundles its own runtime — **no Python needed**.
A read-only self-check runs at startup and tells you what to fix if something is missing;
press `c` in the song list for the full check.

## Comparison

| | This project | Emulator-based tools (e.g. ChissaQAQ/OurNotes-Auto) |
|---|---|---|
| Device | **real phone, unrooted** | MuMu emulator required |
| Resolution | **any** (scales with screen height) | must be 16:9, otherwise refuses to start |
| Geometry | **from the game's scene data, zero calibration** | fixed 16:9 plane; per-machine offset learned over plays |
| Sync | human anchor (kernel timestamp) | per-frame first-note fitting + FAST/SLOW feedback loop |
| Runtime vision | once, to find the confirm page | continuous |
| Trade-off | you tap the first note | can run unattended |

We are not the tool with the most features — we are the one that removed the most assumptions, and
that claim is reproducible: the geometry check prints a residual, and the timestamp proof is
verifiable without trusting us.

## Verification and timestamp proof

* Geometry: `tools/verify_geometry.py`, or drag a folder of full-screen screenshots onto `谛听.exe`.
  The program measures lane-line spacing and the judgement line itself and prints the residuals.
* `timestamp/`: SHA-256 of the portable archive, an RFC 3161 timestamp (FreeTSA) and a Bitcoin
  timestamp (OpenTimestamps), plus
  [independent verification steps](timestamp/验证方法.md).

## Disclaimer

For study and technical research only. Not affiliated with or endorsed by BanG Dream!, Bushiroad,
Craft Egg, bilibili or the game's operators. Automation may violate the game's terms of service and
can get an account restricted or banned — use at your own risk, never on your main account and never
in ranked or competitive contexts. MIT licensed.
