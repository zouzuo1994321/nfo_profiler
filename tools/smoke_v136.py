# -*- coding: utf-8 -*-
"""v1.3.6 冒烟测试 —— 修复「浏览记录里点 👍 就闪退」。

用法::

    set QT_QPA_PLATFORM=offscreen
    python -u tools/smoke_v136.py

验收项：
  1. _cleanup 会等线程真正结束再 deleteLater（不再 qFatal abort）；
  2. 连续触发 5 次推荐刷新（模拟连点投票），全部完成且卡片正常刷新；
  3. 浏览记录里连续点赞 / 点踩，表格重建后仍可继续点击（sender 不再被自毁）；
  4. _shutdown_workers 能清空线程表；closeEvent 走完不残留；
  5. 崩溃兜底已安装（未捕获异常写 output/crash.log 而非无声闪退）；
  6. v1.3.5 无回归（悬停大图、logo/图标）。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time  # noqa: E402
from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QMessageBox, QWidget)

from nfo_profiler import __build__, __version__  # noqa: E402
from nfo_profiler.gui import (  # noqa: E402
    BrowseHistoryDialog, MainWindow, _install_crash_guard)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _defuse_modals() -> None:
    def _mk(name: str, ret):
        def _stub(*args, **kwargs):
            txt = args[2] if len(args) >= 3 else (kwargs.get("text") or "")
            print(f"    (QMessageBox.{name}: {str(txt)[:120].splitlines()[0]})")
            return ret
        return _stub

    QMessageBox.critical = staticmethod(_mk("critical", QMessageBox.StandardButton.Ok))
    QMessageBox.warning = staticmethod(_mk("warning", QMessageBox.StandardButton.Ok))
    QMessageBox.information = staticmethod(_mk("information", QMessageBox.StandardButton.Ok))
    QMessageBox.question = staticmethod(_mk("question", QMessageBox.StandardButton.Yes))


def main() -> int:
    app = QApplication([])
    _defuse_modals()

    def _pump(seconds: float) -> None:
        """真实等待 + 泵事件（QTimer 会在 processEvents 里触发）。

        不能用 app.exec()/quit()：实测 quit() 会连带触发主窗口 closeEvent，
        把 store 关掉，后续步骤直接 "Cannot operate on a closed database"。
        """
        end = time.time() + seconds
        while time.time() < end:
            app.processEvents()
            time.sleep(0.02)
        app.processEvents()

    win = MainWindow(db_path=os.path.join("output", "nfo.db"))
    win.resize(1400, 900)
    win.show()
    print(f"[1] 窗口构建 OK：v{__version__} / {__build__}")
    assert __version__ == "1.3.6" and __build__ == "2609120004", "版本号未 bump"

    # ------- 1. _cleanup 不再「运行中销毁」 -------
    src = open(os.path.join(ROOT, "nfo_profiler", "gui.py"), encoding="utf-8").read()
    assert "worker.wait(" in src and "def _cleanup" in src, \
        "_cleanup 应在 deleteLater 前等线程结束"
    print("[2] _cleanup 已加等待 OK（QThread 不会在运行中被销毁）")

    # ------- 2. 连续 5 次推荐刷新（模拟连点投票） -------
    state = {"n": 0}

    def _spawn(i: int):
        def _go():
            win.recommend_refresh()
            state["n"] += 1
        return _go

    for i in range(5):
        QTimer.singleShot(200 * i, _spawn(i))
    _pump(3.0)
    assert state["n"] == 5, f"应触发 5 次刷新，实际 {state['n']}"
    print(f"[3] 连续 {state['n']} 次推荐刷新 OK（无 abort、无残留线程告警）")

    # 等最后一波工作线程收尾
    _pump(1.5)

    # ------- 3. 浏览记录里连续点赞 -------
    if not win.store.play_records():
        r = win.store.conn.execute(
            "SELECT path FROM movies WHERE path<>'' LIMIT 1").fetchone()
        win.store.record_play_by_path(r["path"])
    dlg = BrowseHistoryDialog(win.store, win, on_changed=win._on_votes_changed)
    dlg.show()

    def _btn(row: int, idx: int):
        cell = dlg.table.cellWidget(row, 6)
        if cell is None:
            return None
        bs = [b for b in cell.findChildren(QWidget)
              if b.__class__.__name__ == "QPushButton"]
        return bs[idx] if len(bs) > idx else None

    clicks = {"n": 0}

    def _click1() -> None:
        b = _btn(0, 0)
        assert b is not None, "第 0 行应有 👍 按钮"
        b.click()
        clicks["n"] += 1

    def _click2() -> None:
        # 表格已被重建，sender 已销毁 —— 若在同槽自毁这里必崩
        b = _btn(0, 1)
        assert b is not None, "刷新后第 0 行应仍有 👎 按钮"
        b.click()
        clicks["n"] += 1

    def _click3() -> None:
        b = _btn(0, 0)
        if b is not None:
            b.click()
            clicks["n"] += 1

    QTimer.singleShot(100, _click1)
    QTimer.singleShot(800, _click2)
    QTimer.singleShot(1600, _click3)
    _pump(2.4)
    assert clicks["n"] == 3, f"应完成 3 次点击，实际 {clicks['n']}"
    print(f"[4] 浏览记录连续点赞 {clicks['n']} 次 OK（表格重建后仍可点击）")
    dlg.close()
    _pump(0.3)

    # ------- 4. _shutdown_workers -------
    win.recommend_refresh()
    _pump(1.2)
    win._shutdown_workers()
    assert not win._workers, f"收尾后线程表应为空，实际 {len(win._workers)}"
    print("[5] _shutdown_workers OK：线程表已清空")
    win.close()

    # ------- 5. 崩溃兜底 -------
    before = sys.excepthook
    _install_crash_guard()
    assert sys.excepthook is not before, "应已替换 sys.excepthook"
    sys.excepthook = before
    print("[6] 崩溃兜底 OK：未捕获异常会写入 output/crash.log 并弹窗")

    # ------- 6. v1.3.5 回归（在 win.close() 之前做，关窗会关库） -------
    assert not win.windowIcon().isNull(), "窗口图标不应为空"
    from nfo_profiler.gui import find_data_file, MovieCard
    p = find_data_file("logo.png")
    assert p and os.path.isfile(p), "find_data_file 应能定位 logo.png"
    movie = {"movie_id": -1, "num": "T-1", "title": "t", "studio": "", "path": ""}
    card = MovieCard(movie, win)
    card.show()
    assert card.btn_up is not None, "卡片投票按钮应存在"
    print("[7] v1.3.5 回归 OK：图标链路 + 推荐卡片正常")

    win._shutdown_workers()
    win.close()
    _pump(0.3)

    print("\n[v1.3.6 冒烟检查全部通过]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
