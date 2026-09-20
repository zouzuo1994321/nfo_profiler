# -*- coding: utf-8 -*-
"""v1.6.0 离屏冒烟测试（向量编辑：双击「手动权重」编辑 + 按钮字号缩小）。

覆盖：
  1. 版本号 bump（__version__=1.6.0 / __build__=2609170007）；
  2. 按钮字号 11px（QSS 断言）；
  3. 双击「手动权重」编辑链路（monkeypatch QInputDialog.getDouble）：
     无手动覆盖 → 新增；有 → 覆盖；输入 0 → 屏蔽；取消 → 不变；非 col=4 → 无操作；
  4. 功能回归：屏蔽 / 删手动 / grab() 渲染。

运行：PYTHONPATH=<项目根> python tools/smoke_v160.py
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
    check("__version__ == 1.6.0", __version__ == "1.6.0", f"实际 {__version__}")
    check("__build__ == 2609170007", __build__ == "2609170007", f"实际 {__build__}")


def test_gui():
    print("[2] GUI（双击编辑 / 字号 / 回归）")
    from PySide6.QtWidgets import QApplication, QPushButton, QInputDialog
    import nfo_profiler.gui as gui_mod
    from nfo_profiler.gui import MainWindow
    tmp = tempfile.mkdtemp(prefix="smoke160_")
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
                "tags": [f"标签{i % 2}", f"独有标签{i}"], "studio": f"片商{i % 2}",
                "directors": [f"导演{i % 2}"],
                "parse_status": "ok", "mtime": d + " 00:00:00",
                "filesize": 10, "source": "D:\\g",
            })
        store.upsert_records(recs, commit=True)
        ids = [r["id"] for r in store.conn.execute(
            "SELECT id FROM movies ORDER BY id LIMIT 3")]
        for i, mid in enumerate(ids):
            win._recommender().vote(mid, f"GHI-{i:03d}", 1)
        win.vector_refresh()

        # 字号 11px
        check("按钮 QSS 字号 11px", "font-size: 11px" in MainWindow._VEC_BTN_QSS,
              MainWindow._VEC_BTN_QSS)

        # 双击编辑链路：monkeypatch 弹窗
        def find_row(name):
            for r in range(win.vec_table.rowCount()):
                if win.vec_table.item(r, 1).text() == name:
                    return r
            return -1

        # 3.1 无手动覆盖 → 新增 3.5
        gui_mod.QInputDialog.getDouble = staticmethod(
            lambda *a, **k: (3.5, True))
        r = find_row("独有标签0")
        win._vec_cell_double_clicked(r, 4)
        m = {o["kind"] + ":" + o["name"]: o["weight"]
             for o in store.vector_overrides()}
        check("双击新增手动权重 3.5", m.get("tag:独有标签0") == 3.5, f"{m}")

        # 3.2 有手动覆盖 → 覆盖为 -1.0
        gui_mod.QInputDialog.getDouble = staticmethod(
            lambda *a, **k: (-1.0, True))
        win._vec_cell_double_clicked(find_row("独有标签0"), 4)
        m = {o["kind"] + ":" + o["name"]: o["weight"]
             for o in store.vector_overrides()}
        check("再次双击覆盖为 -1.0", m.get("tag:独有标签0") == -1.0, f"{m}")

        # 3.3 输入 0 → 屏蔽（等效 weight=0）
        gui_mod.QInputDialog.getDouble = staticmethod(
            lambda *a, **k: (0.0, True))
        win._vec_cell_double_clicked(find_row("独有标签0"), 4)
        m = {o["kind"] + ":" + o["name"]: o["weight"]
             for o in store.vector_overrides()}
        check("输入 0 等效屏蔽", m.get("tag:独有标签0") == 0.0, f"{m}")

        # 3.4 取消 → 不变
        before = len(store.vector_overrides())
        gui_mod.QInputDialog.getDouble = staticmethod(
            lambda *a, **k: (9.9, False))
        win._vec_cell_double_clicked(find_row("独有标签0"), 4)
        check("取消不落库", len(store.vector_overrides()) == before
              and {o["weight"] for o in store.vector_overrides()
                   if o["name"] == "独有标签0"} == {0.0})

        # 3.5 非手动权重列 → 无操作
        gui_mod.QInputDialog.getDouble = staticmethod(
            lambda *a, **k: (9.9, True))
        before = len(store.vector_overrides())
        win._vec_cell_double_clicked(find_row("独有标签0"), 2)
        check("双击其他列不触发编辑", len(store.vector_overrides()) == before)

        # 回归：屏蔽 / 删手动 / 渲染
        win._vector_mask("actor", "演员0")
        win.vector_refresh()
        hit = [r for r in range(win.vec_table.rowCount())
               if win.vec_table.item(r, 1).text() == "演员0"]
        check("屏蔽回归", len(hit) == 1
              and win.vec_table.item(hit[0], 4).text().startswith("+0.00"))
        pm = win.tab_vector.grab()
        check("grab() 渲染", pm is not None and not pm.isNull())
        win.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    print(f"=== NFO 画像矿工 v1.6.0 冒烟测试 @ {datetime.now():%Y-%m-%d %H:%M:%S} ===")
    test_version()
    test_gui()
    print()
    if FAILS:
        print(f"RESULT: FAILED ({len(FAILS)} 项) -> {FAILS}")
        sys.exit(1)
    print("RESULT: ALL PASSED")


if __name__ == "__main__":
    main()
