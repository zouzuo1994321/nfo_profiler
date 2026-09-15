# -*- coding: utf-8 -*-
"""一次性：扫描近半年加权强度，隔离 v1.3.4「最近已推避让」的干扰（每批 reset_recent）。"""
import os
import sqlite3
import sys
import tempfile
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from nfo_profiler.store import Store  # noqa: E402
from nfo_profiler import recommender as R  # noqa: E402
from nfo_profiler.recommender import Recommender  # noqa: E402

SRC = os.path.join(ROOT, "output", "nfo.db")
TODAY = date.today()
CUT = TODAY - timedelta(days=180)
ROUNDS = 15
LIMIT = 10


def days_ago_map(conn, mids):
    out = {}
    for i in range(0, len(mids), 400):
        ch = mids[i:i + 400]
        ph = ",".join("?" * len(ch))
        for r in conn.execute(f"SELECT id, premiered, dateadded FROM movies WHERE id IN ({ph})", ch):
            s = (r["premiered"] or r["dateadded"] or "")[:10]
            try:
                d = datetime.strptime(s, "%Y-%m-%d").date()
                out[int(r["id"])] = (TODAY - d).days
            except ValueError:
                pass
    return out


def main():
    tmp = os.path.join(tempfile.gettempdir(), "_sweep_recency.db")
    for suf in ("", "-wal", "-shm"):
        p = tmp + suf
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass
    s = sqlite3.connect(SRC, timeout=30.0)
    d = sqlite3.connect(tmp)
    with d:
        s.backup(d)
    s.close()
    d.close()

    store = Store(tmp)
    rec = Recommender(store)
    conn = store.conn
    recent_global = conn.execute(
        "SELECT COUNT(*) c FROM movies WHERE premiered>=? AND path IS NOT NULL AND path<>''",
        (CUT.isoformat(),)).fetchone()["c"]
    total = store.movie_count()
    print(f"全库近半年占比基线: {100.0*recent_global/total:.1f}%  (期望随机采样接近此值)\n")

    for frac in (0.0, 0.3, 0.5, 0.8, 1.0):
        R.RECENT_BOOST_FRAC = float(frac)
        rec.reset_recent()
        allm = []
        for _ in range(ROUNDS):
            picks = rec.random_picks(limit=LIMIT, recent_boost_on=True, browse_demote_on=True)
            rec.reset_recent()  # 隔离避让干扰，只看加权本身
            allm.extend(p["movie_id"] for p in picks)
        dam = days_ago_map(conn, allm)
        recent = [v for v in dam.values() if 0 <= v <= 180]
        print(f"RECENT_BOOST_FRAC={frac:>4}: 近半年占比 {100.0*len(recent)/len(allm):5.1f}%  "
              f"平均距今天数 {sum(dam.values())/len(dam):7.1f}")
    store.close()


if __name__ == "__main__":
    main()
