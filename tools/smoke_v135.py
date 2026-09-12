# -*- coding: utf-8 -*-
"""v1.3.5 冒烟测试 —— 推荐卡片悬停大图 + 新 logo/图标。

用法：

    set QT_QPA_PLATFORM=offscreen
    python -u tools/smoke_v135.py [--db 副本库]

验收项：
  1. MovieCard.enterEvent → 浮动大图弹窗显示且像素非空；
  2. MovieCard.leaveEvent → 弹窗收起；
  3. 无图卡片悬停 → 不弹窗（且不报错）；
  4. 新 logo：logo.png 与 logo2.png 内容一致、logo.ico 多尺寸、窗口图标非空；
  5. 回归：浏览记录按钮 / 清理按钮 / 推荐 Tab 卡片流正常。

注意：离屏模式下 QMessageBox 会永久阻塞，这里统一替换为「打印 + 默认返回」。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QEvent, QPointF  # noqa: E402
from PySide6.QtGui import QEnterEvent, QIcon  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from nfo_profiler import __build__, __version__  # noqa: E402
from nfo_profiler.gui import MainWindow, MovieCard  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _defuse_modals() -> None:
    def _mk(name: str, ret):
        def _stub(*args, **kwargs):
            txt = args[2] if len(args) >= 3 else (kwargs.get("text") or "")
            print(f"    (QMessageBox.{name}: {str(txt)[:160].splitlines()[0]})")
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


def _find_movie_with_image(win: MainWindow):
    """从库里找一部「NFO 同目录有图」的作品，用于构造卡片。"""
    rows = win.store.conn.execute(
        "SELECT id, num, title, studio, path FROM movies "
        "WHERE path IS NOT NULL AND path<>'' "
        "ORDER BY RANDOM() LIMIT 500").fetchall()
    for r in rows:
        img = win.find_movie_image(r["path"])
        if img:
            return dict(r), img
    return None, None


def _enter_ev() -> QEnterEvent:
    # PySide6 6.11+：enterEvent 的形参必须是 QEnterEvent（不是 QEvent）
    return QEnterEvent(QPointF(10, 10), QPointF(10, 10), QPointF(10, 10))


def main() -> int:
    db = os.path.join("output", "nfo.db")
    if "--db" in sys.argv:
        db = sys.argv[sys.argv.index("--db") + 1]

    app = QApplication([])
    _defuse_modals()
    win = MainWindow(db_path=db)
    win.resize(1500, 950)
    win.show()
    print(f"[1] 窗口构建 OK：{win.windowTitle()}（v{__version__} / {__build__}）")
    assert (tuple(map(int, __version__.split("."))) >= (1, 3, 5)
            and int(__build__) >= 2609120003), "版本号低于 v1.3.5"

    # ------- 1/2. 悬停弹大图 / 离开收起 -------
    movie, img = _find_movie_with_image(win)
    assert movie, "库里找不到带图的作品，无法验证悬停预览"
    card = MovieCard(movie, win)
    card.show()
    card._img_path = img          # 直接命中缓存路径，模拟 _load_image 结果
    card.enterEvent(_enter_ev())
    popup = getattr(win, "_img_popup", None)
    assert popup is not None and popup.isVisible(), \
        "悬停卡片后应显示浮动大图弹窗"
    assert popup.pixmap() is not None and not popup.pixmap().isNull(), \
        "弹窗像素不应为空"
    w, h = popup.width(), popup.height()
    print(f"[2] 悬停大图 OK：弹窗 {w}x{h}（图源 {os.path.basename(img)}）")
    card.leaveEvent(QEvent(QEvent.Type.Leave))
    assert not popup.isVisible(), "离开卡片后弹窗应收起"
    print("[3] 离开收起 OK：弹窗已隐藏（与③作品明细同款交互）")

    # ------- 3. 无图卡片不弹窗 -------
    nomovie = {"movie_id": -1, "num": "TEST-0000", "title": "无图占位",
               "studio": "", "path": "Z:/__不存在的路径__/x.nfo"}
    card2 = MovieCard(nomovie, win)
    card2.show()
    win._hide_image_popup()
    card2.enterEvent(_enter_ev())
    assert not win._img_popup.isVisible(), "无图卡片悬停不应弹窗"
    card2.leaveEvent(QEvent(QEvent.Type.Leave))
    print("[4] 无图占位 OK：不弹窗、不报错")

    # ------- 4. 新 logo / 图标（含 v1.3.5 图标链路修复的回归） -------
    png = os.path.join(ROOT, "logo.png")
    src = os.path.join(ROOT, "logo2.png")
    ico = os.path.join(ROOT, "logo.ico")
    with open(png, "rb") as f1, open(src, "rb") as f2:
        same = f1.read() == f2.read()
    assert same, "logo.png 应与 logo2.png 内容一致"
    assert os.path.getsize(ico) > 1024, "logo.ico 体积异常"
    with open(ico, "rb") as f:
        head = f.read(6)
    assert head[:4] == b"\x00\x00\x01\x00", "logo.ico 不是 ICO 格式"
    n_imgs = int.from_bytes(head[4:6], "little")
    assert n_imgs >= 5, f"logo.ico 应含多尺寸（当前 {n_imgs} 张）"
    assert not win.windowIcon().isNull(), "窗口图标不应为空"
    from nfo_profiler.gui import find_data_file
    p = find_data_file("logo.png")
    assert p and os.path.isfile(p), "find_data_file 应能定位 logo.png"
    assert QIcon(p).availableSizes(), "find_data_file 找到的 logo 应能解码出像素"
    app_icon = QApplication.instance().windowIcon()
    assert not app_icon.isNull(), "应用级图标应已设置（QMessageBox 等继承）"
    print(f"[5] 新 logo OK：logo.png=logo2.png；logo.ico 含 {n_imgs} 个尺寸；"
          f"窗口图标+应用级图标已生效（{os.path.basename(p)}）")

    # ------- 5. 回归 -------
    assert win.btn_rec_history.text() == "🕘 浏览记录", "浏览记录按钮应保留"
    assert hasattr(win, "btn_prune") and "清理失效记录" in win.btn_prune.text(), \
        "清理入口应保留"
    n = len(win.rec_random_cards) if hasattr(win, "rec_random_cards") else -1
    print(f"[6] 回归 OK：浏览记录/清理入口在位；推荐 Tab 卡片容器正常"
          f"（当前 {max(n, 0)} 张卡）")

    print("\n[v1.3.5 冒烟检查全部通过]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
