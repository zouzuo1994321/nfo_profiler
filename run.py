# -*- coding: utf-8 -*-
"""NFO 画像矿工 —— 命令行入口。

    python run.py scan   <目录>      扫描 NFO 入库（默认增量）
    python run.py report             生成可视化 HTML 画像报告
    python run.py export             导出 CSV / JSON / XLSX / Markdown / HTML
    python run.py ui                 启动本地 Web 界面
    python run.py stats              查看数据库概况
    python run.py reindex            用当前同义词表重建归一化键

双击打包后的 exe（不带任何参数）= 直接进入 Web 模式，浏览器自动打开到
http://127.0.0.1:9527，无需记忆命令。

Windows 多进程要求入口脚本受 __main__ 保护，这里即为此存在。
"""

import multiprocessing
import sys

from nfo_profiler.cli import main

if __name__ == "__main__":
    # PyInstaller 打包后，多进程（ProcessPoolExecutor）子进程会重新执行本 exe，
    # 必须先调用 freeze_support() 才能正确引导子进程（Windows 上必需）。
    multiprocessing.freeze_support()

    # 双击启动：没有任何参数时直接进入 Web 模式（端口 9527，自动开浏览器）。
    if len(sys.argv) <= 1:
        from nfo_profiler.webui import serve
        serve(port=9527, open_browser=True)
    else:
        sys.exit(main())
