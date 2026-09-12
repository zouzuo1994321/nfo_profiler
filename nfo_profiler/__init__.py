"""NFO 画像矿工 —— 从海量 KODI/tinyMediaManager NFO 中提取有效信息并生成用户画像。

核心仅依赖 Python 标准库；导出 XLSX 需要 openpyxl（可选）；
v1.1.0 起的桌面界面（双击 exe 直接用）需要 PySide6。

版本规则
--------
* ``__version__``：对外大版本，语义化 ``主.次.修订``（如 1.1.0）；
* ``__build__``：内部版本号，规则 ``YYMMDD + 当日迭代号(4 位)``，
  例 ``2609060001`` = 2026-09-06 的第 1 次构建；同一天再次构建顺延为 0002。
"""

__version__ = "1.3.6"
#: 内部版本号：YYMMDD + 当日迭代号（4 位）
__build__ = "2609120004"
__author__ = "肆月Aperture"

APP_NAME = "NFO 画像矿工"
APP_CODE = "nfo-profiler"

COPYRIGHT_NOTICE = "Copyright © 2026 肆月Aperture 本软件不得用于商业用途，仅做学习交流使用。"


def version_text() -> str:
    """界面展示用的完整版本串，如 ``v1.1.0 (Build 2609060001)``。"""
    return f"v{__version__} (Build {__build__})"
