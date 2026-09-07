# -*- coding: utf-8 -*-
"""v1.1.5 桌面界面冒烟测试 —— 修复筛选 SQL 别名 / 表头排序 / 双击图片预览。

用法：

    set QT_QPA_PLATFORM=offscreen
    python tools/smoke_v115.py

验收项：
  1. ③ 作品明细：在「标签 / 演员」输入关键词后点「应用筛选」不爆
     `OperationalError: no such column: m.id`；
  2. ③ 作品明细：点击表头（番号 / 标题 / 年份等）能切换排序，SQL 白名单安全；
  3. ③ 作品明细：双击行能同时打开文件夹并调用图片预览方法（找不到图时安全返回）；
  4. 旧流程（画像概览 / AI 聚类 / 相似推荐）不破坏。
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from nfo_profiler.gui import MainWindow  # noqa: E402


def wait_workers(win: MainWindow, app: QApplication, timeout: float = 180.0) -> None:
    deadline = time.time() + timeout
    while win._workers and time.time() < deadline:
        app.processEvents()
        time.sleep(0.1)
    app.processEvents()


def main() -> int:
    app = QApplication([])
    win = MainWindow(db_path=os.path.join("output", "nfo.db"))
    win.show()
    print(f"[1] 窗口构建 OK：{win.windowTitle()}，"
          f"{win.lbl_scan_stat.text()}")

    # --- 1. 作品明细：带 tag_actor 的筛选不报错 ---
    win.ed_tag_actor.setText("中出")
    win.search_movies(0)
    wait_workers(win, app)
    rows = win.detail_table.rowCount()
    print(f"[2] 标签/演员关键词筛选 OK：{rows} 行，"
          f"{win.lbl_detail.text()[:80]}")
    assert rows > 0, "筛选结果不应为空"
    # 清掉
    win.ed_tag_actor.setText("")

    # --- 2. 作品明细：表头排序 ---
    # 默认排序状态 -1；点击 标题 列（col=3）
    assert win.detail_sort_col == -1
    win._on_detail_header_clicked(3)
    app.processEvents()
    assert win.detail_sort_col == 3 and win.detail_sort_asc is True, \
        "点击标题列后排序列应为 3 且升序"
    # 再点一次切降序
    win._on_detail_header_clicked(3)
    assert win.detail_sort_asc is False, "再次点击同列应收降序"
    print("[3] 表头排序 OK：标题列 升→降")
    # 点一个不支持的列（如 col=11 标签）不应改状态
    old_col, old_asc = win.detail_sort_col, win.detail_sort_asc
    win._on_detail_header_clicked(11)
    assert win.detail_sort_col == old_col and win.detail_sort_asc == old_asc, \
        "不支持排序的列不应影响当前排序"
    print("[4] 排序白名单 OK：不支持列被忽略")

    # --- 3. 双击行打开文件夹 + 图片预览（健壮性） ---
    win.detail_table.selectRow(0)
    app.processEvents()
    # 取第 0 行首列 UserRole 的 path
    first = win.detail_table.item(0, 0)
    assert first is not None, "表格至少应有一行"
    path = first.data(Qt.ItemDataRole.UserRole) or ""
    print(f"    首行 path：{path[:60]}…")
    # 直接调用预览方法（找不到图也应安全返回）
    win._show_detail_image_preview(path)
    print("[5] 图片预览方法 OK（找不到对应 jpg 也能安全返回）")
    # 双击信号槽本身
    win._on_detail_double_clicked(first)
    print("[6] 双击行 OK：触发打开文件夹 + 图片预览")

    # --- 4. 回归：画像概览 与 AI 聚类 ---
    win.run_overview()
    wait_workers(win, app)
    ov = (win.last_data or {}).get("overview", {})
    print(f"[7] 回归 · 画像概览：{ov.get('total', 0):,} 部作品 / "
          f"标题词数 {ov.get('distinct', {}).get('title_terms', 0):,}")
    win.ai_clusters()
    wait_workers(win, app)
    print(f"[8] 回归 · 综合主题聚类：{win.ai_clu_tree.topLevelItemCount()} 个主题")

    print("\n[v1.1.5 冒烟检查全部通过]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
