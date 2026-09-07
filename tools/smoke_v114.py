# -*- coding: utf-8 -*-
"""v1.1.4 桌面界面冒烟测试 —— 高频标题词 + 综合主题聚类 + AI 开关。

用法：

    set QT_QPA_PLATFORM=offscreen
    python tools/smoke_v114.py

验收项：
  1. ① 画像概览 —— "高频标题词" 维度有数据，KPI 卡有"标题词数"；
  2. ⑥ AI 分析：
     - AI 引擎开关（开启 / 关闭 / 自检）按钮可点击，状态条文案更新；
     - 综合主题聚类（cluster_themes）填出 N 个主题（带 members/title_terms/representatives）；
     - 代表作品节点 UserRole 携带 path，双击能调到 _on_cluster_tree_double_clicked。
  3. 旧流程（重复检测 / 明细 / AI 相似 / 解读）不破坏。
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from nfo_profiler.gui import MainWindow  # noqa: E402


def wait_workers(win: MainWindow, app: QApplication, timeout: float = 180.0) -> None:
    deadline = time.time() + timeout
    while win._workers and time.time() < deadline:
        app.processEvents()
        time.sleep(0.1)
    app.processEvents()


def main() -> int:
    app = QApplication([])
    win = MainWindow(db_path=os.path.join("output", "nfo.db"))
    win.show()
    print(f"[1] 窗口构建 OK：{win.windowTitle()}，"
          f"{win.lbl_scan_stat.text()}")

    # --- ② 画像概览：含「高频标题词」 ---
    win.cmb_ov_source.setCurrentIndex(0)
    win.run_overview()
    wait_workers(win, app)
    ov = (win.last_data or {}).get("overview", {})
    distinct = ov.get("distinct", {})
    assert "title_terms" in distinct, "KPI 缺标题词数"
    print(f"[2] 画像概览 KPI 新增「标题词数」：{distinct['title_terms']:,}")
    # 维度表能找到 title_terms
    win.cmb_dim.setCurrentIndex(0)  # 第一项 = 高频标题词
    app.processEvents()
    assert win.cmb_dim.currentText().startswith("高频标题词"), \
        "维度表第一项应是高频标题词"
    win._fill_dim_table()
    app.processEvents()
    n_rows = win.dim_table.rowCount()
    n_terms = len((win.last_data or {}).get("title_terms", []))
    print(f"[3] 高频标题词维度行数：{n_rows}（{n_terms} 项可用）")
    assert n_rows > 0, "高频标题词维度应有数据"

    # 抽样前 5 条展示
    print("    Top 词：", end="")
    top = (win.last_data or {}).get("title_terms", [])[:5]
    print(", ".join(f"{d['name']}({d['count']})" for d in top))

    # --- ⑥ AI 引擎开关 ---
    assert hasattr(win, "btn_ai_enable"), "缺 AI 开启按钮"
    assert hasattr(win, "btn_ai_disable"), "缺 AI 关闭按钮"
    assert hasattr(win, "btn_ai_redetect"), "缺 AI 自检按钮"
    assert win.btn_ai_enable.isEnabled(), "默认应能点开启"
    assert not win.btn_ai_disable.isEnabled(), "默认下关闭应灰"
    print("[4] AI 引擎按钮 OK：默认开启可点 / 关闭灰")
    # 真点一下
    win.btn_ai_enable.click()
    app.processEvents()
    # 单击后状态可能是 "initializing"
    print(f"    开启后状态：{win.lbl_ai_state.text()}")
    # 等探测完成（直接看 ai.status() 而不是 label，避免 polling 不及时）
    deadline = time.time() + 8
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.1)
        st = win._ai_engine().status()
        if st.get("state") == "ready":
            break
    # 强制刷一次 UI label
    win._refresh_ai_state()
    app.processEvents()
    st = win._ai_engine().status()
    print(f"    探测完成：backend={st.get('backend')}, state={st.get('state')}, "
          f"enabled={st.get('enabled')}, msg={st.get('message', '')[:50]}…")
    assert st.get("enabled") and st.get("state") == "ready", \
        f"AI 开启后未达到 ready 状态：{st}"
    print(f"    探测完成后 UI 显示：{win.lbl_ai_state.text()[:80]}")
    # 点关闭
    win.btn_ai_disable.click()
    app.processEvents()
    assert win.btn_ai_enable.isEnabled(), "关闭后开启按钮应可用"
    print("[5] AI 引擎开启 / 关闭切换 OK")

    # --- 综合主题聚类（v1.1.4 新结构） ---
    win.ai_clusters()
    wait_workers(win, app)
    clu_n = win.ai_clu_tree.topLevelItemCount()
    print(f"[6] 综合主题聚类 OK：{clu_n} 个主题（{win.lbl_ai_clu_summary.text()[:60]}…）")
    # 检查带 members/title_terms/representatives 的主题存在
    has_three = False
    found_rep = False
    for i in range(clu_n):
        top = win.ai_clu_tree.topLevelItem(i)
        kind_label = top.text(1)  # "种子" 或 "种子(标题词)"
        child_labels = [top.child(j).text(0) for j in range(top.childCount())]
        if any("关联标签" in c for c in child_labels) \
                and any("关联标题词" in c for c in child_labels) \
                and any("代表作品" in c for c in child_labels):
            has_three = True
            # 在代表作品下找 path
            for j in range(top.childCount()):
                rep = top.child(j)
                if "代表作品" in rep.text(0):
                    for k in range(rep.childCount()):
                        rep_item = rep.child(k)
                        path = rep_item.data(0, Qt.ItemDataRole.UserRole)
                        if path:
                            found_rep = True
    assert has_three, "至少有一个主题应同时含「关联标签 / 关联标题词 / 代表作品」三层"
    assert found_rep, "代表作品节点的 UserRole 应携带 path"
    print("[7] 新三层结构 OK：关联标签 + 关联标题词 + 代表作品（含 path）")

    # 双击代表作品节点（用 _on_cluster_tree_double_clicked 模拟）
    win._on_cluster_tree_double_clicked(None, 0)  # None item 应安全 early-return
    # 真找到第一个有 path 的代表作品子节点测双击
    target = None
    for i in range(clu_n):
        top = win.ai_clu_tree.topLevelItem(i)
        for j in range(top.childCount()):
            rep = top.child(j)
            if "代表作品" in rep.text(0):
                for k in range(rep.childCount()):
                    it = rep.child(k)
                    if it.data(0, Qt.ItemDataRole.UserRole):
                        target = it
                        break
                if target is not None:
                    break
        if target is not None:
            break
    assert target is not None, "找不到有 path 的代表作品节点"
    win._on_cluster_tree_double_clicked(target, 0)
    print("[8] 双击信号挂载 + 健壮性 OK")

    # --- AI 相似 + 解读回归（确保没破坏老流程） ---
    win.ed_ai_num.setText("DDK-173")
    win.ai_similar()
    wait_workers(win, app)
    sim_n = win.ai_similar_table.rowCount()
    print(f"[9] 回归 · 相似推荐：{sim_n} 行")
    win.ai_interpret()
    wait_workers(win, app)
    interp = win.ai_interp_browser.toPlainText().strip()
    print(f"[10] 回归 · 画像解读：{len(interp)} 字")
    assert sim_n > 0 and interp, "回归失败"

    print("\n[v1.1.4 冒烟检查全部通过]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
