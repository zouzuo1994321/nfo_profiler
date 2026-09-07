# -*- coding: utf-8 -*-
"""作品推荐引擎（v1.2.0）—— 基于 👍/👎 反馈的偏好学习 + 加权随机推荐。

设计要点
--------
* **偏好画像**：把用户投过票（``preferences`` 表）的作品的
  ``tag / actor / studio / 标题分词`` 聚合成两份权重表（正 / 负）；
* **随机刷新**：SQLite ``ORDER BY RANDOM()`` 先抽一个 500 部左右的候选池，
  在池内按「偏好得分 + 探索抖动」排序取前 N ——
  投票越多，随机刷出来的作品越贴近口味，但抖动保证不会只推同几部；
* **相似推荐**：基于标签 + 演员的 Jaccard 相似度（与 :mod:`ai_engine` 一致思路），
  本模块独立实现一份按 ``movie_id`` 起查的版本，避免循环依赖；
* **零重依赖**：只用标准库 + :mod:`title_tokenizer`。

::

    Copyright © 2026 肆月Aperture 本软件不得用于商业用途，仅做学习交流使用。
"""

from __future__ import annotations

import random
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

from .title_tokenizer import tokenize_title, infer_known_from_store, _default_stopwords


class Recommender:
    """基于用户 👍/👎 反馈的作品推荐器。"""

    def __init__(self, store: Any) -> None:
        self.store = store
        self._known: Optional[Set[str]] = None

    # ------------------------------------------------------------------
    # 投票（带 toggle 语义：重复点同一个按钮 = 取消投票）
    # ------------------------------------------------------------------
    def vote(self, movie_id: int, num: str, vote: int) -> int:
        """投票 +1 / -1；与已有投票相同则撤销。返回最终 vote（0=已撤销）。

        **线程安全**：toggle 是「查→改」两步操作，整段持锁保证原子性。
        """
        with self.store.lock():
            cur = self.store.get_vote(movie_id)
            if cur == vote:
                self.store.remove_vote(movie_id)
                return 0
            self.store.upsert_vote(movie_id, num, vote)
            return vote

    # ------------------------------------------------------------------
    # 偏好画像：从投票历史聚合 token 权重
    # ------------------------------------------------------------------
    def _known_words(self) -> Set[str]:
        if self._known is None:
            try:
                self._known = infer_known_from_store(self.store)
            except Exception:
                self._known = set()
        return self._known

    def _movie_tokens(self, movie_id: int, title: str, studio: str) -> Set[str]:
        """一部作品的 token 集合：tag / actor / studio / 标题分词，带类型前缀避免撞名。"""
        conn = self.store.conn
        toks: Set[str] = set()
        for r in conn.execute("SELECT tag FROM movie_tags WHERE movie_id=?", (movie_id,)):
            if r["tag"]:
                toks.add("tag:" + r["tag"])
        for r in conn.execute("SELECT actor FROM movie_actors WHERE movie_id=?", (movie_id,)):
            if r["actor"]:
                toks.add("actor:" + r["actor"])
        if studio:
            toks.add("studio:" + studio)
        for t in tokenize_title(title or "", known=self._known_words(),
                                stopwords=_default_stopwords()):
            toks.add("title:" + t)
        return toks

    def profile_tokens(self) -> Tuple[Dict[str, float], Dict[str, float]]:
        """返回 (正权重表, 负权重表)。

        * 👍 作品：tag/actor 权重 2.0，studio 1.0，标题词 1.0；
        * 👎 作品：同权重进负表；
        * token 在多部 👍 作品中出现则累加（次数即强度）。

        **线程安全**：整段持 ``store.lock()``（RLock 可重入，
        ``_random_picks_locked`` 内部调用本方法不会死锁）。
        """
        with self.store.lock():
            return self._profile_tokens_locked()

    def _profile_tokens_locked(self) -> Tuple[Dict[str, float], Dict[str, float]]:
        votes = self.store.votes()
        pos: Dict[str, float] = defaultdict(float)
        neg: Dict[str, float] = defaultdict(float)
        if not votes:
            return dict(pos), dict(neg)
        conn = self.store.conn
        for v in votes:
            mid = v["movie_id"]
            row = conn.execute(
                "SELECT title, studio FROM movies WHERE id=?", (mid,)).fetchone()
            if row is None:
                continue
            toks = self._movie_tokens(mid, row["title"] or "", row["studio"] or "")
            target = pos if v["vote"] > 0 else neg
            for t in toks:
                kind = t.split(":", 1)[0]
                w = 2.0 if kind in ("tag", "actor") else 1.0
                target[t] += w
        return dict(pos), dict(neg)

    # ------------------------------------------------------------------
    # 随机推荐（偏好加权 + 探索抖动）
    # ------------------------------------------------------------------
    def random_picks(self, limit: int = 10, pool: int = 500,
                     exclude_voted: bool = True) -> List[Dict[str, Any]]:
        """随机刷一批推荐作品。

        流程：
        1. SQLite 随机抽 ``pool * 2`` 部候选（多抽一倍便于过滤已投票）；
        2. 过滤掉已投票作品（可关）；
        3. 若有投票历史：对每部候选计算「正 token 命中权重 − 负 token 命中权重」；
        4. 最终 key = score + uniform(0, jitter)，取 top N（jitter 保证探索性）。

        **线程安全**：整个方法持有 ``store.lock()`` —— GUI 可能同时跑
        「随机刷新」和「相似推荐」两个 worker，共用同一个 sqlite3 连接
        必须串行化，否则会触发 ``Recursive use of cursors not allowed``。
        """
        with self.store.lock():
            return self._random_picks_locked(limit, pool, exclude_voted)

    def _random_picks_locked(self, limit: int, pool: int,
                             exclude_voted: bool) -> List[Dict[str, Any]]:
        conn = self.store.conn
        voted = self.store.voted_ids()
        rows = conn.execute(
            "SELECT id, num, title, path, studio FROM movies "
            "WHERE path IS NOT NULL AND path<>'' "
            "ORDER BY RANDOM() LIMIT ?", (pool * 2,)).fetchall()
        if not rows:
            return []
        if exclude_voted and voted:
            filtered = [r for r in rows if r["id"] not in voted]
            rows = filtered if filtered else rows  # 全投过票就不排除

        def _out(r: Any, score: float = 0.0) -> Dict[str, Any]:
            return {
                "movie_id": r["id"], "num": r["num"] or "",
                "title": r["title"] or "", "path": r["path"] or "",
                "studio": r["studio"] or "", "score": round(score, 3),
            }

        pos, neg = self.profile_tokens()
        if not pos and not neg:
            # 无投票历史 → 纯随机
            return [_out(r) for r in rows[:limit]]

        # 批量拉候选的 tag / actor，避免 N 次小查询
        ids = [r["id"] for r in rows]
        ph = ",".join("?" * len(ids))
        toks_by_mid: Dict[int, Set[str]] = {i: set() for i in ids}
        for r in conn.execute(
                f"SELECT movie_id, tag FROM movie_tags WHERE movie_id IN ({ph})", ids):
            toks_by_mid.setdefault(r["movie_id"], set()).add("tag:" + (r["tag"] or ""))
        for r in conn.execute(
                f"SELECT movie_id, actor FROM movie_actors WHERE movie_id IN ({ph})", ids):
            toks_by_mid.setdefault(r["movie_id"], set()).add("actor:" + (r["actor"] or ""))

        scored: List[Tuple[float, Any]] = []
        for r in rows:
            toks = toks_by_mid.get(r["id"], set())
            if r["studio"]:
                toks.add("studio:" + r["studio"])
            for t in tokenize_title(r["title"] or "", known=self._known_words(),
                                    stopwords=_default_stopwords()):
                toks.add("title:" + t)
            s = 0.0
            for t in toks:
                s += pos.get(t, 0.0) - neg.get(t, 0.0)
            scored.append((s, r))
        # 探索抖动：与最强得分同量级，保证推荐不完全收敛
        max_abs = max((abs(s) for s, _ in scored), default=0.0)
        jitter = max(2.0, max_abs * 0.6)
        scored.sort(key=lambda x: -(x[0] + random.uniform(0, jitter)))
        return [_out(r, s) for s, r in scored[:limit]]

    # ------------------------------------------------------------------
    # 相似推荐（标签 + 演员 Jaccard，按 movie_id 起查）
    # ------------------------------------------------------------------
    def similar_picks(self, movie_id: int, limit: int = 10) -> List[Dict[str, Any]]:
        """与指定作品最相似的 N 部（tag + actor Jaccard）。

        **线程安全**：整段持 ``store.lock()`` 串行化 DB 访问，
        避免与「随机刷新」worker 并发共用同一 sqlite3 连接。
        """
        with self.store.lock():
            return similar_by_id(self.store.conn, movie_id, limit=limit)


def _chunks(seq: List[Any], size: int = 400) -> Any:
    """把长列表切成 SQLite 参数上限以内的块（默认 400 < 999）。"""
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def similar_by_id(conn: Any, movie_id: int, limit: int = 10,
                  pool: int = 200) -> List[Dict[str, Any]]:
    """与指定作品最相似的 N 部（tag + actor Jaccard）—— **批量 SQL 版**。

    :mod:`ai_engine`.``similar_works`` 与 :class:`Recommender`.``similar_picks``
    共用本实现。算法（v1.2.0 重写；旧版逐候选 3 次查询，热门标签下
    候选可达数万部、耗时数分钟）：

    1. 两条 ``GROUP BY`` 聚合查询直接拿到每个候选与目标的
       共享 tag 数 / 共享 actor 数（走 ``idx_tags_tag`` / ``idx_actors_actor``
       索引，需 store schema v1.2.0 自动创建）；
    2. 按「共享总数」预排序，只保留前 ``pool``（默认 200）部进入精算；
    3. 对这 ``pool`` 部**批量**拉全量 tag / actor / 影片行（IN 查询分块），
       计算精确 Jaccard，取 top ``limit``。

    查询次数为 O(标签块数 + 常数)，与库规模无关。
    """
    row = conn.execute(
        "SELECT id, num, title, path, studio, userrating FROM movies WHERE id=?",
        (movie_id,)).fetchone()
    if row is None:
        return []
    t_tags = {r["tag"] for r in conn.execute(
        "SELECT tag FROM movie_tags WHERE movie_id=?", (movie_id,)) if r["tag"]}
    t_actors = {r["actor"] for r in conn.execute(
        "SELECT actor FROM movie_actors WHERE movie_id=?", (movie_id,)) if r["actor"]}
    if not t_tags and not t_actors:
        return []

    inter_tags: Dict[int, int] = {}
    inter_actors: Dict[int, int] = {}
    for chunk in _chunks(sorted(t_tags)):
        ph = ",".join("?" * len(chunk))
        for r in conn.execute(
                f"SELECT movie_id, COUNT(*) AS c FROM movie_tags "
                f"WHERE tag IN ({ph}) AND movie_id<>? GROUP BY movie_id",
                list(chunk) + [movie_id]):
            inter_tags[r["movie_id"]] = r["c"]
    for chunk in _chunks(sorted(t_actors)):
        ph = ",".join("?" * len(chunk))
        for r in conn.execute(
                f"SELECT movie_id, COUNT(*) AS c FROM movie_actors "
                f"WHERE actor IN ({ph}) AND movie_id<>? GROUP BY movie_id",
                list(chunk) + [movie_id]):
            inter_actors[r["movie_id"]] = r["c"]

    # 预排序：共享 tag+actor 总数最多的前 pool 部进入精算
    ranked = sorted(set(inter_tags) | set(inter_actors),
                    key=lambda m: -(inter_tags.get(m, 0) + inter_actors.get(m, 0)))
    ranked = ranked[:pool]
    if not ranked:
        return []

    tags_by: Dict[int, Set[str]] = defaultdict(set)
    actors_by: Dict[int, Set[str]] = defaultdict(set)
    rows_by: Dict[int, Any] = {}
    for chunk in _chunks(ranked):
        ph = ",".join("?" * len(chunk))
        for r in conn.execute(
                f"SELECT movie_id, tag FROM movie_tags WHERE movie_id IN ({ph})",
                chunk):
            if r["tag"]:
                tags_by[r["movie_id"]].add(r["tag"])
        for r in conn.execute(
                f"SELECT movie_id, actor FROM movie_actors WHERE movie_id IN ({ph})",
                chunk):
            if r["actor"]:
                actors_by[r["movie_id"]].add(r["actor"])
        for r in conn.execute(
                f"SELECT id, num, title, path, studio, userrating FROM movies "
                f"WHERE id IN ({ph})", chunk):
            rows_by[r["id"]] = r

    t_set = t_tags | t_actors
    out: List[Dict[str, Any]] = []
    for m in ranked:
        c_tags = tags_by.get(m, set())
        c_actors = actors_by.get(m, set())
        inter = len((t_tags & c_tags) | (t_actors & c_actors))
        if inter < 1:
            continue
        union = len(t_set | c_tags | c_actors)
        j = round(inter / union, 3) if union else 0.0
        mr = rows_by.get(m)
        if mr is None:
            continue
        out.append({
            "movie_id": mr["id"], "num": mr["num"] or "",
            "title": mr["title"] or "", "path": mr["path"] or "",
            "studio": mr["studio"] or "", "userrating": mr["userrating"],
            "score": j,
            "shared_tags": len(t_tags & c_tags),
            "shared_actors": len(t_actors & c_actors),
        })
    out.sort(key=lambda x: (-x["score"], x["num"]))
    return out[:limit]


__all__ = ["Recommender", "similar_by_id"]
