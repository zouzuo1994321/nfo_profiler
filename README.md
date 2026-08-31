# NFO 画像矿工 · NFO Profiler

> ⚠️ **声明：本软件仅供娱乐向的个人使用。** 它读取你**自己收藏**的影视刮削信息（`.nfo` 元数据文件），
> 用于分析**个人的观影偏好**并生成一份可视化画像报告。它不是爬虫、不下载任何内容、不访问任何网站，
> 仅对你本地已有的文件做离线统计。请遵守所在地区法律法规，勿用于任何商业或侵权用途。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Python](https://img.shields.io/badge/Python-3.9%2B-blue.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/Platform-Windows%20%2F%20Linux%20%2F%20macOS-lightgrey.svg)]()
[![Zero Dependency](https://img.shields.io/badge/Core-零第三方依赖-brightgreen.svg)]()

从海量 KODI / tinyMediaManager / Emby / Jellyfin 等媒体中心生成的 `.nfo` 中提取全部有效信息，
生成**完整的用户画像报告**，并梳理出**高频标签、高频艺人、高频片商 / 系列 / 导演**等统计，支持一键导出 CSV / JSON / XLSX / Markdown / HTML。

核心只用 Python 标准库，**零第三方依赖即可运行**（仅导出 XLSX 需要可选依赖 `openpyxl`）。

---

## 目录

- [一、它能做什么](#一它能做什么)
- [二、快速开始](#二快速开始)
  - [方式 A：直接下载打包版 exe（推荐，Windows）](#方式-a直接下载打包版-exewindows)
  - [方式 B：从源码运行](#方式-b从源码运行)
- [三、命令详解](#三命令详解)
- [四、NFO 文件规范（本工具读取什么）](#四nfo-文件规范本工具读取什么)
- [五、提取了哪些信息](#五提取了哪些信息)
- [六、画像与统计产出](#六画像与统计产出)
- [七、导出格式](#七导出格式)
- [八、多路径扫描与数据源管理](#八多路径扫描与数据源管理)
- [九、艺人 / 标签归并](#九艺人--标签归并)
- [十、性能实测](#十性能实测)
- [十一、目录结构](#十一目录结构)
- [十二、从源码打包 exe](#十二从源码打包-exe)
- [十三、常见问题](#十三常见问题)
- [十四、免责声明](#十四免责声明)
- [十五、开源协议](#十五开源协议)

---

## 一、它能做什么

- **批量解析**：递归扫描一个或多个目录下的 `.nfo`（默认增量，二次运行只处理变动文件）
- **容错兜底**：编码自动探测、畸形 XML 修复、结构化损坏正则抢救，单文件失败不中断
- **用户画像**：一句话画像 + 八维偏好雷达
- **高频统计**：高频标签 / 艺人 / 片商 / 系列 / 导演 / 番号前缀 / 剧情高频词
- **关系分析**：标签共现矩阵、高频共演组合
- **一键导出**：CSV / JSON / XLSX / Markdown / 单文件 HTML（不引任何 CDN，双击即开）
- **本地 Web 界面**：图形化操作，开箱即用
- **多数据源**：可登记多个盘符 / 目录，分别统计、分别导出

---

## 二、快速开始

### 方式 A：直接下载打包版 exe（推荐，Windows）

前往 [Releases](../../releases) 下载 `nfo_profiler.exe`，**双击即可使用**——程序会自动：

1. 打开浏览器到本地网页 `http://127.0.0.1:9527`；
2. 在网页里**浏览并勾选一个或多个 NFO 目录**（支持多数据源），点击「开始扫描」；
3. 网页**实时显示扫描进度、当前正在处理的文件、滚动日志**；
4. 扫描完成后一键生成画像报告、导出数据。

> **刷新网页不会中断扫描，关掉网页后端仍继续运行**（扫描在后台线程进行，与网页请求解耦）。
> 想停止时，双击根目录下的 **`kill.bat`** 即可结束整个进程树（按端口 9527 查找并 `taskkill /T /F`）。

如果你更习惯命令行，也可以这样用（端口默认 9527）：

```bat
:: 扫描一个或多个 NFO 目录入库（默认增量，第二次起几乎瞬时）
nfo_profiler.exe scan "Y:\【03】Jav甄选" "D:\MyMovies" --workers 8

:: 生成可视化画像报告（单文件 HTML，双击即可打开）
nfo_profiler.exe report --out output\我的画像.html

:: 导出全部数据（CSV / JSON / XLSX / Markdown / HTML）
nfo_profiler.exe export --out output

:: 启动本地 Web 界面（图形化，双击 exe 即等效于此，默认端口 9527）
nfo_profiler.exe ui --port 9527
```

> 数据库与输出默认写在**当前目录**下的 `output/`。建议把 `nfo_profiler.exe` 放到一个长期存放分析结果的文件夹再双击运行。
> 打包版已内置 `config/synonyms.json` 归一化表；如需自定义，放一份 `config/synonyms.json` 到运行目录即可覆盖。

### 方式 B：从源码运行

```bash
git clone <本仓库地址>
cd 提取用户画像

# 核心功能只需 Python 3.9+，零依赖
python run.py scan "Y:\Jav" --workers 8
python run.py report --out output/我的画像.html
python run.py ui --port 9527
```

查看全部命令：`python run.py -h`

---

## 三、命令详解

| 命令 | 作用 | 常用参数 |
|---|---|---|
| `scan <目录...>` | 扫描一个或多个 NFO 目录并写入 SQLite | `--workers 8` 并发进程数<br>`--paths-file f.txt` 从文件读目录列表<br>`--full` 全量重扫（忽略增量缓存）<br>`--no-probe-video` 不探测视频文件（略快） |
| `sources` | 查看 / 移除已登记的数据源 | `--remove <ROOT>` 删除某数据源及其记录 |
| `report` | 生成单文件 HTML 画像报告 | `--out` 输出路径<br>`--movies 1000` 报告内嵌明细条数<br>`--top-tags 40` / `--top-actors 30`<br>`--no-cooccurrence` / `--no-keywords` 跳过耗时项<br>`--source <ROOT>` 只统计某数据源 |
| `export` | 导出数据文件 | `--format csv,json,xlsx,md,html`<br>`--out` 输出目录<br>`--source <ROOT>` 只导出某数据源 |
| `ui` | 启动本地 Web 界面（双击 exe 即等效；默认端口 9527） | `--port 9527` `--host` `--no-browser` |
| `stats` | 查看数据库概况 | — |
| `reindex` | 用同义词表重建归一化键 | `--full` 强制重算 |

全局参数：`--db`（数据库路径，默认 `output/nfo.db`）、`--config`（同义词配置）。

---

## 四、NFO 文件规范（本工具读取什么）

`.nfo` 是 **Kodi / tinyMediaManager / Emby / Jellyfin / Plex 元数据代理**在刮削影视资料后，
写在每个影片文件夹里的 **XML 元数据文件**。它本质上是影片的“信息卡片”，记录标题、年份、演员、导演、
片商、分级、剧情简介、分辨率等技术规格——**不含任何视频内容本身**。

一个典型的 `<movie>.nfo` 结构（节选）：

```xml
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<movie>
  <title>作品标题</title>
  <sorttitle>...</sorttitle>
  <year>2024</year>
  <premiered>2024-03-15</premiered>
  <studio>片商名</studio>
  <label>厂牌</label>
  <director>导演名</director>
  <plot>剧情简介……</plot>
  <genre>标签A</genre>
  <genre>标签B</genre>
  <tag>标签C</tag>
  <actor>
    <name>演员名</name>
    <role>角色</role>
    <order>1</order>
  </actor>
  <rating>8.5</rating>
  <runtime>120</runtime>
  <height>1080</height>
  <videocodec>HEVC</videocodec>
  <uniqueid type="jav" default="true">DSOD-069</uniqueid>
</movie>
```

**本工具只读取这些元数据字段**，用于分析你**个人收藏**的偏好分布，不修改、不删除、不传播原始 `.nfo`，
更不会触碰任何视频文件。它适合回答“我到底偏好哪些标签 / 艺人 / 片商 / 画质”这类个人向问题。

> 提示：如果你的媒体库用 Kodi / tinyMediaManager 管理，这些 `.nfo` 已经存在；直接把对应目录交给 `scan` 即可。

---

## 五、提取了哪些信息

解析器针对 KODI / tinyMediaManager 的 NFO 做了完整适配：

- **基础信息**：番号、番号前缀、标题、清洁标题（去掉标题尾部 `「演员名」`）、原名、年份、发行日期、入库时间
- **内容信息**：剧情简介、系列、片商、制作商、发行商、厂牌、导演、演员及其排序
- **标签**：`genre` / `tag` 全量提取，并做智能分类（见下文）
- **技术规格**：分辨率、宽高、视频 / 音频编码、时长
- **行为数据**：用户评分、是否看过、播放次数
- **文件信息**：NFO 路径与大小、编码、视频文件是否存在及其大小（用于容量统计）

### 标签智能去噪

NFO 常常把结构化信息也塞进 `genre`/`tag` 里。程序会把它们拆回各自的字段，保证「高频标签」统计的是**真正的内容标签**：

| 原始标签 | 归类 |
|---|---|
| `系列: xxx` | → 系列字段 |
| `片商: xxx` / `发行: xxx` / `レーベル: xxx` | → 片商 / 发行商 / 厂牌字段 |
| `导演: xxx` | → 导演字段 |
| `720P` `4K` `AVC1` `HEVC` `高清` | → 技术标签（不计入内容标签） |
| `有码` / `无码` | → 马赛克状态字段 |
| `DSOD`（与番号前缀相同）、`响莲`（与演员名相同） | → 冗余标签，剔除 |
| 其余 | → **内容标签**，参与高频统计 |

### 容错设计

5 万量级数据里什么样的脏数据都会有，解析器逐层兜底：

- **编码**：UTF-8 / UTF-8-BOM / GB18030 / Shift-JIS / CP1252 依次尝试
- **畸形 XML**：清洗非法控制字符与未转义的 `&`；解析失败后退化为 `<movie>` 片段重试
- **结构性损坏**：正则兜底抢救关键字段，标记为「降级」而非丢弃
- **绝不中断**：单个文件解析失败只记录不抛出，任务继续跑完

---

## 六、画像与统计产出

### 用户画像
- **一句话画像**：自动总结偏好标签、最常收录艺人、主力片商、画质倾向
- **八维偏好雷达**：标签广度、艺人专一度、片商忠诚度、高清偏好、长片偏好、打分积极度、系列收集度、追新度

### 高频词统计
高频标签（Top 40）、高频艺人（Top 30，含每人平均评分 / 活跃年份 / 代表片商 / 代表标签 / 常搭档）、
片商、发行商、厂牌、系列、导演、番号前缀、剧情高频词

### 分布与关系
- **分布**：分辨率、有码/无码、评分、时长、发行年份、入库月份、入库时段（24h）、入库星期、演出人数
- **关系**：标签共现矩阵与最强共现组合、高频共演组合
- **剧情高频词**：无词典新词发现（凝固度 + 左右邻字熵 + 扩展度三重检验，无 jieba / MeCab 依赖）

---

## 七、导出格式

| 格式 | 内容 |
|---|---|
| **HTML** | 单文件自包含可视化报告，**不引任何 CDN**，双击即开；含主题切换、表格搜索排序分页、前端导出 CSV |
| **CSV** | 17 张表（UTF-8-BOM，Excel 双击不乱码） |
| **XLSX** | 多工作表，带表头样式、冻结窗格、自动筛选（需可选依赖 `openpyxl`） |
| **JSON** | 完整结构化数据，便于二次开发 |
| **Markdown** | 可读的画像卡片，便于贴到笔记里 |

CSV 明细表包含 27 列：番号、标题、清洁标题、年份、发行日期、入库时间、片商、制作商、发行商、厂牌、
系列、导演、演员、内容标签、技术标签、马赛克、分辨率、视频编码、时长、评分、已看、播放次数、
番号前缀、视频文件、视频大小、来源链接、NFO 路径。

---

## 八、多路径扫描与数据源管理

可以一次传入多个目录（不同盘符 / 不同目录均可），或把目录列表写进文件用 `--paths-file` 读取：

```bat
nfo_profiler.exe scan "Y:\Jav" "D:\Movies" "E:\收藏"
nfo_profiler.exe scan --paths-file my_dirs.txt
```

规则：
- 同一个库可登记任意多个数据源，文件按「最长匹配根」归属，不会重复入库
- 每个文件记录来源根，后续可按数据源 `report --source <ROOT>` / `export --source <ROOT>` 分别统计导出
- `sources` 命令查看各数据源作品数；`sources --remove <ROOT>` 移除某数据源及其全部记录

---

## 九、艺人 / 标签归并

同一个艺人或标签常有繁简、中日文、异体字等多种写法，会被错误拆成多条。编辑 `config/synonyms.json` 即可修正：

```json
{
  "tag_synonyms":  { "中出し": "中出", "內射": "中出", "フェラ": "口交" },
  "actor_aliases": { "响莲": "響蓮", "三上悠亚": "三上悠亜" },
  "tag_stopwords": ["高清", "超清"]
}
```

改完后执行 `python run.py reindex --full`，再重新生成报告即可生效。
程序**内置异体字归一化表**（293 条，覆盖響/响、蓮/莲、桜/樱、沢/泽、愛/爱 等），并在统计时
自动把「同键不同写法」归到出现次数最多的那个写法上。

---

## 十、性能实测

在 5 万份 NFO（8 进程，本地 SSD）上的实测：

| 环节 | 耗时 |
|---|---|
| 全量扫描入库 | **28 秒**（约 1700 文件/秒） |
| 增量重扫（无变动） | **0.6 秒** |
| 全量分析（含共现 + 抽词） | **8 秒** |
| 全量导出（CSV+XLSX+JSON+MD+HTML） | 约 40 秒 |
| 数据库体积 | 约 90 MB |

**注意**：若数据在移动 / 网络盘上，首次全量扫描的瓶颈在磁盘 IO 而非解析，耗时可能显著更长；
建议先跑一次，之后每次增量扫描只处理变动文件，几秒即可完成。
提速建议：首次扫描用 `--workers 8`；只要统计不要容量可加 `--no-probe-video`；
数据量极大时用 `--no-cooccurrence --no-keywords` 跳过两个耗时项。

---

## 十一、目录结构

```
提取用户画像/
├── run.py                     命令行入口
├── nfo_profiler/
│   ├── parser.py              NFO 容错解析（编码/XML/降级抢救/标签分类）
│   ├── normalize.py           异体字归一化、同义词、无词典新词发现
│   ├── store.py               SQLite 仓储（增量指纹、批量写入）
│   ├── scanner.py             多进程扫描引擎
│   ├── analyze.py             画像与高频统计
│   ├── report.py              单文件 HTML 报告模板（原生 SVG 图表）
│   ├── exporter.py            CSV / XLSX / JSON / Markdown 导出
│   ├── webui.py               本地 Web 界面（标准库 http.server）
│   └── cli.py                 命令行
├── config/synonyms.json       同义词与别名配置（打包版内置一份）
├── tools/
│   ├── make_sample_nfo.py     生成合成测试数据（压测用）
│   └── smoke_test_report.js   报告渲染冒烟测试
├── output/                    默认输出目录（数据库、报告、导出文件）
└── requirements.txt           可选依赖声明
```

设计上分为 **解析 → 入库 → 分析 → 导出** 四层，互不耦合，可单独调用：

```python
from nfo_profiler.store import Store
from nfo_profiler.analyze import Analyzer

store = Store("output/nfo.db")
data = Analyzer(store).build_report_data()   # 拿到全部统计结果
```

---

## 十二、从源码打包 exe

本工具核心零第三方依赖，可直接用 PyInstaller 打包（多进程入口已用 `multiprocessing.freeze_support()` 保护）：

```bash
python -m venv .venv_build
.venv_build/Scripts/python.exe -m pip install pyinstaller
.venv_build/Scripts/pyinstaller.exe --noconfirm --onefile --name nfo_profiler --console ^
    --add-data "config/synonyms.json;config/synonyms.json" run.py
```

产物位于 `dist/nfo_profiler.exe`。

---

## 十三、常见问题

**Q：报告里的「未知」分辨率 / 马赛克状态很多？**
A：分辨率来自 `<height>` 或画质标签，马赛克来自「有码/无码/無修正/UNCENSORED」等关键词。
若 NFO 里本身没写，就归为未知。可在 `config/synonyms.json` 里补同义词提高命中率。

**Q：想合并两个艺人 / 两个标签？**
A：写进 `config/synonyms.json`，然后 `python run.py reindex --full`。

**Q：XLSX 没生成？**
A：需要 openpyxl：`pip install openpyxl`。不影响其他格式。

---

## 十四、免责声明

1. **娱乐向 / 个人使用**：本软件仅供个人分析**自己合法收藏**的影视元数据，用于娱乐向的偏好统计与自我了解。
2. **离线只读**：软件仅读取本地已有的 `.nfo` 文本元数据，不联网、不下载、不修改原始文件、不抓取任何第三方内容。
3. **数据归属**：请仅对你**拥有合法权利**管理的文件运行本工具；由此产生的数据由使用者自行负责。
4. **合规**：使用者须遵守所在地区法律法规与平台条款。因不当使用导致的任何后果由使用者自行承担，作者与贡献者不承担责任。
5. **无担保**：本软件按“现状”提供，不保证适用性或无缺陷。

---

## 十五、开源协议

本项目以 [MIT 协议](./LICENSE) 发布。

---

## 十六、版权与使用限制

- **Copyright © 2026 肆月Aperture**
- 本软件不得用于商业用途，仅做学习交流使用。
