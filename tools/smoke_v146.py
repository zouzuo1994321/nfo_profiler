# -*- coding: utf-8 -*-
"""v1.4.6 离屏冒烟测试（向量编辑 UI 布局修复验证）。

背景：v1.4.5「操作」列未设列宽策略（默认 Interactive），被 Stretch 的名称列挤压，
行内「屏蔽 / 删手动」按钮被裁切（截图实测）。修复：数字列 / 操作列固定宽度、
名称列独占 Stretch、按钮定高 24、行高 32、数字列居中。

覆盖：
  1. 版本号 bump（__version__=1.4.6 / __build__=2609170002）；
  2. 向量表列宽策略（0/2/3/4/5/6 固定、1 Stretch、操作列 ≥160）；
  3. 行高 32、按钮定高 24；
  4. 功能回归：屏蔽 / 删手动 / 过滤 / grab() 渲染。

运行：PYTHONPATH=<项目根> python tools/smoke_v146.py
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
    check("__version__ == 1.4.6", __version__ == "1.4.6", f"实际 {__version__}")
    check("__build__ == 2609170002", __build__ == "2609170002", f"实际 {__build__}")


def test_gui_layout():
    print("[2] GUI 向量表布局（列宽 / 行高 / 按钮 / 渲染）")
    from PySide6.QtWidgets import QApplication
    from PySide6.QtWidgets import QHeaderView
    from nfo_profiler.gui import MainWindow
    tmp = tempfile.mkdtemp(prefix="smoke146_")
    try:
        app = QApplication.instance() or QApplication([])
        win = MainWindow(db_path=os.path.join(tmp, "gui.db"),
                         out_dir=os.path.join(tmp, "out"))
        store = win.store
        recs = []
        for i in range(4):
            d = f"2026-0{(i % 9) + 1}-1{i % 9}"
            recs.append({
                "path": f"D:\\g\\g{i:03d}.nfo", "num": f"GHI-{i:03d}",
                "title": f"作品 {i}", "premiered": d, "dateadded": d,
                "year": 2026, "actors": [f"演员{i % 2}"],
                "tags": [f"标签{i % 2}"], "studio": f"片商{i % 2}",
                "directors": [f"导演{i % 2}"],
                "parse_status": "ok", "mtime": d + " 00:00:00",
                "filesize": 10, "source": "D:\\g",
            })
        store.upsert_records(recs, commit=True)
        ids = [r["id"] for r in store.conn.execute(
            "SELECT id FROM movies ORDER BY id LIMIT 2")]
        for i, mid in enumerate(ids):
            win._recommender().vote(mid, f"GHI-{i:03d}", 1)
        store.upsert_vector_override("actor", "手动演员", 2.0)

        win.vector_refresh()
        hdr = win.vec_table.horizontalHeader()
        check("名称列为 Stretch", hdr.sectionResizeMode(1) == QHeaderView.ResizeMode.Stretch)
        fixed_cols = {0: 76, 2: 92, 3: 92, 4: 92, 5: 92, 6: 168}
        ok_w = all(win.vec_table.columnWidth(c) == w for c, w in fixed_cols.items())
        check("数字列 / 操作列固定宽度", ok_w,
              f"实际 {{c: win.vec_table.columnWidth(c) for c in fixed_cols}}")
        check("行高 32", win.vec_table.verticalHeader().defaultSectionSize() == 32)

        # 含操作按钮的行：取第一个 cell widget 里的按钮验证定高
        btn = None
        for r in range(win.vec_table.rowCount()):
            cellw = win.vec_table.cellWidget(r, 6)
            if cellw is not None:
                from PySide6.QtWidgets import QPushButton
                bl = cellw.findChildren(QPushButton)
                if bl:
                    btn = bl[0]
                    break
        check("操作按钮存在", btn is not None)
        if btn is not None:
            check("按钮定高 24", btn.height() == 24 or btn.minimumHeight() == 24,
                  f"h={btn.height()} minH={btn.minimumHeight()}")

        # 功能回归
        win._vector_mask("actor", "演员0")
        hit = [r for r in range(win.vec_table.rowCount())
               if win.vec_table.item(r, 1).text() == "演员0"]
        check("屏蔽生效", len(hit) == 1
              and win.vec_table.item(hit[0], 4).text().startswith("+0.00"))
        win._vector_del_manual("actor", "手动演员")
        names = {win.vec_table.item(r, 1).text()
                 for r in range(win.vec_table.rowCount())}
        check("删手动生效", "手动演员" not in names)

        pm = win.tab_vector.grab()
        check("grab() 渲染", pm is not None and not pm.isNull())
        win.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    print(f"=== NFO 画像矿工 v1.4.6 冒烟测试 @ {datetime.now():%Y-%m-%d %H:%M:%S} ===")
    test_version()
    test_gui_layout()
    print()
    if FAILS:
        print(f"RESULT: FAILED ({len(FAILS)} 项) -> {FAILS}")
        sys.exit(1)
    print("RESULT: ALL PASSED")


if __name__ == "__main__":
    main()
