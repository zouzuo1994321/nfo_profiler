# -*- coding: utf-8 -*-
"""扫描引擎：多进程解析 + 主进程单写 + 增量更新。

性能设计：
  * 解析是 CPU 密集（XML + 正则），用 ProcessPoolExecutor 摊到多核；
  * 写库只在主进程做，天然避免 SQLite 锁竞争；
  * 增量扫描按 (mtime, size) 指纹跳过未变动文件，二次运行几乎瞬时；
  * 批处理大小自适应，避免攒太多 dict 占内存。
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple, Union

from .normalize import Normalizer
from .parser import parse_nfo_file
from .store import Store

DEFAULT_EXTS = (".nfo",)

#: 进度回调签名: (phase: str, done: int, total: int, message: str)
ProgressCB = Callable[[str, int, int, str], None]


def iter_nfo_files(root: str, exts: Sequence[str] = DEFAULT_EXTS) -> Iterator[str]:
    """递归遍历目录下的 NFO 文件。跳过常见垃圾目录。"""
    exts = tuple(e.lower() for e in exts)
    skip_dirs = {".git", ".svn", "node_modules", "__pycache__", ".workbuddy",
                 "$recycle.bin", "system volume information", ".idea", ".vscode"}
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        dirnames[:] = [d for d in dirnames if d.lower() not in skip_dirs]
        for fn in filenames:
            if fn.lower().endswith(exts):
                yield os.path.join(dirpath, fn)


def _worker_parse(path: str, probe_video: bool = True) -> Dict[str, Any]:
    """子进程入口：必须是模块级函数才能被 pickle。"""
    try:
        return parse_nfo_file(path, probe_video=probe_video)
    except Exception as exc:  # pragma: no cover - 极端兜底
        return {"path": path, "parse_status": f"worker_error:{exc.__class__.__name__}"}


class ScanResult:
    def __init__(self) -> None:
        self.root = ""
        self.roots: List[str] = []
        self.scanned = 0
        self.added = 0
        self.updated = 0
        self.skipped = 0
        self.failed = 0
        self.recovered = 0
        self.duplicate_paths = 0
        self.duration_sec = 0.0
        self.workers = 1
        self.errors: List[Tuple[str, str]] = []

    def as_dict(self) -> Dict[str, Any]:
        return {
            "root": self.root,
            "roots": self.roots,
            "scanned": self.scanned,
            "added": self.added,
            "updated": self.updated,
            "skipped": self.skipped,
            "failed": self.failed,
            "recovered": self.recovered,
            "duplicate_paths": self.duplicate_paths,
            "duration_sec": round(self.duration_sec, 2),
            "workers": self.workers,
            "speed_per_sec": round(self.scanned / self.duration_sec, 1) if self.duration_sec else 0,
            "errors": self.errors[:50],
        }

    def __repr__(self) -> str:  # pragma: no cover
        d = self.as_dict()
        return (f"<ScanResult scanned={d['scanned']} added={d['added']} updated={d['updated']} "
                f"skipped={d['skipped']} failed={d['failed']} {d['duration_sec']}s>")


def scan_directory(
    root: str,
    store: Store,
    *,
    workers: Optional[int] = None,
    incremental: bool = True,
    probe_video: bool = True,
    batch_size: int = 500,
    progress: Optional[ProgressCB] = None,
    on_file: Optional[Callable[[str], None]] = None,
    max_files: int = 0,
) -> ScanResult:
    """扫描目录并写入数据库（单路径，内部转调 scan_paths）。"""
    return scan_paths(
        [root], store, workers=workers, incremental=incremental,
        probe_video=probe_video, batch_size=batch_size,
        progress=progress, on_file=on_file, max_files=max_files,
    )


def scan_paths(
    roots: Union[str, Sequence[str]],
    store: Store,
    *,
    workers: Optional[int] = None,
    incremental: bool = True,
    probe_video: bool = True,
    batch_size: int = 500,
    progress: Optional[ProgressCB] = None,
    on_file: Optional[Callable[[str], None]] = None,
    max_files: int = 0,
) -> ScanResult:
    """扫描**一个或多个**根目录并写入数据库。

    多路径规则：
      * 同一个库里可以登记任意多个数据源（不同盘符、不同目录均可）；
      * 路径之间允许嵌套，文件按「最长匹配根」归属，不会重复入库；
      * 每个文件都会记录来源根，后续可按数据源筛选统计；
      * 新增 / 移除数据源后再扫描，只影响该数据源的记录。

    Parameters
    ----------
    roots:
        一个或多个根目录（也可传单个字符串）。
    store:
        已打开的 Store。
    workers:
        并发进程数，默认 min(cpu_count, 8)。
    incremental:
        为 True 时跳过 (mtime, size) 未变化的文件。
    probe_video:
        是否探测同目录下 original_filename 指向的视频文件（用于容量统计）。
    batch_size:
        每批写入多少条。
    progress:
        进度回调。
    """
    # --- 归一化根目录列表 ---
    if isinstance(roots, str):
        roots = [roots]
    norm_roots: List[str] = []
    missing: List[str] = []
    for r in roots:
        if not r:
            continue
        p = os.path.abspath(os.path.expanduser(str(r).strip().strip('"')))
        if not os.path.isdir(p):
            missing.append(str(r))
            continue
        if p not in norm_roots:
            norm_roots.append(p)
    # 长路径优先，保证嵌套目录时按"最长匹配根"归属
    norm_roots.sort(key=len, reverse=True)

    res = ScanResult()
    res.root = " | ".join(norm_roots)
    res.roots = list(norm_roots)
    started = datetime.now()
    t0 = time.time()

    def _emit(phase: str, done: int, total: int, msg: str) -> None:
        if progress:
            try:
                progress(phase, done, total, msg)
            except Exception:
                pass

    if not norm_roots:
        res.duration_sec = time.time() - t0
        for m in missing:
            res.errors.append((m, "目录不存在"))
            res.failed += 1
        _emit("done", 0, 0, "没有可扫描的目录")
        return res

    store.register_sources(norm_roots)

    def _source_of(path: str) -> str:
        """按最长匹配把文件归属到某个数据源。"""
        for r in norm_roots:
            if path == r or path.startswith(r.rstrip("\\/") + os.sep):
                return r
        return norm_roots[-1]

    _emit("collect", 0, 0, f"正在遍历 {len(norm_roots)} 个目录…")
    #: [(文件路径, 归属数据源)]
    all_files: List[Tuple[str, str]] = []
    for ri, r in enumerate(norm_roots):
        n_before = len(all_files)
        for i, p in enumerate(iter_nfo_files(r)):
            all_files.append((p, r))
            if max_files and len(all_files) >= max_files:
                break
            if (i + 1) % 2000 == 0:
                _emit("collect", len(all_files), 0,
                      f"[{ri+1}/{len(norm_roots)}] {os.path.basename(r)}：累计 {len(all_files)} 个 NFO")
        _emit("collect", len(all_files), 0,
              f"[{ri+1}/{len(norm_roots)}] {os.path.basename(r)}：+{len(all_files)-n_before} 个")
        if max_files and len(all_files) >= max_files:
            break

    # 去重：同一路径只保留第一个（norm_roots 已按长度降序，天然取最长匹配根）
    seen: set = set()
    unique_files: List[Tuple[str, str]] = []
    for p, r in all_files:
        k = os.path.normcase(p)
        if k in seen:
            continue
        seen.add(k)
        unique_files.append((p, r))
    total_found = len(unique_files)
    res.duplicate_paths = len(all_files) - total_found
    _emit("collect", total_found, total_found,
          f"共发现 {total_found} 个 NFO（{len(norm_roots)} 个数据源，去重 {res.duplicate_paths} 个）")
    for m in missing:
        res.errors.append((m, "目录不存在，已跳过"))

    # --- 增量过滤 ---
    todo: List[Tuple[str, str]] = unique_files
    if incremental:
        known = store.known_files()
        todo = []
        for p, r in unique_files:
            try:
                st = os.stat(p)
            except OSError:
                res.failed += 1
                res.errors.append((p, "stat failed"))
                continue
            fp = (datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"), st.st_size)
            if known.get(p) == fp:
                res.skipped += 1
            else:
                todo.append((p, r))
    res.scanned = len(todo)

    if not todo:
        res.duration_sec = time.time() - t0
        store.touch_sources(norm_roots)
        _emit("done", total_found, total_found, "全部文件无变化，跳过解析")
        store.record_scan_run(
            started_at=started.strftime("%Y-%m-%d %H:%M:%S"),
            finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            root=res.root, duration_sec=res.duration_sec, scanned=0, added=0,
            updated=0, skipped=res.skipped, failed=res.failed, workers=0,
        )
        return res

    cpu = os.cpu_count() or 4
    n_workers = max(1, min(workers or min(cpu, 8), cpu))
    res.workers = n_workers
    if len(todo) < 50:  # 量太小，多进程开销大于收益
        n_workers = 1
        res.workers = 1

    _emit("parse", 0, len(todo), f"开始解析 {len(todo)} 个文件（{n_workers} 进程）…")

    batch: List[Dict[str, Any]] = []
    done = 0
    chunk = max(batch_size, n_workers * 40)
    #: 进度刷新节流：既按文件数（密集扫描时），也按墙钟时间（文件大、单文件慢时）
    #: 保证前端轮询能拿到平滑推进的 done / current，避免进度条"卡住"到末尾才跳。
    emit_every = max(50, chunk // 10)          # 每 ~1/10 批至少刷一次
    last_emit_ts = time.time()
    emit_interval = 0.3                         # 秒：最快 300ms 上报一次
    last_emit_done = 0                          # 上次已上报的 done 计数

    def _on_file_safe(path: str) -> None:
        if on_file:
            try:
                on_file(path)
            except Exception:
                pass

    def _maybe_emit(cur_path: str = "") -> None:
        nonlocal last_emit_ts, last_emit_done
        now = time.time()
        if cur_path:
            _on_file_safe(cur_path)
        if (done - last_emit_done >= emit_every) or (now - last_emit_ts >= emit_interval):
            _emit("parse", done, len(todo), f"{done}/{len(todo)}")
            last_emit_ts = now
            last_emit_done = done

    def _flush(items: List[Dict[str, Any]]) -> None:
        if not items:
            return
        a, u = store.upsert_records(items)
        res.added += a
        res.updated += u
        for it in items:
            st = str(it.get("parse_status", ""))
            if st == "recovered":
                res.recovered += 1
                if len(res.errors) < 50:
                    res.errors.append((it.get("path", ""), "recovered(降级正则抽取)"))
            elif st.startswith(("read_error", "parse_error", "worker_error", "future_error")):
                res.failed += 1
                if len(res.errors) < 50:
                    res.errors.append((it.get("path", ""), st))

    if n_workers == 1:
        for p, src in todo:
            rec = _worker_parse(p, probe_video)
            rec["source"] = src
            batch.append(rec)
            done += 1
            _maybe_emit(p)
            if len(batch) >= chunk:
                _flush(batch)
                batch = []
    else:
        with ProcessPoolExecutor(max_workers=n_workers) as pool:
            futures = {pool.submit(_worker_parse, p, probe_video): (p, src) for p, src in todo}
            for fut in as_completed(futures):
                path, src = futures[fut]
                try:
                    rec = fut.result()
                except Exception as exc:
                    rec = {"path": path, "parse_status": f"future_error:{exc.__class__.__name__}"}
                rec["source"] = src
                batch.append(rec)
                done += 1
                _maybe_emit(path)
                if len(batch) >= chunk:
                    _flush(batch)
                    batch = []
    _flush(batch)

    res.duration_sec = time.time() - t0
    store.touch_sources(norm_roots)
    store.record_scan_run(
        started_at=started.strftime("%Y-%m-%d %H:%M:%S"),
        finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        root=res.root, duration_sec=round(res.duration_sec, 2), scanned=res.scanned,
        added=res.added, updated=res.updated, skipped=res.skipped,
        failed=res.failed, workers=res.workers,
    )
    _emit("done", total_found, total_found,
          f"完成：新增 {res.added}，更新 {res.updated}，跳过 {res.skipped}，失败 {res.failed}")
    return res


__all__ = ["scan_paths", "scan_directory", "iter_nfo_files", "ScanResult"]
