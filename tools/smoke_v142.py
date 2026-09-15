# -*- coding: utf-8 -*-
"""v1.4.2 离屏冒烟测试（QT_QPA_PLATFORM=offscreen，无真实显示）。

覆盖：
  1. 版本号 bump（__version__=1.4.2 / __build__=2609160001）；
  2. MovieCard 粉色流光选中高光：选中启动动画（边框变粉色、外发光开启、定时器运行），
     相位推进颜色流动（_tick_glow 后 QSS 变化），取消选中熄灭（定时器停、恢复基础样式）；
  3. 全局 QSS 已含 `qproperty-drawBase:0`（去除主导航 TabBar 白色基线）。

运行：QT_QPA_PLATFORM=offscreen PYTHONPATH=<项目根> python tools/smoke_v142.py
"""
import os
import sys
import re
import tempfile
import shutil
from datetime import datetime

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from nfo_profiler import __version__, __build__  # noqa: E402
from nfo_profiler import gui as g  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ✅ {name}")
    else:
        print(f"  ❌ {name}  {detail}")
        FAILS.append(name)


class _DummyWin:
    """MovieCard 只在找图时用到 win.find_movie_image。"""

    @staticmethod
    def find_movie_image(_path):
        return ""


def border_color(qss: str):
    m = re.search(r"border:3px solid (#[0-9a-fA-F]{6})", qss)
    return m.group(1).upper() if m else None


def main():
    print(f"=== NFO 画像矿工 v1.4.2 冒烟测试 @ {datetime.now():%Y-%m-%d %H:%M:%S} ===")

    print("[1] 版本号")
    check("__version__ == 1.4.2", __version__ == "1.4.2", f"实际 {__version__}")
    check("__build__ == 2609160001", __build__ == "2609160001", f"实际 {__build__}")

    print("[2] QSS 白线修复（QTabBar drawBase 关闭）")
    check("QSS 含 qproperty-drawBase:0",
          "qproperty-drawBase:0" in g.QSS.replace(" ", "").replace("QTabBar{", "QTabBar {")
          or "qproperty-drawBase:0" in g.QSS)

    app = QApplication.instance() or QApplication([])

    print("[3] MovieCard 粉色流光高光")
    tmp = tempfile.mkdtemp(prefix="smoke142_")
    try:
        movie = {"movie_id": 1, "num": "TEST-001", "title": "测试作品",
                 "path": os.path.join(tmp, "x.nfo"), "studio": "片商X",
                 "actors": "演员A、演员B", "score": 0.0}
        card = g.MovieCard(movie, _DummyWin())

        check("副标题为演员名", card._sub_text == "演员A、演员B",
              f"实际 {card._sub_text!r}")

        card.set_selected(True)
        c0 = border_color(card.styleSheet())
        check("选中即进入粉色流光（3px 粉色边框）",
              card._glow_timer.isActive() and c0 is not None
              and c0 in {c.upper() for c in card._GLOW_COLORS},
              f"timer={card._glow_timer.isActive()} color={c0}")
        check("选中即有外发光（blurRadius>0）", card._glow_effect.blurRadius() > 0,
              f"实际 {card._glow_effect.blurRadius()}")

        seen = set()
        for _ in range(40):
            card._tick_glow()
            c = border_color(card.styleSheet())
            if c:
                seen.add(c)
        check("流光颜色随相位流动（多帧出现不同颜色）", len(seen) >= 3,
              f"仅 {len(seen)} 种: {sorted(seen)}")

        card.set_selected(False)
        check("取消选中熄灭流光（定时器停 + blurRadius=0 + 基础样式）",
              not card._glow_timer.isActive()
              and card._glow_effect.blurRadius() == 0
              and card.styleSheet() == card._BASE_QSS
              and card.property("selected") == "false")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILS:
        print(f"RESULT: FAILED ({len(FAILS)} 项) -> {FAILS}")
        sys.exit(1)
    print("RESULT: ALL PASSED ✅")


if __name__ == "__main__":
    main()
