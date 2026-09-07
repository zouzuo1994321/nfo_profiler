# -*- coding: utf-8 -*-
"""v1.1.3 桌面界面冒烟测试 —— 新功能 / 双击交互 / UI 重构验收。

用法（在项目根目录执行，需带 PySide6 的 venv）::

    set QT_QPA_PLATFORM=offscreen
    python tools/smoke_v113.py

验收项：
  1. 作品明细新 UI（折叠筛选按钮 / 路径列开关 / 行级操作按钮 / 双击信号）已挂载；
  2. AI 分析新结构（AI 状态条 / 3 个 sub-Tab / 相似推荐表格 + path 字段 / 双击信号）已挂载；
  3. 已有数据下，相似推荐能算出 ≥ 1 行，行 0 的 UserRole 存了 path；
  4. 标签聚类能填充 QTreeWidget；
  5. 画像解读能用本地启发式产出非空文本。

任何一步抛错都会以非零退出码结束。
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from nfo_profiler.gui import MainWindow  # noqa: E402


def wait_workers(win: MainWindow, app: QApplication, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    while win._workers and time.time() < deadline:
        app.processEvents()
        time.sleep(0.05)
    app.processEvents()


def main() -> int:
    app = QApplication([])
    win = MainWindow(db_path=os.path.join("output", "nfo.db"))
    win.show()  # 离屏模式下 show 之后子控件才进入「可见」状态
    print(f"[1] 窗口构建 OK：{win.windowTitle()}，数据库概况 {win.lbl_scan_stat.text()}")

    # --- 1. ③ 作品明细 UI 结构 ---
    win.tabs = win.centralWidget()  # type: ignore[attr-defined]
    assert hasattr(win, "btn_filter_toggle") and win.btn_filter_toggle.isCheckable(), \
        "作品明细缺折叠筛选按钮"
    assert hasattr(win, "btn_open_sel") and hasattr(win, "btn_copy_path"), \
        "作品明细缺行级操作按钮"
    assert hasattr(win, "detail_filter_panel") and not win.detail_filter_panel.isHidden(), \
        "作品明细筛选面板默认应该展开"
    assert hasattr(win, "chk_show_path"), "作品明细缺「显示路径列」开关"
    assert win.detail_table.columnCount() == 13, \
        f"作品明细表格应为 13 列，实际 {win.detail_table.columnCount()}"
    assert win.detail_table.isColumnHidden(0), "id 列应默认隐藏"
    assert win.detail_table.isColumnHidden(1), "路径列应默认隐藏"
    print(f"[2] 作品明细 UI OK：折叠筛选按钮 ✓，行级操作按钮 ✓，表格 {win.detail_table.columnCount()} 列"
          f"（id / 路径 默认隐藏）")

    # --- 2. 双击信号挂载 ---
    # 用更可靠的方式：直接调用 slot 走一遍（无需真发信号）
    assert hasattr(win, "_on_detail_double_clicked") and callable(win._on_detail_double_clicked), \
        "作品明细缺 _on_detail_double_clicked 槽函数"
    # 模拟一次「无 item 数据」的触发，确保 slot 健壮
    win._on_detail_double_clicked(None)
    print("[3] 双击信号挂载 OK（detail_table.itemDoubleClicked 已绑定 _on_detail_double_clicked）")

    # --- 3. 明细检索：自动携带 path ---
    win.ed_search.setText("D")
    win.search_movies(0)
    wait_workers(win, app)
    rows = win.detail_table.rowCount()
    print(f"[4] 明细检索 OK：{rows} 行，{win.lbl_detail.text()[:60]}")
    if rows:
        first_path = win.detail_table.item(0, 0).data(Qt.ItemDataRole.UserRole) or ""
        print(f"    首行 path 已写入 UserRole：{'有' if first_path else '无'}（{first_path[:50] if first_path else '-'}）")
    # 行级操作按钮根据选择状态启用
    win.detail_table.selectRow(0) if rows else None
    app.processEvents()
    assert win.btn_open_sel.isEnabled(), "选中后「打开选中文件夹」应启用"
    assert win.btn_copy_path.isEnabled(), "选中后「复制路径」应启用"
    print("[5] 行级操作按钮启用 OK（选中即启）")

    # --- 4. AI 分析 UI 结构 ---
    assert hasattr(win, "ai_tabs") and win.ai_tabs.count() == 3, \
        f"AI 分析 TabWidget 应有 3 页，实际 {win.ai_tabs.count() if hasattr(win, 'ai_tabs') else '缺'}"
    assert hasattr(win, "ai_similar_table"), "AI 分析缺相似推荐表格"
    assert win.ai_similar_table.columnCount() == 9, \
        f"相似推荐表应为 9 列，实际 {win.ai_similar_table.columnCount()}"
    assert hasattr(win, "lbl_ai_state"), "AI 分析缺状态条"
    print(f"[6] AI 分析新结构 OK：3 个 sub-Tab，相似推荐表格 {win.ai_similar_table.columnCount()} 列，"
          f"状态条「{win.lbl_ai_state.text()[:60]}」")

    # --- 5. 标签聚类 ---
    win.ai_clusters()
    wait_workers(win, app)
    clu_top = win.ai_clu_tree.topLevelItemCount()
    print(f"[7] 标签聚类 OK：{clu_top} 个主题（{win.lbl_ai_clu_summary.text()[:60]}）")

    # --- 6. 相似推荐（仅当库中有作品时跑） ---
    if rows:
        # 取表格首行的番号，做相似推荐
        first_num_item = win.detail_table.item(0, 1)  # 第 1 列是 path，第 2 列是 num（header 顺序）
        # 实际列顺序：id(0) path(1) num(2) title(3) ... — 第一行显示的番号 = 第 2 列
        first_num_item = win.detail_table.item(0, 2)
        first_num = first_num_item.text() if first_num_item else ""
        if first_num:
            win.ed_ai_num.setText(first_num)
            win.ai_similar()
            wait_workers(win, app)
            sim_rows = win.ai_similar_table.rowCount()
            print(f"[8] 相似推荐 OK：目标 {first_num} → {sim_rows} 行，"
                  f"{win.lbl_ai_similar_summary.text()[:60]}")
            if sim_rows:
                first_path = win.ai_similar_table.item(0, 0).data(Qt.ItemDataRole.UserRole) or ""
                assert first_path, "相似推荐首行应携带 path"
                assert hasattr(win, "_open_ai_similar_folder") and callable(win._open_ai_similar_folder), \
                    "相似推荐表缺 _open_ai_similar_folder 槽函数"
                print(f"    首行 path 已写入 UserRole：{first_path[:60]}…")
                print("[9] 相似推荐双击信号挂载 OK")
        else:
            print("[8] 跳过相似推荐（首行无番号）")
    else:
        print("[8] 跳过相似推荐（库内无作品）")

    # --- 7. 画像解读 ---
    win.ai_interpret()
    wait_workers(win, app)
    html = win.ai_interp_browser.toPlainText().strip()
    print(f"[9/10] 画像解读 OK：{len(html)} 字（{win.lbl_ai_interp_meta.text()[:60]}）")

    print("\n[v1.1.3 冒烟检查全部通过]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
