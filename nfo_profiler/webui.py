# -*- coding: utf-8 -*-
"""本地 Web 界面（双击即用）。

只用标准库 http.server 实现，无需 FastAPI/Flask。核心模式零第三方依赖；
可选的「AI 增强」开关默认关闭，开启后在后台线程惰性检测本地 AI 环境
（Ollama / MiniLM），检测不到则自动回退到本地启发式检索——绝不 import torch，
也绝不阻塞主线程：
  * 左侧：目录浏览器（选一个或多个 NFO 目录）→ 开始扫描（实时进度）→ 生成报告 → 导出
  * 右侧：画像报告（复用 report.py 产出的单文件报告，iframe 内嵌）+ 明细检索
  * 纯本地运行：解析 → 画像统计 → 报告 → 导出，全部基于 Python 标准库完成。

设计要点（满足"刷新不杀进程 / 关页后台继续扫描"）：
  * 扫描放在**后台守护线程**，主线程只负责响应 HTTP 轮询；
  * 扫描状态保存在进程级单例 AppState 里，与任何一次 HTTP 请求无关；
  * 页面用 JS 轮询 /api/scan/state 读取同一份状态，刷新或重开页面都只是重新轮询，
    不会重启、也不会中断正在进行的扫描；
  * 服务器由主线程 serve_forever 常驻，关掉浏览器标签不会结束进程。
"""

from __future__ import annotations

import json
import os
import re
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from . import APP_NAME, __version__
from .ai_engine import AIEngine
from .analyze import Analyzer
from .exporter import DETAIL_COLUMNS, export_all, iter_movie_rows
from .normalize import Normalizer, resolve_config_paths
from .scanner import scan_paths
from .store import Store


# ---------------------------------------------------------------------------
# 应用状态
# ---------------------------------------------------------------------------

class AppState:
    """全局状态：数据库句柄 + 后台扫描任务 + 最近一次分析结果 + 已选目录。"""

    def __init__(self, db_path: str, out_dir: str, config: Optional[str] = None) -> None:
        self.db_path = db_path
        self.out_dir = os.path.abspath(out_dir)
        self.config = config
        os.makedirs(self.out_dir, exist_ok=True)
        self.norm = Normalizer.from_files(*resolve_config_paths(config))
        self.store = Store(db_path, normalizer=self.norm)
        self.lock = threading.RLock()

        self.selected_dirs: List[str] = []

        self.scan: Dict[str, Any] = {
            "running": False, "phase": "idle", "done": 0, "total": 0,
            "message": "空闲", "result": None, "errors": [],
            "started_at": "", "current": "", "log": [],
        }
        self.last_report: Dict[str, Any] = {"path": "", "generated_at": "", "elapsed": 0}
        self.last_data: Optional[Dict[str, Any]] = None
        self.last_export: Dict[str, Any] = {}

        # 可选本地 AI 增强引擎（惰性、绝不阻塞主线程；缺环境时自动回退启发式）
        self.ai = AIEngine()
        # 用户实际词表缓存（标签/片商/演员），供离线启发式检索扫描连续中文自然语言
        self._ai_vocab_cache: Optional[List[str]] = None
        # AI 标签主题聚类缓存（扫描后失效）
        self._ai_clusters_cache: Optional[List[Dict[str, Any]]] = None
        # 语义向量索引构建状态（后台线程；需 Ollama / MiniLM 后端）
        self._ai_index: Dict[str, Any] = {
            "running": False, "done": 0, "total": 0,
            "message": "未构建", "error": None,
        }

    # -- 目录选择 ------------------------------------------------------
    def _norm(self, path: str) -> str:
        return os.path.abspath(os.path.expanduser(str(path).strip().strip('"')))

    def add_dir(self, path: str) -> List[str]:
        p = self._norm(path)
        if not os.path.isdir(p):
            return self.selected_dirs
        if p not in self.selected_dirs:
            self.selected_dirs.append(p)
        return list(self.selected_dirs)

    def remove_dir(self, path: str) -> List[str]:
        p = self._norm(path)
        self.selected_dirs = [d for d in self.selected_dirs if d != p]
        return list(self.selected_dirs)

    def clear_dirs(self) -> List[str]:
        self.selected_dirs = []
        return []

    # -- 扫描 ----------------------------------------------------------
    def start_scan(self, roots: List[str], workers: Optional[int], full: bool,
                   probe_video: bool) -> None:
        if self.scan["running"]:
            return
        roots = [self._norm(r) for r in (roots or []) if r and os.path.isdir(self._norm(r))]
        if not roots:
            self.scan.update({
                "running": False, "phase": "done", "message": "没有可扫描的目录",
                "result": {"error": "请先选择至少一个有效的 NFO 目录"},
            })
            return

        self.scan.update({
            "running": True, "phase": "start", "done": 0, "total": 0,
            "message": "正在启动…", "result": None, "errors": [],
            "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "current": "", "log": [],
        })

        def _progress(phase: str, done: int, total: int, msg: str) -> None:
            self.scan["phase"] = phase
            self.scan["done"] = done
            self.scan["total"] = total
            self.scan["message"] = msg
            log = self.scan["log"]
            log.append(f"[{phase}] {msg}")
            if len(log) > 80:
                del log[: len(log) - 80]

        def _on_file(path: str) -> None:
            self.scan["current"] = path

        def _run() -> None:
            try:
                with self.lock:
                    res = scan_paths(
                        roots, self.store, workers=workers, incremental=not full,
                        probe_video=probe_video, progress=_progress, on_file=_on_file,
                    )
                d = res.as_dict()
                self.scan["result"] = d
                self.scan["errors"] = d.get("errors", [])
                self.scan["message"] = "扫描完成"
            except Exception as exc:
                self.scan["result"] = {"error": f"{exc.__class__.__name__}: {exc}"}
                self.scan["message"] = f"扫描失败：{exc}"
            finally:
                self.scan["running"] = False
                self.scan["phase"] = "done"
                self.scan["current"] = ""
                self._ai_vocab_cache = None  # 词表可能已变化，下次检索前重建
                self._ai_clusters_cache = None  # 聚类结果可能已变化，下次分析前重建

        threading.Thread(target=_run, daemon=True).start()

    # -- 分析 ----------------------------------------------------------
    def build_report(self, **kw: Any) -> Dict[str, Any]:
        report_movies = int(kw.pop("report_movies", 1000) or 1000)
        top_tags = int(kw.pop("top_tags", 40) or 40)
        top_actors = int(kw.pop("top_actors", 30) or 30)
        with self.lock:
            t0 = time.time()
            data = Analyzer(self.store).build_report_data(
                top_tags=top_tags, top_actors=top_actors, **kw)
            movies = []
            for row in iter_movie_rows(self.store, limit=report_movies):
                movies.append({
                    "num": row.get("num", ""),
                    "title": (row.get("title", "") or "")[:46],
                    "year": row.get("year", ""),
                    "premiered": row.get("premiered", ""),
                    "studio": row.get("studio", ""),
                    "director": row.get("director", ""),
                    "actors": (row.get("actors", "") or "")[:40],
                    "tags": (row.get("tags", "") or "")[:46],
                    "userrating": row.get("userrating", ""),
                    "runtime_min": row.get("runtime_min", ""),
                    "resolution": row.get("resolution", ""),
                })
            from .report import write_html_report
            path = write_html_report(
                data, os.path.join(self.out_dir, "用户画像报告.html"),
                movies=movies, source_label=self.db_path,
                top_tags=top_tags, top_actors=top_actors,
            )
            self.last_data = data
            self.last_report = {
                "path": path,
                "generated_at": data.get("generated_at", ""),
                "elapsed": round(time.time() - t0, 2),
                "size_kb": round(os.path.getsize(path) / 1024, 1),
                "movies": len(movies),
            }
            return self.last_report

    def export(self, formats: List[str], movie_limit: int = 0, report_movies: int = 1000) -> Dict[str, Any]:
        with self.lock:
            data = self.last_data or Analyzer(self.store).build_report_data()
            res = export_all(
                data, os.path.join(self.out_dir, "导出"),
                store=self.store,
                formats=[f for f in formats if f != "html"],
                html="html" in formats,
                movie_limit=movie_limit,
                report_movies=report_movies,
                source_label=self.db_path,
            )
            files: List[str] = []
            for v in res.values():
                if isinstance(v, list):
                    files.extend(v)
                elif isinstance(v, str) and v:
                    files.append(v)
            self.last_export = {"files": files, "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
            return self.last_export

    def movies(self, q: str = "", limit: int = 50, offset: int = 0) -> Dict[str, Any]:
        # AI 查询扩展（同义词 / Ollama 网络调用）放在锁外，避免阻塞扫描线程
        ai_base: List[str] = []
        if self.ai.enabled:
            ai_base = self.ai.expand_terms(q, self.norm)
        with self.lock:
            conn = self.store.conn
            # 扫描用户实际词表（标签/片商/演员）：覆盖「健身房题材巨乳作品」这类
            # 连续中文自然语言——只要查询里出现了库中的某个词，就纳入 OR 检索。
            ai_terms = list(ai_base)
            if self.ai.enabled:
                for w in self._ai_vocab():
                    if w and w in q and w not in ai_terms:
                        ai_terms.append(w)
            where, params = self._movie_where(q, ai_terms)
            total = int(conn.execute(f"SELECT COUNT(*) c FROM movies {where}", params).fetchone()["c"])
            rows = []
            sql = (f"SELECT num, title, year, premiered, studio, director, userrating, "
                   f"runtime_min, resolution, censor_status, id FROM movies {where} "
                   f"ORDER BY userrating DESC, id LIMIT ? OFFSET ?")
            for r in conn.execute(sql, params + [limit, offset]):
                d = dict(r)
                tags = [x["tag"] for x in conn.execute(
                    "SELECT tag FROM movie_tags WHERE movie_id=?", (d["id"],))]
                actors = [x["actor"] for x in conn.execute(
                    "SELECT actor FROM movie_actors WHERE movie_id=?", (d.pop("id"),))]
                d["tags"] = " / ".join(tags)
                d["actors"] = " / ".join(actors)
                rows.append(d)
            return {"total": total, "rows": rows, "offset": offset, "limit": limit,
                    "ai": bool(ai_terms), "backend": self.ai.backend if ai_terms else "none"}

    def _ai_vocab(self) -> List[str]:
        """返回用户库里实际出现过的词（标签 / 片商 / 演员），供离线启发式扫描。

        结果缓存；扫描完成后由 ``start_scan`` 置空以触发重建。
        """
        if self._ai_vocab_cache is None:
            conn = self.store.conn
            words: List[str] = []
            for sql in (
                "SELECT DISTINCT tag FROM movie_tags",
                "SELECT DISTINCT studio FROM movies WHERE studio <> ''",
                "SELECT DISTINCT actor FROM movie_actors",
            ):
                try:
                    words.extend(r[0] for r in conn.execute(sql))
                except Exception:
                    pass
            self._ai_vocab_cache = [w for w in words if w]
        return self._ai_vocab_cache

    def ai_clusters(self) -> List[Dict[str, Any]]:
        """标签主题聚类（AI 增强开启时调用），结果缓存，扫描后失效。"""
        if self._ai_clusters_cache is None:
            self._ai_clusters_cache = self.ai.cluster_tags(self.store)
        return self._ai_clusters_cache

    def ai_similar(self, num: str, limit: int = 10) -> List[Dict[str, Any]]:
        """相似作品推荐（AI 增强开启时调用）。"""
        return self.ai.similar_works(self.store, num, limit=limit)

    def ai_interpret(self) -> Dict[str, Any]:
        """AI 画像解读（AI 增强开启时调用）。"""
        return self.ai.interpret(self.store)

    # -- 语义向量检索（需 Ollama / MiniLM 后端 + 已构建索引）-----------
    def ai_index_status(self) -> Dict[str, Any]:
        """返回语义向量索引的构建进度与规模。"""
        with self.lock:
            total = int(self.store.conn.execute(
                "SELECT COUNT(*) c FROM movies").fetchone()["c"])
            indexed = int(self.store.conn.execute(
                "SELECT COUNT(*) c FROM embeddings").fetchone()["c"])
            indexed_model = 0
            if self.ai.backend in ("ollama", "minilm"):
                indexed_model = int(self.store.conn.execute(
                    "SELECT COUNT(*) c FROM embeddings WHERE model=?",
                    (self.ai.backend,)).fetchone()["c"])
            st = dict(self._ai_index)
        st["total_movies"] = total
        st["indexed"] = indexed
        st["indexed_model"] = indexed_model
        st["ready"] = bool(total) and indexed_model >= total
        st["stale"] = bool(total) and indexed_model < total
        return st

    def ai_build_index(self) -> Dict[str, Any]:
        """在后台线程为全库构建语义向量索引（需 Ollama / MiniLM 后端）。"""
        if self._ai_index["running"]:
            return self.ai_index_status()
        if not self.ai.enabled:
            return {"disabled": True}
        if self.ai.backend not in ("ollama", "minilm"):
            return {"unavailable": True,
                    "message": "当前后端不支持语义向量（需本地 Ollama / MiniLM）"}
        self._ai_index.update({
            "running": True, "done": 0, "total": 0,
            "message": "正在构建向量索引…", "error": None,
        })
        threading.Thread(target=self._run_build_index, daemon=True).start()
        return self.ai_index_status()

    def _run_build_index(self) -> None:
        """后台构建：用独立连接写 embeddings（WAL 下不阻塞主连接读）。"""
        import sqlite3 as _sqlite3
        bconn = _sqlite3.connect(self.store.db_path, timeout=30.0, check_same_thread=False)
        bconn.row_factory = _sqlite3.Row
        bconn.execute("PRAGMA busy_timeout=30000")
        try:
            n = self.ai.build_index(self.store, conn=bconn)
        except Exception as exc:
            with self.lock:
                self._ai_index["running"] = False
                self._ai_index["message"] = "构建失败：%s" % exc
                self._ai_index["error"] = "%s: %s" % (exc.__class__.__name__, exc)
            try:
                bconn.close()
            except Exception:
                pass
            return
        total = int(bconn.execute("SELECT COUNT(*) c FROM movies").fetchone()["c"])
        with self.lock:
            self._ai_index["running"] = False
            self._ai_index["done"] = n
            self._ai_index["total"] = total
            self._ai_index["message"] = "已构建 %d 条向量" % n
        try:
            bconn.close()
        except Exception:
            pass

    def ai_search(self, q: str, limit: int = 20) -> Dict[str, Any]:
        """语义向量检索（需 Ollama / MiniLM 后端 + 已构建索引）。"""
        if not self.ai.enabled:
            return {"disabled": True, "results": []}
        if self.ai.backend not in ("ollama", "minilm"):
            return {"unavailable": True, "results": [],
                    "message": "当前后端不支持语义检索（需本地 Ollama / MiniLM）"}
        indexed = int(self.store.conn.execute(
            "SELECT COUNT(*) c FROM embeddings WHERE model=?",
            (self.ai.backend,)).fetchone()["c"])
        if not indexed:
            return {"unavailable": True, "results": [],
                    "message": "语义索引尚未构建，请先点「构建向量索引」"}
        res = self.ai.semantic_search(self.store, q, limit=limit)
        if res is None:
            return {"unavailable": True, "results": [],
                    "message": "语义检索不可用（向量后端异常）"}
        return {"disabled": False, "backend": self.ai.backend,
                "results": res, "indexed": indexed}

    @staticmethod
    def _movie_where(q: str, ai_terms: List[str]) -> "tuple[str, list]":
        """构造 movies 检索的 WHERE 与参数。

        - 有 AI 扩展词：每个词作为 OR 条件，跨 番号/标题/片商/导演/标签/演员 全字段匹配；
        - 否则（无 AI 或扩展为空）：沿用原「整体子串」匹配，行为不变。
        """
        if ai_terms:
            clauses = []
            params: List[str] = []
            for t in ai_terms:
                clauses.append(
                    "(num LIKE ? OR title LIKE ? OR studio LIKE ? OR director LIKE ? "
                    "OR id IN (SELECT movie_id FROM movie_tags WHERE tag LIKE ?) "
                    "OR id IN (SELECT movie_id FROM movie_actors WHERE actor LIKE ?))")
                p = f"%{t}%"
                params.extend([p, p, p, p, p, p])
            return "WHERE " + " OR ".join(clauses), params
        q = (q or "").strip()
        if not q:
            return "", []
        like = f"%{q}%"
        return ("WHERE num LIKE ? OR title LIKE ? OR studio LIKE ? OR director LIKE ? "
                "OR id IN (SELECT movie_id FROM movie_tags WHERE tag LIKE ?) "
                "OR id IN (SELECT movie_id FROM movie_actors WHERE actor LIKE ?)"), [like] * 6


# ---------------------------------------------------------------------------
# 本地文件系统浏览（供网页选目录，避免浏览器安全限制拿不到真实路径）
# ---------------------------------------------------------------------------

def list_drives() -> List[str]:
    drives: List[str] = []
    try:
        import ctypes
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        for i in range(26):
            if mask & (1 << i):
                drives.append(chr(65 + i) + ":\\")
    except Exception:
        for d in "CDEFGH":
            if os.path.exists(d + ":\\"):
                drives.append(d + ":\\")
    return drives or [os.path.abspath(os.sep)]


def list_dir(path: str) -> Dict[str, Any]:
    if not path:
        return {"path": "", "parent": None, "items": []}
    if len(path) == 2 and path[1] == ":":
        path = path + "\\"
    if not os.path.isdir(path):
        return {"error": f"目录不存在：{path}"}
    is_root = bool(re.match(r"^[A-Za-z]:[\\/]?$", path))
    parent = None if is_root else (os.path.dirname(path.rstrip("\\/")) or None)
    if parent == path:
        parent = None
    items: List[Dict[str, str]] = []
    try:
        for name in sorted(os.listdir(path)):
            if name.startswith("."):
                continue
            if name.upper() in ("$RECYCLE.BIN", "SYSTEM VOLUME INFORMATION"):
                continue
            full = os.path.join(path, name)
            if os.path.isdir(full):
                items.append({"name": name, "path": full})
    except PermissionError:
        pass
    return {"path": path, "parent": parent, "items": items[:500]}


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------

_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__APP__ v__VER__</title>
<link rel="icon" href="/api/logo" type="image/png">
<style>
:root{--bg:#0f1115;--card:#1a1f2b;--card2:#202634;--line:#2a3242;--tx:#e6ebf5;
--tx2:#9aa7bd;--tx3:#6b7893;--ac:#4f8cff;--ac2:#17c9a5;--warn:#ff5f6d;--ok:#17c9a5}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);font:14px/1.6 -apple-system,BlinkMacSystemFont,
"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;display:flex;height:100vh;overflow:hidden}
aside{width:360px;background:var(--card);border-right:1px solid var(--line);
display:flex;flex-direction:column;overflow-y:auto;flex:0 0 360px}
main{flex:1;display:flex;flex-direction:column;min-width:0}
header{padding:14px 16px;border-bottom:1px solid var(--line);background:var(--card2)}
header h1{margin:0;font-size:16px;display:flex;align-items:center;gap:8px}
header h1 span{font-size:11px;color:var(--tx3);font-weight:400}
.pad{padding:14px 16px}
.grp{margin-bottom:16px}
label{display:block;font-size:12px;color:var(--tx2);margin-bottom:5px}
input,select{width:100%;background:var(--card2);border:1px solid var(--line);color:var(--tx);
border-radius:7px;padding:8px 10px;font-size:13px}
input:focus,select:focus{outline:none;border-color:var(--ac)}
.row{display:flex;gap:8px}
.row>*{flex:1}
button{background:var(--ac);border:none;color:#fff;border-radius:7px;padding:9px 14px;
font-size:13px;cursor:pointer;transition:.15s}
button:hover{opacity:.88}
button:disabled{opacity:.45;cursor:not-allowed}
button.ghost{background:var(--card2);border:1px solid var(--line);color:var(--tx)}
button.ok{background:var(--ac2)}
button.mini{padding:3px 9px;font-size:11.5px;width:auto}
h3{font-size:12px;color:var(--tx3);margin:0 0 9px;text-transform:uppercase;letter-spacing:.6px;
font-weight:600;border-bottom:1px solid var(--line);padding-bottom:7px}
.kv{display:flex;justify-content:space-between;font-size:12.5px;padding:3px 0;color:var(--tx2)}
.kv b{color:var(--tx);font-weight:600}
.bar{height:7px;background:var(--card2);border-radius:4px;overflow:hidden;margin:8px 0 5px}
.bar i{display:block;height:100%;background:linear-gradient(90deg,#4f8cff,#17c9a5);width:0;transition:width .3s}
.msg{font-size:12px;color:var(--tx2);min-height:17px}
.tag{display:inline-block;background:var(--card2);border:1px solid var(--line);border-radius:5px;
padding:1px 7px;font-size:11.5px;color:var(--tx2);margin:2px 3px 2px 0}
.err{color:var(--warn);font-size:11.5px;max-height:120px;overflow:auto;
background:var(--card2);border-radius:6px;padding:7px 9px;margin-top:7px}
iframe{flex:1;border:none;width:100%;background:#fff}
.tabs{display:flex;gap:6px;padding:9px 14px;border-bottom:1px solid var(--line);background:var(--card2)}
.tabs button{width:auto;padding:6px 15px;font-size:12.5px}
.tabs button.on{background:var(--ac);color:#fff}
.tabs button:not(.on){background:var(--card);color:var(--tx2)}
#detail{display:none;flex:1;overflow:auto;padding:14px 18px}
#ai{display:none;flex:1;overflow:auto;padding:14px 18px}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th,td{padding:6px 9px;border-bottom:1px solid var(--line);text-align:left}
th{color:var(--tx3);font-size:11.5px;position:sticky;top:0;background:var(--bg);cursor:pointer}
td.n{text-align:right;font-variant-numeric:tabular-nums}
.empty{color:var(--tx3);text-align:center;padding:40px}
.hintline{font-size:11.5px;color:var(--tx3);margin-top:6px;line-height:1.5}

/* AI 增强开关 */
.ai-box{margin-top:9px;border:1px solid var(--line);border-radius:8px;padding:9px 10px;background:var(--card2)}
.ai-row{display:flex;align-items:center;justify-content:space-between}
.ai-title{font-size:13px;font-weight:600;color:var(--tx)}
.ai-msg{font-size:11.5px;color:var(--tx2);margin-top:6px;line-height:1.5;min-height:16px}
.ai-msg.ok{color:var(--ok)}
.ai-msg.warn{color:var(--warn)}
.switch{position:relative;display:inline-block;width:42px;height:22px;flex:0 0 auto}
.switch input{opacity:0;width:0;height:0}
.slider{position:absolute;cursor:pointer;inset:0;background:var(--card);border:1px solid var(--line);
border-radius:22px;transition:.2s}
.slider:before{content:"";position:absolute;height:16px;width:16px;left:3px;top:2px;background:var(--tx3);
border-radius:50%;transition:.2s}
.switch input:checked + .slider{background:linear-gradient(90deg,#4f8cff,#17c9a5);border-color:transparent}
.switch input:checked + .slider:before{transform:translateX(20px);background:#fff}


/* 依赖横幅 */
#deps{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}
.chip{font-size:11px;padding:2px 9px;border-radius:11px;border:1px solid var(--line)}
.chip.ok{background:rgba(23,201,165,.14);color:var(--ok);border-color:rgba(23,201,165,.4)}
.chip.warn{background:rgba(255,95,109,.12);color:var(--warn);border-color:rgba(255,95,109,.4)}
.chip.bad{background:rgba(255,95,109,.2);color:var(--warn);border-color:var(--warn)}

/* 目录浏览器 */
.crumb{font-size:12px;color:var(--tx2);padding:7px 0;word-break:break-all}
.crumb a{color:var(--ac);cursor:pointer;text-decoration:none}
.crumb a:hover{text-decoration:underline}
.drives{display:flex;flex-wrap:wrap;gap:5px;margin:4px 0 8px}
.drive{font-size:12px;padding:3px 10px;border:1px solid var(--line);border-radius:6px;
background:var(--card2);cursor:pointer;color:var(--tx)}
.drive:hover{border-color:var(--ac)}
.folders{max-height:200px;overflow:auto;border:1px solid var(--line);border-radius:7px;background:var(--card2)}
.folder{display:flex;align-items:center;justify-content:space-between;padding:5px 9px;
border-bottom:1px solid var(--line);font-size:12.5px;cursor:pointer}
.folder:last-child{border-bottom:none}
.folder:hover{background:var(--card)}
.folder .nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.folders .empty{padding:14px;text-align:center;color:var(--tx3);font-size:12px}
.seldirs{margin-top:6px}
.seldir{border:1px solid var(--line);border-radius:6px;background:var(--card2);
padding:5px 8px;margin-bottom:5px;display:flex;align-items:center;justify-content:space-between;gap:8px}
.seldir .p{font-size:11.5px;color:var(--tx2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.seldir .x{color:var(--warn);cursor:pointer;font-weight:700;flex:0 0 auto}
.slog{font:11px/1.5 ui-monospace,Menlo,Consolas,monospace;color:var(--tx2);
background:var(--card2);border-radius:6px;padding:7px 9px;margin-top:7px;max-height:130px;overflow:auto;
white-space:pre-wrap;word-break:break-all}
.curfile{font-size:11.5px;color:var(--tx3);margin-top:5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
@keyframes spin{to{transform:rotate(360deg)}}
@keyframes prog{from{opacity:.6}to{opacity:1}}
</style>
</head>
<body>
<aside>
  <header><h1><img src="/api/logo" style="width:22px;height:22px;vertical-align:middle;margin-right:6px;border-radius:4px"> __APP__ <span>v__VER__</span></h1></header>
  <div class="pad">

    <div class="grp">
      <h3>运行状态</h3>
      <div class="chip ok">核心模式 · 纯本地运行</div>
      <div class="ai-box" id="aiBox">
        <div class="ai-row">
          <span class="ai-title">🤖 AI 增强</span>
          <label class="switch"><input type="checkbox" id="aiToggle"><span class="slider"></span></label>
        </div>
        <div class="ai-msg" id="aiMsg">关闭</div>
      </div>
      <div class="hintline" style="margin-top:6px">开启后可对检索做自然语言理解 / 同义词扩展。需本地 Ollama 或 MiniLM 模型；未安装则自动回退「本地启发式」增强，开关始终可用。</div>
    </div>

    <div class="grp">
      <h3>选择 NFO 目录（可多选）</h3>
      <div class="drives" id="drives"></div>
      <div class="crumb" id="crumb"></div>
      <div class="folders" id="folders"><div class="empty">加载中…</div></div>
      <div class="row" style="margin-top:7px">
        <button class="ghost mini" id="btnUp">↑ 上级</button>
        <button class="ok mini" id="btnAddCur">＋ 添加当前目录</button>
      </div>
      <div class="row" style="margin-top:7px">
        <input id="manual" placeholder="或手动粘贴路径，如 D:\\影片">
        <button class="ghost mini" id="btnAddManual" style="flex:0 0 auto">添加</button>
      </div>
      <div class="seldirs" id="seldirs"></div>
      <div class="row" style="margin-top:6px">
        <button class="ghost mini" id="btnClear">清空</button>
        <span class="hintline" id="selCount" style="flex:1"></span>
      </div>
    </div>

    <div class="grp">
      <h3>扫描</h3>
      <div class="row">
        <div><label>进程数</label><input id="workers" type="number" value="8" min="1" max="32"></div>
        <div><label style="visibility:hidden">模式</label>
          <select id="mode"><option value="inc">增量扫描</option><option value="full">全量重扫</option></select>
        </div>
      </div>
      <label style="margin-top:8px"><input type="checkbox" id="probe" checked
        style="width:auto;margin-right:6px;vertical-align:-1px">探测视频文件（统计容量，稍慢）</label>
      <button id="btnScan" style="margin-top:10px">开始扫描</button>
      <div class="bar"><i id="pbar"></i></div>
      <div class="msg" id="pmsg">就绪</div>
      <div class="curfile" id="curfile"></div>
      <div class="slog" id="slog"></div>
      <div id="scanOut"></div>
    </div>

    <div class="grp">
      <h3>画像报告</h3>
      <div class="row">
        <div><label>标签 TopN</label><input id="topTags" type="number" value="40"></div>
        <div><label>艺人 TopN</label><input id="topActors" type="number" value="30"></div>
      </div>
      <label style="margin-top:8px">报告内嵌明细条数</label>
      <input id="rpMovies" type="number" value="1000">
      <button id="btnReport" class="ok" style="margin-top:10px">生成 / 刷新报告</button>
      <div class="msg" id="rmsg"></div>
    </div>

    <div class="grp">
      <h3>导出</h3>
      <label><input type="checkbox" class="fx" value="csv" checked style="width:auto;margin-right:6px">CSV（17 张表）</label>
      <label><input type="checkbox" class="fx" value="xlsx" checked style="width:auto;margin-right:6px">XLSX（多工作表）</label>
      <label><input type="checkbox" class="fx" value="json" checked style="width:auto;margin-right:6px">JSON（完整结构化）</label>
      <label><input type="checkbox" class="fx" value="md" checked style="width:auto;margin-right:6px">Markdown 画像卡片</label>
      <label><input type="checkbox" class="fx" value="html" checked style="width:auto;margin-right:6px">HTML 可视化报告</label>
      <button id="btnExport" class="ghost" style="margin-top:10px">导出到输出目录</button>
      <div class="msg" id="emsg"></div>
    </div>

    <div class="grp">
      <h3>数据库</h3>
      <div class="kv"><span>作品</span><b id="sMovies">-</b></div>
      <div class="kv"><span>艺人</span><b id="sActors">-</b></div>
      <div class="kv"><span>标签</span><b id="sTags">-</b></div>
      <div class="kv"><span>片商 / 系列</span><b id="sStudio">-</b></div>
      <div class="kv"><span>数据库</span><b id="sDb">-</b></div>
      <button id="btnRefresh" class="ghost" style="margin-top:9px">刷新状态</button>
      <div class="hintline">输出目录：<span id="outDir">-</span></div>
    </div>

  </div>

    <div class="legal" style="margin-top:14px;padding-top:10px;border-top:1px solid #2c3140;font-size:11px;color:#8a93a6;line-height:1.5">
      Copyright &copy; 2026 肆月Aperture<br>
      本软件不得用于商业用途，仅做学习交流使用。
    </div>
</aside>

<main>
  <div class="tabs">
    <button id="tabReport" class="on">画像报告</button>
    <button id="tabDetail">明细检索</button>
    <button id="tabAI">AI 分析</button>
  </div>
  <iframe id="frame" src="/report.html"></iframe>
  <div id="detail">
    <input id="q" placeholder="搜索番号 / 标题 / 片商 / 导演 / 演员 / 标签，回车查询">
    <div id="aiHint" class="hintline" style="margin:6px 0;display:none">🤖 AI 增强检索已启用：支持自然语言 / 同义词扩展</div>
    <div id="dsum" class="hintline" style="margin:8px 0"></div>
    <div id="dtbl"></div>
  </div>
  <div id="ai">
    <div id="aiDisableHint" class="hintline">请先在左侧「运行状态」开启 🤖 <b>AI 增强</b> 开关，AI 分析功能才会启用（开启后自动以本地启发式运行，若装了 Ollama / MiniLM 则升级为语义版）。</div>
    <div id="aiPanel" style="display:none">
      <h3>🏷️ 标签主题聚类</h3>
      <div class="hintline">基于标签共现，把常一起出现的标签聚成「题材 / 主题」分组。</div>
      <button id="btnClusters" style="margin-top:8px">生成主题聚类</button>
      <div id="clusters" class="hintline" style="margin-top:10px"></div>

      <h3 style="margin-top:20px">🔍 相似作品推荐</h3>
      <div class="row" style="margin-top:6px">
        <input id="simNum" placeholder="输入番号，如 ABC-001">
        <button class="ghost mini" id="btnSim" style="flex:0 0 auto">查相似</button>
      </div>
      <div id="simRes" class="hintline" style="margin-top:10px"></div>

      <h3 style="margin-top:20px">📝 AI 画像解读</h3>
      <div class="hintline">用题材 / 演员 / 片商统计，生成一段自然语言「偏好画像」。装了 Ollama 时由本地大模型撰写，否则用本地规则总结。</div>
      <button id="btnInterpret" style="margin-top:8px">生成画像解读</button>
      <div id="interpret" class="hintline" style="margin-top:10px;white-space:pre-wrap"></div>

      <h3 style="margin-top:20px">🔎 语义检索</h3>
      <div class="hintline">基于作品「标题 + 标签 + 演员」句向量的余弦相似检索：用一句话描述想找的片子，返回语义最接近的若干结果。需本地 Ollama 或 MiniLM 提供向量能力；离线启发式不开放此功能。</div>
      <div id="semBlock" style="display:none">
        <div class="ai-row" style="margin-top:6px">
          <span class="ai-msg" id="semIndexInfo" style="margin:0">向量索引：0 / 0 条</span>
          <button class="ghost mini" id="btnBuildIdx" style="flex:0 0 auto">构建向量索引</button>
        </div>
        <div class="row" style="margin-top:8px">
          <input id="semQuery" placeholder="用一句话描述，如：校园背景的青春恋爱故事">
          <button class="ghost mini" id="btnSemSearch" style="flex:0 0 auto">语义搜索</button>
        </div>
        <div id="semRes" class="hintline" style="margin-top:10px"></div>
      </div>
      <div id="semBlockHint" class="hintline" style="margin-top:6px;color:var(--tx3)">（当前后端为「本地启发式」，无向量能力；安装 Ollama 或 MiniLM 模型后，此处会自动出现「构建向量索引」入口。）</div>
    </div>
  </div>
</main>

<script>
const $ = id => document.getElementById(id);
const esc = s => String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmt = n => n==null?'':Number(n).toLocaleString('zh-CN');
const api = async (url, opt) => {
  const r = await fetch(url, opt); const j = await r.json();
  if(!r.ok || j.error) throw new Error(j.error || ('HTTP '+r.status));
  return j;
};

let curPath = '';
let scanTimer = null;

/* ---- 目录浏览器 ---- */
async function renderDrives(){
  const ds = await api('/api/drives');
  $('drives').innerHTML = ds.map(d=>`<span class="drive" data-p="${esc(d)}">${esc(d)}</span>`).join('');
  $('drives').querySelectorAll('.drive').forEach(el=>{
    el.onclick = ()=> navigate(el.dataset.p);
  });
}
function renderCrumb(path){
  if(!path){ $('crumb').innerHTML=''; return; }
  const parts = path.split(/[\\/]/).filter(Boolean);
  let acc='';
  const segs = parts.map((p,i)=>{
    acc += (i===0 && /^[A-Za-z]:$/.test(p) ? p+"\\\\" : (i===0?'/':'')+p+"\\\\");
    const pp = acc.replace(/\\\\$/,"\\\\");
    return `<a data-p="${esc(pp)}">${esc(p)}</a>`;
  });
  $('crumb').innerHTML = segs.join(' <span style="color:var(--tx3)">/</span> ');
  $('crumb').querySelectorAll('a').forEach(a=> a.onclick=()=>navigate(a.dataset.p));
}
async function navigate(path){
  curPath = path;
  renderCrumb(path);
  try{
    const r = await api('/api/list_dir?path=' + encodeURIComponent(path));
    if(r.error){ $('folders').innerHTML = '<div class="empty">'+esc(r.error)+'</div>'; return; }
    if(!r.items.length){ $('folders').innerHTML = '<div class="empty">（没有子目录）</div>'; return; }
    $('folders').innerHTML = r.items.map(it=>
      `<div class="folder" data-p="${esc(it.path)}"><span class="nm">📁 ${esc(it.name)}</span></div>`).join('');
    $('folders').querySelectorAll('.folder').forEach(el=>{
      el.onclick = ()=> navigate(el.dataset.p);
    });
  }catch(e){ $('folders').innerHTML = '<div class="empty">加载失败：'+esc(e.message)+'</div>'; }
}
$('btnUp').onclick = ()=>{
  if(!curPath) return;
  const r = (function(p){ // 取父目录
    const i = Math.max(p.lastIndexOf("\\\\"), p.lastIndexOf('/'));
    return i>0 ? p.slice(0,i) : (p.replace(/[\\/]/g,'') ? p.slice(0,2) : '');
  })(curPath);
  navigate(r || curPath);
};
$('btnAddCur').onclick = ()=>{ if(curPath) addDir(curPath); };
$('btnAddManual').onclick = ()=>{
  const v = $('manual').value.trim();
  if(v) addDir(v);
};
async function addDir(p){
  try{ await api('/api/dirs',{method:'POST',headers:{'Content-Type':'application/json'},
    body: JSON.stringify({action:'add', path:p})}); refreshDirs(); }
  catch(e){ alert('添加失败：'+e.message); }
}
async function refreshDirs(){
  try{
    const d = await api('/api/dirs');
    const list = d.dirs || [];
    $('seldirs').innerHTML = list.length ? list.map(p=>
      `<div class="seldir"><span class="p" title="${esc(p)}">${esc(p)}</span><span class="x" data-p="${esc(p)}">×</span></div>`).join('')
      : '<div class="hintline">还没有选择目录</div>';
    $('seldirs').querySelectorAll('.x').forEach(el=> el.onclick=()=> removeDir(el.dataset.p));
    $('selCount').textContent = list.length ? `已选 ${list.length} 个目录` : '';
  }catch(e){}
}
async function removeDir(p){
  await api('/api/dirs',{method:'POST',headers:{'Content-Type':'application/json'},
    body: JSON.stringify({action:'remove', path:p})}).catch(()=>{});
  refreshDirs();
}
$('btnClear').onclick = async ()=>{
  await api('/api/dirs',{method:'POST',headers:{'Content-Type':'application/json'},
    body: JSON.stringify({action:'clear'})}).catch(()=>{});
  refreshDirs();
};

/* ---- 扫描 ---- */
async function pollScan(){
  try{
    const s = await api('/api/scan/state');
    const pct = s.total ? Math.round(s.done*100/s.total) : (s.running?5:0);
    $('pbar').style.width = pct + '%';
    $('pmsg').textContent = s.message + (s.total ? `  (${s.done}/${s.total})` : '');
    $('curfile').textContent = s.current ? '正在解析：'+s.current : '';
    if(s.log && s.log.length) $('slog').textContent = s.log.slice(-40).join('\\n');
    $('btnScan').disabled = s.running;
    $('btnScan').textContent = s.running ? '扫描中…' : '开始扫描';
    // 扫描进行中：300ms 高频轮询，配合后端 ~300ms 节流上报，进度条与文件名实时刷新
    if(s.running){ scanTimer = setTimeout(pollScan, 300); return; }
    if(scanTimer){ clearTimeout(scanTimer); scanTimer=null; }
    if(s.result){
      const r = s.result;
      if(r.error){ $('scanOut').innerHTML = '<div class="err">'+esc(r.error)+'</div>'; }
      else {
        $('scanOut').innerHTML =
          `<div style="margin-top:9px">
             <div class="kv"><span>新增</span><b>${fmt(r.added)}</b></div>
             <div class="kv"><span>更新</span><b>${fmt(r.updated)}</b></div>
             <div class="kv"><span>跳过</span><b>${fmt(r.skipped)}</b></div>
             <div class="kv"><span>降级/失败</span><b>${fmt(r.recovered)} / ${fmt(r.failed)}</b></div>
             <div class="kv"><span>耗时</span><b>${r.duration_sec}s（${fmt(r.speed_per_sec)}/秒）</b></div>
           </div>` + (r.errors && r.errors.length
              ? '<div class="err">'+r.errors.slice(0,8).map(e=>esc((e[1]||'')+'  '+(e[0]||''))).join('<br>')+'</div>' : '');
      }
      refresh();
    }
  }catch(e){ $('pmsg').textContent = '轮询失败：'+e.message; }
}
$('btnScan').onclick = async () => {
  const d = await api('/api/dirs').catch(()=>({dirs:[]}));
  if(!d.dirs || !d.dirs.length){ alert('请先在左侧选择至少一个 NFO 目录'); return; }
  $('scanOut').innerHTML = '';
  try{
    await api('/api/scan', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({roots: d.dirs, workers: +$('workers').value || null,
        full: $('mode').value==='full', probe_video: $('probe').checked})});
    pollScan();
  }catch(e){ alert('启动失败：'+e.message); }
};

/* ---- 报告 / 导出 / 状态 / 检索（沿用既有逻辑）---- */
$('btnReport').onclick = async () => {
  $('btnReport').disabled = true; $('rmsg').textContent = '正在统计并渲染…';
  try{
    const r = await api('/api/report', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({top_tags:+$('topTags').value||40, top_actors:+$('topActors').value||30,
        report_movies:+$('rpMovies').value||1000})});
    $('rmsg').textContent = `已生成（${r.size_kb} KB，内嵌 ${fmt(r.movies)} 条，耗时 ${r.elapsed}s）`;
    $('frame').src = '/report.html?t=' + Date.now();
    showTab('report');
  }catch(e){ $('rmsg').textContent = '失败：'+e.message; }
  finally{ $('btnReport').disabled = false; }
};
$('btnExport').onclick = async () => {
  const fx = [...document.querySelectorAll('.fx:checked')].map(x=>x.value);
  if(!fx.length){ alert('请至少选择一种格式'); return; }
  $('btnExport').disabled = true; $('emsg').textContent = '正在导出…';
  try{
    const r = await api('/api/export', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({formats: fx})});
    $('emsg').textContent = `已导出 ${r.files.length} 个文件（${r.at}）`;
    alert('导出完成，共 '+r.files.length+' 个文件：\\n\\n' + r.files.join('\\n'));
  }catch(e){ $('emsg').textContent = '失败：'+e.message; }
  finally{ $('btnExport').disabled = false; }
};
async function refresh(){
  try{
    const s = await api('/api/status');
    $('sMovies').textContent = fmt(s.movies);
    $('sActors').textContent = fmt(s.distinct && s.distinct.actors);
    $('sTags').textContent = fmt(s.distinct && s.distinct.tags);
    $('sStudio').textContent = fmt(s.distinct && s.distinct.studios) + ' / ' + fmt(s.distinct && s.distinct.series);
    $('sDb').textContent = s.db_size_mb + ' MB';
    $('outDir').textContent = s.out_dir;
  }catch(e){}
}
$('btnRefresh').onclick = refresh;

function showTab(which){
  const map = {report: $('tabReport'), detail: $('tabDetail'), ai: $('tabAI')};
  for(const k in map) map[k].className = (k===which) ? 'on' : '';
  $('frame').style.display  = (which==='report') ? 'block' : 'none';
  $('detail').style.display = (which==='detail') ? 'block' : 'none';
  $('ai').style.display     = (which==='ai')     ? 'block' : 'none';
  if(which==='detail') search();
  if(which==='ai') refreshAI();
}
$('tabReport').onclick = ()=>showTab('report');
$('tabDetail').onclick = ()=>showTab('detail');
$('tabAI').onclick = ()=>showTab('ai');

/* ---- AI 分析标签页 ---- */
async function refreshAI(){
  try{
    const s = await api('/api/ai/state');
    const on = !!s.enabled && s.state==='ready';
    $('aiDisableHint').style.display = on ? 'none' : 'block';
    $('aiPanel').style.display = on ? 'block' : 'none';
    // 语义检索区块：仅支持向量的后端（Ollama / MiniLM）才出现
    const canVec = (s.backend === 'ollama' || s.backend === 'minilm');
    $('semBlock').style.display = canVec ? 'block' : 'none';
    $('semBlockHint').style.display = canVec ? 'none' : 'block';
    if(canVec){
      const idx = s.index || {};
      const cnt = idx.indexed_model != null ? idx.indexed_model : idx.indexed;
      let info = `向量索引：${fmt(cnt)} / ${fmt(idx.total_movies)} 条`;
      if(idx.running) info += '（构建中…）';
      else if(idx.stale) info += ' · 索引不完整，建议重新构建';
      else if(idx.message && cnt) info += ' · ' + idx.message;
      $('semIndexInfo').textContent = info;
      $('btnBuildIdx').disabled = !!idx.running;
    }
  }catch(e){}
}
$('btnBuildIdx').onclick = async () => {
  $('semIndexInfo').textContent = '正在启动构建…';
  try{
    const r = await api('/api/ai/index', {method:'POST'});
    if(r.disabled){ $('semIndexInfo').textContent = 'AI 增强未开启'; return; }
    if(r.unavailable){ $('semIndexInfo').textContent = r.message || '不可用'; return; }
    pollSemIndex();
  }catch(e){ $('semIndexInfo').textContent = '启动失败：' + e.message; }
};
async function pollSemIndex(){
  try{
    const s = await api('/api/ai/index/state');
    $('semIndexInfo').textContent =
      `向量索引：${fmt(s.indexed)} / ${fmt(s.total_movies)} 条` +
      (s.running ? '（构建中…）' : (' · ' + (s.message || '')));
    $('btnBuildIdx').disabled = !!s.running;
    if(s.running){ setTimeout(pollSemIndex, 800); }
  }catch(e){}
}
$('btnSemSearch').onclick = async () => {
  const q = $('semQuery').value.trim();
  if(!q){ alert('请输入查询语句'); return; }
  $('semRes').textContent = '语义检索中…';
  try{
    const r = await api('/api/ai/search?q=' + encodeURIComponent(q));
    if(r.disabled){ $('semRes').textContent = 'AI 增强未开启'; return; }
    if(r.unavailable){ $('semRes').textContent = r.message || '语义检索不可用'; return; }
    if(!r.results || !r.results.length){
      $('semRes').textContent = '没有语义匹配结果（可尝试先构建索引或更换描述）'; return; }
    $('semRes').innerHTML = '<table><thead><tr>' +
      ['番号','标题','片商','评分','相似度'].map(h=>`<th>${h}</th>`).join('') +
      '</tr></thead><tbody>' + r.results.map(x =>
      `<tr><td>${esc(x.num)}</td><td>${esc(x.title)}</td><td>${esc(x.studio)}</td>` +
      `<td class="n">${esc(x.userrating)}</td><td class="n">${x.score}</td></tr>`).join('') +
      '</tbody></table>';
  }catch(e){ $('semRes').textContent = '失败：' + e.message; }
};
$('btnClusters').onclick = async () => {
  $('clusters').textContent = '计算中…';
  try{
    const r = await api('/api/ai/clusters');
    if(r.disabled){ $('clusters').textContent = 'AI 增强未开启'; return; }
    const cs = r.clusters || [];
    if(!cs.length){ $('clusters').textContent = '库中没有足够的标签数据用于聚类'; return; }
    $('clusters').innerHTML = cs.slice(0,15).map(c =>
      `<div style="margin:8px 0;padding:9px 11px;background:var(--card2);border:1px solid var(--line);border-radius:8px">
        <div style="font-weight:600;color:var(--ac)">🎯 ${esc(c.theme)}
          <span style="color:var(--tx3);font-weight:400">· ${fmt(c.size)} 部${c.singleton?' · 独立高频':''}</span></div>
        <div style="margin-top:5px">${c.members.slice(0,14).map(m=>`<span class="tag">${esc(m)}</span>`).join('')}</div>
      </div>`).join('');
  }catch(e){ $('clusters').textContent = '失败：'+e.message; }
};
$('btnSim').onclick = async () => {
  const num = $('simNum').value.trim();
  if(!num){ alert('请输入番号'); return; }
  $('simRes').textContent = '查询中…';
  try{
    const r = await api('/api/ai/similar?num=' + encodeURIComponent(num));
    if(r.disabled){ $('simRes').textContent = 'AI 增强未开启'; return; }
    if(!r.similar || !r.similar.length){
      $('simRes').textContent = '未找到相似作品（该番号可能不存在或标签过少）'; return; }
    $('simRes').innerHTML = '<table><thead><tr>' +
      ['番号','标题','片商','评分','相似度','共享'].map(h=>`<th>${h}</th>`).join('') +
      '</tr></thead><tbody>' + r.similar.map(x =>
      `<tr><td>${esc(x.num)}</td><td>${esc(x.title)}</td><td>${esc(x.studio)}</td>` +
      `<td class="n">${esc(x.userrating)}</td><td class="n">${x.score}</td>` +
      `<td class="n">${x.shared_tags}标签/${x.shared_actors}演员</td></tr>`).join('') +
      '</tbody></table>';
  }catch(e){ $('simRes').textContent = '失败：'+e.message; }
};
$('btnInterpret').onclick = async () => {
  $('interpret').textContent = '生成中…';
  try{
    const r = await api('/api/ai/interpret');
    if(r.disabled){ $('interpret').textContent = 'AI 增强未开启'; return; }
    const tag = r.backend === 'ollama' ? '🤖 Ollama 语义解读' : '🧩 本地启发式解读';
    $('interpret').textContent = tag + '：\\n\\n' + r.text;
  }catch(e){ $('interpret').textContent = '失败：'+e.message; }
};

let dtSort = 'userrating', dtDir = -1;
async function search(){
  const q = $('q').value.trim();
  try{
    const r = await api('/api/movies?q=' + encodeURIComponent(q) + '&limit=100');
    const ai = r.ai ? ` · 🤖 AI 增强（${esc(r.backend)}）` : '';
    $('dsum').textContent = `匹配 ${fmt(r.total)} 条，显示前 ${r.rows.length} 条${ai}`;
    const cols = [['番号','num'],['标题','title'],['年份','year',1],['片商','studio'],
                  ['导演','director'],['演员','actors'],['标签','tags'],['评分','userrating',1],
                  ['时长','runtime_min',1],['画质','resolution']];
    $('dtbl').innerHTML = r.rows.length ? `<table><thead><tr>${
      cols.map(c=>`<th>${c[0]}</th>`).join('')}</tr></thead><tbody>${
      r.rows.map(x=>'<tr>'+cols.map(c=>`<td class="${c[2]?'n':''}">${esc(x[c[1]])}</td>`).join('')+'</tr>').join('')
    }</tbody></table>` : '<div class="empty">没有匹配的记录</div>';
  }catch(e){ $('dsum').textContent = '查询失败：'+e.message; }
}
$('q').onkeydown = e => { if(e.key==='Enter') search(); };

/* ---- AI 增强开关 ---- */
async function pollAI(){
  try{
    const s = await api('/api/ai/state');
    const on = !!s.enabled;
    if($('aiToggle').checked !== on) $('aiToggle').checked = on;
    const cls = (s.state==='ready' && s.backend && s.backend!=='heuristic') ? 'ok'
              : (s.state==='initializing' ? 'warn' : '');
    $('aiMsg').textContent = s.message || (on ? '已开启' : '关闭');
    $('aiMsg').className = 'ai-msg' + (cls ? ' '+cls : '');
    $('aiHint').style.display = on ? 'block' : 'none';
    if($('ai').style.display !== 'none') refreshAI();
  }catch(e){}
}
$('aiToggle').onchange = async () => {
  const on = $('aiToggle').checked;
  $('aiMsg').textContent = on ? '正在检测本地 AI 环境…' : '正在关闭…';
  try{
    await api(on ? '/api/ai/enable' : '/api/ai/disable', {method:'POST'});
  }catch(e){ alert('AI 增强切换失败：'+e.message); $('aiToggle').checked = !on; }
  pollAI();
};

/* ---- 初始化 ---- */
renderDrives(); navigate(''); refreshDirs(); refresh(); pollScan();
pollAI(); setInterval(pollAI, 1500);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# 报告未生成时的占位页（自包含；轮询后端状态，扫描中不白屏、可一键生成）
# ---------------------------------------------------------------------------

_EMPTY_REPORT_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>画像报告 · 尚未生成</title>
<style>
:root{--bg:#0f1115;--card:#1a1f2b;--card2:#202634;--line:#2a3242;--tx:#e6ebf5;
--tx2:#9aa7bd;--tx3:#6b7893;--ac:#4f8cff;--ac2:#17c9a5;--ac3:#ff8f4f;--warn:#ff5f6d;
--grad1:linear-gradient(135deg,#4f8cff,#17c9a5)}
*{box-sizing:border-box}
html,body{margin:0;height:100%}
body{background:var(--bg);color:var(--tx);font:14px/1.6 -apple-system,BlinkMacSystemFont,
"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;display:flex;flex-direction:column}
header.hero{background:var(--grad1);padding:26px 0 24px;box-shadow:0 6px 30px rgba(0,0,0,.28)}
header.hero .wrap{max-width:760px;margin:0 auto;padding:0 22px}
header.hero h1{margin:0;font-size:22px;color:#fff;letter-spacing:.5px}
header.hero .sub{color:rgba(255,255,255,.86);font-size:12.5px;margin-top:4px}
main{flex:1;display:flex;align-items:center;justify-content:center;padding:26px 18px}
.card{width:100%;max-width:620px;background:var(--card);border:1px solid var(--line);
border-radius:16px;padding:30px 30px 26px;box-shadow:0 16px 50px rgba(0,0,0,.35)}
.top{display:flex;align-items:center;gap:16px;margin-bottom:18px}
.top svg{flex:0 0 auto}
.top .t1{font-size:17px;font-weight:700}
.top .t2{color:var(--tx2);font-size:12.5px;margin-top:2px}
.status{font-size:13px;color:var(--tx2);min-height:20px;margin:6px 0 14px}
.status b{color:var(--tx)}
.bar{height:9px;background:var(--card2);border-radius:5px;overflow:hidden;margin:8px 0 6px;display:none}
.bar i{display:block;height:100%;background:var(--grad1);width:0;transition:width .35s}
.cur{font:11.5px/1.5 ui-monospace,Menlo,Consolas,monospace;color:var(--tx3);
overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-height:16px;display:none}
.steps{list-style:none;margin:10px 0 0;padding:0;color:var(--tx2);font-size:13px}
.steps li{padding:7px 0;border-bottom:1px dashed var(--line);display:flex;gap:10px;align-items:baseline}
.steps li:last-child{border-bottom:none}
.steps .n{flex:0 0 22px;height:22px;border-radius:50%;background:var(--card2);
border:1px solid var(--line);color:var(--ac);font-weight:700;text-align:center;line-height:22px;font-size:12px}
button{margin-top:16px;background:var(--ac);border:none;color:#fff;border-radius:9px;
padding:11px 20px;font-size:14px;cursor:pointer;transition:.15s}
button:hover{opacity:.9}
button:disabled{opacity:.5;cursor:not-allowed}
.hide{display:none}
.foot{color:var(--tx3);font-size:11.5px;text-align:center;padding:14px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--ac2);
margin-right:7px;animation:pulse 1.2s infinite}
@keyframes pulse{0%,100%{opacity:.35}50%{opacity:1}}
</style>
</head>
<body>
<header class="hero"><div class="wrap">
  <h1>NFO 画像矿工 · 用户×癖分析报告</h1>
  <div class="sub">本地离线 · 单文件 HTML · 双击即用</div>
</div></header>
<main>
  <div class="card">
    <div class="top">
      <svg width="46" height="46" viewBox="0 0 46 46" fill="none">
        <rect x="3" y="6" width="40" height="34" rx="6" stroke="#4f8cff" stroke-width="2"/>
        <line x1="3" y1="15" x2="43" y2="15" stroke="#4f8cff" stroke-width="2"/>
        <line x1="3" y1="31" x2="43" y2="31" stroke="#4f8cff" stroke-width="2"/>
        <circle cx="14" cy="10.5" r="1.6" fill="#17c9a5"/><circle cx="22" cy="10.5" r="1.6" fill="#17c9a5"/>
        <circle cx="14" cy="23" r="1.6" fill="#17c9a5"/><circle cx="22" cy="23" r="1.6" fill="#17c9a5"/>
        <circle cx="30" cy="29" r="7" stroke="#ff8f4f" stroke-width="2.4"/>
        <line x1="35" y1="34" x2="40" y2="39" stroke="#ff8f4f" stroke-width="2.4" stroke-linecap="round"/>
      </svg>
      <div><div class="t1">报告尚未生成</div>
      <div class="t2">完成扫描后，点下方按钮即可生成可视化画像</div></div>
    </div>

    <div class="status" id="status">正在读取本地状态…</div>

    <div class="bar" id="bar"><i id="barfill"></i></div>
    <div class="cur" id="cur"></div>

    <ul class="steps hide" id="steps">
      <li><span class="n">1</span><span>在左侧「选择 NFO 目录」里挑一个或多部作品的目录</span></li>
      <li><span class="n">2</span><span>点击「开始扫描」，解析全部 .nfo 并入库</span></li>
      <li><span class="n">3</span><span>扫描完成后回到这里，点「生成画像报告」</span></li>
    </ul>

    <button id="gen" class="hide">✨ 生成画像报告</button>
  </div>
</main>
<div class="foot">本页会随后端状态自动刷新 · 扫描进行中也会实时显示进度</div>

<script>
const $ = s => document.querySelector(s);
let timer = null;
async function getJSON(u){ try{ return await (await fetch(u)).json(); }catch(e){ return {}; } }

function showScan(s){
  $('steps').classList.add('hide'); $('gen').classList.add('hide');
  $('bar').style.display = 'block'; $('cur').style.display = 'block';
  const pct = s.total ? Math.round(s.done*100/s.total) : (s.running?6:0);
  $('barfill').style.width = pct + '%';
  $('status').innerHTML = '<span class="dot"></span>正在扫描 NFO 目录… <b>' + (s.done||0) + ' / ' + (s.total||0) + '</b>';
  $('cur').textContent = s.current ? ('当前：' + s.current) : '';
}
function showReady(movies, s){
  $('bar').style.display = 'none'; $('cur').style.display = 'none';
  $('steps').classList.add('hide');
  $('status').innerHTML = '已收录 <b>' + movies.toLocaleString('zh-CN') + '</b> 部作品' +
    (s && s.db_size_mb ? '（数据库 ' + s.db_size_mb + ' MB）' : '') + '，可以生成报告了。';
  $('gen').classList.remove('hide');
}
function showEmpty(){
  $('bar').style.display = 'none'; $('cur').style.display = 'none';
  $('status').textContent = '数据库还没有作品，请先扫描 NFO 目录。';
  $('steps').classList.remove('hide');
  $('gen').classList.add('hide');
}

async function tick(){
  const s = await getJSON('/api/status');
  const scan = await getJSON('/api/scan/state');
  const movies = (s.movies || 0);
  if(scan.running){ showScan(scan); }
  else if(movies > 0){ showReady(movies, s); }
  else { showEmpty(); }
  timer = setTimeout(tick, 900);
}

$('gen').onclick = async () => {
  const b = $('gen'); b.disabled = true; b.textContent = '正在生成…';
  try{
    const r = await fetch('/api/report', {method:'POST',
      headers:{'Content-Type':'application/json'}, body:'{}'});
    if(!r.ok) throw new Error('HTTP ' + r.status);
    location.href = '/report.html?t=' + Date.now();
  }catch(e){ alert('生成失败：' + e.message); b.disabled = false; b.textContent = '✨ 生成画像报告'; }
};

tick();
</script>
</body>
</html>
"""



# ---------------------------------------------------------------------------
# HTTP 处理
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    state: AppState
    report_path: str = ""

    def log_message(self, fmt: str, *args: Any) -> None:
        try:
            sys.stderr.write("  [web] " + (fmt % args) + "\n")
        except Exception:
            pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    def _json(self, obj: Any, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _html(self, text: str) -> None:
        self._send(200, text.encode("utf-8"), "text/html; charset=utf-8")

    def _serve_logo(self) -> None:
        """Serve the logo image (favicon)."""
        # Search order: PyInstaller temp dir → script parent → exe dir
        # v1.3.5：补 PyInstaller 6.x 的 dest-as-dir 嵌套形态（logo.png/logo.png、
        # assets/logo.png），否则打包后 favicon 404
        candidates = []
        if getattr(sys, "frozen", False):
            meipass = getattr(sys, "_MEIPASS", None)
            if meipass:
                for rel in ("logo.png", os.path.join("assets", "logo.png"),
                            os.path.join("logo.png", "logo.png")):
                    candidates.append(os.path.join(meipass, rel))
        base = os.path.dirname(os.path.abspath(__file__))
        candidates.extend([
            os.path.normpath(os.path.join(base, "..", "logo.png")),
            os.path.join(base, "logo.png"),
            os.path.join(os.path.dirname(sys.executable), "logo.png"),
        ])
        for candidate in candidates:
            if os.path.isfile(candidate):
                with open(candidate, "rb") as f:
                    data = f.read()
                self._send(200, data, "image/png")
                return
        self._send(404, b"", "image/png")

    def _body(self) -> Dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:
            return {}

    def do_GET(self) -> None:
        u = urlparse(self.path)
        p, qs = u.path, parse_qs(u.query)
        try:
            if p in ("/", "/index.html"):
                page = (_PAGE
                        .replace("__APP__", APP_NAME)
                        .replace("__VER__", __version__))
                self._html(page)
            elif p == "/report.html":
                path = self.state.last_report.get("path") or self.report_path
                if not path or not os.path.exists(path):
                    self._html(_EMPTY_REPORT_HTML)
                else:
                    with open(path, "r", encoding="utf-8") as fh:
                        self._html(fh.read())
            elif p == "/api/status":
                st = self.state.store.stats()
                ov = Analyzer(self.state.store).overview() if st["movies"] else {}
                self._json({**st, "distinct": ov.get("distinct", {}), "out_dir": self.state.out_dir})
            elif p == "/api/scan/state":
                self._json(self.state.scan)
            elif p == "/api/dirs":
                self._json({"dirs": list(self.state.selected_dirs)})
            elif p == "/api/drives":
                self._json(list_drives())
            elif p == "/api/list_dir":
                self._json(list_dir(qs.get("path", [""])[0]))
            elif p == "/api/movies":
                self._json(self.state.movies(
                    q=qs.get("q", [""])[0],
                    limit=int(qs.get("limit", ["50"])[0]),
                    offset=int(qs.get("offset", ["0"])[0]),
                ))
            elif p == "/api/logo":
                self._serve_logo()
            elif p == "/api/ai/state":
                st = self.state.ai.status()
                st["index"] = self.state.ai_index_status()
                self._json(st)
            elif p == "/api/ai/clusters":
                if not self.state.ai.enabled:
                    self._json({"disabled": True, "clusters": []})
                else:
                    self._json({"disabled": False, "clusters": self.state.ai_clusters()})
            elif p == "/api/ai/similar":
                num = qs.get("num", [""])[0]
                if not self.state.ai.enabled:
                    self._json({"disabled": True, "similar": []})
                elif not num:
                    self._json({"error": "缺少 num"}, 400)
                else:
                    self._json({"disabled": False, "similar": self.state.ai_similar(num)})
            elif p == "/api/ai/interpret":
                if not self.state.ai.enabled:
                    self._json({"disabled": True, "text": "", "backend": "none"})
                else:
                    self._json({"disabled": False, **self.state.ai_interpret()})
            elif p == "/api/ai/index/state":
                self._json(self.state.ai_index_status())
            elif p == "/api/ai/search":
                self._json(self.state.ai_search(
                    q=qs.get("q", [""])[0],
                    limit=int(qs.get("limit", ["20"])[0]),
                ))
            else:
                self._json({"error": "not found"}, 404)
        except Exception as exc:
            self._json({"error": f"{exc.__class__.__name__}: {exc}"}, 500)

    def do_POST(self) -> None:
        p = urlparse(self.path).path
        body = self._body()
        try:
            if p == "/api/scan":
                roots = body.get("roots") or []
                if not isinstance(roots, list) or not roots:
                    self._json({"error": "缺少 roots"}, 400)
                    return
                self.state.start_scan(
                    roots,
                    body.get("workers") or None,
                    bool(body.get("full")),
                    bool(body.get("probe_video", True)),
                )
                self._json({"ok": True})
            elif p == "/api/dirs":
                action = body.get("action", "list")
                path = body.get("path", "")
                if action == "add":
                    self.state.add_dir(path)
                elif action == "remove":
                    self.state.remove_dir(path)
                elif action == "clear":
                    self.state.clear_dirs()
                self._json({"dirs": list(self.state.selected_dirs)})
            elif p == "/api/report":
                r = self.state.build_report(
                    top_tags=int(body.get("top_tags", 40)),
                    top_actors=int(body.get("top_actors", 30)),
                    report_movies=int(body.get("report_movies", 1000)),
                )
                self._json(r)
            elif p == "/api/export":
                r = self.state.export(
                    formats=body.get("formats") or ["csv", "json", "xlsx", "md", "html"],
                    report_movies=int(body.get("report_movies", 1000)),
                )
                self._json(r)
            elif p == "/api/ai/enable":
                self._json(self.state.ai.set_enabled(True))
            elif p == "/api/ai/disable":
                self._json(self.state.ai.set_enabled(False))
            elif p == "/api/ai/index":
                self._json(self.state.ai_build_index())
            else:
                self._json({"error": "not found"}, 404)
        except Exception as exc:
            self._json({"error": f"{exc.__class__.__name__}: {exc}"}, 500)


def _is_our_instance(host: str, port: int, timeout: float = 1.0) -> bool:
    """判断 port 是否已被「本程序」的实例占用（而非其它随机服务）。

    先探测端口是否可连，再请求 /api/status：我们的响应一定带 ``schema_version``
    字段，借此与恰好占用同端口的其它程序区分开。
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except OSError:
        return False
    try:
        req = urllib.request.Request(f"http://{host}:{port}/api/status")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
        return isinstance(data, dict) and "schema_version" in data
    except Exception:
        return False


def serve(
    db_path: str = "output/nfo.db",
    out_dir: str = "output",
    host: str = "127.0.0.1",
    port: int = 9527,
    config: Optional[str] = None,
    open_browser: bool = True,
) -> None:
    """启动本地 Web 服务（常驻，阻塞）。双击 exe 即进入此模式。"""
    # windowed 打包下无控制台，把输出重定向到日志文件以便排查（尽早，连单实例判断都要记）
    abs_out = os.path.abspath(out_dir)
    os.makedirs(abs_out, exist_ok=True)
    if sys.stdout is None:
        try:
            log_path = os.path.join(abs_out, "nfo_profiler.log")
            fh = open(log_path, "a", encoding="utf-8")
            sys.stdout = fh
            sys.stderr = fh
        except Exception:
            pass

    # ---- 单实例守护 -------------------------------------------------
    # 旧逻辑端口被占用时会顺延到 9528/9529 再起一个服务，却仍打开同一个
    # output/nfo.db，两个进程抢库锁，第二个在初始化时即 "database is locked"。
    # 因此若 9527 已被「自己的」实例占用，直接把浏览器指向现有实例并退出即可。
    if _is_our_instance(host, port):
        url = f"http://{host}:{port}"
        print(f"  检测到实例已在运行：{url}，直接打开浏览器（不重复启动）。")
        if open_browser:
            webbrowser.open(url)
        return

    state = AppState(db_path, out_dir, config)

    # 核心模式：不调用任何运行环境检测 / 不加载 torch，避免 torch 半安装时
    # import 挂起占用 GIL 导致整个 HTTP 服务卡死。解析→画像→报告→导出全程基于标准库。
    handler = type("BoundHandler", (Handler,), {"state": state})

    # 端口占用时向后顺延；但若主端口是被「自己的」实例占用，则不再另起炉灶抢同一库
    httpd = None
    used_port = port
    for cand in range(port, port + 11):
        try:
            httpd = ThreadingHTTPServer((host, cand), handler)
            used_port = cand
            break
        except OSError:
            if cand == port and _is_our_instance(host, port):
                url = f"http://{host}:{port}"
                print(f"  主端口 {port} 已被本程序占用，打开现有实例：{url}")
                if open_browser:
                    webbrowser.open(url)
                state.store.close()
                return
            continue
    if httpd is None:
        print(f"  无法在 {host}:{port} 起服务（端口可能被占用且无可用顺延端口）。")
        state.store.close()
        return

    url = f"http://{host}:{used_port}"
    print(f"  {APP_NAME} v{__version__} 已启动：{url}")
    print(f"  数据库：{os.path.abspath(db_path)}")
    print(f"  输出目录：{state.out_dir}")
    if used_port != port:
        print(f"  注意：端口 {port} 被占用，已改用 {used_port}")

    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  正在停止服务…")
    finally:
        httpd.server_close()
        state.store.close()


__all__ = ["serve", "AppState", "list_drives", "list_dir"]
