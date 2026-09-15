# -*- coding: utf-8 -*-
"""一次性探查：为 v1.3.7 推荐改造确认真实数据分布（日期字段 / 浏览次数 / 投票）。"""
import os
import sqlite3
from datetime import date, datetime, timedelta

ROOT = r"Z:\【01】自研软件\【26-06】提取用户画像（正式版）"
DB = os.path.join(ROOT, "output", "nfo.db")
TODAY = date(2026, 9, 12)
CUT = TODAY - timedelta(days=180)

conn = sqlite3.connect(DB, timeout=30.0)
conn.row_factory = sqlite3.Row


def yr(v):
    if not v:
        return None
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


print("=== movies 总量 ===")
n = conn.execute("SELECT COUNT(*) c FROM movies WHERE path IS NOT NULL AND path<>''").fetchone()["c"]
print("有路径作品:", n)

print("\n=== premiered / dateadded 格式与近半年分布 ===")
prem_valid = prem_recent = dateadded_recent = both_recent = 0
prem_years = {}
sample_prem = []
for r in conn.execute("SELECT premiered, dateadded FROM movies WHERE path IS NOT NULL AND path<>''"):
    p = yr(r["premiered"])
    if p is not None:
        prem_valid += 1
        prem_years[p.year] = prem_years.get(p.year, 0) + 1
        if p >= CUT:
            prem_recent += 1
        if len(sample_prem) < 5:
            sample_prem.append((r["premiered"], r["dateadded"]))
    d = yr(r["dateadded"])
    if d is not None and d >= CUT:
        dateadded_recent += 1
    if p is not None and p >= CUT and d is not None and d >= CUT:
        both_recent += 1
print("premiered 可解析:", prem_valid)
print("premiered 在", CUT, "之后:", prem_recent)
print("dateadded 在", CUT, "之后:", dateadded_recent)
print("两者都近半年:", both_recent)
print("premiered 年份 Top:", sorted(prem_years.items(), key=lambda x: -x[1])[:8])
print("样例 premiered/dateadded:", sample_prem)

print("\n=== year 字段分布 ===")
for r in conn.execute("SELECT year, COUNT(*) c FROM movies WHERE year IS NOT NULL GROUP BY year ORDER BY c DESC LIMIT 8"):
    print(" ", r["year"], r["c"])

print("\n=== play_history（浏览记录）===")
ph_total = conn.execute("SELECT COUNT(*) c FROM play_history").fetchone()["c"]
print("浏览记录总数:", ph_total)
for thr in (2, 3, 4, 5):
    c = conn.execute("SELECT COUNT(*) c FROM play_history WHERE play_count>=?", (thr,)).fetchone()["c"]
    print(f"  play_count>={thr}: {c}")
print("play_count 最大:", conn.execute("SELECT MAX(play_count) m FROM play_history").fetchone()["m"])

print("\n=== 多次浏览但未投票（v1.3.7 欲降权群体）===")
unvoted_browsed = conn.execute(
    "SELECT COUNT(*) c FROM play_history h LEFT JOIN preferences p ON p.movie_id=h.movie_id "
    "WHERE h.play_count>=3 AND (p.vote IS NULL OR p.vote=0)").fetchone()["c"]
print("play_count>=3 且未投票:", unvoted_browsed)

print("\n=== preferences 投票分布（👎 不应进随机推荐）===")
up = conn.execute("SELECT COUNT(*) c FROM preferences WHERE vote>0").fetchone()["c"]
down = conn.execute("SELECT COUNT(*) c FROM preferences WHERE vote<0").fetchone()["c"]
print("👍:", up, " 👎:", down)

conn.close()
