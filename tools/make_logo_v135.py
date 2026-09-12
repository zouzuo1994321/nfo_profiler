# -*- coding: utf-8 -*-
"""v1.3.5：把根目录 logo.png / logo.ico 换成 logo2.png（多尺寸 ico）。

用法::

    python -S -u tools/make_logo_v135.py

::

    Copyright © 2026 肆月Aperture 本软件不得用于商业用途，仅做学习交流使用。
"""

from __future__ import annotations

import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "logo2.png")
PNG = os.path.join(ROOT, "logo.png")
ICO = os.path.join(ROOT, "logo.ico")
HIST = os.path.join(ROOT, "history")
SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def main() -> int:
    if not os.path.isfile(SRC):
        print(f"[FAIL] 找不到源图 {SRC}")
        return 1

    # 1) 备份旧 logo（仅首次）
    os.makedirs(HIST, exist_ok=True)
    for old in (PNG, ICO):
        if os.path.isfile(old):
            dst = os.path.join(HIST, "logo_old_v134" + os.path.splitext(old)[1])
            if not os.path.exists(dst):
                shutil.copy2(old, dst)
                print(f"[备份] {old} -> {dst}")

    # 2) 替换 logo.png
    shutil.copy2(SRC, PNG)
    print(f"[OK] {PNG} <- logo2.png（{os.path.getsize(PNG)} 字节）")

    # 3) 生成多尺寸 ico（优先 Pillow；没有则现场装）
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        print("[..] 共享 venv 缺 Pillow，正在安装…")
        rc = os.system(f'"{sys.executable}" -m pip install -q pillow')
        if rc != 0:
            print("[FAIL] Pillow 安装失败")
            return 1
    from PIL import Image
    img = Image.open(SRC).convert("RGBA")
    img.save(ICO, format="ICO", sizes=SIZES)
    print(f"[OK] {ICO} <- {img.size[0]}x{img.size[1]} 共 {len(SIZES)} 尺寸"
          f"（{os.path.getsize(ICO)} 字节）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
