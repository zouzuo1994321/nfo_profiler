# -*- coding: utf-8 -*-
"""验证 v1.3.7 的三项推荐改造（改前 vs 改后 同脚本对照）。

做法：在真实库副本上，把 v1.3.7 的两个新开关（recent_boost_on / browse_demote_on）
全部关掉当「v1.3.6 基线」，再全部打开当「v1.3.7」，对比以下指标：

① 近半年加权：随机推荐里「premiered 在近 180 天」的作品占比 / 平均距今天数；
② 多次浏览未投降权：推荐里落入「play_count>=3 且未投票」集合的作品数；
③ 点踩硬排除：推荐里出现 👎 作品的数量（两种开关下都应为 0）。

::

    Copyright © 2026 肆月Aperture 本软件不得用于商业用途，仅做学习交流使用。
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Set

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from nfo_profiler.store import Store  # noqa: E402
from nfo_profiler.recommender import Recommender  # noqa: E402

SRC_DB = os.path.join(ROOT, "output", "nfo.db")
REPORT = os.path.join(ROOT, "output", "_verify_rec_v137.md")
ROUNDS = 40
LIMIT = 10
TODAY = date.today()
CUT = TODAY - timedelta(days=180)

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


def parse_date(v: Any) -> Any:
    if not v:
        return None
    s = str(v).strip()
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


def days_ago_map(conn: Any, mids: List[int]) -> Dict[int, int]:
    out: Dict[int, int] = {}
    for i in range(0, len(mids), 400):
        chunk = mids[i:i + 400]
        ph = ",".join("?" * len(chunk))
        for r in conn.execute(
                f"SELECT id, premiered, dateadded FROM movies WHERE id IN ({ph})", chunk):
            d = parse_date(r["premiered"]) or parse_date(r["dateadded"])
            if d is not None:
                out[int(r["id"])] = (TODAY - d).days
    return out


def browsed_unvoted_set(conn: Any) -> Set[int]:
    return {int(r["movie_id"]) for r in conn.execute(
        "SELECT h.movie_id FROM play_history h LEFT JOIN preferences p "
        "ON p.movie_id=h.movie_id WHERE h.play_count>=3 AND (p.vote IS NULL OR p.vote=0)")}


def down_set(conn: Any) -> Set[int]:
    return {int(r["movie_id"]) for r in conn.execute(
        "SELECT movie_id FROM preferences WHERE vote<0")}


def run(conn: Any, rec: Recommender, on: bool) -> List[List[int]]:
    rec.reset_recent()
    batches: List[List[int]] = []
    for _ in range(ROUNDS):
        picks = rec.random_picks(limit=LIMIT, recent_boost_on=on, browse_demote_on=on)
        batches.append([p["movie_id"] for p in picks])
    return batches


def metrics(conn: Any, batches: List[List[int]],
            buv: Set[int], down: Set[int]) -> Dict[str, float]:
    allm: List[int] = []
    for b in batches:
        allm.extend(b)
    dam = days_ago_map(conn, allm)
    recent = [d for d in dam.values() if 0 <= d <= 180]
    browsed = sum(1 for m in allm if m in buv)
    down_hits = sum(1 for m in allm if m in down)
    return {
        "total": len(allm),
        "recent_ratio": 100.0 * len(recent) / max(len(allm), 1),
        "mean_days": sum(dam.values()) / len(dam) if dam else 0.0,
        "browsed_in": browsed,
        "browsed_ratio": 100.0 * browsed / max(len(allm), 1),
        "down_in": down_hits,
    }


def main() -> int:
    tmp = os.path.join(tempfile.gettempdir(), "_verify_rec_v137.db")
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
    buv = browsed_unvoted_set(conn)
    down = down_set(conn)
    recent_global = conn.execute(
        "SELECT COUNT(*) c FROM movies WHERE premiered>=? AND path IS NOT NULL AND path<>''",
        (CUT.isoformat(),)).fetchone()["c"]
    emit(f"\n[0] 库 {total} 部　已投票 {len(voted)} 部　👎 {len(down)} 部")
    emit(f"    全库近半年作品: {recent_global} 部（占比 {100.0*recent_global/total:.1f}%）")
    emit(f"    多次浏览未投票(>=3): {len(buv)} 部　点踩: {len(down)} 部")

    emit(f"\n[1] 连刷 {ROUNDS} 次 × {LIMIT} 部对照（v1.3.6 基线 vs v1.3.7）：")
    base = metrics(conn, run(conn, rec, on=False), buv, down)
    rec.reset_recent()
    new = metrics(conn, run(conn, rec, on=True), buv, down)

    emit(f"    {'指标':<22}{'改前(关)':>12}{'改后(开)':>12}{'变化':>14}")
    rows = [
        ("近半年作品占比 %", base["recent_ratio"], new["recent_ratio"], "越高越好"),
        ("平均距今天数", base["mean_days"], new["mean_days"], "越低越新"),
        ("多次浏览未投票(部)", base["browsed_in"], new["browsed_in"], "越低越好"),
        ("多次浏览未投票占比%", base["browsed_ratio"], new["browsed_ratio"], "越低越好"),
        ("点踩作品出现(部)", base["down_in"], new["down_in"], "必须=0"),
    ]
    for name, o, v, note in rows:
        if v < o:
            delta = f"↓ {100.0 * (o - v) / o:.0f}%" if o else "—"
        elif v > o:
            delta = f"↑ {100.0 * (v - o) / max(o, 1e-9):.0f}%" if o else "—"
        else:
            delta = "持平"
        emit(f"    {name:<22}{o:>12.2f}{v:>12.2f}{delta:>14}   {note}")

    emit("\n[2] 无投票历史路径（清空 preferences 跑弱排序）：")
    saved = list(voted.items())
    with store.lock():
        conn.execute("DELETE FROM preferences")
        conn.commit()
    rec._df_cache = None
    rec.reset_recent()
    b2 = metrics(conn, run(conn, rec, on=False), buv, down)
    rec.reset_recent()
    n2 = metrics(conn, run(conn, rec, on=True), buv, down)
    emit(f"    近半年占比 %：{b2['recent_ratio']:.2f} → {n2['recent_ratio']:.2f}"
         f"（{('↑' if n2['recent_ratio'] > b2['recent_ratio'] else '↓')} "
         f"{abs(n2['recent_ratio']-b2['recent_ratio']):.1f}）")
    emit(f"    点踩出现(部)：{b2['down_in']} / {n2['down_in']}  （应为 0/0）")
    with store.lock():
        for mid, v in saved:
            row = conn.execute("SELECT num FROM movies WHERE id=?", (mid,)).fetchone()
            conn.execute(
                "INSERT OR REPLACE INTO preferences(movie_id, num, vote, voted_at) "
                "VALUES(?,?,?,datetime('now'))",
                (mid, row["num"] if row else "", int(v)))
        conn.commit()

    emit("\n[3] 健壮性：")
    rec.reset_recent()
    ok = True
    for desc, kw in (("limit=0", dict(limit=0)),
                     ("pool=1", dict(limit=6, pool=1)),
                     ("pool=50", dict(limit=6, pool=50)),
                     ("explore=0+diversity=0", dict(limit=6, explore=0, diversity=0.0))):
        picks = rec.random_picks(recent_boost_on=True, browse_demote_on=True, **kw)
        exp = kw.get("limit", 0)
        if "pool" in kw:        # 候选不足时返回全部（而非报错）
            exp = min(exp, kw["pool"] * 2)
        good = len(picks) == exp
        ok = ok and good
        emit(f"    {desc:<22}→ {len(picks)} 部 {'[OK]' if good else '[FAIL]'}")
    picks = rec.random_picks(limit=20, recent_boost_on=True, browse_demote_on=True)
    emit(f"    limit=20 → {len(picks)} 部 {'[OK]' if len(picks)==20 else '[FAIL]'}")
    ok = ok and len(picks) == 20

    emit("\n[判定]")
    if new["recent_ratio"] > base["recent_ratio"]:
        emit(f"    [OK] ① 近半年占比 {base['recent_ratio']:.1f}% → {new['recent_ratio']:.1f}%"
             f"（↑ {new['recent_ratio']-base['recent_ratio']:.1f}）")
    else:
        emit(f"    [FAIL] ① 近半年占比未提升（{base['recent_ratio']:.1f}% → {new['recent_ratio']:.1f}%）")
        ok = False
    if new["browsed_in"] < base["browsed_in"]:
        emit(f"    [OK] ② 多次浏览未投票 {base['browsed_in']} → {new['browsed_in']} 部（↓）")
    else:
        emit(f"    [WARN] ② 多次浏览未投票 {base['browsed_in']} → {new['browsed_in']} 部（未降）")
    if new["down_in"] == 0:
        emit("    [OK] ③ 点踩作品在随机推荐中 0 出现（硬排除生效）")
    else:
        emit(f"    [FAIL] ③ 点踩作品仍出现 {new['down_in']} 次")
        ok = False

    store.close()
    emit(f"\n[{'全部通过' if ok else '存在失败项'}]")
    with open(REPORT, "w", encoding="utf-8") as fh:
        fh.write("# v1.3.7 推荐改造验证（v1.3.6 基线 vs v1.3.7）\n\n```\n")
        fh.write("\n".join(_LINES))
        fh.write("\n```\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
