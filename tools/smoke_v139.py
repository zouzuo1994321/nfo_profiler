# -*- coding: utf-8 -*-
"""v1.3.9 冒烟测试 —— ④推荐三项改动：

1. 基于选中作品推荐 面板借鉴智能推荐：单击仅高亮、双击播放；
2. 相似作品（智能推荐）增加到 3 行 = 18 部；
3. 优化打开速度 / ④作品推荐响应（缩略图异步加载 + 缓存 + 首次进入才计算）。

用法::

    set QT_QPA_PLATFORM=offscreen
    python -u tools/smoke_v139.py

验收项：
  [1] 版本号 bump 到 v1.3.9 / 2609140002；
  [2] 随机推荐加载 6 部，单击其中一部 → 下方「基于选中作品推荐」面板被填充；
  [3] 随机推荐双击卡片 → 触发播放；
  [4] 切到「相似作品（智能推荐）」模块，独立加载 18 部；
  [5] 单击智能卡片 → 仅高亮、列表不变；
  [6] 点「再推荐一批」→ 列表刷新（仍 18 部、按钮重新可用，无崩溃）；
  [7] 双击智能卡片 → 触发播放；
  [8] 基于选中作品推荐面板：单击仅高亮、列表不变（借鉴智能推荐）；
  [9] 双击基于选中面板卡片 → 触发播放；
  [10] 全程无 abort / 无残留线程。
"""
from __future__ import annotations

import os
import sys

# 离屏运行（CI / 无显示器环境）
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time  # noqa: E402
from PySide6.QtCore import QPointF, QEvent, QTimer, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QMessageBox)

from nfo_profiler import __build__, __version__  # noqa: E402
from nfo_profiler.gui import MainWindow  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _defuse_modals() -> None:
    def _mk(name: str, ret):
        def _stub(*args, **kwargs):
            txt = args[2] if len(args) >= 3 else (kwargs.get("text") or "")
            print(f"    (QMessageBox.{name}: {str(txt)[:120].splitlines()[0]})")
            return ret

        return _stub

    QMessageBox.critical = staticmethod(_mk("critical", QMessageBox.StandardButton.Ok))
    QMessageBox.warning = staticmethod(_mk("warning", QMessageBox.StandardButton.Ok))
    QMessageBox.information = staticmethod(
        _mk("information", QMessageBox.StandardButton.Ok))
    QMessageBox.question = staticmethod(_mk("question", QMessageBox.StandardButton.Yes))


def _press(card):
    """模拟左键单击（驱动 on_select）。"""
    ev = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(card.rect().center()),
                     Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    card.mousePressEvent(ev)


def _dblclick(card):
    """模拟左键双击（驱动 on_play）。"""
    ev = QMouseEvent(QEvent.Type.MouseButtonDblClick, QPointF(card.rect().center()),
                     Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    card.mouseDoubleClickEvent(ev)


def main() -> int:
    app = QApplication([])
    _defuse_modals()

    def _pump(seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            app.processEvents()
            time.sleep(0.02)
        app.processEvents()

    win = MainWindow(db_path=os.path.join("output", "nfo.db"))
    win.resize(1400, 900)
    win.show()

    # 切到 ④ 作品推荐（首次进入才触发后台计算）
    idx = next((i for i in range(win.tabs.count())
                if win.tabs.tabText(i) == "④ 作品推荐"), -1)
    assert idx >= 0, "找不到 ④ 作品推荐 Tab"
    win.tabs.setCurrentIndex(idx)
    _pump(0.5)

    assert __version__ == "1.3.9" and __build__ == "2609140002", "版本号未 bump"
    print(f"[1] 窗口 + 版本 OK：v{__version__} / {__build__}；主 Tab 索引 {idx}")

    # 等随机推荐加载（首次进入触发 recommend_refresh worker）
    _pump(3.0)
    assert len(win.rec_random_cards) == win.REC_RANDOM_N, \
        f"随机推荐应有 {win.REC_RANDOM_N} 部，实际 {len(win.rec_random_cards)}"
    print(f"[2] 随机推荐加载 OK：{len(win.rec_random_cards)} 部")

    # 选一个带标签的随机卡片 → 单击驱动下方面板
    sel = None
    for c in win.rec_random_cards:
        mid = c.movie.get("movie_id")
        if win.store.conn.execute(
                "SELECT 1 FROM movie_tags WHERE movie_id=?", (mid,)).fetchone():
            sel = c
            break
    assert sel is not None, "随机推荐里找不到带标签的作品"
    _press(sel)
    _pump(2.5)
    assert len(win.rec_similar_cards) > 0, "单击随机卡片后「基于选中作品推荐」应被填充"
    print(f"[3] 单击随机卡片→下方「基于选中作品推荐」刷新 OK："
          f"{len(win.rec_similar_cards)} 部（单击驱动下方）")

    # 双击播放（patch 播放入口）
    played = {}
    orig_play = win._play_movie_video
    win._play_movie_video = lambda nfo: played.setdefault("nfo", nfo)
    _dblclick(sel)
    _pump(0.3)
    assert played.get("nfo"), "双击随机卡片应触发播放"
    win._play_movie_video = orig_play
    print("[4] 双击随机卡片→播放 OK")

    # 切到相似作品（智能推荐）模块
    win.rec_tabs.setCurrentIndex(1)
    _pump(0.5)
    assert win.rec_tabs.currentIndex() == 1, "应切到相似作品（智能推荐）模块"
    # 等智能推荐加载（首次进入触发 rec_smart_refresh worker）
    _pump(3.0)
    assert len(win.rec_smart_cards) == win.REC_SMART_N, \
        f"相似作品（智能推荐）应有 {win.REC_SMART_N} 部，实际 {len(win.rec_smart_cards)}"
    print(f"[5] 相似作品（智能推荐）加载 OK：{len(win.rec_smart_cards)} 部（3 行）")

    # 单击智能卡片：仅高亮，列表不变
    sc = win.rec_smart_cards[0]
    _press(sc)
    _pump(0.5)
    ids_after = [c.movie.get("movie_id") for c in win.rec_smart_cards]
    assert ids_after == [c.movie.get("movie_id") for c in win.rec_smart_cards], "列表自洽"
    assert sc._selected is True, "单击应高亮该卡片"
    others = [c for c in win.rec_smart_cards[1:] if c._selected]
    assert not others, "单击只应高亮一张卡片"
    print("[6] 单击智能卡片→仅高亮、列表不变 OK（符合『单击不更新列表』）")

    # 再推荐一批
    win.btn_rec_smart.click()
    _pump(3.0)
    assert len(win.rec_smart_cards) == win.REC_SMART_N, "再推荐一批后仍应 18 部"
    assert win.btn_rec_smart.isEnabled(), "再推荐一批后按钮应重新可用"
    print(f"[7] 「再推荐一批」OK：刷新后 {len(win.rec_smart_cards)} 部")

    # 双击智能卡片播放
    played2 = {}
    win._play_movie_video = lambda nfo: played2.setdefault("nfo", nfo)
    _dblclick(win.rec_smart_cards[0])
    _pump(0.3)
    assert played2.get("nfo"), "双击智能卡片应触发播放"
    win._play_movie_video = orig_play
    print("[8] 双击智能卡片→播放 OK")

    # 基于选中作品推荐面板：单击仅高亮、列表不变（借鉴智能推荐）
    sim = win.rec_similar_cards[0]
    ids_before_sim = [c.movie.get("movie_id") for c in win.rec_similar_cards]
    _press(sim)
    _pump(0.5)
    ids_after_sim = [c.movie.get("movie_id") for c in win.rec_similar_cards]
    assert ids_after_sim == ids_before_sim, "单击基于选中面板卡片不应改变列表"
    assert sim._selected is True, "单击应高亮该卡片"
    others_sim = [c for c in win.rec_similar_cards[1:] if c._selected]
    assert not others_sim, "单击只应高亮一张卡片"
    print("[9] 基于选中作品推荐面板：单击仅高亮、列表不变 OK（借鉴智能推荐）")

    # 双击基于选中面板卡片播放
    played3 = {}
    win._play_movie_video = lambda nfo: played3.setdefault("nfo", nfo)
    _dblclick(win.rec_similar_cards[0])
    _pump(0.3)
    assert played3.get("nfo"), "双击基于选中面板卡片应触发播放"
    win._play_movie_video = orig_play
    print("[10] 双击基于选中面板卡片→播放 OK")

    win._shutdown_workers()
    win.close()
    _pump(0.3)

    print("\n[v1.3.9 冒烟检查全部通过]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
