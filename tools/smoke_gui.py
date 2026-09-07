# -*- coding: utf-8 -*-
"""桌面界面冒烟测试（离屏执行，不需要真正弹出窗口）。

用法（在项目根目录执行，需带 PySide6 的 venv）::

    set QT_QPA_PLATFORM=offscreen
    python tools/smoke_gui.py

依次验证：窗口构建 → 重复检测 → 画像统计 → 明细检索 → 重复清单导出。
任何一步抛错都会以非零退出码结束。
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication  # noqa: E402

from nfo_profiler.gui import MainWindow  # noqa: E402


def wait_workers(win: MainWindow, app: QApplication, timeout: float = 120.0) -> None:
    """等待所有后台线程结束（期间持续处理事件队列）。"""
    deadline = time.time() + timeout
    while win._workers and time.time() < deadline:
        app.processEvents()
        time.sleep(0.05)
    app.processEvents()


def main() -> int:
    app = QApplication([])
    win = MainWindow(db_path=os.path.join("output", "nfo.db"))
    print(f"[1] 窗口构建 OK：{win.windowTitle()}，数据源 {win.src_table.rowCount()} 个")

    # --- 重复检测 ---
    win.cmb_dup_conf.setCurrentText("低")
    win.run_dedupe()
    wait_workers(win, app)
    rep = win.dup_report
    assert rep is not None, "重复检测没有产出结果"
    s = rep.summary()
    print(f"[2] 重复检测 OK：{s['dup_groups']} 组 / 冗余 {s['redundant_copies']} 份 / "
          f"可回收 {s['redundant_text']} / 分片 {s['multipart_groups']} 组已排除 "
          f"({s['elapsed']}s)")
    print(f"    树节点：顶层 {win.dup_tree.topLevelItemCount()} 个，"
          f"首组子行 {win.dup_tree.topLevelItem(0).childCount() if win.dup_tree.topLevelItemCount() else 0} 个")
    print(f"    汇总文案：{win.lbl_dup_summary.text()[:80]}…")

    # --- 重复清单导出 ---
    out = os.path.join("output", "_smoke_dedupe")
    from nfo_profiler.dedupe import export_all as dup_export
    files = dup_export(rep, out, formats=("csv", "json"))
    for k, v in files.items():
        assert os.path.exists(v), f"导出文件不存在：{v}"
        print(f"[3] 导出 {k} OK：{v}（{os.path.getsize(v)} 字节）")

    # --- 画像统计 ---
    win.cmb_ov_source.setCurrentIndex(0)
    win.run_overview()
    wait_workers(win, app)
    ov = (win.last_data or {}).get("overview", {})
    print(f"[4] 画像统计 OK：作品 {ov.get('total', 0):,} 部 / 容量 "
          f"{ov.get('total_tb', 0)} TB / 维度表 {win.dim_table.rowCount()} 行")

    # --- 明细检索 ---
    win.ed_search.setText("DDK")
    win.search_movies(0)
    wait_workers(win, app)
    print(f"[5] 明细检索 OK：{win.lbl_detail.text()}，表格 {win.detail_table.rowCount()} 行")

    print("\n全部冒烟检查通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
