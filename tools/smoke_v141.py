# -*- coding: utf-8 -*-
"""v1.4.1 离屏冒烟测试（无 GUI / 无显示依赖，纯逻辑验证）。

覆盖：
  1. 版本号 bump（__version__=1.4.1 / __build__=2609150003）；
  2. 推荐结果附「演员名」（random_picks / smart_picks / similar_picks 均带 actors 键，
     卡片副标题由片商改为演员）；
  3. 投票记录 vote_records / 浏览记录 play_records 返回 actors 聚合字段（表头「片商」→「演员」）。

运行：PYTHONPATH=<项目根> python tools/smoke_v141.py
"""
import os
import sys
import tempfile
import shutil
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from nfo_profiler import __version__, __build__  # noqa: E402
from nfo_profiler.store import Store  # noqa: E402
from nfo_profiler.recommender import Recommender  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ✅ {name}")
    else:
        print(f"  ❌ {name}  {detail}")
        FAILS.append(name)


def build_lib(n=40):
    tmp = tempfile.mkdtemp(prefix="smoke141_")
    db = os.path.join(tmp, "s.db")
    store = Store(db)
    rec = Recommender(store)
    today = datetime.now().date()
    recs = []
    for i in range(n):
        d = (today.toordinal() - i) and f"2026-0{(i % 9) + 1}-1{i % 9}"
        recs.append({
            "path": f"D:\\m\\m{i:03d}.nfo", "num": f"ABC-{i:03d}",
            "title": f"作品 {i}", "premiered": d, "dateadded": d,
            "year": 2026, "actors": [f"演员{i % 5}", f"演员{(i + 1) % 5}"],
            "tags": [f"标签{i % 4}"], "studio": f"片商{i % 3}",
            "parse_status": "ok", "mtime": d + " 00:00:00",
            "filesize": 10, "source": "D:\\m",
        })
    store.upsert_records(recs, commit=True)
    rec.vote(0, "ABC-000", 1)
    rec.vote(1, "ABC-001", 1)
    store.record_play_by_path("D:\\m\\m002.nfo")
    store.record_play_by_path("D:\\m\\m003.nfo")
    return tmp, store, rec


def test_version():
    print("[1] 版本号")
    check("__version__ == 1.4.1", __version__ == "1.4.1", f"实际 {__version__}")
    check("__build__ == 2609150003", __build__ == "2609150003", f"实际 {__build__}")


def test_picks_have_actors():
    print("[2] 推荐结果附演员名（卡片副标题 → 演员）")
    tmp, store, rec = build_lib()
    try:
        rp = rec.random_picks(limit=6)
        check("random_picks 带 actors 键", all("actors" in p for p in rp),
              f"实际 {sum(1 for p in rp if 'actors' in p)}/{len(rp)}")
        check("random_picks 演员名非空且来自 movie_actors",
              any(p["actors"] for p in rp), "全部为空")
        bad = [p["num"] for p in rp if p["actors"] and p["actors"].startswith("片商")]
        check("random_picks 不再是片商名", not bad, f"仍为片商: {bad}")

        sp = rec.smart_picks(limit=18)
        check("smart_picks 带 actors 键", all("actors" in p for p in sp),
              f"实际 {sum(1 for p in sp if 'actors' in p)}/{len(sp)}")
        check("smart_picks 演员名非空", any(p["actors"] for p in sp), "全部为空")

        sim = rec.similar_picks(0, limit=6)
        check("similar_picks 带 actors 键", all("actors" in p for p in sim),
              f"实际 {sum(1 for p in sim if 'actors' in p)}/{len(sim)}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_history_actors():
    print("[3] 投票 / 浏览记录带演员聚合（表头 片商 → 演员）")
    tmp, store, rec = build_lib()
    try:
        vr = store.vote_records()
        check("vote_records 返回 actors 字段", all("actors" in r for r in vr),
              f"{len(vr)} 条")
        check("vote_records 演员名来自 movie_actors",
              any(r["actors"] and r["actors"].startswith("演员") for r in vr), "无命中")
        pr = store.play_records()
        check("play_records 返回 actors 字段", all("actors" in r for r in pr),
              f"{len(pr)} 条")
        check("play_records 演员名非空", any(r["actors"] for r in pr), "全部为空")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    print(f"=== NFO 画像矿工 v1.4.1 冒烟测试 @ {datetime.now():%Y-%m-%d %H:%M:%S} ===")
    test_version()
    test_picks_have_actors()
    test_history_actors()
    print()
    if FAILS:
        print(f"RESULT: FAILED ({len(FAILS)} 项) -> {FAILS}")
        sys.exit(1)
    print("RESULT: ALL PASSED ✅")


if __name__ == "__main__":
    main()
