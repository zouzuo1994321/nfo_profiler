# -*- coding: utf-8 -*-
"""v1.3.3 冒烟测试 —— 独立「清理失效记录」入口 + 扫描列表自动带出数据源。

用法：

    set QT_QPA_PLATFORM=offscreen
    python tools/smoke_v133.py --db D:/copy.db   # 务必用副本库

验收项：
  1. 扫描 Tab 新增「🧹 清理失效记录」按钮与 start_prune / _on_prune_done / _on_prune_error；
  2. 启动时已登记数据源自动进入扫描列表（不再空列表 → 点扫描不再撞「没有目录」）；
  3. PruneWorker 可构造、可请求终止（不真跑遍历，遍历交给 tools/verify_prune.py）；
  4. store.prune_missing 空集合保护（掉盘不清库）；
  5. v1.3.2 功能无回归（浏览记录按钮 / 统计标签纯文本）。

注意：离屏模式下 QMessageBox 会永久阻塞，这里统一替换为「打印 + 默认返回」。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from nfo_profiler.gui import MainWindow, PruneWorker  # noqa: E402


def _defuse_modals() -> None:
    def _mk(name: str, ret):
        def _stub(*args, **kwargs):
            txt = args[2] if len(args) >= 3 else (kwargs.get("text") or "")
            print(f"    (QMessageBox.{name}: {str(txt)[:160].splitlines()[0]})")
            return ret
        return _stub

    QMessageBox.critical = staticmethod(   # type: ignore[assignment]
        _mk("critical", QMessageBox.StandardButton.Ok))
    QMessageBox.warning = staticmethod(    # type: ignore[assignment]
        _mk("warning", QMessageBox.StandardButton.Ok))
    QMessageBox.information = staticmethod(  # type: ignore[assignment]
        _mk("information", QMessageBox.StandardButton.Ok))
    QMessageBox.question = staticmethod(   # type: ignore[assignment]
        _mk("question", QMessageBox.StandardButton.Yes))


def main() -> int:
    db = os.path.join("output", "nfo.db")
    if "--db" in sys.argv:
        db = sys.argv[sys.argv.index("--db") + 1]

    app = QApplication([])
    _defuse_modals()
    win = MainWindow(db_path=db)
    win.resize(1500, 950)
    win.show()
    win.find_movie_image = lambda p: None  # type: ignore[method-assign]
    print(f"[1] 窗口构建 OK：{win.windowTitle()}，db={db}")

    # ------- 1. 清理按钮与回调 -------
    assert hasattr(win, "btn_prune"), "扫描 Tab 应有「🧹 清理失效记录」按钮"
    assert "清理失效记录" in win.btn_prune.text(), f"按钮文案异常：{win.btn_prune.text()}"
    for m in ("start_prune", "_on_prune_done", "_on_prune_error"):
        assert hasattr(win, m) and callable(getattr(win, m)), f"应实现 {m}()"
    print(f"[2] 清理入口 OK：按钮「{win.btn_prune.text()}」+ 3 个回调")

    # ------- 2. 扫描列表自动带出已登记数据源 -------
    n_dirs = win.dir_list.count()
    n_src = len(win.store.list_sources())
    assert n_dirs >= 1, "启动时扫描列表不应为空（应自动带出已登记数据源）"
    assert n_dirs == n_src, f"扫描列表应等于数据源数：{n_dirs} vs {n_src}"
    print(f"[3] 扫描列表自动填充 OK：{n_dirs} 个数据源已就位（可直接点开始扫描）")

    # ------- 3. PruneWorker 可构造 / 可终止 -------
    worker = PruneWorker(win.store, [win.dir_list.item(0).text()])
    assert not worker._stop
    worker.stop()
    assert worker._stop, "PruneWorker 应能请求终止"
    print("[4] PruneWorker OK：可构造、可终止（遍历逻辑见 tools/verify_prune.py）")

    # ------- 4. 空集合保护 -------
    root = win.dir_list.item(0).text()
    before = win.store.count_movies()
    assert win.store.prune_missing(root, set()) == 0, "空集合不得删任何记录"
    assert win.store.count_movies() == before, "空集合保护失效，记录被误删"
    print(f"[5] 掉盘保护 OK：existing 为空时 0 删除（库内仍 {before:,} 条）")

    # ------- 5. v1.3.2 无回归 -------
    assert win.btn_rec_history.text() == "🕘 浏览记录", "浏览记录按钮应保留"
    assert "查看记录" not in win.lbl_rec_stat.text(), "查看记录入口应已删除"
    assert win.chk_prune.isChecked() and not win.chk_prune.isEnabled(), \
        "增量开启时清理开关仍应禁用"
    win._on_incremental_toggled(False)
    assert win.chk_prune.isEnabled() and win.chk_prune.isChecked(), \
        "关闭增量后应自动勾选并解锁"
    print("[6] v1.3.2 功能无回归 OK：浏览记录在、查看记录已删、增量联动正常")

    print("\n[v1.3.3 冒烟检查全部通过]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
