# -*- coding: utf-8 -*-
"""命令行入口。

    python run.py scan   <目录>      扫描 NFO 入库
    python run.py report             生成可视化 HTML 画像报告
    python run.py export             导出 CSV / JSON / XLSX / Markdown / HTML
    python run.py ui                 启动本地 Web 界面
    python run.py stats              查看数据库概况
    python run.py reindex            用当前同义词表重建归一化键
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from . import APP_NAME, __version__
from .analyze import Analyzer
from .exporter import export_all, export_csv, export_json, export_markdown, export_xlsx
from .normalize import Normalizer
from .scanner import scan_paths
from .store import Store

DEFAULT_DB = os.path.join("output", "nfo.db")
DEFAULT_OUT = "output"


# ---------------------------------------------------------------------------
# 控制台输出
# ---------------------------------------------------------------------------

def _setup_console() -> None:
    """Windows 控制台默认 GBK，强制切 UTF-8，避免中文路径与艺人乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _bar(done: int, total: int, width: int = 28) -> str:
    if total <= 0:
        return ""
    p = min(1.0, done / total)
    filled = int(width * p)
    return "[" + "█" * filled + "·" * (width - filled) + "]"


def _line(msg: str = "") -> None:
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


def _rule(title: str) -> None:
    _line()
    _line("─" * 60)
    _line(f"  {title}")
    _line("─" * 60)


# ---------------------------------------------------------------------------
# 公共
# ---------------------------------------------------------------------------

def load_config(path: Optional[str]) -> Normalizer:
    """加载同义词 / 别名配置（frozen 时回退到随 exe 携带的 config）。"""
    from .normalize import resolve_config_paths

    return Normalizer.from_files(*resolve_config_paths(path))


def open_store(db_path: str, config: Optional[str] = None) -> Store:
    return Store(db_path, normalizer=load_config(config))


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------

def _collect_roots(args: argparse.Namespace) -> List[str]:
    """汇总命令行传入的多个目录 + --paths-file 里的目录。"""
    roots: List[str] = list(getattr(args, "directories", []) or [])
    pf = getattr(args, "paths_file", None)
    if pf:
        if not os.path.isfile(pf):
            _line(f"[错误] 路径列表文件不存在：{pf}")
            return []
        with open(pf, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if line and not line.startswith("#"):
                    roots.append(line)
    # 去重保序
    out: List[str] = []
    for r in roots:
        k = os.path.abspath(os.path.expanduser(r.strip().strip('"')))
        if k not in out:
            out.append(k)
    return out


def cmd_scan(args: argparse.Namespace) -> int:
    roots = _collect_roots(args)
    if not roots:
        _line("[错误] 请至少指定一个目录（或用 --paths-file 指定路径列表文件）")
        return 2

    store = open_store(args.db, args.config)
    _rule(f"扫描 {len(roots)} 个数据源")
    for r in roots:
        mark = "OK  " if os.path.isdir(r) else "缺失"
        _line(f"  [{mark}] {r}")
    _line(f"数据库：{os.path.abspath(args.db)}")
    _line(f"模式：{'增量（跳过未变动文件）' if not args.full else '全量重扫'}　进程数：{args.workers or '自动'}")
    _line()

    def progress(phase: str, done: int, total: int, msg: str) -> None:
        if phase == "collect":
            sys.stdout.write(f"\r  遍历中… 已发现 {done} 个 NFO   ")
        elif phase == "parse":
            pct = f"{done*100//total:3d}%" if total else "  0%"
            sys.stdout.write(f"\r  解析中 {_bar(done, total)} {pct}  {done}/{total}   ")
        elif phase == "done":
            sys.stdout.write("\r" + " " * 100 + "\r")
        sys.stdout.flush()

    res = scan_paths(
        roots, store,
        workers=args.workers,
        incremental=not args.full,
        probe_video=not args.no_probe_video,
        progress=progress,
    )
    d = res.as_dict()
    _line(f"\n  扫描完成，耗时 {d['duration_sec']} 秒（{d['speed_per_sec']} 文件/秒）")
    _line(f"    新增   {d['added']}")
    _line(f"    更新   {d['updated']}")
    _line(f"    跳过   {d['skipped']}（未变动）")
    _line(f"    降级   {d['recovered']}（XML 损坏，已用正则抢救）")
    _line(f"    失败   {d['failed']}")
    if d.get("duplicate_paths"):
        _line(f"    去重   {d['duplicate_paths']}（多路径重叠）")
    if res.errors:
        _line("\n  异常条目（前 10 条）：")
        for p, why in res.errors[:10]:
            _line(f"    · {why}  {p}")

    _line("\n  各数据源作品数：")
    for src, cnt in store.count_movies_by_source().items():
        _line(f"    {cnt:>7}  {src}")
    st = store.stats()
    _line(f"\n  数据库现有 {st['movies']} 部作品，{st['db_size_mb']} MB")
    store.close()
    return 0


def cmd_sources(args: argparse.Namespace) -> int:
    """管理数据源：列出 / 删除。"""
    store = open_store(args.db, args.config)
    if args.remove:
        n = store.remove_source(args.remove)
        _line(f"  已移除数据源「{args.remove}」，删除 {n} 部作品记录")
        store.close()
        return 0

    _rule("数据源")
    srcs = store.list_sources()
    if not srcs:
        _line("  还没有登记任何数据源，先用 scan 命令添加。")
    for s in srcs:
        _line(f"\n  · {s['root']}")
        _line(f"      作品 {s['movie_count']}　失败 {s['fail_count']}　"
              f"登记于 {s['added_at'] or '-'}　最后扫描 {s['last_scan_at'] or '从未'}")
    st = store.stats()
    _line(f"\n  合计 {st['movies']} 部作品")
    store.close()
    return 0


def _norm_source(value: Optional[str]) -> str:
    """归一化数据源路径。

    库里存的是 os.path.abspath 得到的绝对路径（Windows 为反斜杠），
    而命令行 / Git Bash 可能传入正斜杠或相对路径，必须统一后再比对，
    否则 --source 会筛出 0 条。
    """
    if not value:
        return ""
    return os.path.abspath(os.path.expanduser(str(value).strip().strip('"')))


def _build_data(store: Store, args: argparse.Namespace) -> Dict[str, Any]:
    src = _norm_source(getattr(args, "source", None))
    if src:
        args.source = src
        known = {s["root"] for s in store.list_sources()}
        if src not in known:
            _line(f"  [警告] 数据源「{src}」不在已登记列表中，可能筛出 0 条")
            _line("          可用 run.py sources 查看已登记的数据源")
        _line(f"  数据源筛选：{src}")
    an = Analyzer(store, source=src or None)
    _line("  正在统计…")
    t0 = time.time()
    data = an.build_report_data(
        top_tags=args.top_tags,
        top_actors=args.top_actors,
        top_misc=args.top_misc,
        cooc_tags=args.cooc_tags,
        keywords=args.keywords,
        with_cooccurrence=not args.no_cooccurrence,
        with_keywords=not args.no_keywords,
    )
    _line(f"  统计完成，耗时 {time.time() - t0:.2f} 秒")
    return data


def cmd_report(args: argparse.Namespace) -> int:
    store = open_store(args.db, args.config)
    if store.count_movies() == 0:
        _line("[提示] 数据库为空，请先执行 scan 命令。")
        store.close()
        return 1
    _rule("生成画像报告")
    data = _build_data(store, args)
    from .exporter import movies_for_report
    from .report import write_html_report

    movies = movies_for_report(store, limit=args.movies,
                                source=getattr(args, "source", None) or None)
    label = getattr(args, "source", "") or os.path.abspath(args.db)
    out = write_html_report(
        data, args.out, movies=movies, source_label=label,
        top_tags=args.top_tags, top_actors=args.top_actors,
    )
    _line(f"\n  报告已生成：{os.path.abspath(out)}")
    _line(f"  文件大小：{os.path.getsize(out)/1024:.1f} KB   内嵌明细：{len(movies)} 条")
    store.close()
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    store = open_store(args.db, args.config)
    if store.count_movies() == 0:
        _line("[提示] 数据库为空，请先执行 scan 命令。")
        store.close()
        return 1
    formats = [f.strip().lower() for f in args.format.split(",") if f.strip()]
    html = "html" in formats
    formats = [f for f in formats if f != "html"]

    _rule("导出数据")
    data = _build_data(store, args)
    result = export_all(
        data, args.out, store=store,
        formats=formats,
        html=html,
        report_movies=args.movies,
        source_label=getattr(args, "source", "") or os.path.abspath(args.db),
        source=getattr(args, "source", None) or None,
    )
    _line()
    for key in ("json", "xlsx", "md", "html"):
        if key in result:
            _line(f"  {key.upper():5s} → {result[key]}")
    if "csv" in result:
        _line(f"  CSV   → {len(result['csv'])} 个文件，目录：{os.path.join(args.out, 'csv')}")
        for p in result["csv"]:
            _line(f"           · {os.path.basename(p)}")
    store.close()
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    store = open_store(args.db, args.config)
    st = store.stats()
    _rule("数据库概况")
    _line(f"  数据库：{os.path.abspath(args.db)}")
    _line(f"  作品数：{st['movies']}　文件记录：{st['files']}　大小：{st['db_size_mb']} MB")
    if st["movies"]:
        an = Analyzer(store)
        ov = an.overview()
        dist = ov.get("distinct") or {}
        _line(f"  艺人：{dist.get('actors', 0)}　标签：{dist.get('tags', 0)}　"
              f"片商：{dist.get('studios', 0)}　系列：{dist.get('series', 0)}　导演：{dist.get('directors', 0)}")
        _line(f"  年份：{ov.get('year_min','')} – {ov.get('year_max','')}　"
              f"总时长：{ov.get('total_hours', 0)} 小时　平均评分：{ov.get('avg_rating', 0)}")
        by_src = store.count_movies_by_source()
        if len(by_src) > 1 or (by_src and list(by_src)[0] != "(未分类)"):
            _line("\n  各数据源作品数：")
            for src, cnt in by_src.items():
                _line(f"    {cnt:>7}  {src}")
        runs = list(store.conn.execute(
            "SELECT started_at, root, duration_sec, scanned, added, updated, skipped, failed, workers "
            "FROM scan_runs ORDER BY id DESC LIMIT 5"))
        if runs:
            _line("\n  最近扫描记录：")
            for r in runs:
                _line(f"    {r['started_at']}  {r['scanned']:>6} 个 / {r['duration_sec']}s / "
                      f"{r['workers']} 进程  新增{r['added']} 更新{r['updated']} 跳过{r['skipped']}")
    store.close()
    return 0


def cmd_reindex(args: argparse.Namespace) -> int:
    store = open_store(args.db, args.config)
    _rule("重建归一化键")
    n = store.force_reindex_all() if args.full else store.reindex_keys()
    _line(f"  已重算 {n} 条键（同义词表：{args.config or 'config/synonyms.json'}）")
    store.close()
    return 0


def _ensure_out(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


def cmd_ui(args: argparse.Namespace) -> int:
    from .webui import serve

    _rule("启动本地 Web 界面")
    _line(f"  数据库：{os.path.abspath(args.db)}")
    _line(f"  输出目录：{os.path.abspath(args.out)}")
    _line(f"  访问地址：http://{args.host}:{args.port}")
    _line("\n  按 Ctrl+C 停止服务\n")
    serve(
        db_path=args.db,
        out_dir=args.out,
        host=args.host,
        port=args.port,
        config=args.config,
        open_browser=not args.no_browser,
    )
    return 0


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run.py",
        description=f"{APP_NAME} v{__version__} —— 从海量 NFO 中提取信息并生成用户画像",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python run.py scan  \"Y:\\Jav\" --workers 8\n"
            "  python run.py report --out output/我的画像.html\n"
            "  python run.py export --format csv,xlsx,html --out output\n"
            "  python run.py ui --port 9527\n"
        ),
    )
    p.add_argument("-v", "--version", action="version", version=f"{APP_NAME} v{__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--db", default=DEFAULT_DB, help=f"数据库路径（默认 {DEFAULT_DB}）")
        sp.add_argument("--config", default=None, help="同义词/别名配置文件")

    sp = sub.add_parser("scan", help="扫描一个或多个 NFO 目录并入库")
    sp.add_argument("directories", nargs="*", help="要扫描的根目录，可同时传多个")
    sp.add_argument("--paths-file", default=None,
                    help="从文件读取目录列表（每行一个，# 开头为注释）")
    sp.add_argument("--workers", type=int, default=None, help="并发进程数（默认 CPU 核数，上限 8）")
    sp.add_argument("--full", action="store_true", help="全量重扫，忽略增量缓存")
    sp.add_argument("--no-probe-video", action="store_true", help="不探测视频文件（略快，但无容量统计）")
    common(sp)
    sp.set_defaults(func=cmd_scan)

    sp = sub.add_parser("sources", help="查看 / 管理已登记的数据源")
    sp.add_argument("--remove", default=None, metavar="ROOT", help="移除指定数据源及其全部记录")
    common(sp)
    sp.set_defaults(func=cmd_sources)

    for name, help_text in (("report", "生成可视化 HTML 画像报告"), ("export", "导出数据文件")):
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("--out", default=DEFAULT_OUT if name == "export"
                        else os.path.join(DEFAULT_OUT, "用户画像报告.html"),
                        help="输出路径（report 为文件，export 为目录）")
        sp.add_argument("--movies", type=int, default=1000, help="报告内嵌明细条数（默认 1000）")
        if name == "export":
            sp.add_argument("--format", default="csv,json,xlsx,md,html",
                            help="导出格式，逗号分隔：csv,json,xlsx,md,html")
        sp.add_argument("--top-tags", type=int, default=40, help="高频标签取前 N 个")
        sp.add_argument("--top-actors", type=int, default=30, help="高频艺人取前 N 个")
        sp.add_argument("--top-misc", type=int, default=20, help="片商/系列等取前 N 个")
        sp.add_argument("--cooc-tags", type=int, default=36, help="共现矩阵取前 N 个标签")
        sp.add_argument("--keywords", type=int, default=60, help="剧情高频词取前 N 个")
        sp.add_argument("--no-cooccurrence", action="store_true", help="跳过共现计算（大数据集可加速）")
        sp.add_argument("--no-keywords", action="store_true", help="跳过剧情高频词计算")
        sp.add_argument("--source", default=None, help="只统计指定数据源（根目录绝对路径），默认统计全部")
        common(sp)
        sp.set_defaults(func=cmd_report if name == "report" else cmd_export)

    sp = sub.add_parser("stats", help="查看数据库概况")
    common(sp)
    sp.set_defaults(func=cmd_stats)

    sp = sub.add_parser("reindex", help="用当前同义词表重建归一化键")
    sp.add_argument("--full", action="store_true", help="强制重算全部（默认只算缺失的）")
    common(sp)
    sp.set_defaults(func=cmd_reindex)

    sp = sub.add_parser("ui", help="启动本地 Web 界面")
    sp.add_argument("--host", default="127.0.0.1", help="监听地址")
    sp.add_argument("--port", type=int, default=9527, help="监听端口（默认 9527）")
    sp.add_argument("--out", default=DEFAULT_OUT, help="输出目录")
    sp.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    common(sp)
    sp.set_defaults(func=cmd_ui)

    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    _setup_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        _line("\n[已取消]")
        return 130
    except Exception as exc:  # pragma: no cover
        import traceback
        _line(f"\n[未预期的错误] {exc.__class__.__name__}: {exc}")
        traceback.print_exc()
        return 1


__all__ = ["main", "build_parser"]
