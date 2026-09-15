# -*- coding: utf-8 -*-
"""v1.4.0 离屏冒烟测试（无 GUI / 无显示依赖，纯逻辑验证）。

覆盖：
  1. 版本号 bump（__version__=1.4.0 / __build__=2609150001）；
  2. 智能推荐 18 部「构成要求（软性补充）」（近半年≥2 / 近1月≥2 / 点赞演员共享≥1）+ 去重；
  3. 关键词偏向（标签 / 演员）生效；
  4. 扫描小库（<200 文件）自动回落单进程（res.workers==1）。

运行：PYTHONPATH=<项目根> python tools/smoke_v140.py
"""
import os
import sys
import tempfile
import shutil
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from nfo_profiler import __version__, __build__  # noqa: E402
from nfo_profiler.store import Store  # noqa: E402
from nfo_profiler.recommender import Recommender  # noqa: E402
from nfo_profiler.scanner import scan_paths  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ✅ {name}")
    else:
        print(f"  ❌ {name}  {detail}")
        FAILS.append(name)


def build_lib(n=120):
    tmp = tempfile.mkdtemp(prefix="smoke140_")
    db = os.path.join(tmp, "s.db")
    store = Store(db)
    rec = Recommender(store)
    today = datetime.now().date()
    actors = ["演员A", "演员B", "演员C", "演员D"]
    tags = ["标签X", "标签Y", "标签Z", "标签W"]
    recs = []
    for i in range(n):
        if i < 6:
            pd = today - timedelta(days=5 + i)
        elif i < 20:
            pd = today - timedelta(days=40 + (i - 6) * 8)
        else:
            pd = today - timedelta(days=400 + i)
        d = pd.strftime("%Y-%m-%d")
        recs.append({
            "path": f"D:\\m\\m{i:03d}.nfo", "num": f"ABC-{i:03d}",
            "title": f"作品 {i}", "premiered": d, "dateadded": d,
            "year": pd.year, "actors": [actors[i % 4]], "tags": [tags[i % 4]],
            "parse_status": "ok", "mtime": d + " 00:00:00",
            "filesize": 10, "source": "D:\\m",
        })
    store.upsert_records(recs, commit=True)
    rec.vote(0, "ABC-000", 1)
    rec.vote(1, "ABC-001", 1)
    rec.vote(100, "ABC-100", -1)
    return tmp, store, rec


def test_version():
    print("[1] 版本号")
    check("__version__ == 1.4.0", __version__ == "1.4.0", f"实际 {__version__}")
    check("__build__ == 2609150001", __build__ == "2609150001", f"实际 {__build__}")


def test_composition():
    print("[2] 智能推荐构成要求（软性补充） + 去重")
    tmp, store, rec = build_lib()
    try:
        picks = rec.smart_picks(limit=18, explore=3, diversity=0.5)
        ids = [p["movie_id"] for p in picks]
        check("返回 18 部", len(picks) == 18, f"实际 {len(picks)}")
        check("无重复 movie_id", len(set(ids)) == 18, f"实际 {len(set(ids))}")

        rows = {r["id"]: r for r in store.conn.execute(
            "SELECT id, premiered, dateadded FROM movies").fetchall()}
        today = datetime.now().date()
        c6 = (today - timedelta(days=180)).strftime("%Y-%m-%d")
        c1 = (today - timedelta(days=30)).strftime("%Y-%m-%d")

        def recent(mid, cut):
            r = rows[mid]
            for k in ("premiered", "dateadded"):
                try:
                    v = r[k]
                except Exception:
                    v = None
                if v and str(v)[:10] >= cut:
                    return True
            return False

        n6 = sum(1 for m in ids if recent(m, c6))
        n1 = sum(1 for m in ids if recent(m, c1))
        liked = {r["actor"] for r in store.conn.execute(
            "SELECT actor FROM movie_actors WHERE movie_id IN (0,1)").fetchall()}
        pa = set()
        for m in ids:
            for r in store.conn.execute(
                "SELECT actor FROM movie_actors WHERE movie_id=?", (m,)).fetchall():
                pa.add(r["actor"])
        check("近半年 ≥2 部", n6 >= 2, f"实际 {n6}")
        check("近1月 ≥2 部", n1 >= 2, f"实际 {n1}")
        check("点赞演员共享 ≥1 部", len(pa & liked) >= 1, f"实际 {len(pa & liked)}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_keyword():
    print("[3] 关键词偏向")
    tmp, store, rec = build_lib()
    try:
        kw = rec.smart_picks(limit=18, keyword="标签X")
        kw_ids = {p["movie_id"] for p in kw}
        tagx = {r["movie_id"] for r in store.conn.execute(
            "SELECT movie_id FROM movie_tags WHERE tag LIKE ?", ("%标签X%",)).fetchall()}
        check("标签关键词命中", len(kw_ids & tagx) >= 1)
        kw2 = rec.smart_picks(limit=18, keyword="演员C")
        kw2_ids = {p["movie_id"] for p in kw2}
        actc = {r["movie_id"] for r in store.conn.execute(
            "SELECT movie_id FROM movie_actors WHERE actor LIKE ?", ("%演员C%",)).fetchall()}
        check("演员关键词命中", len(kw2_ids & actc) >= 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_scan_fallback():
    print("[4] 扫描小库自动单进程")
    tmp = tempfile.mkdtemp(prefix="smoke140_scan_")
    root = os.path.join(tmp, "nfo")
    os.makedirs(root)
    for i in range(10):
        with open(os.path.join(root, f"m{i:02d}.nfo"), "w", encoding="utf-8") as f:
            f.write(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                f'<movie><title>T{i}</title><num>N-{i}</num>'
                f'<premiered>2025-01-0{(i%9)+1}</premiered>'
                f'<genre>g{i%3}</genre></movie>')
    db = os.path.join(tmp, "scan.db")
    store = Store(db)
    res = scan_paths(root, store, workers=None, incremental=False,
                     probe_video=False, batch_size=500)
    check("扫描到 10 个文件", res.scanned == 10, f"实际 {res.scanned}")
    check("小库回落单进程 (workers==1)", res.workers == 1, f"实际 {res.workers}")
    store.close()
    shutil.rmtree(tmp, ignore_errors=True)


def main():
    print(f"=== NFO 画像矿工 v1.4.0 冒烟测试 @ {datetime.now():%Y-%m-%d %H:%M:%S} ===")
    test_version()
    test_composition()
    test_keyword()
    test_scan_fallback()
    print()
    if FAILS:
        print(f"RESULT: FAILED ({len(FAILS)} 项) -> {FAILS}")
        sys.exit(1)
    print("RESULT: ALL PASSED ✅")


if __name__ == "__main__":
    main()
