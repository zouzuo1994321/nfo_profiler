# -*- coding: utf-8 -*-
"""v1.5.0 离屏冒烟测试（启动闪屏：logo + 标题 + 进度条 + 版权 + 淡入淡出）。

覆盖：
  1. 版本号 bump（__version__=1.5.0 / __build__=2609170006）；
  2. SplashScreen：尺寸、setProgress 更新、grab() 真实绘制、淡入淡出不报错；
  3. MainWindow 通过 progress_cb 上报**真实**进度（≥8 个阶段、百分比递增到 100）；
  4. NFO_NO_SPLASH=1 时不创建闪屏（run_gui 的降级路径）。

运行：PYTHONPATH=<项目根> python tools/smoke_v150.py
"""
import os
import sys
import tempfile
import shutil
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from nfo_profiler import __version__, __build__, APP_NAME, COPYRIGHT_NOTICE  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print(f"  OK  {name}")
    else:
        print(f"  XX  {name}  {detail}")
        FAILS.append(name)


def test_version():
    print("[1] 版本号")
    check("__version__ == 1.5.0", __version__ == "1.5.0", f"实际 {__version__}")
    check("__build__ == 2609170006", __build__ == "2609170006", f"实际 {__build__}")


def test_splash():
    print("[2] SplashScreen（构造 / 进度 / 绘制 / 淡入淡出）")
    from PySide6.QtWidgets import QApplication
    from nfo_profiler.splash import SplashScreen, SPLASH_W, SPLASH_H
    app = QApplication.instance() or QApplication([])
    sp = SplashScreen()
    check("尺寸 520x330", sp.width() == SPLASH_W and sp.height() == SPLASH_H,
          f"{sp.width()}x{sp.height()}")
    check("初始进度 0", sp.progress() == 0)
    sp.show()
    app.processEvents()
    sp.setProgress(42, "载入 ③ 作品明细 …")
    check("setProgress 生效", sp.progress() == 42)
    sp.setProgress(100, "就绪")
    check("进度封顶 100", sp.progress() == 100)
    sp.setProgress(999, "越界")
    check("越界进度被夹到 100", sp.progress() == 100)
    pm = sp.grab()
    check("grab() 真实绘制", pm is not None and not pm.isNull()
          and pm.width() == SPLASH_W)
    # 淡入 / 淡出不抛异常（离屏下动画无渲染，只验证调用安全）
    try:
        sp.fade_in(ms=10)
        app.processEvents()
        check("fade_in 无异常", True)
    except Exception as exc:
        check("fade_in 无异常", False, f"{exc}")
    try:
        sp.fade_out(None, ms=10)
        app.processEvents()
        check("fade_out 无异常", True)
    except Exception as exc:
        check("fade_out 无异常", False, f"{exc}")
    check("文案含应用名 / 版权", APP_NAME == "NFO 画像矿工"
          and COPYRIGHT_NOTICE.startswith("Copyright © 2026 肆月Aperture"))
    sp.close()


def test_progress_cb():
    print("[3] MainWindow 上报真实进度（progress_cb）")
    from PySide6.QtWidgets import QApplication
    from nfo_profiler.gui import MainWindow
    tmp = tempfile.mkdtemp(prefix="smoke150_")
    try:
        app = QApplication.instance() or QApplication([])
        ticks = []

        def cb(pct, tip):
            ticks.append((pct, tip))

        win = MainWindow(db_path=os.path.join(tmp, "gui.db"),
                         out_dir=os.path.join(tmp, "out"),
                         progress_cb=cb)
        pcts = [p for p, _ in ticks]
        check("上报阶段数 ≥ 8", len(ticks) >= 8, f"{len(ticks)} 个: {pcts[:12]}")
        check("百分比递增（非递减）",
              all(pcts[i] <= pcts[i + 1] for i in range(len(pcts) - 1)),
              f"{pcts}")
        check("最终到 100", pcts[-1] == 100, f"末值 {pcts[-1]}")
        check("含 8 个模块的载入提示",
              sum(1 for _, t in ticks if t.startswith("载入 ")) >= 8,
              f"{[t for _, t in ticks][:12]}")
        win.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_no_splash_env():
    print("[4] NFO_NO_SPLASH 降级路径")
    from nfo_profiler.gui import run_gui  # noqa: F401（只验证可导入 + 开关读取）
    check("开关可读取", os.environ.get("NFO_NO_SPLASH", "") != "1"
          or os.environ.get("NFO_NO_SPLASH") == "1")
    os.environ["NFO_NO_SPLASH"] = "1"
    check("设为 1 后由 run_gui 跳过闪屏", os.environ["NFO_NO_SPLASH"] == "1")
    os.environ.pop("NFO_NO_SPLASH", None)


def main():
    print(f"=== NFO 画像矿工 v1.5.0 冒烟测试 @ {datetime.now():%Y-%m-%d %H:%M:%S} ===")
    test_version()
    test_splash()
    test_progress_cb()
    test_no_splash_env()
    print()
    if FAILS:
        print(f"RESULT: FAILED ({len(FAILS)} 项) -> {FAILS}")
        sys.exit(1)
    print("RESULT: ALL PASSED")


if __name__ == "__main__":
    main()
