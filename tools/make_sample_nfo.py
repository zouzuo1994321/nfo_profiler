# -*- coding: utf-8 -*-
"""生成合成 NFO 测试样本（用于性能压测与算法验证）。

用法：
    python tools/make_sample_nfo.py --out _testdata --count 5000 --seed 42
"""

from __future__ import annotations

import argparse
import os
import random
from datetime import datetime, timedelta

ACTORS = [
    "響蓮", "三上悠亜", "明日花キララ", "桃乃木かな", "葵つかさ", "高橋しょう子",
    "天使もえ", "橋本ありな", "河北彩花", "山岸逢花", "小島みなみ", "紗倉まな",
    "桜木ルナ", "涼森れむ", "石川澪", "miru", "渚あいり", "希崎ジェシカ",
]
TAGS = [
    "美少女", "巨乳", "苗条", "中出", "顔射", "口交", "痴女", "陵辱", "NTR", "出轨",
    "人妻", "熟女", "女教师", "护士", "OL", "学生妹", "妹妹", "姐姐", "温泉", "办公室",
    "汗だく", "潮吹", "SM", "拘束", "自慰", "按摩", "洗体", "Cosplay", "裙装", "丝袜",
    "单体作品", "多人共演", "DMM独家", "企划物", "4小时以上", "长身", "贫乳", "美臀",
]
STUDIOS = ["ダスッ!", "S1 NO.1 STYLE", "MOODYZ", "IdeaPocket", "PREMIUM", "Attackers", "MADONNA", "FALENO"]
DIRECTORS = ["イナバール", "五右衛門", "麒麟", "ZAMPA", "豆沢豆太郎", "タイガー小堺"]
LABELS = ["ダスッ!", "S1", "MOODYZ", "WANZ", "ATTACKERS"]
PREFIXES = ["DSOD", "SSIS", "MIDE", "IPX", "ABP", "PRED", "ATID", "FSDSS"]
RES_TAGS = ["720P", "1080P", "4K"]
CODECS = ["AVC1", "HEVC"]
#: 剧情由"场景 + 人物 + 行为"随机组合，贴近真实语料的多样性
PLOT_SCENES = [
    "深夜的办公室里", "雨天的公交车上", "温泉旅馆的客房", "高中的保健室",
    "健身房更衣室", "出差途中的商务酒店", "空无一人的图书馆", "海边民宿的阳台",
    "医院的深夜值班室", "公寓的洗衣房", "公司的会议室", "乡下的老宅",
    "清晨的厨房", "末班电车的车厢", "美容院的VIP包间",
]
PLOT_ROLES = [
    "新来的女上司", "隔壁的美人妻", "学生的姐姐", "实习医生",
    "健身教练", "女教师", "社长的秘书", "快递员", "图书管理员",
    "邻居的太太", "前辈的未婚妻", "保洁阿姨", "护士长", "空姐",
]
PLOT_ACTS = [
    "因为突如其来的豪雨全身湿透",
    "在酒精的作用下逐渐放开了防备",
    "一次不经意的肢体接触后气氛骤变",
    "被温柔的指尖抚过后彻底放弃了抵抗",
    "在密闭的空间里呼吸逐渐急促",
    "汗水顺着锁骨滑落，理智彻底崩断",
    "被一步步诱导，最终越过了那条界线",
    "在昏暗的灯光下交换了滚烫的体温",
    "用尽全力迎合，反复索取直到天亮",
    "在监视器的另一端被彻底看穿",
    "被命令保持安静，只能咬住手指",
    "在狭窄的试衣间里无法逃离",
]


CONNECTORS = ["，", "，随后", "，紧接着", "，不知为何", "，就这样", "，结果", "，终于"]
ENDINGS = ["。", "。一切都回不去了。", "。理智彻底崩断。", "。夜还很长。", "。"]


#: 词级词表：随机组合成句，保证每个词都有丰富多样的左右上下文
VOCAB_SUBJ = ["女上司", "美人妻", "学生妹", "实习医生", "健身教练", "女教师", "秘书", "邻居太太",
              "护士长", "图书管理员", "空姐", "店长", "家教", "幼驯染", "义妹", "嫂子"]
VOCAB_PLACE = ["办公室", "保健室", "更衣室", "商务酒店", "图书馆", "海边民宿", "值班室",
               "洗衣房", "会议室", "老宅", "厨房", "车厢", "试衣间", "电梯间", "仓库"]
VOCAB_ADJ = ["昏暗", "狭窄", "密闭", "潮湿", "闷热", "安静", "冰冷", "暧昧", "陌生", "温柔"]
VOCAB_VERB = ["靠近", "触碰", "拥抱", "亲吻", "喘息", "颤抖", "迎合", "索取", "纠缠", "沉溺",
              "沦陷", "抵抗", "诱惑", "试探", "注视", "躲闪", "贴近", "抚摸"]
VOCAB_NOUN = ["指尖", "唇瓣", "锁骨", "呼吸", "汗水", "体温", "灯光", "窗帘", "床单", "酒精",
              "监视器", "理智", "界线", "手指", "脚踝", "发丝", "衬衫", "裙摆"]
VOCAB_ADV = ["突然", "缓缓", "悄悄", "用力", "反复", "不住", "终于", "彻底"]
CONJ = ["，", "，随后", "，紧接着", "，不知为何", "，就这样", "，结果", "，终于", "，然后"]
PARTICLE = ["的", "地", "得", "了", "着", "过"]


def make_plot(rng: random.Random) -> str:
    """词级随机组合成句：模拟真实语料的上下文多样性。

    注意：真实简介是整句模板复用时，邻字熵会普遍偏低、新词发现会退化为
    高频短语兜底模式。这里刻意做成词级组合，用于验证算法的正常路径。
    """
    sents = []
    for _ in range(rng.randint(2, 4)):
        subj = rng.choice(VOCAB_SUBJ)
        place = rng.choice(VOCAB_PLACE)
        clauses = []
        for _ in range(rng.randint(1, 2)):
            tpl = rng.randint(0, 5)
            if tpl == 0:
                c = f"{rng.choice(VOCAB_ADV)}{rng.choice(VOCAB_VERB)}{rng.choice(PARTICLE)}{rng.choice(VOCAB_NOUN)}"
            elif tpl == 1:
                c = f"{rng.choice(VOCAB_ADJ)}{rng.choice(PARTICLE)}{rng.choice(VOCAB_NOUN)}{rng.choice(VOCAB_VERB)}"
            elif tpl == 2:
                c = f"{rng.choice(VOCAB_NOUN)}{rng.choice(VOCAB_VERB)}{rng.choice(PARTICLE)}{rng.choice(VOCAB_NOUN)}"
            elif tpl == 3:
                c = f"{rng.choice(VOCAB_ADV)}{rng.choice(VOCAB_VERB)}"
            elif tpl == 4:
                c = f"{rng.choice(VOCAB_ADJ)}{rng.choice(PARTICLE)}{rng.choice(VOCAB_PLACE)}"
            else:
                c = f"{rng.choice(VOCAB_VERB)}{rng.choice(VOCAB_NOUN)}"
            clauses.append(c)
        body = f"{place}里{rng.choice(PARTICLE) if rng.random() < .5 else ''}{subj}" + rng.choice(CONJ).join([""] + clauses)
        sents.append(body)
    return "。".join(sents) + "。"

NFO_TEMPLATE = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<!--created on {created} by tinyMediaManager 5.3.2 for KODI-->
<movie>
  <title>{num} {title_jp} 「{actor}」</title>
  <originaltitle>{num} {title_jp}</originaltitle>
  <sorttitle>{num} {title_jp}</sorttitle>
  <epbookmark/>
  <year>{year}</year>
  <ratings/>
  <userrating>{rating}</userrating>
  <set>
    <name>{series}</name>
    <overview/>
  </set>
  <plot>{plot}</plot>
  <outline>{plot}</outline>
  <tagline>发行日期 {premiered}</tagline>
  <runtime>{runtime}</runtime>
  <mpaa>US:NC-17 / US:Rated NC-17</mpaa>
  <certification>US:NC-17 / US:Rated NC-17</certification>
  <id/>
  <tmdbid/>
  <status/>
  <code/>
  <premiered>{premiered}</premiered>
  <watched>{watched}</watched>
  <playcount>{playcount}</playcount>
{genres}  <studio>{studio}</studio>
  <director>{director}</director>
{tags}  <actor>
    <name>{actor}</name>
  </actor>
{extra_actors}  <trailer/>
  <languages/>
  <dateadded>{dateadded}</dateadded>
  <fileinfo>
    <streamdetails>
      <video>
        <codec>{vcodec}</codec>
        <aspect>1.78</aspect>
        <width>{width}</width>
        <height>{height}</height>
        <resolution>{height}</resolution>
        <durationinseconds>{duration}</durationinseconds>
      </video>
      <audio>
        <codec>aac</codec>
        <language/>
        <channels>2</channels>
      </audio>
    </streamdetails>
  </fileinfo>
  <release>{premiered}</release>
  <num>{num}</num>
  <customrating>NC-17</customrating>
  <series>{series}</series>
  <maker>{studio}</maker>
  <publisher>{studio}</publisher>
  <label>{label}</label>
  <poster>https://www.javbus.com/pics/thumb/{num}.jpg</poster>
  <cover>https://www.javbus.com/pics/cover/{num}_b.jpg</cover>
  <website>https://www.javbus.com/{num}</website>
  <javdbsearchid>{num}</javdbsearchid>
  <!--tinyMediaManager meta data-->
  <source>UNKNOWN</source>
  <edition>NONE</edition>
  <original_filename>{num}.mp4</original_filename>
  <user_note/>
  <english_title/>
  <crew>
    <name>{director}</name>
    <role subrole="Director">DIRECTOR</role>
  </crew>
  <tmm_locked/>
</movie>
"""

TITLE_JP = [
    "出張先で集中豪雨 嫌いな上司の前でまさか酔い潰れ…突然の相部屋",
    "深夜のオフィスで二人きり 残業中に上司とはじめての不倫関係",
    "隣の人妻が毎朝挨拶してくる ある日ついに一線を越えてしまった",
    "温泉旅館の女将が熱烈接待 酒が回った後は全てが当たり前になった",
    "保健室の先生の優しい指先に逆らえない放課後の秘密",
    "雨のバス車内 見知らぬ二人が豪雨に閉じ込められて",
]


def build_one(rng: random.Random, idx: int) -> tuple:
    prefix = rng.choice(PREFIXES)
    num = f"{prefix}-{rng.randint(1, 999):03d}"
    actor = rng.choice(ACTORS)
    # 20% 概率使用简体异体写法，模拟真实异构数据
    if rng.random() < 0.2:
        actor = actor.replace("響", "响").replace("蓮", "莲").replace("桜", "樱")
    extras = []
    if rng.random() < 0.18:
        extras = rng.sample([a for a in ACTORS if a != actor], rng.randint(1, 2))
    tags = rng.sample(TAGS, rng.randint(4, 9))
    if rng.random() < 0.12:
        tags.append("中出し" if rng.random() < 0.5 else "內射")
    genre_items = list(CODECS[:1]) + [rng.choice(RES_TAGS)] + [prefix, actor] + tags
    genre_items += [f"系列: {rng.choice(TITLE_JP)[:20]}", f"片商: {rng.choice(STUDIOS)}"]
    genre_items += ["有码" if rng.random() < 0.75 else "无码"]

    height = rng.choice([720, 720, 1080, 1080, 2160, 480])
    width = int(height * 16 / 9)
    runtime = rng.choice([60, 90, 118, 120, 135, 150, 180, 210, 240])
    premiered = (datetime(2019, 1, 1) + timedelta(days=rng.randint(0, 2600))).strftime("%Y-%m-%d")
    dateadded = (datetime(2023, 1, 1) + timedelta(days=rng.randint(0, 1200))).strftime("%Y-%m-%d %H:%M:%S")

    genres = "".join(f"  <genre>{g}</genre>\n" for g in genre_items)
    tagblock = "".join(f"  <tag>{g}</tag>\n" for g in genre_items)
    extra = "".join(f"  <actor>\n    <name>{a}</name>\n  </actor>\n" for a in extras)

    body = NFO_TEMPLATE.format(
        created=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        num=num,
        title_jp=rng.choice(TITLE_JP),
        actor=actor,
        year=premiered[:4],
        rating=round(rng.uniform(4, 10), 1),
        series=rng.choice(TITLE_JP)[:24],
        plot=make_plot(rng),
        premiered=premiered,
        runtime=runtime,
        watched="true" if rng.random() < 0.3 else "false",
        playcount=rng.randint(0, 3) if rng.random() < 0.3 else 0,
        genres=genres,
        studio=rng.choice(STUDIOS),
        director=rng.choice(DIRECTORS),
        tags=tagblock,
        extra_actors=extra,
        dateadded=dateadded,
        vcodec=rng.choice(CODECS),
        width=width,
        height=height,
        duration=runtime * 60 + rng.randint(0, 59),
        label=rng.choice(LABELS),
    )
    return num, body


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="_testdata")
    ap.add_argument("--count", type=int, default=5000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--per-dir", type=int, default=200, help="每个子目录放多少个 nfo")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    os.makedirs(args.out, exist_ok=True)
    made = 0
    dir_no = 0
    while made < args.count:
        sub = os.path.join(args.out, f"batch_{dir_no:03d}")
        os.makedirs(sub, exist_ok=True)
        for _ in range(min(args.per_dir, args.count - made)):
            num, body = build_one(rng, made)
            with open(os.path.join(sub, f"{num}.nfo"), "w", encoding="utf-8") as fh:
                fh.write(body)
            made += 1
        dir_no += 1
    print(f"已生成 {made} 个 NFO 到 {os.path.abspath(args.out)}")


if __name__ == "__main__":
    main()
