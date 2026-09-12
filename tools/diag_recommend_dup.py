# -*- coding: utf-8 -*-
"""诊断：④作品推荐「随机推荐」重复率为何偏高（v1.3.4 排查用）。

用法::

    python -u tools/diag_recommend_dup.py [轮数]

输出：
1. 库规模 / 投票数 / 偏好 token 概况
2. 连刷 N 次 random_picks(6) 的**实测重复率**（唯一作品数、相邻批次重叠、
   重复出现 >=2 次的作品数）
3. 得分分布 + **有效轮换池**（得分落在 [S_max - jitter, S_max] 的候选数）
   —— 这个池子越小，"换一批"越像"换了个寂寞"
4. 抽样池覆盖率：pool*2 候选占全库比例

::

    Copyright © 2026 肆月Aperture 本软件不得用于商业用途，仅做学习交流使用。
"""

from __future__ import annotations

import collections
import os
import random
import sqlite3
import sys
import tempfile
from typing import Any, Dict, List, Set

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nfo_profiler.store import Store  # noqa: E402
from nfo_profiler.recommender import Recommender  # noqa: E402
from nfo_profiler.title_tokenizer import (  # noqa: E402
    tokenize_title, infer_known_from_store, _default_stopwords)

SRC_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "output", "nfo.db")
REPORT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "output", "_diag_rec_dup.md")

_LINES: List[str] = []


def _emit(line: str = "") -> None:
    """同时打印到 stdout 并收集进 Markdown 报告（避免控制台编码问题）。"""
    sys.stdout.write(line + "\n")
    sys.stdout.flush()
    _LINES.append(line)


def copy_db(src: str, dst: str) -> None:
    """WAL 热库安全复制（sqlite3 backup API）。"""
    s = sqlite3.connect(src, timeout=30.0)
    d = sqlite3.connect(dst)
    with d:
        s.backup(d)
    s.close()
    d.close()


def main() -> int:
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    tmp = os.path.join(tempfile.gettempdir(), "_diag_rec_dup.db")
    for suffix in ("", "-wal", "-shm"):
        p = tmp + suffix
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass
    _emit(f"[*] 复制数据库副本 -> {tmp}")
    copy_db(SRC_DB, tmp)

    store = Store(tmp)
    rec = Recommender(store)
    conn = store.conn

    total = conn.execute("SELECT COUNT(*) c FROM movies").fetchone()["c"]
    with_path = conn.execute(
        "SELECT COUNT(*) c FROM movies WHERE path IS NOT NULL AND path<>''").fetchone()["c"]
    voted = store.voted_ids()
    summary = store.vote_summary()
    _emit(f"\n[1] 库规模：{total} 部（有路径 {with_path}）　"
          f"已投票 {len(voted)} 部（👍{summary['up']} / 👎{summary['down']}）")

    pos, neg = rec.profile_tokens()
    _emit(f"[2] 偏好 token：正 {len(pos)} 个 / 负 {len(neg)} 个")
    if pos:
        top = sorted(pos.items(), key=lambda x: -x[1])[:10]
        _emit("    正向 Top10：" + "、".join(f"{k}({v:.0f})" for k, v in top))
        # token 覆盖面：每个 token 在全库覆盖多少部（抽样估算）
        hot = []
        for t, w in top:
            kind, val = t.split(":", 1)
            if kind == "tag":
                c = conn.execute("SELECT COUNT(*) c FROM movie_tags WHERE tag=?",
                                 (val,)).fetchone()["c"]
            elif kind == "actor":
                c = conn.execute("SELECT COUNT(*) c FROM movie_actors WHERE actor=?",
                                 (val,)).fetchone()["c"]
            elif kind == "studio":
                c = conn.execute("SELECT COUNT(*) c FROM movies WHERE studio=?",
                                 (val,)).fetchone()["c"]
            else:
                c = -1
            hot.append(f"{t}({c}部)")
        _emit("    覆盖面：" + "、".join(hot))
    if neg:
        topn = sorted(neg.items(), key=lambda x: -x[1])[:5]
        _emit("    负向 Top5：" + "、".join(f"{k}({v:.0f})" for k, v in topn))

    # ---------------- 实测重复率 ----------------
    _emit(f"\n[3] 连刷 {rounds} 次 random_picks(6)：")
    batches: List[List[Dict[str, Any]]] = []
    for _ in range(rounds):
        batches.append(rec.random_picks(limit=6))

    counter: collections.Counter = collections.Counter()
    for b in batches:
        for m in b:
            counter[m["movie_id"]] += 1
    slots = sum(len(b) for b in batches)
    uniq = len(counter)
    _emit(f"    总槽位 {slots}　唯一作品 {uniq}　"
          f"**重复率 {100.0 * (slots - uniq) / slots:.1f}%**")

    ov = []
    for i in range(1, len(batches)):
        a = {m["movie_id"] for m in batches[i - 1]}
        b = {m["movie_id"] for m in batches[i]}
        ov.append(len(a & b))
    if ov:
        _emit(f"    相邻两批平均重叠 {sum(ov) / len(ov):.2f} / 6 部"
              f"（最高 {max(ov)}）")

    multi = [(mid, c) for mid, c in counter.items() if c >= 2]
    multi.sort(key=lambda x: -x[1])
    _emit(f"    出现 >=2 次的作品：{len(multi)} 部")
    for mid, c in multi[:12]:
        r = conn.execute("SELECT num, title FROM movies WHERE id=?", (mid,)).fetchone()
        _emit(f"      ×{c}　{r['num'] if r else '?'}　{(r['title'] if r else '')[:28]}")

    # ---------------- 得分分布 / 有效轮换池 ----------------
    _emit("\n[4] 得分分布与有效轮换池（决定 '换一批' 能换出多少新东西）：")
    known = rec._known_words()
    stop = _default_stopwords()
    rows = conn.execute(
        "SELECT id, num, title, studio FROM movies "
        "WHERE path IS NOT NULL AND path<>'' ORDER BY RANDOM() LIMIT 5000").fetchall()
    ids = [r["id"] for r in rows]
    toks_by: Dict[int, Set[str]] = {i: set() for i in ids}
    ph = ",".join("?" * len(ids))
    for r in conn.execute(f"SELECT movie_id, tag FROM movie_tags WHERE movie_id IN ({ph})", ids):
        toks_by.setdefault(r["movie_id"], set()).add("tag:" + (r["tag"] or ""))
    for r in conn.execute(f"SELECT movie_id, actor FROM movie_actors WHERE movie_id IN ({ph})", ids):
        toks_by.setdefault(r["movie_id"], set()).add("actor:" + (r["actor"] or ""))
    scored = []
    for r in rows:
        t = toks_by.get(r["id"], set())
        if r["studio"]:
            t.add("studio:" + r["studio"])
        for x in tokenize_title(r["title"] or "", known=known, stopwords=stop):
            t.add("title:" + x)
        scored.append((sum(pos.get(k, 0.0) - neg.get(k, 0.0) for k in t), r))

    scores = sorted((s for s, _ in scored), reverse=True)
    mx = scores[0] if scores else 0.0
    jitter = max(2.0, max(abs(s) for s, _ in scored) * 0.6)
    _emit(f"    抽样 {len(scores)} 部：最高分 {mx:.1f}　"
          f"中位 {scores[len(scores) // 2]:.1f}　最低 {scores[-1]:.1f}")
    _emit(f"    当前 jitter = max(2.0, {max(abs(s) for s, _ in scored):.1f}×0.6) "
          f"= {jitter:.1f}")
    # 有效轮换池：得分 >= mx - jitter 的作品（只有这些有机会进 top）
    eff = [s for s in scores if s >= mx - jitter]
    _emit(f"    **有效轮换池 = 得分 >= {mx - jitter:.1f} 的作品：{len(eff)} 部 "
          f"（占抽样 {100.0 * len(eff) / len(scores):.1f}%）**")
    _emit(f"       → 换算到全库约 {int(len(eff) / len(scores) * with_path)} 部，"
          f"6 张卡从中轮换")

    # 直方图
    _emit("    得分直方图（top 分段）：")
    for lo, hi in ((mx - jitter, mx), (mx - 2 * jitter, mx - jitter),
                   (mx - 3 * jitter, mx - 2 * jitter)):
        n = sum(1 for s in scores if lo <= s < hi)
        _emit(f"      [{lo:7.1f}, {hi:7.1f}) : {n} 部")
    n0 = sum(1 for s in scores if abs(s) < 1e-9)
    _emit(f"      [   0.0 分（完全没命中偏好）] : {n0} 部 "
          f"（{100.0 * n0 / len(scores):.1f}%）")

    # ---------------- 抽样池覆盖 ----------------
    _emit(f"\n[5] 抽样池：pool=500 → 抽 {500 * 2} 部候选，占全库 "
          f"{100.0 * 1000 / with_path:.2f}%")
    _emit(f"    即：每刷一次，全库只有 {100.0 * 1000 / with_path:.2f}% 的作品"
          f"有机会被看到（再经得分排序，实际可选面更小）")

    # ---------------- 跨批次「长得像不像」 ----------------
    # 客观重复率低 ≠ 用户不觉得重复：若跨批次作品 tag 高度重合，
    # 观感就是「翻来覆去都一个样」。
    _emit("\n[6] 跨批次特征重合度（观感重复的真正来源）：")
    all_mids: List[int] = []
    batch_sets: List[Set[int]] = []
    for b in batches:
        s = {m["movie_id"] for m in b}
        batch_sets.append(s)
        all_mids.extend(s)
    tags_of: Dict[int, Set[str]] = {i: set() for i in all_mids}
    ph = ",".join("?" * len(all_mids))
    for r in conn.execute(f"SELECT movie_id, tag FROM movie_tags WHERE movie_id IN ({ph})",
                          all_mids):
        if r["tag"]:
            tags_of.setdefault(r["movie_id"], set()).add(r["tag"])

    def _jacc(a: int, b: int) -> float:
        ta, tb = tags_of.get(a, set()), tags_of.get(b, set())
        if not ta or not tb:
            return 0.0
        return len(ta & tb) / len(ta | tb)

    cross, within = [], []
    for i in range(len(batch_sets)):
        for j in range(i + 1, len(batch_sets)):
            for a in batch_sets[i]:
                for b in batch_sets[j]:
                    cross.append(_jacc(a, b))
    for s in batch_sets:
        lst = list(s)
        for x in range(len(lst)):
            for y in range(x + 1, len(lst)):
                within.append(_jacc(lst[x], lst[y]))
    if cross:
        _emit(f"    跨批次两两 tag Jaccard 平均 {sum(cross) / len(cross):.3f} "
              f"（最高 {max(cross):.2f}）")
    if within:
        _emit(f"    同批次内两两 tag Jaccard 平均 {sum(within) / len(within):.3f}")
        if cross:
            ratio = (sum(cross) / len(cross)) / max(sum(within) / len(within), 1e-9)
            _emit(f"    **跨批次 / 同批次 = {ratio:.2f}**"
                  f"（接近 1.0 说明「不同批次」和「同一批」一样像 → 换汤不换药）")

    # 全库基线：随机两部作品的 Jaccard（用来判断上面的数是高是低）
    base_rows = conn.execute(
        "SELECT id FROM movies WHERE path IS NOT NULL AND path<>'' "
        "ORDER BY RANDOM() LIMIT 300").fetchall()
    base_ids = [r["id"] for r in base_rows]
    ph = ",".join("?" * len(base_ids))
    bt: Dict[int, Set[str]] = {i: set() for i in base_ids}
    for r in conn.execute(f"SELECT movie_id, tag FROM movie_tags WHERE movie_id IN ({ph})",
                          base_ids):
        if r["tag"]:
            bt.setdefault(r["movie_id"], set()).add(r["tag"])
    base_sims = []
    for i in range(0, len(base_ids) - 1, 2):
        a, b = bt.get(base_ids[i], set()), bt.get(base_ids[i + 1], set())
        if a and b:
            base_sims.append(len(a & b) / len(a | b))
    if base_sims:
        _emit(f"    对照基线：全库随机两部的 Jaccard 平均 "
              f"{sum(base_sims) / len(base_sims):.3f}")

    # ---------------- 泛化标签权重模拟 ----------------
    _emit("\n[7] 泛化标签诊断 + IDF 降权模拟：")
    N = max(with_path, 1)
    gen = []
    for t, w in sorted(pos.items(), key=lambda x: -x[1]):
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
            continue
        gen.append((t, w, df, df / N))
    over = [g for g in gen if g[3] > 0.20]
    _emit(f"    权重 Top20 里，覆盖 >20% 全库的泛化标签：{len(over)} 个")
    for t, w, df, r in over[:8]:
        _emit(f"      {t}　权重 {w:.0f}　覆盖 {df} 部（{r * 100:.1f}%）")

    # 模拟：泛化标签权重 ×0.15，其余不变 → 重算得分分布
    new_pos = dict(pos)
    for t, w, df, r in gen:
        if r > 0.20:
            new_pos[t] = w * 0.15
    new_scored = []
    for r in rows:
        t = toks_by.get(r["id"], set())
        if r["studio"]:
            t.add("studio:" + r["studio"])
        for x in tokenize_title(r["title"] or "", known=known, stopwords=stop):
            t.add("title:" + x)
        new_scored.append(sum(new_pos.get(k, 0.0) - neg.get(k, 0.0) for k in t))
    ns = sorted(new_scored, reverse=True)
    nmx = ns[0] if ns else 0.0
    nmean = sum(ns) / len(ns) if ns else 0.0
    nvar = (sum((x - nmean) ** 2 for x in ns) / len(ns)) ** 0.5 if ns else 0.0
    mean = sum(scores) / len(scores)
    var = (sum((x - mean) ** 2 for x in scores) / len(scores)) ** 0.5
    nj = max(2.0, max(abs(x) for x in new_scored) * 0.6)
    neff = sum(1 for x in ns if x >= nmx - nj)
    _emit(f"    现状：均值 {mean:.1f}　标准差 {var:.1f}　"
          f"有效轮换池 {len(eff)} 部（{100.0 * len(eff) / len(scores):.1f}%）")
    _emit(f"    降权后：均值 {nmean:.1f}　标准差 {nvar:.1f}　"
          f"有效轮换池 {neff} 部（{100.0 * neff / len(ns):.1f}%）")
    _emit(f"    → 有效轮换池收窄 {100.0 * (len(eff) - neff) / max(len(eff), 1):.0f}%，"
          f"推荐区分度提升 {nvar / max(var, 1e-9):.2f}×")

    store.close()
    with open(REPORT, "w", encoding="utf-8") as fh:
        fh.write("# 随机推荐重复率诊断报告\n\n```\n")
        fh.write("\n".join(_LINES))
        fh.write("\n```\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
