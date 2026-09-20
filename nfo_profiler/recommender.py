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
from datetime import datetime, date, timedelta
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

# ----------------------------------------------------------------------
# v1.3.7 推荐质量常量
# ----------------------------------------------------------------------
#: 近半年窗口（天）：作品 premiered（优先）/ dateadded（回退）落在此窗口内 → 加权
RECENT_DAYS = 180
#: 近半年加权强度（相对 jitter 的倍数）：freshest ≈ FRAC×jitter，窗口边缘仍有
#: 0.45×FRAC×jitter 保底；随偏好强度自适应，保证「近期作品」稳定上浮而不被淹没。
RECENT_BOOST_FRAC = 0.7
#: 多次浏览降权阈值：play_history.play_count >= 此值且未投票 → 开始降权
BROWSE_PENALTY_THRESHOLD = 3
#: 每超出阈值 1 次，额外 -STEP×jitter（相对偏好尺度，随投票量自适应）
BROWSE_PENALTY_STEP = 0.12
#: 多次浏览降权上限（×jitter，避免把某作品一次打到无限负）
BROWSE_PENALTY_CAP = 1.2


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
        # v1.4.4：构成要求「点赞演员共享」的演员级轮换记忆 actor -> 批次号，
        # 连续 RECENT_FORGET 批内不再选中同一演员（避免批批都是同一位点赞演员）
        self._req_actors: Dict[str, int] = {}

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
        """一部作品的 token 集合：tag / actor / director / studio / 标题分词，带类型前缀避免撞名。

        v1.4.5 起纳入 ``director:``（导演维度）—— 与向量编辑模块的维度一致。
        """
        conn = self.store.conn
        toks: Set[str] = set()
        for r in conn.execute("SELECT tag FROM movie_tags WHERE movie_id=?", (movie_id,)):
            if r["tag"]:
                toks.add("tag:" + r["tag"])
        for r in conn.execute("SELECT actor FROM movie_actors WHERE movie_id=?", (movie_id,)):
            if r["actor"]:
                toks.add("actor:" + r["actor"])
        for r in conn.execute(
                "SELECT director FROM movie_directors WHERE movie_id=?", (movie_id,)):
            if r["director"]:
                toks.add("director:" + r["director"])
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

    def _profile_tokens_locked(self, include_overrides: bool = True) -> Tuple[Dict[str, float], Dict[str, float]]:
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
                w = 2.0 if kind in ("tag", "actor", "director") else 1.0
                target[t] += w
        # v1.3.4-A：按全库稀有度做 IDF 加权，抑制「单体作品/中出/巨乳」这类
        # 覆盖 30%~57% 全库却拿到最高权重的泛化标签
        pos = self._apply_idf(dict(pos))
        neg = self._apply_idf(dict(neg))
        # v1.4.5：合并手动向量（向量编辑模块）—— 显式用户意图，不做 IDF 衰减
        if include_overrides:
            self._apply_vector_overrides(pos, neg)
        return pos, neg

    def _apply_vector_overrides(self, pos: Dict[str, float],
                                neg: Dict[str, float]) -> None:
        """把 ``vector_overrides`` 手动向量就地合并进正 / 负权重表（v1.4.5）。

        * ``weight > 0``：加入正表（叠加到自动权重之上）；
        * ``weight < 0``：加入负表（软排斥）；
        * ``weight == 0``：**屏蔽** —— 把该 token 的自动正负权重清零（即"删除自动向量"）。

        调用方需已持有 ``store.lock()``（``_profile_tokens_locked`` / ``vector_snapshot``
        均在锁内调用本方法）。
        """
        try:
            overrides = self.store.vector_overrides()
        except Exception:
            overrides = []
        for o in (overrides or []):
            t = f"{o['kind']}:{o['name']}"
            try:
                w = float(o.get("weight") or 0.0)
            except (TypeError, ValueError):
                w = 0.0
            if w > 0:
                pos[t] = pos.get(t, 0.0) + w
            elif w < 0:
                neg[t] = neg.get(t, 0.0) + (-w)
            else:
                pos.pop(t, None)
                neg.pop(t, None)

    def vector_snapshot(self) -> Dict[str, Any]:
        """向量编辑模块的数据源（v1.4.5）：自动画像 + 手动覆盖 + 生效权重。

        返回::

            {
              "auto_pos":  {token: 正权重}   # 仅投票画像（IDF 后，未含手动向量）
              "auto_neg":  {token: 负权重}
              "pos":       {token: 生效正权重}  # 合并手动向量后
              "neg":       {token: 生效负权重}
              "manual":    [{kind, name, weight, note, updated_at}, ...]
            }

        **线程安全**：整段持 ``store.lock()``。
        """
        with self.store.lock():
            pos, neg = self._profile_tokens_locked(include_overrides=False)
            epos, eneg = dict(pos), dict(neg)
            self._apply_vector_overrides(epos, eneg)
            try:
                manual = self.store.vector_overrides()
            except Exception:
                manual = []
            return {"auto_pos": pos, "auto_neg": neg,
                    "pos": epos, "neg": eneg, "manual": manual}

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
                                     ("director", "movie_directors", "director"),
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
                     diversity: float = 0.35, *,
                     recent_boost_on: bool = True,
                     browse_demote_on: bool = True,
                     keyword: Optional[str] = None) -> List[Dict[str, Any]]:
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
        :param recent_boost_on: v1.3.7 之①——开启近半年作品加权（基于 premiered /
            dateadded），越近加分越多；默认开。
        :param browse_demote_on: v1.3.7 之②——开启「多次浏览但一直没投票」作品降权
            （读 play_history.play_count）；默认开。👎 作品由本方法硬排除，不受此开关影响。
        :param keyword: v1.4.0——关键词偏向。命中「演员 / 标签 / 番号 / 标题」任一字段
            的作品会被强制拉进候选池并整体上浮，让本批推荐明显偏向该关键词；
            为空 / None 时不生效。

        **线程安全**：整个方法持有 ``store.lock()`` —— GUI 可能同时跑
        「随机刷新」和「相似推荐」两个 worker，共用同一个 sqlite3 连接
        必须串行化，否则会触发 ``Recursive use of cursors not allowed``。
        """
        with self.store.lock():
            return self._random_picks_locked(
                limit, pool, exclude_voted, explore, diversity,
                recent_boost_on, browse_demote_on, keyword=keyword)

    # ---- v1.3.4-B：最近已推避让 ----
    def reset_recent(self) -> None:
        """清空「最近已推」记忆（换库 / 清空投票后可调用）。"""
        self._seen.clear()
        self._batch = 0
        self._req_actors.clear()

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

    # ---- v1.3.7：近半年加权 + 多次浏览未投降权 ----
    @staticmethod
    def _parse_recent_date(row: Any) -> Optional[date]:
        """取作品的「时间锚点」：优先 premiered（作品发行日），缺失则回退 dateadded。

        注意：候选行是 ``sqlite3.Row``，它**没有** ``.get`` 方法，必须用 ``row[key]``
        （try/except），否则永远取不到值 → 加权失效。
        """
        for key in ("premiered", "dateadded"):
            try:
                v = row[key]
            except (KeyError, IndexError, TypeError):
                v = None
            if not v:
                continue
            s = str(v).strip()
            if len(s) >= 10 and s[4] == "-" and s[7] == "-":
                try:
                    return datetime.strptime(s[:10], "%Y-%m-%d").date()
                except ValueError:
                    continue
        return None

    def _recency_boost(self, row: Any, jitter: float) -> float:
        """近半年作品加分：窗口内整体加权（非越近才加）。

        以 ``jitter``（偏好得分尺度）为基准：freshest ≈ FRAC×jitter，窗口边缘仍有
        0.45×FRAC×jitter 保底，保证「近半年」整段作品都被加权；窗口外不加分。
        """
        d = self._parse_recent_date(row)
        if d is None:
            return 0.0
        days_ago = (datetime.now().date() - d).days
        if days_ago < 0 or days_ago > RECENT_DAYS:
            return 0.0
        frac = 1.0 - days_ago / RECENT_DAYS   # 今天=1.0 → 180 天前=0.0
        return jitter * RECENT_BOOST_FRAC * (0.45 + 0.55 * frac)

    @staticmethod
    def _browse_penalty(movie_id: int, play_counts: Dict[int, int],
                        voted: Dict[int, int], jitter: float) -> float:
        """多次浏览但一直没投票 → 降权；已投票（含 👍）不降。

        * 已点 👍：用户喜欢，保留推荐权重，不降；
        * 已点 👎：调用方已硬排除（见 ``_random_picks_locked``），不会进来；
        * 未投票且浏览达阈值：每超出 1 次 -STEP×jitter，封顶 CAP×jitter。
        """
        if movie_id in voted:
            return 0.0
        pc = play_counts.get(movie_id, 0)
        over = pc - BROWSE_PENALTY_THRESHOLD
        if over <= 0:
            return 0.0
        return -min(BROWSE_PENALTY_CAP, BROWSE_PENALTY_STEP * over) * jitter

    # ---- v1.4.0：关键词 / 近半年 / 点赞演员 检索辅助 ----
    def _keyword_ids(self, keyword: str) -> Set[int]:
        """返回命中「演员 / 标签 / 番号 / 标题」任一字段的作品 id 集合。

        关键词做 LIKE 模糊匹配（前缀/包含皆可），并对 ``%`` ``_`` 转义，
        避免用户输入通配符时语义异常。连接已被调用方持锁，这里只查不改。
        """
        kw = (keyword or "").strip()
        if not kw:
            return set()
        pat = "%" + kw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        ids: Set[int] = set()
        conn = self.store.conn
        for sql, n in (
            ("SELECT movie_id FROM movie_tags WHERE tag LIKE ? ESCAPE '\\'", 1),
            ("SELECT movie_id FROM movie_actors WHERE actor LIKE ? ESCAPE '\\'", 1),
            ("SELECT id FROM movies WHERE title LIKE ? ESCAPE '\\' "
             "OR num LIKE ? ESCAPE '\\'", 2),
        ):
            params = (pat, pat) if n == 2 else (pat,)
            for r in conn.execute(sql, params):
                ids.add(int(r[0]))
        return ids

    def _recent_ids(self, days: int, exclude: Set[int], cap: int) -> List[int]:
        """返回最近 ``days`` 天内（premiered 优先，回退 dateadded）的作品 id 列表。

        ``exclude`` 中的 id 直接跳过；最多返回 ``cap`` 部，按 dateadded 倒序
        （最新在前）。连接已被调用方持锁。
        """
        cutoff = (datetime.now().date() - timedelta(days=days)).strftime("%Y-%m-%d")
        conn = self.store.conn
        out: List[int] = []
        seen = set(exclude)
        for r in conn.execute(
                "SELECT id FROM movies WHERE path IS NOT NULL AND path<>'' "
                "AND (substr(premiered,1,10) >= ? OR substr(dateadded,1,10) >= ?) "
                "ORDER BY substr(dateadded,1,10) DESC LIMIT ?",
                (cutoff, cutoff, max(1, cap) * 4)):
            mid = int(r["id"])
            if mid in seen:
                continue
            seen.add(mid)
            out.append(mid)
            if len(out) >= cap:
                break
        return out

    def _liked_actor_ids(self, exclude: Set[int], cap: int) -> List[int]:
        """返回与任意 👍 作品共享演员的作品 id 列表（排除 exclude）。

        v1.4.4 演员级轮换：v1.4.0 版本按 ``sorted(actors)`` 字母序遍历演员、
        内层查询无随机且无演员级记忆，导致每批恒定命中同一位点赞演员
        （实测批批都是同一位，如 JULIA）。现改为：

        * 一次查询构建 ``actor -> [movie_id, ...]`` 映射；
        * **演员级避让**：近 ``RECENT_FORGET`` 批内已作为该要求来源的演员
          （``_req_actors``）不再选中；若所有演员都在避让期内（极端情况），
          清空避让重新随机，保证构成要求仍可兜底不空转；
        * 从剩余候选中**随机**选演员、再从其作品中**随机**取 ``cap`` 部；
        * 选中后登记该演员批次号，并随 ``_seen`` 同步瘦身。
        """
        voted = self.store.voted_ids()
        up_ids = [mid for mid, v in voted.items() if v and v > 0]
        if not up_ids:
            return []
        conn = self.store.conn
        # 第一步：点赞作品的演员集合
        actors: Set[str] = set()
        for chunk in _chunks(up_ids):
            ph = ",".join("?" * len(chunk))
            for r in conn.execute(
                    f"SELECT actor FROM movie_actors "
                    f"WHERE movie_id IN ({ph}) AND actor IS NOT NULL AND actor<>''",
                    chunk):
                actors.add(r["actor"])
        if not actors:
            return []
        # 第二步：这些演员的**全库**作品（与点赞作品共享演员的其他作品）
        by_actor: Dict[str, List[int]] = {a: [] for a in actors}
        for chunk in _chunks(sorted(actors)):
            ph = ",".join("?" * len(chunk))
            for r in conn.execute(
                    f"SELECT DISTINCT movie_id, actor FROM movie_actors "
                    f"WHERE actor IN ({ph})", chunk):
                if r["actor"] in by_actor:
                    by_actor[r["actor"]].append(int(r["movie_id"]))
        # 演员级避让：RECENT_FORGET 批内已用过的演员不再选中
        actors = [a for a in by_actor
                  if (self._batch - self._req_actors.get(a, -9999)) > RECENT_FORGET]
        if not actors:
            # 所有演员都在避让期内 → 清空避让（防死锁），保证构成要求仍可兜底
            self._req_actors.clear()
            actors = list(by_actor)
        random.shuffle(actors)
        out: List[int] = []
        seen = set(exclude)
        for actor in actors:
            mids = list(by_actor[actor])
            random.shuffle(mids)
            picked = 0
            for mid in mids:
                if mid in seen:
                    continue
                seen.add(mid)
                out.append(mid)
                picked += 1
                if len(out) >= cap:
                    break
            if picked:
                self._req_actors[actor] = self._batch
            if len(out) >= cap:
                break
        # 瘦身：与 _seen 同周期清理过期演员记忆
        cutoff = self._batch - RECENT_FORGET
        self._req_actors = {a: b for a, b in self._req_actors.items() if b >= cutoff}
        return out

    def _attach_actors(self, picks: List[Dict[str, Any]]) -> None:
        """给推荐结果批量补「演员名」（v1.4.1：卡片副标题由片商改为演员）。

        只填 ``actors`` 键缺失（None）的条目，已带演员的结果不重复查询；
        分块 IN 查询，一次推荐最多 18 部，开销可忽略。
        """
        ids = [p["movie_id"] for p in picks if p.get("actors") is None]
        if not ids:
            return
        by_mid: Dict[int, List[str]] = {}
        for chunk in _chunks(ids):
            ph = ",".join("?" * len(chunk))
            for r in self.store.conn.execute(
                    f"SELECT movie_id, actor FROM movie_actors "
                    f"WHERE movie_id IN ({ph}) ORDER BY rowid", chunk):
                if r["actor"]:
                    by_mid.setdefault(int(r["movie_id"]), []).append(r["actor"])
        for p in picks:
            if p.get("actors") is None:
                p["actors"] = "、".join(by_mid.get(p["movie_id"], []))

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
                             diversity: float,
                             recent_boost_on: bool = True,
                             browse_demote_on: bool = True,
                             keyword: Optional[str] = None,
                             *, remember: bool = True) -> List[Dict[str, Any]]:
        conn = self.store.conn
        voted = self.store.voted_ids()
        # v1.3.7 之三：👎 硬排除（即便 exclude_voted=False 也绝不进入随机推荐）
        down_ids = {mid for mid, v in voted.items() if v < 0}
        rows = conn.execute(
            "SELECT id, num, title, path, studio, premiered, dateadded, year "
            "FROM movies WHERE path IS NOT NULL AND path<>'' "
            "ORDER BY RANDOM() LIMIT ?", (pool * 2,)).fetchall()
        if not rows:
            return []
        # 排除已投票（默认开启）
        if exclude_voted and voted:
            filtered = [r for r in rows if r["id"] not in voted]
            rows = filtered if filtered else rows
        # 👎 硬排除
        if down_ids:
            filtered = [r for r in rows if r["id"] not in down_ids]
            rows = filtered if filtered else rows
        if not rows:
            return []
        rows_by_id = {r["id"]: r for r in rows}

        # v1.4.0：关键词偏向（演员 / 标签 / 番号 / 标题）
        kw_ids: Set[int] = set()
        if keyword:
            kw_ids = self._keyword_ids(keyword)
            if kw_ids:
                # 👎 命中的关键词作品绝不进随机推荐
                kw_ids -= down_ids
                missing = [mid for mid in kw_ids if mid not in rows_by_id]
                if missing:
                    # 把关键词命中的作品补进候选池（限制上限，避免池子爆炸）
                    cap = pool * 2
                    ph = ",".join("?" * len(missing[:cap]))
                    for r in conn.execute(
                            "SELECT id, num, title, path, studio, premiered, dateadded, year "
                            f"FROM movies WHERE id IN ({ph}) AND path IS NOT NULL AND path<>''",
                            missing[:cap]):
                        rows.append(r)
                        rows_by_id[r["id"]] = r

        rows_by_id = {r["id"]: r for r in rows}

        def _out(r: Any, score: float = 0.0) -> Dict[str, Any]:
            return {
                "movie_id": r["id"], "num": r["num"] or "",
                "title": r["title"] or "", "path": r["path"] or "",
                "studio": r["studio"] or "", "score": round(score, 3),
            }

        # 候选范围内的浏览次数（v1.3.7 之二：多次浏览未投降权）
        ids = list(rows_by_id)
        play_counts: Dict[int, int] = {}
        for chunk in _chunks(ids, 400):
            ph = ",".join("?" * len(chunk))
            for r in conn.execute(
                    f"SELECT movie_id, play_count FROM play_history "
                    f"WHERE movie_id IN ({ph})", chunk):
                play_counts[int(r["movie_id"])] = int(r["play_count"])

        pos, neg = self.profile_tokens()
        if not pos and not neg:
            # 无投票历史：按「近半年优先 + 多次浏览未投降权」弱排序；仍登记防撞车
            scored = []
            for m in ids:
                s = 0.0
                if recent_boost_on:
                    s += self._recency_boost(rows_by_id[m], 2.0)
                if browse_demote_on:
                    s += self._browse_penalty(m, play_counts, voted, 2.0)
                # v1.4.0 关键词偏向：命中的作品整体上浮，确保出现在结果里
                if kw_ids and m in kw_ids:
                    s += 4.0
                scored.append((m, s))
            scored.sort(key=lambda x: -x[1])
            picks = [m for m, _ in scored[:limit]]
            if remember:
                self._remember(picks)
            out_rows = [_out(rows_by_id[m]) for m in picks]
            self._attach_actors(out_rows)
            return out_rows

        # ---- 有投票历史 ----
        # 批量拉候选的 tag / actor，避免 N 次小查询（分块规避参数上限）
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

        # 探索抖动：与最强得分同量级（IDF 后整体尺度变小，jitter 自适应）
        max_abs = max((abs(s) for s in raw.values()), default=0.0)
        jitter = max(2.0, max_abs * 0.6)

        # v1.4.0 关键词偏向：命中的作品整体上浮，确保出现在结果里
        if kw_ids:
            kbonus = jitter * 2.0
            for mid in kw_ids:
                if mid in raw:
                    raw[mid] += kbonus

        # 合并四类得分：偏好 + 最近已推避让 + 近半年加权 + 多次浏览降权
        base: Dict[int, float] = {}
        for m, s in raw.items():
            b = s + self._recent_penalty(m, jitter)
            if recent_boost_on:
                b += self._recency_boost(rows_by_id[m], jitter)
            if browse_demote_on:
                b += self._browse_penalty(m, play_counts, voted, jitter)
            base[m] = b

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

        if remember:
            self._remember(picks)
        out_rows = [_out(rows_by_id[m], raw[m]) for m in picks if m in rows_by_id]
        self._attach_actors(out_rows)
        return out_rows

    # ------------------------------------------------------------------
    # 相似推荐（标签 + 演员 Jaccard，按 movie_id 起查）
    # ------------------------------------------------------------------
    def similar_picks(self, movie_id: int, limit: int = 10) -> List[Dict[str, Any]]:
        """与指定作品最相似的 N 部（tag + actor Jaccard）。

        **线程安全**：整段持 ``store.lock()`` 串行化 DB 访问，
        避免与「随机刷新」worker 并发共用同一 sqlite3 连接。
        """
        with self.store.lock():
            out = similar_by_id(self.store.conn, movie_id, limit=limit)
            self._attach_actors(out)   # v1.4.1：相似推荐卡片副标题用演员名
            return out


    def smart_picks(self, limit: int = 18, explore: int = 3,
                    diversity: float = 0.5,
                    keyword: Optional[str] = None) -> List[Dict[str, Any]]:
        """智能推荐（个性化 / 更多元 / 构成要求软性补充）—— ④推荐 Tab 的「智能推荐」模块来源。

        v1.4.0 在 v1.3.9「18 部（3 行 × 6 列）」基础上，进一步在智能推荐中**加入构成要求**：

        * **个性化基础流**（占绝大多数）：在 :meth:`random_picks` 基础上加大探索位与多样性权重，
          产出贴合偏好、彼此差异更大的推荐（继承 v1.3.4 的 A/B/C/D 与 v1.3.7 的①②③）；
          正常轮换由它驱动（含最近已推避让）。
        * **构成要求（软性补充，不强制置顶、不参与轮换抢占）**：在满足基础流之后，
          尽量让结果覆盖「近半年 ≥2 部、近 1 月 ≥2 部、与任意 👍 作品共享演员 ≥1 部」；
          这些是**基础流未自然覆盖时的兜底补齐**，而非强制保障位。
          v1.4.4 起「点赞演员共享」引入**演员级轮换**：随机选演员 + 近期已用演员避让，
          不再批批命中同一位点赞演员。
        * 全程硬排除 👎 作品、去重（按 movie_id）；补齐项同样避让「最近已推」，
          使「再推荐一批」正常换内容、正常轮换，不被这几条要求锁死。
        * 若数据库不足以凑齐构成要求（如近期作品太少），则尽力填充，不强行凑数。

        :param keyword: v1.4.0 关键词偏向（演员 / 标签 / 番号 / 标题），透传给
            :meth:`random_picks`，让基础流明显偏向该关键词。

        **线程安全**：整段持 ``store.lock()``（与 random_picks / similar_picks 共用
        同一 sqlite3 连接，必须串行化）。
        """
        with self.store.lock():
            return self._smart_picks_locked(
                limit, explore, diversity, keyword)

    def _smart_picks_locked(self, limit: int, explore: int,
                            diversity: float,
                            keyword: Optional[str]) -> List[Dict[str, Any]]:
        conn = self.store.conn
        # 1) 个性化基础流（含关键词偏向、👎 硬排除、最近已推避让）—— 正常轮换由它驱动。
        #    此处 remember=False：由本方法在末尾对「整批统一结果」登记一次，保证
        #    基础流与补齐项一视同仁地进入「最近已推」，轮换一致、不会双重计数批号。
        general = self._random_picks_locked(
            limit=limit, pool=2000, exclude_voted=True, explore=explore,
            diversity=diversity, recent_boost_on=True, browse_demote_on=True,
            keyword=keyword, remember=False)
        voted = self.store.voted_ids()
        down_ids = {mid for mid, v in voted.items() if v and v < 0}
        # 已投票（👍/👎）永不进智能推荐；同时避让「最近已推」，保证「再推荐一批」正常轮换
        recently_pushed = {mid for mid, b in self._seen.items()
                           if (self._batch - b) <= RECENT_FORGET}
        used: Set[int] = set(voted.keys()) | recently_pushed

        def _row(mid: int) -> Optional[Dict[str, Any]]:
            r = conn.execute(
                "SELECT id, num, title, path, studio, premiered, dateadded, year "
                "FROM movies WHERE id=?", (mid,)).fetchone()
            if r is None:
                return None
            return {
                "movie_id": r["id"], "num": r["num"] or "",
                "title": r["title"] or "", "path": r["path"] or "",
                "studio": r["studio"] or "", "score": 0.0,
            }

        # 2) 构成要求：近半年 ≥2、近 1 月 ≥2、点赞演员共享 ≥1（软性补充，仅兜底补齐）
        #    这些作品**追加到基础流之后**（不抢占前排、不强制置顶），且同样避让
        #    「最近已推」—— 若基础流已自然覆盖则不再强塞，轮换不受影响。
        final_ids: Set[int] = {p["movie_id"] for p in general}
        req: List[Dict[str, Any]] = []
        req_ids: Set[int] = set()

        def _collect(ids: List[int]) -> None:
            for mid in ids:
                if mid in used or mid in final_ids or mid in req_ids:
                    continue
                row = _row(mid)
                if row is None:
                    continue
                req.append(row)
                req_ids.add(row["movie_id"])

        _collect(self._recent_ids(RECENT_DAYS, used | final_ids, 2))
        _collect(self._recent_ids(30, used | final_ids, 2))
        _collect(self._liked_actor_ids(used | final_ids, 1))

        # 组装：保留基础流前排，构成要求项紧随其后（恒被保留），总数裁到 limit。
        # 若基础流本身已超过「limit − len(req)」，只多保留前排，被裁掉的只是基础流相对低优先的尾部。
        keep_general = max(0, limit - len(req))
        final: List[Dict[str, Any]] = list(general[:keep_general]) + req
        final_ids = {p["movie_id"] for p in final}

        # 3) 仍不足用随机兜底（避让 used，含已投票与最近已推）
        if len(final) < limit:
            need = limit - len(final)
            ph = ",".join("?" * len(used)) or "0"
            for r in conn.execute(
                    "SELECT id, num, title, path, studio, premiered, dateadded, year "
                    "FROM movies WHERE path IS NOT NULL AND path<>'' "
                    f"AND id NOT IN ({ph}) ORDER BY RANDOM() LIMIT ?",
                    list(used) + [need]):
                mid = int(r["id"])
                if mid in used or mid in final_ids:
                    continue
                final.append({
                    "movie_id": mid, "num": r["num"] or "",
                    "title": r["title"] or "", "path": r["path"] or "",
                    "studio": r["studio"] or "", "score": 0.0,
                })
                final_ids.add(mid)
                if len(final) >= limit:
                    break

        # 整批统一登记「最近已推」一次：基础流与补齐项轮换一致，下一次「再推荐一批」正常换内容
        self._remember([p["movie_id"] for p in final])
        self._attach_actors(final)   # v1.4.1：补齐 / 兜底条目也补演员名
        return final[:limit]


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
