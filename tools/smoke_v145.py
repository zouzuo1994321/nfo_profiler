# -*- coding: utf-8 -*-
"""v1.4.5 离屏冒烟测试（QT_QPA_PLATFORM=offscreen）。

覆盖：
  1. 版本号 bump（__version__=1.4.5 / __build__=2609170001）；
  2. store.vector_overrides CRUD（添加 / 覆盖 / 删除 / 非法 kind 拒绝）；
  3. 推荐引擎：
     a. 导演维度进入偏好画像（director: token）；
     b. 手动向量合并（>0 加正权 / <0 加负权 / =0 屏蔽自动向量）；
     c. vector_snapshot 结构（auto_pos/auto_neg/pos/neg/manual）；
  4. GUI 离屏全窗口：向量编辑 Tab 存在、vector_refresh 填表、屏蔽/删手动、grab() 渲染。

运行：PYTHONPATH=<项目根> python tools/smoke_v145.py
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
from nfo_profiler.store import Store  # noqa: E402
from nfo_profiler.recommender import Recommender  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print(f"  OK  {name}")
    else:
        print(f"  XX  {name}  {detail}")
        FAILS.append(name)


def build_lib():
    """临时库：6 部作品（含导演），前 3 部投 👍。"""
    tmp = tempfile.mkdtemp(prefix="smoke145_")
    db = os.path.join(tmp, "s.db")
    store = Store(db)
    rec = Recommender(store)
    recs = []
    for i in range(6):
        d = f"2026-0{(i % 9) + 1}-1{i % 9}"
        recs.append({
            "path": f"D:\\m\\m{i:03d}.nfo", "num": f"ABC-{i:03d}",
            "title": f"作品 {i}", "premiered": d, "dateadded": d,
            "year": 2026, "actors": [f"演员{i % 3}"],
            "tags": [f"标签{i % 2}", f"独家{i}"], "studio": f"片商{i % 2}",
            "directors": [f"导演{i % 2}"],
            "parse_status": "ok", "mtime": d + " 00:00:00",
            "filesize": 10, "source": "D:\\m",
        })
    store.upsert_records(recs, commit=True)
    ids = [r["id"] for r in store.conn.execute(
        "SELECT id FROM movies ORDER BY id LIMIT 3")]
    for i, mid in enumerate(ids):
        rec.vote(mid, f"ABC-{i:03d}", 1)
    return tmp, store, rec


def test_version():
    print("[1] 版本号")
    check("__version__ == 1.4.5", __version__ == "1.4.5", f"实际 {__version__}")
    check("__build__ == 2609170001", __build__ == "2609170001", f"实际 {__build__}")


def test_store_crud():
    print("[2] store 向量 CRUD")
    tmp, store, rec = build_lib()
    try:
        store.upsert_vector_override("actor", "测试演员", 3.0)
        store.upsert_vector_override("actor", "测试演员", 5.0)  # 覆盖
        store.upsert_vector_override("tag", "测试标签", -1.5)
        rows = store.vector_overrides()
        check("覆盖写入（同 kind+name 只一条）", len(rows) == 2, f"{rows}")
        m = {r["kind"] + ":" + r["name"]: r["weight"] for r in rows}
        check("覆盖后权重生效", m.get("actor:测试演员") == 5.0
              and m.get("tag:测试标签") == -1.5, f"{m}")
        check("删除手动向量", store.delete_vector_override("actor", "测试演员") == 1
              and len(store.vector_overrides()) == 1)
        try:
            store.upsert_vector_override("title", "x", 1.0)
            check("非法 kind 拒绝", False, "未抛异常")
        except ValueError:
            check("非法 kind 拒绝", True)
        try:
            store.upsert_vector_override("tag", "  ", 1.0)
            check("空名称拒绝", False, "未抛异常")
        except ValueError:
            check("空名称拒绝", True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_profile_merge():
    print("[3] 画像合并（导演维度 / 正负 / 屏蔽 / snapshot）")
    tmp, store, rec = build_lib()
    try:
        with store.lock():
            pos, neg = rec._profile_tokens_locked(include_overrides=False)
        check("导演维度自动入画像（director:导演0/导演1）",
              any(t.startswith("director:") for t in pos),
              f"pos keys 样例: {list(pos)[:8]}")
        auto_actor = pos.get("actor:演员0", 0.0)
        check("自动演员向量存在", auto_actor > 0, f"actor:演员0={auto_actor}")

        # >0 加正权（不做 IDF）
        store.upsert_vector_override("actor", "手动演员", 2.5)
        with store.lock():
            pos2, neg2 = rec._profile_tokens_locked()
        check("手动正权进入画像", pos2.get("actor:手动演员", 0.0) == 2.5,
              f"{pos2.get('actor:手动演员')}")
        # <0 加负权
        store.upsert_vector_override("tag", "独家0", -1.0)
        with store.lock():
            pos3, neg3 = rec._profile_tokens_locked()
        auto_tag0 = (pos.get("tag:独家0", 0.0) - neg.get("tag:独家0", 0.0))
        eff_tag0 = (pos3.get("tag:独家0", 0.0) - neg3.get("tag:独家0", 0.0))
        check("手动负权软排斥（净权下降 1.0）",
              abs((auto_tag0 - eff_tag0) - 1.0) < 1e-6,
              f"auto={auto_tag0} eff={eff_tag0}")
        # =0 屏蔽
        store.upsert_vector_override("actor", "演员0", 0.0)
        with store.lock():
            pos4, neg4 = rec._profile_tokens_locked()
        check("屏蔽后自动向量清零",
              pos4.get("actor:演员0", 0.0) == 0.0 and neg4.get("actor:演员0", 0.0) == 0.0,
              f"pos={pos4.get('actor:演员0')} neg={neg4.get('actor:演员0')}")

        # snapshot 结构
        snap = rec.vector_snapshot()
        check("snapshot 含 manual 3 条", len(snap["manual"]) == 3,
              f"{len(snap['manual'])}")
        check("snapshot auto/eff 分离（auto_pos 含被屏蔽向量）",
              snap["auto_pos"].get("actor:演员0", 0.0) > 0
              and snap["pos"].get("actor:演员0", 0.0) == 0.0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_gui():
    print("[4] GUI 离屏（向量编辑 Tab / 刷新 / 屏蔽 / 删手动 / 渲染）")
    from PySide6.QtWidgets import QApplication
    from nfo_profiler.gui import MainWindow
    tmp = tempfile.mkdtemp(prefix="smoke145gui_")
    try:
        app = QApplication.instance() or QApplication([])
        win = MainWindow(db_path=os.path.join(tmp, "gui.db"),
                         out_dir=os.path.join(tmp, "out"))
        # 灌数据 + 投票，产生自动向量
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

        check("rec_tabs 含 3 个子模块（随机/智能/向量）",
              win.rec_tabs.count() == 3,
              f"实际 {win.rec_tabs.count()}: "
              f"{[win.rec_tabs.tabText(i) for i in range(win.rec_tabs.count())]}")
        check("第 3 个子模块名为 🧬 向量编辑",
              win.rec_tabs.tabText(2) == "🧬 向量编辑",
              f"实际 {win.rec_tabs.tabText(2)!r}")

        # 手动加一条 → 刷新应出现在表里
        store.upsert_vector_override("actor", "手动演员", 3.0)
        win.vector_refresh()
        names = {(win.vec_table.item(r, 0).text(), win.vec_table.item(r, 1).text())
                 for r in range(win.vec_table.rowCount())}
        check("手动向量出现在表内", ("演员", "手动演员") in names,
              f"行数={win.vec_table.rowCount()}")
        auto_rows = [r for r in range(win.vec_table.rowCount())
                     if win.vec_table.item(r, 0).text() == "导演"]
        check("导演维度向量在表内", len(auto_rows) >= 1)

        # 屏蔽一条自动向量 → 生效列归零、按钮变「删手动」
        win._vector_mask("actor", "演员0")
        hit = [r for r in range(win.vec_table.rowCount())
               if win.vec_table.item(r, 1).text() == "演员0"]
        check("屏蔽后仍显示该行（手动权重 0）", len(hit) == 1)
        if hit:
            r = hit[0]
            check("屏蔽行手动权重显示 +0.00",
                  win.vec_table.item(r, 4).text().startswith("+0.00"),
                  f"实际 {win.vec_table.item(r, 4).text()!r}")

        # 删手动 → 该向量恢复自动（或从表消失若无自动）
        win._vector_del_manual("actor", "手动演员")
        names2 = {win.vec_table.item(r, 1).text()
                  for r in range(win.vec_table.rowCount())}
        check("删手动后消失（无自动来源）", "手动演员" not in names2)

        # 过滤：只看手动 + 名称过滤
        store.upsert_vector_override("tag", "过滤器标签", 1.0)
        win.vec_filter_edit.setText("过滤器")
        win.vector_refresh()
        check("名称过滤生效", win.vec_table.rowCount() == 1
              and win.vec_table.item(0, 1).text() == "过滤器标签",
              f"行数={win.vec_table.rowCount()}")
        win.vec_filter_edit.clear()
        win.vec_only_manual.setChecked(True)
        win.vector_refresh()
        n_manual_rows = win.vec_table.rowCount()
        check("只看手动向量", n_manual_rows == len(store.vector_overrides()),
              f"行数={n_manual_rows} overrides={len(store.vector_overrides())}")
        win.vec_only_manual.setChecked(False)

        # 渲染：grab() 触发真实绘制无异常
        pm = win.tab_vector.grab()
        check("向量 Tab grab() 渲染", pm is not None and not pm.isNull())
        win.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    print(f"=== NFO 画像矿工 v1.4.5 冒烟测试 @ {datetime.now():%Y-%m-%d %H:%M:%S} ===")
    test_version()
    test_store_crud()
    test_profile_merge()
    test_gui()
    print()
    if FAILS:
        print(f"RESULT: FAILED ({len(FAILS)} 项) -> {FAILS}")
        sys.exit(1)
    print("RESULT: ALL PASSED")


if __name__ == "__main__":
    main()
