# -*- coding: utf-8 -*-
"""扫描终止哨兵功能验证脚本。

场景：1500 个 NFO / 4 进程 / parse_batch=40，0.5s 后写入哨兵，期望扫描在
1s 内停止并保留已处理的记录。
"""

import os
import sys
import time
import tempfile
import shutil
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nfo_profiler.scanner import scan_paths, default_stop_sentinel
from nfo_profiler.store import Store


def main() -> int:
    base = os.path.abspath("output/_stop_nfo2")
    shutil.rmtree(base, ignore_errors=True)
    os.makedirs(base)
    for i in range(1500):
        d = os.path.join(base, f"M{i:04d}")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"M{i:04d}.nfo"), "w", encoding="utf-8") as f:
            f.write(
                f'<?xml version="1.0"?><movie><num>M{i:04d}</num><title>t{i}</title></movie>')
    print("created 1500 NFO")

    db = "output/_stop_test2.db"
    for ext in ("", "-wal", "-shm"):
        try:
            os.remove(db + ext)
        except OSError:
            pass
    store = Store(db)
    sentinel = default_stop_sentinel()

    triggered = [False]

    def progress(phase, done, total, msg):
        # 已有成果后再触发停止
        if (not triggered[0]) and phase == "parse" and done >= 100:
            triggered[0] = True
            open(sentinel, "w").close()
            print(f"sentinel written at {time.time() - t0:.2f}s after {done} files done")

    t0 = time.time()
    res = scan_paths(
        [base], store, workers=4, incremental=False, probe_video=False,
        stop_sentinel=sentinel, parse_batch=40,
        progress=progress,
    )
    dur = time.time() - t0
    print(f"result: processed={res.added + res.updated} stopped={res.stopped} "
          f"duration={dur:.2f}s")
    print(f"sentinel cleaned: exists={os.path.exists(sentinel)}")

    for ext in ("", "-wal", "-shm"):
        try:
            os.remove(db + ext)
        except OSError:
            pass
    shutil.rmtree(base, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())