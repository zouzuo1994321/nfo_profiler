# -*- coding: utf-8 -*-
"""复现：④作品推荐 →「🕘 浏览记录」里点 👍 后闪退。

用法::

    set QT_QPA_PLATFORM=offscreen
    python -u tools/repro_browse_vote.py

离屏下用 BrowseHistoryDialog 直接构造（不 exec，避免模态阻塞），
点击第 0 行的 👍 按钮，捕获 traceback。
"""
from __future__ import annotations

import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QMessageBox, QWidget)

from nfo_profiler.gui import BrowseHistoryDialog, MainWindow  # noqa: E402


def _defuse_modals() -> None:
    def _mk(name: str, ret):
        def _stub(*args, **kwargs):
            txt = args[2] if len(args) >= 3 else (kwargs.get("text") or "")
            print(f"    (QMessageBox.{name}: {str(txt)[:160].splitlines()[0]})")
            return ret
        return _stub

    QMessageBox.critical = staticmethod(_mk("critical", QMessageBox.StandardButton.Ok))
    QMessageBox.warning = staticmethod(_mk("warning", QMessageBox.StandardButton.Ok))
    QMessageBox.information = staticmethod(_mk("information", QMessageBox.StandardButton.Ok))
    QMessageBox.question = staticmethod(_mk("question", QMessageBox.StandardButton.Yes))


def main() -> int:
    app = QApplication([])
    _defuse_modals()

    def _hook(etype, exc, tb):
        print("\n!!!! 未捕获异常（会导致 PySide6 6.11 直接 abort = 闪退）:")
        traceback.print_exception(etype, exc, tb)
    sys.excepthook = _hook

    win = MainWindow(db_path=os.path.join("output", "nfo.db"))
    win.resize(1400, 900)
    win.show()
    print("[1] 主窗口 OK")

    # 确保浏览记录非空
    rows = win.store.play_records()
    if not rows:
        r = win.store.conn.execute(
            "SELECT id, path FROM movies WHERE path<>'' LIMIT 1").fetchone()
        win.store.record_play_by_path(r["path"])
        rows = win.store.play_records()
    print(f"[2] 浏览记录 {len(rows)} 条")

    dlg = BrowseHistoryDialog(win.store, win, on_changed=win._on_votes_changed)
    dlg.show()
    app.processEvents()
    assert dlg.table.rowCount() > 0, "浏览记录表应有行"
    cell: QWidget = dlg.table.cellWidget(0, 6)
    assert cell is not None, "第 7 列应有投票按钮容器"
    btns = cell.findChildren(QWidget)
    steps = []

    def _vote_btn(row: int, idx: int):
        cell = dlg.table.cellWidget(row, 6)
        if cell is None:
            return None
        btns = [b for b in cell.findChildren(QWidget)
                if b.__class__.__name__ == "QPushButton"]
        return btns[idx] if len(btns) > idx else None

    def _step1() -> None:
        up = _vote_btn(0, 0)
        print(f"[3] 点击第 0 行 👍（{up.text() if up else '?'}）…")
        up.click()
        steps.append(1)

    def _step2() -> None:
        dn = _vote_btn(0, 1)
        print(f"[4] 1.5s 后点击同行的 👎（{dn.text() if dn else '?'}，撤销/改投）…")
        dn.click()

    def _step3() -> None:
        up = _vote_btn(1, 0) if dlg.table.rowCount() > 1 else _vote_btn(0, 0)
        print("[5] 3.0s 后再点一次赞（叠加推荐刷新）…")
        if up:
            up.click()

    def _quit() -> None:
        print("[6] 5.0s 后关闭对话框并退出（观察退出时是否有 QThread 仍在运行）")
        dlg.close()
        win.close()          # 走 MainWindow.closeEvent（确认框被 defuse 成 Yes）
        app.quit()

    QTimer.singleShot(300, _step1)
    QTimer.singleShot(1800, _step2)
    QTimer.singleShot(3300, _step3)
    QTimer.singleShot(5000, _quit)
    rc = app.exec()
    print(f"\n[事件循环退出 rc={rc}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
