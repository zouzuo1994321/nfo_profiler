# -*- coding: utf-8 -*-
"""v1.3.3 清理失效记录：单元验证（不碰真实库，用临时目录 + 临时库）。

验证项：
  1. 只删幽灵记录：2 条真实 NFO + 1 条已删除 → 清理后剩 2 条；
  2. 子表与偏好一并清理（tags / actors / preferences）；
  3. **空集合保护**：数据源遍历结果为空（掉盘 / 路径失效）→ 一条都不删；
  4. **不可访问目录**：直接跳过并给出警告，不清空该源；
  5. 独立清理入口 `prune_missing_paths` 与扫描收尾用的是同一套 store 逻辑。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nfo_profiler.normalize import Normalizer  # noqa: E402
from nfo_profiler.scanner import prune_missing_paths  # noqa: E402
from nfo_profiler.store import Store  # noqa: E402


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="nfo_prune_")
    root = os.path.join(tmp, "src")
    os.makedirs(os.path.join(root, "sub"), exist_ok=True)
    real_a = os.path.join(root, "a.nfo")
    real_b = os.path.join(root, "sub", "b.nfo")
    for p in (real_a, real_b):
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("<movie><title>x</title></movie>")
    ghost = os.path.join(root, "ghost.nfo")   # 只入库，磁盘上不存在

    db = os.path.join(tmp, "t.db")
    store = Store(db, normalizer=Normalizer())
    for i, p in enumerate((real_a, real_b, ghost), start=1):
        store.upsert_records([{
            "path": p, "num": f"TEST-{i:03d}", "title": f"t{i}",
            "source": root, "parse_status": "ok",
            "tags": ["标签A"], "actors": ["演员甲"],
        }])
    assert store.count_movies() == 3, f"应入库 3 条，实际 {store.count_movies()}"
    store.upsert_vote(3, "TEST-003", 1)      # 给幽灵记录投一票，验证一并清理
    print(f"[1] 构造完成：真实 2 条 + 幽灵 1 条（库内共 {store.count_movies()} 条）")

    # --- 3. 空集合保护（掉盘 / 路径失效） ---
    n0 = store.prune_missing(root, set())
    assert n0 == 0 and store.count_movies() == 3, \
        f"空集合绝不能删数据：删了 {n0} 条，剩 {store.count_movies()}"
    print("[2] 空集合保护 OK：existing 为空时一条都不删（防掉盘误删）")

    # --- 4. 不可访问的目录 ---
    n_bad, warns = prune_missing_paths(store, [os.path.join(tmp, "no_such_dir")])
    assert n_bad == 0 and store.count_movies() == 3 and warns, \
        f"不可访问目录应跳过并给出警告：{n_bad} / {warns}"
    print(f"[3] 不可访问目录 OK：跳过 + 警告「{warns[0][:36]}…」，未误删")

    # --- 1 & 2. 正常清理 ---
    n, warns = prune_missing_paths(store, [root])
    left = store.count_movies()
    assert n == 1, f"应只删 1 条幽灵记录，实际 {n}"
    assert left == 2, f"清理后应剩 2 条，实际 {left}"
    paths = {r["path"] for r in store.conn.execute("SELECT path FROM movies")}
    assert ghost not in paths and real_a in paths and real_b in paths, \
        f"删错了对象：{paths}"
    # 子表 + 投票应一并清掉
    n_tags = store.conn.execute("SELECT COUNT(*) c FROM movie_tags").fetchone()["c"]
    n_vote = store.conn.execute("SELECT COUNT(*) c FROM preferences").fetchone()["c"]
    assert n_tags == 2, f"标签应剩 2 条（幽灵的已删），实际 {n_tags}"
    assert n_vote == 0, f"幽灵记录的投票应一并删除，实际剩 {n_vote}"
    # --- 4b. 超比例保护（疑似盘未挂载 / 只读到一部分） ---
    #     造 3 条真实 + 2 条幽灵（待删 2/5 = 40%，未超阈值 → 正常删）
    n_half, warns_half = prune_missing_paths(store, [root])
    assert n_half == 0 and store.count_movies() == 2, "已无幽灵可删"
    #     直接调 store：把 existing 换成几乎不含本源路径的集合 → 100% 待删
    try:
        blocked = store.prune_missing(root, {"Z:/definitely/not/here.nfo"})
        assert blocked == 0, f"待删比例 100% 时应拒绝删除，实际删了 {blocked}"
        assert store.count_movies() == 2, "超阈值却删了数据"
    except Exception as exc:  # pragma: no cover
        raise AssertionError(f"超比例保护异常：{exc}")
    print("[4b] 超比例保护 OK：待删 100% 时拒绝执行（疑似离线），数据未动")

    print(f"[4] 清理 OK：删 {n} 条幽灵（剩 {left} 条），标签剩 {n_tags}、投票剩 {n_vote}")

    store.close()
    shutil.rmtree(tmp, ignore_errors=True)
    print("\n[v1.3.3 清理功能单元验证全部通过]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
