# -*- coding: utf-8 -*-
"""诊断：库内有多少条记录「磁盘上已经不存在」（即清理功能应当删掉的量）。

为什么不用逐个 os.path.exists：库里几万条、数据源在网络盘，逐个 stat 要走
几万个来回（分钟级且抖）。这里**按目录缓存 listdir** —— 同一个目录只列一次，
拿到文件名集合后批量比对，目录级别的往返数直接降一个数量级。

用法：

    python tools/diag_missing.py                    # 默认 output/nfo.db
    python tools/diag_missing.py --db D:/copy.db
    python tools/diag_missing.py --sample 2000      # 只抽查前 N 条（快速摸底）
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from collections import defaultdict


def main() -> int:
    db = os.path.join("output", "nfo.db")
    sample = 0
    if "--db" in sys.argv:
        db = sys.argv[sys.argv.index("--db") + 1]
    if "--sample" in sys.argv:
        sample = int(sys.argv[sys.argv.index("--sample") + 1])

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    sql = "SELECT id, path, num, source FROM movies"
    if sample:
        sql += f" LIMIT {sample}"
    rows = list(conn.execute(sql))
    total = len(rows)
    print(f"库内记录：{total} 条（db={db}）")

    # 按目录分组：同一目录只 listdir 一次
    by_dir: dict = defaultdict(list)
    for r in rows:
        p = r["path"] or ""
        if p:
            by_dir[os.path.dirname(p)].append(r)

    print(f"涉及目录：{len(by_dir)} 个，开始比对…（网络盘可能要几分钟）")
    gone: list = []
    gone_dirs: list = []
    t0 = time.time()
    cache: dict = {}
    for i, (d, items) in enumerate(by_dir.items()):
        try:
            names = cache.get(d)
            if names is None:
                names = set(os.listdir(d))
                cache[d] = names
        except OSError:
            # 整个目录都读不到 → 目录内所有记录都算失效
            gone_dirs.append((d, len(items)))
            gone.extend(items)
            names = None
            continue
        for r in items:
            if os.path.basename(r["path"] or "") not in names:
                gone.append(r)
        if (i + 1) % 500 == 0:
            print(f"  …{i + 1}/{len(by_dir)} 目录，已发现失效 {len(gone)} 条 "
                  f"（{time.time() - t0:.0f}s）")

    # 按数据源汇总
    by_src: dict = defaultdict(int)
    for r in gone:
        by_src[r["source"] or "(未分类)"] += 1
    print(f"\n=== 磁盘上已不存在（清理目标）：{len(gone)} / {total} 条 ===")
    for s, n in sorted(by_src.items(), key=lambda x: -x[1]):
        print(f"  {n:>7}  {s}")
    if gone_dirs:
        print(f"其中整个目录读不到的：{len(gone_dirs)} 个目录")
        for d, n in sorted(gone_dirs, key=lambda x: -x[1])[:10]:
            print(f"  {n:>7}  {d}")
    print(f"耗时 {time.time() - t0:.1f}s")
    if gone:
        print("\n示例（前 10 条）：")
        for r in gone[:10]:
            print(f"  {r['num']}  {r['path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
