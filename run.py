# -*- coding: utf-8 -*-
"""NFO 画像矿工 —— 程序入口。

    python run.py scan   <目录>      扫描 NFO 入库（默认增量）
    python run.py report             生成可视化 HTML 画像报告
    python run.py export             导出 CSV / JSON / XLSX / Markdown / HTML
    python run.py dedupe             重复影片检测（跨目录，同目录分片自动排除）
    python run.py ui                 启动界面
    python run.py stats              查看数据库概况
    python run.py reindex            用当前同义词表重建归一化键

双击打包后的 exe（不带任何参数）= 直接打开**原生桌面窗口**（PySide6 / Qt），
不再需要浏览器、不再占用端口。旧版「本地 HTTP + 浏览器」界面仍可通过
``nfo_profiler.exe --web`` 或 ``nfo_profiler.exe ui --web`` 调用，仅为兼容保留。

Windows 多进程要求入口脚本受 __main__ 保护，这里即为此存在。

::

    Copyright © 2026 肆月Aperture 本软件不得用于商业用途，仅做学习交流使用。
"""

import multiprocessing
import sys

from nfo_profiler.cli import main

#: 走旧版浏览器界面的开关
_WEB_FLAGS = {"--web", "webui"}


def _launch_gui() -> int:
    from nfo_profiler.gui import run_gui

    return run_gui()


def _launch_web() -> int:
    from nfo_profiler.webui import serve

    serve(port=9527, open_browser=True)
    return 0


if __name__ == "__main__":
    # PyInstaller 打包后，多进程（ProcessPoolExecutor）子进程会重新执行本 exe，
    # 必须先调用 freeze_support() 才能正确引导子进程（Windows 上必需）。
    multiprocessing.freeze_support()

    args = sys.argv[1:]
    if not args:
        # 双击启动：直接进入原生桌面窗口
        sys.exit(_launch_gui())
    if args[0] in _WEB_FLAGS:
        sys.exit(_launch_web())
    if args[0] == "ui" and "--web" not in args:
        # `run.py ui` 默认也是桌面窗口（cli.cmd_ui 里已同样处理，这里提前拦截
        # 只为少走一遍控制台初始化）
        sys.exit(_launch_gui())
    sys.exit(main())
