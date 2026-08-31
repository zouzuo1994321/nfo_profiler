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
from datetime import datetime
from typing import Any, Dict, List, Optional

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
    def cluster_tags(self, store: Any, top_n: int = 50, min_co: int = 3) -> List[Dict[str, Any]]:
        """基于标签共现的贪心主题聚类（离线、零依赖）。

        取高频标签作为种子，把与其在 >= ``min_co`` 部作品里共同出现过的标签归入同一主题。
        MiniLM 可用时可改为句向量聚类；Ollama 可用时可让模型归纳主题名。本实现保证
        即使没有任何本地 AI 环境也能产出可用的「题材 / 主题」分组。
        """
        conn = store.conn
        top = [r[0] for r in conn.execute(
            "SELECT tag, COUNT(*) c FROM movie_tags GROUP BY tag ORDER BY c DESC LIMIT ?", (top_n,))]
        if not top:
            return []
        ph = ",".join("?" * len(top))
        freq = {t: c for t, c in conn.execute(
            f"SELECT tag, COUNT(*) c FROM movie_tags WHERE tag IN ({ph}) GROUP BY tag", top)}
        # 共现统计：只扫含高频标签的作品
        movie_tags: Dict[int, set] = {}
        for mid, tag in conn.execute(
                f"SELECT movie_id, tag FROM movie_tags WHERE tag IN ({ph})", top):
            movie_tags.setdefault(mid, set()).add(tag)
        co: Dict[tuple, int] = {}
        for tags in movie_tags.values():
            lt = [t for t in tags if t in top]
            for i in range(len(lt)):
                for j in range(i + 1, len(lt)):
                    a, b = lt[i], lt[j]
                    key = (a, b) if a < b else (b, a)
                    co[key] = co.get(key, 0) + 1
        # 贪心聚类：按频率降序取未用标签作种子
        used: set = set()
        clusters: List[Dict[str, Any]] = []
        for seed in sorted(top, key=lambda t: -freq.get(t, 0)):
            if seed in used:
                continue
            members = [seed]
            used.add(seed)
            for t in top:
                if t in used:
                    continue
                key = (seed, t) if seed < t else (t, seed)
                if co.get(key, 0) >= min_co:
                    members.append(t)
                    used.add(t)
            clusters.append({"theme": seed, "size": freq.get(seed, 0),
                             "members": members, "singleton": len(members) == 1})
        clusters.sort(key=lambda c: -c["size"])
        return clusters

    def similar_works(self, store: Any, num: str, limit: int = 10,
                      min_shared: int = 1) -> List[Dict[str, Any]]:
        """基于标签 + 演员 Jaccard 相似度的相似作品推荐（离线、零依赖）。

        与目标作品共享标签 / 演员越多、并集越小，相似度越高。MiniLM 可用时可用
        句向量余弦相似度替代；此处保证无本地 AI 也能给出可用的「猜你喜欢」。
        """
        conn = store.conn
        row = conn.execute("SELECT id, num FROM movies WHERE num=?", (num,)).fetchone()
        if not row:
            return []
        mid = row["id"]
        t_tags = {t["tag"] for t in conn.execute("SELECT tag FROM movie_tags WHERE movie_id=?", (mid,))}
        t_actors = {a["actor"] for a in conn.execute("SELECT actor FROM movie_actors WHERE movie_id=?", (mid,))}
        if not t_tags and not t_actors:
            return []
        cand: set = set()
        if t_tags:
            ph = ",".join("?" * len(t_tags))
            for r in conn.execute(
                    f"SELECT DISTINCT movie_id FROM movie_tags WHERE tag IN ({ph}) AND movie_id<>?",
                    list(t_tags) + [mid]):
                cand.add(r[0])
        t_set = t_tags | t_actors
        out: List[Dict[str, Any]] = []
        for cmid in cand:
            c_tags = {t["tag"] for t in conn.execute("SELECT tag FROM movie_tags WHERE movie_id=?", (cmid,))}
            c_actors = {a["actor"] for a in conn.execute("SELECT actor FROM movie_actors WHERE movie_id=?", (cmid,))}
            c_set = c_tags | c_actors
            inter = len((t_tags & c_tags) | (t_actors & c_actors))
            if inter < min_shared:
                continue
            union = len(t_set | c_set)
            j = round(inter / union, 3) if union else 0
            m = conn.execute(
                "SELECT num, title, studio, userrating FROM movies WHERE id=?", (cmid,)).fetchone()
            out.append({
                "num": m["num"], "title": m["title"], "studio": m["studio"],
                "userrating": m["userrating"], "score": j,
                "shared_tags": len(t_tags & c_tags), "shared_actors": len(t_actors & c_actors),
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
