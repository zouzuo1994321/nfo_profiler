# -*- coding: utf-8 -*-
"""v1.2.0 桌面界面冒烟测试 —— 作品推荐模块 / 悬停预览 / 双击播放。

用法：

    set QT_QPA_PLATFORM=offscreen
    python tools/smoke_v12.py                     # 用 output/nfo.db
    python tools/smoke_v12.py --db D:/copy.db     # 指定副本（主程序正开着时用）

验收项：
  1. 主 Tab 顺序：④ 作品推荐 插在 ③ 作品明细 与 ⑤ 重复检测 之间（共 8 个 Tab）；
  2. 随机推荐：刷新后 ≥1 张卡片（MovieCard）；
  3. 👍 投票落库（toggle 语义）且卡片状态更新；
  4. 点选卡片 → 下方相似推荐刷新 ≥1 张卡片；
  5. ③ 作品明细：悬停信号挂载（itemEntered）、双击播放方法可用；
  6. 「高频标题词」维度的 (v1.1.4 新增) 文字已清理。
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QTabWidget  # noqa: E402

from nfo_profiler.gui import MainWindow, MovieCard  # noqa: E402


def wait_workers(win: MainWindow, app: QApplication, timeout: float = 180.0) -> None:
    deadline = time.time() + timeout
    while win._workers and time.time() < deadline:
        app.processEvents()
        time.sleep(0.1)
    app.processEvents()


def main() -> int:
    db = os.path.join("output", "nfo.db")
    if "--db" in sys.argv:
        db = sys.argv[sys.argv.index("--db") + 1]
    app = QApplication([])
    win = MainWindow(db_path=db)
    win.show()
    # 网络盘上的缩略图 IO 在 CI 环境可能阻塞数分钟（SMB 超时），
    # 冒烟只验证 UI 逻辑，跳过真实图片加载。
    win.find_movie_image = lambda p: None  # type: ignore[method-assign]
    print(f"[1] 窗口构建 OK：{win.windowTitle()}，db={db}")

    # --- Tab 顺序 ---
    tabs = win.centralWidget()
    assert isinstance(tabs, QTabWidget) and tabs.count() == 8, \
        f"主 Tab 应为 8 个，实际 {tabs.count()}"
    names = [tabs.tabText(i) for i in range(tabs.count())]
    print(f"[2] Tab 顺序 OK：{names}")
    assert names[3] == "④ 作品推荐", "第 4 个 Tab 应为作品推荐"
    assert names[4] == "⑤ 重复检测", "第 5 个 Tab 应为重复检测"

    # --- 随机推荐 ---
    wait_workers(win, app, timeout=60)  # 等 QTimer.singleShot(200) 触发的首刷
    if not getattr(win, "rec_random_cards", []):
        win.recommend_refresh()
        wait_workers(win, app)
    n_cards = len(win.rec_random_cards)
    print(f"[3] 随机推荐 OK：{n_cards} 张卡片，{win.lbl_rec_stat.text()}")
    assert n_cards > 0, "随机推荐应有卡片"
    card0 = win.rec_random_cards[0]
    assert isinstance(card0, MovieCard)
    assert card0.movie.get("movie_id"), "卡片应携带 movie_id"
    assert card0.movie.get("path"), "卡片应携带 path"
    print(f"    首卡片：{card0.movie['num']} → {card0.movie['title'][:30]}")

    # --- 投票（toggle） ---
    mid = card0.movie["movie_id"]
    num = card0.movie.get("num") or ""
    before = win.store.get_vote(mid)
    win._on_recommend_card_vote(card0.movie, +1)
    app.processEvents()
    after = win.store.get_vote(mid)
    print(f"[4] 投票 OK：{num} {before} → {after}（+1 应为 1）")
    assert after == 1, f"投票后应为 1，实际 {after}"
    # toggle 撤销
    win._on_recommend_card_vote(card0.movie, +1)
    app.processEvents()
    assert win.store.get_vote(mid) == 0, "再次投票应撤销"
    print("    toggle 撤销 OK（vote 回到 0）")
    # 恢复一个 👍 便于后续验证
    win._on_recommend_card_vote(card0.movie, +1)
    app.processEvents()

    # --- 相似推荐 ---
    win._on_recommend_card_selected(card0.movie)
    wait_workers(win, app)
    n_sim = len(getattr(win, "rec_similar_cards", []))
    print(f"[5] 相似推荐 OK：{n_sim} 张卡片")
    assert n_sim > 0, "相似推荐应有卡片"

    # --- 作品明细：悬停 / 双击播放 挂载 ---
    assert win.detail_table.hasMouseTracking(), "明细表应开启 mouseTracking"
    win.ed_search.setText("")
    win.search_movies(0)
    wait_workers(win, app)
    assert win.detail_table.rowCount() > 0, "明细检索应有数据"
    first = win.detail_table.item(0, 0)
    nfo = first.data(Qt.ItemDataRole.UserRole)
    # 悬停方法（找不到图也应安全）
    win._on_detail_item_entered(first)
    app.processEvents()
    # 双击 → 播放方法（内部找视频 / fallback 文件夹）
    win._on_detail_double_clicked(first)
    app.processEvents()
    # 找视频方法
    video = win._find_movie_video(nfo)
    print(f"[6] 悬停 / 双击播放 OK：video={'有' if video else '无（fallback 打开文件夹）'}")
    print("    状态栏：" + win.status_label.text()[:60])

    # --- (v1.1.4 新增) 文字清理 ---
    dims = [n for n, _ in MainWindow._dims()]
    assert "高频标题词" in dims and not any("v1.1.4" in d for d in dims), \
        "维度名应已清理版本标注"
    print("[7] 维度文字清理 OK（无 v1.1.4 标注）")

    print("\n[v1.2.0 冒烟检查全部通过]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
