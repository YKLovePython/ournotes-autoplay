"""独立跑几何核对（等价于把截图文件夹拖到 谛听.exe 上）。需要系统 Python + 依赖。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _cand in (ROOT / "src", ROOT / "_internal", Path(__file__).resolve().parents[2] / "src"):
    if _cand.exists():
        sys.path.insert(0, str(_cand))

from bandori_autoplay.verify import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
