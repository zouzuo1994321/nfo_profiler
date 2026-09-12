# -*- coding: utf-8 -*-
"""v1.3.5 图标诊断：读运行中窗口的真实 HICON（WM_GETICON / GCLP_HICON）。

用法::

    python -u tools/probe_icon.py [窗口标题关键字]

判据（对应 win-app-icon-debug 技能）：
* WM_GETICON 三档（ICON_SMALL2/SMALL/BIG）全 NULL → 程序没设窗口图标，
  任务栏回落窗口类图标（Qt 默认 = IDI_APPLICATION 白底窗口）；
* 任一档非 NULL → 程序设了图标，问题在别处（尺寸档/缓存）。

::

    Copyright © 2026 肆月Aperture 本软件不得用于商业用途，仅做学习交流使用。
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

KEYWORD = sys.argv[1] if len(sys.argv) > 1 else "NFO"

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

WM_GETICON, SMTO_ABORTIFHUNG = 0x007F, 0x0002
GCLP_HICON, GCLP_HICONSM = -14, -34
ICON_SMALL, ICON_BIG, ICON_SMALL2 = 0, 1, 2

HP = ctypes.c_void_p
user32.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                       wintypes.LPARAM, wintypes.UINT, wintypes.UINT,
                                       ctypes.POINTER(HP)]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetClassLongPtrW.restype = HP
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetClassLongPtrW.restype = HP
user32.GetClassLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]

EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
found = []


def _cb(hwnd, lparam):
    if not user32.IsWindowVisible(hwnd):
        return True
    n = user32.GetWindowTextLengthW(hwnd)
    if not n:
        return True
    b = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, b, n + 1)
    if KEYWORD in b.value:
        found.append((hwnd, b.value))
    return True


def main() -> int:
    user32.EnumWindows(EnumProc(_cb), 0)
    if not found:
        print(f"[FAIL] 没找到标题含「{KEYWORD}」的可见窗口")
        return 1
    ok = False
    for hwnd, title in found[:3]:
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        print(f"窗口 0x{hwnd:X}　pid={pid.value}　class={cls.value!r}　title={title!r}")
        null_cnt = 0
        for which, label in ((ICON_SMALL2, "SMALL2"), (ICON_BIG, "BIG"),
                             (ICON_SMALL, "SMALL")):
            res = HP()
            user32.SendMessageTimeoutW(hwnd, WM_GETICON, which, 0,
                                       SMTO_ABORTIFHUNG, 1000, ctypes.byref(res))
            state = "NULL" if not res.value else f"0x{res.value:X}"
            if not res.value:
                null_cnt += 1
            print(f"  WM_GETICON {label:6} -> {state}")
        small = user32.GetClassLongPtrW(hwnd, GCLP_HICONSM)
        big = user32.GetClassLongPtrW(hwnd, GCLP_HICON)
        small_s = "NULL" if not small else hex(small or 0)
        big_s = "NULL" if not big else hex(big or 0)
        print(f"  类图标 GCLP_HICONSM -> {small_s}　GCLP_HICON -> {big_s}")
        if null_cnt == 3 and not small.value and not big.value:
            print("  ==> 结论：程序没有设置窗口图标，任务栏用的是系统默认类图标")
        elif null_cnt == 3:
            print("  ==> 结论：只有类图标（无 WM_GETICON）——Qt setWindowIcon 未生效")
        else:
            ok = True
            print("  ==> 结论：程序设置了窗口图标（链路正常）")
        print()
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
