# OurNotes Autoplay — 内部技术档案（私有）

> 这个仓库是**私有**的，用于保存实现与时间证据。
> 公开出去的是另一份「效果与思路」仓库，**不包含**这里的代码。

**English abstract** — Private archive of a human-in-the-loop autoplay for *BanG Dream!
Our Notes* on an unrooted Android phone: a player taps the first note, the tool takes over
from the second using the game's own chart data. This repository holds the implementation,
the measurements and the development timeline; it exists so the work has a dated,
tamper-evident record.

---

## 这是什么

《BanG Dream! Our Notes》的自动演奏实现，跑在**未 root** 的小米 2510DRK44C（Android 16）
上，只用普通 ADB。

**人机协同**：你选歌 → 脚本点 LIVE START → 演奏画面出来后**你按第 1 个音符** →
脚本取那一下的**内核时间戳**当基准，从**第 2 个音符**起把整首打完。

## 目录

```
src/bandori_autoplay/     实现（27 个模块）
tools/human_play.py       入口：选歌 → 点开始 → 等第一下 → 从第二下接管
tools/live_start_template.png   认「乐队确认页」的按钮模板
tools/scrcpy/scrcpy-server-v4.1 UHID 虚拟触摸屏的服务端
config/default.yaml       设备尺寸、判定线、触控后端等
samples/                  一个示例谱面 + 全量索引 + 曲名表（全量谱面另取，见 docs）
docs/01_独特思路.md        这个项目独一无二的四点
docs/02_技术细节.md        协议、坐标、时间轴、踩过的坑
docs/03_验证记录.md        实机数据与离线回归
docs/04_时间线.md          开发时间线（时间证据）
```

## 怎么跑

```
装依赖：  pip install -r requirements.txt
连手机：  adb 打开 USB 调试；把手机停在目标歌曲的「乐队确认」页
开跑：    双击 人手起手.bat   →  回车（默认上次那首）→ 演奏画面出来后按第 1 个音符
```

日志写在 `logs/human_play_日期_时间.log`，会记下：往返延迟标定值、锚点、
从第几个音符接管、手势构成、有没有动作迟到。

## 独一无二的四点（详见 docs/01）

1. **锚点 = 玩家自己的落指内核时间戳**（不是猜前摇，也不是画面检测）
2. **非 root 的 UHID 虚拟触摸屏注入**（系统认成真实 HID 数字化仪）
3. **谱面驱动**（画面只用来认确认页；不靠视频流识别音符）
4. **长条沿轨迹连续跟随 + 头/尾轻扫**（长条会横移，尾巴可能是甩出去的动作）

## 时间证据

见 [docs/04_时间线.md](docs/04_时间线.md)。要点：

* 工程目录建立于 **2026‑09‑28 23:44**（本机文件时间戳）；
* UHID 注入、谱面解码、几何标定在 **2026‑09‑29** 完成；
* 人机锚点方案 **2026‑09‑30 02:48** 写入代码，**03:16** 首次实机跑通（有日志）；
* 本仓库的第一次提交带有该时刻的 commit 时间。

`docs/04_时间线.md` 末尾附有本仓库关键文件的 **SHA‑256**，可用于核对之后是否被改动。

## 授权

保留所有权利（见 [LICENSE](LICENSE)）。**未授权转载或再分发**。
仅供内部存档与时间证明使用。
