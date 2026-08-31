# -*- coding: utf-8 -*-
"""文本归一化层。

解决三件事，直接决定「高频艺人 / 高频标签」统计准不准：
1. 繁简与日文异体字差异（響蓮 / 响莲、桜 / 樱、沢 / 泽）——同一个艺人被拆成两条。
2. 同义词合并（中出 / 中出し / 內射）——同一个标签被拆成多条。
3. 噪声字符（全角空格、标点、大小写）——造成无意义的重复项。

映射表可通过外部 JSON 扩展，见 config/synonyms.json 与 config/aliases.json。
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import unicodedata
from typing import Dict, Iterable, List, Mapping, Optional

# ---------------------------------------------------------------------------
# 异体字 -> 规范字（覆盖 JAV 艺人名与常见标签里的高频汉字）
# ---------------------------------------------------------------------------
KANJI_VARIANTS: Dict[str, str] = {
    # 人名高频字
    "響": "响", "蓮": "莲", "桜": "樱", "沢": "泽", "實": "实", "実": "实",
    "斉": "齐", "齊": "齐", "島": "岛", "宮": "宫", "濱": "滨", "浜": "滨",
    "愛": "爱", "優": "优", "橋": "桥", "涼": "凉", "凜": "凛", "恵": "惠",
    "繪": "绘", "絵": "绘", "綾": "绫", "結": "结", "華": "华", "穂": "穗",
    "楓": "枫", "姫": "姬", "夢": "梦", "陽": "阳", "蒼": "苍", "藍": "蓝",
    "薫": "熏", "蘭": "兰", "颯": "飒", "嵐": "岚", "電": "电", "風": "风",
    "雲": "云", "霧": "雾", "氷": "冰", "葉": "叶", "種": "种", "純": "纯",
    "絢": "绚", "綺": "绮", "織": "织", "縫": "缝", "縁": "缘", "縦": "纵",
    "縮": "缩", "績": "绩", "繊": "纤", "統": "统", "絶": "绝", "給": "给",
    "綿": "绵", "緊": "紧", "総": "总", "綠": "绿", "緑": "绿", "線": "线",
    "緩": "缓", "縛": "缚", "縣": "县", "県": "县", "廣": "广", "広": "广",
    "間": "间", "関": "关", "郷": "乡", "戶": "户", "戸": "户", "亞": "亚",
    "亜": "亚", "惡": "恶", "惡": "恶", "個": "个", "個": "个", "會": "会",
    "體": "体", "體": "体", "傳": "传", "傳": "传", "動": "动", "動": "动",
    "務": "务", "勝": "胜", "勝": "胜", "區": "区", "區": "区", "參": "参",
    "參": "参", "味": "味", "喚": "唤", "喚": "唤", "單": "单", "嚴": "严",
    "嚴": "严", "園": "园", "圓": "圆", "壓": "压", "壓": "压", "壞": "坏",
    "壞": "坏", "壯": "壮", "壽": "寿", "夢": "梦", "夾": "夹", "奧": "奥",
    "奧": "奥", "媽": "妈", "姐": "姐", "姉": "姐", "娛": "娱", "姬": "姬",
    "學": "学", "學": "学", "寶": "宝", "寶": "宝", "專": "专", "專": "专",
    "層": "层", "岡": "冈", "島": "岛", "峯": "峰", "峰": "峰", "帶": "带",
    "帶": "带", "帳": "帐", "幾": "几", "幾": "几", "飾": "饰", "廣": "广",
    "廳": "厅", "後": "后", "後": "后", "從": "从", "從": "从", "復": "复",
    "徵": "征", "德": "德", "恆": "恒", "愛": "爱", "慾": "欲", "應": "应",
    "懷": "怀", "戀": "恋", "戀": "恋", "戰": "战", "戲": "戏", "戲": "戏",
    "戶": "户", "掃": "扫", "擇": "择", "擊": "击", "擔": "担", "據": "据",
    "攝": "摄", "收": "收", "敵": "敌", "數": "数", "斷": "断", "時": "时",
    "晝": "昼", "晉": "晋", "曉": "晓", "書": "书", "會": "会", "權": "权",
    "橫": "横", "歐": "欧", "歸": "归", "殺": "杀", "殼": "壳", "氣": "气",
    "氣": "气", "漢": "汉", "澤": "泽", "濁": "浊", "濃": "浓", "濕": "湿",
    "為": "为", "烏": "乌", "無": "无", "燈": "灯", "燒": "烧", "營": "营",
    "爭": "争", "獎": "奖", "獵": "猎", "獸": "兽", "現": "现", "球": "球",
    "環": "环", "當": "当", "疊": "叠", "瘋": "疯", "發": "发", "白": "白",
    "盡": "尽", "監": "监", "盤": "盘", "縣": "县", "縣": "县", "眾": "众",
    "睡": "睡", "矚": "瞩", "碩": "硕", "禮": "礼", "禿": "秃", "種": "种",
    "窮": "穷", "節": "节", "築": "筑", "簡": "简", "簽": "签", "簾": "帘",
    "紅": "红", "納": "纳", "純": "纯", "細": "细", "終": "终",
    "組": "组", "統": "统", "絕": "绝", "經": "经", "緊": "紧", "總": "总",
    "聽": "听", "膚": "肤", "臨": "临", "興": "兴", "舉": "举", "舊": "旧",
    "舌": "舌", "舖": "铺", "艷": "艳", "艷": "艳", "藝": "艺", "藥": "药",
    "處": "处", "虛": "虚", "術": "术", "衛": "卫", "衝": "冲", "補": "补",
    "裝": "装", "裡": "里", "裹": "裹", "觸": "触", "計": "计", "訓": "训",
    "記": "记", "訪": "访", "設": "设", "許": "许", "試": "试", "詩": "诗",
    "話": "话", "語": "语", "誠": "诚", "說": "说", "誤": "误", "誘": "诱",
    "語": "语", "讀": "读", "變": "变", "讓": "让", "讚": "赞", "豬": "猪",
    "財": "财", "賣": "卖", "賢": "贤", "賭": "赌", "購": "购", "贈": "赠",
    "賽": "赛", "贏": "赢", "車": "车", "輕": "轻", "輯": "辑", "輸": "输",
    "辦": "办", "邊": "边", "達": "达", "違": "违", "遠": "远", "適": "适",
    "選": "选", "遺": "遗", "醫": "医", "釀": "酿", "釋": "释", "釣": "钓",
    "鉢": "钵", "銀": "银", "鋪": "铺", "錄": "录", "錢": "钱", "錯": "错",
    "鍊": "炼", "鐘": "钟", "鐵": "铁", "開": "开", "關": "关", "闇": "暗",
    "防": "防", "阻": "阻", "險": "险", "難": "难", "雲": "云", "靈": "灵",
    "靜": "静", "非": "非", "韓": "韩", "頂": "顶", "願": "愿", "顯": "显",
    "類": "类", "飛": "飞", "飼": "饲", "養": "养", "館": "馆", "騎": "骑",
    "體": "体", "鹽": "盐", "麗": "丽", "黃": "黄", "黑": "黑", "點": "点",
    "齋": "斋", "齊": "齐", "龍": "龙", "龜": "龟",
}

# 建索引时过滤非法项（只接受单字符 -> 单字符），防止误写多字符串导致 ord() 报错
_KANJI_TABLE = {ord(k): v for k, v in KANJI_VARIANTS.items() if len(k) == 1 and len(v) == 1}

#: 分组键里要剔除的符号
_NOISE_PAT = re.compile(r"[\s·・･｡。，、,.!！?？:：;；'\"“”‘’()（）\[\]【】〔〕/\\|~～\-—_+=*&^%$#@]")


# ---------------------------------------------------------------------------
# 同义词 / 别名配置
# ---------------------------------------------------------------------------

DEFAULT_SYNONYMS: Dict[str, str] = {}


def variant_key(text: str) -> str:
    """生成用于分组的归一化键：繁简统一 + 去符号 + 小写。"""
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text)
    s = s.translate(_KANJI_TABLE)
    s = unicodedata.normalize("NFKC", s)
    s = _NOISE_PAT.sub("", s)
    return s.casefold()


def resolve_config_paths(path: Optional[str]) -> List[str]:
    """返回同义词配置候选路径（按优先级）。

    解析顺序（后者为兜底）：
      1. 命令行 --config 指定的文件
      2. 当前工作目录下的 config/synonyms.json（用户可覆盖）
      3. 打包后随 exe 携带的 config/synonyms.json（sys._MEIPASS 或 exe 同目录）

    PyInstaller 打包（onedir / onefile）后，用户 cwd 里往往没有 config 目录，
    因此必须回退到随 exe 携带的那一份，否则归一化表会“静默失效”。
    """
    cands: List[str] = []
    if path:
        cands.append(path)
    cands.append(os.path.join("config", "synonyms.json"))
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", None) or os.path.dirname(sys.executable)
        cands.append(os.path.join(base, "config", "synonyms.json"))
    return [p for p in cands if p]


class Normalizer:
    """带可配置同义词表的归一化器。

    Parameters
    ----------
    synonyms:
        {别名: 规范名}。例如 {"中出し": "中出", "內射": "中出"}。
    aliases:
        {艺人别名: 规范艺人}。例如 {"响莲": "響蓮"}。
    stopwords:
        统计时忽略的词。
    """

    def __init__(
        self,
        synonyms: Optional[Mapping[str, str]] = None,
        aliases: Optional[Mapping[str, str]] = None,
        stopwords: Optional[Iterable[str]] = None,
        studio_aliases: Optional[Mapping[str, str]] = None,
        actor_stopwords: Optional[Iterable[str]] = None,
    ) -> None:
        self.synonyms: Dict[str, str] = {self.variant_key(k): v for k, v in (synonyms or {}).items()}
        self.aliases: Dict[str, str] = {self.variant_key(k): v for k, v in (aliases or {}).items()}
        self.studio_aliases: Dict[str, str] = {
            self.variant_key(k): v for k, v in (studio_aliases or {}).items()
        }
        self.stopwords = {self.variant_key(w) for w in (stopwords or set())}
        # 占位演员名（"未知演员" 等），不应进入高频艺人榜
        self.actor_stopwords = {self.variant_key(w) for w in (actor_stopwords or set())}

    # -- 基础 ------------------------------------------------------------
    def variant_key(self, text: str) -> str:
        """分组用的归一化键（等价于模块级 variant_key，保留实例方法便于调用）。"""
        return variant_key(text)

    # -- 标签 ------------------------------------------------------------
    def tag(self, raw: str) -> str:
        """归一化标签：去噪 + 同义词合并。返回用于展示的规范名。"""
        text = (raw or "").strip()
        if not text:
            return ""
        # 去掉前缀式尾巴（如 "系列: xxx" 已在 parser 处理，这里再兜一层）
        key = self.variant_key(text)
        if not key or key in self.stopwords:
            return ""
        return self.synonyms.get(key, text)

    def tag_key(self, raw: str) -> str:
        text = self.tag(raw)
        return self.variant_key(text) if text else ""

    # -- 艺人 ------------------------------------------------------------
    def actor(self, raw: str) -> str:
        """归一化艺人：别名合并，返回用于展示的规范名。"""
        text = (raw or "").strip()
        if not text:
            return ""
        key = self.variant_key(text)
        if not key:
            return ""
        return self.aliases.get(key, text)

    def actor_key(self, raw: str) -> str:
        text = self.actor(raw)
        return self.variant_key(text) if text else ""

    # -- 片商 ------------------------------------------------------------
    def studio(self, raw: str) -> str:
        """归一化片商 / 发行商 / 厂牌：合并同一家的中日英文写法。

        例如 ムーディーズ → MOODYZ、マドンナ → MADONNA。
        """
        text = (raw or "").strip()
        if not text:
            return ""
        return self.studio_aliases.get(self.variant_key(text), text)

    def studio_key(self, raw: str) -> str:
        return self.variant_key(self.studio(raw))

    # -- 占位演员 --------------------------------------------------------
    def is_stop_actor(self, raw: str) -> bool:
        """是否为占位/无效演员名（"未知演员" 等）。"""
        if not raw:
            return True
        key = self.variant_key(raw)
        if not key:
            return True
        if key in self.actor_stopwords:
            return True
        # 兜底：包含"未知/不明/未定"等占位词的直接过滤
        return any(w in raw for w in ("未知", "不明", "未定", "その他", "N/A", "n/a"))

    # -- 通用实体 --------------------------------------------------------
    def entity(self, raw: str) -> str:
        text = (raw or "").strip()
        return re.sub(r"\s+", " ", text) if text else ""

    # -- 加载外部配置 ----------------------------------------------------
    @classmethod
    def from_files(cls, *paths: str) -> "Normalizer":
        syn: Dict[str, str] = dict(DEFAULT_SYNONYMS)
        ali: Dict[str, str] = {}
        studio: Dict[str, str] = {}
        stop: List[str] = []
        astop: List[str] = []
        for p in paths:
            if not p or not os.path.isfile(p):
                continue
            try:
                with open(p, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                continue
            syn.update(data.get("tag_synonyms", {}) or {})
            ali.update(data.get("actor_aliases", {}) or {})
            studio.update(data.get("studio_aliases", {}) or {})
            stop.extend(data.get("tag_stopwords", []) or [])
            astop.extend(data.get("actor_stopwords", []) or [])
        return cls(syn, ali, stop, studio, astop)

    def learn_aliases(self, counter: Mapping[str, int]) -> Dict[str, str]:
        """从频次表中自动发现「同键不同写法」，返回 {别名: 最高频写法}。

        用于艺人归并：同一个 variant_key 下可能有 響蓮 / 响莲 两种写法，
        自动以出现次数最多的写法作为规范名。
        """
        groups: Dict[str, List[tuple]] = {}
        for surface, cnt in counter.items():
            if not surface:
                continue
            key = self.variant_key(surface)
            groups.setdefault(key, []).append((surface, cnt))
        learned: Dict[str, str] = {}
        for key, items in groups.items():
            if len(items) <= 1:
                continue
            items.sort(key=lambda x: (-x[1], len(x[0]), x[0]))
            canonical = items[0][0]
            for surface, _ in items[1:]:
                learned[self.variant_key(surface)] = canonical
        return learned


# ---------------------------------------------------------------------------
# 剧情文本关键词（无第三方分词依赖）
# ---------------------------------------------------------------------------

#: 中文停用词
CN_STOPWORDS = set(
    """
    的 了 是 在 和 与 及 或 就 都 而 也 很 太 更 最 又 还 却 才 把 被 让 给 从 到 对 向 为 以 之 其 这 那
    一个 一种 一样 一起 一直 一些 什么 怎么 这样 那样 因为 所以 但是 如果 可以 已经 还是 只有 没有
    自己 他们 她们 我们 你们 男人 女人 时候 之后 之前 突然 开始 继续 无法 不能 感到 变得 一样
    中 上 下 里 外 前 后 内 外 时 分 秒 年 月 日 次 回 部 个 位 名 种 类 版 集 话
    本作 本片 作品 影片 内容 相关 系列 收录 发行 出品 制作 主演 共演 出演 出演者 简介 剧情 介绍
    不知 为何 终于 紧接 接着 随后 一次 一条 那条 这条 来的 起来 之后 之前 一切 无法 不能
    不知不觉 突如其来 逐渐 突然 完全 非常 特别 于是 结果 最后 开始 继续 这时 那时 此时
    不住 不停 不断 反复 再次 一直 总是 偶尔 有时 好像 似乎 仿佛 几乎 差点 终于 彻底
    """.split()
)

#: 日文停用词（ひらがな常见助词等）
JP_STOPWORDS = set(
    """
    の に は を が と で も から まで へ や な ね よ か ば た だ です ます した して いる ある れる られる
    せる させる ない ぬ ず たい らしい そう よう こと もの ため ほど だけ しか さえ など ながら
    それ これ あれ どれ この その あの どの ここ そこ あそこ どこ 私 僕 俺 君 彼 彼女
    """.split()
)

#: 中文虚词 / 常用功能字：作为词边界，防止抽出"隔壁的""如其来的"这类跨词碎片
CN_BOUNDARY_CHARS = set(
    "的了是在和与或就都而也很太更最又还却才把被让给从到对向为以之其这那"
    "上下里外前后内时我你他她它们个不只没没能够会要可将已更被所很再"
    "于是但是因为所以如果然后并且或者还是以及对于关于通过"
)
#: 日文助词：仅取明确独立的格助词，不动用言词尾（だ/る/た/て 等属于词内）
JP_BOUNDARY_CHARS = set("のにはをがもへやとで")

_BOUNDARY_CHARS = CN_BOUNDARY_CHARS | JP_BOUNDARY_CHARS

#: 句号、逗号等标点
_PUNCT_PAT = re.compile(
    r"[，。、；：！？,.!?;:()\[\]【】「」『』（）\"'“”‘’…—\-~～/\\|　\s]+"
)

#: 只保留中日英字符序列
_TOKEN_PAT = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\u31f0-\u31ffA-Za-z]+")


def split_segments(text: str) -> List[str]:
    """把文本切成不含虚词与标点的短片段，作为新词发现的基本单位。"""
    if not text:
        return []
    out: List[str] = []
    for piece in _PUNCT_PAT.split(text):
        for seg in re.split(f"[{re.escape(''.join(_BOUNDARY_CHARS))}]", piece):
            seg = seg.strip()
            if len(seg) >= 2:
                out.append(seg)
    return out


def _entropy(counter: "Counter | None") -> float:
    """香农熵（以 2 为底）。无邻居分布时返回 0。"""
    if not counter:
        return 0.0
    total = sum(counter.values())
    if total <= 0:
        return 0.0
    ent = 0.0
    for c in counter.values():
        p = c / total
        if p > 0:
            ent -= p * math.log(p, 2)
    return ent


_EDGE_CHARS = _BOUNDARY_CHARS | set("的地得着了过")


def _trim_function_chars(gram: str) -> str:
    """剥掉首尾虚词（"隔壁的" -> "隔壁"、"来的" -> "来"）。
    允许剥到单字，由调用方判断长度是否达标后丢弃。"""
    s = gram
    while len(s) > 1 and s[0] in _EDGE_CHARS:
        s = s[1:]
    while len(s) > 1 and s[-1] in _EDGE_CHARS:
        s = s[:-1]
    return s


def extract_keywords(
    texts: Iterable[str],
    *,
    top_n: int = 120,
    max_len: int = 4,
    min_count: int = 4,
    min_solidity: float = 25.0,
    min_entropy: float = 0.45,
    max_glue: float = 0.92,
    max_docs: int = 8000,
    max_chars_per_doc: int = 1500,
    fallback: bool = True,
) -> List[tuple]:
    """从剧情文本中做**无词典新词发现**，返回 [(词, 次数), ...]。

    不依赖 jieba / MeCab 等第三方分词库，采用四道检验的无监督方案：

    1. **凝固度 Solidity**：候选词任意切分后的最小点互信息。
       内部结合紧密的真词（"居酒屋"）远高于偶然拼接（"司突然"）。
    2. **左右邻字熵 Branching Entropy**：上下文的丰富程度。
       自由成词（"豪雨"）邻字多样；词碎片（"与不擅"）邻字高度固定。
    3. **扩展度 Glue**：若某一侧总是黏着同一个字（如"力迎合"总带"全"），
       说明它只是更长片段的一部分，不是完整词。
    4. **虚词剥离**：把"隔壁的""如其来的"这类被助词污染的候选修回词干。

    说明：该算法依赖语料的**上下文多样性**。若库里大量简介是整句复读
    （常见于批量抓取的模板化简介），熵值会普遍偏低、抽词数量变少，
    此时会退化为"高频短语"兜底模式（fallback）。

    max_docs / max_chars_per_doc 用于把 5 万级语料控制在可接受的耗时内。
    """
    from collections import Counter, defaultdict

    docs: List[str] = []
    for text in texts:
        if not text:
            continue
        docs.append(text[:max_chars_per_doc])
        if len(docs) >= max_docs:
            break
    if not docs:
        return []

    # ---- Pass 1：按句切分，句内统计 1..max_len 的所有子串 ----
    counts: "Counter" = Counter()
    sentences: List[str] = []
    for text in docs:
        for piece in _PUNCT_PAT.split(text):
            s = piece.strip()
            if len(s) >= 2:
                sentences.append(s)
    for s in sentences:
        n = len(s)
        for size in range(1, min(max_len, n) + 1):
            for i in range(n - size + 1):
                counts[s[i:i + size]] += 1
    if not counts:
        return []

    total_chars = sum(c for g, c in counts.items() if len(g) == 1) or 1

    candidates = {
        g for g, c in counts.items()
        if 2 <= len(g) <= max_len and c >= min_count
    }
    if not candidates:
        return []

    # ---- Pass 2：收集左右邻字分布（句内相邻字符）----
    left_nb: "Dict[str, Counter]" = defaultdict(Counter)
    right_nb: "Dict[str, Counter]" = defaultdict(Counter)
    max_len_c = max(len(g) for g in candidates)
    for s in sentences:
        n = len(s)
        for size in range(2, min(max_len_c, n) + 1):
            for i in range(n - size + 1):
                g = s[i:i + size]
                if g in candidates:
                    if i > 0:
                        left_nb[g][s[i - 1]] += 1
                    if i + size < n:
                        right_nb[g][s[i + size]] += 1

    def solidity(g: str) -> float:
        cg = counts[g]
        best = float("inf")
        for k in range(1, len(g)):
            cl, cr = counts[g[:k]], counts[g[k:]]
            if cl <= 0 or cr <= 0:
                return 0.0
            pmi = (cg / total_chars) / ((cl / total_chars) * (cr / total_chars))
            best = min(best, pmi)
        return best if best != float("inf") else 0.0

    def glue_ratio(g: str, nb: "Counter") -> float:
        """最强黏着邻居占比：越接近 1 说明这个词越不独立。"""
        if not nb or counts[g] <= 0:
            return 0.0
        return max(nb.values()) / counts[g]

    # ---- Pass 3：四道检验 ----
    scored: List[tuple] = []
    for g in candidates:
        cnt = counts[g]
        sol = solidity(g)
        if sol < min_solidity:
            continue
        le, re_ = _entropy(left_nb.get(g)), _entropy(right_nb.get(g))
        if min(le, re_) < min_entropy:
            continue
        glue = max(glue_ratio(g, left_nb.get(g)), glue_ratio(g, right_nb.get(g)))
        if glue > max_glue:
            continue

        word = _trim_function_chars(g)
        if len(word) < 2 or word in CN_STOPWORDS or word in JP_STOPWORDS:
            continue
        if word != g:
            cnt = counts.get(word, cnt)  # 用剥离后的词干频次

        length_bonus = 1.0 + 0.5 * (len(word) - 2)
        score = cnt * length_bonus * (1.0 + min(le, re_) * 0.6) * math.log1p(sol)
        scored.append((word, cnt, score, sol, min(le, re_)))

    # 合并同词干
    merged: "Dict[str, list]" = {}
    for word, cnt, score, sol, ent in scored:
        cur = merged.get(word)
        if cur is None or score > cur[2]:
            merged[word] = [word, cnt, score, sol, ent]
    scored = sorted(merged.values(), key=lambda x: (-x[2], -x[1], x[0]))

    # ---- Pass 4：子串压制 ----
    kept: List[list] = []
    for row in scored:
        word, cnt = row[0], row[1]
        dominated = False
        for k in kept:
            if word in k[0] and cnt <= k[1] * 1.6:
                dominated = True
                break
        if not dominated:
            kept.append(row)
        if len(kept) >= top_n:
            break

    result = [(r[0], r[1]) for r in kept[:top_n]]

    # ---- 兜底：语料过于模板化时，放宽"邻字熵/扩展度"，但仍保留凝固度门槛 ----
    if fallback and len(result) < 12:
        fb_solidity = max(min_solidity * 0.8, 20.0)
        phrases: "Dict[str, float]" = {}
        for g, c in counts.items():
            if not (2 <= len(g) <= max_len) or c < max(min_count, 3):
                continue
            sol = solidity(g)
            if sol < fb_solidity:
                continue
            w = _trim_function_chars(g)
            if len(w) < 2 or w in CN_STOPWORDS or w in JP_STOPWORDS:
                continue
            cnt = counts.get(w) or c
            # 可扩展惩罚：若某一侧总黏着同一个字，说明只是更长片段的一部分
            glue = max(glue_ratio(g, left_nb.get(g)), glue_ratio(g, right_nb.get(g)))
            penalty = 0.25 if glue >= 0.9 else (0.7 if glue >= 0.7 else 1.0)
            score = cnt * (1 + 0.4 * (len(w) - 2)) * math.log1p(sol) * penalty
            if score > phrases.get(w, (0.0, 0))[0]:
                phrases[w] = (score, cnt)
        ranked = sorted(phrases.items(), key=lambda x: (-x[1][0], -len(x[0]), x[0]))
        out: List[tuple] = []
        counts_out: List[int] = []
        for w, (_score, c) in ranked:
            if any((w in kw or kw in w) and c <= kc * 1.6 for kw, kc in zip(out, counts_out)):
                continue
            out.append(w)
            counts_out.append(c)
            if len(out) >= top_n:
                break
        return list(zip(out, counts_out))

    return result


__all__ = ["Normalizer", "extract_keywords", "KANJI_VARIANTS"]
