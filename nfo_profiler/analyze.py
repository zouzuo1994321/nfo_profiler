# -*- coding: utf-8 -*-
"""分析层：从数据库里算出「用户画像」和各类高频词统计。

输出结构统一为可 JSON 序列化的 dict，供 HTML 报告 / CSV / XLSX / Markdown 复用。
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .normalize import Normalizer, extract_keywords
from .store import Store

# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

_MONTH_PAT = re.compile(r"^(\d{4})-(\d{2})")
_HOUR_PAT = re.compile(r"\s(\d{2}):")


def _pct(part: int, total: int) -> float:
    return round(part * 100.0 / total, 2) if total else 0.0


def _safe_div(a: float, b: float) -> float:
    return a / b if b else 0.0


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


class Analyzer:
    """对已入库的 NFO 数据做统计分析。"""

    def __init__(
        self,
        store: Store,
        normalizer: Optional[Normalizer] = None,
        source: Optional[str] = None,
    ) -> None:
        """Parameters
        ----------
        source:
            限定只统计某个数据源（根目录绝对路径）。为空则统计全部数据源。
        """
        self.store = store
        self.conn = store.conn
        self.norm = normalizer or store.norm
        self.source = source or ""

    # -- 数据源过滤 -----------------------------------------------------
    def _mv(self, alias: str = "m") -> str:
        """movies 表的过滤条件（供 JOIN 使用）。"""
        return f"{alias}.source = ?" if self.source else ""

    def _sub(self, alias: str = "t") -> str:
        """子表（movie_tags 等）的过滤条件：限定 movie_id 属于该数据源。"""
        return (f"{alias}.movie_id IN (SELECT id FROM movies WHERE source = ?)"
                if self.source else "")

    def _allowed_ids(self) -> Optional[set]:
        """该数据源下的 movie_id 集合；未限定数据源时返回 None（表示全部放行）。"""
        if not self.source:
            return None
        return {r["id"] for r in self.conn.execute(
            "SELECT id FROM movies WHERE source = ?", self._p())}

    def _p(self) -> List[Any]:
        return [self.source] if self.source else []

    # ------------------------------------------------------------------
    # 频次：按归一化键聚合，展示名取出现最多的写法
    # ------------------------------------------------------------------
    def _freq(
        self,
        table: str,
        key_col: str,
        val_col: str,
        *,
        top_n: int = 30,
        min_count: int = 1,
        where: str = "",
        join_movies: bool = False,
        map_fn=None,
    ) -> List[Dict[str, Any]]:
        """按归一化键做频次聚合。

        关键：分组键必须**现算**而不是读入库时写死的 tag_key/actor_key——
        否则改了同义词表后必须重新索引才生效，用户体验很差。
        map_fn 用于在分组前做同义词 / 别名归并。
        """
        sql = f"SELECT t.{key_col} k, t.{val_col} v, COUNT(*) c FROM {table} t"
        if join_movies:
            sql += " JOIN movies m ON m.id = t.movie_id"
        conds = []
        params: List[Any] = []
        if where:
            conds.append(where)
        sub = self._sub()
        if sub:
            conds.append(sub)
            params.append(self.source)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += f" GROUP BY t.{key_col}, t.{val_col}"

        groups: Dict[str, List[Tuple[str, int]]] = defaultdict(list)
        for row in self.conn.execute(sql, params):
            v = row["v"] or ""
            if not v:
                continue
            if map_fn is not None:
                v = map_fn(v)
                if not v:
                    continue
            k = self.norm.variant_key(v) or v
            if not k:
                continue
            groups[k].append((v, row["c"]))

        out: List[Dict[str, Any]] = []
        for k, items in groups.items():
            items.sort(key=lambda x: (-x[1], len(x[0]), x[0]))
            total = sum(c for _, c in items)
            if total < min_count:
                continue
            out.append({
                "key": k,
                "name": items[0][0],
                "count": total,
                "aliases": [v for v, _ in items[1:3]],
            })
        out.sort(key=lambda x: (-x["count"], x["name"]))
        return out[:top_n] if top_n else out

    def _column_freq(
        self,
        column: str,
        *,
        top_n: int = 30,
        min_count: int = 1,
        normalize_variant: bool = True,
        map_fn=None,
    ) -> List[Dict[str, Any]]:
        """对 movies 表里的普通列做频次统计。

        map_fn 用于在分组前做别名归并（如片商的 ムーディーズ → MOODYZ）。
        """
        sql = f"SELECT {column} v, COUNT(*) c FROM movies WHERE {column} IS NOT NULL AND {column}<>''"
        params: List[Any] = []
        if self.source:
            sql += " AND source = ?"
            params.append(self.source)
        sql += f" GROUP BY {column}"
        groups: Dict[str, List[Tuple[str, int]]] = defaultdict(list)
        for row in self.conn.execute(sql, params):
            v = str(row["v"]).strip()
            if not v:
                continue
            if map_fn is not None:
                v = map_fn(v)
                if not v:
                    continue
            k = self.norm.variant_key(v) if normalize_variant else v.casefold()
            groups[k].append((v, row["c"]))
        out: List[Dict[str, Any]] = []
        for k, items in groups.items():
            items.sort(key=lambda x: (-x[1], len(x[0]), x[0]))
            total = sum(c for _, c in items)
            if total < min_count:
                continue
            out.append({"key": k, "name": items[0][0], "count": total,
                        "aliases": [v for v, _ in items[1:3]]})
        out.sort(key=lambda x: (-x["count"], x["name"]))
        return out[:top_n] if top_n else out

    # ------------------------------------------------------------------
    # 总览
    # ------------------------------------------------------------------
    def _mw(self, prefix: str = " WHERE ") -> str:
        """movies 表的过滤子句。prefix 可为 " WHERE " 或 " AND "（接在既有条件后）。"""
        return f"{prefix}source = ?" if self.source else ""

    def overview(self) -> Dict[str, Any]:
        conn = self.conn
        params = self._p()
        total = (conn.execute("SELECT COUNT(*) c FROM movies" + self._mw(), params).fetchone()["c"]
                 if self.source else self.store.count_movies())
        if total == 0:
            return {"total": 0}

        agg = conn.execute(
            "SELECT "
            "  SUM(CASE WHEN userrating>0 THEN 1 ELSE 0 END) rated, "
            "  SUM(CASE WHEN userrating>0 THEN userrating ELSE 0 END) rating_sum, "
            "  SUM(CASE WHEN runtime_min>0 THEN runtime_min ELSE 0 END) minutes, "
            "  SUM(video_size) bytes, "
            "  SUM(CASE WHEN video_exists=1 THEN 1 ELSE 0 END) with_video, "
            "  SUM(CASE WHEN watched=1 THEN 1 ELSE 0 END) watched, "
            "  SUM(playcount) plays, "
            "  SUM(tag_count) tags, "
            "  SUM(actor_count) actors, "
            "  AVG(CASE WHEN runtime_min>0 THEN runtime_min END) avg_runtime, "
            "  MIN(CASE WHEN year>0 THEN year END) min_year, "
            "  MAX(CASE WHEN year>0 THEN year END) max_year, "
            "  AVG(CASE WHEN userrating>0 THEN userrating END) avg_rating "
            "FROM movies" + self._mw(), params
        ).fetchone()

        # 艺人数剔除"未知演员"这类占位名；片商数按别名归并后的口径统计
        actor_sql = ("SELECT DISTINCT a.actor_key k, a.actor v FROM movie_actors a"
                     + (" JOIN movies m ON m.id = a.movie_id WHERE " + self._mv()
                        if self.source else ""))
        actor_keys = {
            r["k"] for r in conn.execute(actor_sql, params)
            if r["k"] and not self.norm.is_stop_actor(r["v"])
        }
        studio_keys = {
            self.norm.studio_key(r["v"]) for r in
            conn.execute("SELECT DISTINCT studio v FROM movies WHERE studio<>''"
                         + (" AND source = ?" if self.source else ""), params)
        }
        tag_sql = ("SELECT COUNT(DISTINCT t.tag_key) c FROM movie_tags t"
                   + (" WHERE " + self._sub() if self.source else ""))
        dir_sql = ("SELECT COUNT(DISTINCT d.director) c FROM movie_directors d"
                   + (" WHERE " + self._sub("d") if self.source else ""))
        distinct = {
            "actors": len(actor_keys),
            "tags": conn.execute(tag_sql, params).fetchone()["c"],
            "studios": len(studio_keys),
            "series": conn.execute(
                "SELECT COUNT(DISTINCT series) c FROM movies WHERE series<>''"
                + (" AND source = ?" if self.source else ""), params).fetchone()["c"],
            "directors": conn.execute(dir_sql, params).fetchone()["c"],
            "prefixes": conn.execute(
                "SELECT COUNT(DISTINCT num_prefix) c FROM movies WHERE num_prefix<>''"
                + (" AND source = ?" if self.source else ""), params).fetchone()["c"],
        }

        rated = agg["rated"] or 0
        minutes = agg["minutes"] or 0
        return {
            "total": total,
            "rated": rated,
            "rated_ratio": _pct(rated, total),
            "avg_rating": round(agg["avg_rating"] or 0, 2),
            "total_hours": round(minutes / 60, 1),
            "total_days": round(minutes / 1440, 2),
            "avg_runtime": round(agg["avg_runtime"] or 0, 1),
            "total_bytes": agg["bytes"] or 0,
            "total_gb": round((agg["bytes"] or 0) / 1073741824, 2),
            "total_tb": round((agg["bytes"] or 0) / 1099511627776, 2),
            "with_video": agg["with_video"] or 0,
            "watched": agg["watched"] or 0,
            "plays": agg["plays"] or 0,
            "avg_tags": round(_safe_div(agg["tags"] or 0, total), 2),
            "avg_actors": round(_safe_div(agg["actors"] or 0, total), 2),
            "year_min": agg["min_year"] or "",
            "year_max": agg["max_year"] or "",
            "year_span": (agg["max_year"] or 0) - (agg["min_year"] or 0) + 1 if agg["min_year"] else 0,
            "distinct": distinct,
        }

    # ------------------------------------------------------------------
    # 高频维度
    # ------------------------------------------------------------------
    def top_tags(self, top_n: int = 40, min_count: int = 1) -> List[Dict[str, Any]]:
        # 现算同义词归并（中出し / 內射 → 中出），改配置即时生效，无需 reindex
        return self._freq("movie_tags", "tag_key", "tag",
                          top_n=top_n, min_count=min_count, map_fn=self.norm.tag)

    def top_tech_tags(self, top_n: int = 20) -> List[Dict[str, Any]]:
        out = self._freq("movie_tech", "tech", "tech", top_n=top_n)
        for it in out:
            it.setdefault("aliases", [])
        return out

    def top_actors(self, top_n: int = 30, min_count: int = 1) -> List[Dict[str, Any]]:
        """高频艺人，附带评分、年代、片商、代表标签等维度。"""
        params = self._p()
        # 先取全部演员频次，过滤掉"未知演员"这类占位名后再做 TopN，
        # 避免占位名霸榜导致真实艺人被挤出榜单。
        rows = self.conn.execute(
            "SELECT a.actor_key k, a.actor v, COUNT(*) c, "
            "       AVG(CASE WHEN m.userrating>0 THEN m.userrating END) rating, "
            "       MIN(CASE WHEN m.year>0 THEN m.year END) y0, "
            "       MAX(CASE WHEN m.year>0 THEN m.year END) y1, "
            "       AVG(CASE WHEN m.runtime_min>0 THEN m.runtime_min END) rt, "
            "       SUM(CASE WHEN m.watched=1 THEN 1 ELSE 0 END) watched "
            "FROM movie_actors a JOIN movies m ON m.id=a.movie_id "
            + ((" WHERE " + self._mv()) if self.source else "")
            + " GROUP BY a.actor_key, a.actor", params
        ).fetchall()

        groups: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            # 同样现算别名归并（响莲 → 響蓮），改配置即时生效
            name = self.norm.actor(r["v"] or "")
            k = self.norm.variant_key(name)
            if not k or self.norm.is_stop_actor(name):
                continue
            g = groups.setdefault(k, {"key": k, "name": name, "count": 0,
                                      "rating_sum": 0.0, "rated": 0, "y0": 9999,
                                      "y1": 0, "rt_sum": 0.0, "rt_n": 0, "watched": 0})
            g["count"] += r["c"]
            if r["rating"]:
                g["rating_sum"] += r["rating"] * r["c"]
                g["rated"] += r["c"]
            if r["y0"]:
                g["y0"] = min(g["y0"], r["y0"])
            if r["y1"]:
                g["y1"] = max(g["y1"], r["y1"])
            if r["rt"]:
                g["rt_sum"] += r["rt"] * r["c"]
                g["rt_n"] += r["c"]
            g["watched"] += r["watched"] or 0
            # 展示名取出现次数最多的写法
            if r["c"] > g.get("_top", 0):
                g["_top"] = r["c"]
                g["name"] = name

        out = []
        for g in groups.values():
            if g["count"] < min_count:
                continue
            out.append({
                "key": g["key"],
                "name": g["name"],
                "count": g["count"],
                "avg_rating": round(_safe_div(g["rating_sum"], g["rated"]), 2),
                "year_from": g["y0"] if g["y0"] != 9999 else "",
                "year_to": g["y1"],
                "avg_runtime": round(_safe_div(g["rt_sum"], g["rt_n"]), 0) or "",
                "watched": g["watched"],
            })
        out.sort(key=lambda x: (-x["count"], x["name"]))
        out = out[:top_n] if top_n else out

        # 补充每人最常合作的片商 / 标签 / 对手演员
        self._enrich_actors(out)
        return out

    def _enrich_actors(self, actors: List[Dict[str, Any]]) -> None:
        if not actors:
            return
        params = self._p()
        keys = [a["key"] for a in actors]
        qmarks = ",".join("?" * len(keys))

        studios: Dict[str, Counter] = defaultdict(Counter)
        for r in self.conn.execute(
            f"SELECT a.actor_key k, m.studio s FROM movie_actors a JOIN movies m ON m.id=a.movie_id "
            f"WHERE a.actor_key IN ({qmarks}) AND m.studio<>''"
            + (f" AND {self._mv()}" if self.source else ""), keys + params
        ):
            studios[r["k"]][r["s"]] += 1

        tags: Dict[str, Counter] = defaultdict(Counter)
        for r in self.conn.execute(
            f"SELECT a.actor_key k, t.tag tg FROM movie_actors a JOIN movie_tags t ON t.movie_id=a.movie_id "
            f"WHERE a.actor_key IN ({qmarks})"
            + (f" AND {self._sub('t')}" if self.source else ""), keys + params
        ):
            if r["tg"]:
                tags[r["k"]][self.norm.tag(r["tg"])] += 1

        partners: Dict[str, Counter] = defaultdict(Counter)
        for r in self.conn.execute(
            f"SELECT a1.actor_key k, a2.actor p FROM movie_actors a1 "
            f"JOIN movie_actors a2 ON a2.movie_id=a1.movie_id AND a2.actor_key<>a1.actor_key "
            f"WHERE a1.actor_key IN ({qmarks})", keys
        ):
            partners[r["k"]][self.norm.actor(r["p"])] += 1

        for a in actors:
            k = a["key"]
            a["top_studios"] = [{"name": n, "count": c} for n, c in studios.get(k, Counter()).most_common(3)]
            a["top_tags"] = [{"name": n, "count": c} for n, c in tags.get(k, Counter()).most_common(6)]
            a["top_partners"] = [{"name": n, "count": c} for n, c in partners.get(k, Counter()).most_common(3)]

    def top_studios(self, top_n: int = 20) -> List[Dict[str, Any]]:
        # 合并同一家片商的中日英文写法（ムーディーズ / MOODYZ …）
        return self._column_freq("studio", top_n=top_n, map_fn=self.norm.studio)

    def top_publishers(self, top_n: int = 20) -> List[Dict[str, Any]]:
        return self._column_freq("publisher", top_n=top_n, map_fn=self.norm.studio)

    def top_labels(self, top_n: int = 20) -> List[Dict[str, Any]]:
        return self._column_freq("label", top_n=top_n, map_fn=self.norm.studio)

    def top_series(self, top_n: int = 20) -> List[Dict[str, Any]]:
        return self._column_freq("series", top_n=top_n)

    def top_directors(self, top_n: int = 20) -> List[Dict[str, Any]]:
        return self._freq("movie_directors", "director", "director",
                          top_n=top_n, map_fn=self.norm.entity)

    def top_prefixes(self, top_n: int = 20) -> List[Dict[str, Any]]:
        return self._column_freq("num_prefix", top_n=top_n, normalize_variant=False)

    # ------------------------------------------------------------------
    # 高频标题词（v1.1.4）：把 title 拆词后做聚合
    # ------------------------------------------------------------------
    def top_title_terms(self, top_n: int = 50, min_count: int = 2,
                        known: Optional[Set[str]] = None) -> Tuple[List[Dict[str, Any]], int, int]:
        """从 movies.title 中提取高频短语词。

        Returns
        -------
        (terms, n_titles_with_term, distinct_terms):
          - terms: 与 top_tags 同构 ``[{"name", "count", "aliases"}, ...]``，
            放在 GUI 的「高频标题词」维度表里展示；
          - n_titles_with_term: 实际被分词到至少一个 token 的 title 数（用于 KPI 卡）；
          - distinct_terms: 唯一 token 数（即画像里的「词表大小」）。
        """
        from .title_tokenizer import top_title_terms as _top_title_terms
        # 关闭 source 时 store 是全局；与 _freq/_column_freq 行为一致
        return _top_title_terms(
            self.store, source=self.source or None, top_n=top_n,
            min_count=min_count, known=known)

    # ------------------------------------------------------------------
    # 分布
    # ------------------------------------------------------------------
    def dist_resolution(self) -> List[Dict[str, Any]]:
        params = self._p()
        order = ["480P", "720P", "1080P", "2K", "4K", "8K"]
        rows = {r["v"]: r["c"] for r in self.conn.execute(
            "SELECT resolution v, COUNT(*) c FROM movies" + (self._mw(" WHERE ") if self.source else "") + " GROUP BY resolution", params)}
        out = [{"name": k, "count": rows.get(k, 0)} for k in order]
        if rows.get("", 0):
            out.append({"name": "未知", "count": rows[""]})
        return out

    def dist_censor(self) -> List[Dict[str, Any]]:
        params = self._p()
        return [{"name": r["v"] or "未知", "count": r["c"]} for r in self.conn.execute(
            "SELECT censor_status v, COUNT(*) c FROM movies" + (self._mw(" WHERE ") if self.source else "") + " GROUP BY censor_status ORDER BY c DESC", params)]

    def dist_rating(self) -> List[Dict[str, Any]]:
        """评分分布（0-10，按 1 分一档）。"""
        params = self._p()
        buckets = Counter()
        for r in self.conn.execute("SELECT userrating FROM movies WHERE userrating>0" + (self._mw(" AND ") if self.source else ""), params):
            v = float(r["userrating"])
            b = int(min(10, max(0, math.floor(v))))
            buckets[b] += 1
        return [{"name": f"{i} 分", "count": buckets.get(i, 0)} for i in range(0, 11)]

    def dist_runtime(self) -> List[Dict[str, Any]]:
        params = self._p()
        buckets = Counter()
        for r in self.conn.execute("SELECT runtime_min FROM movies WHERE runtime_min>0" + (self._mw(" AND ") if self.source else ""), params):
            v = int(r["runtime_min"])
            if v < 60:
                b = "<60"
            elif v < 90:
                b = "60-89"
            elif v < 120:
                b = "90-119"
            elif v < 150:
                b = "120-149"
            elif v < 180:
                b = "150-179"
            elif v < 240:
                b = "180-239"
            else:
                b = "240+"
            buckets[b] += 1
        order = ["<60", "60-89", "90-119", "120-149", "150-179", "180-239", "240+"]
        return [{"name": k, "count": buckets.get(k, 0)} for k in order]

    def dist_year(self) -> List[Dict[str, Any]]:
        params = self._p()
        rows = list(self.conn.execute(
            "SELECT year y, COUNT(*) c FROM movies WHERE year>0" + (self._mw(" AND ") if self.source else "") + " GROUP BY year ORDER BY year", params))
        return [{"name": str(r["y"]), "count": r["c"]} for r in rows]

    def dist_added_month(self) -> List[Dict[str, Any]]:
        """按入库月份统计（dateadded），反映收藏节奏。"""
        params = self._p()
        buckets = Counter()
        for r in self.conn.execute("SELECT dateadded FROM movies WHERE dateadded<>''" + (self._mw(" AND ") if self.source else ""), params):
            m = _MONTH_PAT.match(r["dateadded"] or "")
            if m:
                buckets[f"{m.group(1)}-{m.group(2)}"] += 1
        return [{"name": k, "count": v} for k, v in sorted(buckets.items())]

    def dist_added_hour(self) -> List[Dict[str, Any]]:
        params = self._p()
        buckets = Counter()
        for r in self.conn.execute("SELECT dateadded FROM movies WHERE dateadded<>''" + (self._mw(" AND ") if self.source else ""), params):
            m = _HOUR_PAT.search(r["dateadded"] or "")
            if m:
                buckets[int(m.group(1))] += 1
        return [{"name": f"{h:02d}", "count": buckets.get(h, 0)} for h in range(24)]

    def dist_weekday(self) -> List[Dict[str, Any]]:
        from datetime import datetime
        names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        params = self._p()
        buckets = Counter()
        for r in self.conn.execute("SELECT dateadded FROM movies WHERE dateadded<>''"
                                   + (self._mw(" AND ") if self.source else ""), params):
            txt = (r["dateadded"] or "")[:10]
            try:
                buckets[datetime.strptime(txt, "%Y-%m-%d").weekday()] += 1
            except ValueError:
                continue
        return [{"name": names[i], "count": buckets.get(i, 0)} for i in range(7)]

    def dist_actor_count(self) -> List[Dict[str, Any]]:
        params = self._p()
        rows = {r["v"]: r["c"] for r in self.conn.execute(
            "SELECT actor_count v, COUNT(*) c FROM movies" + (self._mw(" WHERE ") if self.source else "") + " GROUP BY actor_count", params)}
        out = [{"name": "单人作品", "count": rows.get(1, 0)},
               {"name": "双人共演", "count": rows.get(2, 0)},
               {"name": "多人(3-5)", "count": sum(rows.get(i, 0) for i in range(3, 6))},
               {"name": "大乱斗(6+)", "count": sum(v for k, v in rows.items() if k and k >= 6)},
               {"name": "无演员信息", "count": rows.get(0, 0)}]
        return [o for o in out if o["count"]]

    # ------------------------------------------------------------------
    # 关系
    # ------------------------------------------------------------------
    def tag_cooccurrence(self, top_tags: int = 40, top_pairs: int = 200) -> Dict[str, Any]:
        """标签共现：返回节点与边，用于关系图 / 热力矩阵。"""
        freq = self._freq("movie_tags", "tag_key", "tag", top_n=top_tags)
        wanted = {f["key"] for f in freq}
        name_of = {f["key"]: f["name"] for f in freq}

        pair_counter: Counter = Counter()
        from itertools import combinations
        allowed = self._allowed_ids()
        for _mid, tags in self.store.tag_pairs():
            if allowed is not None and _mid not in allowed:
                continue
            keys = []
            for t in tags:
                k = self.norm.tag_key(t)
                if k in wanted and k not in keys:
                    keys.append(k)
            if len(keys) >= 2:
                for a, b in combinations(sorted(keys), 2):
                    pair_counter[(a, b)] += 1

        edges = [
            {"source": name_of.get(a, a), "target": name_of.get(b, b), "value": c}
            for (a, b), c in pair_counter.most_common(top_pairs)
        ]
        nodes = [{"name": f["name"], "value": f["count"]} for f in freq]

        # 矩阵形式（热力图）
        names = [f["name"] for f in freq]
        idx = {n: i for i, n in enumerate(names)}
        matrix = [[0] * len(names) for _ in names]
        for (a, b), c in pair_counter.items():
            na, nb = name_of.get(a, a), name_of.get(b, b)
            if na in idx and nb in idx:
                i, j = idx[na], idx[nb]
                matrix[i][j] = c
                matrix[j][i] = c
        return {"nodes": nodes, "edges": edges, "matrix": matrix, "labels": names}

    def actor_pairs(self, top_n: int = 30) -> List[Dict[str, Any]]:
        """高频共演组合。"""
        counter: Counter = Counter()
        sql = ("SELECT a1.actor_key k1, a1.actor n1, a2.actor_key k2, a2.actor n2 "
               "FROM movie_actors a1 JOIN movie_actors a2 "
               "  ON a2.movie_id=a1.movie_id AND a1.actor_key < a2.actor_key"
               + ((" WHERE " + self._sub("a1")) if self.source else ""))
        for r in self.conn.execute(sql, self._p()):
            if not r["k1"] or not r["k2"]:
                continue
            counter[(r["k1"], r["k2"], r["n1"], r["n2"])] += 1
        return [
            {"a": n1, "b": n2, "count": c}
            for (_, _, n1, n2), c in counter.most_common(top_n)
        ]

    def plot_keywords(self, top_n: int = 60) -> List[Dict[str, Any]]:
        """从剧情简介里抽取高频词（无词典新词发现）。"""
        total = self.store.count_movies()
        min_count = max(4, int(total / 400) + 2)
        plots = self.store.all_plots(limit=8000, source=self.source or None)
        pairs = extract_keywords(plots, top_n=top_n, min_count=min_count)
        return [{"name": w, "count": c} for w, c in pairs]

    # ------------------------------------------------------------------
    # 用户画像
    # ------------------------------------------------------------------
    def portrait(self) -> Dict[str, Any]:
        """生成结构化用户画像 + 雷达图数据 + 一句话总结。"""
        ov = self.overview()
        if not ov.get("total"):
            return {"empty": True}

        total = ov["total"]
        tags = self.top_tags(top_n=15)
        actors = self.top_actors(top_n=10)
        studios = self.top_studios(top_n=6)
        series = self.top_series(top_n=6)
        directors = self.top_directors(top_n=6)
        res = self.dist_resolution()
        censor = self.dist_censor()
        runtime = self.dist_runtime()
        years = self.dist_year()
        rated = self.dist_rating()

        res_map = {r["name"]: r["count"] for r in res}
        hd = res_map.get("1080P", 0) + res_map.get("2K", 0) + res_map.get("4K", 0) + res_map.get("8K", 0)
        censor_map = {c["name"]: c["count"] for c in censor}

        rt_map = {r["name"]: r["count"] for r in runtime}
        long_film = rt_map.get("120-149", 0) + rt_map.get("150-179", 0) + rt_map.get("180-239", 0) + rt_map.get("240+", 0)

        year_map = {int(y["name"]): y["count"] for y in years if y["name"].isdigit()}
        recent = sum(c for y, c in year_map.items() if year_map and y >= max(year_map) - 1)

        top10_actor_share = _pct(sum(a["count"] for a in actors[:10]), total)
        top3_studio = sum(s["count"] for s in studios[:3])
        top3_studio_share = _pct(top3_studio, total)
        series_covered = sum(s["count"] for s in series[:6])

        # --- 雷达图 8 个维度（0-100）---
        # 说明：绝大多数维度直接取"占比"，便于直观解读（如 艺人专一度 68 即
        # Top10 艺人占总收藏 68%）；系列收集度 / 追新度 天然占比偏低，
        # 用 2 倍系数放大到可读区间，并在报告中标注为相对值。
        series_share = _pct(series_covered, total)
        recent_share = _pct(recent, total)
        radar = [
            {"name": "标签广度", "value": _clamp(_safe_div(ov["distinct"]["tags"], 300) * 100),
             "hint": f"去重标签 {ov['distinct']['tags']} 个"},
            {"name": "艺人专一度", "value": _clamp(top10_actor_share),
             "hint": f"Top10 艺人占比 {top10_actor_share}%"},
            {"name": "片商忠诚度", "value": _clamp(top3_studio_share),
             "hint": f"Top3 片商占比 {top3_studio_share}%"},
            {"name": "高清偏好", "value": _clamp(_pct(hd, total)),
             "hint": f"1080P 及以上占比 {_pct(hd, total)}%"},
            {"name": "长片偏好", "value": _clamp(_pct(long_film, total)),
             "hint": f"120 分钟以上占比 {_pct(long_film, total)}%"},
            {"name": "打分积极度", "value": _clamp(ov["rated_ratio"]),
             "hint": f"已评分占比 {ov['rated_ratio']}%"},
            {"name": "系列收集度", "value": _clamp(series_share * 2),
             "hint": f"Top6 系列占比 {series_share}%（×2 放大）"},
            {"name": "追新度", "value": _clamp(recent_share * 2),
             "hint": f"近两年作品占比 {recent_share}%（×2 放大）"},
        ]

        # --- 一句话画像 ---
        bits = []
        if tags:
            bits.append("偏好 " + "、".join(t["name"] for t in tags[:4]))
        if actors:
            bits.append("最常收录 " + actors[0]["name"] + f"（{actors[0]['count']} 部）")
        if studios:
            bits.append("片商以 " + studios[0]["name"] + " 为主")
        if hd and _pct(hd, total) >= 50:
            bits.append("以高清片源为主")
        summary = "；".join(bits) + "。"

        return {
            "empty": False,
            "overview": ov,
            "summary": summary,
            "radar": radar,
            "favorite_tags": tags[:12],
            "favorite_actors": actors[:10],
            "favorite_studios": studios[:6],
            "favorite_series": series[:6],
            "favorite_directors": directors[:6],
            "resolution": res,
            "censor": censor,
            "censor_map": censor_map,
            "runtime": runtime,
            "years": years,
            "rating": rated,
            "top10_actor_share": top10_actor_share,
            "top3_studio_share": top3_studio_share,
            "hd_share": _pct(hd, total),
            "long_film_share": _pct(long_film, total),
            "recent_share": _pct(recent, total),
        }

    # ------------------------------------------------------------------
    # 汇总（供报告 / 导出使用）
    # ------------------------------------------------------------------
    def build_report_data(
        self,
        *,
        top_tags: int = 40,
        top_actors: int = 30,
        top_misc: int = 20,
        cooc_tags: int = 36,
        keywords: int = 60,
        with_cooccurrence: bool = True,
        with_keywords: bool = True,
    ) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "generated_at": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "overview": self.overview(),
            "portrait": self.portrait(),
            "tags": self.top_tags(top_n=top_tags),
            "tech_tags": self.top_tech_tags(top_n=15),
            "actors": self.top_actors(top_n=top_actors),
            "studios": self.top_studios(top_n=top_misc),
            "publishers": self.top_publishers(top_n=top_misc),
            "labels": self.top_labels(top_n=top_misc),
            "series": self.top_series(top_n=top_misc),
            "directors": self.top_directors(top_n=top_misc),
            "prefixes": self.top_prefixes(top_n=top_misc),
            "dist_resolution": self.dist_resolution(),
            "dist_censor": self.dist_censor(),
            "dist_rating": self.dist_rating(),
            "dist_runtime": self.dist_runtime(),
            "dist_year": self.dist_year(),
            "dist_added_month": self.dist_added_month(),
            "dist_added_hour": self.dist_added_hour(),
            "dist_weekday": self.dist_weekday(),
            "dist_actor_count": self.dist_actor_count(),
            "actor_pairs": self.actor_pairs(top_n=25),
        }
        # v1.1.4：新增标题词频维度（tag 不全时也能挖出隐藏在标题里的偏好）
        terms, n_titles, distinct = self.top_title_terms(top_n=top_tags)
        data["title_terms"] = terms
        # 把它也回填到 overview.distinct 里供顶部 KPI 卡显示
        if data.get("overview"):
            data["overview"].setdefault("distinct", {})
            data["overview"]["distinct"]["title_terms"] = distinct
            data["overview"]["title_term_titles"] = n_titles
        if with_cooccurrence:
            data["cooccurrence"] = self.tag_cooccurrence(top_tags=cooc_tags)
        if with_keywords:
            data["keywords"] = self.plot_keywords(top_n=keywords)
        return data


__all__ = ["Analyzer"]
