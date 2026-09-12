# -*- coding: utf-8 -*-
"""作品推荐引擎（v1.2.0）—— 基于 👍/👎 反馈的偏好学习 + 加权随机推荐。

设计要点
--------
* **偏好画像**：把用户投过票（``preferences`` 表）的作品的
  ``tag / actor / studio / 标题分词`` 聚合成两份权重表（正 / 负），
  v1.3.4 起再按**全库稀有度（IDF）**衰减，避免「覆盖半个库的泛化标签」霸榜；
* **随机刷新**：SQLite ``ORDER BY RANDOM()`` 抽候选池（v1.3.4 起 2000 部基数），
  按得分**分高 / 中 / 低三档配额**取，档内做 **MMR 多样性重排**，
  并对**最近已推作品**施加时间衰减惩罚 —— 保证「换一批」真的换出不一样的东西；
* **相似推荐**：基于标签 + 演员的 Jaccard 相似度（与 :mod:`ai_engine` 一致思路），
  本模块独立实现一份按 ``movie_id`` 起查的版本，避免循环依赖；
* **零重依赖**：只用标准库 + :mod:`title_tokenizer`。

::

    Copyright © 2026 肆月Aperture 本软件不得用于商业用途，仅做学习交流使用。
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .title_tokenizer import tokenize_title, infer_known_from_store, _default_stopwords


# ----------------------------------------------------------------------
# v1.3.4 推荐质量常量
# ----------------------------------------------------------------------
#: 参考稀有度：覆盖全库 0.1%（且不少于 50 部）的 token 视为「足够精准」，
#: IDF 因子取 1.0 —— 即**不放大任何标签，只衰减泛化标签**。
DF_REF_RATIO = 0.001
DF_REF_MIN = 50
#: IDF 因子下限：泛化标签再怎么衰减也保留一点信号，不直接归零
IDF_FLOOR = 0.05
#: IDF 因子上限：1.0 = 不放大（避免 df=1 的极稀有标签过拟合主导排序）
IDF_CAP = 1.0
#: 标题词没有 df 可查（需 LIKE 全表扫描），统一打折承认其噪声更大
TITLE_DISCOUNT = 0.5
#: 最近已推惩罚：间隔 gap 批 → 惩罚 -jitter × max(0.15, 1/gap)
RECENT_DECAY_FLOOR = 0.15
#: 超过这么多批没再出现就彻底遗忘（同时控制 _seen 体积）
RECENT_FORGET = 12
#: 每档进入 MMR 精算的候选上限（档内先按「得分+抖动」截断，控制计算量）
MMR_POOL = 150


class Recommender:
    """基于用户 👍/👎 反馈的作品推荐器。"""

    def __init__(self, store: Any) -> None:
        self.store = store
        self._known: Optional[Set[str]] = None
        # v1.3.4：token 覆盖数缓存（key=带前缀 token，value=覆盖作品数）
        self._df_cache: Optional[Dict[str, int]] = None
        self._df_cache_n: int = -1
        # v1.3.4：最近已推记忆 movie_id -> 批次号
        self._seen: Dict[int, int] = {}
        self._batch = 0

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
        # v1.3.4-A：按全库稀有度做 IDF 加权，抑制「单体作品/中出/巨乳」这类
        # 覆盖 30%~57% 全库却拿到最高权重的泛化标签
        return self._apply_idf(dict(pos)), self._apply_idf(dict(neg))

    # ------------------------------------------------------------------
    # v1.3.4-A：IDF（逆文档频率）降权
    # ------------------------------------------------------------------
    def _df_lookup(self, tokens: Sequence[str]) -> Dict[str, int]:
        """查各 token 在全库覆盖的作品数（**带缓存**，库规模变化时自动失效）。"""
        n = max(self.store.movie_count(), 1)
        if self._df_cache is None or self._df_cache_n != n:
            self._df_cache, self._df_cache_n = {}, n
        miss = [t for t in tokens if t not in self._df_cache]
        if miss:
            conn = self.store.conn
            for kind, table, col in (("tag", "movie_tags", "tag"),
                                     ("actor", "movie_actors", "actor"),
                                     ("studio", "movies", "studio")):
                vals = [t.split(":", 1)[1] for t in miss
                        if t.startswith(kind + ":")]
                if not vals:
                    continue
                cnt_col = ("COUNT(DISTINCT movie_id)" if table != "movies"
                           else "COUNT(*)")
                for i in range(0, len(vals), 400):
                    chunk = vals[i:i + 400]
                    ph = ",".join("?" * len(chunk))
                    for r in conn.execute(
                            f"SELECT {col} AS k, {cnt_col} AS c FROM {table} "
                            f"WHERE {col} IN ({ph}) GROUP BY {col}", chunk):
                        self._df_cache[f"{kind}:{r['k']}"] = r["c"]
                # 查不到的（数据已删）记为 0，避免每次重查
                for t in miss:
                    if t.startswith(kind + ":") and t not in self._df_cache:
                        self._df_cache[t] = 0
        return self._df_cache

    def _apply_idf(self, weights: Dict[str, float]) -> Dict[str, float]:
        """把原始「投票次数累加权重」乘以 IDF 因子。

        * ``tag`` / ``actor`` / ``studio``：``log(N/df) / log(N/df_ref)``，
          clamp 到 ``[IDF_FLOOR, IDF_CAP]`` —— 只衰减、不放大；
        * ``title``：无 df 可查，统一 ``TITLE_DISCOUNT`` 打折。
        """
        if not weights:
            return {}
        n = max(self.store.movie_count(), 1)
        ref = max(DF_REF_MIN, n * DF_REF_RATIO)
        ref_idf = math.log(n / ref) if n > ref else 1.0
        df = self._df_lookup([t for t in weights if not t.startswith("title:")])
        out: Dict[str, float] = {}
        for t, w in weights.items():
            if t.startswith("title:"):
                out[t] = w * TITLE_DISCOUNT
                continue
            d = df.get(t, 0)
            if d <= 0 or ref_idf <= 0:
                out[t] = w
                continue
            factor = math.log(n / d) / ref_idf
            out[t] = w * min(IDF_CAP, max(IDF_FLOOR, factor))
        return out

    # ------------------------------------------------------------------
    # v1.3.4：随机推荐（IDF 偏好 + 分层采样 + 多样性重排 + 最近已推避让）
    # ------------------------------------------------------------------
    def random_picks(self, limit: int = 10, pool: int = 2000,
                     exclude_voted: bool = True, explore: int = 1,
                     diversity: float = 0.35) -> List[Dict[str, Any]]:
        """随机刷一批推荐作品。

        v1.3.4 四步改造（针对实测「换一批 = 换汤不换药」）：

        1. **A · IDF 加权**（:meth:`_apply_idf`）：偏好权重按标签在库里的稀有度
           衰减，让「单体作品 / 中出」这类覆盖 45%~57% 全库的泛化标签不再霸榜；
        2. **B · 最近已推避让**（:meth:`_recent_penalty`）：距上次出现越近惩罚
           越重，直接消灭「刚看过又来」；
        3. **D · 分层采样**：候选按得分分位分高 / 中 / 低三档配额取，
           低档为探索位（且排除负分作品），避免只推同一撮作品；
        4. **C · 档内 MMR 多样性重排**（:meth:`_mmr_select`）：贪心
           ``(1−λ)·相关性 − λ·与已选最大相似度``，强制同批作品拉开差异。

        :param pool: 候选池基数（实际抽 ``pool * 2`` 部）。v1.3.4 从 500 提到
            **2000**，候选覆盖从全库约 2% 提到约 8%。
        :param explore: 探索位数量（从最低分档取），默认 1。
        :param diversity: MMR 多样性权重，0=只顾相关性、1=只顾差异，默认 0.35。

        **线程安全**：整个方法持有 ``store.lock()`` —— GUI 可能同时跑
        「随机刷新」和「相似推荐」两个 worker，共用同一个 sqlite3 连接
        必须串行化，否则会触发 ``Recursive use of cursors not allowed``。
        """
        with self.store.lock():
            return self._random_picks_locked(limit, pool, exclude_voted,
                                             explore, diversity)

    # ---- v1.3.4-B：最近已推避让 ----
    def reset_recent(self) -> None:
        """清空「最近已推」记忆（换库 / 清空投票后可调用）。"""
        self._seen.clear()
        self._batch = 0

    def _recent_penalty(self, movie_id: int, jitter: float) -> float:
        """距上次出现 ``gap`` 批 → 惩罚 ``-jitter × max(0.15, 1/gap)``。"""
        gap = self._batch - self._seen.get(movie_id, -9999)
        if gap > RECENT_FORGET or gap <= 0:
            return 0.0
        return -jitter * max(RECENT_DECAY_FLOOR, 1.0 / gap)

    def _remember(self, movie_ids: Sequence[int]) -> None:
        """登记本批已推作品，并定期瘦身防止长期运行无限增长。"""
        self._batch += 1
        for mid in movie_ids:
            self._seen[mid] = self._batch
        if len(self._seen) > 2000:
            cutoff = self._batch - RECENT_FORGET
            self._seen = {k: v for k, v in self._seen.items() if v >= cutoff}

    # ---- v1.3.4-C：批内 MMR 多样性重排 ----
    @staticmethod
    def _mmr_select(cands: Sequence[int], base: Dict[int, float],
                    toks: Dict[int, Set[str]], k: int,
                    diversity: float) -> List[int]:
        """MMR 贪心：``(1−λ)·相关性 − λ·max_sim(已选)``。

        ``cands`` 需已按「相关性 + 抖动」降序传入；``base`` 为原始相关性得分，
        会在本方法内按传入候选做 min-max 归一化。
        """
        if not cands or k <= 0:
            return []
        vals = [base.get(m, 0.0) for m in cands]
        lo = min(vals)
        span = (max(vals) - lo) or 1.0

        picked = [cands[0]]
        picked_toks = [toks.get(cands[0], set())]
        rest = list(cands[1:])
        while len(picked) < k and rest:
            best_i, best_v = 0, -1e18
            for i, m in enumerate(rest):
                t = toks.get(m, set())
                sim = 0.0
                if t:
                    sim = max(((len(t & pt) / len(t | pt)) if (t | pt) else 0.0)
                              for pt in picked_toks)
                v = (1.0 - diversity) * ((base.get(m, 0.0) - lo) / span) \
                    - diversity * sim
                if v > best_v:
                    best_v, best_i = v, i
            picked.append(rest.pop(best_i))
            picked_toks.append(toks.get(picked[-1], set()))
        return picked

    def _random_picks_locked(self, limit: int, pool: int,
                             exclude_voted: bool, explore: int,
                             diversity: float) -> List[Dict[str, Any]]:
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
            # 无投票历史 → 纯随机（仍登记，避免连续两批撞车）
            picks = [_out(r) for r in rows[:limit]]
            self._remember([p["movie_id"] for p in picks])
            return picks

        # 批量拉候选的 tag / actor，避免 N 次小查询（分块规避参数上限）
        ids = [r["id"] for r in rows]
        toks_by_mid: Dict[int, Set[str]] = {i: set() for i in ids}
        for chunk in _chunks(ids, 400):
            ph = ",".join("?" * len(chunk))
            for r in conn.execute(
                    f"SELECT movie_id, tag FROM movie_tags "
                    f"WHERE movie_id IN ({ph})", chunk):
                toks_by_mid.setdefault(r["movie_id"], set()).add(
                    "tag:" + (r["tag"] or ""))
            for r in conn.execute(
                    f"SELECT movie_id, actor FROM movie_actors "
                    f"WHERE movie_id IN ({ph})", chunk):
                toks_by_mid.setdefault(r["movie_id"], set()).add(
                    "actor:" + (r["actor"] or ""))

        raw: Dict[int, float] = {}
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
            raw[r["id"]] = s
        rows_by_id = {r["id"]: r for r in rows}

        # 探索抖动：与最强得分同量级（IDF 后整体尺度变小，jitter 自适应）
        max_abs = max((abs(s) for s in raw.values()), default=0.0)
        jitter = max(2.0, max_abs * 0.6)

        # ---- v1.3.4-B：叠加「最近已推」惩罚 ----
        base = {m: s + self._recent_penalty(m, jitter) for m, s in raw.items()}

        # ---- v1.3.4-D：按分位数分高 / 中 / 低三档 ----
        ordered = sorted(base, key=lambda m: -base[m])
        n = len(ordered)
        hi_end = int(n * 0.30)
        mid_end = int(n * 0.75)
        tiers = [ordered[:hi_end], ordered[hi_end:mid_end], ordered[mid_end:]]
        # 探索位只从「非负面」作品里取，避免推到用户明确讨厌的类型
        tiers[2] = [m for m in tiers[2] if base[m] >= 0.0]

        exp_n = min(max(explore, 0), max(0, limit // 6))
        mid_n = max(0, (limit - exp_n) * 2 // 5)
        quota = [limit - exp_n - mid_n, mid_n, exp_n]

        picks: List[int] = []
        for tier, q in zip(tiers, quota):
            if q <= 0 or not tier:
                continue
            # 档内抖动 → 取前 MMR_POOL 部做 MMR 精算（控制计算量）
            keyed = sorted(tier, key=lambda m: -(base[m] + random.uniform(0, jitter)))
            picks.extend(self._mmr_select(keyed[:MMR_POOL], base, toks_by_mid,
                                          q, diversity))
        # 配额没填满（候选太少）时用剩余高分补齐
        if len(picks) < limit:
            have = set(picks)
            picks.extend([m for m in ordered if m not in have][:limit - len(picks)])
        picks = picks[:limit]

        self._remember(picks)
        return [_out(rows_by_id[m], raw[m]) for m in picks if m in rows_by_id]

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
