# -*- coding: utf-8 -*-
"""一次性调试：单批随机推荐里，近半年候选的 raw / boost / base 到底怎么分布。"""
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

# 直接复用内部逻辑做单批探查
tmp = os.path.join(tempfile.gettempdir(), "_dbg_recency.db")
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
R.RECENT_BOOST_MAX = 25.0

# 单元自测：直接对一行调用 _recency_boost
one = conn.execute("SELECT id, premiered, dateadded FROM movies WHERE premiered IS NOT NULL ORDER BY RANDOM() LIMIT 1").fetchone()
print("[UNIT] premiered=", one["premiered"], " parsed=", rec._parse_recent_date(one),
      " boost=", rec._recency_boost(one), " RECENT_BOOST_MAX=", R.RECENT_BOOST_MAX,
      " RECENT_DAYS=", R.RECENT_DAYS)

# 复制 _random_picks_locked 的核心（仅探查，不改逻辑）
rows = conn.execute(
    "SELECT id, num, title, path, studio, premiered, dateadded, year FROM movies "
    "WHERE path IS NOT NULL AND path<>'' ORDER BY RANDOM() LIMIT 4000").fetchall()
ids = [r["id"] for r in rows]
print(f"候选 {len(rows)} 部")
recent_ids = set()
for r in rows:
    pr = r["premiered"] or r["dateadded"] or ""
    try:
        dd = datetime.strptime(pr[:10], "%Y-%m-%d").date()
        if 0 <= (TODAY - dd).days <= 180:
            recent_ids.add(r["id"])
    except ValueError:
        pass
print(f"其中近半年候选: {len(recent_ids)} 部 ({100.0*len(recent_ids)/len(rows):.1f}%)")

# 计算 raw（偏好）与 boost
pos, neg = rec.profile_tokens()
print(f"偏好画像: 正 {len(pos)} 词 / 负 {len(neg)} 词")

# 拉 tag/actor
from nfo_profiler.recommender import _chunks
toks_by = {i: set() for i in ids}
for ch in _chunks(ids, 400):
    ph = ",".join("?" * len(ch))
    for r in conn.execute(f"SELECT movie_id, tag FROM movie_tags WHERE movie_id IN ({ph})", ch):
        toks_by.setdefault(r["movie_id"], set()).add("tag:" + (r["tag"] or ""))
    for r in conn.execute(f"SELECT movie_id, actor FROM movie_actors WHERE movie_id IN ({ph})", ch):
        toks_by.setdefault(r["movie_id"], set()).add("actor:" + (r["actor"] or ""))

from nfo_profiler.title_tokenizer import tokenize_title, _default_stopwords
raw = {}
for r in rows:
    t = toks_by.get(r["id"], set())
    if r["studio"]:
        t.add("studio:" + r["studio"])
    for tok in tokenize_title(r["title"] or "", known=rec._known_words(), stopwords=_default_stopwords()):
        t.add("title:" + tok)
    sc = sum(pos.get(x, 0.0) - neg.get(x, 0.0) for x in t)
    raw[r["id"]] = sc

rows_by = {r["id"]: r for r in rows}
boosts = {m: rec._recency_boost(rows_by[m]) for m in ids}

# 分近半年 / 非近半年，看 raw 与 base 分布
def stats(flag_recent):
    sel = [m for m in ids if (m in recent_ids) == flag_recent]
    raws = [raw[m] for m in sel]
    bases = [(raw[m] + boosts[m]) for m in sel]
    return (len(sel),
            sum(raws)/len(raws), min(raws), max(raws),
            sum(bases)/len(bases), min(bases), max(bases))

for label, fr in (("近半年", True), ("非近半年", False)):
    n, rmean, rmin, rmax, bmean, bmin, bmax = stats(fr)
    print(f"[{label}] n={n} raw均值={rmean:.2f}[{rmin:.1f},{rmax:.1f}] "
          f"base(含boost)均值={bmean:.2f}[{bmin:.1f},{bmax:.1f}]")

# 实际跑一次，看 picks
picks = rec.random_picks(limit=10, recent_boost_on=True, browse_demote_on=True)
print("\n实际 picks (movie_id, 近半年?, raw, boost, base):")
for p in picks:
    m = p["movie_id"]
    dd = (r["premiered"] or r["dateadded"] or "")
    is_recent = m in recent_ids
    print(f"  {m}  recent={is_recent}  raw={raw[m]:.2f}  boost={boosts[m]:.2f}  "
          f"base={raw[m]+boosts[m]:.2f}")

store.close()
