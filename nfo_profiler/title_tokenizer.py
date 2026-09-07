# -*- coding: utf-8 -*-
"""纯 Python 的 NFO 标题分词器（zero-dependency）。

设计要点
--------
* 不引入 jieba/snaki 等重依赖，避免 exe 包体暴涨；
* **词表优先**：把 DB 里现存的高频 tag 作为「已知偏好词」灌进词表，前向最大匹配优先切出
  完整词，剩下的中文段做 2~4 字 N-gram 滑窗，落到一个「高频碎片词」里；
* **停用词**：内置一份高频但无语义的字符 / 双字（如「的」「之」），不会出现在最终结果中；
* **英文 / 数字**：保留原写法（不去 stop 词、不切大小写），允许「OL」「HD」等工业词出现。

::

    Copyright © 2026 肆月Aperture 本软件不得用于商业用途，仅做学习交流使用。
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple


# ---------------------------------------------------------------------------
# 默认词典 / 停用词
# ---------------------------------------------------------------------------

# 50+ 常识中文偏好词，覆盖用户提到的「美腿 / 秘书」类题材。
# 这些 tag 在某些作品上没被打到，但标题里往往会出现 —— 词典优先能直接把整词切出来。
DEFAULT_HINT_WORDS: Set[str] = {
    # 外观 / 身材
    "美腿", "巨乳", "贫乳", "黑丝", "丝袜", "高跟", "高跟鞋", "苗条", "丰满",
    "车模", "模特", "全裸", "制服", "泳装", "内衣",
    # 角色 / 关系
    "秘书", "护士", "教师", "空姐", "人妻", "熟女", "萝莉", "御姐", "痴女",
    "女仆", "主仆", "侠女", "学生", "校服", "老板娘", "继母", "嫂子",
    "按摩师", "店员", "服务员", "主播", "模特",
    # 场所 / 场景
    "健身房", "温泉", "电车", "办公室", "教室", "医院", "美容院", "学校",
    "酒店", "旅馆", "泳池", "浴室", "厨房", "公园", "电梯", "阳台", "海边",
    # 状态 / 题材
    "偷拍", "出轨", "不伦", "婚外情", "人前", "群P", "乱交", "捆绑", "SM",
    "颜射", "骑乘", "站街", "素人", "无码", "有码",
    # 厂商常用关键字
    "COSPLAY", "4K", "8K", "VR", "AI", "独家", "首发", "破解",
}

# 单字高频但无语义 → 过滤掉
_STOP_CHARS: Set[str] = set(
    "的了之与和是在被把有有给我你他她它它们的不这那还有就来去做啊哦呢嘛呀呗"
    "吗吧啦呢呀哎呦哦喔哈hi吧与及等中上下大小那个这些那些这么怎样如何什么自己"
)

# 高频但语义极弱的 2 字组合
_STOP_BIGRAMS: Set[str] = set("""
的 人 事 体 情 感 绪 是 在 有 让 让 把 被 让
剧情 介绍 精彩 精彩 警告 提醒 本文 不负 责任 转载 内容 原文 标题 略 略
HD 高清 完整 完整版 完整 全程 全程
开始 结束 第一 第二 这次 那次 此 此
""".split())

# 仅做标点 / 切分，保留中文与英文字母/数字
_SPLIT_RE = re.compile(r"[\s\W_]+", flags=re.UNICODE)
_KEEP_RE = re.compile(r"[一-鿿A-Za-z0-9]+", flags=re.UNICODE)


def _default_known_words() -> Set[str]:
    return set(DEFAULT_HINT_WORDS)


def _default_stopwords() -> Set[str]:
    """返回的集合里，单字取自 _STOP_CHARS，双字取自 _STOP_BIGRAMS。"""
    return set(_STOP_CHARS) | set(_STOP_BIGRAMS)


# ---------------------------------------------------------------------------
# 分词主逻辑
# ---------------------------------------------------------------------------

def _is_cjk_word(word: str) -> bool:
    """判断是否纯中日韩字符。"""
    return any("一" <= ch <= "鿿" for ch in word)


def _splits_for_forward_match(text: str) -> List[Tuple[int, int, str]]:
    """把文本切成一些「稳定段」：纯中文字段单独出来，英文/数字段也单独出来。
    返回 (start, end, segment) 列表，便于在原字符串上映射。
    """
    out: List[Tuple[int, int, str]] = []
    pos = 0
    while pos < len(text):
        m = _KEEP_RE.match(text, pos)
        if not m:
            pos += 1
            continue
        s = m.group()
        if not s:
            pos += 1
            continue
        # 把全 ASCII 段当成一个稳定段；CJK 段也可整体切出
        out.append((m.start(), m.end(), s))
        pos = m.end()
    return out


def _forward_max_match(
    segment: str,
    known: Set[str],
    min_len: int = 2,
    max_len: int = 4,
) -> List[str]:
    """对一段纯 CJK 文本做前向最大匹配：先在 known 里查最长命中；
    否则取一个 N-gram 滑窗（min_len..max_len）。"""
    tokens: List[str] = []
    pos = 0
    n = len(segment)
    while pos < n:
        matched: Optional[str] = None
        for L in range(max_len, min_len - 1, -1):
            if pos + L > n:
                continue
            cand = segment[pos:pos + L]
            if cand in known:
                matched = cand
                break
        if matched is None:
            # 没命中已知词 → 退化为 N-gram（用 min_len 切，意为「N 个中文字」）
            cand = segment[pos:pos + max_len]
            if len(cand) >= min_len:
                tokens.append(cand[:max_len])
                pos += max_len
            else:
                # 末尾不足 min_len：尝试切一个 max_len 内的 N-gram
                if len(cand) >= min_len:
                    tokens.append(cand)
                    pos += len(cand)
                else:
                    pos += 1
        else:
            tokens.append(matched)
            pos += len(matched)
    return tokens


def tokenize_title(
    title: str,
    known: Optional[Set[str]] = None,
    stopwords: Optional[Set[str]] = None,
    *,
    min_len: int = 2,
    max_len: int = 4,
) -> List[str]:
    """对一部作品的 title 做分词，返回去停用词后的 token 列表（不去重）。

    Parameters
    ----------
    title:
        原始 NFO 标题，可能含番号/星级等前缀。
    known:
        「已知偏好词」词典。优先匹配，整词切出后被忽略剩余位置的 N-gram。
    stopwords:
        不应出现在结果的停用词集合（单字 + 双字）。
    """
    if not title:
        return []
    known = known if known is not None else _default_known_words()
    stopwords = stopwords if stopwords is not None else _default_stopwords()

    out: List[str] = []
    segments = _splits_for_forward_match(title)
    for _, _, seg in segments:
        if not seg:
            continue
        if _is_cjk_word(seg):
            for tok in _forward_max_match(seg, known, min_len, max_len):
                if tok in stopwords:
                    continue
                if len(tok) == 1:
                    # 单字不出；残留单字通常是噪声
                    continue
                out.append(tok)
        else:
            # 英文 / 数字段：按空白 / 标点切，整体保留 ≥ min_len 的词
            for word in re.findall(r"[A-Za-z0-9]+", seg):
                word = word.strip()
                if not word:
                    continue
                if len(word) < min_len:
                    continue
                out.append(word)
    return out


# ---------------------------------------------------------------------------
# 词频统计：用于画像概览
# ---------------------------------------------------------------------------

def infer_known_from_store(store: Any, source: Optional[str] = None,
                           min_tag_count: int = 3, max_tags: int = 500) -> Set[str]:
    """从 DB 把已存在的高频 tag 灌进「已知偏好词」词表（前向最大匹配优先匹配它们）。

    这种「先用 DB 内容反哺分词器」的思路借鉴了 jieba 的「自适应词典」：
    既保留通用词表，又能捕捉用户实际库内的强相关偏好词。
    """
    params: List[Any] = []
    where = ""
    if source:
        where = " WHERE m.source = ?"
        params.append(source)
    rows = store.conn.execute(
        f"SELECT t.tag, COUNT(*) c FROM movie_tags t "
        f"JOIN movies m ON m.id=t.movie_id {where} "
        f"GROUP BY t.tag ORDER BY c DESC LIMIT ?",
        params + [max_tags * 5]
    ).fetchall()
    words = set(_default_known_words())
    for r in rows:
        tag = (r["tag"] or "").strip()
        if not tag or r["c"] < min_tag_count:
            continue
        words.add(tag)
        if len(words) >= max_tags:
            break
    return words


def top_title_terms(
    store: Any,
    *,
    source: Optional[str] = None,
    top_n: int = 50,
    min_count: int = 2,
    known: Optional[Set[str]] = None,
    stopwords: Optional[Set[str]] = None,
    min_len: int = 2,
    max_len: int = 4,
) -> List[Dict[str, Any]]:
    """对每部作品的 title 做分词，统计 token 频次，返回 TopN。

    返回格式与 Analyzer.top_tags 一致：
        ``[{"name": ..., "count": ..., "aliases": [...]}, ...]``

    之所以把 ``aliases`` 留作扩展点：将来若做同义 N-gram 归并（例如「人妻」/「人婦」归并）
    可以无缝接入。
    """
    known = known if known is not None else infer_known_from_store(store, source=source)
    stopwords = stopwords if stopwords is not None else _default_stopwords()

    where = ""
    params: List[Any] = []
    if source:
        where = " WHERE source = ?"
        params.append(source)
    counter: Counter = Counter()
    n_titles = 0
    for r in store.conn.execute(f"SELECT title FROM movies{where}", params):
        t = r["title"] or ""
        if not t.strip():
            continue
        toks = tokenize_title(t, known=known, stopwords=stopwords,
                              min_len=min_len, max_len=max_len)
        # 同一 title 内重复 token 只计 1 次（避免 "美腿美腿美" 这种加权过高）
        uniq = set(toks)
        if uniq:
            n_titles += 1
            counter.update(uniq)
    out: List[Dict[str, Any]] = []
    for tok, cnt in counter.most_common(top_n * 3):
        if cnt < min_count:
            continue
        out.append({"name": tok, "count": cnt, "aliases": []})
        if len(out) >= top_n:
            break
    return out, n_titles, len(counter)


__all__ = [
    "DEFAULT_HINT_WORDS",
    "tokenize_title",
    "top_title_terms",
    "infer_known_from_store",
]
