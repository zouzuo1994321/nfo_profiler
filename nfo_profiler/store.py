# -*- coding: utf-8 -*-
"""SQLite 仓储层。

为什么用 SQLite 而不是内存 / 单个 JSON：
  * 5 万条记录 + 50 万条标签关系，内存里堆 dict 会吃掉几个 GB；
  * 增量扫描需要记住每个文件的 mtime/size，必须落盘；
  * 高频统计用 SQL GROUP BY 比 Python 循环快一个数量级；
  * 单文件数据库，方便备份、拷走、二次分析。

并发模型：
  * 工作进程只负责解析（CPU 密集），主进程单线程批量写入，从根上避免 SQLite 锁竞争；
  * Web 服务（ThreadingHTTPServer）是多线程的：扫描线程写库、请求线程读库会并发访问
    同一个连接。因此这里用一个 RLock 把所有 DB 访问串行化，既保证线程安全，又能在
    出现短暂锁竞争时由 busy_timeout 自动等待，而不是抛 "database is locked"。
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from datetime import datetime
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from .normalize import Normalizer
from .parser import FIELDS

SCHEMA_VERSION = 1

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS files (
    path         TEXT PRIMARY KEY,
    mtime        TEXT,
    size         INTEGER,
    status       TEXT,
    parse_status TEXT,
    encoding     TEXT,
    source       TEXT,
    scanned_at   TEXT
);

CREATE TABLE IF NOT EXISTS sources (
    id            INTEGER PRIMARY KEY,
    root          TEXT UNIQUE NOT NULL,
    label         TEXT,
    added_at      TEXT,
    last_scan_at  TEXT,
    movie_count   INTEGER DEFAULT 0,
    fail_count    INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS movies (
    id                INTEGER PRIMARY KEY,
    path              TEXT UNIQUE NOT NULL,
    num               TEXT,
    num_prefix        TEXT,
    title             TEXT,
    clean_title       TEXT,
    originaltitle     TEXT,
    year              INTEGER,
    premiered         TEXT,
    release           TEXT,
    dateadded         TEXT,
    runtime_min       INTEGER,
    userrating        REAL,
    rating_max        REAL,
    mpaa              TEXT,
    plot              TEXT,
    plot_len          INTEGER,
    series            TEXT,
    studio            TEXT,
    maker             TEXT,
    publisher         TEXT,
    label             TEXT,
    director          TEXT,
    actor_count       INTEGER,
    tag_count         INTEGER,
    resolution        TEXT,
    width             INTEGER,
    height            INTEGER,
    video_codec       TEXT,
    audio_codec       TEXT,
    duration_sec      INTEGER,
    censor_status     TEXT,
    original_filename TEXT,
    video_exists      INTEGER DEFAULT 0,
    video_size        INTEGER DEFAULT 0,
    website           TEXT,
    languages         TEXT,
    watched           INTEGER DEFAULT 0,
    playcount         INTEGER DEFAULT 0,
    filesize          INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS movie_tags (
    movie_id INTEGER NOT NULL,
    tag      TEXT,
    tag_key  TEXT
);

CREATE TABLE IF NOT EXISTS movie_actors (
    movie_id  INTEGER NOT NULL,
    actor     TEXT,
    actor_key TEXT,
    ord       INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS movie_directors (
    movie_id  INTEGER NOT NULL,
    director  TEXT
);

CREATE TABLE IF NOT EXISTS movie_tech (
    movie_id INTEGER NOT NULL,
    tech     TEXT
);

CREATE TABLE IF NOT EXISTS scan_runs (
    id           INTEGER PRIMARY KEY,
    started_at   TEXT,
    finished_at  TEXT,
    root         TEXT,
    duration_sec REAL,
    scanned      INTEGER,
    added        INTEGER,
    updated      INTEGER,
    skipped      INTEGER,
    failed       INTEGER,
    workers      INTEGER
);

CREATE INDEX IF NOT EXISTS idx_movies_num        ON movies(num);
CREATE INDEX IF NOT EXISTS idx_movies_year       ON movies(year);
CREATE INDEX IF NOT EXISTS idx_movies_studio     ON movies(studio);
CREATE INDEX IF NOT EXISTS idx_movies_series      ON movies(series);
CREATE INDEX IF NOT EXISTS idx_movies_res        ON movies(resolution);
CREATE INDEX IF NOT EXISTS idx_movies_censor     ON movies(censor_status);
CREATE INDEX IF NOT EXISTS idx_movies_rating     ON movies(userrating);
CREATE INDEX IF NOT EXISTS idx_movies_added      ON movies(dateadded);
CREATE INDEX IF NOT EXISTS idx_movies_premiered  ON movies(premiered);
CREATE INDEX IF NOT EXISTS idx_tags_key          ON movie_tags(tag_key);
CREATE INDEX IF NOT EXISTS idx_tags_movie        ON movie_tags(movie_id);
CREATE INDEX IF NOT EXISTS idx_actors_key        ON movie_actors(actor_key);
CREATE INDEX IF NOT EXISTS idx_actors_movie      ON movie_actors(movie_id);
CREATE INDEX IF NOT EXISTS idx_directors_name    ON movie_directors(director);
CREATE INDEX IF NOT EXISTS idx_tech_movie        ON movie_tech(movie_id);

-- 语义向量索引（AI 增强开启 + 本地 Ollama/MiniLM 时填充；离线启发式不需要）
CREATE TABLE IF NOT EXISTS embeddings (
    movie_id   INTEGER PRIMARY KEY,
    vec        BLOB,
    dim        INTEGER,
    model      TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_embeddings_model ON embeddings(model);
"""

#: 写入 movies 表的列（与 parser.FIELDS 的子集对应）
_MOVIE_COLS: Tuple[str, ...] = (
    "path", "num", "num_prefix", "title", "clean_title", "originaltitle",
    "year", "premiered", "release", "dateadded", "runtime_min",
    "userrating", "rating_max", "mpaa", "plot", "plot_len",
    "series", "studio", "maker", "publisher", "label", "director",
    "actor_count", "tag_count", "resolution", "width", "height",
    "video_codec", "audio_codec", "duration_sec", "censor_status",
    "original_filename", "video_exists", "video_size", "website",
    "languages", "watched", "playcount", "filesize", "source",
)

#: 需要为旧版本数据库补齐的列（{表名: [(列名, 类型)]}）
_MIGRATIONS = {
    "movies": [("source", "TEXT")],
    "files": [("source", "TEXT")],
}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Store:
    """NFO 数据仓储。所有 DB 访问均经 RLock 串行化，线程安全。"""

    def __init__(self, db_path: str, normalizer: Optional[Normalizer] = None) -> None:
        self.db_path = db_path
        self.norm = normalizer or Normalizer()
        # 多线程（扫描写 + Web 读）共享本 Store，用可重入锁串行化所有 DB 访问。
        self._lock = threading.RLock()
        parent = os.path.dirname(os.path.abspath(db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        # check_same_thread=False：Web 界面在多线程环境下复用同一个连接；
        # 写入仍由单一线程/调用方加锁保证，这里只放开 sqlite3 自身的线程检查。
        self.conn = sqlite3.connect(db_path, timeout=30.0, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # 初始化建表 + 写 meta 可能遇到其他进程短暂持锁，重试若干次并给出可读报错。
        last_exc: Optional[Exception] = None
        for attempt in range(5):
            try:
                with self._lock:
                    self.conn.execute("PRAGMA busy_timeout=30000")
                    self.conn.executescript(_SCHEMA)
                    self._migrate()
                    self._set_meta("schema_version", str(SCHEMA_VERSION))
                break
            except sqlite3.OperationalError as exc:
                last_exc = exc
                msg = str(exc).lower()
                if "locked" in msg and attempt < 4:
                    time.sleep(1.0)
                    continue
                if "locked" in msg:
                    raise sqlite3.OperationalError(
                        "数据库被其他进程锁定，无法启动。请先关闭其他正在运行的本程序实例"
                        "（或用 kill.bat / 任务管理器结束 nfo_profiler.exe、python.exe），再重试。"
                    ) from exc
                raise
        else:
            if last_exc is not None:
                raise last_exc

    def _migrate(self) -> None:
        """为旧版本数据库补齐新增列，保证升级不丢数据。"""
        with self._lock:
            for table, cols in _MIGRATIONS.items():
                existing = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
                if not existing:
                    continue
                for col, decl in cols:
                    if col not in existing:
                        try:
                            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                        except sqlite3.OperationalError:
                            pass
            self.conn.commit()

    # ------------------------------------------------------------------
    # 基础
    # ------------------------------------------------------------------
    def _set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def get_meta(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def close(self) -> None:
        try:
            with self._lock:
                self.conn.commit()
        finally:
            self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # 增量：文件指纹
    # ------------------------------------------------------------------
    def known_files(self) -> Dict[str, Tuple[str, int]]:
        """返回 {path: (mtime, size)}，用于增量扫描跳过未变动文件。"""
        out: Dict[str, Tuple[str, int]] = {}
        with self._lock:
            for row in self.conn.execute("SELECT path, mtime, size FROM files"):
                out[row["path"]] = (row["mtime"] or "", row["size"] or 0)
        return out

    def count_movies(self) -> int:
        with self._lock:
            return int(self.conn.execute("SELECT COUNT(*) c FROM movies").fetchone()["c"])

    def count_files(self) -> int:
        with self._lock:
            return int(self.conn.execute("SELECT COUNT(*) c FROM files").fetchone()["c"])

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def upsert_records(
        self,
        records: Sequence[Dict[str, Any]],
        *,
        commit: bool = True,
    ) -> Tuple[int, int]:
        """批量写入解析结果，返回 (新增数, 更新数)。"""
        with self._lock:
            added = updated = 0
            cur = self.conn.cursor()
            placeholders = ",".join("?" * len(_MOVIE_COLS))
            update_clause = ",".join(f"{c}=excluded.{c}" for c in _MOVIE_COLS if c != "path")

            for rec in records:
                path = rec.get("path") or ""
                if not path:
                    continue

                status = rec.get("parse_status", "")
                failed = status.startswith(("read_error", "parse_error"))

                # files 表指纹
                cur.execute(
                    "INSERT INTO files(path, mtime, size, status, parse_status, encoding, source, scanned_at) "
                    "VALUES(?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime, size=excluded.size, "
                    "status=excluded.status, parse_status=excluded.parse_status, "
                    "encoding=excluded.encoding, source=excluded.source, "
                    "scanned_at=excluded.scanned_at",
                    (
                        path,
                        rec.get("mtime", ""),
                        int(rec.get("filesize") or 0),
                        "failed" if failed else ("ok" if status == "ok" else "recovered"),
                        status,
                        rec.get("encoding", ""),
                        rec.get("source", "") or "",
                        _now(),
                    ),
                )
                if failed:
                    continue

                row = cur.execute("SELECT id FROM movies WHERE path=?", (path,)).fetchone()
                is_new = row is None

                values = []
                for col in _MOVIE_COLS:
                    v = rec.get(col, "")
                    if col in ("year", "runtime_min", "width", "height", "duration_sec",
                               "video_exists", "video_size", "watched", "playcount",
                               "filesize", "actor_count", "tag_count", "plot_len"):
                        v = int(v) if str(v).strip().lstrip("-").isdigit() else 0
                    elif col in ("userrating", "rating_max"):
                        try:
                            v = float(v)
                        except (TypeError, ValueError):
                            v = 0.0
                    elif v is None:
                        v = ""
                    values.append(v)

                cur.execute(
                    f"INSERT INTO movies({','.join(_MOVIE_COLS)}) VALUES({placeholders}) "
                    f"ON CONFLICT(path) DO UPDATE SET {update_clause}",
                    values,
                )

                if is_new:
                    mid = cur.lastrowid
                    added += 1
                else:
                    mid = row["id"]
                    updated += 1
                    for t in ("movie_tags", "movie_actors", "movie_directors", "movie_tech"):
                        cur.execute(f"DELETE FROM {t} WHERE movie_id=?", (mid,))

                # 标签
                tags = rec.get("tags") or []
                if tags:
                    cur.executemany(
                        "INSERT INTO movie_tags(movie_id, tag, tag_key) VALUES(?,?,?)",
                        [(mid, t, self.norm.tag_key(t)) for t in tags if t],
                    )
                # 演员
                actors = rec.get("actors") or []
                if actors:
                    cur.executemany(
                        "INSERT INTO movie_actors(movie_id, actor, actor_key, ord) VALUES(?,?,?,?)",
                        [(mid, a, self.norm.actor_key(a), i) for i, a in enumerate(actors) if a],
                    )
                # 导演
                directors = rec.get("directors") or []
                if directors:
                    cur.executemany(
                        "INSERT INTO movie_directors(movie_id, director) VALUES(?,?)",
                        [(mid, d) for d in directors if d],
                    )
                # 技术标签
                techs = rec.get("tech_tags") or []
                if techs:
                    cur.executemany(
                        "INSERT INTO movie_tech(movie_id, tech) VALUES(?,?)",
                        [(mid, t) for t in techs if t],
                    )

            if commit:
                self.conn.commit()
            return added, updated

    # ------------------------------------------------------------------
    # 数据源（多路径）
    # ------------------------------------------------------------------
    def register_sources(self, roots: Iterable[str]) -> List[str]:
        """登记数据源根目录，返回规范化后的路径列表。"""
        now = _now()
        out: List[str] = []
        with self._lock:
            for raw in roots:
                root = os.path.abspath(os.path.expanduser(str(raw).strip().strip('"')))
                if not root:
                    continue
                out.append(root)
                self.conn.execute(
                    "INSERT INTO sources(root, label, added_at) VALUES(?,?,?) "
                    "ON CONFLICT(root) DO NOTHING",
                    (root, os.path.basename(root.rstrip("\\/")) or root, now),
                )
            self.conn.commit()
        return out

    def touch_sources(self, roots: Iterable[str]) -> None:
        """更新数据源的最后扫描时间与作品数。"""
        with self._lock:
            now = _now()
            for root in roots:
                cnt = self.conn.execute(
                    "SELECT COUNT(*) c FROM movies WHERE source=?", (root,)).fetchone()["c"]
                fail = self.conn.execute(
                    "SELECT COUNT(*) c FROM files WHERE source=? AND status='failed'", (root,)
                ).fetchone()["c"]
                self.conn.execute(
                    "UPDATE sources SET last_scan_at=?, movie_count=?, fail_count=? WHERE root=?",
                    (now, cnt, fail, root),
                )
            self.conn.commit()

    def list_sources(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(
                "SELECT id, root, label, added_at, last_scan_at, movie_count, fail_count "
                "FROM sources ORDER BY root")]

    def remove_source(self, root: str) -> int:
        """删除某个数据源及其下所有作品记录。返回删除的作品数。"""
        with self._lock:
            root = os.path.abspath(root)
            ids = [r["id"] for r in self.conn.execute(
                "SELECT id FROM movies WHERE source=?", (root,))]
            n = len(ids)
            if ids:
                qmarks = ",".join("?" * len(ids))
                for t in ("movie_tags", "movie_actors", "movie_directors", "movie_tech"):
                    self.conn.execute(f"DELETE FROM {t} WHERE movie_id IN ({qmarks})", ids)
                self.conn.execute(f"DELETE FROM movies WHERE id IN ({qmarks})", ids)
            self.conn.execute("DELETE FROM files WHERE source=?", (root,))
            self.conn.execute("DELETE FROM sources WHERE root=?", (root,))
            self.conn.commit()
        return n

    def count_movies_by_source(self) -> Dict[str, int]:
        with self._lock:
            return {
                r["source"] or "(未分类)": r["c"]
                for r in self.conn.execute(
                    "SELECT source, COUNT(*) c FROM movies GROUP BY source ORDER BY c DESC")
            }

    def record_scan_run(self, **kw: Any) -> int:
        with self._lock:
            cols = ("started_at", "finished_at", "root", "duration_sec", "scanned",
                    "added", "updated", "skipped", "failed", "workers")
            data = {c: kw.get(c, "" if c in ("started_at", "finished_at", "root") else 0) for c in cols}
            cur = self.conn.execute(
                f"INSERT INTO scan_runs({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                [data[c] for c in cols],
            )
            self.conn.commit()
        return int(cur.lastrowid)

    # ------------------------------------------------------------------
    # 重建归一化键（换了同义词表后调用）
    # ------------------------------------------------------------------
    def reindex_keys(self, batch: int = 20000) -> int:
        """用当前 Normalizer 重算所有 tag_key / actor_key。返回影响行数。"""
        with self._lock:
            cur = self.conn.cursor()
            n = 0
            while True:
                rows = cur.execute(
                    "SELECT rowid, tag FROM movie_tags WHERE tag_key IS NULL OR tag_key='' LIMIT ?",
                    (batch,),
                ).fetchall()
                if not rows:
                    break
                cur.executemany(
                    "UPDATE movie_tags SET tag_key=? WHERE rowid=?",
                    [(self.norm.tag_key(r["tag"]), r["rowid"]) for r in rows],
                )
                n += len(rows)
            while True:
                rows = cur.execute(
                    "SELECT rowid, actor FROM movie_actors WHERE actor_key IS NULL OR actor_key='' LIMIT ?",
                    (batch,),
                ).fetchall()
                if not rows:
                    break
                cur.executemany(
                    "UPDATE movie_actors SET actor_key=? WHERE rowid=?",
                    [(self.norm.actor_key(r["actor"]), r["rowid"]) for r in rows],
                )
                n += len(rows)
            self.conn.commit()
        return n

    def force_reindex_all(self) -> int:
        """强制重算全部键（同义词表变更后使用）。"""
        with self._lock:
            self.conn.execute("UPDATE movie_tags SET tag_key=NULL")
            self.conn.execute("UPDATE movie_actors SET actor_key=NULL")
            self.conn.commit()
        return self.reindex_keys()

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------
    def iter_movies(
        self,
        *,
        where: str = "",
        params: Sequence[Any] = (),
        limit: int = 0,
    ) -> Iterator[sqlite3.Row]:
        sql = "SELECT * FROM movies"
        if where:
            sql += " WHERE " + where
        sql += " ORDER BY id"
        if limit:
            sql += f" LIMIT {int(limit)}"
        with self._lock:
            yield from self.conn.execute(sql, tuple(params))

    def movie_count(self, where: str = "", params: Sequence[Any] = ()) -> int:
        sql = "SELECT COUNT(*) c FROM movies"
        if where:
            sql += " WHERE " + where
        with self._lock:
            return int(self.conn.execute(sql, tuple(params)).fetchone()["c"])

    def all_movies_light(self) -> List[sqlite3.Row]:
        """统计画像所需的最小列集（不含 plot，省内存）。"""
        with self._lock:
            return list(
                self.conn.execute(
                    "SELECT id, num, num_prefix, year, premiered, dateadded, runtime_min, "
                    "userrating, rating_max, series, studio, maker, publisher, label, director, "
                    "actor_count, tag_count, resolution, censor_status, video_size, "
                    "duration_sec, watched, playcount FROM movies"
                )
            )

    def all_plots(self, limit: int = 8000, source: Optional[str] = None) -> List[str]:
        sql = "SELECT plot FROM movies WHERE plot IS NOT NULL AND plot<>''"
        params: List[Any] = []
        if source:
            sql += " AND source = ?"
            params.append(source)
        sql += " LIMIT ?"
        params.append(limit)
        with self._lock:
            return [r["plot"] for r in self.conn.execute(sql, params)]

    def tag_pairs(self) -> Iterator[Tuple[int, List[str]]]:
        """按电影产出标签列表，用于共现统计（流式，不一次性加载）。"""
        with self._lock:
            cur = self.conn.execute(
                "SELECT movie_id, tag FROM movie_tags ORDER BY movie_id"
            )
            current_id: Optional[int] = None
            buf: List[str] = []
            for row in cur:
                if row["movie_id"] != current_id:
                    if current_id is not None and buf:
                        yield current_id, buf
                    current_id = row["movie_id"]
                    buf = []
                buf.append(row["tag"])
            if current_id is not None and buf:
                yield current_id, buf

    def vacuum(self) -> None:
        with self._lock:
            self.conn.execute("VACUUM")

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "movies": self.count_movies(),
                "files": self.count_files(),
                "db_size_mb": round(os.path.getsize(self.db_path) / 1048576, 2)
                if os.path.exists(self.db_path) else 0,
                "schema_version": self.get_meta("schema_version"),
            }


__all__ = ["Store", "SCHEMA_VERSION"]
