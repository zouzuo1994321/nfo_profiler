# -*- coding: utf-8 -*-
"""启动闪屏（v1.5.0）—— 打开软件前显示 logo + 应用名 + 载入进度 + 版权。

设计要点
--------
* **自绘**：``QSplashScreen`` 只提供底图，标题 / 进度条 / 提示 / 版权全部在
  ``drawContents`` 里用 ``QPainter`` 画，避免嵌控件带来的布局与缩放问题；
* **淡入淡出**：``QPropertyAnimation`` 动画 ``windowOpacity``（0→1 淡入、1→0 淡出），
  淡出结束再 ``finish(win)``，主窗口出现时闪屏同步消失；
* **真实进度**：``setProgress(pct, tip)`` 更新后立刻 ``repaint()`` +
  ``QApplication.processEvents()``，保证主线程构建 UI 期间画面也能刷新
  （否则进度条会“冻结”）；
* **低侵入**：``MainWindow`` 通过 ``progress_cb`` 回调上报各阶段进度；
  设为环境变量 ``NFO_NO_SPLASH=1`` 可关闭闪屏（离屏测试 / 无 GUI 环境用）。

::

    Copyright © 2026 肆月Aperture 本软件不得用于商业用途，仅做学习交流使用。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import (QEasingCurve, QPropertyAnimation, QRect, QRectF, Qt,
                            QTimer)
from PySide6.QtGui import (QColor, QFont, QLinearGradient, QPainter, QPainterPath,
                           QPixmap)
from PySide6.QtWidgets import QApplication, QSplashScreen

from . import APP_NAME, COPYRIGHT_NOTICE, version_text

#: 闪屏尺寸（设备像素无关，Qt 逻辑像素）
SPLASH_W = 520
SPLASH_H = 330

_BG = QColor("#14171c")
_PANEL = QColor("#1b1e23")
_BORDER = QColor("#2c313a")
_TEXT = QColor("#e8ecf1")
_HINT = QColor("#9aa4b2")
_TRACK = QColor("#2c313a")
_BAR_A = QColor("#ff5fb8")
_BAR_B = QColor("#a63a78")


class SplashScreen(QSplashScreen):
    """logo + 应用名 + 进度条 + 版权的启动闪屏。"""

    def __init__(self, title: str = APP_NAME,
                 copyright_text: str = COPYRIGHT_NOTICE,
                 width: int = SPLASH_W, height: int = SPLASH_H) -> None:
        pix = QPixmap(width, height)
        pix.fill(Qt.GlobalColor.transparent)
        super().__init__(pix, Qt.WindowType.WindowStaysOnTopHint)
        self._title = title
        self._copyright = copyright_text
        self._pct = 0
        self._tip = "正在启动…"
        self._logo: Optional[QPixmap] = self._load_logo()
        self.setWindowFlags(Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(width, height)

    # ------------------------------------------------------------------
    # 资源
    # ------------------------------------------------------------------
    def _load_logo(self) -> Optional[QPixmap]:
        """载入 logo.png（源码 / PyInstaller 冻结双模式，找不到返回 None）。"""
        try:
            from .gui import find_data_file   # 延迟导入，避免与 gui 循环依赖
        except Exception:
            return None
        p = find_data_file("logo.png")
        if not p:
            return None
        pm = QPixmap(p)
        if pm.isNull():
            return None
        return pm.scaled(132, 132, Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)

    # ------------------------------------------------------------------
    # 进度
    # ------------------------------------------------------------------
    def setProgress(self, pct: int, tip: str = "") -> None:
        """更新进度（0~100）与提示文字，并立即重绘 + 处理事件。"""
        self._pct = max(0, min(100, int(pct)))
        if tip:
            self._tip = tip
        self.repaint()
        app = QApplication.instance()
        if app is not None:
            app.processEvents()

    def progress(self) -> int:
        return self._pct

    # ------------------------------------------------------------------
    # 绘制
    # ------------------------------------------------------------------
    def drawContents(self, painter: QPainter) -> None:  # noqa: N802（Qt 覆写）
        w, h = self.width(), self.height()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        # 面板（圆角 + 描边）
        path = QPainterPath()
        path.addRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 14.0, 14.0)
        painter.fillPath(path, _BG)
        painter.setPen(_BORDER)
        painter.drawPath(path)

        # logo（居中偏上）
        logo = self._logo
        if logo is not None and not logo.isNull():
            lx = (w - logo.width()) // 2
            painter.drawPixmap(lx, 34, logo)

        # 应用名
        painter.setPen(_TEXT)
        f_title = QFont("Microsoft YaHei UI", 21)
        f_title.setBold(True)
        painter.setFont(f_title)
        painter.drawText(QRect(0, 182, w, 34), Qt.AlignmentFlag.AlignCenter,
                         self._title)

        # 进度条
        bar_x, bar_w, bar_h = 60, w - 120, 8
        bar_y = 232
        track = QPainterPath()
        track.addRoundedRect(QRectF(bar_x, bar_y, bar_w, bar_h), 4.0, 4.0)
        painter.fillPath(track, _TRACK)
        fill_w = max(0.0, bar_w * self._pct / 100.0)
        if fill_w > 0:
            grad = QLinearGradient(bar_x, bar_y, bar_x + bar_w, bar_y)
            grad.setColorAt(0.0, _BAR_B)
            grad.setColorAt(1.0, _BAR_A)
            filled = QPainterPath()
            filled.addRoundedRect(QRectF(bar_x, bar_y, fill_w, bar_h), 4.0, 4.0)
            painter.fillPath(filled, grad)

        # 提示 + 百分比
        painter.setPen(_HINT)
        f_hint = QFont("Microsoft YaHei UI", 9)
        painter.setFont(f_hint)
        painter.drawText(QRect(bar_x, bar_y + 14, bar_w - 60, 20),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         self._tip)
        painter.drawText(QRect(bar_x + bar_w - 60, bar_y + 14, 60, 20),
                         Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                         f"{self._pct}%")

        # 版权（底部小字）
        painter.setPen(QColor("#6f7885"))
        f_cp = QFont("Microsoft YaHei UI", 8)
        painter.setFont(f_cp)
        painter.drawText(QRect(0, h - 30, w, 20),
                         Qt.AlignmentFlag.AlignCenter, self._copyright)

    # ------------------------------------------------------------------
    # 淡入淡出
    # ------------------------------------------------------------------
    def fade_in(self, ms: int = 620) -> None:
        """淡入（0 → 1）。"""
        self._animate(0.0, 1.0, ms)

    def fade_out(self, win=None, ms: int = 380) -> None:
        """淡出（1 → 0），结束后 ``finish(win)`` 把焦点交给主窗口。"""
        anim = self._animate(1.0, 0.0, ms)
        if anim is None:
            self._finish(win)
            return

        def _done() -> None:
            self._finish(win)

        anim.finished.connect(_done)

    def _animate(self, start: float, end: float, ms: int):
        """windowOpacity 动画；失败（无动画环境）时直接设为终值并返回 None。"""
        try:
            self.setWindowOpacity(start)
            anim = QPropertyAnimation(self, b"windowOpacity")
            anim.setDuration(max(1, int(ms)))
            anim.setStartValue(start)
            anim.setEndValue(end)
            anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
            anim.start()
            # 持有引用，避免被 GC 后动画提前终止
            self._anim = anim
            return anim
        except Exception:
            self.setWindowOpacity(end)
            return None

    def _finish(self, win=None) -> None:
        try:
            self.close()
        except Exception:
            pass
        try:
            if win is not None:
                self.finish(win)   # QSplashScreen.finish：主窗口绘制完成后关闭
        except Exception:
            pass


__all__ = ["SplashScreen", "SPLASH_W", "SPLASH_H"]
