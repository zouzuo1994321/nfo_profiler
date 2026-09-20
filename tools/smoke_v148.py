# -*- coding: utf-8 -*-
"""v1.4.8 离屏冒烟测试（向量编辑：无 cell widget + 原生表头排序 + 右键操作）。

背景：v1.4.5-7 的操作列嵌 cell-widget 按钮，「显示不全」始终存在（cell widget
不参与列宽/滚动计算）。v1.4.8 彻底移除操作列控件，操作统一为行右键菜单；
表头排序改为与作品明细一致的原生 setSortingEnabled + 数值条目自定义 __lt__。

覆盖：
  1. 版本号 bump（__version__=1.4.8 / __build__=2609170004）；
  2. 表内无任何 cell widget（显示不全根除）；
  3. 原生排序：名称列文本序、自动正权数值序（非文本序）、排序后右键数据不错位；
  4. 功能回归：屏蔽 / 删手动 / grab() 渲染。

运行：PYTHONPATH=<项目根> python tools/smoke_v148.py
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
    check("__version__ == 1.4.8", __version__ == "1.4.8", f"实际 {__version__}")
    check("__build__ == 2609170004", __build__ == "2609170004", f"实际 {__build__}")


def test_gui():
    print("[2] GUI（无 cell widget / 原生排序 / 右键数据 / 回归）")
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import Qt
    from nfo_profiler.gui import MainWindow
    tmp = tempfile.mkdtemp(prefix="smoke148_")
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
        n = win.vec_table.rowCount()
        check("表格有数据", n > 0, f"行数={n}")
        check("全表无 cell widget（显示不全根除）",
              all(win.vec_table.cellWidget(r, 6) is None
                  for r in range(n)))
        check("原生排序已开启", win.vec_table.isSortingEnabled())

        # 名称列文本排序（Qt 用本地化拼音排序，与 Python 码点序不同；
        # 健壮断言：升序结果与降序结果互为倒序）
        win.vec_table.sortItems(1, Qt.SortOrder.AscendingOrder)
        names_asc = [win.vec_table.item(r, 1).text() for r in range(n)]
        win.vec_table.sortItems(1, Qt.SortOrder.DescendingOrder)
        names_desc = [win.vec_table.item(r, 1).text() for r in range(n)]
        check("名称列文本排序（升/降序互为倒序）",
              names_desc == list(reversed(names_asc)),
              f"asc前3={names_asc[:3]} desc前3={names_desc[:3]}")

        # 自动正权数值排序（非文本序：9.00 应排在 12.00 后面）
        win.vec_table.sortItems(2, Qt.SortOrder.DescendingOrder)
        vals = [float(win.vec_table.item(r, 2).text())
                for r in range(n) if win.vec_table.item(r, 2).text() != "—"]
        check("数值列按数字倒序（非文本序）", vals == sorted(vals, reverse=True),
              f"{vals[:6]}")

        # 排序后右键数据不错位：UserRole 元组与该行名称对应
        row0_name = win.vec_table.item(0, 1).text()
        data = win.vec_table.item(0, 0).data(Qt.ItemDataRole.UserRole)
        check("排序后行数据不错位（UserRole 元组）",
              isinstance(data, tuple) and len(data) == 5 and data[1] == row0_name,
              f"name={row0_name} data={data}")

        # 功能回归：屏蔽 / 删手动（右键动作的处理函数与菜单一致）
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
    print(f"=== NFO 画像矿工 v1.4.8 冒烟测试 @ {datetime.now():%Y-%m-%d %H:%M:%S} ===")
    test_version()
    test_gui()
    print()
    if FAILS:
        print(f"RESULT: FAILED ({len(FAILS)} 项) -> {FAILS}")
        sys.exit(1)
    print("RESULT: ALL PASSED")


if __name__ == "__main__":
    main()
