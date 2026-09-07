# -*- coding: utf-8 -*-
"""桌面图形界面（PySide6）—— 双击 exe 直接用，**不依赖任何浏览器**。

v1.1.0 起，软件的主界面由「本地 HTTP + 浏览器」改为**原生 Qt 桌面窗口**：
启动即见窗口，扫描 / 统计 / 检索 / 重复检测 / 导出全部在窗口内完成，
不再需要浏览器、不再占用端口，也不存在「关掉页面后台还在跑」的困惑。

线程模型（与旧 Web 版一致的经验）：
  * 所有耗时操作（扫描、统计、检测、导出、AI）都放进 ``QThread`` 子线程，
    通过 Signal 回传进度与结果，主线程只负责刷新界面，绝不卡死；
  * SQLite 访问统一由 :class:`~nfo_profiler.store.Store` 的 RLock 串行化。

标签页：
  1. 数据源与扫描    —— 目录管理、增量 / 全量扫描、实时进度与日志
  2. 画像概览        —— 关键指标卡 + 任意维度 TopN
  3. 作品明细        —— 关键词检索 + 分页表格（表头排序 / 悬停缩略图 / 双击播放）
  4. 作品推荐        —— 随机卡片流 + 👍/👎 偏好反馈 + 相似推荐（v1.2.0）
  5. 重复检测        —— 跨目录重复影片识别（同目录分片自动排除）
  6. 导出与报告      —— CSV / JSON / XLSX / Markdown / HTML 报告
  7. AI 分析         —— 标签聚类、相似推荐、画像解读（离线优先）
  8. 关于与声明      —— 版本、版权与使用限制

::

    Copyright © 2026 肆月Aperture 本软件不得用于商业用途，仅做学习交流使用。
"""

from __future__ import annotations

import csv
import os
import sys
import tempfile
import threading
import traceback
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from PySide6.QtCore import QSize, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QBrush, QColor, QCursor, QDesktopServices, QFont, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QFileDialog,
    QFrame, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSpinBox, QSplitter,
    QStatusBar, QTableWidget, QTableWidgetItem, QTabWidget, QTextBrowser,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget, QDoubleSpinBox,
)

from . import (
    APP_NAME, COPYRIGHT_NOTICE, __author__, __build__, __version__, version_text,
)
from .normalize import Normalizer, resolve_config_paths
from .scanner import scan_paths
from .store import Store


# ---------------------------------------------------------------------------
# 路径 / 主题
# ---------------------------------------------------------------------------

def resource_dir() -> str:
    """打包资源目录（config/、logo.png 所在处）。"""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def app_dir() -> str:
    """程序目录（数据库与导出产物的根目录）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def default_db_path() -> str:
    return os.path.join(app_dir(), "output", "nfo.db")


def default_out_dir() -> str:
    return os.path.join(app_dir(), "output")


QSS = """
QWidget { background:#1b1e23; color:#dfe4ea;
          font-family:"Microsoft YaHei UI","Microsoft YaHei","Segoe UI",sans-serif; font-size:13px; }
QMainWindow::separator { background:#2c313a; width:1px; }
QTabWidget::pane { border:1px solid #2c313a; background:#20242b; top:-1px; }
QTabBar::tab { background:#262b33; color:#aeb6c2; padding:9px 20px; border:1px solid #2c313a; }
QTabBar::tab:selected { background:#2f6fb5; color:#ffffff; }
QTabBar::tab:hover:!selected { background:#303743; }
QGroupBox { border:1px solid #2c313a; border-radius:5px; margin-top:14px; padding:14px 10px 10px; }
QGroupBox::title { subcontrol-origin:margin; left:12px; padding:0 6px; color:#8ab4f8; font-weight:bold; }
QPushButton { background:#2b313b; border:1px solid #3a414d; padding:7px 16px; border-radius:4px; }
QPushButton:hover { background:#343b47; }
QPushButton:pressed { background:#262c35; }
QPushButton:disabled { color:#6b7280; background:#24282f; }
QPushButton[accent="true"] { background:#2f6fb5; border-color:#2f6fb5; color:#ffffff; font-weight:bold; }
QPushButton[accent="true"]:hover { background:#3a7ec7; }
QLineEdit, QSpinBox, QComboBox, QListWidget, QTreeWidget, QTableWidget, QTextBrowser, QPlainTextEdit {
    background:#171a1f; border:1px solid #2c313a; border-radius:4px; padding:5px; selection-background-color:#2f6fb5; }
QComboBox::drop-down { border:none; }
QHeaderView::section { background:#232832; color:#c8d0da; padding:7px; border:none;
                       border-right:1px solid #2c313a; border-bottom:1px solid #2c313a; }
QProgressBar { border:1px solid #2c313a; background:#171a1f; border-radius:4px; text-align:center; height:20px; }
QProgressBar::chunk { background:#2f6fb5; border-radius:3px; }
QTreeWidget::item, QTableWidget::item { padding:3px; }
QTreeWidget::item:selected, QTableWidget::item:selected { background:#2f6fb5; color:#ffffff; }
QStatusBar { background:#171a1f; color:#9aa4b2; }
QLabel[hint="true"] { color:#8b939f; }
QLabel[card="true"] { background:#22272f; border:1px solid #2c313a; border-radius:6px; padding:10px 14px; }
QLabel[num="true"] { color:#8ab4f8; font-size:20px; font-weight:bold; }
"""


def human_gb(n: int) -> str:
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        return "0"
    tb = n / 1099511627776.0
    if tb >= 1:
        return f"{tb:.2f} TB"
    return f"{n / 1073741824.0:.1f} GB"


# ---------------------------------------------------------------------------
# 后台线程
# ---------------------------------------------------------------------------

class _Worker(QThread):
    """通用后台任务：把耗时调用丢进子线程，用信号回传结果/异常。"""

    done = Signal(object)
    failed = Signal(str)

    def __init__(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        super().__init__()
        self._func, self._args, self._kwargs = func, args, kwargs

    def run(self) -> None:  # pragma: no cover - 线程体
        try:
            res = self._func(*self._args, **self._kwargs)
        except Exception as exc:
            self.failed.emit(f"{exc.__class__.__name__}: {exc}\n\n{traceback.format_exc()}")
        else:
            self.done.emit(res)


class ScanWorker(QThread):
    """扫描线程：把 scanner 的进度回调转成 Qt 信号；支持暂停/终止。

    终止信号用「哨兵文件」实现：写到 ``tempfile.gettempdir()``，主线程和
    所有子进程都 ``os.path.exists()`` 检查它，绕过 PyInstaller ``--onefile``
    下 ``multiprocessing.Event`` 跨解压目录失效的问题。
    """

    progressed = Signal(str, int, int, str)   # phase, done, total, message
    currentFile = Signal(str)
    succeeded = Signal(object)
    failed = Signal(str)
    pausedChanged = Signal(bool)

    def __init__(self, store: Store, roots: Sequence[str], workers: Optional[int],
                 incremental: bool, probe_video: bool, parse_batch: int = 40,
                 prune_missing: bool = False) -> None:
        super().__init__()
        self.store, self.roots = store, list(roots)
        self.workers, self.incremental, self.probe_video = workers, incremental, probe_video
        self.parse_batch = parse_batch
        self.prune_missing = prune_missing
        self.stop_sentinel = os.path.join(tempfile.gettempdir(), "nfo_profiler_scan.stop")
        self.pause_event = threading.Event()
        self.pause_event.set()
        self._paused = False

    def pause(self) -> None:
        if not self._paused:
            self.pause_event.clear()
            self._paused = True
            self.pausedChanged.emit(True)

    def resume(self) -> None:
        if self._paused:
            self.pause_event.set()
            self._paused = False
            self.pausedChanged.emit(False)

    def toggle_pause(self) -> bool:
        if self._paused:
            self.resume()
        else:
            self.pause()
        return self._paused

    def stop(self) -> None:
        """写入哨兵文件；子进程在每个文件解析前检查，主线程在每个批次间检查。"""
        try:
            with open(self.stop_sentinel, "w", encoding="utf-8") as fh:
                fh.write("stop")
        except OSError:
            pass
        self.resume()  # 解除暂停，让主循环能及时看到哨兵

    def clear_stop(self) -> None:
        try:
            os.remove(self.stop_sentinel)
        except OSError:
            pass

    def run(self) -> None:  # pragma: no cover - 线程体
        def _progress(phase: str, done: int, total: int, msg: str) -> None:
            self.progressed.emit(phase, done, total, msg)

        def _on_file(path: str) -> None:
            self.currentFile.emit(path)

        self.clear_stop()  # 启动前先清掉残留哨兵
        try:
            res = scan_paths(
                self.roots, self.store, workers=self.workers,
                incremental=self.incremental, probe_video=self.probe_video,
                progress=_progress, on_file=_on_file,
                pause_event=self.pause_event,
                stop_sentinel=self.stop_sentinel,
                parse_batch=self.parse_batch,
                prune_missing=self.prune_missing,
            )
        except Exception as exc:
            self.failed.emit(f"{exc.__class__.__name__}: {exc}\n\n{traceback.format_exc()}")
        else:
            self.succeeded.emit(res.as_dict())


class DedupeWorker(QThread):
    """重复检测线程：进度通过信号回传（严禁在子线程里直接操作控件）。"""

    progressed = Signal(int, int, str)   # done, total, message
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, store: Store, source: Optional[str], use_num: bool,
                 use_title: bool, min_confidence: str) -> None:
        super().__init__()
        self.store, self.source = store, source
        self.use_num, self.use_title, self.min_confidence = use_num, use_title, min_confidence

    def run(self) -> None:  # pragma: no cover - 线程体
        from .dedupe import DuplicateFinder
        try:
            finder = DuplicateFinder(self.store, source=self.source)
            with self.store.lock():
                rep = finder.scan(
                    use_num=self.use_num, use_title=self.use_title,
                    min_confidence=self.min_confidence,
                    progress=lambda d, t, m: self.progressed.emit(d, t, m))
        except Exception as exc:
            self.failed.emit(f"{exc.__class__.__name__}: {exc}\n\n{traceback.format_exc()}")
        else:
            self.succeeded.emit(rep)


# ---------------------------------------------------------------------------


class MovieCard(QFrame):
    """作品推荐 Tab 的缩略图卡片（v1.2.0）。

    * 图片区：fanart / thumb / poster 优先级加载，无图显示占位；
    * 右上角 👍 / 👎 覆盖按钮：悬停卡片时才显示，点击投票（toggle 语义）；
    * 单击 → 选中（回调 ``on_select``）；双击 → 播放视频（回调 ``on_play``）。
    """

    CARD_W = 200          # 卡片总宽（默认，运行时可按窗口高度动态放大）
    IMG_H = 130           # 图片区高度
    INFO_H = 56           # 信息区高度（番号 + 标题）
    MIN_W, MAX_W = 150, 400          # 动态缩放的宽度边界
    MIN_IMG_H, MAX_IMG_H = 100, 320  # 图片区高度边界（fanart 为 16:9，太高会留黑边）

    def __init__(self, movie: Dict[str, Any], win: "MainWindow",
                 on_select: Optional[Callable[[Dict[str, Any]], None]] = None,
                 on_play: Optional[Callable[[Dict[str, Any]], None]] = None,
                 on_vote: Optional[Callable[[Dict[str, Any], int], None]] = None,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.movie = movie
        self._win = win
        self._on_select = on_select
        self._on_play = on_play
        self._on_vote = on_vote
        self._selected = False
        self._vote = 0  # 本会话内的投票状态（+1/-1/0）
        self._img_path: Optional[str] = None
        self._card_w, self._img_h = self.CARD_W, self.IMG_H
        self._num_text = movie.get("num") or ""
        self._title_text = movie.get("title") or ""
        self._studio_text = movie.get("studio") or ""

        self.setStyleSheet(
            "MovieCard { background:#171a1f; border:1px solid #2c3038; "
            "border-radius:6px; }"
            "MovieCard[selected='true'] { border:2px solid #8ab4f8; }")

        # 图片
        self.img_label = QLabel(self)
        self.img_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.img_label.setStyleSheet(
            "background:#0d0f13; border-radius:4px; color:#4b5263; font-size:11px;")

        # 信息区
        self.lbl_num = QLabel(self)
        self.lbl_num.setStyleSheet(
            "color:#e6e1cf; font-weight:bold; font-size:12px; background:transparent;")
        self.lbl_title = QLabel(self)
        self.lbl_title.setStyleSheet(
            "color:#a9b1ba; font-size:11px; background:transparent;")
        self.lbl_studio = QLabel(self)
        self.lbl_studio.setStyleSheet(
            "color:#6b7280; font-size:10px; background:transparent;")

        # 右上角 👍 / 👎 覆盖按钮（默认隐藏，悬停显示）
        self.btn_up = QPushButton("👍", self)
        self.btn_down = QPushButton("👎", self)
        for i, btn in enumerate((self.btn_up, self.btn_down)):
            btn.setFixedSize(28, 28)
            btn.setToolTip("👍 喜欢（再次点击撤销）" if i == 0
                           else "👎 不喜欢（再次点击撤销）")
            btn.setStyleSheet(
                "QPushButton { background:rgba(30,33,40,220); border:1px solid #3a3f4b; "
                "border-radius:14px; font-size:13px; }"
                "QPushButton:hover { background:rgba(60,66,80,240); }")
            btn.hide()
        self.btn_up.clicked.connect(lambda: self._handle_vote(+1))
        self.btn_down.clicked.connect(lambda: self._handle_vote(-1))

        self.apply_size(self.CARD_W, self.IMG_H)
        self._load_image()

    # ------------------------------------------------------------------
    # 尺寸自适应（v1.3.0：推荐 Tab 顶满页面）
    # ------------------------------------------------------------------
    def apply_size(self, card_w: int, img_h: int) -> None:
        """按可用空间重排卡片内部控件；标题截断长度随之自适应。"""
        w = max(self.MIN_W, min(self.MAX_W, int(card_w)))
        h = max(self.MIN_IMG_H, min(self.MAX_IMG_H, int(img_h)))
        self._card_w, self._img_h = w, h
        self.setFixedSize(w, h + self.INFO_H)
        self.img_label.setGeometry(4, 4, w - 8, h - 4)
        self.lbl_num.setGeometry(8, h + 2, w - 16, 18)
        self.lbl_title.setGeometry(8, h + 20, w - 16, 18)
        self.lbl_studio.setGeometry(8, h + 38, w - 16, 16)
        # 11px 字体下中日字符约 11px 宽，据此动态截断
        max_chars = max(10, (w - 16) // 11)
        title = self._title_text
        if len(title) > max_chars:
            title = title[:max_chars] + "…"
        studio = self._studio_text
        if len(studio) > max(6, max_chars // 2):
            studio = studio[:max(6, max_chars // 2)] + "…"
        self.lbl_num.setText(self._num_text)
        self.lbl_title.setText(title)
        self.lbl_studio.setText(studio)
        self._position_buttons()

    def _load_image(self) -> None:
        """加载并等比缩放缩略图（找不到图时显示占位文字）。"""
        path = self._win.find_movie_image(self.movie.get("path", "") or "")
        self._img_path = path
        if path:
            pm = QPixmap(path)
            if not pm.isNull():
                pm = pm.scaled(self.img_label.width(), self.img_label.height(),
                               Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
                self.img_label.setPixmap(pm)
        if self.img_label.pixmap() is None or self.img_label.pixmap().isNull():
            self.img_label.setText(
                (self.movie.get("num") or "无番号") + "\n（无缩略图）")

    def resize_image(self) -> None:
        """尺寸变化后重新缩放已加载的图（避免放大后模糊）。"""
        if self._img_path:
            self.img_label.setPixmap(QPixmap())
            self._load_image()

    # ------------------------------------------------------------------
    def _position_buttons(self) -> None:
        self.btn_up.move(self.width() - 62, 8)
        self.btn_down.move(self.btn_up.x() + 32, 8)

    def resizeEvent(self, event: Any) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._position_buttons()

    def enterEvent(self, event: Any) -> None:  # noqa: N802
        self.btn_up.show()
        self.btn_down.show()
        super().enterEvent(event)

    def leaveEvent(self, event: Any) -> None:  # noqa: N802
        self.btn_up.hide()
        self.btn_down.hide()
        super().leaveEvent(event)

    # ------------------------------------------------------------------
    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        self.setProperty("selected", "true" if selected else "false")
        # 强制刷新 QSS
        self.style().unpolish(self)
        self.style().polish(self)

    def _handle_vote(self, vote: int) -> None:
        if self._on_vote is not None:
            self._on_vote(self.movie, vote)

    def set_vote_state(self, vote: int) -> None:
        """根据投票结果（含 toggle 撤销后为 0）刷新按钮外观。"""
        self._vote = vote
        up = "✅" if vote > 0 else "👍"
        down = "❌" if vote < 0 else "👎"
        self.btn_up.setText(up)
        self.btn_down.setText(down)
        if vote > 0:
            self.btn_up.setStyleSheet(
                "QPushButton { background:rgba(46,125,50,230); border:1px solid #66bb6a; "
                "border-radius:14px; font-size:13px; }")
            self.btn_down.setStyleSheet(
                "QPushButton { background:rgba(30,33,40,220); border:1px solid #3a3f4b; "
                "border-radius:14px; font-size:13px; }")
        elif vote < 0:
            self.btn_down.setStyleSheet(
                "QPushButton { background:rgba(183,28,28,230); border:1px solid #ef5350; "
                "border-radius:14px; font-size:13px; }")
            self.btn_up.setStyleSheet(
                "QPushButton { background:rgba(30,33,40,220); border:1px solid #3a3f4b; "
                "border-radius:14px; font-size:13px; }")
        else:
            for btn in (self.btn_up, self.btn_down):
                btn.setStyleSheet(
                    "QPushButton { background:rgba(30,33,40,220); border:1px solid #3a3f4b; "
                    "border-radius:14px; font-size:13px; }"
                    "QPushButton:hover { background:rgba(60,66,80,240); }")
        self.btn_up.setText("👍" if vote <= 0 else "✅")
        self.btn_down.setText("👎" if vote >= 0 else "❌")

    # ------------------------------------------------------------------
    def mousePressEvent(self, event: Any) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton \
                and self._on_select is not None:
            self._on_select(self.movie)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: Any) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton \
                and self._on_play is not None:
            self._on_play(self.movie)
        super().mouseDoubleClickEvent(event)


# ---------------------------------------------------------------------------

class VoteManagerDialog(QDialog):
    """👍 / 👎 投票记录管理器（v1.3.0）。

    * 顶部筛选：全部 / 👍 喜欢 / 👎 不喜欢；
    * 表格列出番号 / 标题 / 片商 / 投票 / 时间，支持多选；
    * 删除选中（Del）、清空当前筛选、关闭；
    * 双击行 → 打开该作品所在文件夹。
    """

    def __init__(self, store: Any, parent: Optional[QWidget] = None,
                 on_changed: Optional[Callable[[], None]] = None) -> None:
        super().__init__(parent)
        self.store = store
        self.on_changed = on_changed
        self.setWindowTitle("📑 投票记录（👍 / 👎）")
        self.resize(820, 520)
        v = QVBoxLayout(self)
        v.setContentsMargins(10, 10, 10, 10)
        v.setSpacing(8)

        # 顶部：筛选 + 统计
        top = QHBoxLayout()
        top.setSpacing(6)
        self.cmb_filter = QComboBox()
        self.cmb_filter.addItem("全部", 0)
        self.cmb_filter.addItem("👍 喜欢", 1)
        self.cmb_filter.addItem("👎 不喜欢", -1)
        self.cmb_filter.currentIndexChanged.connect(lambda _i: self.reload())
        top.addWidget(QLabel("筛选："))
        top.addWidget(self.cmb_filter)
        self.lbl_sum = QLabel("")
        self.lbl_sum.setProperty("hint", "true")
        top.addWidget(self.lbl_sum)
        top.addStretch(1)
        v.addLayout(top)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["投票", "番号", "标题", "片商", "投票时间"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 60)
        self.table.setColumnWidth(1, 110)
        self.table.setColumnWidth(3, 110)
        self.table.setColumnWidth(4, 140)
        self.table.itemDoubleClicked.connect(self._open_folder)
        v.addWidget(self.table, 1)

        btns = QHBoxLayout()
        btns.setSpacing(6)
        b_del = QPushButton("🗑 删除选中")
        b_del.setProperty("danger", "true")
        b_del.setToolTip("删除选中的投票记录（Del 键）")
        b_del.clicked.connect(self._delete_selected)
        b_clear = QPushButton("清空当前筛选")
        b_clear.clicked.connect(self._clear_filtered)
        b_open = QPushButton("📂 打开文件夹")
        b_open.clicked.connect(lambda: self._open_folder(None))
        b_close = QPushButton("关闭")
        b_close.clicked.connect(self.accept)
        for b in (b_del, b_clear, b_open):
            btns.addWidget(b)
        btns.addStretch(1)
        btns.addWidget(b_close)
        v.addLayout(btns)

        self.setStyleSheet(
            "QPushButton[danger='true'] { background:#7f1d1d; color:#fee2e2; "
            "border:1px solid #b91c1c; border-radius:4px; padding:5px 12px; }"
            "QPushButton[danger='true']:hover { background:#991b1b; }")

        self.reload()

    # ------------------------------------------------------------------
    def reload(self) -> None:
        vote = int(self.cmb_filter.currentData() or 0)
        rows = self.store.vote_records(vote=vote)
        self.table.setRowCount(len(rows))
        for r, rec in enumerate(rows):
            mark = "👍" if (rec.get("vote") or 0) > 0 else "👎"
            vals = [mark, rec.get("num") or "—", (rec.get("title") or "")[:60],
                    rec.get("studio") or "", rec.get("voted_at") or ""]
            for c, txt in enumerate(vals):
                it = QTableWidgetItem(str(txt))
                it.setData(Qt.ItemDataRole.UserRole, rec.get("movie_id"))
                if c == 0:
                    it.setForeground(QBrush(QColor(
                        "#9ece6a" if (rec.get("vote") or 0) > 0 else "#f7768e")))
                self.table.setItem(r, c, it)
        try:
            s = self.store.vote_summary()
        except Exception:
            s = {"up": 0, "down": 0, "total": 0}
        self.lbl_sum.setText(
            f"共 {s['total']} 条记录　|　👍 {s['up']} · 👎 {s['down']}"
            f"　|　当前显示 {len(rows)} 条")

    def _selected_ids(self) -> List[int]:
        ids: List[int] = []
        for r in sorted({i.row() for i in self.table.selectedIndexes()}):
            it = self.table.item(r, 0)
            if it is not None:
                mid = it.data(Qt.ItemDataRole.UserRole)
                if mid:
                    ids.append(int(mid))
        return ids

    def _delete_selected(self) -> None:
        ids = self._selected_ids()
        if not ids:
            QMessageBox.information(self, "未选择", "请先选中要删除的记录。")
            return
        ans = QMessageBox.question(
            self, "确认删除", f"将删除 {len(ids)} 条投票记录，推荐偏好会随之调整。\n继续？")
        if ans != QMessageBox.StandardButton.Yes:
            return
        for mid in ids:
            self.store.remove_vote(mid)
        self.reload()
        if self.on_changed:
            self.on_changed()

    def _clear_filtered(self) -> None:
        vote = int(self.cmb_filter.currentData() or 0)
        ans = QMessageBox.question(
            self, "确认清空",
            f"将清空「{self.cmb_filter.currentText()}」的全部投票记录。\n继续？")
        if ans != QMessageBox.StandardButton.Yes:
            return
        n = self.store.clear_votes(vote=vote)
        self.reload()
        if self.on_changed:
            self.on_changed()
        QMessageBox.information(self, "已清空", f"共清除 {n} 条投票记录。")

    def _open_folder(self, _item: Any) -> None:
        ids = self._selected_ids()
        if not ids:
            return
        recs = {r["movie_id"]: r for r in self.store.vote_records()}
        rec = recs.get(ids[0])
        path = (rec or {}).get("path") or ""
        if path and os.path.isdir(os.path.dirname(path)):
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(path)))

    def keyPressEvent(self, event: Any) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self._delete_selected()
            return
        super().keyPressEvent(event)


class MainWindow(QMainWindow):
    """NFO 画像矿工 —— 桌面主窗口。"""

    def __init__(self, db_path: Optional[str] = None, out_dir: Optional[str] = None) -> None:
        super().__init__()
        self.db_path = os.path.abspath(db_path or default_db_path())
        self.out_dir = os.path.abspath(out_dir or default_out_dir())
        os.makedirs(self.out_dir, exist_ok=True)

        try:
            self.norm = Normalizer.from_files(*resolve_config_paths(None))
            self.store = Store(self.db_path, normalizer=self.norm)
        except Exception as exc:
            QMessageBox.critical(None, "无法启动",
                                 f"数据库打开失败：{exc}\n\n请先关闭其它正在运行的本程序实例。")
            raise

        self._workers: List[QThread] = []
        self.scan_worker: Optional[ScanWorker] = None
        self.last_data: Optional[Dict[str, Any]] = None
        self.dup_report: Optional[Any] = None

        self.setWindowTitle(f"{APP_NAME} · {version_text()}")
        self.resize(1280, 820)
        self._set_icon()
        self._init_ui()
        self.refresh_sources()
        self.fill_detail_filters()
        self._set_status("就绪")

    # -- 基础 ----------------------------------------------------------
    def _set_icon(self) -> None:
        for name in ("logo.ico", "logo.png"):
            p = os.path.join(resource_dir(), name)
            if os.path.exists(p):
                self.setWindowIcon(QIcon(p))
                break

    def _init_ui(self) -> None:
        tabs = QTabWidget()
        tabs.addTab(self._build_scan_tab(), "① 数据源与扫描")
        tabs.addTab(self._build_overview_tab(), "② 画像概览")
        tabs.addTab(self._build_detail_tab(), "③ 作品明细")
        tabs.addTab(self._build_recommend_tab(), "④ 作品推荐")
        tabs.addTab(self._build_dedupe_tab(), "⑤ 重复检测")
        tabs.addTab(self._build_export_tab(), "⑥ 导出与报告")
        tabs.addTab(self._build_ai_tab(), "⑦ AI 分析")
        tabs.addTab(self._build_about_tab(), "⑧ 关于与声明")
        # v1.3.0：切到「④ 作品推荐」时按实际尺寸重排卡片网格
        # （非当前页不会收到 resize，尺寸停留在构造时的默认值）
        tabs.currentChanged.connect(self._on_tab_changed)
        self.tabs = tabs
        self.setCentralWidget(tabs)

        bar = QStatusBar()
        self.status_label = QLabel("就绪")
        bar.addWidget(self.status_label, 1)
        bar.addPermanentWidget(QLabel(f"{version_text()} · {COPYRIGHT_NOTICE}"))
        self.setStatusBar(bar)

    def _on_tab_changed(self, idx: int) -> None:
        """切换主 Tab 时按需重排（推荐卡片网格 / 概览卡片列数）。"""
        try:
            if self.tabs.tabText(idx).startswith("④"):
                QTimer.singleShot(60, lambda: self._relayout_recommend(refresh=True))
            elif self.tabs.tabText(idx).startswith("②"):
                QTimer.singleShot(60, self._relayout_overview_cards)
        except Exception:
            pass

    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)

    def _run_worker(self, worker: QThread, on_done: Callable[[Any], None],
                    on_error: Optional[Callable[[str], None]] = None) -> QThread:
        """统一注册后台任务：保持引用、绑定回调、结束时自动清理。"""
        self._workers.append(worker)

        def _done(res: Any) -> None:
            try:
                on_done(res)
            finally:
                self._cleanup(worker)

        def _fail(msg: str) -> None:
            try:
                (on_error or self._show_error)(msg)
            finally:
                self._cleanup(worker)

        if isinstance(worker, _Worker):
            worker.done.connect(_done)
        else:  # ScanWorker / DedupeWorker
            worker.succeeded.connect(_done)
        worker.failed.connect(_fail)
        worker.start()
        return worker

    def _cleanup(self, worker: QThread) -> None:
        try:
            self._workers.remove(worker)
        except ValueError:
            pass
        worker.deleteLater()

    def _show_error(self, msg: str) -> None:
        self._set_status("操作失败")
        QMessageBox.critical(self, "出错了", msg[:4000])

    # ==================================================================
    # ① 数据源与扫描
    # ==================================================================
    def _build_scan_tab(self) -> QWidget:
        w = QWidget()
        root = QHBoxLayout(w)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        root.addWidget(splitter, 1)

        # -- 左侧：目录 + 控制 + 进度 + 日志 --
        left = QWidget()
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(8)

        # 目录列表
        gb_dir = QGroupBox("待扫描的 NFO 目录（可多选，支持不同盘符）")
        gl = QVBoxLayout(gb_dir)
        gl.setSpacing(6)
        self.dir_list = QListWidget()
        self.dir_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.dir_list.setMinimumHeight(100)
        gl.addWidget(self.dir_list)
        btns = QHBoxLayout()
        for text, slot, tip in (
            ("＋ 添加目录", self._pick_dir, "选择本地目录加入扫描队列"),
            ("＋ 已登记数据源", self._add_registered_sources, "把库里已有的数据源加入队列"),
            ("－ 移除", self._remove_selected_dirs, "移除选中的待扫描目录"),
            ("清空", self._clear_dirs, "清空扫描队列"),
        ):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            btns.addWidget(b)
        btns.addStretch(1)
        gl.addLayout(btns)
        left_lay.addWidget(gb_dir, 1)

        # 选项 + 控制按钮
        gb_ctrl = QGroupBox("扫描选项")
        ctrl = QGridLayout(gb_ctrl)
        ctrl.setSpacing(6)
        self.chk_incremental = QCheckBox("增量扫描（跳过未变动文件）")
        self.chk_incremental.setChecked(True)
        self.chk_incremental.setToolTip("只解析 mtime/size 发生变化的文件，二次运行几乎瞬时")
        # v1.3.0：关闭增量 = 全量重扫，可顺带清掉磁盘上已删除作品的残留记录
        self.chk_prune = QCheckBox("全量重扫时清理已删除文件的记录")
        self.chk_prune.setChecked(True)
        self.chk_prune.setEnabled(False)
        self.chk_prune.setToolTip(
            "关闭增量扫描后生效：把「库里有记录、但磁盘上 NFO 已不存在」的条目删掉，\n"
            "清理已删视频留下的幽灵记录（复用本次扫描结果，几乎不增加耗时）")
        self.chk_incremental.toggled.connect(self._on_incremental_toggled)
        self.chk_probe = QCheckBox("探测视频文件")
        self.chk_probe.setChecked(True)
        self.chk_probe.setToolTip("统计视频容量；关闭可略微提速")
        self.spin_workers = QSpinBox()
        self.spin_workers.setRange(1, 32)
        self.spin_workers.setValue(min(os.cpu_count() or 4, 8))
        self.spin_workers.setPrefix("进程 ")
        self.spin_workers.setToolTip("多进程解析 NFO；文件很多时效果显著")
        self.spin_parse_batch = QSpinBox()
        self.spin_parse_batch.setRange(1, 200)
        self.spin_parse_batch.setValue(40)
        self.spin_parse_batch.setPrefix("批量 ")
        self.spin_parse_batch.setToolTip("每个子进程一次解析多少个文件，越大 IPC 开销越小")

        ctrl.addWidget(self.chk_incremental, 0, 0)
        ctrl.addWidget(self.chk_probe, 0, 1)
        ctrl.addWidget(QLabel("进程数"), 0, 2)
        ctrl.addWidget(self.spin_workers, 0, 3)
        ctrl.addWidget(QLabel("每批文件"), 0, 4)
        ctrl.addWidget(self.spin_parse_batch, 0, 5)
        ctrl.addWidget(self.chk_prune, 1, 0, 1, 6)

        self.btn_scan = QPushButton("开始扫描")
        self.btn_scan.setProperty("accent", "true")
        self.btn_scan.setToolTip("开始扫描上方列表中的目录")
        self.btn_scan.clicked.connect(self.start_scan)
        self.btn_pause = QPushButton("暂停")
        self.btn_pause.setEnabled(False)
        self.btn_pause.setToolTip("暂停扫描（当前批次结束后生效）")
        self.btn_pause.clicked.connect(self._toggle_scan_pause)
        self.btn_stop = QPushButton("终止")
        self.btn_stop.setEnabled(False)
        self.btn_stop.setToolTip("终止扫描并保留已处理结果")
        self.btn_stop.clicked.connect(self._stop_scan)
        bh = QHBoxLayout()
        bh.addStretch(1)
        bh.addWidget(self.btn_scan)
        bh.addWidget(self.btn_pause)
        bh.addWidget(self.btn_stop)
        ctrl.addLayout(bh, 1, 0, 1, 6)
        left_lay.addWidget(gb_ctrl)

        # 进度
        self.scan_bar = QProgressBar()
        self.scan_bar.setFormat("%v / %m (%p%)")
        self.scan_bar.setRange(0, 0)  # 初始忙碌
        left_lay.addWidget(self.scan_bar)
        self.lbl_scan_current = QLabel("空闲")
        self.lbl_scan_current.setProperty("hint", "true")
        self.lbl_scan_current.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.lbl_scan_result = QLabel("")
        top = QHBoxLayout()
        top.addWidget(self.lbl_scan_current, 1)
        top.addWidget(self.lbl_scan_result)
        left_lay.addLayout(top)

        # 日志
        self.scan_log = QPlainTextEdit()
        self.scan_log.setReadOnly(True)
        self.scan_log.setMaximumBlockCount(3000)
        self.scan_log.setMinimumHeight(120)
        left_lay.addWidget(self.scan_log, 2)

        splitter.addWidget(left)
        splitter.setStretchFactor(0, 7)

        # -- 右侧：已登记数据源 --
        right = QWidget()
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(0, 0, 0, 0)
        right_lay.setSpacing(8)
        gb_src = QGroupBox("已登记的数据源（库内记录）")
        v2 = QVBoxLayout(gb_src)
        v2.setSpacing(6)
        self.src_table = QTableWidget(0, 5)
        self.src_table.setHorizontalHeaderLabels(
            ["数据源目录", "作品数", "失败", "最后扫描", "操作"])
        # v1.3.0：列宽策略修正 —— 之前「操作」列被挤没，移除按钮显示不完整
        hh = self.src_table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for c in (1, 2, 3):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self.src_table.setColumnWidth(4, 78)
        self.src_table.verticalHeader().setVisible(False)
        self.src_table.verticalHeader().setDefaultSectionSize(28)
        self.src_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.src_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.src_table.setMinimumWidth(420)
        v2.addWidget(self.src_table)
        rb = QHBoxLayout()
        b1 = QPushButton("刷新列表")
        b1.clicked.connect(self.refresh_sources)
        b2 = QPushButton("移除选中")
        b2.setToolTip("删除该数据源及其下所有作品记录（不删磁盘文件）")
        b2.clicked.connect(self.remove_selected_source)
        rb.addWidget(b1)
        rb.addWidget(b2)
        rb.addStretch(1)
        v2.addLayout(rb)
        right_lay.addWidget(gb_src, 1)

        # 快速统计
        self.lbl_scan_stat = QLabel("数据库概况：—")
        self.lbl_scan_stat.setProperty("hint", "true")
        self.lbl_scan_stat.setWordWrap(True)
        right_lay.addWidget(self.lbl_scan_stat)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 3)

        self._update_scan_stat()
        return w

    def _update_scan_stat(self) -> None:
        try:
            n = self.store.count_movies()
            f = self.store.count_files()
            self.lbl_scan_stat.setText(f"数据库概况：{n:,} 部作品 / {f:,} 个 NFO")
        except Exception:
            self.lbl_scan_stat.setText("数据库概况：—")

    def _on_incremental_toggled(self, incremental: bool) -> None:
        """v1.3.0：关闭增量扫描 → 自动勾选（并解锁）「清理已删除文件的记录」。"""
        self.chk_prune.setEnabled(not incremental)
        if not incremental:
            self.chk_prune.setChecked(True)
        else:
            self.chk_prune.setChecked(False)

    def _pick_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择包含 NFO 的目录")
        if d:
            self._add_dir(d)

    def _add_dir(self, d: str) -> None:
        d = os.path.abspath(d)
        if not os.path.isdir(d):
            return
        for i in range(self.dir_list.count()):
            if self.dir_list.item(i).text() == d:
                return
        self.dir_list.addItem(QListWidgetItem(d))

    def _add_registered_sources(self) -> None:
        for s in self.store.list_sources():
            self._add_dir(s["root"])

    def _remove_selected_dirs(self) -> None:
        for item in reversed(self.dir_list.selectedItems()):
            self.dir_list.takeItem(self.dir_list.row(item))

    def _clear_dirs(self) -> None:
        self.dir_list.clear()

    # -- 扫描流程 --
    def start_scan(self) -> None:
        if self.scan_worker is not None and self.scan_worker.isRunning():
            QMessageBox.information(self, "正在扫描", "已有扫描任务在运行，请等它结束。")
            return
        roots = [self.dir_list.item(i).text() for i in range(self.dir_list.count())]
        if not roots:
            QMessageBox.warning(self, "没有目录", "请先在上方添加至少一个 NFO 目录。")
            return
        self._set_scan_buttons(running=True, paused=False)
        self.scan_bar.setRange(0, 0)  # 收集阶段先显示忙碌
        self.scan_log.clear()
        prune = bool(getattr(self, "chk_prune", None)
                     and self.chk_prune.isChecked() and not self.chk_incremental.isChecked())
        self._log(f"开始扫描：{len(roots)} 个目录，进程 {self.spin_workers.value()}，"
                  f"每批 {self.spin_parse_batch.value()} 个文件"
                  + ("，全量重扫 + 清理已删除记录" if prune else "，增量扫描"))
        self._set_status("正在扫描…")
        self.scan_worker = ScanWorker(
            self.store, roots, self.spin_workers.value(),
            self.chk_incremental.isChecked(), self.chk_probe.isChecked(),
            parse_batch=self.spin_parse_batch.value(), prune_missing=prune)
        self.scan_worker.progressed.connect(self._on_scan_progress)
        self.scan_worker.currentFile.connect(
            lambda p: self.lbl_scan_current.setText(f"正在解析：{p}"))
        self.scan_worker.pausedChanged.connect(self._on_scan_paused)
        self._run_worker(self.scan_worker, self._on_scan_done, self._on_scan_error)

    def _set_scan_buttons(self, running: bool, paused: bool) -> None:
        self.btn_scan.setEnabled(not running)
        self.btn_pause.setEnabled(running)
        self.btn_stop.setEnabled(running)
        self.btn_pause.setText("继续" if paused else "暂停")
        self.btn_pause.setToolTip("继续扫描" if paused else "暂停扫描（当前批次结束后生效）")

    def _toggle_scan_pause(self) -> None:
        if self.scan_worker is None or not self.scan_worker.isRunning():
            return
        now_paused = self.scan_worker.toggle_pause()
        self._set_scan_buttons(running=True, paused=now_paused)
        status = "已暂停" if now_paused else "继续扫描"
        self._set_status(status)
        self._log(status)

    def _stop_scan(self) -> None:
        if self.scan_worker is None or not self.scan_worker.isRunning():
            return
        self.scan_worker.stop()
        self._log("已请求终止扫描…")
        self._set_status("正在终止扫描…")

    def _on_scan_paused(self, paused: bool) -> None:
        self._set_scan_buttons(running=True, paused=paused)

    def _log(self, msg: str) -> None:
        self.scan_log.appendPlainText(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

    def _on_scan_progress(self, phase: str, done: int, total: int, msg: str) -> None:
        if phase == "parse" and total > 0:
            self.scan_bar.setMaximum(total)
            self.scan_bar.setValue(min(done, total))
        elif phase == "collect":
            self.scan_bar.setRange(0, 0)  # 收集阶段忙碌
        elif phase == "done":
            self.scan_bar.setMaximum(max(1, total))
            self.scan_bar.setValue(done)
        self.lbl_scan_result.setText(f"[{phase}] {msg}")
        if phase in ("collect", "done", "parse") and msg:
            if not msg.startswith(f"{done}/"):
                self._log(msg)
        self._set_status(f"扫描中：{msg}")

    def _on_scan_done(self, res: Dict[str, Any]) -> None:
        stopped = res.get("stopped", False)
        self._set_scan_buttons(running=False, paused=False)
        self.scan_bar.setMaximum(max(1, res.get("scanned", 0)))
        self.scan_bar.setValue(res.get("scanned", 0) if not stopped else res.get("scanned", 0))
        self.lbl_scan_current.setText("扫描完成" if not stopped else "扫描已终止")
        pruned = int(res.get("pruned", 0) or 0)
        self.lbl_scan_result.setText(
            f"新增 {res.get('added', 0)} · 更新 {res.get('updated', 0)} · "
            f"跳过 {res.get('skipped', 0)} · 失败 {res.get('failed', 0)}"
            + (f" · 清理失效 {pruned}" if pruned else "")
            + f" · 耗时 {res.get('duration_sec', 0)}s")
        self._log(f"{'已终止' if stopped else '扫描完成'}：{res}")
        if pruned:
            self._log(f"已清理 {pruned} 条磁盘上已不存在的作品记录")
        self._set_status("扫描完成" if not stopped else "扫描已终止")
        self.scan_worker = None
        self.refresh_sources()
        self.fill_detail_filters()
        self.last_data = None
        self._update_scan_stat()

    def _on_scan_error(self, msg: str) -> None:
        self._set_scan_buttons(running=False, paused=False)
        self.scan_worker = None
        self._show_error(msg)

    # -- 数据源表 --
    def refresh_sources(self) -> None:
        srcs = self.store.list_sources()
        self.src_table.setRowCount(len(srcs))
        for r, s in enumerate(srcs):
            vals = [s.get("root", ""), str(s.get("movie_count", 0)), str(s.get("fail_count", 0)),
                    s.get("last_scan_at", "") or "—"]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(v)
                it.setToolTip(v)
                self.src_table.setItem(r, c, it)
                if c in (1, 2):  # 数字列居中
                    it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            # v1.3.0：操作列 —— 用容器 + 布局撑满单元格，按钮不再被裁切
            cell = QWidget()
            cl = QHBoxLayout(cell)
            cl.setContentsMargins(2, 1, 2, 1)
            cl.setSpacing(2)
            btn = QPushButton("移除")
            btn.setFixedHeight(22)
            btn.setMinimumWidth(56)   # 保证「移除」两个字完整显示，不被压成 "移…"
            btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            btn.setToolTip(f"删除数据源及其下所有作品记录（不删磁盘文件）\n{s['root']}")
            btn.setStyleSheet(
                "QPushButton { background:#3a2226; border:1px solid #7f1d1d; "
                "color:#fecaca; border-radius:3px; padding:0 6px; font-size:11px; }"
                "QPushButton:hover { background:#7f1d1d; color:#fff; }")
            btn.clicked.connect(lambda _=False, root=s["root"]: self._remove_source(root))
            cl.addWidget(btn)
            self.src_table.setCellWidget(r, 4, cell)
            self.src_table.setRowHeight(r, 30)
        # 画像概览 / 重复检测的「数据源」下拉同步刷新
        for combo in (self.cmb_ov_source, self.cmb_dup_source):
            cur = combo.currentData()
            self._fill_source_combo(combo)
            idx = combo.findData(cur)
            if idx >= 0:
                combo.setCurrentIndex(idx)

    def _fill_source_combo(self, combo: QComboBox, include_all: bool = True) -> None:
        combo.blockSignals(True)
        combo.clear()
        if include_all:
            combo.addItem("全部数据源", "")
        for s in self.store.list_sources():
            combo.addItem(f"{s['root']}（{s['movie_count']}）", s["root"])
        combo.blockSignals(False)

    def _selected_source_root(self) -> Optional[str]:
        row = self.src_table.currentRow()
        if row < 0:
            return None
        it = self.src_table.item(row, 0)
        return it.text() if it else None

    def remove_selected_source(self) -> None:
        root = self._selected_source_root()
        if not root:
            QMessageBox.information(self, "未选择", "请先在表格里点选一个数据源。")
            return
        self._remove_source(root)

    def _remove_source(self, root: str) -> None:
        ans = QMessageBox.question(
            self, "确认移除",
            f"将删除该数据源及其下所有作品记录：\n{root}\n\n（只删数据库记录，不删磁盘文件）")
        if ans != QMessageBox.StandardButton.Yes:
            return
        n = self.store.remove_source(root)
        self.refresh_sources()
        self._set_status(f"已移除数据源 {root}（{n} 部作品记录）")

    # ==================================================================
    # ② 画像概览
    # ==================================================================
    def _build_overview_tab(self) -> QWidget:
        """② 画像概览（v1.3.0 UI 迭代）：

        * 顶部工具条（数据源 / TopN / 生成刷新 / 导出维度）；
        * 「一句话画像」摘要条（点生成后立即给出可读结论）；
        * 上区：KPI 卡片**自适应列数**铺满（随窗口宽度重排，不再固定 6 列留白）；
        * 下区：左侧维度列表 + 右侧 TopN 表格（含占比条 / 复制 / 导出）。
        """
        w = QWidget()
        root = QVBoxLayout(w)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # ---- 工具条 ----
        bar = QHBoxLayout()
        bar.setSpacing(6)
        self.cmb_ov_source = QComboBox()
        self.cmb_ov_source.setMinimumWidth(240)
        self.spin_topn = QSpinBox()
        self.spin_topn.setRange(5, 200)
        self.spin_topn.setValue(40)
        self.spin_topn.setPrefix("Top ")
        self.btn_overview = QPushButton("🔄 生成 / 刷新画像")
        self.btn_overview.setProperty("accent", "true")
        self.btn_overview.clicked.connect(self.run_overview)
        self.btn_ov_export_dim = QPushButton("💾 导出当前维度")
        self.btn_ov_export_dim.setToolTip("把右侧当前维度的 TopN 导出为 CSV")
        self.btn_ov_export_dim.clicked.connect(self._export_current_dim)
        bar.addWidget(QLabel("数据源："))
        bar.addWidget(self.cmb_ov_source, 1)
        bar.addWidget(self.spin_topn)
        bar.addWidget(self.btn_overview)
        bar.addWidget(self.btn_ov_export_dim)
        root.addLayout(bar)

        # ---- 一句话画像摘要 ----
        self.lbl_ov_persona = QLabel(
            "尚未生成画像 —— 选择数据源后点击「🔄 生成 / 刷新画像」。")
        self.lbl_ov_persona.setWordWrap(True)
        self.lbl_ov_persona.setProperty("hint", "true")
        self.lbl_ov_persona.setStyleSheet(
            "padding:7px 10px; background:#171a1f; border:1px solid #2c3038; "
            "border-radius:4px;")
        root.addWidget(self.lbl_ov_persona)

        split = QSplitter(Qt.Orientation.Vertical)
        split.setChildrenCollapsible(False)

        # ---- 上区：KPI 卡片（自适应列） ----
        cards_host = QWidget()
        self.ov_cards_grid = QGridLayout(cards_host)
        self.ov_cards_grid.setSpacing(8)
        self.ov_cards_grid.setContentsMargins(2, 2, 2, 2)
        self.ov_cards_scroll = QScrollArea()
        self.ov_cards_scroll.setWidgetResizable(True)
        self.ov_cards_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.ov_cards_scroll.setWidget(cards_host)
        self.ov_cards_host = cards_host
        split.addWidget(self.ov_cards_scroll)

        # ---- 下区：维度 ----
        dim_host = QWidget()
        dv = QVBoxLayout(dim_host)
        dv.setContentsMargins(0, 4, 0, 0)
        dv.setSpacing(6)
        self.dim_table = QTableWidget(0, 4)
        self.dim_table.setHorizontalHeaderLabels(["#", "名称", "数量", "占比"])
        self.dim_table.verticalHeader().setVisible(False)
        self.dim_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.dim_table.setAlternatingRowColors(True)
        self.dim_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        hh = self.dim_table.horizontalHeader()
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.dim_table.setColumnWidth(0, 46)
        self.dim_table.setColumnWidth(2, 90)
        dv.addWidget(self.dim_table, 1)
        split.addWidget(dim_host)

        split.setStretchFactor(0, 4)
        split.setStretchFactor(1, 5)
        root.addWidget(split, 1)

        # 维度选择器（放在底部，紧凑）
        dim = QHBoxLayout()
        dim.setSpacing(6)
        self.cmb_dim = QComboBox()
        self.cmb_dim.addItems([n for n, _ in self._dims()])
        self.cmb_dim.setMinimumWidth(160)
        self.cmb_dim.currentIndexChanged.connect(lambda _idx: self._fill_dim_table())
        b_copy = QPushButton("📋 复制维度")
        b_copy.clicked.connect(self._copy_current_dim)
        dim.addWidget(QLabel("统计维度："))
        dim.addWidget(self.cmb_dim)
        dim.addWidget(b_copy)
        dim.addStretch(1)
        self.lbl_ov_meta = QLabel("")
        self.lbl_ov_meta.setProperty("hint", "true")
        dim.addWidget(self.lbl_ov_meta)
        root.addLayout(dim)

        self._ov_cards: List[QFrame] = []
        return w

    # ------------------------------------------------------------------
    # 概览：KPI 卡片自适应重排（v1.3.0）
    # ------------------------------------------------------------------
    def _relayout_overview_cards(self) -> None:
        """按窗口宽度决定 KPI 卡片列数（3 ~ 8 列），铺满可用宽度。"""
        if not getattr(self, "ov_cards_host", None) or not self._ov_cards:
            return
        width = self.ov_cards_host.parentWidget().width() if self.ov_cards_host.parentWidget() \
            else self.width()
        cols = max(3, min(8, max(1, int(width // 168))))
        grid = self.ov_cards_grid
        while grid.count():
            item = grid.takeAt(0)
            if item.widget():
                item.widget().setParent(self.ov_cards_host)  # 先摘下，稍后重排
        for i, box in enumerate(self._ov_cards):
            grid.addWidget(box, i // cols, i % cols)
        for c in range(cols):
            grid.setColumnStretch(c, 1)

    def _persona_line(self, ov: Dict[str, Any], data: Dict[str, Any]) -> str:
        """拼一句可读的「一句话画像」（v1.3.0）。"""
        try:
            top_tags = [t.get("name", "") for t in (data.get("tags") or [])[:5]]
            top_terms = [t.get("name", "") for t in (data.get("title_terms") or [])[:3]]
            top_actors = [a.get("name", "") for a in (data.get("actors") or [])[:3]]
        except Exception:
            top_tags, top_terms, top_actors = [], [], []
        src = self.cmb_ov_source.currentText() or "全部数据源"
        parts = [f"📦 {src}", f"共 {ov.get('total', 0):,} 部",
                 f"容量 {human_gb(ov.get('total_bytes', 0))}"]
        if ov.get("year_min") and ov.get("year_max"):
            parts.append(f"{ov['year_min']}–{ov['year_max']} 年")
        if ov.get("avg_rating"):
            parts.append(f"均分 {ov['avg_rating']}")
        line = "　|　".join(parts)
        if top_tags:
            line += "\n🏷 偏好标签：" + "、".join(x for x in top_tags if x)
        if top_terms:
            line += "\n💬 标题高频：" + "、".join(x for x in top_terms if x)
        if top_actors:
            line += "\n⭐ 常看艺人：" + "、".join(x for x in top_actors if x)
        return line

    @staticmethod
    def _dims() -> List[Tuple[str, str]]:
        return [
            ("高频标题词", "title_terms"),
            ("高频标签", "tags"), ("高频艺人", "actors"), ("片商", "studios"),
            ("发行商", "publishers"), ("系列", "series"), ("导演", "directors"),
            ("番号前缀", "prefixes"), ("分辨率分布", "dist_resolution"),
            ("年份分布", "dist_year"), ("马赛克状态", "dist_censor"),
            ("时长分布", "dist_runtime"), ("评分分布", "dist_rating"),
            ("加入月份", "dist_added_month"), ("加入时段", "dist_added_hour"),
            ("加入星期", "dist_weekday"), ("演员数量分布", "dist_actor_count"),
        ]

    def run_overview(self) -> None:
        self.btn_overview.setEnabled(False)
        self._set_status("正在统计画像…")
        top_n = self.spin_topn.value()
        source = self.cmb_ov_source.currentData() or ""

        def _job() -> Dict[str, Any]:
            from .analyze import Analyzer
            with self.store.lock():
                return Analyzer(self.store, source=source or None).build_report_data(
                    top_tags=top_n, top_actors=top_n, top_misc=top_n,
                    with_cooccurrence=False, with_keywords=False)

        self._run_worker(_Worker(_job), self._on_overview_done)

    def _on_overview_done(self, data: Dict[str, Any]) -> None:
        self.btn_overview.setEnabled(True)
        self.last_data = data
        ov = data.get("overview", {})
        self._fill_cards(ov)
        self._fill_dim_table()
        # v1.3.0：一句话画像摘要
        try:
            self.lbl_ov_persona.setText(self._persona_line(ov, data))
        except Exception:
            self.lbl_ov_persona.setText(f"共 {ov.get('total', 0):,} 部作品")
        self._set_status(f"画像已生成：{ov.get('total', 0)} 部作品")

    def _fill_cards(self, ov: Dict[str, Any]) -> None:
        # 清掉旧卡片
        grid = self.ov_cards_grid
        while grid.count():
            item = grid.takeAt(0)
            wgt = item.widget()
            if wgt:
                wgt.deleteLater()
        self._ov_cards = []
        if not ov or not ov.get("total"):
            lbl = QLabel("暂无数据 —— 请先在「① 数据源与扫描」里扫描 NFO 目录。")
            lbl.setProperty("hint", "true")
            grid.addWidget(lbl, 0, 0)
            return
        d = ov.get("distinct", {})
        icons = ("🎬", "💾", "⏱", "🎞", "⭐", "👀", "👥", "🏷", "💬",
                 "🏢", "📚", "🎥", "📅")
        cards = [
            ("作品总数", f"{ov.get('total', 0):,}"),
            ("视频总容量", human_gb(ov.get("total_bytes", 0))),
            ("累计时长", f"{ov.get('total_days', 0)} 天"),
            ("平均时长", f"{ov.get('avg_runtime', 0)} 分"),
            ("平均评分", f"{ov.get('avg_rating', 0)}"),
            ("已看 / 播放", f"{ov.get('watched', 0)} / {ov.get('plays', 0)}"),
            ("艺人数", f"{d.get('actors', 0):,}"),
            ("标签数", f"{d.get('tags', 0):,}"),
            ("标题词数", f"{d.get('title_terms', 0):,}"),
            ("片商数", f"{d.get('studios', 0):,}"),
            ("系列数", f"{d.get('series', 0):,}"),
            ("导演数", f"{d.get('directors', 0):,}"),
            ("年份跨度", f"{ov.get('year_min', '—')} – {ov.get('year_max', '—')}"),
        ]
        for i, (name, value) in enumerate(cards):
            box = QFrame()
            box.setProperty("card", "true")
            vl = QVBoxLayout(box)
            vl.setContentsMargins(10, 7, 10, 7)
            vl.setSpacing(1)
            top = QHBoxLayout()
            n = QLabel(value)
            n.setProperty("num", "true")
            ic = QLabel(icons[i] if i < len(icons) else "•")
            ic.setProperty("hint", "true")
            top.addWidget(n)
            top.addStretch(1)
            top.addWidget(ic)
            t = QLabel(name)
            t.setProperty("hint", "true")
            vl.addLayout(top)
            vl.addWidget(t)
            self._ov_cards.append(box)
        self._relayout_overview_cards()

    def _fill_dim_table(self) -> None:
        data = self.last_data
        self.dim_table.setRowCount(0)
        if not data:
            return
        key = self._dims()[max(0, self.cmb_dim.currentIndex())][1]
        items = data.get(key) or []
        if not items:
            return
        total = sum(i.get("count", 0) for i in items) or 1
        self.dim_table.setRowCount(len(items))
        for r, it in enumerate(items):
            cnt = int(it.get("count", 0))
            pct = cnt * 100 / total
            no = QTableWidgetItem(str(r + 1))
            no.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.dim_table.setItem(r, 0, no)
            self.dim_table.setItem(r, 1, QTableWidgetItem(str(it.get("name", ""))))
            c = QTableWidgetItem(f"{cnt:,}")
            c.setTextAlignment(Qt.AlignmentFlag.AlignRight
                               | Qt.AlignmentFlag.AlignVCenter)
            self.dim_table.setItem(r, 2, c)
            bar = QProgressBar()
            bar.setMaximum(1000)
            bar.setValue(int(pct * 10))
            bar.setFormat(f"{pct:.1f}%")
            bar.setTextVisible(True)
            self.dim_table.setCellWidget(r, 3, bar)
        self.lbl_ov_meta.setText(
            f"「{self.cmb_dim.currentText()}」共 {len(items)} 项，合计 {total:,}")

    # -- 当前维度的复制 / 导出（v1.3.0） --
    def _dim_rows(self) -> List[Tuple[int, str, int, float]]:
        data = self.last_data
        if not data:
            return []
        key = self._dims()[max(0, self.cmb_dim.currentIndex())][1]
        items = data.get(key) or []
        total = sum(i.get("count", 0) for i in items) or 1
        return [(r + 1, str(it.get("name", "")), int(it.get("count", 0)),
                 round(int(it.get("count", 0)) * 100 / total, 2))
                for r, it in enumerate(items)]

    def _copy_current_dim(self) -> None:
        rows = self._dim_rows()
        if not rows:
            QMessageBox.information(self, "无数据", "请先生成画像并选择维度。")
            return
        txt = "\n".join(f"{r}\t{name}\t{cnt}\t{pct}%" for r, name, cnt, pct in rows)
        QApplication.clipboard().setText(txt)
        self._set_status(f"已复制「{self.cmb_dim.currentText()}」{len(rows)} 行到剪贴板")

    def _export_current_dim(self) -> None:
        rows = self._dim_rows()
        if not rows:
            QMessageBox.information(self, "无数据", "请先生成画像并选择维度。")
            return
        dim_name = self.cmb_dim.currentText().replace("/", "_")
        default = os.path.join(self.out_dir, f"画像维度_{dim_name}.csv")
        path, _ = QFileDialog.getSaveFileName(
            self, "导出维度 CSV", default, "CSV 文件 (*.csv)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as fh:
                wr = csv.writer(fh)
                wr.writerow(["排名", "名称", "数量", "占比(%)"])
                for r, name, cnt, pct in rows:
                    wr.writerow([r, name, cnt, pct])
        except OSError as exc:
            self._show_error(f"导出失败：{exc}")
            return
        self._set_status(f"已导出：{path}")

    # ==================================================================
    # ③ 作品明细
    # ==================================================================
    def _build_detail_tab(self) -> QWidget:
        """v1.1.3 调整：顶部紧凑工具栏 / 筛选可折叠 / 表格加路径 + 双击打开文件夹。"""
        w = QWidget()
        root = QVBoxLayout(w)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        # -- 紧凑工具栏：搜索 + 分页 + 折叠筛选 --
        bar = QHBoxLayout()
        bar.setSpacing(6)
        self.ed_search = QLineEdit()
        self.ed_search.setPlaceholderText("按番号 / 标题 / 片商 / 导演 / 演员 / 标签检索，回车确认")
        self.ed_search.setClearButtonEnabled(True)
        self.ed_search.returnPressed.connect(lambda: self.search_movies(0))
        bar.addWidget(self.ed_search, 1)

        b = QPushButton("🔍 检索")
        b.setProperty("accent", "true")
        b.clicked.connect(lambda: self.search_movies(0))
        bar.addWidget(b)

        bar.addSpacing(12)
        bar.addWidget(QLabel("每页"))
        self.spin_page = QSpinBox()
        self.spin_page.setRange(10, 1000)
        self.spin_page.setSingleStep(100)
        self.spin_page.setValue(200)
        bar.addWidget(self.spin_page)

        self.btn_prev = QPushButton("← 上一页")
        self.btn_next = QPushButton("下一页 →")
        self.btn_prev.clicked.connect(lambda: self.search_movies(max(0, self.offset - self.spin_page.value())))
        self.btn_next.clicked.connect(lambda: self.search_movies(self.offset + self.spin_page.value()))
        bar.addWidget(self.btn_prev)
        bar.addWidget(self.btn_next)

        bar.addSpacing(12)
        self.btn_filter_toggle = QPushButton("▼ 筛选")
        self.btn_filter_toggle.setCheckable(True)
        self.btn_filter_toggle.setChecked(True)
        self.btn_filter_toggle.toggled.connect(self._toggle_detail_filter_panel)
        bar.addWidget(self.btn_filter_toggle)
        root.addLayout(bar)

        # -- 可折叠筛选面板（先把控件建好，再用 _toggle 收/放） --
        self.detail_filter_panel = QWidget()
        self._build_detail_filter_panel()
        root.addWidget(self.detail_filter_panel)

        # -- 信息行 + 行级操作 --
        info = QHBoxLayout()
        info.setSpacing(6)
        self.lbl_detail = QLabel("共 0 条")
        self.lbl_detail.setProperty("hint", "true")
        info.addWidget(self.lbl_detail, 1)

        self.btn_open_sel = QPushButton("📂 打开选中文件夹")
        self.btn_open_sel.setToolTip("用资源管理器打开所选 NFO 所在的文件夹")
        self.btn_open_sel.clicked.connect(self._open_selected_detail_folder)
        info.addWidget(self.btn_open_sel)

        self.btn_copy_path = QPushButton("📋 复制路径")
        self.btn_copy_path.setToolTip("复制所选 NFO 的绝对路径到剪贴板")
        self.btn_copy_path.clicked.connect(self._copy_selected_detail_path)
        info.addWidget(self.btn_copy_path)

        self.chk_show_path = QCheckBox("显示「路径」列")
        self.chk_show_path.setToolTip("勾选后在表格里露出路径列，方便核对数据来源")
        self.chk_show_path.toggled.connect(self._toggle_detail_path_column)
        info.addWidget(self.chk_show_path)

        root.addLayout(info)

        # -- 主表格 --
        self.detail_table = QTableWidget(0, 13)
        self.detail_table.setHorizontalHeaderLabels(
            ["id", "路径", "番号", "标题", "年份", "发行", "片商", "导演",
             "评分", "时长", "分辨率", "标签", "演员"])
        self.detail_table.setShowGrid(False)
        self.detail_table.setAlternatingRowColors(True)
        self.detail_table.verticalHeader().setVisible(False)
        self.detail_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.detail_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.detail_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.detail_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)  # 标题 stretch
        # id / 路径 默认隐藏，需要时通过右上「显示路径」开关打开
        self.detail_table.setColumnHidden(0, True)
        self.detail_table.setColumnHidden(1, True)
        self.detail_table.setColumnWidth(2, 110)   # 番号
        self.detail_table.setColumnWidth(4, 60)    # 年份
        self.detail_table.setColumnWidth(8, 60)    # 评分
        self.detail_table.setColumnWidth(9, 60)    # 时长
        self.detail_table.setColumnWidth(10, 80)   # 分辨率
        # 表头排序（v1.1.5）：记录当前排序列与方向
        self.detail_table.horizontalHeader().setSortIndicatorShown(True)
        self.detail_sort_col = -1   # -1 = 默认排序
        self.detail_sort_asc = True
        self.detail_table.horizontalHeader().sectionClicked.connect(self._on_detail_header_clicked)
        # 双击任一单元格播放视频（v1.2.0：视频优先，fallback 打开文件夹）
        self.detail_table.itemDoubleClicked.connect(self._on_detail_double_clicked)
        # v1.2.0：鼠标悬停显示缩略图预览
        self.detail_table.setMouseTracking(True)
        self.detail_table.itemEntered.connect(self._on_detail_item_entered)
        self.detail_table.viewport().installEventFilter(self)
        # 行被选中时同步给动作按钮启用状态
        self.detail_table.itemSelectionChanged.connect(self._update_detail_action_buttons)
        self.btn_open_sel.setEnabled(False)
        self.btn_copy_path.setEnabled(False)

        root.addWidget(self.detail_table, 1)
        self.offset = 0
        return w

    # v1.1.5：作品明细排序映射（白名单，防 SQL 注入）
    _DETAIL_SORT_MAP: Dict[int, Tuple[str, str]] = {
        2: ("m.num", "番号"),
        3: ("m.title", "标题"),
        4: ("m.year", "年份"),
        5: ("m.premiered", "发行"),
        6: ("m.studio", "片商"),
        7: ("m.director", "导演"),
        8: ("m.userrating", "评分"),
        9: ("m.runtime_min", "时长"),
        10: ("m.resolution", "分辨率"),
    }

    def _on_detail_header_clicked(self, col: int) -> None:
        """点击表头切换排序列 / 方向。"""
        if col not in self._DETAIL_SORT_MAP:
            return
        if col == self.detail_sort_col:
            self.detail_sort_asc = not self.detail_sort_asc
        else:
            self.detail_sort_col = col
            self.detail_sort_asc = True
        order = Qt.SortOrder.AscendingOrder if self.detail_sort_asc else Qt.SortOrder.DescendingOrder
        self.detail_table.horizontalHeader().setSortIndicator(col, order)
        self.search_movies(0)

    def _detail_order_by(self) -> str:
        """根据表头排序状态返回 SQL ORDER BY 子句。"""
        if self.detail_sort_col < 0 or self.detail_sort_col not in self._DETAIL_SORT_MAP:
            return "m.userrating DESC, m.id"
        col_sql, _ = self._DETAIL_SORT_MAP[self.detail_sort_col]
        direction = "ASC" if self.detail_sort_asc else "DESC"
        # 排序列本身可能 NULL/空，用 m.id 保证稳定
        return f"{col_sql} {direction}, m.id"

    def _build_detail_filter_panel(self) -> None:
        """独立的筛选面板构建：可被整体显示/隐藏。"""
        gb = QGroupBox("筛选条件")
        fl = QGridLayout(gb)
        fl.setSpacing(6)
        fl.setContentsMargins(8, 8, 8, 8)

        self.cmb_det_source = QComboBox()
        self.cmb_det_source.setMinimumWidth(220)
        self.cmb_det_studio = QComboBox()
        self.cmb_det_studio.setEditable(True)
        self.cmb_det_studio.setMinimumWidth(140)
        self.cmb_det_director = QComboBox()
        self.cmb_det_director.setEditable(True)
        self.cmb_det_director.setMinimumWidth(140)
        self.cmb_det_resolution = QComboBox()
        self.cmb_det_resolution.setMinimumWidth(90)

        self.spin_year_from = QSpinBox()
        self.spin_year_from.setRange(0, 2100)
        self.spin_year_from.setSpecialValueText("不限")
        self.spin_year_from.setValue(0)
        self.spin_year_to = QSpinBox()
        self.spin_year_to.setRange(0, 2100)
        self.spin_year_to.setSpecialValueText("不限")
        self.spin_year_to.setValue(0)

        self.spin_rating_from = QDoubleSpinBox()
        self.spin_rating_from.setRange(0.0, 10.0)
        self.spin_rating_from.setSingleStep(0.5)
        self.spin_rating_from.setSpecialValueText("不限")
        self.spin_rating_from.setValue(0.0)
        self.spin_rating_to = QDoubleSpinBox()
        self.spin_rating_to.setRange(0.0, 10.0)
        self.spin_rating_to.setSingleStep(0.5)
        self.spin_rating_to.setSpecialValueText("不限")
        self.spin_rating_to.setValue(0.0)

        self.ed_tag_actor = QLineEdit()
        self.ed_tag_actor.setPlaceholderText("标签 / 演员 关键词（支持模糊匹配）")

        row = 0
        fl.addWidget(QLabel("数据源："), row, 0)
        fl.addWidget(self.cmb_det_source, row, 1, 1, 3)
        fl.addWidget(QLabel("片商："), row, 4)
        fl.addWidget(self.cmb_det_studio, row, 5)
        fl.addWidget(QLabel("导演："), row, 6)
        fl.addWidget(self.cmb_det_director, row, 7)
        fl.addWidget(QLabel("分辨率："), row, 8)
        fl.addWidget(self.cmb_det_resolution, row, 9)
        row += 1
        fl.addWidget(QLabel("年份："), row, 0)
        yh = QHBoxLayout()
        yh.setContentsMargins(0, 0, 0, 0)
        yh.addWidget(self.spin_year_from)
        yh.addWidget(QLabel("—"))
        yh.addWidget(self.spin_year_to)
        yh.addStretch(1)
        fl.addLayout(yh, row, 1)
        fl.addWidget(QLabel("评分："), row, 2)
        rh = QHBoxLayout()
        rh.setContentsMargins(0, 0, 0, 0)
        rh.addWidget(self.spin_rating_from)
        rh.addWidget(QLabel("—"))
        rh.addWidget(self.spin_rating_to)
        rh.addStretch(1)
        fl.addLayout(rh, row, 3)
        fl.addWidget(QLabel("标签 / 演员："), row, 4)
        fl.addWidget(self.ed_tag_actor, row, 5, 1, 5)
        row += 1
        bh = QHBoxLayout()
        bh.setContentsMargins(0, 0, 0, 0)
        bh.addStretch(1)
        btn_apply = QPushButton("✅ 应用筛选")
        btn_apply.setProperty("accent", "true")
        btn_apply.clicked.connect(lambda: self.search_movies(0))
        btn_reset = QPushButton("↺ 重置")
        btn_reset.clicked.connect(self._reset_detail_filters)
        bh.addWidget(btn_apply)
        bh.addWidget(btn_reset)
        fl.addLayout(bh, row, 0, 1, 10)

        lay = QVBoxLayout(self.detail_filter_panel)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(gb)

    def _toggle_detail_filter_panel(self, checked: bool) -> None:
        self.detail_filter_panel.setVisible(checked)
        self.btn_filter_toggle.setText("▲ 筛选" if checked else "▼ 筛选")

    def _toggle_detail_path_column(self, checked: bool) -> None:
        self.detail_table.setColumnHidden(1, not checked)
        if checked:
            self.detail_table.setColumnWidth(1, 320)

    def _selected_detail_paths(self) -> List[str]:
        """返回所有选中行的 NFO 路径（去重 + 仅真实存在）。"""
        paths: List[str] = []
        seen = set()
        for r in sorted({i.row() for i in self.detail_table.selectedItems()}):
            it = self.detail_table.item(r, 0)
            if not it:
                continue
            p = it.data(Qt.ItemDataRole.UserRole)
            if p and p not in seen:
                seen.add(p)
                paths.append(p)
        return paths

    def _update_detail_action_buttons(self) -> None:
        has = bool(self._selected_detail_paths())
        self.btn_open_sel.setEnabled(has)
        self.btn_copy_path.setEnabled(has)

    def _open_selected_detail_folder(self) -> None:
        paths = self._selected_detail_paths()
        for p in paths:
            if os.path.exists(p):
                QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(p)))
        self._set_status(f"已打开 {sum(1 for p in paths if os.path.exists(p))} 个文件夹")

    def _copy_selected_detail_path(self) -> None:
        paths = self._selected_detail_paths()
        if not paths:
            return
        QApplication.clipboard().setText("\n".join(paths))
        self._set_status(f"已复制 {len(paths)} 条路径到剪贴板")

    def _on_detail_double_clicked(self, item: QTableWidgetItem) -> None:
        """v1.2.0：双击任一单元格 → 播放该行作品文件夹里的视频；找不到视频则打开文件夹。"""
        if not item:
            return
        row = item.row()
        first = self.detail_table.item(row, 0)
        if first is None:
            return
        path = first.data(Qt.ItemDataRole.UserRole)
        if not path or not os.path.exists(path):
            self._set_status("路径已失效")
            return
        self._hide_image_popup()  # 播放前先收掉悬停预览
        self._play_movie_video(path)

    # v1.2.0：视频扩展名（按优先级）
    VIDEO_EXTS = (".mp4", ".mkv", ".avi", ".wmv", ".m2ts", ".ts",
                  ".flv", ".mov", ".rmvb", ".mpg", ".mpeg", ".webm")

    def _find_movie_video(self, nfo_path: str) -> Optional[str]:
        """在 NFO 同目录下找视频文件；同名前缀优先，其次目录内任意视频。"""
        folder = os.path.dirname(nfo_path)
        base = os.path.splitext(os.path.basename(nfo_path))[0]
        try:
            entries = os.listdir(folder)
        except OSError:
            return None
        # 1) 同名前缀的视频（ABC-123.mp4 / ABC-123-cd1.mkv…）
        same_base = []
        any_video = []
        for name in entries:
            low = name.lower()
            for ext in self.VIDEO_EXTS:
                if low.endswith(ext):
                    stem = os.path.splitext(name)[0]
                    if stem == base or stem.startswith(base):
                        same_base.append(name)
                    else:
                        any_video.append(name)
                    break
        for pool in (same_base, any_video):
            if pool:
                pool.sort()
                return os.path.join(folder, pool[0])
        return None

    def _play_movie_video(self, nfo_path: str) -> None:
        """播放 NFO 对应的视频文件；找不到视频则打开所在文件夹。"""
        if not nfo_path or not os.path.exists(nfo_path):
            self._set_status("路径已失效")
            return
        video = self._find_movie_video(nfo_path)
        if video:
            QDesktopServices.openUrl(QUrl.fromLocalFile(video))
            self._set_status(f"▶ 播放：{os.path.basename(video)}")
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(nfo_path)))
            self._set_status(f"未找到视频，已打开文件夹：{os.path.dirname(nfo_path)}")

    # v1.2.0：缩略图查找（fanart → thumb → poster，兼容 png/jpeg）
    IMAGE_SUFFIXES = (
        "-fanart.jpg", "-fanart.png", "-fanart.jpeg",
        "-thumb.jpg", "-thumb.png", "-thumb.jpeg",
        "-poster.jpg", "-poster.png", "-poster.jpeg",
    )

    def find_movie_image(self, nfo_path: str) -> Optional[str]:
        """返回 NFO 同目录下对应的作品图片路径（找不到返回 None）。"""
        if not nfo_path:
            return None
        folder = os.path.dirname(nfo_path)
        base = os.path.splitext(os.path.basename(nfo_path))[0]
        # 按用户要求的优先级：-fanart → -thumb → -poster
        for main in ("-fanart", "-thumb", "-poster"):
            for ext in (".jpg", ".jpeg", ".png"):
                candidate = os.path.join(folder, base + main + ext)
                if os.path.isfile(candidate):
                    return candidate
        # 兜底：目录下不带前缀的 fanart.jpg / poster.jpg / thumb.jpg
        for name in ("fanart.jpg", "thumb.jpg", "poster.jpg",
                     "fanart.png", "poster.png"):
            candidate = os.path.join(folder, name)
            if os.path.isfile(candidate):
                return candidate
        return None

    def _on_detail_item_entered(self, item: QTableWidgetItem) -> None:
        """v1.2.0：鼠标悬停在作品明细行上 → 浮动显示该作品的缩略图。"""
        if item is None:
            return
        first = self.detail_table.item(item.row(), 0)
        if first is None:
            return
        nfo_path = first.data(Qt.ItemDataRole.UserRole)
        if not nfo_path:
            return
        img = self.find_movie_image(nfo_path)
        if img:
            # 悬停预览不自动隐藏，靠鼠标离开表格时收起
            self._show_image_popup(img, auto_hide_ms=0)
        else:
            self._hide_image_popup()

    def _show_detail_image_preview(self, nfo_path: str) -> None:
        """（兼容保留）找到 nfo_path 同目录下对应的图片并浮动显示。"""
        img = self.find_movie_image(nfo_path)
        if img:
            self._show_image_popup(img)

    def _show_image_popup(self, image_path: str, max_size: int = 360,
                          auto_hide_ms: int = 3000) -> None:
        """在鼠标旁显示一个浮动图片预览。

        ``auto_hide_ms=0`` 时不自动隐藏（悬停模式，靠鼠标离开事件收起）。
        """
        if not hasattr(self, "_img_popup") or self._img_popup is None:
            self._img_popup = QLabel(None, Qt.WindowType.ToolTip)
            self._img_popup.setFrameStyle(QFrame.Shape.StyledPanel)
            self._img_popup.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._img_popup.setStyleSheet("background:#0f1115; border:1px solid #2c3038;")
        pm = QPixmap(image_path)
        if pm.isNull():
            return
        if pm.width() > max_size or pm.height() > max_size:
            pm = pm.scaled(max_size, max_size,
                           Qt.AspectRatioMode.KeepAspectRatio,
                           Qt.TransformationMode.SmoothTransformation)
        self._img_popup.setPixmap(pm)
        self._img_popup.resize(pm.size() + QSize(6, 6))
        # 定位在鼠标右下方 15px，避免遮挡表格
        pos = QCursor.pos()
        screen = QApplication.primaryScreen().availableGeometry()
        x = min(pos.x() + 15, screen.width() - self._img_popup.width() - 5)
        y = min(pos.y() + 15, screen.height() - self._img_popup.height() - 5)
        self._img_popup.move(x, y)
        self._img_popup.show()
        self._img_popup.raise_()
        if auto_hide_ms > 0:
            QTimer.singleShot(auto_hide_ms, self._hide_image_popup)

    def _hide_image_popup(self) -> None:
        if hasattr(self, "_img_popup") and self._img_popup is not None:
            self._img_popup.hide()

    def eventFilter(self, obj: Any, event: Any) -> bool:
        """v1.2.0：鼠标离开作品明细表格 → 收起悬停缩略图。
        v1.3.0：点击「已投 👍x · 👎y」统计标签 → 打开投票记录管理器。"""
        table = getattr(self, "detail_table", None)
        if table is not None and obj is table.viewport() \
                and event is not None and event.type() == event.Type.Leave:
            self._hide_image_popup()
        stat = getattr(self, "lbl_rec_stat", None)
        if stat is not None and obj is stat and event is not None \
                and event.type() == event.Type.MouseButtonPress:
            self.open_vote_manager()
            return True
        return super().eventFilter(obj, event)

    def _reset_detail_filters(self) -> None:
        self.cmb_det_source.setCurrentIndex(0)
        self.cmb_det_studio.setCurrentIndex(0)
        self.cmb_det_director.setCurrentIndex(0)
        self.cmb_det_resolution.setCurrentIndex(0)
        self.spin_year_from.setValue(0)
        self.spin_year_to.setValue(0)
        self.spin_rating_from.setValue(0.0)
        self.spin_rating_to.setValue(0.0)
        self.ed_tag_actor.setText("")
        self.ed_search.setText("")
        self.search_movies(0)

    def fill_detail_filters(self) -> None:
        """从数据库拉取可选值，用于筛选下拉框。扫描完成后应再调用一次。"""
        # 数据源由 refresh_sources 统一维护，这里同步
        cur_src = self.cmb_det_source.currentData()
        self.cmb_det_source.blockSignals(True)
        self.cmb_det_source.clear()
        self.cmb_det_source.addItem("全部数据源", "")
        for s in self.store.list_sources():
            self.cmb_det_source.addItem(f"{s['root']}（{s['movie_count']}）", s["root"])
        idx = self.cmb_det_source.findData(cur_src)
        self.cmb_det_source.setCurrentIndex(max(0, idx))
        self.cmb_det_source.blockSignals(False)

        def _fill(combo: QComboBox, sql: str) -> None:
            cur = combo.currentText()
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("全部", "")
            for row in self.store.conn.execute(sql):
                v = row[0]
                if v:
                    combo.addItem(v, v)
            combo.setCurrentText(cur if cur else "全部")
            combo.blockSignals(False)

        with self.store.lock():
            _fill(self.cmb_det_studio, "SELECT DISTINCT studio FROM movies WHERE studio<>'' ORDER BY studio LIMIT 300")
            _fill(self.cmb_det_director, "SELECT DISTINCT director FROM movies WHERE director<>'' ORDER BY director LIMIT 300")
            _fill(self.cmb_det_resolution, "SELECT DISTINCT resolution FROM movies WHERE resolution<>'' ORDER BY resolution")

    def search_movies(self, offset: int = 0) -> None:
        q = self.ed_search.text().strip()
        limit = self.spin_page.value()
        self.offset = max(0, offset)
        self._set_status("正在检索…")

        # 读取筛选条件（在主线程读取控件值，避免跨线程）
        source = self.cmb_det_source.currentData() or ""
        studio = self.cmb_det_studio.currentText().strip() if self.cmb_det_studio.currentIndex() > 0 else ""
        director = self.cmb_det_director.currentText().strip() if self.cmb_det_director.currentIndex() > 0 else ""
        resolution = self.cmb_det_resolution.currentData() or ""
        year_from = self.spin_year_from.value()
        year_to = self.spin_year_to.value()
        rating_from = self.spin_rating_from.value()
        rating_to = self.spin_rating_to.value()
        tag_actor = self.ed_tag_actor.text().strip()

        def _job() -> Dict[str, Any]:
            conn = self.store.conn
            conds: List[str] = []
            params: List[Any] = []
            if q:
                like = f"%{q}%"
                conds.append(
                    "(num LIKE ? OR title LIKE ? OR clean_title LIKE ? "
                    "OR studio LIKE ? OR director LIKE ? OR originaltitle LIKE ?)")
                params.extend([like] * 6)
            if source:
                conds.append("source = ?")
                params.append(source)
            if studio:
                conds.append("studio = ?")
                params.append(studio)
            if director:
                conds.append("director = ?")
                params.append(director)
            if resolution:
                conds.append("resolution = ?")
                params.append(resolution)
            if year_from > 0:
                conds.append("year >= ?")
                params.append(year_from)
            if year_to > 0:
                conds.append("year <= ?")
                params.append(year_to)
            if rating_from > 0:
                conds.append("userrating >= ?")
                params.append(rating_from)
            if rating_to > 0:
                conds.append("userrating <= ?")
                params.append(rating_to)
            if tag_actor:
                like_ta = f"%{tag_actor}%"
                conds.append(
                    "(EXISTS (SELECT 1 FROM movie_tags t WHERE t.movie_id=m.id AND t.tag LIKE ?) "
                    "OR EXISTS (SELECT 1 FROM movie_actors a WHERE a.movie_id=m.id AND a.actor LIKE ?))")
                params.extend([like_ta, like_ta])
            where = "WHERE " + " AND ".join(conds) if conds else ""

            with self.store.lock():
                # v1.1.5 修复：WHERE 子句里 tag_actor 筛选引用了 m.id，
                # 所以 FROM 也要带别名 m（否则 SQLite 报 no such column: m.id）
                total = int(conn.execute(
                    f"SELECT COUNT(*) c FROM movies m {where}", params).fetchone()["c"])
                rows = []
                # v1.1.3：多取 path 字段，用于「双击打开文件夹 / 复制路径」
                order_by = self._detail_order_by()
                sql = ("SELECT m.id, m.num, m.title, m.year, m.premiered, m.studio, m.director, "
                       "m.userrating, m.runtime_min, m.resolution, m.path FROM movies m "
                       f"{where} ORDER BY {order_by} LIMIT ? OFFSET ?")
                for r in conn.execute(sql, params + [limit, self.offset]):
                    d = dict(r)
                    mid = d.pop("id")
                    path = d.pop("path", "") or ""
                    d["tags"] = " / ".join(x["tag"] for x in conn.execute(
                        "SELECT tag FROM movie_tags WHERE movie_id=?", (mid,)))
                    d["actors"] = " / ".join(x["actor"] for x in conn.execute(
                        "SELECT actor FROM movie_actors WHERE movie_id=?", (mid,)))
                    d["__path__"] = path
                    rows.append(d)
            return {"total": total, "rows": rows, "offset": self.offset, "limit": limit}

        self._run_worker(_Worker(_job), self._on_search_done)

    def _on_search_done(self, res: Dict[str, Any]) -> None:
        rows = res["rows"]
        self.detail_table.setRowCount(len(rows))
        # id(0) path(1) num(2) title(3) year(4) premiered(5) studio(6) director(7)
        # rating(8) runtime_min(9) resolution(10) tags(11) actors(12)
        keys = ["num", "title", "year", "premiered", "studio", "director",
                "userrating", "runtime_min", "resolution", "tags", "actors"]
        for r, d in enumerate(rows):
            mid_it = QTableWidgetItem(str(d.get("num", "")))  # 第 0 列
            mid_it.setData(Qt.ItemDataRole.UserRole, d.get("__path__", "") or "")
            self.detail_table.setItem(r, 0, mid_it)
            path_text = d.get("__path__", "") or ""
            self.detail_table.setItem(r, 1, QTableWidgetItem(path_text))
            for offset, k in enumerate(keys, start=2):
                v = d.get(k, "")
                if k == "title" and v and len(str(v)) > 60:
                    v = str(v)[:60] + "…"
                cell = QTableWidgetItem(str(v if v else ""))
                if k == "userrating":
                    try:
                        cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    except Exception:
                        pass
                self.detail_table.setItem(r, offset, cell)
        start = res["offset"] + 1 if rows else 0
        self.lbl_detail.setText(
            f"共 {res['total']:,} 条，当前显示第 {start} – {res['offset'] + len(rows)} 条"
            "（双击行可打开文件夹；选中行用上方按钮）")
        self._set_status("检索完成")
        self.btn_prev.setEnabled(res["offset"] > 0)
        self.btn_next.setEnabled(res["offset"] + len(rows) < res["total"])
        self._update_detail_action_buttons()

    # ==================================================================
    # ④ 作品推荐（v1.2.0 新增）
    # ==================================================================
    def _recommender(self):
        """惰性获取推荐引擎单例。"""
        if not hasattr(self, "_rec"):
            from .recommender import Recommender
            self._rec = Recommender(self.store)
        return self._rec

    def _build_recommend_tab(self) -> QWidget:
        """④ 作品推荐：随机卡片流（上） + 相似推荐卡片流（下），👍/👎 反馈闭环。

        v1.3.0：**卡片按可用空间自动放大 / 换行容量自适应**，两条卡片流都撑满页面，
        不再像 v1.2.0 那样固定 130px 缩略图、下方大片留白。
        """
        w = QWidget()
        root = QVBoxLayout(w)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # ---- 顶部工具条：标题 + 换一批 + 投票统计（可点）+ 投票记录 ----
        bar = QHBoxLayout()
        bar.setSpacing(6)
        lbl = QLabel("🎲 随机推荐（每部右上角可 👍 / 👎，投得越多推荐越准）")
        lbl.setStyleSheet("font-weight:bold;")
        bar.addWidget(lbl, 1)
        self.btn_rec_refresh = QPushButton("🔄 换一批")
        self.btn_rec_refresh.setProperty("accent", "true")
        self.btn_rec_refresh.clicked.connect(self.recommend_refresh)
        bar.addWidget(self.btn_rec_refresh)
        self.btn_rec_votes = QPushButton("📑 投票记录")
        self.btn_rec_votes.setToolTip("查看 / 删除已投的 👍 👎 记录")
        self.btn_rec_votes.clicked.connect(self.open_vote_manager)
        bar.addWidget(self.btn_rec_votes)
        self.lbl_rec_stat = QLabel("")
        self.lbl_rec_stat.setProperty("hint", "true")
        self.lbl_rec_stat.setCursor(Qt.CursorShape.PointingHandCursor)
        self.lbl_rec_stat.setToolTip("点击查看 / 管理投票记录")
        self.lbl_rec_stat.installEventFilter(self)  # 点击 → 投票记录管理器
        bar.addWidget(self.lbl_rec_stat)
        root.addLayout(bar)

        splitter = QSplitter(Qt.Orientation.Vertical)

        # ---------- 上区：随机推荐 ----------
        top = QWidget()
        tv = QVBoxLayout(top)
        tv.setContentsMargins(0, 0, 0, 0)
        tv.setSpacing(4)
        # 随机：6 部一行，居中，无滚动
        self.rec_random_wrap, self.rec_random_area, self.rec_random_row = \
            self._build_card_row(vertical_scroll=False, center=True)
        tv.addWidget(self.rec_random_wrap, 1)
        splitter.addWidget(top)

        # ---------- 下区：相似推荐（基于选中） ----------
        bottom = QWidget()
        bv = QVBoxLayout(bottom)
        bv.setContentsMargins(0, 0, 0, 0)
        bv.setSpacing(4)
        self.lbl_rec_similar = QLabel(
            "🎯 相关推荐 —— 点上方任意卡片选中一部作品后，这里展示与它最像的 12 部（2 行，可上下滚动）")
        self.lbl_rec_similar.setStyleSheet("font-weight:bold;")
        self.lbl_rec_similar.setWordWrap(True)
        bv.addWidget(self.lbl_rec_similar)
        # 相似：12 部两行，超出高度时上下滚动
        self.rec_similar_wrap, self.rec_similar_area, self.rec_similar_row = \
            self._build_card_row(vertical_scroll=True, center=False)
        bv.addWidget(self.rec_similar_wrap, 1)
        splitter.addWidget(bottom)

        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 6)
        root.addWidget(splitter, 1)

        # 状态
        self.rec_random_cards: List[MovieCard] = []
        self.rec_similar_cards: List[MovieCard] = []
        self.rec_selected_movie: Optional[Dict[str, Any]] = None

        # 进入 Tab 时先刷一批（延迟触发避免阻塞启动）
        QTimer.singleShot(200, self.recommend_refresh)
        # 尺寸就绪后再按实际空间放大卡片
        QTimer.singleShot(400, lambda: self._relayout_recommend(refresh=True))
        return w

    # ------------------------------------------------------------------
    # 卡片布局（v1.3.1：固定数量 —— 随机 6 部一行，相似 12 部两行可上下滚）
    # ------------------------------------------------------------------
    REC_RANDOM_N = 6        # 随机推荐固定 6 部
    REC_RANDOM_COLS = 6
    REC_RANDOM_ROWS = 1
    REC_SIMILAR_N = 12      # 相关（相似）推荐固定 12 部
    REC_SIMILAR_COLS = 6
    REC_SIMILAR_ROWS = 2

    def _rec_metrics(self, area: QScrollArea, cols: int,
                     rows: int) -> Tuple[int, int]:
        """按「固定 cols 列 / rows 行」把卡片拉伸到铺满可用区域。

        * 卡片宽 = (可用宽 - 间距) / 列数，夹在 140–420；
        * 行高 = (可用高 - 间距) / 行数，夹在 120–280 ——
          相似区 2 行若超过可用高度，就会自然出现**上下滚动条**。
        """
        vw = area.viewport().width() or area.width() or 900
        vh = area.viewport().height() or area.height() or 300
        card_w = int((vw - (cols + 1) * 10) / max(1, cols))
        card_w = max(140, min(420, card_w))
        row_h = int((vh - (rows + 1) * 10) / max(1, rows))
        row_h = max(120, min(280, row_h))
        img_h = max(90, min(300, row_h - MovieCard.INFO_H - 6))
        return card_w, img_h

    def _rec_metrics_for(self, which: str) -> Tuple[int, int]:
        """which: "random" / "similar" —— 取对应区域的卡片尺寸。"""
        if which == "similar" and hasattr(self, "rec_similar_area"):
            return self._rec_metrics(self.rec_similar_area,
                                     self.REC_SIMILAR_COLS, self.REC_SIMILAR_ROWS)
        if not hasattr(self, "rec_random_area"):
            return MovieCard.CARD_W, MovieCard.IMG_H
        return self._rec_metrics(self.rec_random_area,
                                 self.REC_RANDOM_COLS, self.REC_RANDOM_ROWS)

    def _relayout_recommend(self, refresh: bool = False) -> None:
        """按当前窗口大小重排上下两区卡片（数量固定 6 / 12）。"""
        if not hasattr(self, "rec_random_area"):
            return
        w1, h1 = self._rec_metrics_for("random")
        w2, h2 = self._rec_metrics_for("similar")
        self.rec_random_cards = self._fit_cards(
            self.rec_random_row, getattr(self, "rec_random_cards", []),
            self.REC_RANDOM_N, w1, h1, self.REC_RANDOM_COLS)
        self.rec_similar_cards = self._fit_cards(
            self.rec_similar_row, getattr(self, "rec_similar_cards", []),
            self.REC_SIMILAR_N, w2, h2, self.REC_SIMILAR_COLS)
        if refresh and len(self.rec_random_cards) < self.REC_RANDOM_N:
            self.recommend_refresh()
        else:
            self._refresh_vote_stat()

    def _fit_cards(self, row: QWidget, cards: List["MovieCard"], cap: int,
                   card_w: int, img_h: int, cols: int) -> List["MovieCard"]:
        """按容量裁剪 / 缩放卡片并重排网格。"""
        cards = list(cards or [])
        if cap > 0 and len(cards) > cap:
            for extra in cards[cap:]:
                row.layout().removeWidget(extra)
                extra.deleteLater()
            cards = cards[:cap]
        for card in cards:
            card.apply_size(card_w, img_h)
            card.resize_image()
        self._reflow_cards(row, cards, cols)
        return cards

    def _reflow_cards(self, row: QWidget, cards: List["MovieCard"],
                      cols: Optional[int] = None) -> None:
        """把卡片按列数重排进网格（先摘下再放回，触发布局重算）。"""
        if not cards:
            return
        lay = row.layout()
        if lay is None:
            return
        n_cols = max(1, int(cols or getattr(self, "_rec_cols", 6) or 6))
        for card in cards:
            lay.removeWidget(card)
        for i, card in enumerate(cards):
            lay.addWidget(card, i // n_cols, i % n_cols,
                          Qt.AlignmentFlag.AlignCenter)

    def resizeEvent(self, event: Any) -> None:  # noqa: N802
        super().resizeEvent(event)
        # 窗口缩放时让推荐卡片跟着变大 / 变小（节流：只在整数百毫秒后触发）
        if hasattr(self, "_rec_resize_timer") and self._rec_resize_timer:
            return
        self._rec_resize_timer = True
        QTimer.singleShot(250, self._rec_resize_done)

    def _rec_resize_done(self) -> None:
        self._rec_resize_timer = False
        try:
            self._relayout_recommend(refresh=True)
        except Exception:
            pass

    def _build_card_row(self, vertical_scroll: bool = False,
                        center: bool = False) -> Tuple[QWidget, QScrollArea, QWidget]:
        """构建一条卡片区（v1.3.1）。

        * **取消左右滑动**：卡片按列数铺满宽度，不再需要横向滚动（v1.3.0 的
          ◀/▶ 按钮在网格布局下已经失效，这里直接移除）；
        * ``vertical_scroll=True``（相关推荐 2 行）→ 高度不够时出现**上下滚动条**；
        * ``center=True``（随机推荐 1 行）→ 卡片在区域内居中。
        """
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded if vertical_scroll
            else Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        lay = QGridLayout(inner)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(10)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter if center
                         else (Qt.AlignmentFlag.AlignTop
                               | Qt.AlignmentFlag.AlignHCenter))
        area.setWidget(inner)

        wrap = QWidget()
        wl = QHBoxLayout(wrap)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.setSpacing(0)
        wl.addWidget(area, 1)
        return wrap, area, inner

    def _clear_card_row(self, row: QWidget) -> None:
        """清空卡片流里所有 MovieCard。"""
        lay = row.layout()
        if lay is None:
            return
        while lay.count():
            item = lay.takeAt(0)
            wgt = item.widget()
            if wgt is not None:
                wgt.deleteLater()

    def _fill_card_row(self, row: QWidget, movies: List[Dict[str, Any]],
                       tag: str) -> List[MovieCard]:
        """把 movies 填充成一张卡片网格（v1.3.1：随机 6 部一行 / 相似 12 部两行）。"""
        self._clear_card_row(row)
        which = "similar" if tag == "similar" else "random"
        card_w, img_h = self._rec_metrics_for(which)
        cols = self.REC_SIMILAR_COLS if which == "similar" else self.REC_RANDOM_COLS
        cap = self.REC_SIMILAR_N if which == "similar" else self.REC_RANDOM_N
        cards: List[MovieCard] = []
        for m in movies[:cap]:
            card = MovieCard(
                m, self,
                on_select=self._on_recommend_card_selected,
                on_play=self._on_recommend_card_play,
                on_vote=self._on_recommend_card_vote)
            card.apply_size(card_w, img_h)
            card.resize_image()
            # 已有投票状态的卡片直接上色
            try:
                cur = self.store.get_vote(m["movie_id"])
                if cur:
                    card.set_vote_state(cur)
            except Exception:
                pass
            cards.append(card)
        self._reflow_cards(row, cards, cols)
        return cards

    # ------------------------------------------------------------------
    # 随机推荐
    # ------------------------------------------------------------------
    def recommend_refresh(self) -> None:
        """刷新「随机推荐」卡片流（后台线程算推荐，避免卡 UI）。"""
        if not hasattr(self, "btn_rec_refresh"):
            return
        self.btn_rec_refresh.setEnabled(False)
        self.lbl_rec_stat.setText("推荐计算中…")
        self._set_status("正在刷新随机推荐…")
        # v1.3.1：固定 6 部（用户要求），不再随窗口大小变化
        limit = int(self.REC_RANDOM_N)

        def _job() -> List[Dict[str, Any]]:
            return self._recommender().random_picks(limit=limit)

        self._run_worker(_Worker(_job), self._on_recommend_refreshed)

    def _on_recommend_refreshed(self, picks: List[Dict[str, Any]]) -> None:
        self.btn_rec_refresh.setEnabled(True)
        if not picks:
            self.lbl_rec_stat.setText("库内没有可用作品")
            self._set_status("随机推荐：无数据")
            return
        self.rec_random_cards = self._fill_card_row(self.rec_random_row, picks, "random")
        self._relayout_recommend()   # 填充后立即按实际空间铺满
        self._refresh_vote_stat()
        self._set_status("随机推荐已刷新（双击卡片播放；点击卡片看相似）")

    def _refresh_vote_stat(self) -> None:
        """刷新「已投 👍x · 👎y」统计标签（v1.3.0：可点击查看记录）。"""
        try:
            s = self.store.vote_summary()
        except Exception:
            s = {"up": 0, "down": 0}
        n = len(getattr(self, "rec_random_cards", []))
        self.lbl_rec_stat.setText(
            f"共 {n} 部　|　👍 {s['up']} · 👎 {s['down']}　📑查看记录")

    # ------------------------------------------------------------------
    # 投票记录管理（v1.3.0）
    # ------------------------------------------------------------------
    def open_vote_manager(self) -> None:
        """弹出投票记录窗口：查看 / 删除 👍👎 记录。"""
        dlg = VoteManagerDialog(self.store, self, on_changed=self._on_votes_changed)
        dlg.exec()

    def _on_votes_changed(self) -> None:
        """投票记录被删除后：刷新统计 + 重排推荐。"""
        self._refresh_vote_stat()
        # 同步当前卡片上的按钮状态（被删的作品恢复未投）
        try:
            voted = self.store.voted_ids()
        except Exception:
            voted = {}
        for cards in (getattr(self, "rec_random_cards", []),
                      getattr(self, "rec_similar_cards", [])):
            for card in cards:
                card.set_vote_state(voted.get(card.movie.get("movie_id"), 0))
        self._set_status("投票记录已更新，推荐会按新偏好调整")
        QTimer.singleShot(300, self.recommend_refresh)

    # ------------------------------------------------------------------
    # 相似推荐（基于选中）
    # ------------------------------------------------------------------
    def _on_recommend_card_selected(self, movie: Dict[str, Any]) -> None:
        """点击卡片 → 选中并刷新下方相似推荐。"""
        self.rec_selected_movie = movie
        # 视觉高亮
        for card in getattr(self, "rec_random_cards", []):
            card.set_selected(card.movie.get("movie_id") == movie.get("movie_id"))
        num = movie.get("num") or "（无番号）"
        title = (movie.get("title") or "")[:40]
        self.lbl_rec_similar.setText(
            f"🎯 与「{num} {title}」相似的作品（点击上方卡片可换一部）")
        self._set_status(f"正在计算与 {num} 相似的作品…")
        mid = movie.get("movie_id")
        limit = int(self.REC_SIMILAR_N)   # v1.3.1：固定 12 部，2 行展示

        def _job() -> List[Dict[str, Any]]:
            return self._recommender().similar_picks(mid, limit=limit)

        self._run_worker(_Worker(_job), self._on_recommend_similar_done)

    def _on_recommend_similar_done(self, picks: List[Dict[str, Any]]) -> None:
        if not picks:
            self.lbl_rec_similar.setText(
                "🎯 相关推荐 —— 该作品没有标签 / 演员信息，无法计算相似（点其他卡片试试）")
            self._clear_card_row(self.rec_similar_row)
            self.rec_similar_cards = []
            return
        self.rec_similar_cards = self._fill_card_row(self.rec_similar_row, picks, "similar")
        self._relayout_recommend()
        self._set_status(f"相关推荐已刷新（{len(self.rec_similar_cards)} 部，可上下滚动查看）")

    # ------------------------------------------------------------------
    # 播放 / 投票
    # ------------------------------------------------------------------
    def _on_recommend_card_play(self, movie: Dict[str, Any]) -> None:
        """双击卡片 → 播放视频（fallback 打开文件夹）。"""
        nfo = movie.get("path") or ""
        self._play_movie_video(nfo)

    def _on_recommend_card_vote(self, movie: Dict[str, Any], vote: int) -> None:
        """👍 / 👎 投票（toggle 语义）→ 落库 + 刷新卡片外观。"""
        try:
            final = self._recommender().vote(
                movie["movie_id"], movie.get("num") or "", vote)
        except Exception as exc:
            self._set_status(f"投票失败：{exc}")
            return
        # 刷新两张卡片流里同一部作品的按钮状态
        for cards in (getattr(self, "rec_random_cards", []),
                      getattr(self, "rec_similar_cards", [])):
            for card in cards:
                if card.movie.get("movie_id") == movie.get("movie_id"):
                    card.set_vote_state(final)
        num = movie.get("num") or ""
        if final == 0:
            self._set_status(f"已撤销对 {num} 的投票")
        elif final > 0:
            self._set_status(f"👍 已记录对 {num} 的喜欢（推荐会更贴合）")
        else:
            self._set_status(f"👎 已记录对 {num} 的不喜欢（会减少同类推荐）")
        # 投票后立即重算一批随机推荐（后台静默），体现「不断优化」
        QTimer.singleShot(300, self.recommend_refresh)

    # ==================================================================
    # ⑤ 重复检测（v1.1.0 核心新功能）
    # ==================================================================
    def _build_dedupe_tab(self) -> QWidget:
        w = QWidget()
        root = QVBoxLayout(w)

        tip = QLabel(
            "判定规则：番号（或「标题 + 年份」）相同即视为同一部作品；"
            "同一个文件夹内的多份 NFO 视为一部片子的多段（CD1/CD2、part1/part2），自动排除；"
            "只有分布在不同文件夹的同名作品，才会被判定为重复收藏。")
        tip.setWordWrap(True)
        tip.setProperty("hint", "true")
        root.addWidget(tip)

        bar = QHBoxLayout()
        self.cmb_dup_source = QComboBox()
        self.cmb_dup_source.setMinimumWidth(300)
        self.cmb_dup_mode = QComboBox()
        self.cmb_dup_mode.addItems(["自动（番号优先，缺失时用标题+年份）", "只用番号", "只用标题+年份"])
        self.cmb_dup_conf = QComboBox()
        self.cmb_dup_conf.addItems(["极高", "高", "中", "低"])
        self.cmb_dup_conf.setCurrentIndex(3)
        self.chk_show_multipart = QCheckBox("同时列出已排除的同目录分片")
        self.btn_dup = QPushButton("开始检测")
        self.btn_dup.setProperty("accent", "true")
        self.btn_dup.clicked.connect(self.run_dedupe)
        bar.addWidget(QLabel("数据源："))
        bar.addWidget(self.cmb_dup_source)
        bar.addWidget(QLabel("依据："))
        bar.addWidget(self.cmb_dup_mode)
        bar.addWidget(QLabel("最低置信度："))
        bar.addWidget(self.cmb_dup_conf)
        bar.addWidget(self.chk_show_multipart)
        bar.addStretch(1)
        bar.addWidget(self.btn_dup)
        root.addLayout(bar)

        self.dup_bar = QProgressBar()
        root.addWidget(self.dup_bar)

        self.lbl_dup_summary = QLabel("尚未检测")
        self.lbl_dup_summary.setProperty("hint", "true")
        self.lbl_dup_summary.setWordWrap(True)
        root.addWidget(self.lbl_dup_summary)

        self.dup_tree = QTreeWidget()
        self.dup_tree.setHeaderLabels(["标识 / 所在目录", "分辨率", "体积", "时长", "加入时间", "说明"])
        self.dup_tree.setColumnWidth(0, 560)
        self.dup_tree.setAlternatingRowColors(True)
        self.dup_tree.itemDoubleClicked.connect(lambda item, col: self._open_dup_folder())
        root.addWidget(self.dup_tree, 1)

        btns = QHBoxLayout()
        for text, slot in (
            ("导出重复清单…", self.export_dedupe),
            ("打开选中项所在文件夹", self._open_dup_folder),
            ("复制选中路径", self._copy_dup_path),
            ("展开 / 折叠全部", self._toggle_dup_tree),
        ):
            b = QPushButton(text)
            b.clicked.connect(slot)
            btns.addWidget(b)
        btns.addStretch(1)
        root.addLayout(btns)
        return w

    def run_dedupe(self) -> None:
        self.btn_dup.setEnabled(False)
        self.dup_tree.clear()
        self.dup_bar.setValue(0)
        self._set_status("正在检测重复影片…")
        source = self.cmb_dup_source.currentData() or None
        mode = self.cmb_dup_mode.currentIndex()
        min_conf = self.cmb_dup_conf.currentText()
        show_mp = self.chk_show_multipart.isChecked()
        use_num = mode in (0, 1)
        use_title = mode in (0, 2)

        worker = DedupeWorker(self.store, source, use_num, use_title, min_conf)
        worker.progressed.connect(lambda d, t, m: (
            self.dup_bar.setMaximum(max(t, 1)), self.dup_bar.setValue(d), self._set_status(m)))

        def _done(rep: Any) -> None:
            self.btn_dup.setEnabled(True)
            self.dup_bar.setValue(self.dup_bar.maximum())
            self.dup_report = rep
            self._fill_dup_tree(rep, show_mp)
            s = rep.summary()
            self.lbl_dup_summary.setText(
                f"扫描 {s['scanned']:,} 部作品 → 发现 {s['dup_groups']} 组重复"
                f"（涉及 {s['dup_movies']} 个 NFO，冗余 {s['redundant_copies']} 份，"
                f"可回收约 {s['redundant_text']}）；"
                f"同目录分片 {s['multipart_groups']} 组（{s['multipart_movies']} 个 NFO）已排除；"
                f"耗时 {s['elapsed']}s")
            self._set_status(f"重复检测完成：{s['dup_groups']} 组")

        self._run_worker(worker, _done)

    def _fill_dup_tree(self, rep: Any, show_multipart: bool) -> None:
        self.dup_tree.clear()
        for g in rep.groups:
            top = QTreeWidgetItem([
                f"{g.label}   （{g.copies} 份 · 冗余 {g.redundant_copies} 份 · 可回收 {g.redundant_text}）",
                "", g.total_text, "", "", f"{g.confidence}置信度：{g.note}",
            ])
            font = top.font(0)
            font.setBold(True)
            top.setFont(0, font)
            self.dup_tree.addTopLevelItem(top)
            for folder, members in g.by_folder.items():
                for m in members:
                    from .dedupe import human_duration, human_size
                    child = QTreeWidgetItem([
                        folder, m.resolution or "—", human_size(m.video_size),
                        human_duration(m.duration_sec), m.dateadded or "—",
                        m.nfo_name + (f"（{m.original_filename}）" if m.original_filename else ""),
                    ])
                    child.setData(0, Qt.ItemDataRole.UserRole, m.path)
                    child.setToolTip(0, m.path)
                    top.addChild(child)
            top.setExpanded(False)
        if show_multipart:
            for g in rep.multipart:
                top = QTreeWidgetItem([
                    f"{g.label}   （同目录 {len(g.members)} 段，已排除）", "",
                    g.total_text, "", "", g.folders[0] if g.folders else ""])
                top.setData(0, Qt.ItemDataRole.UserRole,
                            g.members[0].path if g.members else "")
                self.dup_tree.addTopLevelItem(top)

    def _current_dup_path(self) -> Optional[str]:
        item = self.dup_tree.currentItem()
        if not item:
            return None
        return item.data(0, Qt.ItemDataRole.UserRole) or None

    def _open_dup_folder(self) -> None:
        p = self._current_dup_path()
        if not p or not os.path.exists(p):
            QMessageBox.information(self, "提示", "请先在列表中选中一个具体的 NFO 条目（子行）。")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(p)))

    def _copy_dup_path(self) -> None:
        p = self._current_dup_path()
        if not p:
            return
        QApplication.clipboard().setText(p)
        self._set_status(f"已复制路径：{p}")

    def _toggle_dup_tree(self) -> None:
        expand = not self.dup_tree.topLevelItemCount() or not self.dup_tree.topLevelItem(0).isExpanded()
        for i in range(self.dup_tree.topLevelItemCount()):
            self.dup_tree.topLevelItem(i).setExpanded(expand)

    def export_dedupe(self) -> None:
        if self.dup_report is None:
            QMessageBox.information(self, "提示", "请先执行「开始检测」。")
            return
        d = QFileDialog.getExistingDirectory(
            self, "选择导出目录", os.path.join(self.out_dir, "重复检测"))
        if not d:
            return
        from .dedupe import export_all as dup_export
        try:
            files = dup_export(self.dup_report, d, formats=("csv", "json", "xlsx"))
        except Exception as exc:
            self._show_error(f"导出失败：{exc}")
            return
        self._set_status("重复清单已导出")
        QMessageBox.information(self, "导出完成", "已生成：\n" + "\n".join(
            f"{k}: {v}" for k, v in files.items()))
        QDesktopServices.openUrl(QUrl.fromLocalFile(d))

    # ==================================================================
    # ⑥ 导出与报告
    # ==================================================================
    def _build_export_tab(self) -> QWidget:
        w = QWidget()
        root = QVBoxLayout(w)
        gb = QGroupBox("导出格式")
        hl = QHBoxLayout(gb)
        self.chk_fmt = {}
        for key, text, default in (
            ("csv", "CSV 明细", True), ("json", "JSON 数据", True),
            ("xlsx", "XLSX 工作簿", True), ("md", "Markdown 摘要", True),
            ("html", "HTML 画像报告", True),
        ):
            cb = QCheckBox(text)
            cb.setChecked(default)
            self.chk_fmt[key] = cb
            hl.addWidget(cb)
        hl.addStretch(1)
        root.addWidget(gb)

        opt = QHBoxLayout()
        self.spin_export_limit = QSpinBox()
        self.spin_export_limit.setRange(0, 500000)
        self.spin_export_limit.setSingleStep(1000)
        self.spin_export_limit.setValue(0)
        self.spin_export_limit.setSpecialValueText("全部")
        self.spin_report_movies = QSpinBox()
        self.spin_report_movies.setRange(100, 20000)
        self.spin_report_movies.setSingleStep(500)
        self.spin_report_movies.setValue(1000)
        self.btn_export = QPushButton("开始导出")
        self.btn_export.setProperty("accent", "true")
        self.btn_export.clicked.connect(self.run_export)
        opt.addWidget(QLabel("明细条数："))
        opt.addWidget(self.spin_export_limit)
        opt.addWidget(QLabel("报告内嵌作品数："))
        opt.addWidget(self.spin_report_movies)
        opt.addStretch(1)
        opt.addWidget(self.btn_export)
        root.addLayout(opt)

        self.export_log = QTextBrowser()
        self.export_log.setMinimumHeight(220)
        root.addWidget(self.export_log, 1)

        btns = QHBoxLayout()
        b1 = QPushButton("打开输出目录")
        b1.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(self.out_dir)))
        b2 = QPushButton("打开最近一份 HTML 报告")
        b2.clicked.connect(self.open_last_report)
        btns.addWidget(b1)
        btns.addWidget(b2)
        btns.addStretch(1)
        root.addLayout(btns)
        return w

    def run_export(self) -> None:
        fmts = [k for k, cb in self.chk_fmt.items() if cb.isChecked()]
        if not fmts:
            QMessageBox.information(self, "未选择格式", "请至少勾选一种导出格式。")
            return
        self.btn_export.setEnabled(False)
        self._set_status("正在导出…")
        limit = self.spin_export_limit.value()
        report_movies = self.spin_report_movies.value()
        store = self.store

        def _job() -> Dict[str, Any]:
            from .analyze import Analyzer
            from .exporter import export_all
            with store.lock():
                data = self.last_data or Analyzer(store).build_report_data(
                    with_cooccurrence=True, with_keywords=True)
                return export_all(
                    data, os.path.join(self.out_dir, "导出"), store=store,
                    formats=[f for f in fmts if f != "html"], html="html" in fmts,
                    movie_limit=limit, report_movies=report_movies,
                    source_label=self.db_path)

        def _done(res: Dict[str, Any]) -> None:
            self.btn_export.setEnabled(True)
            lines = [f"导出时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                     f"输出目录：{os.path.join(self.out_dir, '导出')}", ""]
            for k, v in res.items():
                if isinstance(v, list):
                    lines.append(f"[{k}]")
                    lines.extend(f"    {x}" for x in v)
                else:
                    lines.append(f"[{k}] {v}")
            self.export_log.setPlainText("\n".join(lines))
            self._set_status("导出完成")

        self._run_worker(_Worker(_job), _done)

    def open_last_report(self) -> None:
        for p in (os.path.join(self.out_dir, "用户画像报告.html"),
                  os.path.join(self.out_dir, "导出", "用户画像报告.html")):
            if os.path.exists(p):
                QDesktopServices.openUrl(QUrl.fromLocalFile(p))
                return
        QMessageBox.information(self, "没有报告", "还没有生成过 HTML 报告，请先导出或生成。")

    # ==================================================================
    # ⑦ AI 分析（v1.1.3 三栏：相似推荐 / 标签聚类 / 画像解读）
    # ==================================================================
    def _build_ai_tab(self) -> QWidget:
        """v1.1.3 → v1.1.4：AI 引擎开关放在顶部；3 页结果保留。"""
        w = QWidget()
        root = QVBoxLayout(w)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        # 顶部 AI 引擎状态条 + 开关按钮（v1.1.4 新增）
        ai_bar = QHBoxLayout()
        ai_bar.setSpacing(6)
        self.lbl_ai_state = QLabel("AI 引擎：探测中…")
        self.lbl_ai_state.setProperty("hint", "true")
        self.lbl_ai_state.setStyleSheet(
            "padding:6px 10px; background:#171a1f; border-radius:4px; "
            "border:1px solid #2c3038;")
        ai_bar.addWidget(self.lbl_ai_state, 1)

        self.btn_ai_enable = QPushButton("🚀 开启 AI")
        self.btn_ai_enable.setProperty("accent", "true")
        self.btn_ai_enable.setToolTip(
            "开启后后台会探测：Ollama（强）→ MiniLM ONNX（语义向量）→ 内置启发式（兜底）")
        self.btn_ai_enable.clicked.connect(self._ai_enable)

        self.btn_ai_disable = QPushButton("⏹ 关闭")
        self.btn_ai_disable.setToolTip("关闭 AI 增强，回到纯本地-零依赖模式")
        self.btn_ai_disable.clicked.connect(self._ai_disable)
        self.btn_ai_disable.setEnabled(False)

        self.btn_ai_redetect = QPushButton("🔄 自检")
        self.btn_ai_redetect.setToolTip("重新探测本地可用的 AI 后端")
        self.btn_ai_redetect.clicked.connect(self._ai_redetect)
        self.btn_ai_redetect.setEnabled(False)

        ai_bar.addWidget(self.btn_ai_enable)
        ai_bar.addWidget(self.btn_ai_disable)
        ai_bar.addWidget(self.btn_ai_redetect)
        root.addLayout(ai_bar)

        # v1.3.0：分析入口重做 —— 输入区 / 动作区 / 参数区分栏，聚类粒度可调
        gb = QGroupBox("🎯 分析入口")
        gl = QGridLayout(gb)
        gl.setSpacing(8)
        gl.setContentsMargins(10, 10, 10, 10)

        self.ed_ai_num = QLineEdit()
        self.ed_ai_num.setPlaceholderText("输入番号（如 ABC-123），按回车直接做相似推荐")
        self.ed_ai_num.returnPressed.connect(self.ai_similar)

        self.btn_ai_similar = QPushButton("🔍 相似作品推荐")
        self.btn_ai_similar.setProperty("accent", "true")
        self.btn_ai_similar.setToolTip("基于标签 + 演员 Jaccard 相似度找最像的 N 部")
        self.btn_ai_similar.clicked.connect(self.ai_similar)

        self.btn_ai_clusters = QPushButton("🧩 综合主题聚类")
        self.btn_ai_clusters.setToolTip(
            "结合标签 + 标题高频词做贪心主题聚类；大簇会自动再细分出二级子主题")
        self.btn_ai_clusters.clicked.connect(self.ai_clusters)

        self.btn_ai_interpret = QPushButton("📊 生成画像解读")
        self.btn_ai_interpret.setToolTip("由本地启发式（或本地 Ollama）生成自然语言画像解读")
        self.btn_ai_interpret.clicked.connect(self.ai_interpret)

        self.btn_ai_export = QPushButton("💾 导出聚类结果")
        self.btn_ai_export.setToolTip("把当前聚类结果导出为文本 / CSV")
        self.btn_ai_export.clicked.connect(self._save_ai_clusters)

        # 第一行：番号 + 相似推荐
        gl.addWidget(QLabel("目标番号："), 0, 0)
        gl.addWidget(self.ed_ai_num, 0, 1)
        gl.addWidget(self.btn_ai_similar, 0, 2)
        # 第二行：聚类 + 解读 + 导出
        gl.addWidget(self.btn_ai_clusters, 1, 1)
        gl.addWidget(self.btn_ai_interpret, 1, 2)
        gl.addWidget(self.btn_ai_export, 2, 2)

        # 聚类参数（v1.3.0：粒度可调，越细 = 主题越多）
        para = QHBoxLayout()
        para.setSpacing(8)
        self.spin_clu_topn = QSpinBox()
        self.spin_clu_topn.setRange(20, 300)
        self.spin_clu_topn.setValue(120)
        self.spin_clu_topn.setPrefix("候选词 ")
        self.spin_clu_topn.setToolTip("参与聚类的高频标签 / 标题词数量，越多主题越细")
        self.spin_clu_minco = QSpinBox()
        self.spin_clu_minco.setRange(1, 50)
        self.spin_clu_minco.setValue(3)
        self.spin_clu_minco.setPrefix("最小共现 ")
        self.spin_clu_minco.setToolTip("两个词至少在多少部作品里同时出现才算关联（调大 → 主题更纯）")
        self.spin_clu_minsize = QSpinBox()
        self.spin_clu_minsize.setRange(0, 5000)
        self.spin_clu_minsize.setValue(30)
        self.spin_clu_minsize.setPrefix("最小作品数 ")
        self.spin_clu_minsize.setToolTip("低于该作品数的主题直接丢弃（调大 → 只看大主题）")
        self.chk_clu_title = QCheckBox("含标题词")
        self.chk_clu_title.setChecked(True)
        self.chk_clu_title.setToolTip("把标题高频词一起纳入聚类（标签打得不全时很有用）")
        for wgt in (self.spin_clu_topn, self.spin_clu_minco,
                    self.spin_clu_minsize, self.chk_clu_title):
            para.addWidget(wgt)
        para.addStretch(1)
        gl.addLayout(para, 2, 0, 1, 2)

        hint = QLabel(
            "提示：聚类 / 解读 不依赖番号；相似推荐必填。AI 增强未开启也能跑——"
            "本软件内置「本地启发式」AI 后端，零依赖、零网络。")
        hint.setProperty("hint", "true")
        hint.setWordWrap(True)
        gl.addWidget(hint, 3, 0, 1, 3)
        root.addWidget(gb)

        # 三页结果
        self.ai_tabs = QTabWidget()
        self.ai_tabs.addTab(self._build_ai_similar_tab(), "🔍 相似作品推荐")
        self.ai_tabs.addTab(self._build_ai_clusters_tab(), "🧩 综合主题聚类")
        self.ai_tabs.addTab(self._build_ai_interpret_tab(), "📊 画像解读")
        root.addWidget(self.ai_tabs, 1)

        # 触发一次后台状态探测，更新 lbl_ai_state + 按钮启用状态
        QTimer.singleShot(50, self._refresh_ai_state)
        return w

    def _build_ai_similar_tab(self) -> QWidget:
        """相似推荐：QTableWidget + 双击打开文件夹。"""
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 6, 0, 0)
        v.setSpacing(6)
        self.lbl_ai_similar_summary = QLabel("请输入番号后点击「相似作品推荐」。")
        self.lbl_ai_similar_summary.setProperty("hint", "true")
        self.lbl_ai_similar_summary.setWordWrap(True)
        v.addWidget(self.lbl_ai_similar_summary)

        self.ai_similar_table = QTableWidget(0, 9)
        self.ai_similar_table.setHorizontalHeaderLabels(
            ["#", "番号", "标题", "片商", "评分", "相似度", "共享标签", "共享演员", "路径"])
        self.ai_similar_table.setShowGrid(False)
        self.ai_similar_table.setAlternatingRowColors(True)
        self.ai_similar_table.verticalHeader().setVisible(False)
        self.ai_similar_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.ai_similar_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.ai_similar_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.ai_similar_table.setColumnHidden(0, True)  # row no
        self.ai_similar_table.setColumnHidden(8, True)  # path
        self.ai_similar_table.itemDoubleClicked.connect(self._open_ai_similar_folder)
        self.ai_similar_table.itemSelectionChanged.connect(self._update_ai_similar_buttons)
        v.addWidget(self.ai_similar_table, 1)

        bh = QHBoxLayout()
        self.btn_ai_sim_open = QPushButton("📂 打开选中文件夹")
        self.btn_ai_sim_open.setEnabled(False)
        self.btn_ai_sim_open.clicked.connect(self._open_ai_similar_folder)
        self.btn_ai_sim_copy = QPushButton("📋 复制番号")
        self.btn_ai_sim_copy.setEnabled(False)
        self.btn_ai_sim_copy.clicked.connect(self._copy_ai_similar_num)
        self.chk_ai_sim_showpath = QCheckBox("显示「路径」列")
        self.chk_ai_sim_showpath.toggled.connect(
            lambda c: self.ai_similar_table.setColumnHidden(8, not c))
        bh.addWidget(self.btn_ai_sim_open)
        bh.addWidget(self.btn_ai_sim_copy)
        bh.addWidget(self.chk_ai_sim_showpath)
        bh.addStretch(1)
        v.addLayout(bh)
        return w

    def _build_ai_clusters_tab(self) -> QWidget:
        """标签聚类：QTreeWidget。"""
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 6, 0, 0)
        v.setSpacing(6)
        self.lbl_ai_clu_summary = QLabel("请点击「标签主题聚类」。")
        self.lbl_ai_clu_summary.setProperty("hint", "true")
        self.lbl_ai_clu_summary.setWordWrap(True)
        v.addWidget(self.lbl_ai_clu_summary)

        self.ai_clu_tree = QTreeWidget()
        self.ai_clu_tree.setHeaderLabels(
            ["主题 / 统计 / 二级子主题 / 关联标签·标题词 / 代表作品", "类别", "规模"])
        self.ai_clu_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.ai_clu_tree.setAlternatingRowColors(True)
        # v1.1.4：双击「代表作品」节点直接打开对应 NFO 文件夹
        self.ai_clu_tree.itemDoubleClicked.connect(self._on_cluster_tree_double_clicked)
        v.addWidget(self.ai_clu_tree, 1)

        bh = QHBoxLayout()
        b1 = QPushButton("全部展开")
        b1.clicked.connect(self.ai_clu_tree.expandAll)
        b2 = QPushButton("全部折叠")
        b2.clicked.connect(self.ai_clu_tree.collapseAll)
        b3 = QPushButton("📋 复制为文本")
        b3.clicked.connect(self._copy_ai_clusters_text)
        bh.addWidget(b1)
        bh.addWidget(b2)
        bh.addWidget(b3)
        bh.addStretch(1)
        v.addLayout(bh)
        return w

    def _build_ai_interpret_tab(self) -> QWidget:
        """画像解读：QTextBrowser。"""
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 6, 0, 0)
        v.setSpacing(6)
        self.lbl_ai_interp_meta = QLabel("后端：—　生成状态：未生成")
        self.lbl_ai_interp_meta.setProperty("hint", "true")
        v.addWidget(self.lbl_ai_interp_meta)
        self.ai_interp_browser = QTextBrowser()
        v.addWidget(self.ai_interp_browser, 1)
        bh = QHBoxLayout()
        b1 = QPushButton("📋 复制全文")
        b1.clicked.connect(self._copy_ai_interp)
        b2 = QPushButton("💾 导出到文件…")
        b2.clicked.connect(self._save_ai_interp)
        bh.addWidget(b1)
        bh.addWidget(b2)
        bh.addStretch(1)
        v.addLayout(bh)
        return w

    def _refresh_ai_state(self) -> None:
        """探测 / 刷新本地 AI 引擎状态，并写入状态条 + 同步按钮启用态。"""
        try:
            ai = self._ai_engine()
            s = ai.status()
            state = s.get("state", "")
            enabled = s.get("enabled", False)
            backend = s.get("backend", "none")
            caps = s.get("capabilities") or []
            # 状态文案（暗色背景上的提示）
            if enabled and state == "ready" and backend not in (None, "none"):
                msg = (f"🤖 AI 增强已开启　|　后端：{backend}　|　能力：{', '.join(caps) or '-'}"
                       f"　|　{s.get('message', '')}")
            elif enabled and state == "initializing":
                msg = "⏳ AI 后端正在探测（后台线程）…"
            elif not enabled:
                msg = ("🔕 AI 增强未开启　|　点击「🚀 开启 AI」启用 "
                       f"（内置本地启发式兜底，{ai.backend or 'idle'}）")
            else:
                msg = f"ℹ️ AI 状态：{s.get('message', state)}"
            self.lbl_ai_state.setText(msg)

            # 同步按钮启用态
            if hasattr(self, "btn_ai_enable"):
                self.btn_ai_enable.setEnabled(not enabled)
                self.btn_ai_disable.setEnabled(enabled)
                self.btn_ai_redetect.setEnabled(enabled)
            # 没启动时，再排一次轮询：等用户点开启就不用再排
            if enabled and state == "initializing":
                QTimer.singleShot(800, self._refresh_ai_state)
        except Exception as exc:
            self.lbl_ai_state.setText(f"AI 引擎状态获取失败：{exc}")

    # ------------------------------------------------------------------
    # AI 引擎开关（v1.1.4）
    # ------------------------------------------------------------------
    def _ai_enable(self) -> None:
        try:
            ai = self._ai_engine()
            ai.set_enabled(True)
        except Exception as exc:
            self._show_error(f"开启 AI 失败：{exc}")
            return
        self.btn_ai_enable.setEnabled(False)
        self.btn_ai_disable.setEnabled(True)
        self.btn_ai_redetect.setEnabled(True)
        self._refresh_ai_state()
        self._set_status("AI 增强已开启，后台正在探测…")

    def _ai_disable(self) -> None:
        try:
            ai = self._ai_engine()
            ai.set_enabled(False)
        except Exception as exc:
            self._show_error(f"关闭 AI 失败：{exc}")
            return
        self.btn_ai_enable.setEnabled(True)
        self.btn_ai_disable.setEnabled(False)
        self.btn_ai_redetect.setEnabled(False)
        self._refresh_ai_state()
        self._set_status("AI 增强已关闭，回到本地启发式")

    def _ai_redetect(self) -> None:
        """手动重新探测后端：先关再开，让后台线程再跑一遍 _detect。"""
        ai = self._ai_engine()
        ai.set_enabled(False)
        QTimer.singleShot(100, self._ai_enable)

    def _ai_engine(self):
        if not hasattr(self, "_ai"):
            from .ai_engine import AIEngine
            self._ai = AIEngine()
        return self._ai

    def ai_similar(self) -> None:
        num = self.ed_ai_num.text().strip()
        if not num:
            QMessageBox.information(self, "请输入番号", "请先填写一个番号，例如 ABC-123。")
            return
        self._set_status("正在计算相似作品…")
        self.lbl_ai_similar_summary.setText(f"目标番号：{num}　|　正在计算相似度…")
        self.ai_similar_table.setRowCount(0)
        self.ai_tabs.setCurrentIndex(0)  # 自动切到结果页
        self._run_worker(_Worker(lambda: self._ai_engine().similar_works(
            self.store, num, limit=15)), self._on_ai_similar)

    def _on_ai_similar(self, rows: List[Dict[str, Any]]) -> None:
        if not rows:
            self.lbl_ai_similar_summary.setText(
                "没有找到相似作品（该番号不存在，或它没有标签 / 演员信息）。")
            self.ai_similar_table.setRowCount(0)
            self._update_ai_similar_buttons()
            return
        self.ai_similar_table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            num = r.get("num", "")
            title = r.get("title", "") or ""
            if len(str(title)) > 50:
                title = str(title)[:50] + "…"
            studio = r.get("studio", "") or ""
            try:
                rating = f"{float(r.get('userrating') or 0):.1f}"
            except Exception:
                rating = ""
            try:
                score = f"{float(r.get('score') or 0):.3f}"
            except Exception:
                score = ""
            path = r.get("path", "") or ""
            cells = [str(i + 1), num, title, studio, rating, score,
                     str(r.get("shared_tags", 0)), str(r.get("shared_actors", 0)), path]
            for c, v in enumerate(cells):
                item = QTableWidgetItem(str(v))
                if c == 0:  # 序号列额外存 path
                    item.setData(Qt.ItemDataRole.UserRole, path)
                elif c == 8:
                    item.setData(Qt.ItemDataRole.UserRole, path)
                self.ai_similar_table.setItem(i, c, item)
        # 隐藏的序号列存 path 便于双击定位
        first = self.ai_similar_table.item(0, 0)
        if first is not None:
            first.setData(Qt.ItemDataRole.UserRole, rows[0].get("path", "") or "")
        self.lbl_ai_similar_summary.setText(
            f"目标番号：{self.ed_ai_num.text().strip()}　|　找到 {len(rows)} 部相似作品"
            f"（双击行打开文件夹）")
        self._set_status("相似作品计算完成")
        self._update_ai_similar_buttons()

    def _update_ai_similar_buttons(self) -> None:
        has = bool(self._ai_similar_selected_paths())
        self.btn_ai_sim_open.setEnabled(has)
        self.btn_ai_sim_copy.setEnabled(has)

    def _ai_similar_selected_paths(self) -> List[str]:
        paths: List[str] = []
        seen = set()
        for r in sorted({i.row() for i in self.ai_similar_table.selectedItems()}):
            it = self.ai_similar_table.item(r, 0)
            if not it:
                continue
            p = it.data(Qt.ItemDataRole.UserRole)
            if p and p not in seen:
                seen.add(p)
                paths.append(p)
        return paths

    def _ai_similar_selected_num(self) -> Optional[str]:
        rows = sorted({i.row() for i in self.ai_similar_table.selectedItems()})
        if not rows:
            return None
        it = self.ai_similar_table.item(rows[0], 1)
        return it.text() if it else None

    def _open_ai_similar_folder(self) -> None:
        paths = self._ai_similar_selected_paths()
        # 双击信号触发时 selectedItems 可能为空，尝试从传入的 item 取
        if not paths:
            paths = [it.data(Qt.ItemDataRole.UserRole) for it in [
                self.ai_similar_table.item(self.ai_similar_table.currentRow(), 0)
            ] if it and it.data(Qt.ItemDataRole.UserRole)]
        opened = 0
        for p in paths:
            if p and os.path.exists(p):
                QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(p)))
                opened += 1
        if opened:
            self._set_status(f"已打开 {opened} 个文件夹")
        else:
            self._set_status("没有可打开的路径")

    def _copy_ai_similar_num(self) -> None:
        n = self._ai_similar_selected_num()
        if not n:
            return
        QApplication.clipboard().setText(n)
        self._set_status(f"已复制番号：{n}")

    def ai_clusters(self) -> None:
        # v1.3.0：粒度来自界面参数（候选词 / 最小共现 / 最小作品数 / 含标题词）
        top_n = int(getattr(self, "spin_clu_topn", None) and self.spin_clu_topn.value() or 120)
        min_co = int(getattr(self, "spin_clu_minco", None) and self.spin_clu_minco.value() or 3)
        min_size = int(getattr(self, "spin_clu_minsize", None) and self.spin_clu_minsize.value() or 30)
        with_title = bool(getattr(self, "chk_clu_title", None) and self.chk_clu_title.isChecked())
        self._set_status("正在做综合主题聚类（标签 + 标题词，含二级细分）…")
        self.ai_tabs.setCurrentIndex(1)
        self.lbl_ai_clu_summary.setText("正在聚类…（标签 + 标题高频词联合分析）")
        self._run_worker(_Worker(lambda: self._ai_engine().cluster_themes(
            self.store, top_n=top_n, min_co=min_co, with_title_terms=with_title,
            limit_representatives=5, min_size=min_size)),
            self._on_ai_clusters)

    def _on_ai_clusters(self, clusters: List[Dict[str, Any]]) -> None:
        """v1.3.0 渲染：组合主题名 → 二级子主题 → 关联标签(带强度) → 标题词
        → 簇统计 → 代表作品（双击打开文件夹）。"""
        self.ai_clu_tree.clear()
        self._last_clusters = clusters
        if not clusters:
            self.lbl_ai_clu_summary.setText("暂无标签/标题词数据（可试试调小「最小作品数」）。")
            return
        n_tag = sum(len(c.get("members", [])) for c in clusters)
        n_title = sum(len(c.get("title_terms", [])) for c in clusters)
        n_sub = sum(len(c.get("children", []) or []) for c in clusters)
        for c in clusters:
            theme = c.get("label") or c.get("theme", "")
            size = int(c.get("size", 0))
            kind = c.get("kind", "tag")
            share = float(c.get("share", 0) or 0)
            members = c.get("members", []) or []
            details = c.get("member_details") or []
            tterms = c.get("title_terms", []) or []
            subs = c.get("children") or []
            reps = c.get("representatives", []) or []
            icon = "🏷️" if kind == "tag" else "💬"
            top = QTreeWidgetItem([
                f"{icon} {theme}",
                "主题",
                f"{size} 部 · {share * 100:.1f}%"])
            font = top.font(0)
            font.setBold(True)
            top.setFont(0, font)
            top.setToolTip(0, f"主题：{theme}\n作品 {size} 部，占全库 {share * 100:.1f}%")
            self.ai_clu_tree.addTopLevelItem(top)

            # -- 簇统计 --
            stat_bits = [f"作品 {size} 部", f"占比 {share * 100:.1f}%"]
            if c.get("avg_rating"):
                stat_bits.append(f"均分 {c['avg_rating']}")
            if c.get("year_range"):
                yr = c["year_range"]
                stat_bits.append(f"{yr[0]}–{yr[-1]} 年")
            stat_node = QTreeWidgetItem([f"　├ 📊 {'　|　'.join(stat_bits)}", "统计", ""])
            stat_node.setForeground(0, QBrush(QColor("#e0af68")))
            top.addChild(stat_node)

            # -- 二级子主题（v1.3.0） --
            if subs:
                sub_node = QTreeWidgetItem([f"　├ 🔸 二级子主题（{len(subs)}）",
                                            "子组", str(len(subs))])
                sub_node.setForeground(0, QBrush(QColor("#f7768e")))
                top.addChild(sub_node)
                for ch in subs:
                    ch_txt = (f"　　{ch.get('label', '')}"
                              f"（{ch.get('size', 0)} 部 · "
                              f"{(ch.get('share') or 0) * 100:.1f}%）")
                    ch_item = QTreeWidgetItem([ch_txt, "子主题", ""])
                    ch_item.setForeground(0, QBrush(QColor("#ff9e64")))
                    sub_node.addChild(ch_item)
                    for r in (ch.get("representatives") or []):
                        rp = QTreeWidgetItem([
                            f"　　　{r.get('num', '')}: {(r.get('title') or '')[:26]}",
                            "代表", ""])
                        rp.setForeground(0, QBrush(QColor("#c0caf5")))
                        rp.setData(0, Qt.ItemDataRole.UserRole, r.get("path", "") or "")
                        rp.setToolTip(0, (r.get("path") or "无路径") + "（双击打开）")
                        ch_item.addChild(rp)

            # -- 关联标签（带共现强度） --
            if members:
                tags_node = QTreeWidgetItem([f"　├ 关联标签（{len(members)}）",
                                             "子组", str(len(members))])
                tags_node.setForeground(0, QBrush(QColor("#8ab4f8")))
                top.addChild(tags_node)
                det = {d.get("name"): d for d in details if d.get("kind") == "tag"}
                for m in members:
                    d = det.get(m) or {}
                    txt = f"　　{m}"
                    if d:
                        txt += f"（共现 {d.get('co', 0)} 部 · 强度 {d.get('strength', 0)}）"
                    tags_node.addChild(QTreeWidgetItem([txt, "成员", ""]))
            # -- 关联标题词 --
            if tterms:
                tt_node = QTreeWidgetItem([f"　├ 关联标题词（{len(tterms)}）",
                                           "子组", str(len(tterms))])
                tt_node.setForeground(0, QBrush(QColor("#bb9af7")))
                top.addChild(tt_node)
                for t in tterms:
                    tt_node.addChild(QTreeWidgetItem([f"　　{t}", "标题词", ""]))
            # -- 代表作品（双击可打开） --
            if reps:
                rep_node = QTreeWidgetItem([f"　└ 代表作品（{len(reps)}）",
                                            "子组", str(len(reps))])
                rep_node.setForeground(0, QBrush(QColor("#9ece6a")))
                top.addChild(rep_node)
                for r in reps:
                    rep_path = r.get("path", "") or ""
                    label = (r.get("num", "") or "") + ": " + (r.get("title", "") or "")[:30]
                    it = QTreeWidgetItem([f"　　{label}",
                                          f"评分 {r.get('rating', 0)}", ""])
                    it.setForeground(0, QBrush(QColor("#c0caf5")))
                    it.setData(0, Qt.ItemDataRole.UserRole, rep_path)
                    it.setToolTip(0, rep_path or "无路径信息")
                    rep_node.addChild(it)
            top.setExpanded(False)
        self.lbl_ai_clu_summary.setText(
            f"共 {len(clusters)} 个主题　|　覆盖 {n_tag} 个标签 + {n_title} 个标题词"
            f"　|　二级子主题 {n_sub} 个"
            f"　|　调「候选词 / 最小共现 / 最小作品数」可改变粗细粒度")
        self._set_status("综合主题聚类完成")

    def _save_ai_clusters(self) -> None:
        """v1.3.0：把聚类结果导出为文本文件。"""
        clusters = getattr(self, "_last_clusters", None) or []
        if not clusters:
            QMessageBox.information(self, "无数据", "请先点击「🧩 综合主题聚类」。")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出聚类结果", os.path.join(self.out_dir, "综合主题聚类.txt"),
            "文本文件 (*.txt);;CSV 文件 (*.csv)")
        if not path:
            return
        try:
            if path.lower().endswith(".csv"):
                with open(path, "w", encoding="utf-8-sig", newline="") as fh:
                    wr = csv.writer(fh)
                    wr.writerow(["主题", "作品数", "占比(%)", "平均评分",
                                 "年份区间", "关联标签", "关联标题词", "二级子主题"])
                    for c in clusters:
                        wr.writerow([
                            c.get("label") or c.get("theme", ""), c.get("size", 0),
                            round((c.get("share") or 0) * 100, 2), c.get("avg_rating", ""),
                            "-".join(str(y) for y in (c.get("year_range") or [])),
                            "、".join(c.get("members") or []),
                            "、".join(c.get("title_terms") or []),
                            "；".join(ch.get("label", "") for ch in (c.get("children") or [])),
                        ])
            else:
                lines: List[str] = ["综合主题聚类报告", "=" * 50, ""]
                for i, c in enumerate(clusters, 1):
                    lines.append(
                        f"{i}. {c.get('label') or c.get('theme','')}"
                        f"（{c.get('size',0)} 部 / {(c.get('share') or 0)*100:.1f}%"
                        f" / 均分 {c.get('avg_rating','-')}）")
                    if c.get("members"):
                        lines.append("   关联标签：" + "、".join(c["members"]))
                    if c.get("title_terms"):
                        lines.append("   关联标题词：" + "、".join(c["title_terms"]))
                    for ch in (c.get("children") or []):
                        lines.append(f"   - 子主题：{ch.get('label','')}（{ch.get('size',0)} 部）")
                    for r in (c.get("representatives") or []):
                        lines.append(f"   代表：{r.get('num','')} {r.get('title','')[:30]}")
                    lines.append("")
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write("\n".join(lines))
        except OSError as exc:
            self._show_error(f"导出失败：{exc}")
            return
        self._set_status(f"已导出聚类结果：{path}")

    def _copy_ai_clusters_text(self) -> None:
        if self.ai_clu_tree.topLevelItemCount() == 0:
            return
        lines = []
        for i in range(self.ai_clu_tree.topLevelItemCount()):
            top = self.ai_clu_tree.topLevelItem(i)
            theme = top.text(0)
            members: List[str] = []
            tt: List[str] = []
            rep: List[str] = []
            for j in range(top.childCount()):
                child = top.child(j)
                if "关联标签" in child.text(0):
                    members = [child.child(k, 0).text().strip()
                              for k in range(child.childCount())]
                elif "关联标题词" in child.text(0):
                    tt = [child.child(k, 0).text().strip()
                          for k in range(child.childCount())]
                elif "代表作品" in child.text(0):
                    rep = [child.child(k, 0).text().strip()
                           for k in range(child.childCount())]
            bit = f"【{theme}】"
            if members:
                bit += " 标签：" + " / ".join(members)
            if tt:
                bit += " 标题词：" + " / ".join(tt)
            lines.append(bit)
            if rep:
                lines.append("    代表：" + "  |  ".join(rep))
        QApplication.clipboard().setText("\n".join(lines))
        self._set_status(f"已复制 {len([l for l in lines if l.startswith('【')])} 个主题到剪贴板")

    def _on_cluster_tree_double_clicked(self, item: QTreeWidgetItem, _col: int) -> None:
        """双击 cluster 树节点：若为「代表作品」子节点（path 在 UserRole），打开文件夹。"""
        if not item:
            return
        path = item.data(0, Qt.ItemDataRole.UserRole)
        if path and os.path.exists(path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(path)))
            self._set_status(f"已打开：{os.path.dirname(path)}")

    def ai_interpret(self) -> None:
        self._set_status("正在生成画像解读…")
        self.ai_tabs.setCurrentIndex(2)
        self.lbl_ai_interp_meta.setText("后端：—　生成状态：计算中…")
        self.ai_interp_browser.clear()
        self._run_worker(_Worker(lambda: self._ai_engine().interpret(self.store)),
                         self._on_ai_interpret)

    def _on_ai_interpret(self, res: Dict[str, Any]) -> None:
        backend = res.get("backend", "—")
        text = res.get("text", "")
        html = (
            f"<div style='font-size:14px; line-height:1.7;'>"
            f"<p style='color:#8ab4f8; font-size:16px; font-weight:bold;'>"
            f"🧭 用户画像解读</p>"
            f"<p style='color:#8b939f; font-size:12px;'>后端：<b>{backend}</b></p>"
            f"<hr/>"
            f"<p>{text.replace(chr(10), '<br/>')}</p>"
            f"<hr/>"
            f"<p style='color:#6b7280; font-size:11px;'>{COPYRIGHT_NOTICE}</p>"
            f"</div>"
        )
        self.ai_interp_browser.setHtml(html)
        self.lbl_ai_interp_meta.setText(
            f"后端：{backend}　|　生成状态：完成 {datetime.now().strftime('%H:%M:%S')}")
        self._set_status("画像解读完成")

    def _copy_ai_interp(self) -> None:
        txt = self.ai_interp_browser.toPlainText().strip()
        if not txt:
            return
        QApplication.clipboard().setText(txt)
        self._set_status("已复制解读文本到剪贴板")

    def _save_ai_interp(self) -> None:
        text = self.ai_interp_browser.toPlainText().strip()
        if not text:
            QMessageBox.information(self, "没有内容", "请先生成画像解读。")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "保存画像解读", os.path.join(self.out_dir, "画像解读.txt"),
            "文本文件 (*.txt);;Markdown (*.md)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text + "\n\n" + COPYRIGHT_NOTICE + "\n")
        except Exception as exc:
            self._show_error(f"保存失败：{exc}")
            return
        self._set_status(f"已保存到：{path}")
        QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(path)))

    # ==================================================================
    # ⑧ 关于与声明
    # ==================================================================
    def _build_about_tab(self) -> QWidget:
        w = QWidget()
        root = QVBoxLayout(w)
        title = QLabel(APP_NAME)
        title.setStyleSheet("font-size:26px; font-weight:bold; color:#8ab4f8;")
        root.addWidget(title)
        root.addWidget(QLabel(f"版本：{version_text()}　作者：{__author__}"))
        root.addWidget(QLabel(f"内部版本号规则：YYMMDD + 当日迭代号（当前 {__build__}）"))

        box = QFrame()
        box.setProperty("card", "true")
        vl = QVBoxLayout(box)
        cp = QLabel("版权与使用限制声明")
        cp.setStyleSheet("font-weight:bold; color:#ffd479;")
        vl.addWidget(cp)
        decl = QLabel(
            f"{COPYRIGHT_NOTICE}\n\n"
            "· 本软件为个人学习交流项目，禁止用于任何商业用途、禁止二次分发获利；\n"
            "· 本软件只在本地运行，不上传任何数据，不访问外部网络（可选 AI 能力仅调用本机 Ollama）；\n"
            "· 使用者应自行遵守所在地法律法规，作者不对使用行为及结果承担任何责任。")
        decl.setWordWrap(True)
        vl.addWidget(decl)
        root.addWidget(box)

        info = QTextBrowser()
        info.setHtml(self._about_html())
        root.addWidget(info, 1)

        btns = QHBoxLayout()
        b1 = QPushButton("打开输出目录")
        b1.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(self.out_dir)))
        b2 = QPushButton("打开数据库目录")
        b2.clicked.connect(lambda: QDesktopServices.openUrl(
            QUrl.fromLocalFile(os.path.dirname(self.db_path))))
        btns.addWidget(b1)
        btns.addWidget(b2)
        btns.addStretch(1)
        root.addLayout(btns)
        return w

    def _about_html(self) -> str:
        return f"""
        <h3 style="color:#8ab4f8">功能一览</h3>
        <ul>
          <li><b>数据源与扫描</b>：多目录 / 多盘符管理，多进程增量扫描，实时进度。</li>
          <li><b>画像概览</b>：作品数、容量、时长、评分等指标 + 任意维度 TopN 统计。</li>
          <li><b>作品明细</b>：番号 / 标题 / 片商 / 导演关键词检索，分页浏览。</li>
          <li><b>重复检测</b>（v1.1.0 新增）：跨目录重复影片识别，同目录分片自动排除，可导出清单。</li>
          <li><b>导出与报告</b>：CSV / JSON / XLSX / Markdown / 单文件 HTML 画像报告。</li>
          <li><b>AI 分析</b>：标签主题聚类、相似作品推荐、画像解读（离线启发式，可选 Ollama）。</li>
        </ul>
        <h3 style="color:#8ab4f8">技术实现</h3>
        <ul>
          <li>解析与统计：<b>Python 标准库</b>（xml / re / sqlite3 / multiprocessing），零第三方依赖即可完成核心流程。</li>
          <li>桌面界面：<b>PySide6 (Qt 6)</b> 原生窗口，耗时任务全部在 QThread 子线程执行。</li>
          <li>存储：<b>SQLite（WAL）</b>，按 (mtime, size) 指纹做增量扫描。</li>
          <li>可选依赖：openpyxl（XLSX 导出）、onnxruntime + MiniLM / Ollama（AI 增强）。</li>
        </ul>
        <h3 style="color:#8ab4f8">版本</h3>
        <p>对外版本 v{__version__}　内部版本 {__build__}（YYMMDD + 当日迭代号）</p>
        <p style="color:#8b939f">{COPYRIGHT_NOTICE}</p>
        """

    # -- 生命周期 ------------------------------------------------------
    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        running = [w for w in self._workers if w.isRunning()]
        if running:
            ans = QMessageBox.question(
                self, "仍有任务在运行",
                f"有 {len(running)} 个后台任务尚未结束，确定要退出吗？")
            if ans != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        try:
            self.store.close()
        except Exception:
            pass
        event.accept()


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def run_gui(db_path: Optional[str] = None, out_dir: Optional[str] = None) -> int:
    """启动桌面界面（阻塞直到窗口关闭）。"""
    if QApplication.instance() is None:
        QApplication(sys.argv)
    app = QApplication.instance()
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    app.setStyleSheet(QSS)
    font = QFont("Microsoft YaHei UI", 9)
    app.setFont(font)

    win = MainWindow(db_path=db_path, out_dir=out_dir)
    win.show()
    return app.exec()


__all__ = ["run_gui", "MainWindow"]
