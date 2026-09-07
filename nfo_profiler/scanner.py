# -*- coding: utf-8 -*-
"""扫描引擎：多进程解析 + 主进程单写 + 增量更新。

性能设计：
  * 解析是 CPU 密集（XML + 正则），用 ProcessPoolExecutor 摊到多核；
  * 写库只在主进程做，天然避免 SQLite 锁竞争；
  * 增量扫描按 (mtime, size) 指纹跳过未变动文件，二次运行几乎瞬时；
  * 批处理大小自适应，避免攒太多 dict 占内存。
"""

from __future__ import annotations

import multiprocessing
import os
import tempfile
import threading
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


def default_stop_sentinel() -> str:
    """跨进程共享的「终止哨兵」文件路径。

    设计缘由：``multiprocessing.Event`` 在 ``ProcessPoolExecutor`` 里走的是
    Semaphore 代理，PyInstaller ``--onefile`` 模式下每个子进程都会重新解压到
    各自的临时目录，Semaphore 在不同解压目录间同步不可靠——主进程 ``set()``
    后子进程可能根本收不到。

    改用文件系统哨兵：任何子进程都能 ``os.path.exists()`` 看到，且不依赖
    multiprocessing 同步层。所有扫描任务共用同一个文件，方便互斥。
    """
    return os.path.join(tempfile.gettempdir(), "nfo_profiler_scan.stop")


def _check_sentinel(sentinel: Optional[str]) -> bool:
    return bool(sentinel and os.path.exists(sentinel))


def _worker_parse_batch(
    paths: Sequence[str],
    probe_video: bool = True,
    sentinel: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """子进程入口：批量解析 + 哨兵文件检查，减少 IPC 开销、支持即时终止。

    注意：``sentinel`` 只用来读取文件系统状态，与 multiprocessing 无关。
    """
    out: List[Dict[str, Any]] = []
    for p in paths:
        if _check_sentinel(sentinel):
            break
        try:
            out.append(parse_nfo_file(p, probe_video=probe_video))
        except Exception as exc:  # pragma: no cover - 极端兜底
            out.append({"path": p, "parse_status": f"worker_error:{exc.__class__.__name__}"})
    return out


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
        self.pruned = 0          # v1.3.0：清理掉的「磁盘已删除」记录数
        self.stopped = False      # True 表示被 GUI「终止」而提前结束
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
            "pruned": self.pruned,
            "duration_sec": round(self.duration_sec, 2),
            "workers": self.workers,
            "stopped": self.stopped,
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
    cancel_event: Optional[Any] = None,
    pause_event: Optional[threading.Event] = None,
    stop_sentinel: Optional[str] = None,
    parse_batch: int = 40,
) -> ScanResult:
    """扫描目录并写入数据库（单路径，内部转调 scan_paths）。"""
    return scan_paths(
        [root], store, workers=workers, incremental=incremental,
        probe_video=probe_video, batch_size=batch_size,
        progress=progress, on_file=on_file, max_files=max_files,
        cancel_event=cancel_event, pause_event=pause_event,
        stop_sentinel=stop_sentinel, parse_batch=parse_batch,
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
    cancel_event: Optional[Any] = None,
    pause_event: Optional[threading.Event] = None,
    stop_sentinel: Optional[str] = None,
    parse_batch: int = 40,
    prune_missing: bool = False,
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
    cancel_event:
        （已废弃，保留向后兼容）``multiprocessing.Event``；置位后仅在主进程生效，
        实际跨进程信号请用 ``stop_sentinel``。
    pause_event:
        线程 ``Event``；清空后主线程在批次间等待，置位后继续。
        用于 GUI「暂停/继续扫描」。
    stop_sentinel:
        文件路径；写入表示终止，主线程与所有子进程都会在每个文件 / 批次边界检查它。
        跨进程可靠，推荐使用。文件若已存在，``scan_paths`` 启动时会先尝试删除。
    parse_batch:
        每个子进程一次接收多少个文件批量解析，默认 40。
        文件很多时可显著降低 IPC 开销；量很小时会自动降到 1。
    prune_missing:
        v1.3.0：为 True 时，扫描收尾会把「库里有记录、但磁盘上已找不到 NFO」的
        条目删掉（视频被删/目录搬走后，库里残留的幽灵记录）。
        复用本次收集到的文件路径集合做差集，**不产生额外磁盘 IO**。
        通常在「关闭增量扫描（全量重扫）」时开启。
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

    # 统一终止哨兵：兼容老的 cancel_event，但实际信号靠 stop_sentinel
    sentinel = stop_sentinel or default_stop_sentinel()
    if cancel_event is not None:
        # 若调用方既给了 cancel_event 也给了 stop_sentinel，以 stop_sentinel 优先
        sentinel = stop_sentinel or default_stop_sentinel()
    # 启动前先清理一次残留（防上回异常退出留下的终止标记）
    try:
        os.remove(sentinel)
    except OSError:
        pass

    def _stop_requested() -> bool:
        if _check_sentinel(sentinel):
            return True
        if cancel_event is not None and cancel_event.is_set():
            return True
        return False

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

    write_buf: List[Dict[str, Any]] = []
    done = 0
    chunk = max(batch_size, n_workers * 40)
    #: 进度刷新节流：既按文件数（密集扫描时），也按墙钟时间（文件大、单文件慢时）
    #: 保证前端轮询能拿到平滑推进的 done / current，避免进度条"卡住"到末尾才跳。
    emit_every = max(20, chunk // 15)           # 每 ~1/15 批至少刷一次
    last_emit_ts = time.time()
    emit_interval = 0.1                         # 秒：最快 100ms 上报一次
    last_emit_done = 0                          # 上次已上报的 done 计数
    cancelled = False

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

    def _process_result(rec: Dict[str, Any], src: str) -> None:
        nonlocal done
        rec["source"] = src
        write_buf.append(rec)
        done += 1
        _maybe_emit(rec.get("path", ""))
        if len(write_buf) >= chunk:
            _flush(write_buf)
            write_buf.clear()

    if n_workers == 1:
        for p, src in todo:
            if _stop_requested():
                cancelled = True
                break
            if pause_event is not None:
                pause_event.wait()
            rec = _worker_parse(p, probe_video)
            _process_result(rec, src)
    else:
        # 批量解析：每个子进程一次处理 parse_batch 个文件，显著降低 IPC 开销。
        # 文件数很少时自动降到 1，避免不必要的批聚合。
        pb = max(1, min(parse_batch, len(todo) // (n_workers * 2) or 1))
        work_batches: List[List[Tuple[str, str]]] = [
            todo[i:i + pb] for i in range(0, len(todo), pb)
        ]
        with ProcessPoolExecutor(max_workers=n_workers) as pool:
            futures: Dict[Any, List[Tuple[str, str]]] = {}
            for wb in work_batches:
                paths_only = [p for p, _ in wb]
                fut = pool.submit(_worker_parse_batch, paths_only, probe_video, sentinel)
                futures[fut] = wb
            for fut in as_completed(futures):
                if pause_event is not None:
                    pause_event.wait()
                # 总是先把这个已完成批次的记录落库（fut 已 done），再判断是否终止。
                # 否则若哨兵恰巧在这一批 done 之后置位，会把整批已解析的结果丢掉。
                wb = futures[fut]
                try:
                    recs = fut.result()
                except Exception as exc:
                    recs = [{"path": p, "parse_status": f"future_error:{exc.__class__.__name__}"}
                            for p, _ in wb]
                for rec, (path, src) in zip(recs, wb):
                    _process_result(rec, src)
                if _stop_requested():
                    cancelled = True
                    break
    _flush(write_buf)

    if cancelled:
        # 已解析的记录先落库再退出，避免本次已做的工作被丢弃
        _flush(write_buf)
        write_buf.clear()
        res.duration_sec = time.time() - t0
        res.stopped = True
        # 清理哨兵，避免下次启动误判
        try:
            os.remove(sentinel)
        except OSError:
            pass
        _emit("done", done, len(todo), f"已终止：处理了 {done}/{len(todo)} 个文件")
        return res

    # --- v1.3.0：清理已删除文件的记录（关闭增量扫描 = 全量重扫时启用） ---
    # 复用上面收集到的 ``unique_files``（本次磁盘上真实存在的 NFO），
    # 与库内该数据源的记录做差集 —— 不需要额外的磁盘 IO，代价几乎为零。
    if prune_missing and not cancelled:
        by_root: Dict[str, set] = {r: set() for r in norm_roots}
        for p, r in unique_files:
            by_root.setdefault(r, set()).add(os.path.normcase(os.path.abspath(p)))
        for r in norm_roots:
            _emit("prune", 0, 0, f"正在清理失效记录：{r}")
            try:
                n = store.prune_missing(r, by_root.get(r) or set())
            except Exception as exc:  # 清理失败不影响主流程
                res.errors.append((r, f"prune_error:{exc.__class__.__name__}"))
                continue
            res.pruned += int(n)
            if n:
                _emit("prune", n, n, f"{os.path.basename(r)}：清理 {n} 条失效记录")

    res.duration_sec = time.time() - t0
    # 清理过失效记录后，数据源的作品数会变，这里统一刷新一次统计
    store.touch_sources(norm_roots)
    store.record_scan_run(
        started_at=started.strftime("%Y-%m-%d %H:%M:%S"),
        finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        root=res.root, duration_sec=round(res.duration_sec, 2), scanned=res.scanned,
        added=res.added, updated=res.updated, skipped=res.skipped,
        failed=res.failed, workers=res.workers,
    )
    # 正常完成后也清掉哨兵（不会主动删除，但兜底防止意外残留）
    try:
        os.remove(sentinel)
    except OSError:
        pass
    _emit("done", total_found, total_found,
          f"完成：新增 {res.added}，更新 {res.updated}，跳过 {res.skipped}，失败 {res.failed}")
    return res


__all__ = ["scan_paths", "scan_directory", "iter_nfo_files", "ScanResult"]
