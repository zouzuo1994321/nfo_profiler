# -*- coding: utf-8 -*-
"""v1.4.7 离屏冒烟测试（向量编辑：操作按钮改下拉菜单 + 表头排序）。

覆盖：
  1. 版本号 bump（__version__=1.4.7 / __build__=2609170003）；
  2. 操作列 UI：单按钮「▾ 操作」+ QMenu（屏蔽 / 删手动按行状态出现）；
  3. 表头排序：_vec_header_sort 名称列升序首行正确、再点切换降序、数字列倒序默认；
  4. 功能回归：屏蔽 / 删手动 / grab() 渲染。

运行：PYTHONPATH=<项目根> python tools/smoke_v147.py
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
    check("__version__ == 1.4.7", __version__ == "1.4.7", f"实际 {__version__}")
    check("__build__ == 2609170003", __build__ == "2609170003", f"实际 {__build__}")


def test_gui():
    print("[2] GUI（下拉菜单按钮 / 表头排序 / 回归）")
    from PySide6.QtWidgets import QApplication, QPushButton, QMenu
    from nfo_profiler.gui import MainWindow
    tmp = tempfile.mkdtemp(prefix="smoke147_")
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
                "tags": [f"标签{i % 2}"], "studio": f"片商{i % 2}",
                "directors": [f"导演{i % 2}"],
                "parse_status": "ok", "mtime": d + " 00:00:00",
                "filesize": 10, "source": "D:\\g",
            })
        store.upsert_records(recs, commit=True)
        ids = [r["id"] for r in store.conn.execute(
            "SELECT id FROM movies ORDER BY id LIMIT 3")]
        for i, mid in enumerate(ids):
            win._recommender().vote(mid, f"GHI-{i:03d}", 1)
        store.upsert_vector_override("actor", "手动演员", 2.0)

        win.vector_refresh()
        check("操作列宽 96", win.vec_table.columnWidth(6) == 96,
              f"实际 {win.vec_table.columnWidth(6)}")

        # 单按钮 + QMenu
        cellw = win.vec_table.cellWidget(0, 6)
        btns = cellw.findChildren(QPushButton) if cellw else []
        check("操作列为单个「▾ 操作」按钮",
              len(btns) == 1 and btns[0].text() == "▾ 操作",
              f"按钮数={len(btns)}")
        menus = btns[0].findChildren(QMenu) if btns else []
        check("按钮带下拉菜单（QMenu）", len(menus) == 1)

        # 排序：名称列（col=1）升序 → 首行名称最小；再点同列 → 降序反转
        win._vec_header_sort(1)
        first_asc = win.vec_table.item(0, 1).text()
        names_asc = [win.vec_table.item(r, 1).text()
                     for r in range(win.vec_table.rowCount())]
        check("名称列升序生效", names_asc == sorted(names_asc, key=str.lower),
              f"{names_asc[:5]}")
        win._vec_header_sort(1)
        names_desc = [win.vec_table.item(r, 1).text()
                      for r in range(win.vec_table.rowCount())]
        check("同列再点切换降序",
              names_desc == sorted(names_asc, key=str.lower, reverse=True),
              f"{names_desc[:5]}")
        # 数字列（col=2 自动正权）默认倒序
        win._vec_header_sort(2)
        vals = [float(win.vec_table.item(r, 2).text())
                for r in range(win.vec_table.rowCount())
                if win.vec_table.item(r, 2).text() != "—"]
        check("数字列默认倒序", vals == sorted(vals, reverse=True), f"{vals[:5]}")

        # 功能回归：屏蔽 / 删手动
        win._vec_header_sort(5)  # 回到默认生效强度倒序
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
    print(f"=== NFO 画像矿工 v1.4.7 冒烟测试 @ {datetime.now():%Y-%m-%d %H:%M:%S} ===")
    test_version()
    test_gui()
    print()
    if FAILS:
        print(f"RESULT: FAILED ({len(FAILS)} 项) -> {FAILS}")
        sys.exit(1)
    print("RESULT: ALL PASSED")


if __name__ == "__main__":
    main()
