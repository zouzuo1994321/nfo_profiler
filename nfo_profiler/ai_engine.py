# -*- coding: utf-8 -*-
"""本地 AI 增强引擎（可选 · 惰性 · 绝不阻塞主线程）。

设计红线（来自 v1.0.4 的教训）：
  * 模块加载时**只** import 标准库——绝不 import torch / onnxruntime / tokenizers；
  * 所有重依赖（onnxruntime、tokenizers）都放在「后台线程」里惰性 import，并带异常隔离；
  * 检测不到任何本地 AI 环境时，自动降级为「本地启发式增强」（同义词 / 异体字扩展），
    保证开关永远可用、HTTP 服务永不被拖垮。

能力分层：
  * ``heuristic``（默认可用，零依赖）
        —— 把自然语言查询拆词 + 同义词 / 异体字扩展，做 OR 检索；
  * ``ollama``（检测到本地 Ollama 时，纯 HTTP 调用，无需 import 任何重依赖）
        —— 用 ``/api/generate`` 把查询改写成检索关键词，再做检索；
  * ``minilm``（检测到本地 MiniLM ONNX 模型时，惰性加载 onnxruntime）
        —— 句向量语义相似检索（预留接口，本期先打通检测与降级链路）。
"""

from __future__ import annotations

import json
import math
import os
import re
import struct
import threading
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# 本地 AI 环境探测参数（可用环境变量覆盖，方便高级用户自定义路径）
OLLAMA_HOST = os.environ.get("NFO_AI_OLLAMA", "http://localhost:11434")
MINILM_DIR = os.environ.get("NFO_AI_MODEL_DIR", os.path.join("ai_models"))
_PROBE_TIMEOUT = 0.8          # Ollama 探活超时（秒，尽量短，避免开启开关时卡顿）
_GENERATE_TIMEOUT = 5.0       # Ollama 改写查询超时（秒）

# 拆词：按空白与常见中英文标点切分
_TOKEN_SPLIT = re.compile(r"[\s,，、;；/\\|]+")


class AIEngine:
    """可选、惰性、线程安全的本地 AI 增强引擎。

    对外只暴露极少量接口：``status()`` / ``set_enabled()`` / ``expand_terms()``。
    所有「重活」（探测 / 网络 / 模型加载）都在后台线程或请求线程里完成，
    且彼此用 ``RLock`` 保护，绝不长时间占用主线程 / 扫描线程的 GIL。
    """

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.enabled = False
        # 状态机：idle | initializing | ready | unavailable
        self.state = "idle"
        # 后端：none | heuristic | ollama | minilm
        self.backend = "none"
        self.message = "AI 增强未开启"
        self.capabilities: List[str] = []
        self._ollama_models: List[str] = []
        self._rt = None  # 运行时句柄（onnxruntime session / tokenizer），惰性加载
        self._init_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # 对外状态
    # ------------------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "enabled": self.enabled,
                "state": self.state,
                "backend": self.backend,
                "message": self.message,
                "capabilities": list(self.capabilities),
            }

    # ------------------------------------------------------------------
    # 开关（HTTP 层调用，立即返回，检测在后台线程跑）
    # ------------------------------------------------------------------
    def set_enabled(self, on: bool) -> Dict[str, Any]:
        with self.lock:
            if on:
                if self.state == "initializing":
                    return self.status()
                self.enabled = True
                self.state = "initializing"
                self.message = "正在检测本地 AI 环境…"
                self.capabilities = []
                self._init_thread = threading.Thread(target=self._detect, daemon=True)
                self._init_thread.start()
            else:
                self.enabled = False
                self.state = "idle"
                self.backend = "none"
                self.message = "AI 增强已关闭"
                self.capabilities = []
            return self.status()

    # ------------------------------------------------------------------
    # 检测（后台线程）
    # ------------------------------------------------------------------
    def _detect(self) -> None:
        # 1) 优先 Ollama（纯 HTTP，无需 import 任何重依赖，最稳）
        try:
            models = self._probe_ollama()
            if models:
                with self.lock:
                    self.backend = "ollama"
                    self.state = "ready"
                    self._ollama_models = models
                    self.capabilities = ["nl_query", "tag_suggest", "keyword_expand"]
                    self.message = "已就绪 · Ollama 语义理解（%s）" % models[0]
                return
        except Exception:
            pass

        # 2) MiniLM ONNX（惰性 import onnxruntime + tokenizers，异常即降级）
        try:
            if self._probe_minilm():
                with self.lock:
                    self.backend = "minilm"
                    self.state = "ready"
                    self.capabilities = ["semantic_search", "similar"]
                    self.message = "已就绪 · MiniLM 向量语义"
                return
        except Exception:
            pass

        # 3) 兜底：本地启发式（永远可用，零依赖）—— 保证开关永不是死胡同
        with self.lock:
            self.backend = "heuristic"
            self.state = "ready"
            self.capabilities = ["keyword_expand", "synonym_search"]
            self.message = "已开启 · 本地启发式（未检测到 Ollama / MiniLM）"

    def _probe_ollama(self) -> List[str]:
        """探测本地 Ollama 是否可用，返回模型名列表（空 = 不可用）。"""
        req = urllib.request.Request("%s/api/tags" % OLLAMA_HOST)
        with urllib.request.urlopen(req, timeout=_PROBE_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
        models = [m.get("name", "") for m in data.get("models", [])]
        return [m for m in models if m]

    def _probe_minilm(self) -> bool:
        """探测本地 MiniLM ONNX 模型是否存在，存在则惰性加载（在后台线程）。"""
        onnx = os.path.join(MINILM_DIR, "model.onnx")
        tok = os.path.join(MINILM_DIR, "tokenizer.json")
        if not (os.path.isfile(onnx) and os.path.isfile(tok)):
            return False
        # 以下两个 import 是「重依赖」，但此刻已在后台线程，且失败会被外层 except 兜住
        import onnxruntime as ort  # noqa: F401
        from tokenizers import Tokenizer  # noqa: F401
        sess = ort.InferenceSession(onnx, providers=["CPUExecutionProvider"])
        tokenizer = Tokenizer.from_file(tok)
        with self.lock:
            self._rt = (sess, tokenizer)
        return True

    # ------------------------------------------------------------------
    # 查询扩展（请求线程调用，可能在锁外做网络，务必轻量 + 带超时）
    # ------------------------------------------------------------------
    def expand_terms(self, q: str, norm: Any = None) -> List[str]:
        """把用户查询扩展成一组检索词。

        网络调用（Ollama 改写）放在锁外、带超时；任何失败都静默回退到本地拆词。
        返回空列表表示「不启用 AI 扩展」，调用方应回退到普通检索。
        """
        q = (q or "").strip()
        if not q:
            return []
        base = self._tokenize(q, norm)
        if self.backend == "ollama":
            try:
                for k in self._ollama_keywords(q):
                    tk = norm.variant_key(k) if norm else k
                    if tk and tk not in base:
                        base.append(tk)
            except Exception:
                pass
        return base

    def _tokenize(self, q: str, norm: Any = None) -> List[str]:
        """本地拆词 + 同义词 / 异体字扩展（零网络、零重依赖）。

        针对中文「连续无空格」的自然语言查询，额外做一层同义词表子串扫描：
        只要查询里出现了某个标签别名 / 规范名，就把该词及其同组词一并纳入检索。
        """
        out: List[str] = []
        seen = set()
        syn = getattr(norm, "synonyms", None) if norm else None

        # 1) 显式分隔符切分
        candidates = [s for s in _TOKEN_SPLIT.split(q) if s.strip()]
        # 2) 同义词表子串扫描（覆盖「健身房题材的巨乳作品」这类连续中文）
        if syn:
            for key in syn.keys():
                if key and key in q and key not in candidates:
                    candidates.append(key)
            for val in set(syn.values()):
                if val and val in q and val not in candidates:
                    candidates.append(val)

        for t in candidates:
            tk = norm.variant_key(t) if norm else t
            if tk and tk not in seen:
                seen.add(tk)
                out.append(tk)
            # 同义词扩展：若 tk 是某规范名的别名，则把同组别名一并纳入
            if syn:
                canon = syn.get(tk) or tk
                for alias, c in syn.items():
                    if c == canon and alias not in seen:
                        seen.add(alias)
                        out.append(alias)
        return out

    def _ollama_keywords(self, q: str) -> List[str]:
        """用 Ollama 把自然语言查询改写成 3~8 个检索关键词。"""
        if self.backend != "ollama" or not self._ollama_models:
            return []
        model = self._ollama_models[0]
        prompt = (
            "你是一个中文影片标签检索助手。用户用自然语言描述想找的影片，"
            "请只返回最相关的 3-8 个检索关键词（每个词 1-4 个汉字），用中文逗号分隔，"
            "不要任何解释。例如：健身房,巨乳,剧情。\n用户描述：" + q
        )
        payload = json.dumps(
            {"model": model, "prompt": prompt, "stream": False}
        ).encode("utf-8")
        req = urllib.request.Request(
            "%s/api/generate" % OLLAMA_HOST,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=_GENERATE_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
        text = data.get("response", "")
        return [k.strip() for k in re.split(r"[,，、;；\s]+", text) if k.strip()][:8]

    # ------------------------------------------------------------------
    # 离线 AI 能力：标签主题聚类 + 相似作品推荐（零依赖，Ollama/MiniLM 时升级）
    # ------------------------------------------------------------------
    def cluster_themes(
        self,
        store: Any,
        top_n: int = 80,
        min_co: int = 3,
        with_title_terms: bool = True,
        source: Optional[str] = None,
        limit_representatives: int = 5,
        min_size: int = 4,
        drop_generic: bool = True,
        max_share: float = 0.20,
    ) -> List[Dict[str, Any]]:
        """综合标签 + 标题高频词的主题聚类（v1.1.4 综合分析 / **v1.3.0 细化**）。

        设计动机：
          不少作品的 ``<tag>`` 不打全，但 ``<title>`` 里其实泄露了题材特征
          （「极品黑丝」、「OL 健身房」等）。本方法把「标签」与「标题高频词」统一看待。

        **v1.3.0 细化点**（用户反馈「还是不够细化和细致」）：

        * **组合主题名**：主题不再只叫一个种子词，而是
          ``种子 × 最强关联词 × 次强关联词``（如「人妻 × 熟女 × 中出」），
          一眼能看懂这个簇讲什么；
        * **过滤泛化词**：覆盖 >55% 作品的超级通用词（「单体作品」「2024」等）
          不再当种子也不进成员表，避免污染主题；
        * **强度明细**：每个成员带 ``strength``（共现强度）与 ``co``（共现作品数），
          UI 可展示「谁和谁贴得最紧」；
        * **子主题**：簇内成员按两两共现强度再聚合出 2-3 个子主题
          （如「人妻 × 熟女」），颗粒度更细；
        * **簇统计**：作品数 + 占比 + 平均评分 + 年份跨度；
        * **过小簇过滤**：``min_size`` 以下的簇直接丢掉，结果更干净。

        返回结构（每项）：
            ``{
                "theme":            主题名（种子 token，保持向后兼容）,
                "label":            v1.3.0 组合展示名,
                "kind":             "tag" | "title_term",
                "size":             包含该簇的作品数,
                "share":            占全库比例（0-1）,
                "members":          [tag1, tag2, ...]   # 关联标签（兼容旧格式）,
                "member_details":   [{name, kind, strength, co}],
                "title_terms":      [tok1, tok2, ...]   # 关联标题词,
                "subthemes":        [{label, count, strength}],
                "representatives":  [{num, title, path, rating}],
                "avg_rating":       簇内平均评分,
                "year_range":       [min, max] 或 [],
                "singleton":        是否单元素簇,
            }``
        """
        from .title_tokenizer import infer_known_from_store, top_title_terms

        conn = store.conn

        # -- 1) 高频标签（按数据源过滤） --
        m_where = ""
        m_params: List[Any] = []
        if source:
            m_where = " WHERE m.source = ?"
            m_params.append(source)
        tag_rows = conn.execute(
            "SELECT t.tag, COUNT(*) c FROM movie_tags t "
            f"JOIN movies m ON m.id=t.movie_id {m_where} "
            "GROUP BY t.tag ORDER BY c DESC LIMIT ?",
            m_params + [top_n * 3]
        ).fetchall()
        tag_freq: Dict[str, int] = {r["tag"]: r["c"] for r in tag_rows if r["tag"]}

        # -- 2) 高频标题词（同源过滤） --
        title_freq: Dict[str, int] = {}
        known = infer_known_from_store(store, source=source)
        if with_title_terms:
            terms_dict, _, _ = top_title_terms(
                store, source=source, top_n=top_n, min_count=2, known=known)
            title_freq = {d["name"]: d["count"] for d in terms_dict}
        if not tag_freq and not title_freq:
            return []

        # -- 2.5) v1.3.0：泛化词过滤 ----------------------------------
        # 「单体作品」「2024」这类覆盖大半个库的词没有区分度，当种子会把别的
        # token 全吸走（v1.1.4 曾出现过 1-2 个巨型簇）。这里按「覆盖比例」
        # + 显式停用词 + 纯数字（年份）三类过滤。
        total_movies = conn.execute(
            f"SELECT COUNT(*) c FROM movies {m_where.replace('m.', '')}",
            m_params).fetchone()["c"] or 1
        _GENERIC = {
            "单体作品", "作品", "高清", "标清", "蓝光", "字幕", "中文字幕",
            "日本", "合集", "总集篇", "无修正", "独家", "首发", "下载",
            "真人", "成人", "限制级", "未删减", "完整版",
        }

        def _generic(tok: str, freq: int) -> bool:
            if not drop_generic:
                return False
            if tok in _GENERIC:
                return True
            if tok.isdigit():          # 2024 / 2025 之类年份
                return True
            if freq > total_movies * 0.55:   # 覆盖过半 → 无区分度
                return True
            return False

        # -- 3) 合并做种子的 token 集合（按频率降序） --
        seed_pool: List[Tuple[str, int, str]] = []  # (token, freq, kind)
        for t, c in tag_freq.items():
            if not _generic(t, c):
                seed_pool.append((t, c, "tag"))
        for t, c in title_freq.items():
            if not _generic(t, c):
                seed_pool.append((t, c, "title_term"))
        seed_pool.sort(key=lambda x: -x[1])
        # 截到 top_n × 2，避免过于分散
        seed_pool = seed_pool[:top_n * 2]
        seeds = {t for t, _, _ in seed_pool}

        # -- 4) 共现矩阵：扫描每部作品的 token 集合，统计 seed 同时出现次数 --
        # token → id（整数）压缩存储，节省内存
        tok2id: Dict[str, int] = {t: i for i, t in enumerate(seeds)}
        movie_seeds: Dict[int, set] = {}

        # 4a) 标签入集合
        for r in conn.execute(
            "SELECT m.id mid, t.tag t FROM movie_tags t "
            f"JOIN movies m ON m.id=t.movie_id {m_where}", m_params
        ):
            t = r["t"]
            if t in tok2id:
                movie_seeds.setdefault(r["mid"], set()).add(tok2id[t])
        # 4b) 标题拆词入集合
        if with_title_terms:
            from .title_tokenizer import tokenize_title, _default_stopwords
            stopwords = _default_stopwords()
            title_where = " WHERE title IS NOT NULL AND title<>''"
            tparams: List[Any] = []
            if source:
                title_where += " AND source = ?"
                tparams.append(source)
            for r in conn.execute(f"SELECT id, title FROM movies {title_where}", tparams):
                toks = tokenize_title(r["title"] or "", known=known, stopwords=stopwords)
                ms = movie_seeds.setdefault(r["id"], set())
                for t in toks:
                    if t in tok2id:
                        ms.add(tok2id[t])

        # -- 5) 共现对计数 --
        co: Dict[Tuple[int, int], int] = {}
        for ms in movie_seeds.values():
            lst = sorted(ms)
            for i in range(len(lst)):
                for j in range(i + 1, len(lst)):
                    a, b = lst[i], lst[j]
                    if a > b:
                        a, b = b, a
                    co[(a, b)] = co.get((a, b), 0) + 1

        # -- 6) 贪心聚类 --
        # 关键改进：限制每个簇的成员数，避免热门种子（"单体作品"等覆盖 ~80% 的作品）
        # 把别的所有 token 都吃掉，最后只剩 1~2 个庞大簇。
        # 同时给高频种子加更严格的 min_co（自适应），避免被过度吸纳。
        used: set = set()
        clusters: List[Dict[str, Any]] = []
        MAX_MEMBERS_PER_CLUSTER = 12  # 上限，超过后跳过而非强制吸收
        for seed_tok, seed_freq, seed_kind in seed_pool:
            if seed_tok in used:
                continue
            sid = tok2id[seed_tok]
            # 高频种子用更严格的 min_co（与出现作品数挂钩）
            effective_min_co = min_co + max(0, (seed_freq - 50) // 50)
            members: List[str] = []
            title_members: List[str] = []
            used.add(seed_tok)
            # 收集候选：按「与种子的共现强度」降序，而不是按频率
            cands: List[Tuple[float, str, str]] = []
            for other_tok, other_freq, other_kind in seed_pool:
                if other_tok in used or other_tok == seed_tok:
                    continue
                oid = tok2id[other_tok]
                key = (sid, oid) if sid < oid else (oid, sid)
                cnt = co.get(key, 0)
                if cnt < effective_min_co:
                    continue
                # 共现强度：Jaccard 简化版（避免"单体作品"等吸收太多）
                strength = cnt / (seed_freq + other_freq - cnt + 1e-9)
                cands.append((strength, other_tok, other_kind))
            cands.sort(key=lambda x: -x[0])
            member_details: List[Dict[str, Any]] = []
            for strength, other_tok, other_kind in cands[:MAX_MEMBERS_PER_CLUSTER]:
                if other_kind == "tag":
                    members.append(other_tok)
                else:
                    title_members.append(other_tok)
                oid2 = tok2id[other_tok]
                pair = (sid, oid2) if sid < oid2 else (oid2, sid)
                member_details.append({
                    "name": other_tok, "kind": other_kind,
                    "strength": round(strength, 3),
                    "co": co.get(pair, 0),
                })
                used.add(other_tok)
            # v1.3.0：组合主题名 —— 种子 × 最强关联 × 次强关联（自动去重）
            label_parts: List[str] = [seed_tok]
            for m in member_details:
                if m["name"] in label_parts:
                    continue
                label_parts.append(m["name"])
                if len(label_parts) >= 3:
                    break
            label = " × ".join(label_parts)
            clusters.append({
                "theme": seed_tok,
                "label": label,
                "kind": seed_kind,
                "size": 0,
                "share": 0.0,
                "members": members,
                "member_details": member_details,
                "title_terms": title_members,
                "subthemes": [],
                "representatives": [],
                "avg_rating": 0.0,
                "year_range": [],
                "singleton": not members and not title_members,
            })

        # -- 7) 把每部作品和它命中的簇做映射（每部作品 → 它有最多命中的簇） --
        cluster_ids: Dict[int, List[int]] = {}  # cluster_index → token ids
        for i, c in enumerate(clusters):
            wanted = {c["theme"], *c["members"], *c["title_terms"]}
            ids = {tok2id[t] for t in wanted if t in tok2id}
            if ids:
                cluster_ids[i] = sorted(ids)

        # 统计：把每部作品分到「token 命中数最多」的簇（避免重复计数）
        cluster_size: Dict[int, int] = {i: 0 for i in range(len(clusters))}
        cluster_movies: Dict[int, List[int]] = {i: [] for i in range(len(clusters))}
        for mid, ms in movie_seeds.items():
            if not ms:
                continue
            best_i, best_n = -1, 0
            for i, ids in cluster_ids.items():
                hit = len(ms & set(ids))
                if hit > best_n:
                    best_i, best_n = i, hit
            if best_i >= 0 and best_n >= 2:  # 至少命中 2 个 token 才计入该簇
                cluster_size[best_i] += 1
                cluster_movies[best_i].append(mid)

        # v1.3.0：过滤过小的簇（结果更干净），并算占比
        keep = [i for i in range(len(clusters)) if cluster_size.get(i, 0) >= min_size]
        if not keep:  # 全部太小时退化为保留最大的几个，避免空结果
            keep = sorted(range(len(clusters)),
                          key=lambda i: -cluster_size.get(i, 0))[:10]
        idx_map = {old: new for new, old in enumerate(keep)}
        clusters = [clusters[i] for i in keep]
        for new_i, old_i in enumerate(keep):
            clusters[new_i]["size"] = cluster_size.get(old_i, 0)
            clusters[new_i]["share"] = round(
                cluster_size.get(old_i, 0) / max(1, total_movies), 4)
            clusters[new_i]["_movie_ids"] = cluster_movies.get(old_i, [])

        # v1.3.0：簇统计（平均评分 / 年份跨度） + 代表作品
        for c in clusters:
            mids = c.pop("_movie_ids", []) or []
            c["_mids"] = mids
        for c in clusters:
            wanted_tokens = {c["theme"], *c["members"], *c["title_terms"]}
            wanted_ids = {tok2id[t] for t in wanted_tokens if t in tok2id}
            mids = c.get("_mids", []) or []
            if mids:
                # 评分 / 年份：只对前 800 部抽样统计，避免大簇拖慢
                sample = mids[:800]
                ph = ",".join("?" * len(sample))
                row = conn.execute(
                    f"SELECT AVG(userrating) avg_r, MIN(year) ymin, MAX(year) ymax "
                    f"FROM movies WHERE id IN ({ph}) AND userrating>0", sample).fetchone()
                c["avg_rating"] = round(float(row["avg_r"] or 0.0), 2)
                yrs = [y for y in (row["ymin"], row["ymax"]) if y]
                c["year_range"] = sorted(set(int(y) for y in yrs))
            if not wanted_tokens:
                continue
            ms_cands: List[Tuple[int, float]] = []  # (movie_id, match_score)
            for mid, ms in movie_seeds.items():
                inter = len(ms & wanted_ids)
                if inter < 2:
                    continue
                ms_cands.append((mid, inter))
            ms_cands.sort(key=lambda x: -x[1])
            top_n_ids = ms_cands[:limit_representatives]
            if not top_n_ids:
                c["representatives"] = []
                continue
            ids = [m[0] for m in top_n_ids]
            ph = ",".join("?" * len(ids))
            row_by_id: Dict[int, Dict[str, Any]] = {}
            for r in conn.execute(
                    f"SELECT id, num, title, path, userrating FROM movies WHERE id IN ({ph})",
                    ids):
                row_by_id[r["id"]] = {
                    "num": r["num"], "title": r["title"] or "",
                    "path": r["path"] or "", "rating": r["userrating"] or 0.0,
                }
            # 按「命中 token 数」的顺序输出（SQL 的 IN 查询不保证顺序）
            c["representatives"] = [row_by_id[m] for m in ids if m in row_by_id]

        # v1.3.0：子主题 —— 簇内成员两两共现强度 top3
        for c in clusters:
            names = [c["theme"]] + list(c["members"]) + list(c["title_terms"])
            ids = [tok2id[t] for t in names if t in tok2id]
            freq_of = {t: (tag_freq.get(t) or title_freq.get(t) or 1) for t in names}
            pairs: List[Tuple[float, str, str, int]] = []
            for i in range(len(ids)):
                for j in range(i + 1, len(ids)):
                    a, b = ids[i], ids[j]
                    key = (a, b) if a < b else (b, a)
                    cnt = co.get(key, 0)
                    if cnt < 2:
                        continue
                    fa = freq_of.get(names[i], 1)
                    fb = freq_of.get(names[j], 1)
                    pairs.append((cnt / (fa + fb - cnt + 1e-9), names[i], names[j], cnt))
            pairs.sort(key=lambda x: -x[0])
            seen_tok: set = set()
            subs: List[Dict[str, Any]] = []
            for strength, na, nb, cnt in pairs:
                if len(subs) >= 3:
                    break
                if na in seen_tok and nb in seen_tok:
                    continue
                seen_tok.update((na, nb))
                subs.append({"label": f"{na} × {nb}", "count": cnt,
                             "strength": round(strength, 3)})
            c["subthemes"] = subs

        # -- 8) v1.3.0：大簇再细分（二级子主题） -----------------------
        # 覆盖超过 ``max_share``（默认 20%）的簇说明还能再切，
        # 在簇内作品集合上重新做一次局部共现 + 贪心，产出 children。
        id2tok = {v: k for k, v in tok2id.items()}
        for c in clusters:
            c.setdefault("children", [])
            if c.get("share", 0) < max_share or c["size"] < 200:
                continue
            mids = set(c.get("_mids", []) or [])
            if not mids:
                continue
            parent_tokens = {c["theme"], *c["members"], *c["title_terms"]}
            # 簇内局部频次 + 共现（只用簇内作品）
            local_freq: Dict[int, int] = defaultdict(int)
            local_co: Dict[Tuple[int, int], int] = {}
            for mid in mids:
                ms = movie_seeds.get(mid)
                if not ms:
                    continue
                lst = sorted(ms)
                for t in lst:
                    local_freq[t] += 1
                for i in range(len(lst)):
                    for j in range(i + 1, len(lst)):
                        a, b = lst[i], lst[j]
                        if a > b:
                            a, b = b, a
                        local_co[(a, b)] = local_co.get((a, b), 0) + 1
            pool = [t for t, f in local_freq.items()
                    if id2tok.get(t) not in parent_tokens and f >= max(5, len(mids) * 0.01)]
            pool.sort(key=lambda t: -local_freq[t])
            used_local: set = set()
            children: List[Dict[str, Any]] = []
            lmin_co = max(3, int(len(mids) * 0.005))
            for seed_id in pool:
                if len(children) >= 4 or seed_id in used_local:
                    break
                used_local.add(seed_id)
                sname = id2tok.get(seed_id, "")
                cands_l: List[Tuple[float, int]] = []
                for other in pool:
                    if other in used_local or other == seed_id:
                        continue
                    a, b = (seed_id, other) if seed_id < other else (other, seed_id)
                    cnt = local_co.get((a, b), 0)
                    if cnt < lmin_co:
                        continue
                    strength = cnt / (local_freq[seed_id] + local_freq[other] - cnt + 1e-9)
                    cands_l.append((strength, other))
                cands_l.sort(key=lambda x: -x[0])
                picks = [seed_id] + [o for _, o in cands_l[:4]]
                for p in picks:
                    used_local.add(p)
                names_l: List[str] = []
                for p in picks:
                    nm = id2tok.get(p, "")
                    if nm and nm not in names_l:
                        names_l.append(nm)
                if len(names_l) < 2:
                    continue
                # 子簇规模：簇内命中 ≥2 个子主题 token 的作品数
                pid = {p for p in picks}
                hit = sum(1 for mid in mids
                          if len((movie_seeds.get(mid) or set()) & pid) >= 2)
                if hit < max(20, c["size"] * 0.02):
                    continue
                children.append({
                    "theme": names_l[0],
                    "label": " × ".join(names_l[:3]),
                    "size": hit,
                    "share": round(hit / max(1, total_movies), 4),
                    "members": names_l[1:],
                    "representatives": [],
                })
            if children:
                children.sort(key=lambda d: -d["size"])
                c["children"] = children
                # 子主题的代表作品（各取 2 部）
                for ch in children:
                    wnames = {ch["theme"], *ch["members"]}
                    wid = {tok2id[t] for t in wnames if t in tok2id}
                    sc: List[Tuple[int, int]] = []
                    for mid in mids:
                        inter = len((movie_seeds.get(mid) or set()) & wid)
                        if inter >= 2:
                            sc.append((mid, inter))
                    sc.sort(key=lambda x: -x[1])
                    top_ids = [m for m, _ in sc[:2]]
                    if top_ids:
                        ph = ",".join("?" * len(top_ids))
                        ch["representatives"] = [{
                            "num": r["num"], "title": r["title"] or "",
                            "path": r["path"] or "", "rating": r["userrating"] or 0.0,
                        } for r in conn.execute(
                            f"SELECT num, title, path, userrating FROM movies "
                            f"WHERE id IN ({ph})", top_ids)]

        for c in clusters:
            c.pop("_mids", None)
        clusters.sort(key=lambda c: -c["size"])
        return clusters

    # 兼容老接口
    def cluster_tags(self, store: Any, top_n: int = 60, min_co: int = 3,
                     **kwargs: Any) -> List[Dict[str, Any]]:
        """向后兼容 v1.1.3 之前的 cluster_tags 调用。
        返回仅含 tag 成员的主题结构，兼容老的 UI 渲染路径。
        """
        themes = self.cluster_themes(store, top_n=top_n, min_co=min_co,
                                     with_title_terms=False, **kwargs)
        out: List[Dict[str, Any]] = []
        for c in themes:
            out.append({
                "theme": c["theme"],
                "size": c["size"],
                "members": c["members"],
                "singleton": c["singleton"],
            })
        return out

    def similar_works(self, store: Any, num: str, limit: int = 10,
                      min_shared: int = 1) -> List[Dict[str, Any]]:
        """基于标签 + 演员 Jaccard 相似度的相似作品推荐（离线、零依赖）。

        与目标作品共享标签 / 演员越多、并集越小，相似度越高。MiniLM 可用时可用
        句向量余弦相似度替代；此处保证无本地 AI 也能给出可用的「猜你喜欢」。

        v1.2.0：底层改为 :func:`nfo_profiler.recommender.similar_by_id`
        的批量 SQL 实现（GROUP BY 聚合 + 候选池截断 + IN 批量精算），
        热门标签（候选数万部）下从数分钟降到亚秒级；旧版逐候选 3 次查询已废弃。
        """
        from .recommender import similar_by_id  # 惰性导入避免循环依赖
        conn = store.conn
        row = conn.execute("SELECT id FROM movies WHERE num=?", (num,)).fetchone()
        if not row:
            return []
        picks = similar_by_id(conn, row["id"], limit=limit + 10)
        out: List[Dict[str, Any]] = []
        for p in picks:
            if p["shared_tags"] + p["shared_actors"] < min_shared:
                continue
            out.append({
                "num": p["num"], "title": p["title"], "studio": p["studio"],
                "userrating": p["userrating"], "path": p["path"],
                "score": p["score"],
                "shared_tags": p["shared_tags"], "shared_actors": p["shared_actors"],
            })
        out.sort(key=lambda x: (-x["score"], -x["shared_tags"]))
        return out[:limit]

    # ------------------------------------------------------------------
    # 离线 AI 能力：用户画像解读（自然语言总结）
    # ------------------------------------------------------------------
    def interpret(self, store: Any, top_tags: int = 12, top_actors: int = 6,
                  top_studios: int = 6) -> Dict[str, Any]:
        """生成「用户画像解读」文本。

        - Ollama 可用：把统计摘要交给本地 LLM 生成自然语言解读；
        - 否则：基于统计的规则化中文叙述（零依赖、离线可用）。

        为保持模块加载零重依赖、不触发循环 import，``Analyzer`` 在此处惰性导入。
        """
        from .analyze import Analyzer
        data = Analyzer(store).build_report_data(
            top_tags=top_tags, top_actors=top_actors, top_misc=top_studios,
            with_cooccurrence=False, with_keywords=False)
        stats = self._summarize_stats(data, data.get("overview", {}))
        if self.backend == "ollama":
            try:
                text = self._ollama_interpret(stats)
                if text:
                    return {"backend": "ollama", "text": text, "stats": stats}
            except Exception:
                pass
        return {"backend": self.backend, "text": self._heuristic_interpret(stats), "stats": stats}

    @staticmethod
    def _summarize_stats(data: Dict[str, Any], ov: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "total": ov.get("total", 0),
            "avg_rating": ov.get("avg_rating"),
            "year_min": ov.get("year_min"),
            "year_max": ov.get("year_max"),
            "total_days": ov.get("total_days"),
            "distinct": ov.get("distinct", {}),
            "tags": [(t["name"], t["count"]) for t in data.get("tags", [])[:12]],
            "actors": [(a["name"], a["count"]) for a in data.get("actors", [])[:6]],
            "studios": [(s["name"], s["count"]) for s in data.get("studios", [])[:6]],
        }

    @staticmethod
    def _heuristic_interpret(stats: Dict[str, Any]) -> str:
        if not stats.get("total"):
            return "数据库还没有作品，请先扫描 NFO 目录后再生成画像解读。"
        parts = []
        head = "你的片库共收录 %d 部作品" % stats["total"]
        if stats.get("avg_rating") is not None:
            head += "，平均评分 %.2f" % stats["avg_rating"]
        if stats.get("year_min"):
            head += "，时间跨度 %s–%s" % (stats["year_min"], stats["year_max"])
        parts.append(head + "。")
        if stats.get("tags"):
            top = "、".join("%s（%d）" % (n, c) for n, c in stats["tags"][:6])
            parts.append("题材偏好上以 %s 等为主。" % top)
        if stats.get("actors"):
            parts.append("出镜较多的演员包括 %s。" % "、".join(n for n, _ in stats["actors"][:5]))
        if stats.get("studios"):
            parts.append("常看的片商 / 厂牌有 %s。" % "、".join(n for n, _ in stats["studios"][:5]))
        d = stats.get("distinct", {})
        if d:
            parts.append("整体覆盖 %s 个标签、%s 位演员、%s 家片商。"
                         % (d.get("tags", "?"), d.get("actors", "?"), d.get("studios", "?")))
        return "".join(parts)

    def _ollama_interpret(self, stats: Dict[str, Any]) -> str:
        if self.backend != "ollama" or not self._ollama_models:
            return ""
        model = self._ollama_models[0]
        brief = ("片库共%d部，平均评分%s，年份%s-%s。高频标签：%s。常看演员：%s。常看片商：%s。"
                 % (stats.get("total", 0), stats.get("avg_rating", ""),
                    stats.get("year_min", ""), stats.get("year_max", ""),
                    "、".join(n for n, _ in stats["tags"][:10]),
                    "、".join(n for n, _ in stats["actors"][:5]),
                    "、".join(n for n, _ in stats["studios"][:5])))
        prompt = ("你是一个「用户观影偏好画像」分析师。下面是一份成人影片库的基础统计，"
                  "请用 3-4 句中文，自然、客观、不低俗地总结这位用户的题材偏好与观看倾向，"
                  "不要编造具体作品名。统计：" + brief)
        payload = json.dumps({"model": model, "prompt": prompt, "stream": False}).encode("utf-8")
        req = urllib.request.Request(
            "%s/api/generate" % OLLAMA_HOST, data=payload,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=8.0) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
        return (data.get("response") or "").strip()

    # ------------------------------------------------------------------
    # 语义向量检索（需 Ollama / MiniLM；离线启发式不启用）
    # ------------------------------------------------------------------
    def _embed_texts(self, texts: List[str]) -> List[List[float]]:
        """批量取文本向量。Ollama 走 /api/embed，MiniLM 走 onnxruntime。"""
        if self.backend == "ollama" and self._ollama_models:
            model = self._ollama_models[0]
            payload = json.dumps({"model": model, "input": texts}).encode("utf-8")
            req = urllib.request.Request(
                "%s/api/embed" % OLLAMA_HOST, data=payload,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30.0) as resp:
                data = json.loads(resp.read().decode("utf-8", "ignore"))
            return [[float(x) for x in e] for e in (data.get("embeddings") or [])]
        if self.backend == "minilm":
            rt = self.encode(texts)
            return rt or []
        raise RuntimeError("no embedding backend available")

    def build_index(self, store: Any, batch: int = 64, conn: Any = None) -> int:
        """为全库作品生成并存储语义向量（后台线程调用）。返回成功条数。

        ``conn`` 可选：传入独立连接时，构建过程不占用 Store 的主连接，
        在 WAL 模式下写库不会阻塞 Web 端读库（扫描/检索并发更顺滑）。
        """
        conn = conn or store.conn
        rows = conn.execute("SELECT id, title FROM movies").fetchall()
        ids, texts = [], []
        for r in rows:
            mid = r["id"]
            tags = [t["tag"] for t in conn.execute("SELECT tag FROM movie_tags WHERE movie_id=?", (mid,))]
            actors = [a["actor"] for a in conn.execute("SELECT actor FROM movie_actors WHERE movie_id=?", (mid,))]
            text = " ".join(filter(None, [(r["title"] or ""), " ".join(tags), " ".join(actors)]))
            ids.append(mid)
            texts.append(text)
        model = self.backend if self.backend in ("ollama", "minilm") else "unknown"
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        total = 0
        for i in range(0, len(texts), batch):
            chunk = texts[i:i + batch]
            try:
                vecs = self._embed_texts(chunk)
            except Exception:
                break
            if not vecs:
                break
            for mid, v in zip(ids[i:i + batch], vecs):
                if not v:
                    continue
                blob = struct.pack("<%df" % len(v), *v)
                conn.execute(
                    "INSERT OR REPLACE INTO embeddings(movie_id, vec, dim, model, updated_at) "
                    "VALUES(?,?,?,?,?)", (mid, blob, len(v), model, now))
                total += 1
        # 全量构建成功后，清理其它模型（Ollama/MiniLM 切换）残留的旧向量，
        # 避免语义检索混入维度/语义不一致的垃圾。构建中途失败则保留既有好数据。
        if total:
            try:
                conn.execute("DELETE FROM embeddings WHERE model <> ?", (model,))
            except Exception:
                pass
        conn.commit()
        return total

    def semantic_search(self, store: Any, query: str, limit: int = 20) -> Optional[List[Dict[str, Any]]]:
        """基于查询向量与库内向量的余弦相似度做语义检索。无向量后端时返回 None。"""
        if self.backend not in ("ollama", "minilm"):
            return None
        qv = self._embed_texts([query])
        if not qv:
            return []
        q = [float(x) for x in qv[0]]
        qn = math.sqrt(sum(x * x for x in q)) or 1.0
        conn = store.conn
        # 只比较「当前模型」写入的向量，避免 Ollama/MiniLM 切换后维度/语义不一致的旧向量污染结果
        rows = conn.execute(
            "SELECT movie_id, vec, dim FROM embeddings WHERE model=?", (self.backend,)
        ).fetchall()
        if not rows:
            return []
        scored = []
        for r in rows:
            dim = r["dim"]
            vec = struct.unpack("<%df" % dim, r["vec"])
            dot = sum(a * b for a, b in zip(q, vec))
            dn = math.sqrt(sum(x * x for x in vec)) or 1.0
            scored.append((dot / (qn * dn), r["movie_id"]))
        scored.sort(key=lambda x: -x[0])
        out: List[Dict[str, Any]] = []
        for sim, mid in scored[:limit]:
            m = conn.execute(
                "SELECT num, title, studio, userrating FROM movies WHERE id=?", (mid,)).fetchone()
            if not m:
                continue
            out.append({"num": m["num"], "title": m["title"], "studio": m["studio"],
                        "userrating": m["userrating"], "score": round(sim, 4)})
        return out

    # ------------------------------------------------------------------
    # 语义向量（预留：MiniLM 可用时做句向量编码；本期不强制，仅占位）
    # ------------------------------------------------------------------
    def encode(self, texts: List[str]) -> Optional[List[List[float]]]:
        """MiniLM 句向量编码；不可用时返回 None（调用方须自行降级）。"""
        with self.lock:
            if self.backend != "minilm" or not self._rt:
                return None
            sess, tokenizer = self._rt
        # 注意：本函数若在请求线程调用也不应长时间持锁，故句柄取出后即释放锁
        out = []
        for t in texts:
            enc = tokenizer(t, padding=True, truncation=True)
            feeds = {sess.get_inputs()[0].name: _to_ints(enc["input_ids"]),
                     sess.get_inputs()[1].name: _to_ints(enc["attention_mask"])}
            vec = sess.run(None, feeds)[0]
            out.append([float(x) for x in vec[0].flatten()])
        return out


def _to_ints(seq):
    """兼容 tokenizers 返回的是 list[int] 还是 numpy 数组。"""
    try:
        import numpy as np  # noqa: F401
        if hasattr(seq, "tolist"):
            return seq.tolist()
    except Exception:
        pass
    return list(seq)


__all__ = ["AIEngine"]
