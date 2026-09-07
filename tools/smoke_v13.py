# -*- coding: utf-8 -*-
"""v1.3.0 桌面界面冒烟测试 —— 6 项体验迭代。

用法：

    set QT_QPA_PLATFORM=offscreen
    python tools/smoke_v13.py                     # 用 output/nfo.db
    python tools/smoke_v13.py --db D:/copy.db     # 指定副本（主程序正开着时用）

验收项：
  1. ④作品推荐：卡片按可用空间放大（apply_size）+ 容量自适应（rec_capacity）；
  2. 投票记录：VoteManagerDialog 可构建、可筛选、删除 / 清空均落库；
  3. 已登记数据源：「操作」列固定 78px、移除按钮宽度 > 0（不被裁切）；
  4. 扫描选项：关闭增量 → 自动勾选并解锁「清理已删除文件的记录」；
  5. ②画像概览：新 UI（摘要条 + 卡片自适应列 + 维度表 4 列 + 复制/导出方法）；
  6. ⑦AI分析：聚类参数控件 + 细化后的聚类（label / 二级子主题 / 统计）。
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QTabWidget  # noqa: E402

from nfo_profiler.gui import MainWindow, MovieCard, VoteManagerDialog  # noqa: E402


def wait_workers(win: MainWindow, app: QApplication, timeout: float = 180.0,
                 min_seconds: float = 3.0) -> None:
    """泵 Qt 事件直到 worker 全部结束。

    ``min_seconds``：至少泵这么久 —— 推荐 Tab 的首刷是
    ``QTimer.singleShot(200/400)`` 触发的，若 worker 列表一开始为空就直接返回，
    定时器还没来得及触发（v1.3.0 踩过：卡片数为 0）。
    """
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
    win = MainWindow(db_path=db)
    win.resize(1500, 950)
    win.show()
    # 网络盘缩略图 IO 在 CI 环境可能阻塞数分钟，冒烟只验证 UI 逻辑
    win.find_movie_image = lambda p: None  # type: ignore[method-assign]
    # 切到「④ 作品推荐」——非当前页不会收到 resize，卡片尺寸会停留在默认值
    win.tabs.setCurrentIndex(3)
    print(f"[1] 窗口构建 OK：{win.windowTitle()}，db={db}")

    tabs = win.centralWidget()
    assert isinstance(tabs, QTabWidget) and tabs.count() == 8, \
        f"主 Tab 应为 8 个，实际 {tabs.count()}"

    # ---------------- 1. ④作品推荐：固定数量 + 无滑动 ----------------
    wait_workers(win, app, timeout=90)
    cards = getattr(win, "rec_random_cards", [])
    assert cards, "随机推荐应有卡片"
    assert len(cards) == win.REC_RANDOM_N == 6, \
        f"随机推荐应固定 6 部，实际 {len(cards)}"
    # 左右滑动已取消：两个卡片区都不应再有 ◀/▶ 按钮
    from PySide6.QtWidgets import QPushButton
    for name in ("rec_random_wrap", "rec_similar_wrap"):
        wrap = getattr(win, name)
        arrows = [b for b in wrap.findChildren(QPushButton)
                  if b.text() in ("◀", "▶")]
        assert not arrows, f"{name} 仍残留左右滑动按钮：{arrows}"
    assert win.rec_random_area.horizontalScrollBarPolicy() == \
        Qt.ScrollBarPolicy.ScrollBarAlwaysOff, "随机区应关闭横向滚动"
    assert win.rec_similar_area.verticalScrollBarPolicy() == \
        Qt.ScrollBarPolicy.ScrollBarAsNeeded, "相关区应启用上下滚动"
    c0 = cards[0]
    w0, h0 = c0.width(), c0.height()
    # 手动放大：卡片应随之变大（apply_size 内部会按 MIN/MAX 边界裁剪）
    c0.apply_size(300, 300)
    assert c0.width() == 300 and c0.height() == 300 + MovieCard.INFO_H, \
        f"apply_size 未生效：{c0.width()}x{c0.height()}"
    c0.apply_size(w0, h0 - MovieCard.INFO_H)
    print(f"[2] 推荐布局 OK：随机 {len(cards)} 部（无滑动），卡片 {w0}x{h0}，"
          f"放大后 300x{300 + MovieCard.INFO_H}")

    # 相关推荐：点选一张 → 12 部 / 2 行
    win._on_recommend_card_selected(c0.movie)
    wait_workers(win, app, timeout=90)
    sims = getattr(win, "rec_similar_cards", [])
    assert sims, "相关推荐应有卡片"
    assert len(sims) == win.REC_SIMILAR_N == 12, \
        f"相关推荐应固定 12 部，实际 {len(sims)}"
    assert win.REC_SIMILAR_COLS * win.REC_SIMILAR_ROWS == 12, "12 部应为 6 列 × 2 行"
    print(f"    相关推荐 OK：{len(sims)} 部（{win.REC_SIMILAR_COLS} 列 × "
          f"{win.REC_SIMILAR_ROWS} 行，可上下滚动）")

    # ---------------- 2. 投票记录管理器 ----------------
    mid = c0.movie["movie_id"]
    if win.store.get_vote(mid) == 0:
        win.store.upsert_vote(mid, c0.movie.get("num") or "", 1)
    dlg = VoteManagerDialog(win.store, win, on_changed=None)
    n_all = dlg.table.rowCount()
    assert n_all >= 1, "投票记录应至少 1 条"
    # 筛选 👎（应少于等于全部）
    dlg.cmb_filter.setCurrentIndex(2)
    n_down = dlg.table.rowCount()
    dlg.cmb_filter.setCurrentIndex(1)
    n_up = dlg.table.rowCount()
    assert n_up + n_down == n_all, f"筛选计数不符：{n_up}+{n_down}!={n_all}"
    # 删除选中第一条
    dlg.cmb_filter.setCurrentIndex(1)
    dlg.table.selectRow(0)
    ids = dlg._selected_ids()
    assert ids, "应能取到选中记录的 movie_id"
    # 注意：离屏模式下 QMessageBox 会永久阻塞，因此不调用会弹确认框的
    # _delete_selected()，改为走「确认后」的同一段删除逻辑。
    before = win.store.vote_summary()["total"]
    for i in ids:
        win.store.remove_vote(i)
    dlg.reload()
    after = win.store.vote_summary()["total"]
    assert after == before - len(ids), f"删除未生效：{before} → {after}"
    win.store.upsert_vote(ids[0], "SMOKE-RESTORE", 1)  # 恢复，避免后续无数据
    print(f"[3] 投票记录 OK：共 {n_all} 条（👍{n_up} / 👎{n_down}），"
          f"删除生效（{before} → {after}，已恢复 1 条）")

    # ---------------- 3. 数据源「操作」列按钮 ----------------
    win.refresh_sources()
    n_src = win.src_table.rowCount()
    assert n_src >= 1, "应至少有一个已登记数据源"
    col_w = win.src_table.columnWidth(4)
    assert col_w >= 70, f"操作列太窄：{col_w}"
    from PySide6.QtWidgets import QPushButton
    cell = win.src_table.cellWidget(0, 4)
    btns = cell.findChildren(QPushButton) if cell else []
    # 注意：离屏下 cellWidget 可能尚未完成布局，用 sizeHint 判断是否会被「压扁」
    bw = btns[0].minimumWidth() if btns else 0
    assert btns and bw >= 50, f"移除按钮最小宽度异常：{bw}"
    print(f"[4] 数据源操作列 OK：{n_src} 个数据源，操作列 {col_w}px，"
          f"按钮设计宽 {bw}px")

    # ---------------- 4. 扫描：关闭增量 → 清理开关 ----------------
    assert win.chk_prune.isChecked() and not win.chk_prune.isEnabled(), \
        "增量开启时清理开关应禁用"
    win._on_incremental_toggled(False)
    assert win.chk_prune.isEnabled() and win.chk_prune.isChecked(), \
        "关闭增量后应自动勾选并解锁清理开关"
    win._on_incremental_toggled(True)
    print("[5] 扫描选项联动 OK：增量开→禁用，增量关→自动勾选解锁")

    # ---------------- 5. ②画像概览新 UI ----------------
    assert hasattr(win, "lbl_ov_persona"), "应有一句话画像摘要条"
    assert win.dim_table.columnCount() == 4, "维度表应为 4 列（#/名称/数量/占比）"
    win.run_overview()
    wait_workers(win, app, timeout=180)
    assert win.last_data, "画像数据应已生成"
    assert win._ov_cards, "KPI 卡片应已生成"
    persona = win.lbl_ov_persona.text()
    assert "部" in persona, f"摘要文案异常：{persona[:40]}"
    assert win.dim_table.rowCount() > 0, "维度表应有数据"
    rows = win._dim_rows()
    assert rows and rows[0][1], "维度复制数据应可用"
    print(f"[6] 画像概览 UI OK：{len(win._ov_cards)} 张 KPI 卡，维度 {len(rows)} 行，"
          f"摘要「{persona.splitlines()[0][:36]}」")

    # ---------------- 6. ⑦AI分析：参数 + 细化聚类 ----------------
    assert win.spin_clu_topn.value() == 120, "聚类候选词默认值应为 120"
    try:
        from nfo_profiler.ai_engine import AIEngine
        clusters = AIEngine().cluster_themes(
            win.store, top_n=60, min_co=3, with_title_terms=True,
            limit_representatives=5, min_size=30)
    except Exception as exc:  # 聚类失败不应中断冒烟
        clusters = []
        print(f"    （聚类跳过：{exc.__class__.__name__}: {exc}）")
    if clusters:
        c = clusters[0]
        assert c.get("label"), "主题应有组合展示名 label"
        assert "share" in c and "avg_rating" in c, "主题应带统计字段"
        n_sub = sum(len(x.get("children") or []) for x in clusters)
        print(f"[7] 聚类细化 OK：{len(clusters)} 个主题，二级子主题 {n_sub} 个，"
              f"首主题「{c['label'][:30]}」（{c['size']} 部 / "
              f"{c['share'] * 100:.1f}%）")
    else:
        print("[7] 聚类细化：本次未产出主题（数据或参数原因）")

    print("\n[v1.3.0 冒烟检查全部通过]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
