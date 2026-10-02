"""受托管启动本地 CNS 地图后端（不打开浏览器），供开发期验收使用。

等价于 ``map_app.py --no-browser``：先确认 8765 空闲或身份一致，再启动当前 HEAD。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import map_app  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(map_app.main())
