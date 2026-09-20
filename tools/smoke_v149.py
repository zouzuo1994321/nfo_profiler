# -*- coding: utf-8 -*-
"""v1.4.9 离屏冒烟测试（向量编辑：紧凑行内按钮回归 + 排序后重建 + 显示完整性）。

背景：v1.4.8 为根除「显示不全」去掉全部行内控件、只留右键菜单，交互过于隐蔽。
v1.4.9 让**按钮回归**并优化 UI：

* 按钮紧凑样式（padding 1×6、高度 22、字号 12），数字列 92→76 收窄、操作列 124；
* 保留作品明细式原生排序；排序后 cell widget 不跟随移动 → 依据条目 UserRole
  **重建按钮**（`_vec_rebuild_buttons`），保证按钮 ↔ 行数据永远对应。

覆盖：
  1. 版本号 bump（__version__=1.4.9 / __build__=2609170005）；
  2. 行内按钮存在且紧凑（高度 22）；
  3. **显示完整性**：按钮整体宽度不超出操作列、右边缘不超出表格视口（显示不全的直接判据）；
  4. 排序后按钮与行数据对应（手动向量行有「删手动」、其余无）；
  5. 功能回归：屏蔽 / 删手动 / grab() 渲染。

运行：PYTHONPATH=<项目根> python tools/smoke_v149.py
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

from nfo_profiler import __version__, __build__  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print(f"  OK  {name}")
    else:
        print(f"  XX  {name}  {detail}")
        FAILS.append(name)


def test_version():
    print("[1] 版本号")
    check("__version__ == 1.4.9", __version__ == "1.4.9", f"实际 {__version__}")
    check("__build__ == 2609170005", __build__ == "2609170005", f"实际 {__build__}")


def test_gui():
    print("[2] GUI（按钮回归 / 显示完整 / 排序后对应 / 回归）")
    from PySide6.QtWidgets import QApplication, QPushButton
    from PySide6.QtCore import Qt, QPoint
    from nfo_profiler.gui import MainWindow
    tmp = tempfile.mkdtemp(prefix="smoke149_")
    try:
        app = QApplication.instance() or QApplication([])
        win = MainWindow(db_path=os.path.join(tmp, "gui.db"),
                         out_dir=os.path.join(tmp, "out"))
        store = win.store
        recs = []
        for i in range(6):
            d = f"2026-0{(i % 9) + 1}-1{i % 9}"
            recs.append({
                "path": f"D:\\g\\g{i:03d}.nfo", "num": f"GHI-{i:03d}",
                "title": f"作品 {i}", "premiered": d, "dateadded": d,
                "year": 2026, "actors": [f"演员{i % 3}"],
                "tags": [f"标签{i % 2}", f"独有标签{i}"], "studio": f"片商{i % 2}",
                "directors": [f"导演{i % 2}"],
                "parse_status": "ok", "mtime": d + " 00:00:00",
                "filesize": 10, "source": "D:\\g",
            })
        store.upsert_records(recs, commit=True)
        ids = [r["id"] for r in store.conn.execute(
            "SELECT id FROM movies ORDER BY id LIMIT 4")]
        for i, mid in enumerate(ids):
            win._recommender().vote(mid, f"GHI-{i:03d}", 1)
        store.upsert_vector_override("actor", "手动演员", 2.0)

        win.vector_refresh()
        win.resize(1280, 820)
        win.show()
        app.processEvents()

        n = win.vec_table.rowCount()
        check("表格有数据", n > 0, f"行数={n}")

        # 行内按钮存在 + 紧凑
        all_btns = []
        for r in range(n):
            cellw = win.vec_table.cellWidget(r, 6)
            if cellw:
                all_btns.extend(cellw.findChildren(QPushButton))
        check("行内按钮存在", len(all_btns) > 0, f"{len(all_btns)} 个")
        check("按钮紧凑（高度 22）",
              all(b.height() <= 24 for b in all_btns),
              f"高度集合={sorted({b.height() for b in all_btns})}")

        # 显示完整性：按钮组宽度不超操作列、右边缘不超视口
        col_w = win.vec_table.columnWidth(6)
        vp_w = win.vec_table.viewport().width()
        worst_extra, worst_right = 0, -1
        for r in range(n):
            cellw = win.vec_table.cellWidget(r, 6)
            if not cellw:
                continue
            btns = cellw.findChildren(QPushButton)
            if not btns:
                continue
            need = sum(b.width() for b in btns) + 6 * max(0, len(btns) - 1) + 8
            worst_extra = max(worst_extra, need - col_w)
            last = btns[-1]
            right = last.mapTo(win.vec_table.viewport(), QPoint(0, 0)).x() + last.width()
            worst_right = max(worst_right, right)
        check(f"按钮组不超出操作列（列宽 {col_w}）", worst_extra <= 0,
              f"最宽超出 {worst_extra}px")
        check(f"按钮右边缘不超出视口（视口 {vp_w}）", worst_right <= vp_w,
              f"最右 {worst_right}px")

        # 排序后按钮 ↔ 行数据对应（手动向量行有「删手动」）
        def row_has_del(r):
            cellw = win.vec_table.cellWidget(r, 6)
            if not cellw:
                return False
            return any(b.text() == "删手动" for b in cellw.findChildren(QPushButton))

        win.vec_table.sortItems(1, Qt.SortOrder.AscendingOrder)
        app.processEvents()
        mismatch = [r for r in range(win.vec_table.rowCount())
                    if row_has_del(r) !=
                    (win.vec_table.item(r, 4).text() != "—")]
        check("排序后按钮与行数据对应（删手动 → 有手动权重）", not mismatch,
              f"不一致行: {mismatch}")

        # 功能回归
        win._vector_mask("actor", "演员0")
        win.vector_refresh()
        hit = [r for r in range(win.vec_table.rowCount())
               if win.vec_table.item(r, 1).text() == "演员0"]
        check("屏蔽生效（手动权重 +0.00）", len(hit) == 1
              and win.vec_table.item(hit[0], 4).text().startswith("+0.00"))
        win._vector_del_manual("actor", "手动演员")
        names2 = {win.vec_table.item(r, 1).text()
                  for r in range(win.vec_table.rowCount())}
        check("删手动生效", "手动演员" not in names2)

        pm = win.tab_vector.grab()
        check("grab() 渲染", pm is not None and not pm.isNull())
        win.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    print(f"=== NFO 画像矿工 v1.4.9 冒烟测试 @ {datetime.now():%Y-%m-%d %H:%M:%S} ===")
    test_version()
    test_gui()
    print()
    if FAILS:
        print(f"RESULT: FAILED ({len(FAILS)} 项) -> {FAILS}")
        sys.exit(1)
    print("RESULT: ALL PASSED")


if __name__ == "__main__":
    main()
