# -*- coding: utf-8 -*-
"""NFO 容错解析器。

设计目标：
1. 5 万量级文件必须"一个都不漏"——任何单文件解析失败都不能中断整体任务。
2. 编码混乱（UTF-8 / GBK / Shift-JIS / BOM / 无声明）全部兜住。
3. 畸形 XML（控制字符、未转义 &、标签未闭合）降级为正则抽取，尽量抢救字段。
4. 把 genre/tag 里被塞进去的"系列:/片商:/发行:/导演:"等结构化信息还原成独立字段，
   保证后续"高频标签"统计出来的是真正的**内容标签**，而不是一堆元数据噪音。

输出统一为一个扁平 dict（字段说明见 FIELDS）。
"""

from __future__ import annotations

import os
import re
import unicodedata
import xml.etree.ElementTree as ET
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .normalize import variant_key

# ---------------------------------------------------------------------------
# 常量与规则表
# ---------------------------------------------------------------------------

#: 尝试的解码顺序，覆盖中日英三种来源
_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "shift_jis", "cp1252", "latin-1")

#: XML 1.0 允许的字符之外的控制字符（会被清洗掉）
_INVALID_XML_CHARS = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f￾￿]"
)

#: 合法实体之外的裸 &
_BARE_AMP = re.compile(r"&(?!(?:[A-Za-z][A-Za-z0-9]*|#[0-9]+|#x[0-9A-Fa-f]+);)")

#: 技术类标签（画质/编码/片源/音轨等），统计"内容标签"时应剔除
TECH_TAG_PAT = re.compile(
    r"^(?:"
    r"AVC1|HEVC|H\.?264|H\.?265|XVID|DIVX|MPEG-?[24]|VP9|AV1|10BIT|8BIT|HDR|SDR|DV|DOLBY"
    r"|\d{3,4}[PIpi]$|\d+K|2K|4K|8K|UHD|FHD|HD|SD"
    r"|BLURAY|Blu-?ray|WEB-?DL|WEBRIP|HDTV|DVDRIP|BDRIP|REMUX|CAM|TS|ISO"
    r"|AAC|AC3|DTS|MP3|FLAC|5\.1|7\.1"
    r"|SUB|CHS|CHT|GB|BIG5|内嵌字幕|外挂字幕|无字幕|字幕"
    r"|高清|超清|标清|高画质|高画質|60FPS|VR|3D"
    r")$",
    re.IGNORECASE,
)

#: 分辨率识别（用于画质维度的关键词）
RES_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"(?:^|[^\w])(8K|4320[Pp])(?:[^\w]|$)"), "8K"),
    (re.compile(r"(?:^|[^\w])(4K|2160[Pp]|UHD)(?:[^\w]|$)", re.I), "4K"),
    (re.compile(r"(?:^|[^\w])(2K|1440[Pp])(?:[^\w]|$)", re.I), "2K"),
    (re.compile(r"(?:^|[^\w])(1080[Pp]|FHD)(?:[^\w]|$)", re.I), "1080P"),
    (re.compile(r"(?:^|[^\w])(720[Pp]|HD)(?:[^\w]|$)", re.I), "720P"),
    (re.compile(r"(?:^|[^\w])(480[Pp]|SD)(?:[^\w]|$)", re.I), "480P"),
]

#: 标签前缀 -> 归属字段（支持中英文冒号）
_PREFIX_RULES: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"^\s*系\s*列\s*[:：]\s*", re.I), "series"),
    (re.compile(r"^\s*(?:片商|制作商|制作|メーカー|maker)\s*[:：]\s*", re.I), "studio"),
    (re.compile(r"^\s*(?:发行|発売|发行商|publisher)\s*[:：]\s*", re.I), "publisher"),
    (re.compile(r"^\s*(?:厂牌|レーベル|label)\s*[:：]\s*", re.I), "label"),
    (re.compile(r"^\s*(?:导演|監督|director)\s*[:：]\s*", re.I), "director"),
    (re.compile(r"^\s*(?:番号|品番|code|id)\s*[:：]\s*", re.I), "num"),
]

#: 有码 / 无码 判定
UNCENSORED_WORDS = ("无码", "無碼", "无修正", "無修正", "UNCENSORED", "无码破解")
CENSORED_WORDS = ("有码", "有碼", "CENSORED", "モザイク", "马赛克")

#: 马赛克状态标签（已有 censor_status 字段承载，不再重复计入内容标签）
CENSOR_TAG_PAT = re.compile(
    r"^(?:无码|無碼|无修正|無修正|有码|有碼|UNCENSORED|CENSORED|无码破解)$",
    re.IGNORECASE,
)

#: 番号（作品编号）通用形态：字母前缀 + 可选分隔符 + 数字
NUM_PAT = re.compile(r"\b([A-Za-z]{1,8}[-_]?\d{2,5})\b")
#: 严格的番号前缀（如 DSOD、ABP、SSIS）
NUM_PREFIX_PAT = re.compile(r"^([A-Za-z]{1,8})[-_]?\d{2,5}$")

_DATE_PAT = re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})")
_DATETIME_PAT = re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})[ T](\d{1,2}):(\d{2})")
_YEAR_PAT = re.compile(r"(19\d{2}|20\d{2})")

_INT_PAT = re.compile(r"-?\d+")


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def _clean_text(value: Any) -> str:
    """把任意输入规整成干净的文本（去零宽字符、压缩空白、去首尾）。"""
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        value = " / ".join(str(v) for v in value if v)
    if not isinstance(value, str):
        value = str(value)
    # 去掉零宽/软连字符等隐形字符
    value = value.replace("​", "").replace("‌", "").replace("‍", "")
    value = value.replace("﻿", "").replace("­", "")
    value = unicodedata.normalize("NFKC", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _to_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return int(value)
    text = _clean_text(value)
    if not text:
        return default
    m = _INT_PAT.search(text.replace(",", ""))
    return int(m.group()) if m else default


def _to_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = _clean_text(value)
    if not text:
        return default
    m = re.search(r"-?\d+(?:\.\d+)?", text)
    if not m:
        return default
    try:
        return float(m.group())
    except ValueError:
        return default


def _norm_date(value: Any) -> str:
    """统一成 YYYY-MM-DD。"""
    text = _clean_text(value)
    if not text:
        return ""
    m = _DATE_PAT.search(text)
    if not m:
        return ""
    y, mo, d = (int(x) for x in m.groups())
    if not (1900 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31):
        return ""
    return f"{y:04d}-{mo:02d}-{d:02d}"


def _norm_datetime(value: Any) -> str:
    """统一成 YYYY-MM-DD HH:MM。"""
    text = _clean_text(value)
    if not text:
        return ""
    m = _DATETIME_PAT.search(text)
    if m:
        y, mo, d, h, mi = (int(x) for x in m.groups())
        if 1900 <= y <= 2100:
            return f"{y:04d}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}"
    return _norm_date(value)


def _norm_year(value: Any) -> Optional[int]:
    text = _clean_text(value)
    if not text:
        return None
    m = _YEAR_PAT.search(text)
    return int(m.group()) if m else None


def _decode(raw: bytes) -> Tuple[str, str]:
    """多编码兜底解码，返回 (文本, 命中的编码名)。"""
    for enc in _ENCODINGS:
        try:
            return raw.decode(enc), enc
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8/replace"


def _sanitize_xml(text: str) -> str:
    """清洗会让 ElementTree 直接报错的字符。"""
    text = _INVALID_XML_CHARS.sub("", text)
    # 裸 & 转义（必须在控制字符清洗之后）
    text = _BARE_AMP.sub("&amp;", text)
    # 去掉 XML 声明之外的非法 PI/注释嵌套（简单兜底）
    return text


def _findall_text(root: ET.Element, path: str) -> List[str]:
    out: List[str] = []
    for node in root.findall(path):
        val = _clean_text(node.text)
        if val:
            out.append(val)
    return out


def _first_text(root: ET.Element, *paths: str) -> str:
    for path in paths:
        for node in root.findall(path):
            val = _clean_text(node.text)
            if val:
                return val
    return ""


# ---------------------------------------------------------------------------
# 降级：正则抽取（XML 完全无法解析时使用）
# ---------------------------------------------------------------------------

_RE_SIMPLE = {
    "title": r"<title>(.*?)</title>",
    "originaltitle": r"<originaltitle>(.*?)</originaltitle>",
    "year": r"<year>(.*?)</year>",
    "premiered": r"<premiered>(.*?)</premiered>",
    "release": r"<release>(.*?)</release>",
    "dateadded": r"<dateadded>(.*?)</dateadded>",
    "runtime": r"<runtime>(.*?)</runtime>",
    "userrating": r"<userrating>(.*?)</userrating>",
    "plot": r"<plot>(.*?)</plot>",
    "studio": r"<studio>(.*?)</studio>",
    "maker": r"<maker>(.*?)</maker>",
    "publisher": r"<publisher>(.*?)</publisher>",
    "label": r"<label>(.*?)</label>",
    "director": r"<director>(.*?)</director>",
    "num": r"<(?:num|id|javdbsearchid)>(.*?)</(?:num|id|javdbsearchid)>",
    "set_name": r"<set>\s*<name>(.*?)</name>",
    "series": r"<series>(.*?)</series>",
    "original_filename": r"<original_filename>(.*?)</original_filename>",
    "width": r"<width>(.*?)</width>",
    "height": r"<height>(.*?)</height>",
    "durationinseconds": r"<durationinseconds>(.*?)</durationinseconds>",
}
_RE_LIST = {
    "genre": r"<genre>(.*?)</genre>",
    "tag": r"<tag>(.*?)</tag>",
}
_RE_ACTOR = re.compile(r"<actor>(.*?)</actor>", re.S)
_RE_ACTOR_NAME = re.compile(r"<name>(.*?)</name>", re.S)


def _regex_parse(text: str) -> Dict[str, Any]:
    """XML 结构损坏时的抢救式抽取。"""
    data: Dict[str, Any] = {"_fallback": True}
    flags = re.S | re.I
    for key, pat in _RE_SIMPLE.items():
        m = re.search(pat, text, flags)
        if m:
            val = re.sub(r"<[^>]+>", "", m.group(1))
            val = _clean_text(val)
            if val:
                data[key] = val
    for key, pat in _RE_LIST.items():
        data[key] = [
            _clean_text(re.sub(r"<[^>]+>", "", v))
            for v in re.findall(pat, text, flags)
            if _clean_text(re.sub(r"<[^>]+>", "", v))
        ]
    actors = []
    for block in _RE_ACTOR.findall(text):
        m = _RE_ACTOR_NAME.search(block)
        if m:
            name = _clean_text(re.sub(r"<[^>]+>", "", m.group(1)))
            if name:
                actors.append({"name": name, "role": "", "thumb": "", "order": None})
    data["actors"] = actors
    return data


# ---------------------------------------------------------------------------
# 语义归一化
# ---------------------------------------------------------------------------


def classify_tag(
    raw: str,
    num_prefix: str = "",
    actors: Iterable[str] = (),
) -> Tuple[str, str]:
    """判断一个 genre/tag 条目的归属。

    返回 (归属类别, 归一化后的值)。类别取值：
      series / studio / publisher / label / director / num —— 结构化元数据，还原成独立字段
      tech     —— 画质、编码、片源等技术标签（不算内容标签）
      censor   —— 有码/无码（已有 censor_status 字段承载）
      meta     —— 与番号前缀或演员名重复的冗余标签（已有对应字段承载）
      content  —— 真正的内容标签，参与「高频标签」统计
    """
    text = _clean_text(raw)
    if not text:
        return "skip", ""

    for pat, field in _PREFIX_RULES:
        m = pat.match(text)
        if m:
            value = _clean_text(text[m.end():])
            if value:
                return field, value
            return "skip", ""

    if CENSOR_TAG_PAT.match(text):
        return "censor", text
    if TECH_TAG_PAT.match(text):
        return "tech", text

    # 与番号前缀 / 演员名重复的冗余标签
    # 必须走 variant_key：演员名常出现繁简差异（響蓮 存演员、响莲 存标签）
    key = variant_key(text)
    if not key:
        return "skip", ""
    if num_prefix and key == variant_key(num_prefix):
        return "meta", text
    for a in actors:
        if a and key == variant_key(a):
            return "meta", text

    return "content", text


def detect_resolution(*candidates: Any) -> str:
    """从多个候选文本/高度值中推断分辨率档位。"""
    for cand in candidates:
        if cand is None:
            continue
        if isinstance(cand, int) and cand > 0:
            h = cand
            if h <= 480:
                return "480P"
            if h <= 720:
                return "720P"
            if h <= 1080:
                return "1080P"
            if h <= 1440:
                return "2K"
            if h <= 2160:
                return "4K"
            return "8K"
        text = _clean_text(cand)
        if not text:
            continue
        for pat, res in RES_PATTERNS:
            if pat.search(text):
                return res
    return ""


def detect_censor_status(texts: Iterable[str]) -> str:
    """判定有码/无码。"""
    blob = " ".join(t for t in texts if t).upper()
    for w in UNCENSORED_WORDS:
        if w.upper() in blob:
            return "无码"
    for w in CENSORED_WORDS:
        if w.upper() in blob:
            return "有码"
    return "未知"


def extract_num(*candidates: Any) -> Tuple[str, str]:
    """从候选文本中提取番号与番号前缀（系列代号）。"""
    for cand in candidates:
        text = _clean_text(cand)
        if not text:
            continue
        m = NUM_PREFIX_PAT.match(text)
        if m:
            num = text.upper().replace("_", "-")
            prefix = m.group(1).upper()
            return num, prefix
        m = NUM_PAT.search(text)
        if m:
            token = m.group(1).upper().replace("_", "-")
            pm = NUM_PREFIX_PAT.match(token)
            if pm:
                return token, pm.group(1).upper()
    return "", ""


def strip_actor_from_title(title: str, actors: List[str]) -> str:
    """把标题尾部「「響蓮」」这类被引号包裹的演员名去掉，得到干净的剧情标题。"""
    if not title:
        return ""
    cleaned = title
    for a in actors:
        if a and a in cleaned:
            cleaned = cleaned.replace(f"「{a}」", " ").replace(f"[{a}]", " ")
    cleaned = re.sub(r"[「」\[\]]", " ", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" -–—·、,，")
    return cleaned


# ---------------------------------------------------------------------------
# 主解析函数
# ---------------------------------------------------------------------------

#: 输出的字段清单（用于文档与导出列顺序）
FIELDS = [
    "path", "filename", "filesize", "mtime", "encoding", "parse_status",
    "num", "num_prefix", "title", "clean_title", "originaltitle",
    "year", "premiered", "release", "dateadded", "runtime_min",
    "userrating", "rating_max", "mpaa",
    "plot", "plot_len",
    "series", "set_name", "studio", "maker", "publisher", "label",
    "director", "directors",
    "actors", "actor_count",     "tags", "tag_count",
    "tech_tags", "meta_tags", "genres_raw",
    "resolution", "width", "height", "video_codec", "audio_codec",
    "duration_sec", "censor_status",
    "original_filename", "video_exists", "video_size",
    "website", "trailer", "languages", "watched", "playcount",
]


def parse_nfo_text(text: str, *, path: str = "", stat: Optional[os.stat_result] = None) -> Dict[str, Any]:
    """解析 NFO 文本，返回扁平记录。"""
    rec: Dict[str, Any] = {k: "" for k in FIELDS}
    rec["path"] = path
    rec["parse_status"] = "ok"
    rec["filesize"] = getattr(stat, "st_size", 0) or 0
    if getattr(stat, "st_mtime", None):
        from datetime import datetime
        rec["mtime"] = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")

    # --- 结构化解析 ---
    root: Optional[ET.Element] = None
    try:
        root = ET.fromstring(_sanitize_xml(text))
    except ET.ParseError:
        # 常见情况：根节点外还有垃圾内容 / 多个根节点
        try:
            m = re.search(r"<movie\b.*?</movie>", text, re.S)
            if m:
                root = ET.fromstring(_sanitize_xml(m.group(0)))
        except ET.ParseError:
            root = None

    if root is None:
        data = _regex_parse(text)
        rec["parse_status"] = "recovered"
    else:
        data = _xml_to_dict(root)
        rec["parse_status"] = "ok"

    _fill_record(rec, data)
    return rec


def _xml_to_dict(root: ET.Element) -> Dict[str, Any]:
    data: Dict[str, Any] = {"_fallback": False}
    for key, path in (
        ("title", "title"),
        ("originaltitle", "originaltitle"),
        ("sorttitle", "sorttitle"),
        ("year", "year"),
        ("premiered", "premiered"),
        ("release", "release"),
        ("dateadded", "dateadded"),
        ("runtime", "runtime"),
        ("userrating", "userrating"),
        ("mpaa", "mpaa"),
        ("certification", "certification"),
        ("customrating", "customrating"),
        ("plot", "plot"),
        ("outline", "outline"),
        ("tagline", "tagline"),
        ("studio", "studio"),
        ("maker", "maker"),
        ("publisher", "publisher"),
        ("label", "label"),
        ("director", "director"),
        ("num", "num"),
        ("id", "id"),
        ("javdbsearchid", "javdbsearchid"),
        ("code", "code"),
        ("series", "series"),
        ("trailer", "trailer"),
        ("languages", "languages"),
        ("website", "website"),
        ("original_filename", "original_filename"),
        ("watched", "watched"),
        ("playcount", "playcount"),
        ("source", "source"),
        ("edition", "edition"),
    ):
        data[key] = _first_text(root, path)

    data["genre"] = _findall_text(root, "genre")
    data["tag"] = _findall_text(root, "tag")
    data["set_name"] = _first_text(root, "set/name")
    data["actor_names"] = _findall_text(root, "actor/name")

    actors: List[Dict[str, Any]] = []
    for idx, node in enumerate(root.findall("actor")):
        name = _first_text(node, "name")
        if not name:
            continue
        actors.append(
            {
                "name": name,
                "role": _first_text(node, "role"),
                "thumb": _first_text(node, "thumb"),
                "order": _to_int(_first_text(node, "order"), idx),
            }
        )
    data["actors"] = actors

    crew: List[Dict[str, str]] = []
    for node in root.findall("crew"):
        crew.append({"name": _first_text(node, "name"), "role": _first_text(node, "role")})
    data["crew"] = crew

    # 视频 / 音频流
    sd = root.find("fileinfo/streamdetails")
    if sd is None:
        sd = root.find("streamdetails")
    if sd is not None:
        data["video_codec"] = _first_text(sd, "video/codec")
        data["width"] = _to_int(_first_text(sd, "video/width"))
        data["height"] = _to_int(_first_text(sd, "video/height"))
        data["durationinseconds"] = _to_int(_first_text(sd, "video/durationinseconds"))
        data["audio_codec"] = _first_text(sd, "audio/codec")
    else:
        data["video_codec"] = ""
        data["width"] = None
        data["height"] = None
        data["durationinseconds"] = None
        data["audio_codec"] = ""

    # ratings（KODI 嵌套评分）
    rating_max = None
    for node in root.findall("ratings/rating"):
        val = _to_float(_first_text(node, "value"))
        if val is not None:
            rating_max = val if rating_max is None else max(rating_max, val)
    data["rating_max"] = rating_max
    return data


def _fill_record(rec: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
    """把半结构化 data 规整进扁平记录。"""
    # --- 标题 ---
    title = _clean_text(data.get("title"))
    originaltitle = _clean_text(data.get("originaltitle")) or _clean_text(data.get("sorttitle"))
    rec["title"] = title
    rec["originaltitle"] = originaltitle

    # --- 演员 ---
    actors: List[Dict[str, Any]] = list(data.get("actors") or [])
    if not actors and data.get("actor_names"):
        actors = [{"name": n, "role": "", "thumb": "", "order": i} for i, n in enumerate(data["actor_names"])]
    # 去重保序
    seen = set()
    uniq_actors: List[Dict[str, Any]] = []
    for a in actors:
        nm = _clean_text(a.get("name"))
        if not nm or nm in seen:
            continue
        seen.add(nm)
        uniq_actors.append({"name": nm, "role": _clean_text(a.get("role")), "order": a.get("order")})
    rec["actors"] = [a["name"] for a in uniq_actors]
    rec["actor_count"] = len(uniq_actors)

    rec["clean_title"] = strip_actor_from_title(title, rec["actors"])

    # --- 番号 ---
    num, prefix = extract_num(
        data.get("num"), data.get("javdbsearchid"), data.get("id"), data.get("code"),
        data.get("original_filename"), title,
    )
    rec["num"] = num
    rec["num_prefix"] = prefix

    # --- 标签分类 ---
    series_vals: List[str] = []
    studio_vals: List[str] = []
    publisher_vals: List[str] = []
    label_vals: List[str] = []
    director_vals: List[str] = []
    content_tags: List[str] = []
    tech_tags: List[str] = []
    meta_tags: List[str] = []

    raw_items = list(data.get("genre") or []) + list(data.get("tag") or [])
    # genre 与 tag 通常重复，去重保序
    dedup: List[str] = []
    seen_tag = set()
    for item in raw_items:
        t = _clean_text(item)
        if not t:
            continue
        key = t.casefold()
        if key in seen_tag:
            continue
        seen_tag.add(key)
        dedup.append(t)

    num_fallback = ""
    for item in dedup:
        kind, value = classify_tag(item, rec.get("num_prefix", ""), rec.get("actors", ()))
        if kind == "skip":
            continue
        if kind == "series":
            series_vals.append(value)
        elif kind == "studio":
            studio_vals.append(value)
        elif kind == "publisher":
            publisher_vals.append(value)
        elif kind == "label":
            label_vals.append(value)
        elif kind == "director":
            director_vals.append(value)
        elif kind == "num":
            num_fallback = num_fallback or value
        elif kind == "tech":
            tech_tags.append(value)
        elif kind in ("censor", "meta"):
            # 已有 censor_status / num_prefix / actors 字段承载，避免污染内容标签
            meta_tags.append(value)
        else:
            content_tags.append(value)

    def _pick(*vals: Any) -> str:
        for v in vals:
            s = _clean_text(v)
            if s:
                return s
        return ""

    rec["series"] = _pick(data.get("series"), data.get("set_name"), series_vals[0] if series_vals else "")
    rec["set_name"] = _pick(data.get("set_name"), rec["series"])
    rec["studio"] = _pick(data.get("studio"), data.get("maker"), studio_vals[0] if studio_vals else "")
    rec["maker"] = _pick(data.get("maker"), rec["studio"])
    rec["publisher"] = _pick(data.get("publisher"), publisher_vals[0] if publisher_vals else "")
    rec["label"] = _pick(data.get("label"), label_vals[0] if label_vals else "")

    directors: List[str] = []
    for v in [_pick(data.get("director"))] + director_vals:
        if v and v not in directors:
            directors.append(v)
    for c in data.get("crew") or []:
        nm = _clean_text(c.get("name"))
        role = _clean_text(c.get("role")).upper()
        if nm and ("DIRECTOR" in role or "导演" in role) and nm not in directors:
            directors.append(nm)
    rec["directors"] = directors
    rec["director"] = directors[0] if directors else ""

    if not rec["num"] and num_fallback:
        n2, p2 = extract_num(num_fallback)
        rec["num"], rec["num_prefix"] = n2, p2

    rec["tags"] = content_tags
    rec["tag_count"] = len(content_tags)
    rec["tech_tags"] = tech_tags
    rec["meta_tags"] = meta_tags
    rec["genres_raw"] = dedup

    # --- 时间 ---
    rec["premiered"] = _norm_date(data.get("premiered")) or _norm_date(data.get("release"))
    rec["release"] = _norm_date(data.get("release")) or rec["premiered"]
    rec["dateadded"] = _norm_datetime(data.get("dateadded"))
    year = _norm_year(data.get("year"))
    if year is None:
        year = _norm_year(rec["premiered"]) or _norm_year(rec["dateadded"])
    rec["year"] = year or ""

    # --- 时长 ---
    runtime_min = _to_int(data.get("runtime"))
    duration_sec = _to_int(data.get("durationinseconds"))
    if runtime_min and 0 < runtime_min < 6000:  # 正常分钟数
        rec["runtime_min"] = runtime_min
    elif duration_sec and duration_sec > 0:
        rec["runtime_min"] = round(duration_sec / 60)
    else:
        rec["runtime_min"] = runtime_min or ""
    rec["duration_sec"] = duration_sec or (runtime_min * 60 if runtime_min else "")

    # --- 评分 ---
    rec["userrating"] = _to_float(data.get("userrating"), 0.0) or 0.0
    rec["rating_max"] = _to_float(data.get("rating_max"), 0.0) or 0.0
    rec["mpaa"] = _pick(data.get("mpaa"), data.get("certification"), data.get("customrating"))

    # --- 剧情 ---
    plot = _clean_text(data.get("plot")) or _clean_text(data.get("outline"))
    rec["plot"] = plot
    rec["plot_len"] = len(plot)

    # --- 技术信息 ---
    rec["width"] = _to_int(data.get("width")) or ""
    rec["height"] = _to_int(data.get("height")) or ""
    rec["video_codec"] = _clean_text(data.get("video_codec"))
    rec["audio_codec"] = _clean_text(data.get("audio_codec"))
    rec["resolution"] = detect_resolution(
        rec["height"] or None, " / ".join(tech_tags), rec["video_codec"], _pick(data.get("source"))
    )

    # --- 有码/无码 ---
    rec["censor_status"] = detect_censor_status(dedup + [title, plot])

    # --- 其它 ---
    rec["original_filename"] = _clean_text(data.get("original_filename"))
    rec["website"] = _clean_text(data.get("website"))
    rec["trailer"] = _clean_text(data.get("trailer"))
    rec["languages"] = _clean_text(data.get("languages"))
    rec["watched"] = 1 if _clean_text(data.get("watched")).lower() in ("true", "1", "yes") else 0
    rec["playcount"] = _to_int(data.get("playcount"), 0) or 0
    rec["filename"] = os.path.basename(rec["path"]) if rec["path"] else ""
    return rec


def parse_nfo_file(path: str, *, probe_video: bool = True) -> Dict[str, Any]:
    """解析单个 NFO 文件，永不抛异常。"""
    rec: Dict[str, Any] = {k: "" for k in FIELDS}
    rec["path"] = path
    rec["filename"] = os.path.basename(path)
    try:
        stat = os.stat(path)
        rec["filesize"] = stat.st_size
        from datetime import datetime
        rec["mtime"] = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    except OSError:
        stat = None

    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        rec["parse_status"] = f"read_error:{exc.__class__.__name__}"
        return rec

    text, enc = _decode(raw)
    rec["encoding"] = enc

    try:
        rec = parse_nfo_text(text, path=path, stat=stat)
        rec["encoding"] = enc
    except Exception as exc:  # pragma: no cover - 兜底
        rec["parse_status"] = f"parse_error:{exc.__class__.__name__}"
        return rec

    if probe_video:
        rec["video_exists"] = 0
        rec["video_size"] = 0
        name = rec.get("original_filename")
        if name:
            cand = os.path.join(os.path.dirname(path), name)
            try:
                st = os.stat(cand)
                rec["video_exists"] = 1
                rec["video_size"] = st.st_size
            except OSError:
                pass
    return rec


__all__ = [
    "parse_nfo_file",
    "parse_nfo_text",
    "classify_tag",
    "detect_resolution",
    "detect_censor_status",
    "extract_num",
    "FIELDS",
]
