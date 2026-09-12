# -*- coding: utf-8 -*-
"""验证 v1.3.4 的 A/B/C/D 四项推荐改造（改前 vs 改后 同脚本对照）。

做法：把模块常量/参数调回「改造前」的行为跑一遍当基线，再恢复跑一遍：

* A 关：``IDF_FLOOR=1.0``（因子全部 clamp 到 1.0 = 不衰减）+ ``TITLE_DISCOUNT=1.0``
* B 关：每批前 ``reset_recent()``
* C 关：``diversity=0.0``
* D 关：``explore=0`` + ``pool=500``

指标：重复率 / 跨批次 Jaccard / 同批次 Jaccard / 相邻重叠 / 单部最高出现次数 /
有效轮换池 / 单次耗时。

::

    Copyright © 2026 肆月Aperture 本软件不得用于商业用途，仅做学习交流使用。
"""

from __future__ import annotations

import collections
import os
import sqlite3
import sys
import tempfile
import time
from typing import Any, Dict, List, Set

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from nfo_profiler import recommender as R  # noqa: E402
from nfo_profiler.store import Store  # noqa: E402
from nfo_profiler.recommender import Recommender  # noqa: E402

SRC_DB = os.path.join(ROOT, "output", "nfo.db")
REPORT = os.path.join(ROOT, "output", "_verify_rec_v134.md")
ROUNDS = 20
LIMIT = 6

_LINES: List[str] = []


def emit(line: str = "") -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()
    _LINES.append(line)


def copy_db(src: str, dst: str) -> None:
    s = sqlite3.connect(src, timeout=30.0)
    d = sqlite3.connect(dst)
    with d:
        s.backup(d)
    s.close()
    d.close()


def tags_of(conn: Any, mids: List[int]) -> Dict[int, Set[str]]:
    out: Dict[int, Set[str]] = {}
    for i in range(0, len(mids), 400):
        chunk = mids[i:i + 400]
        ph = ",".join("?" * len(chunk))
        for r in conn.execute(
                f"SELECT movie_id, tag FROM movie_tags WHERE movie_id IN ({ph})", chunk):
            if r["tag"]:
                out.setdefault(r["movie_id"], set()).add(r["tag"])
    return out


def metrics(conn: Any, batches: List[List[int]]) -> Dict[str, float]:
    allm: List[int] = []
    for b in batches:
        allm.extend(b)
    tg = tags_of(conn, allm)

    def jac(a: int, b: int) -> float:
        ta, tb = tg.get(a, set()), tg.get(b, set())
        if not ta or not tb:
            return 0.0
        return len(ta & tb) / len(ta | tb)

    cross, within, adj = [], [], []
    for i in range(len(batches)):
        li = list(batches[i])
        for x in range(len(li)):
            for y in range(x + 1, len(li)):
                within.append(jac(li[x], li[y]))
        for j in range(i + 1, len(batches)):
            for a in batches[i]:
                for b in batches[j]:
                    cross.append(jac(a, b))
            if j == i + 1:
                adj.append(len(set(batches[i]) & set(batches[j])))
    cnt = collections.Counter(allm)
    slots = sum(len(b) for b in batches)
    return {
        "dup_rate": 100.0 * (slots - len(cnt)) / max(slots, 1),
        "cross": sum(cross) / len(cross) if cross else 0.0,
        "within": sum(within) / len(within) if within else 0.0,
        "adj": sum(adj) / len(adj) if adj else 0.0,
        "max_hit": max(cnt.values()) if cnt else 0,
        "repeat_works": sum(1 for c in cnt.values() if c >= 2),
    }


def run(conn: Any, rec: Recommender, old: bool) -> Dict[str, float]:
    """跑 ROUNDS 批；old=True 时把 A/B/C/D 全部关掉当基线。"""
    if old:
        saved = (R.IDF_FLOOR, R.TITLE_DISCOUNT)
        R.IDF_FLOOR, R.TITLE_DISCOUNT = 1.0, 1.0
    batches: List[List[int]] = []
    t0 = time.time()
    try:
        for _ in range(ROUNDS):
            if old:
                rec.reset_recent()
            picks = rec.random_picks(
                limit=LIMIT,
                pool=500 if old else 2000,
                explore=0 if old else 1,
                diversity=0.0 if old else 0.35)
            batches.append([p["movie_id"] for p in picks])
    finally:
        if old:
            R.IDF_FLOOR, R.TITLE_DISCOUNT = saved
    m = metrics(conn, batches)
    m["sec_per_batch"] = (time.time() - t0) / ROUNDS
    return m


def table(conn: Any, rec: Recommender) -> None:
    """[A] IDF 生效：打印改后权重 Top10 及其全库覆盖。"""
    pos, _ = rec.profile_tokens()
    emit("    改后权重 Top10（含全库覆盖）：")
    n = max(rec.store.movie_count(), 1)
    rows = []
    for t, w in sorted(pos.items(), key=lambda x: -x[1])[:10]:
        kind, val = t.split(":", 1)
        if kind == "tag":
            df = conn.execute("SELECT COUNT(DISTINCT movie_id) c FROM movie_tags "
                              "WHERE tag=?", (val,)).fetchone()["c"]
        elif kind == "actor":
            df = conn.execute("SELECT COUNT(DISTINCT movie_id) c FROM movie_actors "
                              "WHERE actor=?", (val,)).fetchone()["c"]
        elif kind == "studio":
            df = conn.execute("SELECT COUNT(*) c FROM movies WHERE studio=?",
                              (val,)).fetchone()["c"]
        else:
            df = -1
        rows.append((t, w, df))
        pct = f"{100.0 * df / n:.1f}%" if df >= 0 else "  — "
        emit(f"      {t:<22} 权重 {w:7.2f}　覆盖 {df if df >= 0 else '—':>6} 部"
             f"（{pct}）")
    top1 = rows[0]
    gen = [r for r in rows if r[2] > 0]
    emit(f"    改前 Top1 = tag:单体作品（覆盖 56.6%）；改后 Top1 = {top1[0]}")
    if top1[2] > 0 and top1[2] / n > 0.30:
        emit("    [FAIL] A · IDF 未生效：Top1 仍是覆盖 >30% 的泛化标签")
        return
    emit("    [OK] A · IDF 生效：泛化标签已被压出榜首")


def main() -> int:
    tmp = os.path.join(tempfile.gettempdir(), "_verify_rec_v134.db")
    for suf in ("", "-wal", "-shm"):
        p = tmp + suf
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass
    emit(f"[*] 复制数据库副本 -> {tmp}")
    copy_db(SRC_DB, tmp)

    store = Store(tmp)
    rec = Recommender(store)
    conn = store.conn
    total = store.movie_count()
    voted = store.voted_ids()
    emit(f"\n[0] 库 {total} 部　已投票 {len(voted)} 部")

    emit("\n[A] 泛化标签 IDF 降权：")
    table(conn, rec)

    emit(f"\n[B/C/D] 连刷 {ROUNDS} 次 × {LIMIT} 部对照：")
    old = run(conn, rec, old=True)
    rec.reset_recent()
    new = run(conn, rec, old=False)

    emit(f"    {'指标':<22}{'改前':>12}{'改后':>12}{'变化':>14}")
    rows = [
        ("跨批次 Jaccard", old["cross"], new["cross"], "越低越多样"),
        ("同批次 Jaccard", old["within"], new["within"], "越低越多样"),
        ("重复率 %", old["dup_rate"], new["dup_rate"], "越低越好"),
        ("相邻批次重叠(部)", old["adj"], new["adj"], "越低越好"),
        ("单部最高出现次数", old["max_hit"], new["max_hit"], "越低越好"),
        ("重复过的作品数", old["repeat_works"], new["repeat_works"], "越低越好"),
        ("单批耗时(秒)", old["sec_per_batch"], new["sec_per_batch"], "越小越好"),
    ]
    for name, o, v, note in rows:
        if v < o:
            delta = f"↓ {100.0 * (o - v) / o:.0f}%" if o else "—"
        elif v > o:
            delta = f"↑ {100.0 * (v - o) / max(o, 1e-9):.0f}%" if o else "—"
        else:
            delta = "持平"
        emit(f"    {name:<22}{o:>12.3f}{v:>12.3f}{delta:>14}   {note}")

    emit("\n[判定]")
    ok = True
    if new["cross"] >= old["cross"]:
        emit(f"    [FAIL] 跨批次 Jaccard 未下降（{old['cross']:.3f} → {new['cross']:.3f}）")
        ok = False
    else:
        emit(f"    [OK] 跨批次 Jaccard {old['cross']:.3f} → {new['cross']:.3f}"
             f"（降 {100.0 * (old['cross'] - new['cross']) / old['cross']:.0f}%）")
    if new["within"] >= old["within"]:
        emit(f"    [FAIL] 同批次 Jaccard 未下降（MMR 未生效）")
        ok = False
    else:
        emit(f"    [OK] 同批次 Jaccard {old['within']:.3f} → {new['within']:.3f}"
             f"（C · MMR 生效）")
    if new["max_hit"] > old["max_hit"]:
        emit(f"    [WARN] 单部最高出现次数反而升高（{old['max_hit']} → {new['max_hit']}）")
        ok = False
    else:
        emit(f"    [OK] 单部最高出现次数 {old['max_hit']} → {new['max_hit']}"
             f"（B · 最近已推避让生效）")
    if new["sec_per_batch"] > 5.0:
        emit(f"    [WARN] 单批耗时 {new['sec_per_batch']:.2f}s 偏慢")
        ok = False
    else:
        emit(f"    [OK] 单批耗时 {new['sec_per_batch']:.2f}s")

    emit("\n[E] 健壮性：")
    rec.reset_recent()
    picks = rec.random_picks(limit=0)
    emit(f"    limit=0 → {len(picks)} 部 {'[OK]' if not picks else '[FAIL]'}")
    if picks:
        ok = False
    picks = rec.random_picks(limit=6, pool=1)
    emit(f"    pool=1（候选仅 2 部）→ {len(picks)} 部 "
         f"{'[OK]' if len(picks) == 2 else '[FAIL]'}  候选不足时返回全部而非报错")
    if len(picks) != 2:
        ok = False
    picks = rec.random_picks(limit=6, pool=50)
    emit(f"    pool=50（候选 100 部）→ {len(picks)} 部 "
         f"{'[OK]' if len(picks) == 6 else '[FAIL]'}")
    if len(picks) != 6:
        ok = False
    picks = rec.random_picks(limit=6, explore=0, diversity=0.0)
    emit(f"    explore=0 + diversity=0（关掉 C/D）→ {len(picks)} 部 "
         f"{'[OK]' if len(picks) == 6 else '[FAIL]'}")
    if len(picks) != 6:
        ok = False

    saved_ids = list(store.voted_ids())
    try:
        with store.lock():
            conn.execute("DELETE FROM preferences")
            conn.commit()
        rec._df_cache = None
        rec.reset_recent()
        picks = rec.random_picks(limit=6)
        emit(f"    无投票历史（纯随机路径）→ {len(picks)} 部 "
             f"{'[OK]' if len(picks) == 6 else '[FAIL]'}")
        if len(picks) != 6:
            ok = False
    finally:
        with store.lock():
            for mid in saved_ids:
                row = conn.execute("SELECT num FROM movies WHERE id=?", (mid,)).fetchone()
                conn.execute(
                    "INSERT OR REPLACE INTO preferences(movie_id, num, vote, voted_at) "
                    "VALUES(?,?,1,datetime('now'))", (mid, row["num"] if row else ""))
            conn.commit()

    store.close()
    emit(f"\n[{'全部通过' if ok else '存在失败项'}]")
    with open(REPORT, "w", encoding="utf-8") as fh:
        fh.write("# v1.3.4 推荐改造验证（改前 vs 改后）\n\n```\n")
        fh.write("\n".join(_LINES))
        fh.write("\n```\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
