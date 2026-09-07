# -*- coding: utf-8 -*-
"""重复影片检测 —— 基于 NFO 元数据判断「同一部片子在不同目录里多存了一份」。

判定思路（对应需求）
--------------------
1. **身份键**：优先用番号（``num``，缺失时回退到 ``original_filename`` / 标题开头），
   归一化成 ``ddk173`` 这种「字母+数字」形式；没有番号的用
   ``标题(去掉开头番号) + 年份`` 作为身份键。
2. **跨目录才算重复**：把成员按「所在文件夹」归并——
   * 同一文件夹里的多份 NFO：几乎都是**一部片子被切成多段**（CD1/CD2、part1/part2），
     属于正常情况，**不计入重复**，只在结果里单列为「已排除·同目录分片」；
   * 分布在**不同文件夹**的身份相同项：判定为**重复收藏**，需要报告出来。
3. **冗余空间**：同一组的每个文件夹算「一份」，保留最大的那一份，
   其余份数的体积之和即为可回收空间（分片体积按文件夹内累加）。
4. **置信度**：番号一致 = 高；标题+年份一致 = 中；仅有标题 = 低；
   若各份体积/时长完全一致，再上调一级（极高）。

本模块零第三方依赖（XLSX 导出为可选，需 openpyxl）。

::

    Copyright © 2026 肆月Aperture 本软件不得用于商业用途，仅做学习交流使用。
"""

from __future__ import annotations

import csv
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

COPYRIGHT_NOTICE = "Copyright © 2026 肆月Aperture 本软件不得用于商业用途，仅做学习交流使用。"


# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------

#: 标题里只保留 中/日/英/数字 作为比较键
_NON_KEY_RE = re.compile(r"[^0-9a-z\u4e00-\u9fff\u3040-\u30ff]+")
#: 标题开头的番号（如 "DDK-173 関西弁の…"）→ 去掉，避免番号不同的片子被标题前缀误伤
_HEAD_NUM_RE = re.compile(r"^\s*[\[\(【]?\s*[a-z]{1,8}\s*[-_]?\s*\d{2,6}\s*[\]\)】]?\s*[-:：\s]*")
#: 从文件名 / 标题里提取番号
_NUM_RE = re.compile(r"([a-z]{2,10})\s*[-_]?\s*(\d{2,6})(?!\d)")


def norm_num(value: Any) -> str:
    """把番号归一化成 ``abc123`` 形式；无法识别时返回空串。"""
    s = str(value or "").strip().lower()
    if not s:
        return ""
    s = re.sub(r"[^0-9a-z]+", "", s)
    # 至少要有 字母+数字 才算有效番号（纯数字/纯字母的置信度太低，交回给标题键）
    if not s or not re.match(r"^[a-z]+\d+$", s):
        return ""
    return s


def _num_from_text(text: Any) -> str:
    """从文件名 / 标题里提取番号键（优先整体归一化，回退到「字母+数字」正则）。"""
    s = str(text or "").strip().lower()
    if not s:
        return ""
    base = os.path.splitext(os.path.basename(s))[0]
    key = norm_num(base)
    if key:
        return key
    m = _NUM_RE.search(s)
    if m:
        return f"{m.group(1)}{m.group(2)}"
    return ""


def extract_num(*candidates: Any) -> str:
    """从番号字段 / 视频文件名 / 标题里提取番号键。

    注意一个历史坑：部分 NFO 的 ``num`` 字段只存了**前缀**（如 ``T38``，
    而真实番号是 ``T38-002``）。若直接用它当身份键，会把同一前缀下的几十部
    不同作品误判成重复。这里做一次「细化」：当番号键的数字部分长度 <= 2
    （高度疑似只有前缀）且文件名/标题能给出更长的同源键时，采用更长的那个。
    """
    cands = [str(c or "").strip() for c in candidates]
    num = cands[0] if cands else ""
    k_num = norm_num(num)
    if k_num:
        m = re.match(r"^([a-z]+)(\d{1,2})$", k_num)
        if m:  # 疑似只有前缀 → 用文件名 / 标题补全
            for c in cands[1:]:
                k = _num_from_text(c)
                if k and k.startswith(k_num) and len(k) > len(k_num):
                    return k
        return k_num
    for c in cands[1:]:
        k = _num_from_text(c)
        if k:
            return k
    return ""


def norm_title(value: Any) -> str:
    """标题比较键：去掉开头番号与所有标点/空白，仅保留文字与数字。"""
    s = str(value or "").strip().lower()
    if not s:
        return ""
    s = _HEAD_NUM_RE.sub("", s)
    s = _NON_KEY_RE.sub("", s)
    return s


def title_key(title: Any, clean_title: Any, year: Any) -> str:
    """标题 + 年份 身份键（无年份时退化为纯标题）。"""
    t = norm_title(clean_title) or norm_title(title)
    if not t:
        return ""
    y = str(year or "").strip()
    if y.isdigit() and len(y) == 4:
        return f"{t}::{y}"
    return f"{t}::"


def human_size(n: int) -> str:
    """字节 → 人类可读（GB / MB）。"""
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        return "0 B"
    if n <= 0:
        return "—"
    gb = n / 1073741824.0
    if gb >= 1:
        return f"{gb:.2f} GB"
    mb = n / 1048576.0
    if mb >= 1:
        return f"{mb:.1f} MB"
    return f"{n / 1024.0:.0f} KB"


def human_duration(sec: int) -> str:
    try:
        sec = int(sec or 0)
    except (TypeError, ValueError):
        return "—"
    if sec <= 0:
        return "—"
    h, m = divmod(sec // 60, 60)
    return f"{h}小时{m}分" if h else f"{m}分"


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class DupMember:
    """重复组里的一个成员（= 一个 NFO / 一段视频）。"""
    movie_id: int = 0
    path: str = ""
    folder: str = ""
    nfo_name: str = ""
    num: str = ""
    title: str = ""
    year: int = 0
    resolution: str = ""
    video_size: int = 0
    duration_sec: int = 0
    dateadded: str = ""
    source: str = ""
    original_filename: str = ""

    @property
    def size(self) -> int:
        return int(self.video_size or 0)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "movie_id": self.movie_id, "path": self.path, "folder": self.folder,
            "nfo_name": self.nfo_name, "num": self.num, "title": self.title,
            "year": self.year, "resolution": self.resolution,
            "video_size": self.video_size, "size_text": human_size(self.video_size),
            "duration_sec": self.duration_sec, "duration_text": human_duration(self.duration_sec),
            "dateadded": self.dateadded, "source": self.source,
            "original_filename": self.original_filename,
        }


@dataclass
class DupGroup:
    """一个重复组：身份键相同、且分布在 >= 2 个不同文件夹。"""
    key: str = ""
    kind: str = "num"                 # num | title
    label: str = ""
    confidence: str = "高"            # 极高 / 高 / 中 / 低
    members: List[DupMember] = field(default_factory=list)
    note: str = ""
    total_bytes: int = 0
    redundant_bytes: int = 0

    #: 按文件夹归并后的「份」信息：{folder: [members]}
    by_folder: Dict[str, List[DupMember]] = field(default_factory=dict)

    @property
    def copies(self) -> int:
        """有效份数（同目录的多段合并算一份）。"""
        return len(self.by_folder)

    @property
    def redundant_copies(self) -> int:
        return max(0, self.copies - 1)

    @property
    def folders(self) -> List[str]:
        return list(self.by_folder.keys())

    @property
    def total_text(self) -> str:
        return human_size(self.total_bytes)

    @property
    def redundant_text(self) -> str:
        return human_size(self.redundant_bytes)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key, "kind": self.kind, "label": self.label,
            "confidence": self.confidence, "note": self.note,
            "copies": self.copies, "redundant_copies": self.redundant_copies,
            "total_bytes": self.total_bytes, "total_text": self.total_text,
            "redundant_bytes": self.redundant_bytes, "redundant_text": self.redundant_text,
            "folders": self.folders,
            "members": [m.as_dict() for m in self.members],
        }


@dataclass
class DupReport:
    """一次重复检测的完整结果。"""
    groups: List[DupGroup] = field(default_factory=list)
    #: 同目录多份 NFO（视为一部片子的多段，已排除出重复统计）
    multipart: List[DupGroup] = field(default_factory=list)
    scanned: int = 0
    elapsed: float = 0.0
    generated_at: str = ""

    def summary(self) -> Dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "scanned": self.scanned,
            "dup_groups": len(self.groups),
            "dup_movies": sum(len(g.members) for g in self.groups),
            "redundant_copies": sum(g.redundant_copies for g in self.groups),
            "redundant_bytes": sum(g.redundant_bytes for g in self.groups),
            "redundant_text": human_size(sum(g.redundant_bytes for g in self.groups)),
            "multipart_groups": len(self.multipart),
            "multipart_movies": sum(len(g.members) for g in self.multipart),
            "elapsed": round(self.elapsed, 2),
        }

    def as_dict(self) -> Dict[str, Any]:
        return {
            "summary": self.summary(),
            "groups": [g.as_dict() for g in self.groups],
            "multipart": [g.as_dict() for g in self.multipart],
            "copyright": COPYRIGHT_NOTICE,
        }


# ---------------------------------------------------------------------------
# 检测器
# ---------------------------------------------------------------------------

class DuplicateFinder:
    """基于 NFO 元数据的跨目录重复检测器。

    Parameters
    ----------
    store:
        已打开的 :class:`~nfo_profiler.store.Store`。
    source:
        限定数据源根目录；为空表示全库。
    """

    def __init__(self, store: Any, source: Optional[str] = None) -> None:
        self.store = store
        self.source = source or ""

    # -- 取数 ----------------------------------------------------------
    def _rows(self) -> List[Dict[str, Any]]:
        if hasattr(self.store, "dup_candidates"):
            return list(self.store.dup_candidates(source=self.source or None))
        # 兜底：直接用连接查询（兼容任何 Store 实现）
        sql = ("SELECT id, path, num, num_prefix, title, clean_title, originaltitle, "
               "original_filename, year, resolution, video_size, duration_sec, "
               "dateadded, source, filesize FROM movies")
        params: List[Any] = []
        if self.source:
            sql += " WHERE source = ?"
            params.append(self.source)
        return [dict(r) for r in self.store.conn.execute(sql, params)]

    # -- 主流程 --------------------------------------------------------
    def scan(
        self,
        *,
        use_num: bool = True,
        use_title: bool = True,
        min_confidence: str = "低",
        progress: Optional[Any] = None,
    ) -> DupReport:
        """执行一次重复检测，返回 :class:`DupReport`。"""
        t0 = time.time()
        rows = self._rows()
        report = DupReport(scanned=len(rows),
                           generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        if progress:
            try:
                progress(0, len(rows), "读取作品元数据…")
            except Exception:
                pass

        #: key -> 成员列表
        buckets: Dict[Tuple[str, str], List[DupMember]] = {}
        for i, r in enumerate(rows):
            path = r.get("path") or ""
            num = r.get("num") or ""
            num_key = extract_num(num, r.get("original_filename") or "",
                                  r.get("title") or "") if use_num else ""
            if num_key:
                kind, key = "num", num_key
            elif use_title:
                kind, key = "title", title_key(r.get("title"), r.get("clean_title"), r.get("year"))
            else:
                continue
            if not key:
                continue
            m = DupMember(
                movie_id=int(r.get("id") or 0),
                path=path,
                folder=os.path.dirname(path),
                nfo_name=os.path.basename(path),
                num=num, title=(r.get("title") or r.get("clean_title") or ""),
                year=int(r.get("year") or 0),
                resolution=r.get("resolution") or "",
                video_size=int(r.get("video_size") or 0),
                duration_sec=int(r.get("duration_sec") or 0),
                dateadded=r.get("dateadded") or "",
                source=r.get("source") or "",
                original_filename=r.get("original_filename") or "",
            )
            buckets.setdefault((kind, key), []).append(m)
            if progress and (i + 1) % 5000 == 0:
                try:
                    progress(i + 1, len(rows), f"已比对 {i + 1}/{len(rows)} 部作品…")
                except Exception:
                    pass

        rank = {"极高": 3, "高": 2, "中": 1, "低": 0}
        min_rank = rank.get(min_confidence, 0)
        groups: List[DupGroup] = []
        multipart: List[DupGroup] = []

        for (kind, key), members in buckets.items():
            if len(members) < 2:
                continue
            by_folder: Dict[str, List[DupMember]] = {}
            for m in members:
                by_folder.setdefault(m.folder, []).append(m)

            first = members[0]
            if first.num and norm_num(first.num) == key:
                label = first.num
            elif kind == "num":
                label = (key or "").upper() or (first.title[:36] if first.title else key)
            else:
                label = first.title[:36] if first.title else key
            g = DupGroup(key=key, kind=kind, label=label, members=sorted(
                members, key=lambda x: (-x.size, x.path)), by_folder=by_folder)
            g.total_bytes = sum(m.size for m in members)
            biggest = max(sum(m.size for m in v) for v in by_folder.values())
            g.redundant_bytes = max(0, g.total_bytes - biggest)
            g.confidence, g.note = self._judge(kind, members, by_folder)

            if rank.get(g.confidence, 0) < min_rank:
                continue
            if len(by_folder) <= 1:
                # 同一个文件夹内的多份 NFO：一部片子的多段，排除
                g.note = "同目录多份 NFO，判定为分片（多段），已排除"
                multipart.append(g)
            else:
                groups.append(g)

        groups.sort(key=lambda g: (-g.redundant_bytes, -g.copies, g.label))
        multipart.sort(key=lambda g: (-g.total_bytes, g.label))
        report.groups = groups
        report.multipart = multipart
        report.elapsed = time.time() - t0
        if progress:
            try:
                progress(len(rows), len(rows),
                         f"完成：{len(groups)} 组重复，{len(multipart)} 组同目录分片")
            except Exception:
                pass
        return report

    # -- 置信度 --------------------------------------------------------
    @staticmethod
    def _judge(kind: str, members: Sequence[DupMember],
               by_folder: Dict[str, List[DupMember]]) -> Tuple[str, str]:
        """给出置信度与说明文字。"""
        notes: List[str] = []
        sizes = {m.size for m in members if m.size > 0}
        durs = {m.duration_sec for m in members if m.duration_sec > 0}
        res = {m.resolution for m in members if m.resolution}

        if kind == "num":
            conf = "高"
        elif all(m.year for m in members):
            conf = "中"
        else:
            conf = "低"

        if len(sizes) == 1 and sizes:
            conf = "极高"
            notes.append("各份体积完全一致")
        elif len(durs) == 1 and durs:
            notes.append("时长一致")
        if len(res) > 1:
            notes.append("分辨率不同：" + " / ".join(sorted(res)))
        if len(sizes) > 1:
            lo, hi = min(sizes), max(sizes)
            if lo > 0 and hi / max(lo, 1) > 1.5:
                notes.append(f"体积差异较大（{human_size(lo)} ~ {human_size(hi)}），可能是不同版本")
        if len(by_folder) > 1:
            notes.insert(0, f"分布在 {len(by_folder)} 个目录")
        return conf, "；".join(notes) or "元数据一致"


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------

#: 分组汇总表列
GROUP_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("label", "标识"),
    ("key", "身份键"),
    ("kind", "匹配依据"),
    ("confidence", "置信度"),
    ("copies", "份数"),
    ("redundant_copies", "冗余份数"),
    ("total_text", "总体积"),
    ("redundant_text", "可回收空间"),
    ("note", "说明"),
    ("folders", "所在目录"),
)

#: 成员明细表列
MEMBER_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("label", "所属组"),
    ("num", "番号"),
    ("title", "标题"),
    ("year", "年份"),
    ("resolution", "分辨率"),
    ("size_text", "体积"),
    ("duration_text", "时长"),
    ("folder", "所在目录"),
    ("nfo_name", "NFO 文件"),
    ("original_filename", "视频文件"),
    ("dateadded", "加入时间"),
    ("path", "NFO 完整路径"),
)

#: 同目录分片（已排除）表列
MULTIPART_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("label", "标识"),
    ("key", "身份键"),
    ("members_count", "NFO 份数"),
    ("total_text", "总体积"),
    ("folder", "所在目录"),
    ("note", "说明"),
)


def _open_csv(path: str):
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    return open(path, "w", encoding="utf-8-sig", newline="")


def export_csv(report: DupReport, path: str) -> str:
    """导出重复清单为 CSV（UTF-8 BOM，Excel 直接双击不乱码）。"""
    with _open_csv(path) as fh:
        w = csv.writer(fh)
        w.writerow([f"# NFO 画像矿工 · 重复影片检测报告"])
        w.writerow([f"# 生成时间：{report.generated_at}"])
        w.writerow([f"# {COPYRIGHT_NOTICE}"])
        s = report.summary()
        w.writerow(["# 统计", "重复组", s["dup_groups"], "冗余份数", s["redundant_copies"],
                    "可回收空间", s["redundant_text"], "同目录分片组", s["multipart_groups"]])
        w.writerow([])
        w.writerow(["—— 重复组汇总 ——"])
        w.writerow([c[1] for c in GROUP_COLUMNS])
        for g in report.groups:
            d = g.as_dict()
            d["folders"] = " | ".join(g.folders)
            w.writerow([d.get(k, "") for k, _ in GROUP_COLUMNS])
        w.writerow([])
        w.writerow(["—— 成员明细 ——"])
        w.writerow([c[1] for c in MEMBER_COLUMNS])
        for g in report.groups:
            for m in g.members:
                d = m.as_dict()
                d["label"] = g.label
                w.writerow([d.get(k, "") for k, _ in MEMBER_COLUMNS])
        if report.multipart:
            w.writerow([])
            w.writerow(["—— 已排除：同目录分片 ——"])
            w.writerow([c[1] for c in MULTIPART_COLUMNS])
            for g in report.multipart:
                w.writerow([g.label, g.key, len(g.members), g.total_text,
                            g.folders[0] if g.folders else "", g.note])
    return path


def export_json(report: DupReport, path: str) -> str:
    """导出为 JSON（含完整分组与成员信息）。"""
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report.as_dict(), fh, ensure_ascii=False, indent=2)
    return path


def export_xlsx(report: DupReport, path: str) -> Optional[str]:
    """导出为 XLSX（三个工作表）。缺少 openpyxl 时返回 None。"""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
    except Exception:
        return None

    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    wb = Workbook()
    head_font = Font(bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor="2F6FB5")

    def sheet(title: str, cols: Sequence[Tuple[str, str]], rows: Iterable[Sequence[Any]],
              widths: Optional[Sequence[int]] = None):
        ws = wb.create_sheet(title)
        ws.append([c[1] for c in cols])
        for c in ws[1]:
            c.font = head_font
            c.fill = head_fill
            c.alignment = Alignment(horizontal="center")
        for r in rows:
            ws.append(list(r))
        for i, (_, _) in enumerate(cols, start=1):
            ws.column_dimensions[chr(64 + i) if i <= 26 else "A"].width = (
                widths[i - 1] if widths and i <= len(widths) else 18)
        ws.freeze_panes = "A2"
        return ws

    s = report.summary()
    ws = wb.active
    ws.title = "概览"
    ws.append(["NFO 画像矿工 · 重复影片检测报告"])
    ws["A1"].font = Font(bold=True, size=14)
    for k, v in (
        ("生成时间", report.generated_at),
        ("扫描作品数", s["scanned"]),
        ("重复组数", s["dup_groups"]),
        ("涉及作品数", s["dup_movies"]),
        ("冗余份数", s["redundant_copies"]),
        ("可回收空间", s["redundant_text"]),
        ("可回收空间(字节)", s["redundant_bytes"]),
        ("同目录分片组（已排除）", s["multipart_groups"]),
        ("耗时(秒)", s["elapsed"]),
        ("版权声明", COPYRIGHT_NOTICE),
    ):
        ws.append([k, v])
    ws.column_dimensions["A"].width = 26
    ws.column_dimensions["B"].width = 60

    sheet("重复组", GROUP_COLUMNS,
          ([g.label, g.key, "番号" if g.kind == "num" else "标题+年份", g.confidence,
            g.copies, g.redundant_copies, g.total_text, g.redundant_text, g.note,
            " | ".join(g.folders)] for g in report.groups),
          widths=[24, 16, 12, 10, 8, 10, 12, 14, 46, 60])

    sheet("成员明细", MEMBER_COLUMNS,
          ([g.label, m.num, m.title, m.year, m.resolution, human_size(m.size),
            human_duration(m.duration_sec), m.folder, m.nfo_name, m.original_filename,
            m.dateadded, m.path] for g in report.groups for m in g.members),
          widths=[24, 14, 40, 8, 10, 12, 12, 46, 22, 26, 20, 60])

    if report.multipart:
        sheet("同目录分片(已排除)", MULTIPART_COLUMNS,
              ([g.label, g.key, len(g.members), g.total_text,
                g.folders[0] if g.folders else "", g.note] for g in report.multipart),
              widths=[24, 16, 12, 12, 60, 40])

    wb.save(path)
    return path


def export_all(report: DupReport, out_dir: str, *,
               formats: Sequence[str] = ("csv", "json", "xlsx")) -> Dict[str, str]:
    """一次性导出全部格式，返回 {格式: 路径}。"""
    os.makedirs(out_dir, exist_ok=True)
    out: Dict[str, str] = {}
    if "csv" in formats:
        out["csv"] = export_csv(report, os.path.join(out_dir, "重复影片清单.csv"))
    if "json" in formats:
        out["json"] = export_json(report, os.path.join(out_dir, "重复影片清单.json"))
    if "xlsx" in formats:
        p = export_xlsx(report, os.path.join(out_dir, "重复影片清单.xlsx"))
        out["xlsx"] = p or "未生成（缺少 openpyxl，可 pip install openpyxl）"
    return out


__all__ = [
    "DuplicateFinder", "DupReport", "DupGroup", "DupMember",
    "export_csv", "export_json", "export_xlsx", "export_all",
    "human_size", "human_duration", "norm_num", "extract_num", "title_key",
    "COPYRIGHT_NOTICE",
]
