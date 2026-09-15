# -*- coding: utf-8 -*-
"""v1.4.3 离屏冒烟测试（QT_QPA_PLATFORM=offscreen，无真实显示）。

覆盖：
  1. 版本号 bump（__version__=1.4.3 / __build__=2609160002）；
  2. MovieCard 环绕流光选中高光（参照环形流光效果）：选中 → 中粉底色边框 + 恒定外发光 +
     定时器启动；_tick_glow 推进角度（流动而非闪烁）；card.grab() 真实触发
     paintEvent 的 conic 渐变描边（不抛异常）；取消选中熄灭恢复基础样式；
  3. 全局 QSS 已含 `qproperty-drawBase:0`（去除主导航 TabBar 白色基线）。

运行：QT_QPA_PLATFORM=offscreen PYTHONPATH=<项目根> python tools/smoke_v143.py
"""
import os
import sys
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


def main():
    print(f"=== NFO 画像矿工 v1.4.3 冒烟测试 @ {datetime.now():%Y-%m-%d %H:%M:%S} ===")

    print("[1] 版本号")
    check("__version__ == 1.4.3", __version__ == "1.4.3", f"实际 {__version__}")
    check("__build__ == 2609160002", __build__ == "2609160002", f"实际 {__build__}")

    print("[2] QSS 白线修复（QTabBar drawBase 关闭）")
    check("QSS 含 qproperty-drawBase:0", "qproperty-drawBase:0" in g.QSS)

    app = QApplication.instance() or QApplication([])

    print("[3] MovieCard 环绕流光（粉色 · 慢速 · 不闪烁）")
    tmp = tempfile.mkdtemp(prefix="smoke143_")
    try:
        movie = {"movie_id": 1, "num": "TEST-001", "title": "测试作品",
                 "path": os.path.join(tmp, "x.nfo"), "studio": "片商X",
                 "actors": "演员A、演员B", "score": 0.0}
        card = g.MovieCard(movie, _DummyWin())
        check("副标题为演员名", card._sub_text == "演员A、演员B",
              f"实际 {card._sub_text!r}")

        card.set_selected(True)
        check("选中 → 定时器启动", card._glow_timer.isActive())
        check("选中 → 中粉底色边框（2px _GLOW_BASE，流光叠于其上）",
              card._GLOW_BASE in card.styleSheet())
        check("选中 → 恒定外发光（blurRadius=22，不脉动）",
              card._glow_effect.blurRadius() == 22,
              f"实际 {card._glow_effect.blurRadius()}")

        a0 = card._glow_angle
        for _ in range(30):
            card._tick_glow()
        check("流光角度随帧推进（环绕流动而非整圈变色）",
              card._glow_angle > a0,
              f"{a0} → {card._glow_angle}")
        check("推进后外发光仍恒定（blurRadius 不变=不闪烁）",
              card._glow_effect.blurRadius() == 22)

        pm = card.grab()   # 真实触发 paintEvent 的 conic 渐变描边
        check("paintEvent conic 描边渲染成功（grab 无异常且非空）",
              pm is not None and not pm.isNull() and pm.width() > 0)

        card.set_selected(False)
        check("取消选中熄灭（定时器停 + blurRadius=0 + 基础样式）",
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
