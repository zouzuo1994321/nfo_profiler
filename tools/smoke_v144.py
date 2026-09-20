# -*- coding: utf-8 -*-
"""v1.4.4 离屏冒烟测试（无 GUI / 无显示依赖，纯逻辑验证）。

覆盖：
  1. 版本号 bump（__version__=1.4.4 / __build__=2609160003）；
  2. 「点赞演员共享」构成要求的**演员级轮换**（v1.4.4 核心修复）：
     修复前 `_liked_actor_ids` 按 sorted(actors) 字母序 + 无随机 + 无演员记忆，
     导致每批恒定命中同一位点赞演员（实测批批都是 JULIA）；
     修复后：随机选演员 + 近 RECENT_FORGET 批已用演员避让；
  3. smart_picks 全流程两批：正常返回、结果带演员、_req_actors 状态被登记。

运行：PYTHONPATH=<项目根> python tools/smoke_v144.py
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
    """建临时库：40 部作品、5 位演员各 8 部（每部仅 1 位演员，便于按演员断言轮换）；
    对前 5 部（movie_id 1..5，rowid 从 1 起）投 👍（演员0..演员4 各一位点赞演员）。"""
    tmp = tempfile.mkdtemp(prefix="smoke144_")
    db = os.path.join(tmp, "s.db")
    store = Store(db)
    rec = Recommender(store)
    recs = []
    for i in range(n):
        d = f"2026-0{(i % 9) + 1}-1{i % 9}"
        recs.append({
            "path": f"D:\\m\\m{i:03d}.nfo", "num": f"ABC-{i:03d}",
            "title": f"作品 {i}", "premiered": d, "dateadded": d,
            "year": 2026, "actors": [f"演员{i % 5}"],
            "tags": [f"标签{i % 4}"], "studio": f"片商{i % 3}",
            "parse_status": "ok", "mtime": d + " 00:00:00",
            "filesize": 10, "source": "D:\\m",
        })
    store.upsert_records(recs, commit=True)
    ids = [r["id"] for r in store.conn.execute(
        "SELECT id FROM movies ORDER BY id LIMIT 5")]
    for i, mid in enumerate(ids):
        rec.vote(mid, f"ABC-{i:03d}", 1)
    return tmp, store, rec


def actor_of(store, movie_id):
    r = store.conn.execute(
        "SELECT actor FROM movie_actors WHERE movie_id=? "
        "ORDER BY ord, rowid LIMIT 1", (movie_id,)).fetchone()
    return r["actor"] if r else None


def test_version():
    print("[1] 版本号")
    check("__version__ == 1.4.4", __version__ == "1.4.4", f"实际 {__version__}")
    check("__build__ == 2609160003", __build__ == "2609160003", f"实际 {__build__}")


def test_actor_rotation():
    print("[2] 点赞演员共享 → 演员级轮换（不再恒定同一演员）")
    tmp, store, rec = build_lib()
    try:
        voted = set(store.voted_ids().keys())
        check("前置：5 位点赞演员（演员0..演员4，movie_id 1..5）",
              {actor_of(store, i) for i in range(1, 6)} == {f"演员{i}" for i in range(5)})

        # 连续 5 批（5 位候选演员，每位用后即入 RECENT_FORGET 避让期）：
        # 批次 1..5 应全部不同演员 —— 这是避让机制的确定性推论。
        used = []
        exclude = voted
        for i in range(5):
            ids = rec._liked_actor_ids(exclude, 1)
            if len(ids) != 1:
                check(f"第 {i+1} 批返回 1 部", False, f"实际 {len(ids)}")
                break
            a = actor_of(store, ids[0])
            used.append(a)
            rec._remember([])  # 推进批次号（模拟 smart_picks 末尾的统一登记）
        check("连续 5 批命中 5 位不同演员（演员级避让生效）",
              len(set(used)) == 5 and len(used) == 5,
              f"实际 {len(set(used))}/{len(used)}: {used}")
        check("已用演员全部登记进 _req_actors",
              set(rec._req_actors) == set(used),
              f"_req_actors={sorted(rec._req_actors)} used={sorted(set(used))}")

        # 第 9 批：5 位演员全部在避让期内 → 清空避让防死锁，仍能兜底返回
        ids = rec._liked_actor_ids(exclude, 1)
        check("避让期全满时清空避让、仍可兜底返回", len(ids) == 1, f"实际 {len(ids)}")

        # 无点赞作品 → 返回空
        rec2_store = Store(os.path.join(tmp, "empty.db"))
        rec2 = Recommender(rec2_store)
        check("无点赞作品返回空列表", rec2._liked_actor_ids(set(), 1) == [])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_smart_picks_flow():
    print("[3] smart_picks 全流程两批（回归：轮换不被构成要求锁死）")
    tmp, store, rec = build_lib()
    try:
        b1 = rec.smart_picks(limit=18)
        check("第 1 批 18 部", len(b1) == 18, f"实际 {len(b1)}")
        check("第 1 批带演员名", all("actors" in p for p in b1))
        b2 = rec.smart_picks(limit=18)
        check("第 2 批 18 部", len(b2) == 18, f"实际 {len(b2)}")
        ids1 = {p["movie_id"] for p in b1}
        ids2 = {p["movie_id"] for p in b2}
        check("两批间有轮换（非完全重复）", len(ids1 & ids2) < 18,
              f"重叠 {len(ids1 & ids2)}/18")
        check("无重复 movie_id", len(ids2) == 18)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    print(f"=== NFO 画像矿工 v1.4.4 冒烟测试 @ {datetime.now():%Y-%m-%d %H:%M:%S} ===")
    test_version()
    test_actor_rotation()
    test_smart_picks_flow()
    print()
    if FAILS:
        print(f"RESULT: FAILED ({len(FAILS)} 项) -> {FAILS}")
        sys.exit(1)
    print("RESULT: ALL PASSED ✅")


if __name__ == "__main__":
    main()
