# -*- coding: utf-8 -*-
"""v1.3.2 冒烟测试 —— 删除「查看记录」入口 + 新增「浏览记录」。

用法：

    set QT_QPA_PLATFORM=offscreen
    python tools/smoke_v132.py --db D:/copy.db   # 务必用副本库

验收项：
  1. 「📑查看记录」入口已删除：统计标签恢复纯文本（无手型光标、不装事件过滤器、
     文案不含「查看记录」、eventFilter 不再拦截点击）；
  2. 工具条新增「🕘 浏览记录」按钮 + open_browse_history 可构建对话框；
  3. store.record_play_by_path：首播 play_count=1、重复双击更新时间且 +1、
     库外路径返回 None、play_records JOIN 出标题/投票；
  4. 双击播放挂钩：_on_recommend_card_play / _play_movie_video 会写入浏览记录；
  5. BrowseHistoryDialog：行内 👍/👎 直接投票（toggle 撤销）、删除/清空只删浏览痕迹、
     统计文案正确。
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QDesktopServices, QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from nfo_profiler.gui import BrowseHistoryDialog, MainWindow  # noqa: E402


def _defuse_modals() -> None:
    """离屏模式下 QMessageBox.* 会永久阻塞（等一个永远不来的点击）。

    统一替换为「打印 + 返回默认按钮」：既防挂死，又能把 worker 的真实异常
    （走 _show_error 弹窗的那条路）直接暴露到冒烟日志里。
    """
    def _mk(name: str, ret):
        def _stub(*args, **kwargs):
            txt = args[2] if len(args) >= 3 else (kwargs.get("text") or "")
            print(f"!!! QMessageBox.{name}: {str(txt)[:300].splitlines()[0]}")
            return ret
        return _stub

    QMessageBox.critical = staticmethod(   # type: ignore[assignment]
        _mk("critical", QMessageBox.StandardButton.Ok))
    QMessageBox.warning = staticmethod(    # type: ignore[assignment]
        _mk("warning", QMessageBox.StandardButton.Ok))
    QMessageBox.information = staticmethod(  # type: ignore[assignment]
        _mk("information", QMessageBox.StandardButton.Ok))
    QMessageBox.question = staticmethod(   # type: ignore[assignment]
        _mk("question", QMessageBox.StandardButton.Yes))


def wait_workers(win: MainWindow, app: QApplication, timeout: float = 120.0,
                 min_seconds: float = 3.0) -> None:
    """泵 Qt 事件直到 worker 全部结束（首刷是 singleShot 触发的，需最少泵 3s）。"""
    end = time.time() + max(min_seconds, 0.0)
    deadline = max(time.time() + timeout, end)
    while time.time() < end or (win._workers and time.time() < deadline):
        app.processEvents()
        time.sleep(0.1)
    app.processEvents()


def main() -> int:
    db = os.path.join("output", "nfo.db")
    if "--db" in sys.argv:
        db = sys.argv[sys.argv.index("--db") + 1]

    app = QApplication([])
    _defuse_modals()
    win = MainWindow(db_path=db)
    win.resize(1500, 950)
    win.show()
    win.find_movie_image = lambda p: None  # type: ignore[method-assign]
    win.tabs.setCurrentIndex(3)   # ④ 作品推荐
    print(f"[1] 窗口构建 OK：{win.windowTitle()}，db={db}")

    # ------- 1. 「查看记录」入口已删除 -------
    assert hasattr(win, "lbl_rec_stat"), "统计标签应保留（纯展示）"
    assert win.lbl_rec_stat.cursor().shape() != Qt.CursorShape.PointingHandCursor, \
        "统计标签不应再是手型光标"
    assert not hasattr(win, "btn_rec_votes") or win.btn_rec_votes.text() == "📑 投票记录"
    wait_workers(win, app, timeout=90)
    stat_text = win.lbl_rec_stat.text()
    assert "查看记录" not in stat_text, f"统计文案不应再含「查看记录」：{stat_text}"
    assert "📑" not in stat_text, f"统计文案不应再带📑后缀：{stat_text}"
    # eventFilter 不再拦截统计标签点击：构造一次鼠标按下事件喂给它
    press = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(1, 1), QPointF(1, 1),
                        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                        Qt.KeyboardModifier.NoModifier)
    consumed = win.eventFilter(win.lbl_rec_stat, press)
    assert not consumed, "点击统计标签不应再触发任何弹窗"
    print(f"[2] 查看记录入口已删除 OK：文案「{stat_text}」，点击不再拦截")

    # ------- 2. 工具条「🕘 浏览记录」按钮 -------
    assert win.btn_rec_history.text() == "🕘 浏览记录", "应新增浏览记录按钮"
    assert hasattr(win, "open_browse_history") and callable(win.open_browse_history)
    print("[3] 浏览记录按钮 OK")

    # ------- 3. store 浏览记录 CRUD -------
    store = win.store
    row = store.conn.execute(
        "SELECT id, num, path FROM movies WHERE path IS NOT NULL AND path<>'' LIMIT 1"
    ).fetchone()
    assert row is not None, "库内应有作品"
    mid, num, path = int(row["id"]), row["num"] or "", row["path"]
    store.clear_plays()
    r1 = store.record_play_by_path(path)
    assert r1 and r1["play_count"] == 1, f"首播应记 1 次：{r1}"
    r2 = store.record_play_by_path(path)
    assert r2 and r2["play_count"] == 2, f"重复双击应 +1：{r2}"
    assert store.record_play_by_path(r"Z:/__no_such__/x.nfo") is None, "库外路径应忽略"
    assert store.record_play_by_path("") is None, "空路径应忽略"
    recs = store.play_records()
    assert len(recs) == 1 and recs[0]["movie_id"] == mid, "同一作品只留一条最新记录"
    assert "title" in recs[0] and "vote" in recs[0], "play_records 应 JOIN 出标题/投票"
    print(f"[4] store 浏览记录 OK：{num} 播放 {r2['play_count']} 次，共 {len(recs)} 条")

    # ------- 4. 双击播放挂钩 -------
    wait_workers(win, app, timeout=90)
    cards = getattr(win, "rec_random_cards", [])
    assert cards, "随机推荐应有卡片"
    target = cards[0]
    tmid = target.movie["movie_id"]
    before = store.get_play_count(tmid)
    opened: list = []
    QDesktopServices.openUrl = staticmethod(  # type: ignore[assignment]
        lambda url: opened.append(url.toString()))  # 避免冒烟真启动播放器
    win._on_recommend_card_play(target.movie)   # 推荐卡片双击路径
    assert store.get_play_count(tmid) == before + 1, "双击推荐卡片应写入浏览记录"
    win._play_movie_video(target.movie["path"])  # 明细表双击走的同一底层函数
    assert store.get_play_count(tmid) == before + 2, "重复播放应再 +1"
    assert opened, "播放应触发 openUrl（已被 monkeypatch 拦截）"
    print(f"[5] 双击播放挂钩 OK：{target.movie.get('num')} 浏览次数 "
          f"{before} → {store.get_play_count(tmid)}")

    # ------- 5. BrowseHistoryDialog -------
    dlg = BrowseHistoryDialog(store, win, on_changed=None)
    assert dlg.table.columnCount() == 7, "浏览记录表应为 7 列"
    assert dlg.table.rowCount() >= 1, "浏览记录表应有数据"
    # 行内 👍 投票 → 落库；再点一次 → toggle 撤销
    dlg._cast_vote(tmid, target.movie.get("num") or "", +1)
    assert store.get_vote(tmid) == 1, "行内 👍 应落库"
    dlg._cast_vote(tmid, target.movie.get("num") or "", +1)
    assert store.get_vote(tmid) == 0, "重复点 👍 应 toggle 撤销"
    dlg._cast_vote(tmid, target.movie.get("num") or "", -1)
    assert store.get_vote(tmid) == -1, "行内 👎 应落库"
    dlg.reload()
    head = dlg.table.item(0, 5).text()
    assert head in ("👍 已喜欢", "👎 不喜欢", "未投"), f"投票列文案异常：{head}"
    # 删除：只删浏览痕迹，不动投票
    dlg.table.selectRow(0)
    ids = dlg._selected_ids()
    assert ids == [tmid], f"应选中该行：{ids}"
    store.remove_play(tmid)
    dlg.reload()
    assert store.get_vote(tmid) == -1, "删除浏览记录不应影响投票"
    n_rows = dlg.table.rowCount()
    # 清空逻辑走「确认后」同段代码（离屏不能弹 QMessageBox）
    store.clear_plays()
    dlg.reload()
    assert dlg.table.rowCount() == 0, "清空后表格应为空"
    store.remove_vote(tmid)   # 还原冒烟期间产生的投票
    print(f"[6] 浏览记录对话框 OK：7 列 / {n_rows} 条，行内👍👎可投可撤，"
          f"删除仅删痕迹（投票保留）")

    print("\n[v1.3.2 冒烟检查全部通过]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
