"""打包成便携版：``dist/谛听/``（一个 exe + _internal + 数据/配置目录）。

用 Python 写而不是全写在 bat 里，是为了避开 cmd 的编码问题：命令行参数
（含中文的 exe 名）在这里是真正的 unicode。

发布形态（用户只看得到 exe 和几个文件夹）：
    谛听.exe    _internal\    config\    geometry\    work\    platform-tools\
模板与 scrcpy-server 打包进 _internal\tools\，运行时会自己找到。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = "谛听"
DIST = ROOT / "dist"
STALE = ("tools", "src", "shots", "docs",
         "使用说明.txt", "运行.bat", "自检.bat", "打开日志文件夹.bat", "核对几何.bat",
         "README.md")


def main() -> int:
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--console",
           "--name", NAME, "--paths", str(ROOT / "src"),
           "--distpath", str(DIST), "--workpath", str(ROOT / "build"),
           "--specpath", str(ROOT / "build"),
           "--add-data", f"{ROOT / 'tools' / 'live_start_template.png'}{os.pathsep}tools",
           "--add-data",
           f"{ROOT / 'tools' / 'scrcpy' / 'scrcpy-server-v4.1'}{os.pathsep}tools/scrcpy",
           str(ROOT / "tools" / "human_play.py")]
    print("$", " ".join(cmd))
    if subprocess.run(cmd).returncode != 0:
        print("打包失败")
        return 2

    out = DIST / NAME
    for folder in ("work", "geometry", "config"):
        src = ROOT / folder
        if src.exists():
            shutil.copytree(src, out / folder, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__"))
    pt = ROOT / "platform-tools"
    if not pt.exists():
        # 没在项目里放 platform-tools 时，用本机已知的那份（便携包里必须有 adb）
        for cand in (Path(r"C:\Users\67063\Desktop\_桌面整理_20260730\04_其他项目与代码\platform-tools"),
                     Path.home() / "AppData/Local/Android/Sdk/platform-tools"):
            if cand.exists():
                pt = cand
                break
    if pt.exists():
        shutil.copytree(pt, out / "platform-tools", dirs_exist_ok=True)
        print(f"已随包带上 adb：{pt}")
    else:
        print("⚠ 没找到 platform-tools，便携包里将没有 adb（用户得自己装）")
    for stale in STALE:                       # 清掉旧版本留下的、现在不需要的
        p = out / stale
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        elif p.exists():
            p.unlink()
    n = sum(1 for f in out.rglob("*") if f.is_file())
    total = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    print(f"\n完成：{out}（{n} 个文件，{total / 1048576:.1f} MB）")
    print("把这个文件夹整个拷到桌面或别的地方都能用。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
