# doujindigdb (dddb)

**同人音乐展会专辑检索 → 网易云匹配 → 标记管理**

从同人音乐数据库与展会专辑列表里自动发现专辑，跨源合并曲目表、封面、社团与艺人信息，
按 `M3` / `Comiket` 等展会分类生成报告，在网易云做**只读**检索匹配，
并用自定义标记（如「已加入歌单」）管理自己的进度。

> 纯 Python 标准库实现，**零第三方依赖**（连 AES/RSA 加密、HTML DOM、CSS 选择器、二维码生成都是自带的）。

---

## 当前范围（已按反馈瘦身）

**已移除**：

- ❌ **网易云写操作** —— 收藏专辑、新建歌单、批量加入曲目、红心，以及只为它们服务的扫码/密码登录。
  现在只剩**检索 + 匹配打分**：结果写进本地库、在面板里查看，
  **报告里不显示匹配**（匹配只当内部信号用，不占报告版面），加歌单由你自己在 App 里完成。
- ❌ **X (Twitter) 数据源** —— 四个后端全部删除。实测原因：X API 额度用尽（HTTP 402 credits depleted）、
  Nitter 实例全挂（410/451/403/SSL EOF）、匿名 guest 的 GraphQL query id 已失效且代理出口 IP 被限流（429）。
- ❌ **VK / Discogs / TouhouDB / THBWiki** 数据源。

**保留的数据源（默认启用 3 个 + 2 个可选）**：

| 源 | 定位 | trust | 默认启用 |
|---|---|---|---|
| `dojinmusic` | **一手来源**：展会社团名单 + CD 详情 + 各社团特设页试听 | **100** | ✅ |
| `bangumi` | bgm.tv 同人音乐小组的「信息收集贴」贴主收藏（带展会标签） | 80 | ✅ |
| `doujinstyle` | **次要来源**：第三方转载(下载)站，按「缩写+序号」标签索引 | **30** | ✅（仅作旁证） |
| `vgmdb` | VGMdb | 80 | ❌ 当前网络 Cloudflare 403 |
| `dizzylab` | 中国同人专辑平台 | 60 | ❌ 默认不参与展会采集 |

### 来源可信度分级

`Album.trust` 记录这条数据来自多权威的来源（跨源合并取最大值）：

* **100 · 一手来源** —— 社团自己在 dojin-music 登记的信息（含展位号、发售日、曲目、艺人）
* **80 · 权威站点** —— bgm.tv / VGMdb 这类结构化数据库
* **30 · 次要来源** —— DoujinStyle。它是**第三方分享站，不是官方数据库**：
  专辑只在展会结束后陆续上传（除非社团提前上线 Spotify / Bandcamp），
  而且**没有曲目表**、`Date Added` 是**上传日期而不是发售日**。
  所以它只用来回答「这张碟已经流出了吗 / 有可能上流媒体吗」，
  **绝不会**用它的日期去覆盖一手来源（这正是之前那次数据污染的成因）。

Web 面板的专辑卡片上**直接显示具体的数据源名字**（`dojin-music` / `bgm.tv` / `DoujinStyle`），
而不是「一手来源 / 权威站点 / 次要来源」这种抽象分级 —— 一眼就能看出这条记录是哪儿来的。
一张专辑被多个源确认时（例如 bgm.tv + DoujinStyle）会并列显示多个来源标签；
可信度数值保留在标签的悬停提示里。

### 合并：一手来源说了算

`Album.merge()` **按来源可信度决定字段归属**，而不是「先到先得」：

* 后到的记录**可信度更高** → 它的标题 / 社团 / 发售日 / 封面 / 曲目表**直接覆盖**
  已有的值。所以哪怕第三方转载站先入库、写了一个错的发售日，一手来源一到就会纠正它。
* 后到的记录可信度更低或相同 → 只补空缺，不覆盖。
* 集合类字段（艺人 / 标签 / 链接 / 来源 ID / credits）始终取并集；
  `trust` 取最大，`trust_by_source` 逐源取最大，因此 `primary_source`
  能回答「这条记录主要听谁的」。

去重的严格程度可配（`[sources] dedupe_mode`）：

| 取值 | 行为 |
|---|---|
| `balanced`（默认） | 标题相似度 ≥ `threshold`（0.94）且社团归属一致就合并。社团允许括号补充（`nitro hyperspace (nitro)`）与词子集（`Winter Bouquet` ⊂ `柊さんた・雛森アンシア(Winter Bouquet)`） |
| `strict` | 社团规范化后必须**逐字相等**，标题也要几乎一致 —— 宁可留重复，也不要并错 |
| `off` | 除站点原生 ID 外一律不合并 |

判重还额外要求**标题里的数字一致**：`TTL EUROBEAT VOL.19` 与 `VOL.25`、
`Melodious Melodies 4` 与 `5` 的字符串相似度都超过阈值，但它们是不同的碟，
数字不同就不合并。

> 之前的实现是「没有社团就认领第一条同名记录」，会把 `ReMind` 这类常见同名碟
> 错误地并成一条。现在这条路径被封掉了，并写了回归测试。
>
> 再之前 `find_similar_album` 的 `threshold` 参数虽然写在签名里却从未使用 ——
> 实际只做「标题规范化后完全相等 + 社团完全相等」，于是同一张碟在 dojin-music 与
> doujinstyle 上的写法只要差一段括号，每采集一轮就会多出一条。现在标题走相似度、
> 社团走"去括号 + 词子集"判定。

历史遗留的重复用 `dnh db dedupe` 清理：默认只演练，`--apply` 才真合并；每组保留
可信度最高、字段最全的那条，来源 / 标记 / 曲目表会一并迁移过去。

**新增能力**：

- ✅ **发行日 / 展会一致性校验** —— 只有发行日落在该展会窗口内的专辑才会被打上展会标签
- ✅ **来源可信度分级**（见上）与展位号
- ✅ **标记系统** —— 任意标记（如「已加入歌单」「已购买」），可按标记筛选 / 排除
- ✅ **网易云歌单对照** —— 粘贴歌单链接，反查「哪些还没采集 / 哪些还没进歌单」，
  并可一键把命中的专辑打上标记
- ✅ **试听 & 链接页** —— 汇总从 dojin-music 特设页抓到的 YouTube / SoundCloud /
  niconico / 推文 / 特设页链接
- ✅ **Web 面板增删改** —— 手动新增专辑、**编辑已入库专辑的全部元数据（含链接 & 试听）**、
  **上传 / 删除封面（单张 / 批量）**、多选删除、按筛选条件批量删除、清理日期不符
- ✅ **封面质量门** —— 按比例/尺寸判定是否真是封面（正方形 / AD:TRANCE 型书形），
  并限制封面来源（排除 dojin-music 的站内 jacket）
- ✅ **重复专辑清理**（`dnh db dedupe`，默认演练）
- ✅ **任务可停止 + 详细日志**

---

## 快速开始

### 0. 先看效果（不需要网络）

```bash
python scripts/demo_report.py     # 然后打开 reports/demo-report.html
```

### 1. 环境

只需要 **Python 3.11+**。无第三方包。

```bash
python -m dnh --version
python -m dnh init          # 生成 config/config.toml 与目录
```

### 2. 配置代理

```toml
[proxy]
enabled = true
url = "http://127.0.0.1:7897"
mode = "hosts"     # 只代理 hosts 列表里的站点；网易云自动直连
```

### 3. 自检

```bash
python -m dnh doctor
```

逐项检查 Python 版本、目录可写、代理连通性、5 个数据源是否可用、网易云接口是否连通。

### 4. 跑起来

```bash
python -m dnh events list                       # 看最近有哪些展会
python -m dnh discover --event M3-2026秋        # 采集专辑线索
python -m dnh enrich --event M3-2026秋          # 用其它站点补齐曲目表 / 封面
python -m dnh match --event M3-2026秋           # 网易云检索匹配
python -m dnh report --event M3-2026秋 --open   # 生成报告
```

一条命令跑完：`python -m dnh run --event M3-2026秋`

### 5. 打开面板

```bash
python -m dnh serve --open     # http://127.0.0.1:8787
python -m dnh public --port 8940
```

---

## 命令参考

```
dnh init | doctor | events | sources | discover | enrich | match | run | playlist | report | db | cache | robots | public | serve

events list [--year 2026]              events tags <M3-2026秋> [--format text|hash|json]
sources list
discover --event E [--sources a,b] [--limit 80] [--query Q] [--covers]
enrich   [--event E] [--limit 150] [--sources a,b] [--no-covers]
match    [--event E] [--tag T] [--limit 300] [--min-score X] [--force] [--show 30]
run      --event E [--no-enrich] [--no-match] [--no-covers]
playlist compare --url <网易云歌单链接> [--event E] [--mark 已加入歌单] [--show 25]
report   [--event E] [--group-by event|tag|circle|kind|source] [--format md,html,json] [--open]
robots   [<url>] [--audit] [--refresh] [--offline]     # 抓取合规检查(只读, 不发采集请求)
public   [--host 127.0.0.1] [--port 8940] [--base-url URL] [--sessions-db PATH]
serve    [--host 127.0.0.1] [--port 8787] [--open]

db stats
db backup   [--out PATH] [--force]        # sqlite3 online backup(WAL 下安全), 打印大小/耗时
db albums   [--event E] [--tag T] [--query Q] [--limit 50]
db prune    [--drop] [--dry-run] [--before-days N] [--after-days N]
db delete   --key K | --event E | --tag T | --source S | --query Q | --mark M | --unmarked | --unmatched
            [--dry-run] [--yes]
db mark     <key...> [--mark 已加入歌单] [--note ...] [--remove] [--list]
db dedupe   [--threshold 0.94] [--limit 200] [--apply]   # 默认只演练
db reset    --yes [--keep-matches]
```

所有命令都支持 `--json`。

---

## 网易云歌单对照（只读）

你在网易云里手动维护一个 M3 / Comiket 歌单，想反过来知道两件事：

```bash
dnh playlist compare --url "https://music.163.com/#/playlist?id=123456" --event M3-2026秋
```

面板「歌单对照」页把结果做成**两段可折叠**的列表，并按"你当下该做什么"排优先级：

| 段 | 含义 | 默认 |
|---|---|---|
| **① 本地有、歌单里还没加** | 你现在就该去 App 里加的专辑 | **展开** |
| **② 歌单里有、本地库没有** | 还需要补充采集的曲目 | 收起 |

两段都用 `<details>` 做成**可折叠**，标题带计数与一行说明。

**① 用网格排（一行多张小卡片）**，不是一行一张 —— 查漏补缺要反复上下滚动太难受。
每张卡片有封面、标题、社团/日期，以及两个按钮：

* **标记为已添加** —— 直接调打标记接口，不用重新对照整个歌单；打过的显示 ✓ 角标与「已标记」
* **详情** —— 打开和专辑库同一个详情抽屉

**封面**：① 用本地已下载的封面（经 `/api/cover` 代理 + 缓存）；
② 借用网易云返回的专辑图（`al.picUrl`，解析进 `NeteaseSong.album_cover`），
并给每条加「专辑页 ↗」直达链接。

顺手把命中的专辑标记掉，之后在面板里按标记过滤：

```bash
dnh playlist compare --url <链接> --mark 已加入歌单
```

匹配优先级：专辑名（规范化）→ 专辑名（宽松，忽略括号后缀）→ 专辑名+社团 → 曲目名。
链接支持 `music.163.com/#/playlist?id=`、`music.163.com/playlist?id=`、
`y.music.163.com/m/playlist?id=`、`/playlist/<id>` 与裸 id。

> 只调 `/v6/playlist/detail` 与 `/v3/song/detail`，**不修改你的账号**。
> 歌单超过 1000 首时会自动用 `trackIds` 分批补齐。

---

## 发行日与展会一致性（本轮重点修正）

之前的实现把 `list_by_event()` 返回的**每一条**都无条件打上该展会的标签。而某些适配器在标签检索
无结果时会退回泛检索，结果实测 **M3-2026秋 里 86% 的专辑其实是 2011~2025 年已经发售的老碟**。

现在的规则：

* 专辑的 `release_date` **明确落在窗口外** → 丢弃（计入 `RunStats.albums_rejected`，日志会打印前 20 条）
* 落在窗口内，或**没有发行日** → 保留（展会专用列表页/X 动态常常没有单曲发行日，此时展会本身就是日期来源）

窗口默认 **展会前 45 天 ~ 展会后 21 天**（`M3-2026秋` = 2026-10-25 → 2026-09-10 ~ 2026-11-15），可配：

```toml
[events]
release_window_before_days = 45
release_window_after_days = 21
reject_mismatched_release = true
```

历史数据用 `dnh db prune` 修（默认**只摘展会标签**，不删专辑；加 `--drop` 才删）。

---

## 标记与筛选

标记用来记录你自己的进度，例如：

```bash
dnh db mark <key> --mark 已加入歌单          # 或 --mark 已购买 / --mark 待确认
dnh db mark --list                           # 看各标记有多少张
dnh db mark <key> --mark 已加入歌单 --remove # 取消
```

筛选：

```bash
dnh db albums --mark 已加入歌单              # 只看某个标记
dnh db delete --unmarked --yes               # 删掉所有没有标记的
```

Web 面板「专辑库」里可以：

* 勾选多张 → **打标记 / 移除标记 / 删除选中项**
* 顶部下拉 **按标记筛选**，或勾选 **只看未标记 / 只看未匹配**（这正是"手动加完歌单后过滤掉已处理专辑"的场景）
* **＋ 手动新增专辑**（标题、社团、艺人、发行日、展会、封面、标签、曲目、初始标记）
* **✎ 编辑信息** —— 详情抽屉里可直接改任一已入库专辑的标题/社团/艺人/发售日/展会/
  编号/展位/价格/标签/曲风/简介/封面地址/曲目表；**封面（上传 / 删除）也在这个面板里**，
  **链接 &amp; 试听**可逐条增删改（站点条目、通贩页、YouTube / SoundCloud / B 站…）。
  自动采集是"合并"语义（空值不覆盖），人工纠错是"写什么就是什么"。
* **删除封面** —— 判定某张图不是专辑封面时：单张在「编辑信息」里点「删除封面」，
  多张勾选后点工具栏的「删除封面」。会把 `cover_url` 与 `cover_local` 一起清掉，
  并删除 `data/covers/` 下的本地图片（**还有别的专辑引用同一张图时保留文件**；
  `data/covers/` 之外的路径一律不碰）。
  注意：只清空「封面地址」不会删图 —— 封面是靠 `cover_local` 显示的。
* **筛选条件**：搜索 / 展会 / 来源 / 标签 / 标记 / **封面（有 / 无）** / 只看未标记 /
  只看未匹配 —— 「无封面」用来集中做数据检查。
* **清理日期不符** 一键处理历史脏数据

> 阈值说明：`unmarked` 指「没有任何标记」，`unmatched` 指「没有网易云匹配」。

---

## 任务日志、进度与停止

Web 面板「任务」页：

* **实时进度计数**：任务头部显示当前阶段（如 `dojinmusic @ M3-2026春`）与
  `已处理 / 新增 / 更新 / 无变化 / 剔除 / 跳过已入库` 六个计数，2.5 秒刷新
* 每个任务显示**分步日志**（采集了哪个源、命中了什么、匹配到哪一条）
* 运行中的任务有 **停止** 按钮 —— 点击后任务在下一个检查点退出，状态变成 `cancelled`，
  已经写进库的数据保留（不会回滚，也不会留下半截记录）

### 为什么需要进度计数

全量采集一个展会是**几十分钟**量级（M3-2026春 上千张 CD，每次详情请求约 2~5 秒）。
只给文字日志的话用户完全看不出"到底在动还是卡死了"——这正是我实测踩到的：

> 旧实现是"把整个展会收齐 → 跨源合并 → 才写库"。进程跑了 25 分钟、
> HTTP 缓存涨到 546 MB，而**数据库一条都没动**，中途打断全部白跑。

现在 `discover` **边收边合并边落库**（生成器 + 增量合并，阈值与 `merge_album_list`
一致），所以每张专辑取到就可见；配合源侧的 `skip_known`（默认开）跳过已入库的 CD，
**中断后重跑即续跑**。

日志节流：每 25 张才打一行进度，`无变化` / `剔除` 只计数不写行
（一次采集可能上千条，逐条打会把面板刷爆）。

### 适配器怎么把内部计数报给面板

「跳过多少张已入库」这种数**只有适配器自己知道**（跳过发生在抓详情页**之前**，
Pipeline 看不到）。所以 `SourceContext` 上留了一个可选的上报通道：

```python
# 适配器里
self.ctx.report("albums_skipped", source=self.name, event=event.name, count=skipped)
```

`Pipeline` 在 `make_context()` 时把 `self._emit` 接进去，于是这条上报会走
`pipe.on_event` → 面板任务计数。**没有消费者时（CLI 直接跑）静默忽略**，
上报本身抛异常也会被吞掉 —— 进度上报绝不能影响采集。

---

## 数据源说明

| 源 | 接入方式 | 需要代理 |
|---|---|---|
| `dojinmusic` | `/eventV1` 展会列表 → `/event/<id>/audition` 拿 CD id → `/cd/<id>` 内嵌 JSON | 是 |
| `bangumi` | 小组「信息收集贴」→ 贴主 → `/v0/users/<u>/collections` 按展会标签本地过滤 | 是 |
| `doujinstyle` | `?p=search&type=tags&result=M3-58` 网格 → `?p=page&type=1&id=N` 详情页 | 是 |
| `vgmdb` | HTML（`#albumtitle` / `#albuminfos` / `#tracklist`）—— **未能联网验证** | 是 |
| `dizzylab` | 站内 JSON 接口 + HTML 兜底 | 是 |

**展会标签方案**：展会标签是「缩写 + 序号」，序号随每届递增，
由 `dnh.events.edition_label()` 统一推算而不是写死：M3 以 `M3-2023春 = M3-51`
为基准每年春 +2、秋 +1（所以 `M3-2026秋 = M3-58`），Comiket 直接用 `C107` / `C108`。
**DoujinStyle 的标签与 bgm.tv 收藏条目的 tag 用的是同一套编号**，所以两处共用这一个函数。

### bgm.tv 的取数路径（实测结论）

小组 `bgm.tv/group/doujinmusic` 里每个展会都有一个「信息收集贴」
（如 `M3秋2026 信息收集贴`）。适配器的做法是：

1. 从小组话题列表里按标题找到该展会的话题 → 取**贴主**的用户名
2. 拉 `https://api.bgm.tv/v0/users/<user>/collections?subject_type=3`（公开收藏）
3. **在本地**按展会标签（`M3-58`）过滤
4. 需要艺术家/发售日/编号时再打 `/v0/subjects/<id>` 读 `infobox`

两个实测坑（都有测试钉住）：

* 接口的 **`tag=` 查询参数会被静默忽略**（传不传返回完全一样的数据），必须自己筛；
  过滤要**精确匹配**，否则 `M3-5` 会误命中 `M3-58`。
* 帖子里挂的 `/index/<id>` 目录（如 `M3-58 Collection`）**对匿名访客不可读**，
  返回「数据库中没有查询到该目录的信息」，所以不走目录，改走贴主的公开收藏。
  收藏按更新时间倒序，新展会的条目集中在最前面 —— 连续两页没命中就提前收工。

`python -m dnh sources list` 可看每个源当前的可用状态与原因。

---

## 富化与「dojin-music 回填」

`dnh enrich` 的顺序**很关键**（这里踩过坑）：

**第 1 步 · 按原生 ID 回自己的源要详情**
一次 1~2 个请求，拿到的就是这条记录的权威字段。

对 **bgm.tv** 来说这一步还负责补两样关键数据：

* **曲目表** —— 音乐条目的曲目表**不在** `/v0/episodes`（那里是空的），
  只在条目页 HTML 的 `ul.line_list_music` 里：

  ```html
  <li class="cat">Disc 1</li>
  <li class="line_odd"><h6><a href="/ep/1673021">1 Gust</a></h6></li>
  ```

  这就是为什么之前库里 **117 张 M3-2026春 专辑全都没有曲目表** ——
  而曲目表是网易云匹配最强的信号。
* **信息框**（`#infobox`）的 **艺术家 / 厂牌 / 发售日期 / 链接 / 官方网站** →
  credits + 链接。链接按 **URL 主机**归类而不是信标签名
  （实测「链接」那一项贴的是 YouTube 地址）。
  另外：bgm 的「艺术家」在同人音乐里就是社团，`circle` 为空时会用它补上 ——
  没有 `circle` 的话，网易云匹配的艺人维度直接失效、dojin-music 的定位也无从下手。

**第 2 步 · dojin-music 回填**（`link_dojinmusic`，默认开）

从 DoujinStyle / bgm.tv 抓到的专辑**不会**自动带 dojin-music 的特设页、X 原帖、
试听、展位号、展示场与权威发售日。这一步把它们补上：

* 用「**社团名 + 标题**」反查定位到 CD（每个社团 2 次请求，按社团/社团名缓存）
* 合并进来时 dojin-music 的 `trust=100` 自然覆盖次级来源的字段

> **为什么不做全量枚举**：M3-2026春 有 **1742 个展位**、**1161 张 CD**，
> 全量枚举要几千次请求。而"把 dojin.info 的内容补进我已经有的专辑"只需要按社团反查
> —— 每个社团 2 次。全量枚举只在 `discover` 里做。

> **顺序坑**：第 2 步必须在第 1 步**之后**。因为 `circle` 是第 1 步才补出来的，
> 放在前面会因为"没有社团名"而一张都定位不到（实测第一版就是这样，回填 0 张）。

**第 3 步 · 跨源关键词搜索**（`enrich_cross_search`，默认**关**）
既慢又容易错（DoujinStyle 的 `search` 会为每个命中再进详情页），日常用前两步就够。

### dojin-music 的完整抓取路径

这里花了很久才摸清 —— 旧代码只读了一个**试听页**，所以 dojin-music 几乎什么都没贡献。

| 端点 | 作用 |
|---|---|
| `/eventV1` | 展会列表（`M3-2026春` = id 32） |
| `/eventV1/<id>` | Inertia `data-page` props → `event{id,name,date}`、**`headList` 展示场结构**、`counts.cdCount` |
| `/api/event/<id>` | **展位表**：`{"A": {"01a": "社团名"}}` → `A01a → MONOCULAR FOREST`（M3-2026春 共 **1742** 条） |
| `/api/search/circle?word=<社团名>` | 社团名 → `circle_id` |
| `/api/circle/<circle_id>` | 该社团**全部** CD（`cd.years.*`，含 `cd_id` / `day_sale`） |
| `/cd/<id>` | CD 详情页（同样是 Inertia props） |

`/cd/<id>` 的 props 里有：

* `eventSpace` → `space_hall`（**展示场**）/ `space_head`（区域）/ `space_number`（编号）
* `songList` → 曲目 + **逐曲 credits**（`vocal` / `word` / `music` / `arrange` / `mix` / `remix`）
* `circle` → 社团主页 / 推特 / YouTube / 通贩

M3-2026春 的真实结构（你指出的第一/第二展示场 + A–V 分区，完全对上）：

```
hall 'カタログ':     1 个区域 ['1']
hall '第一展示場':  22 个区域 ['A','B',...,'V']
hall '第二展示場①': 6 个区域
hall '第二展示場②': 6 个区域
```

**三个坑**（都写进代码注释了）：

1. `/event/<id>/audition` 是**试听页**，只暴露 1~2 个 `/cd/` 链接，
   而该展会实际有 **1161** 张 CD —— 拿它当 CD 列表就会漏掉 99%。
2. `/cd/audition?event_id=<id>` 看着像按展会，其实是**全站**试听列表
   （11858 条，首条的 SoundCloud URL 写着 `2026m3_autumn`），`space` 全为空。
3. `/api/search/circle` 的参数名是 **`word`**，用 `q` 会**静默返回空列表**（不报错），
   极易误判成「站上没有这个社团」。

**为什么回填不做全量枚举**：1742 个展位 × 2 + 1161 张 CD 详情 ≈ 几千次请求。
回填只需要按社团反查（每个社团 2 次），全量枚举留给 `discover`。
`event_meta()["cd_count"]` 会作为**官方对照值**打日志，用来看抓全了没有。

### 枚举分两档，默认按展会规模**自动选**

```toml
[sources.per_source.dojinmusic]
delay = 0.4                  # CD 详情页只有 ~13KB
api_delay = 0.2              # /api/* 独立节流(都是轻量 JSON)
enumeration = "auto"         # auto | fast | full
api_to_detail_ratio = 0.61   # 选档用: 一次社团接口 ≈ 0.61 次详情页抓取(实测)
skip_known = true            # 续跑: 已入库的 CD 不再抓详情页
```

| 档位 | 做法 | 实测（M3-2026春） |
|---|---|---|
| **快档** | 抓一次全站试听列表（1 个请求），用展位表的**社团名求交集**拿候选 | 枚举 **5.1 秒**，但 2376 个候选 |
| **全档** | 逐个社团问 `/api/search/circle` → `/api/circle/<id>`，用 `day_sale` 精确筛 | 枚举约 52 分钟，但候选精确 |

**为什么不能固定用快档**：试听列表只按**社团名**索引，同一个社团在**其他展会**的碟
也会被带进来。M3-2026春 快档给出 2376 个候选，而官方只有 1161 张 —— 多出来的 1215 张
每张都要抓一次详情页才被发现日期不符而剔除。**快档省了枚举，却在详情页上亏回去。**

**为什么也不能固定用全档**：`/api/event/<id>` 是要一个个社团去问的。M3-2026秋 目前只有
个位数 CD，全档却要白问 1700 多个社团。

所以 `enumeration = "auto"` 用成本模型选（`choose_enumeration`，纯函数、可测）：

```
快档成本 ≈ 候选数                    （每张都要抓详情）
全档成本 ≈ CD 数 + 社团数 × 0.61      （社团接口折算成详情页当量）
```

**单位成本是实测的，而且不能吃缓存**（这点我一开始量错过）：

| | 成本 | 说明 |
|---|---|---|
| CD 详情页 | **4.69s** | 中位值, 6 张真实页面, 强制 `use_cache=False` |
| 每个社团(2 次接口) | **2.84s** | 12 个社团, 同样不吃缓存 |
| 折算比例 | **0.61** | = 2.84 / 4.69 |

> 最早的 0.25 是照"`/api/*` 节流 0.2s"估的，**把接口当成几乎免费**，结果严重高估快档。
> 更早还量到过 0.1s/页 —— 那是**缓存命中**，算出比例 40 这种荒谬值。
> 真实接口延迟是秒级，跟详情页同一量级。

实测决策：

| 展会 | cdCount | 社团 | 快档候选 | 选择 |
|---|---|---|---|---|
| M3-2026春 | 1161 | 1742 | 2376 | **全档**（多收 1215 张 > 1063 张当量） |
| M3-2026秋 | 377 | 0 | 0 | **快档**（展位表为空，全档无人可问） |

### 枚举的三级兜底

展会生命周期里三情况都会遇到，所以任一档收不到东西就往下退：

1. **选中的那一档** —— 正常路径
2. **换另一档** —— 例如全档没收到时退回快档候选
3. **试听页的 `/cd/N` 链接**（`parse_event_cd_ids`）—— 最后兜底

第 3 级很关键：**展会还没到时站点尚未分配展位**，`/api/event/<id>` 是空的
（实测 M3-2026秋：**0 个展位**，官方却已标了 377 张 CD），这时只有试听页上挂着的
那几张能拿到。旧实现把它当主路径（所以 M3-2026春 只出 1 张 / 1161），
现在它只当兜底。

> 快档**只覆盖传了试听的碟**。要连没传试听的一起补齐，用 `enumeration = "full"`。

### 封面也缺？同样去特设页找

dojin-music 的 `jacket` 更缺 —— M3-2026春 1085 张里**只有 60 张有**。特设页几乎一定放了
社团自己的封面，所以也去那儿抓：

```toml
[sources.per_source.dojinmusic]
cover_from_special = true    # 缺封面时从特设页抓
```

优先级：`og:image` → 名字/alt/class 带 `cover`/`jacket`/`ジャケット` 的 `<img>` →
声明尺寸足够大的图。12 张真实特设页上补到 **8 张**。

**这里的坑特别多**，而且都是实测踩出来的：

| 现象 | 原因 | 处理 |
|---|---|---|
| 抓到 `/themes/<主题>/images/ogp.jpg` | og:image 是**站点**通用图 | 排除表挡住 |
| 抓到 `/img/tw.png`、`soundcloud.png`、`applemusic.png` | 特设页上的平台/分享图标 | `deny_host`(站内图) + 平台图标名 |
| 相对路径拼到了错误的站 | `base` 传成了 dojin-music | **改用特设页自己的 URL** 解析 |

> 补名单是个**无底洞** —— 加了 soundcloud 又冒出 applemusic。所以最后改成了
> **没依据就不猜**：只有带封面提示、或声明了足够大尺寸的图才认，否则返回空。
> 宁可漏，不可错。8/12 里没有一个是图标，而 4 张找不到的**正确地返回了空**。

**已知残留**：同一位艺人的两张碟可能拿到**同一张** SoundCloud 作品图(艺人主页图)，
这需要跨专辑去重才能发现，目前没做。

### 全量富化会踩的两个「静默截断」

`dnh enrich --event ... --limit 1200` 跑 1085 张，结果只更新了 **12 张**。不是没数据，
是**两道预算把活儿悄悄截断了**，而且两处都不报错：

| 配置 | 默认 | 症状 | 全量时该设成 |
|---|---|---|---|
| `[sources] enrich_time_budget` | `300`（5 分钟） | 到点就停，剩下的全跳过 | `0`（不设上限） |
| `[sources.per_source.dojinmusic] media_enrich_max` | `40` | 只有前 40 张会进特设页 | `1200` |

第一个尤其阴险：日志只写 `富化完成: 更新 12 / 1085 张`，**看起来像是"没什么可补的"**，
得去翻 `enrich_time_budget` 才知道是被 5 分钟截断了。

> 教训：这类"全局预算"开关出问题时表现为**静默少做**，不表现为报错。
> 跑全量前先把它们确认一遍。

另外第二次跑虽然不再截断，但**很慢**：缺封面的专辑每张都要现抓一次特设页
（不吃缓存，约 3 秒），实测约 **1 张/分钟**。1085 张跑完是小时级，建议分次。

### bgm.tv 外链同步的一个隐藏 bug（已修）

现象：bgm 上明明标了「官方网站 / 特设页面」和试听链接，库里却什么都没有。

原因在 `fetch_album` 的这一行：

```python
if not album.tracks and self.opt("fetch_tracks_html", True):
    self._fill_tracks_from_html(album, sid)     # 信息框链接也在这里面
```

**条目页 HTML 只在「缺曲目表」时才抓**，而信息框里的官方链接也只在那个 HTML 里。
于是「曲目表已经从 API 拿到了」的专辑**永远不会去抓 HTML → 永远补不到链接**。

修法：缺曲目**或**缺 `special` 都去抓一次。

```python
need_tracks = not album.tracks and self.opt("fetch_tracks_html", True)
need_links = ("special" not in (album.urls or {})
              and self.opt("fetch_links_html", True))
if need_tracks or need_links:
    self._fill_tracks_from_html(album, sid)
```

> 中间还踩了个小坑：第一版判据写成「完全没有外链」，但大多数专辑早有 `youtube` 了，
> 条件永远不成立。必须**只盯 `special` 这个字段**。
>
> 附带修了一处：新加的 HTML 请求原本带着 `retries=2` 绕过了检索预算，
> 被 `test_search_budget_disables_retries_when_active` 抓到 —— 现在预算生效时会强制 `retries=0`。

### B 站搬运的试听视频

很多同人碟会被社团或爱好者搬到 B 站，标题常带「試聴 / クロスフェード / XFD」。
把这类视频挂到 `urls["bilibili"]` 上，报告和面板都能直接点开。

```bash
python -m dnh bilibili --event M3-2026春 --budget 15      # 单独补
python -m dnh bilibili --event M3-2026春 --budget 10 --dry-run   # 只看不写
python -m dnh enrich --event M3-2026春 --bilibili         # 采集时顺带补
```

接口实测可用（不需要登录）：

```
GET https://api.bilibili.com/x/web-interface/search/type?search_type=video&keyword=...
-> {"code":0,"data":{"result":[...]}}
```

但**有风控**，连发十来个请求就开始返回非 2xx。所以：

```toml
bilibili_audition = false   # 默认关
bilibili_budget = 40        # 每轮最多查几张
bilibili_delay = 2.0        # 每次请求间隔(秒)
```

另外**连续失败 3 次就熔断整轮** —— 继续打只会延长封禁。已补的会保留，稍后重跑会跳过它们。

#### 误配是最需要防的事（踩过一次）

第一次跑出来 3 条，其中一条是：

```
夢見心地シミュレータ  ->  【3DS游戏大赏】《模拟农场 14》日版PV…ファーミングシミュレーター…
```

**完全无关的农场游戏 PV。** 根因在 `textutil.title_variants`：它给这个标题生成出
`shimyureeta` —— **汉字部分在转 ASCII 时被丢掉了，只剩片假名尾巴**，而它正好是
`faamingushimyureetaa` 的子串。

所以 `dnh/bilibili.py` 不用那套变体，改成自己的保守规则：

* 原文（折叠全角/大小写/空白）作变体，另加一个去标点版本
* **只有标题完全不含汉字时**才追加罗马字变体 —— 有汉字时罗马字必然丢字
* 匹配改成「折叠后的原文包含」，不再用 `norm_key(romaji=True)` 双向归一

修完后同一个样本：农场视频被拒，两条正确的（`【M3-2026春】.banbanshi「Welcome Spring! another」【XFD】`）
保留下来。误配那条已经从库里清掉了。

### 链接来自三处合并

「试听 & 链接」不是只看 dojin-music。三家各有各的字段：

| 源 | 位置 | 收什么 |
|---|---|---|
| dojin-music | CD 记录的 `link_url` / 各 shop 字段 | 特设页（仅带「新譜情報」的条目）、X、试听、通贩 |
| **bgm.tv** | 信息框里的绝对链接 | 试听（YouTube/SoundCloud/…）+ **按字段名**认「官方网站」→ `special` |
| **VGMdb** | **Websites** / **Available at** 两个卡片 | `Official`/`Website` → `special`，`Store`/`Shop`/`Buy` → `booth`，`Review` 也保留 |

VGMdb 的外链都包在 `/redirect/<id>/<真实地址>` 里，必须解开才能用（`_unwrap_redirect`）。

> 注意面板上**「特设页」和「社团主页」是两个不同的字段**：
> `special` 是社团为**这张碟**单独做的页面，`circle_site` 是社团自己的链接聚合页
> （lit.link / linktr.ee / YouTube 频道之类）。旧数据只有 `special` 且全是社团主页，
> 已整体迁移到 `circle_site`（见下）。

### 重抓后的真特设页：0 → 86

迁移把错的 `special` 清空之后，重跑了一次 enrich（让 CD 级 `link_url` 生效）：

```
special 0 -> 99 -> 86（重新归类后）
富化完成: 更新 103 / 1085 张
```

**为什么中间是 99 后来又变 86**：CD 的 `link_url` 也不一定真是特设页 —— 实测有指向
**X 原帖**（7 条）、**YouTube**（4 条）的。所以加了 `classify_link_url()` 按主机归类，
`x.com` → `x`、`youtube.com` → `youtube`，剩下认不出来的才当特设页。重归类掉 13 条。

归类后 `special` 的主机分布很干净，全是社团/专辑自己的站点：

```
2  asterism-music.net     2  espitz.com
2  xxx.wixsite.com        1  xxx.netlify.app
1  aerowife.web.fc2.com   1  nakamurameiko.net  ...
```

> 结论：**1085 张里只有 86 张（8%）真的有特设页** —— 跟你说的「只有带新譜情報的条目
> 才可能有」一致。这也说明指望特设页补封面/曲目表是不现实的。

### 数据迁移：`special` → `circle_site`（已执行）

```
迁移前: special 786 条 / circle_site 0 条
迁移后: special 0 条  / circle_site 786 条
```

迁移后重新跑了封面清理，又清掉 2 张（`Share Image` / `artist_photo` 这类社团站上的非封面图）。

**还有 111 张封面与社团主页同域，但绝大多数是对的** —— 例如
`ARIS-0005/artwork.jpg`（目录号 + artwork）、`ellimia.net/images/exonorm-jacket.webp`
（文件名含专辑名 + jacket）。所以**没有按"同域"一律清除**，只清文件名明显是通用图的。

### 导出可导入的歌单清单（合作歌单只能手机端加曲）

**场景**：目标是两个网易云**合作歌单**，但桌面端与 Web 端都没有"往合作歌单加歌"的入口，
只能手机端一首首加。所以把对照结果导出成清单，手机端照着一遍加。

#### 网易云的批量导入到底是什么（用户澄清后修正）

它不是"接一个 URL"的接口，而是三种能力：

1. **导入其它 App 的已有歌单**（靠通用歌单标准 / 平台适配）
2. **文字识别** —— 认一段文本里出现的**歌名 + 歌手**，逐首匹配
3. 图像识别（同上，走 OCR）

手机端另有「本地导入」，但它只吃手机本地的**音频文件**，跟链接无关。

所以对我们有意义的是 **② 文字识别** —— 它要的不是 songId，而是能被认出来的
「歌名 - 歌手」。导出因此以**纯文本**为主：

```
A1 - CircleA
A2 - CircleA
B1 - CircleB
```

一行一首、不带编号、不带 songId、不带表格 —— 这些都是干扰项。
另外提供「带上专辑名」变体（`歌名 - 歌手 - 专辑`），识别不出时可换这份试。

> songId 列表仍然保留，但只用于**自己核对**或喂给第三方工具，不是给识别器看的。

#### 不存在"一串 songId → 可导入链接"

查证结论（[网易云导入外部歌单的机制](https://jingyan.baidu.com/article/a3aad71a1d3db3f0fb00968c.html)）：

> 网易云「导入外部歌单」**只认它支持的第三方平台歌单链接**（QQ音乐 / 酷狗 / 酷我 / 咪咕）
> 或本域已有歌单链接。**没有**"任意歌曲 ID 列表 → 可导入链接"的通用格式；
> 而创建歌单需要登录态 + 写接口（本项目已移除）。

所以不编造假链接，改为导出它**真正能消费**的三样东西：

| 输出 | 用途 |
|---|---|
| `ids` | 纯 songId，一行一个 —— 很多第三方工具直接吃 |
| `text` | `songId<TAB>歌名 - 歌手`，方便照着在 App 里搜 |
| `m3u8` | 标准播放列表，可给其它播放器直接导入 |

#### 两种变体（对应你的两个歌单）

```
① 全部曲目      —— 每张专辑的全部曲目, 按 disc/track 顺序
② 每张第一首    —— 只取每张专辑第一首, 用来建"专辑目录"歌单
```

曲目顺序 = **对照结果里专辑的顺序** × 每张专辑的 disc/track 顺序；

> 这里踩过一个坑：第一版导出按 `album_id` 排序，把 A 碟的曲目排到了 B 碟前面 ——
> 看起来像"匹配错了"，实际是导出顺序被打乱，白排查一轮。
> **专辑顺序必须跟着传入顺序走**，只有专辑内部才按 disc/track 排。


`first` 模式取的是该专辑 disc/index 最小的那首。

面板上对照完就有两个按钮（`全部曲目` / `每张第一首`），生成后可以**复制 ID 列表**或**下载 M3U8**。

#### 只导出**已匹配网易云**的专辑

没上线的碟进不了歌单，导出时直接跳过；顺带会告诉你哪几张专辑取曲目失败。

实测 3 张已匹配专辑：

```
全部曲目   102 首 / 3 张专辑
每张第一首   3 首 / 3 张专辑
```

### 已匹配却没封面？跑一次 `covers --from-netease`

**现象**：歌单对照里有些碟明明已经匹配到网易云，卡片却显示「无封面」。

**原因（纯数据滞后，不是 bug）**：这些匹配是在「匹配后回填」功能上线**之前**建立的，
当时没有去取网易云的专辑图。回填只对**新建的**匹配生效。

```bash
python -m dnh covers --from-netease      # 一次补齐, 实测 154 张全部补上
```

**现在能自己发现这个问题**：`counts()` 里多了一个 `matches_missing_cover`，
面板顶部在它 > 0 时会显示 `⚠ N 张匹配缺封面`，统计卡片里也会多一格 ——
这样下次再出现就不会是"你偶然看到"了。

> 教训：给已有数据补上新字段时，要同时提供一个**批量回流**的命令，
> 否则老数据会一直缺，而且只在界面上表现为"看起来坏了"。

### 人工编辑保护 + 跨源去重（多键）

**问题**：手改过信息的专辑，下次从别的源采集会**再插一条重复**。因为去重靠
「规范化标题 + 社团相等」，标题一改就配不上了。

两处修改：

**1. `edited` 标记**（人工编辑优先于一切来源可信度）

* 面板/CLI 改过任何字段 → `edited = True`
* 之后自动采集走 `merge`：`other_wins = False`，**只补空缺，不覆盖已有值**
* 面板卡片上会标 `已编辑` 角标

> 不加这一条，你手工改对的标题会在下一轮采集被第三方来源改回去。

**2. 去重多键匹配**

原来只按标题/社团找候选。现在追加两条强信号：

| 键 | 说明 |
|---|---|
| 源内 id（`album_sources`） | 同一站点的同一条目 = 同一张碟，与标题是否被改无关 |
| 商品编号（`catalog_no`） | 编号是唯一标识，用户一般不会改；**编号相同直接判为同一张** |

> 编号判据是**单向**的：编号相同 → 必定同一张；编号不同 → 不翻案
> （同社团同名碟极少，这种误合并比漏合并容易发现）。

### VGMdb：日文标题 / Resale 过滤 / 卷号错配

**1. 日文名被换成罗马音** —— VGMdb 的 `<h1>` 是三个 span：

```
<span lang="en">Kairo Kidou / toumeikairo</span>      ← 可见(罗马音)
<span lang="ja">回路起動 / 透明回路</span>            ← 原文(隐藏)
<span lang="ja-Latn">...</span>                        ← 罗马音(隐藏)
```

原来取第一个非空 span → 拿到罗马音。现在按 `ja > 含日文 > ja-Latn > en` 取，
并且 **`lang="ja"` 只有文本真是日文时才可信**（老页面会把罗马音标成 `lang="ja"`）。

**2. Resale（非首发）混进来** —— event 页每行第一列是 TYPE：`New Release` / `Resale`。
`Resale` 是再贩/非首发，属于该展会页但不是那次的新作，默认跳过
（`skip_resale = true`，可在 `[sources.per_source.vgmdb]` 关掉）。

**3. 网易云 vol.2 匹到 vol.1** —— 整体相似度 0.9+ 但那是两张碟。加了卷号硬冲突：

```python
volume_token("XXX Vol.2") == "2"
volume_conflict(["XXX Vol.2"], "XXX Vol.1")  # True -> 直接判负
```

**4. 错配后封面被覆盖** —— 原来"网易云更清晰就换"。现在已有**非网易云**封面时，
只有匹配分数 ≥ `cover_replace_min_score`(0.9) 才允许替换。

### 「标记」vs「标签」：两个概念，别混

| | 是什么 | 存在哪 |
|---|---|---|
| **标记**(mark) | 人工状态标签，如「已加入歌单」「待补封面」 | `album_marks` 表，按需打/删 |
| **标签**(tag) | 站点给的分类（`#インスト`、`#女性ボーカル` 等） | `album_tags` 表，随采集写入 |

两者在界面上的分工：

* **专辑库**：`标记` 下拉用于**筛选**；批量条保留通用的 `打标记 / 移除标记`
  （输入框**不再预设**「已加入歌单」——那是歌单对照的活）
* **歌单对照**：卡片上的 ✓ 直接打「已加入歌单」，这是该标记的主要来源
* **标签**：在卡片编辑面板里用胶囊增删

> 教训：删除功能前先确认它是不是"另一个功能"的入口 —— 我一度把 标记 当成 标签
> 的重复入口，差点把批量打标记整条链路删掉。

### 展会筛选显示展会名（修序号后缀丢失）

**现象**：浏览专辑的「展会」下拉里，M3-2026春 / M3-2026秋 / M3-58 全显示成 `M3`。

**原因**：`PublicData.events()` 用 `event_kind_label(event_kind)` 当显示标签 ——
那是**类型**标签（`m3` → "M3"），不是展会名，序号后缀自然全丢。

**修法**：标签改用展会名 `row["event"]`；类型标签另存 `kind_label`（展会类型筛选仍用它）。
实测下拉：`全部 · M3-2026春 · M3-2026秋 · C108 · 例大祭 · M3-2019秋`，
库里 5 个展会全出现，裸 `M3` 选项 0 个。

### 用户端不再持有任何来源键表

带来源站名的外链展示顺序表已从用户端移走：

| | 位置 |
|---|---|
| 顺序表 | `dnh/webui/linkorder.py`（站长端侧单独存储） |
| 用户端 | 只按 `urls` 原始顺序渲染；`_url_sort_key()` **只认中性档位**（special/official/site 靠前，其余保持原序） |

现在 `dnh/public/` 全目录 grep 任意来源站名 → **0 命中**。

#### 过程中的一个坑

删表时漏了一处使用点（详情页外链排序仍引用 `_URL_PRIORITY`）→ **详情页会 NameError 500**。
这是本项目第三次踩"改了定义、漏了引用"，所以补了守卫测试
`tests/test_public_events_labels.py`（7 个用例）：展会标签必须是名字、用户端不得出现来源名、
顺序表必须仍在站长端可用。

### 版权与合规政策（已按 A 方案定案）

**署名：不署名（A 方案）。** 理由（用户判断）：

* `bgm.tv` 采用 **Creative Commons BY-SA**
* `VGMdb` / `dojin.info` 未声明许可，但本质上是**用户创建并上传**的数据类网站
* 因此「收录并展示」属于正常使用，**本站不再逐条署名来源**
* 封面虽属于发布者/社团资产，但作为数据库网站收录封面同样属于正常使用，再分发无风险

**本站数据遵从 CC BY-SA 4.0。**「关于本站」与页脚都写明：

> 本站数据由**第三方与用户共同提供**，可能存在错漏或过时，仅供参考，请以发布者官方信息为准；
> 专辑封面与曲目等内容的权利归各**发布者 / 社团**所有，本站仅作收录与展示。
> 本站的整理数据遵从 [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/)。

### 外链与封面：两条硬规则

> **名单放在 `dnh/linkpolicy.py`（public 包外）** —— 这是刻意的：用户明确要求
> 「用户端代码/数据里不出现数据来源名」，而这份名单又必须点名那些站点。
> 把政策放在 `dnh/` 顶层，由 `dnh/public/` import 使用，两个要求就都成立了。
> `tests/test_public_events_labels.py` 有目录级守卫盯着 `dnh/public/**`，
> 所以**不要在 public 包里重新写死这批域名**（注释里也不行）。

### 外链与封面：两条硬规则

| 规则 | 做法 |
|---|---|
| **禁止其它数据网站的外链** | `/link/` 路由按 host 名单拒绝（vgmdb.net / bgm.tv / bangumi.tv / dojin-music.info / doujinstyle.com / dizzylab.net / discogs.com / touhoudb / thwiki / vgm.io 图床）；详情页也**不生成**这类链接。试听（youtube/bilibili/soundcloud/bandcamp…）与通贩（booth/melonbooks/toranoana）照常放行 |
| **封面只走本地缓存或对象存储** | `/cover/<key>` 命中本地缓存直接 200；未命中则**服务端拉取并落盘**后 200；**不再 302 到源站**（那是 hotlink）。配了 `cover_base_url` 则重定向到对象存储/CDN |

> 这两条是在 `public/` 层实现的合规约束，**不是性能优化** —— 改动时别为了"省一次请求"退回 hotlink。

### 匹配收紧：4 条硬判据（宁可漏不可错）

用户要求杜绝三类误配：**只匹配到专辑名相同**、**同系列不同代**、**同艺人不同专辑**。
`dnh/netease/matcher.py` 现在有 4 条**触发即 0 分**的硬判据（任何维度都救不回来）：

| 判据 | 默认阈值 | 防的是什么 |
|---|---|---|
| 版本/代次冲突 | — | `Vol.2`↔`Vol.1`、`第2弾`↔`第1弾`、`Part 2`、`Disc 2`、末尾罗马数字 |
| **标题硬底线** | `title_hard_min = 0.5` | **同艺人不同专辑** —— 旧加权允许 `0.65×0.375 + 0.25×1.0` 凑过阈值 |
| **曲目表硬底线** | `track_hard_min_overlap = 0.25` | 同名不同碟 —— 旧逻辑只 `×0.75`，高峰候选照样过线 |
| 年份 | `year_gap_penalty = 0.85` | 两边都有年份却不等时折扣，使其难以越过 `auto_score` |

实测（30 张真实专辑双策略对照）：**改判 0 · 失去 1**，唯一失去的是
`HARDCORE SYNDROME BEST` → `HARDCORE SYNDROME 19`（**本身就是同系列不同代误配**）。

#### ⚠️ 一个已知取舍，需要你拍板

`Blossoms` → `4 Seasons`（同社团、**曲目表完全一致**、网易云那边换了专辑名）现在会被**判负**。
这是「标题分低于阈值 → 直接判负，艺人/曲目都救不回来」的**必然代价**。
若要救回这一类，唯一干净的口子是「两边曲目表全重合时豁免标题判据」——但那等于放弃这条要求。

#### 从自动匹配降级的人工线索

低于 `auto_score`（0.88）的匹配现在会标 **「待核实」**（站长端卡片与抽屉），
提示人工核对是否同名不同碟 / 同系列不同代。

### 曲目表：整段粘贴 → 智能识别 → 批量填入

编辑专辑（抽屉）与手动新增面板都有**批量粘贴**入口：把从各处复制的曲目列表整段粘进去，
自动解析出**曲名 / 艺人 / 时长 / 轨号**并填入结构化编辑器（「替换曲目」或「追加到末尾」）。

* 解析器 `dnh/trackparse.py` 是**纯函数**，用户端与站长端共用（保证两端行为一致）
* 端点 `POST /api/tracks/parse`（只读、不落库）；接口不可用时前端有**本地兜底解析**并明确提示
* 支持：`01. 曲名 - 艺人 4:12`、全角数字、`Tr.1`/`Track 1`/`#01`、制表符表格、`4分12秒`、
  `1:02:03`、表头与注释行跳过、无编号按顺序编号
* **保守优先**：`曲名/艺人`（无空格）不拆、`Re:Collection -X-` 不拆、行中 `4:12` 不当时长 ——
  宁可少拆，不把标题切碎

## 歌单对照：两条必须守住的约定

这两条都是**真实踩过、且很容易再踩回去**的地方。改对照相关代码前先读这一节。

### ① 渲染的唯一数据源是 `plGroups()`，不是两组并集

**曾经的 bug**：① 直接拿 `plAlbums()`（①②两组 concat）当数据源 →
同一张已标记的专辑**同时出现在 ① 和 ③**（用户报的现象是"标记后在两个分组里都在"）。

**现在的约定**：

```
renderPlCompare()
  -> var groups = plGroups();      // 唯一入口
       groups.add      -> ① 待加
       groups.marked   -> ③ 已标记
```

`plGroups()` 做三件事，缺一不可：

1. 把两组的**并集**按 `key` 合并 —— 并且**合并 `marks` 而不是"先到先得"**
   （先到的那份可能是没带 `marks` 的旧副本，按它分组就会分错）
2. 按"**有没有标记**"这**唯一判据**重新分流
3. **把结果回写** `result.db_only` / `result.db_only_marked`

于是「① ∩ ③ = ∅」是**结构性保证** —— 不依赖调用顺序，也不会被服务端或别处遗留的
脏数据破坏。`plAlbums()` 只是它的**并集视图**（给查找/同步/移除用），**不要再拿它当 ①**。

测试守着它：整条流程（初始 → 标记 → 取消标记）每一步都断言**交集为空**且**并集数量守恒**；
另有把 `plGroups()` 的**真实源码抽进 node 执行**的测试，用"同一张在两组都有 + 组内重复"的
脏模型验证分流。

### ② 那一组按**专辑**聚合，不逐首歌列

**曾经的 bug**：② 「歌单里有、本地库没有」是**逐首歌**渲染的（标题取 `song_name`，
专辑名只是副信息）→ 一张碟 10 首就 10 行，用户看到"全是歌名"。

**现在的约定**：服务端提供**专辑级**聚合 `playlist_only_albums`（`group_playlist_only_albums()`），
前端 ② 只从它渲染：

| 项 | 规则 |
|---|---|
| 聚合键 | 优先 `album_id`（同碟不同版本 id 不同，正好分开）；无 id 退回**规范化专辑名**（去空白/全角）；都缺 → `(无题专辑)` 兜底桶 |
| 排序 | `song_count` 降序 → 专辑名 → `album_id`（后两个键保证同一输入**两次结果一致**，刷新不跳） |
| 守恒 | 每首歌恰好落进一个桶 → `sum(song_count) == len(playlist_only)` 恒成立 |
| 保留 | `playlist_only`（逐首）**原样保留**，导出与既有逻辑继续用它 |

统计框是 `12 张 / 63 首` **双写**：大数是专辑数（新口径），括号里留歌数 ——
免得用户以为"数字变小 = 数据丢了"。

> `song_count` 是"**歌单里能对上的曲目数**"，**不是**网易云该碟的总曲目数（后者要联网查）。

### 相关口径（同一套体系，改一处要想到其它）

| 概念 | 口径 |
|---|---|
| 取消匹配 | **对级拉黑** `(album_key, 网易云 album_id)`；那张碟**仍参与匹配**，只是排除这一条候选 |
| 手动匹配 | `method="manual"`，**权威优先**：自动匹配看到它就跳过（零请求）；与拉黑互斥 |
| 运行统计 | `blocked_excluded`（拉黑排除的候选数）/ `manual_matched`（手动跳过的张数） |

## 用户端站点（`dnh public`）

围绕专辑数据库建立的**独立用户端**：浏览 / 搜索 / 详情 / 社团 / 展会 / 标签 / 看板 /
工作队列，再加上**注册登录与编辑者订正**。它与数据采集面板（`dnh serve`）是两个不相干的
项目——用户端不出现任何外部面板入口、不提数据从哪来，也不引用任何 CDN。

```bash
python -m dnh public --port 8940
# 或
python -m dnh public --host 0.0.0.0 --port 8940
```

| | 用户端 `dnh public` |
|---|---|
| 浏览 / 搜索 / 详情 / 社团 / 展会 / 标签 / 看板 | ✅ 不需要登录 |
| 工作队列（缺封面 / 缺曲目 / 缺简介 …） | ✅ 不需要登录 |
| 注册 / 登录 / 我的账号 | ✅ |
| 编辑专辑资料（标题/社团/艺人/发售日/展会/编号/简介/标签/曲风/词条/外链/曲目） | ✅ 仅 `editor` |
| 数据采集 / 歌单对照 / 后台任务 / 报告 | ❌ 用户端完全没有这些概念 |

> 要把它开放到公网（免费 VPS + Caddy 自动 HTTPS + systemd 常驻 + 备份 + SSH 隧道），
> 见下面的《[部署到公网（免费 VPS）](#部署到公网免费-vps)》。

### 交付内容

```
dnh/public/data.py       只读查询层(ReadOnlyStore 用 mode=ro 开库)
dnh/public/templates.py  纯函数 HTML 模板
dnh/public/site.py       路由 + http.server + 封面/外链跳转 + 账号路由 + CLI
dnh/public/users.py      账号库(独立 sqlite) + pbkdf2 口令哈希 + 邀请码 + CLI
dnh/public/auth.py       服务端会话 / Cookie / CSRF / 鉴权判定
dnh/public/static/       手写暗色主题 CSS(零 CDN, **零 JS**)
tests/test_public_site.py  90 个测试(路由/模板/只读查询/HTTP)
tests/test_public_users.py 77 个测试(口令/角色/会话/CSRF/403/落库/展示修复)
tests/test_public_profile_page.py 18 个测试(资料页头像/元信息/隐私档位/空态)
tests/test_detail_v2_layout.py    19 个测试(按钮位置/社团结社/曲目展开/分碟号)
tests/test_track_extra_fields.py  12 个测试(语种曲风等字段的解析→落库→展示)
tests/test_track_table_structure.py 7 个测试(tr 里只能有 td/输入框可见性)
tests/test_mobile_track_layout.py 12 个测试(手机版卡片式曲目表)
tests/test_external_link_warning.py 8 个测试(外链跳转确认框)
```

### 账号与权限

| 角色 | 能做什么 | 怎么获得 |
|---|---|---|
| 访客（未登录） | 浏览、搜索、看详情与看板 | — |
| `viewer` 普通用户 | 同上 + 查看「我的账号」 | 注册即得（默认角色） |
| `editor` 编辑者 | 同上 + 打开编辑界面、订正专辑资料 | 注册时填入**一次性邀请码**，或由站方用命令行提升 |

账号存在**独立**的 `data/public_users.sqlite3`（`users(id, username, password_hash,
role, created_at, last_login)`），**不往专辑主库加表**。口令用 `hashlib.pbkdf2_hmac`
加随机盐存储（`pbkdf2_sha256$240000$<salt>$<digest>`），不保存明文；校验用
`hmac.compare_digest` 做常数时间比较。

```bash
python -m dnh.public.users init                 # 建表(幂等)
python -m dnh.public.users add alice            # 交互式建号, 默认 viewer
python -m dnh.public.users add bob --role editor
python -m dnh.public.users invite --note "给 bob"   # 打印一次性编辑者邀请码
python -m dnh.public.users invites              # 查看邀请码是否已被使用
python -m dnh.public.users promote alice        # 提升为 editor(--role viewer 可降级)
python -m dnh.public.users passwd alice
python -m dnh.public.users list
```

> 表单里**没有** `role` 字段：角色只由服务端按邀请码或管理命令决定，改前端也提不了权。

### 写操作的边界

* **访客 / 普通用户 / 未登录**：任何非 `GET`/`HEAD` 的请求一律 **403**（不再是笼统的 405）；
* **编辑者**：只有 `POST` + 同源（`Origin`/`Referer` 与 `Host` 一致）+ 隐藏域里的
  CSRF token 三者都成立才会落库，缺一即 403；
* 会话是**服务端**表（`secrets.token_urlsafe` 随机 sid），Cookie 带 `HttpOnly; SameSite=Lax`；
  登出走 `POST`（导航里就是一个带 token 的小表单）并清 Cookie；
* 写路径**按需**才开可写连接（`from dnh.db import Store`）；访客与普通用户的读路径始终是
  `mode=ro` 的只读连接 —— 不写操作的时候，用户端连一个可写句柄都不存在；
* 保存走 `Store.update_album_fields`（白名单字段，自动置 `edited=True`），表单校验失败会把
  原值连同红字一起回显（**不会 500，也不会改到库里**）。

### 不暴露资料来源

用户端页面里不出现来源站名、来源统计、覆盖率、抓取说明，也不出现任何外部管理面板入口。
专辑详情的外链因此走本站的 `/link/<专辑>/<序号>` **中转**：页面上只有「外部链接」，
点出去仍然是真实外链地址，但 HTML 里不会被印上资料来源。

### 外链嵌入播放器（只认 embedding code）

外链里**用户自己粘贴了 `<iframe ...>` embed code** 的，会在链接按钮**下方**渲染播放器：

```
[XFD (SoundCloud)] [XFD (Youtube)] [特设]      ← 按钮先出现
┌──────────────────────────────┐
│ ▶  SoundCloud 播放器 (300px)  │             ← 播放器在下方
└──────────────────────────────┘
┌──────────────────────────────┐
│ ▶  YouTube 播放器 (315px)     │
└──────────────────────────────┘
```

**关键：纯链接不会生成播放器。**

```
https://soundcloud.com/aoivcelica/quinceblume   → 只有按钮，没有播放器
https://www.youtube.com/watch?v=l1JRKYl5CeM     → 只有按钮，没有播放器
<iframe src="https://w.soundcloud.com/player/…"> → 按钮 + 播放器
```

为什么不做自动识别：全库 2123 条 YouTube/SoundCloud 外链里，大量是频道页、个人主页
（`youtube.com/c/xxx`、`soundcloud.com/xxx`），根本没东西可播。自动嵌入只会塞满一排空框
—— 那正是之前出现的问题。

**播放器位置**：`.embed-list` 是 `.links` 的**兄弟节点**（在它下方）。之前塞进了
`.links` 里面，而 `.links` 是 `display: flex`，结果 iframe 成了 flex item，**把按钮拉成和
播放器一样高**。

**embed code 适配**（`dnh/public/embed.py`）：

| 从 code 里取 | 处理 |
|---|---|
| `<iframe src="...">` | 取第一条 iframe 的 src；必须是 http(s) |
| `height="300"` | 用作播放器高度，夹在 80–480px |
| 没有 height | 按平台默认（YouTube 232 / SoundCloud 166），CSS `aspect-ratio: 16/9` |
| code 里的 `<a>标题 · 作者</a>` | **丢弃**（只保留纯文本给 aria-label） |
| 被禁站点的 src | 拒绝（与 `/link` 同一份名单） |
| `javascript:` / `data:` src | 拒绝 |

**合规做法**：iframe 的 `src` 不是平台地址，而是本站的 `/embed/<专辑>/<序号>`，
服务端从 embed code 里抽出真实地址后 302：

```
GET /embed/quince-blume|2026/6
→ 302  https://www.youtube.com/embed/l1JRKYl5CeM
```

所以页面 HTML 里**只有 `/embed/...` 本地路径**，不出现 `youtube.com` / `soundcloud.com` /
`api.soundcloud.com`。CSP 也只放开 `frame-src 'self'`。

`/embed` 只接管 **embed code**：纯链接没有播放器，访问 `/embed` 也返回 404 ——
不给任意 URL 当开放重定向用。

### 修订记录：同一人短时间的多次修改折叠

编辑历史里，**同一个人 10 分钟内的连续修改**会折叠成一行：

```
版本        时间              编辑者   说明
#12–#15     2026-11-17 01:00  alice   [4 次] 约 5 分钟内  改标题；改曲目；改艺人
                                        └ 展开 4 条            （每条仍可单独对比）
#11         2026-11-17 00:43  bob     改社团
```

规则：

* 相邻两条修订 **username 相同** 且 **时间差 ≤ 10 分钟** → 同一组
* 中间有人插进来改过 → **断开**（不会把别人的改动折叠进去）
* 折叠行显示「N 次 · 约 M 分钟内」+ 说明摘要
* `<details>` 可展开逐条（每条仍有独立的「对比」链接）
* 「还原整版」用组内**最新**那条

阈值是 `templates.HISTORY_GROUP_GAP`（默认 600 秒），改它就能调节。

### 只读是怎么保证的

* 读连接用 `file:...?mode=ro` —— 写语句直接抛 `OperationalError`
* 未登录/普通用户的 HTTP 写请求一律 **403**，且没有任何写处理器会被调用
* 唯一能触发写的是编辑者会话 + 同源 + CSRF 校验全部通过的那一个 POST

### 三处前端展示修复

| 问题 | 原因 | 修法 |
|---|---|---|
| 卡片标签显示不全、被裁掉 | `.tagrow` 是 `flex-wrap: nowrap; overflow: hidden`，`.album-card` 又 `overflow: hidden`，第 4 个标签起就被切掉 | 标签行改成 `flex-wrap: wrap` 且不设 `overflow`，卡片去掉 `overflow: hidden`（圆角裁剪只留在封面上），长标签 `overflow-wrap: anywhere` |
| 展会索引右侧「类型」列纵向不对齐 | `.index-row` 是 flex，链接 `flex: 1 1 auto`、类型列只有 `min-width`，各行的类型跟着名字长短左右漂移 | 改成网格 `minmax(0,1fr) 120px 88px`（名字 / 类型 / 数量三列固定），`align-items: center`，数量列右对齐；窄屏收窄但仍是三列 |
| 曲目表「共 N 首」方向/位置不对 | `<caption>` 里 `display: flex` 只是把计数粘在「曲目表」后面，没有左右分列，还会跟着文档方向翻转 | caption 固定 `direction: ltr`、`align-items: baseline`，「曲目表」在行首、「共 N 首 · 总时长 …」用 `margin-inline-start: auto` 顶到行尾并右对齐（逻辑属性，RTL 下自动镜像） |

### 公开资料页（`/u/<用户名>`）

任何访客都能看别人的资料页。**以前这里只有一个「贡献」列表**——因为模板漏了头像、
没渲染加入时间/统计，而简介/收藏/关注又取决于用户是否填过、隐私档位是否开放，
所以大多数用户的页面看起来像"只显示编辑历史"。

现在头部固定展示这些信息（`public_profile_page`）：

```
┌──────────────────────────────────────────────┐
│  ⬤   hhdaf  [编辑者]                         │
│      加入于 2026-11-13 12:16 · 最后活跃 …     │
│      收藏 2 · 关注 3 · 贡献 7                 │
└──────────────────────────────────────────────┘
```

* **头像**：`/avatar/<uid>`，没上传过就发按 uid 生成的默认 SVG（首字母 + 配色）
* **加入时间 / 最后活跃**：来自 `users.created_at` / `last_login`
* **统计**：收藏、关注、贡献 —— **只列出大于 0 的项**，避免一排「收藏 0 · 关注 0」
* **不显示粉丝数**：本站的关注只支持 社团 / 艺人 / 展会（`FOLLOW_KINDS`），
  用户之间不能互相关注，这个数字永远是 0，显示了只会让人以为功能坏了

下面是按**隐私档位**决定是否出现的区块（`vis_bio` / `vis_birthday` / `vis_favorites` /
`vis_follows` / `vis_contributions`，0 所有人 · 1 站内 · 2 关注者 · 3 不可见）：

| 区块 | 内容 |
|---|---|
| 简介 | 纯文本 + URL 自动识别，外链点击前弹「跳转站外」确认 |
| 生日 | 按档位显示 |
| 收藏 | 专辑封面卡片网格，点进专辑详情 |
| 关注 | 社团 / 艺人 / 展会，社团和艺人给站内链接 |
| 贡献 | 时间 + 类型 + 目标（编辑历史） |

全空时给一句「该用户还没有公开其它资料（简介 / 收藏 / 关注）」，而不是一片空白。

> CSS 用 `pub-profile-*` 前缀，与账号页自己的 `.profile-*`（96px 圆角方形头像那套）互不干扰。

### 编辑界面：固定网格布局

编辑界面是**固定**的卡片网格（两列）。位置写死在 CSS 里，**不能拖动、不能切大小** ——
早期版本做过「长卡/半卡切换 + 拖动排序 + 偏好保存」，实际用起来是多余的操作负担，已全部移除。

版面：

```
┌──────────────┬──────────────┐
│ 封面 & 标题   │ 出品信息      │   row1
├──────────────┤ (跨 row1-2)  │
│ 分类          │              │   row2
├──────────────┴──────────────┤
│ 简介（整行长卡）               │   row3
├──────────────┬──────────────┤
│ 制作人员      │ 外链          │   row4（同一行等高）
└──────────────┴──────────────┘
┌──────────────────────────────┐
│ 曲目表（整行，在网格之外）      │
└──────────────────────────────┘
```

关键设计：

* **右上「出品信息」纵向跨两行**（`grid-row: 1 / span 2`）—— 它字段最多，跨行正好填满
  「封面&标题 + 分类」叠起来的高度，不留空隙
* **分类填在封面&标题下方**（左列第二行），把左列的空缺补上
* **简介整行长卡** —— 它是自由文本，需要宽度
* **制作人员 + 外链同一行、两个半卡，等高** —— 靠 `align-items: stretch` +
  卡片 `display: flex; flex-direction: column` + 内容区 `flex: 1 1 auto` 撑开
* **窄屏（≤980px）单列堆叠**，所有卡片 `grid-row: auto`

对应 CSS（`public.css`）：

```css
.edit-cards {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  grid-auto-rows: min-content;
  gap: 14px;
  align-items: stretch;
}
.edit-card-cover      { grid-column: 1;    grid-row: 1; }
.edit-card-category   { grid-column: 1;    grid-row: 2; }
.edit-card-info       { grid-column: 2;    grid-row: 1 / span 2; }
.edit-card-description{ grid-column: 1 / -1; grid-row: 3; }
.edit-card-credits    { grid-column: 1;    grid-row: 4; }
.edit-card-links      { grid-column: 2;    grid-row: 4; }
```

**字段归属**

| 卡片 | 字段 |
|---|---|
| 封面 & 标题 | 封面图 / 上传、标题(原题)*、标题(译名)、罗马字 |
| 出品信息 | 社团/制作人、发行公司、展会、发售日、品番、价格、展馆、展位 |
| 分类 | 标签、曲风 |
| 简介 | 简介（空行分段） |
| 制作人员 | **艺人** + 词条（职责/人员，可增删） |
| 外链 | 链接名 + 网址（可增删） |
| 曲目表 | 分碟曲目编辑（见下一节） |

**几处语义调整**

| 项目 | 改动 |
|---|---|
| 封面 & 标题 | 合并成**同一张卡片** |
| 艺人 | 从「出品信息」移到「制作人员」—— 艺人属于创作者，不是出品信息 |
| 社团 + 发行公司 | 「社团/制作人」和「发行公司」放在一起（同人碟常是自家社团，发行公司留空即可） |
| **编号 → 品番** | 这个字段的标准叫法是「品番」 |
| 发售日 | 选好**展会后自动填入**（见下） |

> 拖动手势只保留在**曲目行**上（`⠿` 调整曲目顺序），卡片本身不可拖动。

### 制作人员：全部 chip 按钮化

**以前**：上面一个「艺人」输入框 + 候选下拉，下面一组「词条 / 人员」文本行 —— 两套东西，
艺人到底是哪个 id 完全看不出来。

**现在**：最上方一排按钮就是**详情页最终显示效果** —— 有职责的显示「职责 · 人名」，纯艺人只显示人名：

```
[艺人 Riku ×] [mastering · Ryo Mishima ×] [Illustration · いぬすけ ×]
┌────────────────────────────────────────┐
│ 搜艺人名 / #编号，点选加入              │
└────────────────────────────────────────┘
词条
[mastering ][Ryo Mishima ×][输入艺人名后回车][×]
[Illustration][いぬすけ ×][输入艺人名后回车 ][×]
```

### 详情页与编辑页**完全一致**

两边用**同一套合并算法**（详情页照抄编辑页规则），所以按钮列表的顺序和内容逐项相同：

1. 先按 `album.artists` 的顺序放入「艺人 · 人名」
2. 再遍历词条：人名已在且还没职责 → **补上职责**（仍是一条按钮）；
   已在且有职责 → 跳过；不在 → 追加
3. 结果：**每个人只有一条按钮**，顺序由 artists 打头

```
专辑艺人: MALVA. / しおの / すばる / 綾花 / CS4W
词条:     Circle Logo→綾花, Jacket Illustration→しおの,
          Lyrics Tr.4→すばる, Produce/Compose/...→MALVA.,
          Collaboration Tr.5→CS4W

详情页 = 编辑页 =
  Produce / Compose / Lyrics CD Design / Web / Movie · MALVA.
  Jacket Illustration · しおの
  Lyrics Tr.4 · すばる
  Circle Logo · 綾花
  Collaboration Tr.5 · CS4W
```

> 曾经的问题：详情页把「艺人」单独列一遍、再把词条列一遍，同一个人出现两次
> （5 个艺人 + 5 条词条 = 10 个按钮，一半重复）。现在合并成 5 个。
> `tests/test_people_parity.py` 有逐场景的一致性守卫。

**职责和人员都是输入框，全靠回车**：

1. 「职责」框填 `mastering`
2. 「人员」框敲 `Ryo Mishima` **回车** → 该行多一个 `Ryo Mishima ×` 小 chip，
   上方同时生成 `mastering · Ryo Mishima` 按钮
3. 人员框支持一次敲多个（`、` 或 `,` 分隔）
4. 改完职责再在职责框回车 → 用新职责重新生成按钮

反过来也行：先在上方按钮区加入艺人，再在人员框输名字。

* **「艺人」绑定栏位已取消** —— 人员统一以按钮形式保存
* 按钮 = 专辑艺人 ∪ 每条词条拆出的「职责 · 人名」组合（去重）
* 每个按钮带 `data-entity-id="<ArtistId>"`，**锁定具体艺人**
* 输入时仍给库内艺人候选（点选也行，但不是必须）
* 隐藏字段 `name="artists"` 保存全部人名（换行分隔），`credit_name` 保存每行的人（`、` 分隔）
* 行内 chip 和上方按钮删除时**双向同步**：删上方按钮会把词条里同名的人一起清掉

> 上方候选框会在**失去焦点 / 点页面其它地方 / Esc / 选中一个之后**自动收起。

### 封面：点击放大 + 多图槽位

封面下方是一排**槽位**：

```
┌────────┬────────┬────────┬────┐
│ 封面图  │  图 2  │  图 3  │ ＋ │   ← 加号永远在最后
│  [封面]│   ×    │   ×    │    │
└────────┴────────┴────────┴────┘
```

* **第 1 格固定是当前封面**（蓝色边框 + 「封面」角标）
* **右侧加号**点击开文件选择，可选多张，逐张上传后插在加号之前
* **每一格都能删**（右上角 ×）；删掉当前封面会把下一张自动提为封面
* 点任意缩略图 → 把它设为封面
* 点大图 / 「放大」按钮 → 全屏灯箱，Esc 或点任意处关闭

### 草稿提示在卡片外

「编辑内容会自动存草稿 / 清除草稿」这条提示原来挤在封面卡片里，现在移到**表单最上方、卡片网格之外**，一眼能看到又不占卡片空间。

### 曲目表：拖动把手在最前 + 时长列

```
排序(⠿) | 音轨 | 歌曲名 | 副标题 | 翻译歌名 | 艺人 | 语种 | 曲风 | 时长 | 操作
```

* 拖动把手放**最前面**
* **时长列加回来了** —— 之前只剩隐藏字段，界面上看不到也没法填
* **新增 Disc 时的表格与模板完全一致**：表头 10 列同序，不再带「批量添加歌曲」按钮，
  并且新碟会**自动生成一行**（原来是空表，看起来像"没加上"）

**为什么要 ArtistId**：同人圈重名极其常见（两个都叫 `Riku` 的艺人），同一个人还常有多个
马甲（不同艺名各自是独立艺人条目）。只靠名字匹配会把 A 的作品挂到 B 头上，所以按钮上
必须带 id。

数据来源：`Store.entity_list("artist", limit=2000)` → `{id, name}`，编辑页把它转成
`bind_ids` 映射；已属于该专辑的艺人名字 → 对应 id 直接写进 chip。

> 保存时一个职责对应多人，后端仍按既有设计**拆成多条 Credit**
> （`compose` + `Riku`、`compose` + `Clion` 两条）。

### 封面：点击放大 + 多图切换

* 点封面图（或右下角「放大」按钮）→ 全屏灯箱，点任意处或 Esc 关闭
* 文件框可以**选多张**：逐张上传，成功后追加到封面下方的缩略图条
* 点缩略图切换主封面（当前那张会有蓝色边框）

> 单张仍然走原来的「上传封面」按钮；多张才走批量上传流程。

### 自动保存草稿

编辑表单内容变化后（防抖 1.2 秒）把**表单值**存到 `localStorage`（`dddb:edit-draft`）：

* 不会提交到服务器，失败也不影响编辑
* 只**提示**「发现 xx:xx 的草稿」并给「恢复草稿」按钮 —— 不自动套用，
  避免冲掉别人对同一张专辑的修改
* 「清除草稿」可以手动丢掉

### 不要把回车当提交

在任意 `input` 里按 Enter **不会**触发保存（这一点很容易误操作，尤其是改曲目表时）：

* 编辑表单内 `input` / `select` 上的 Enter 被 `preventDefault` 掉
* `textarea`（简介）里回车正常换行
* 显式提交方式：点「保存」按钮，或 **Ctrl/Cmd + Enter**

### 展会备选：不用输入就显示

展会字段的候选列表加了 `data-suggest-all="1"`，**聚焦即列出全部候选**（空输入也显示），
其它字段仍保持「有输入才出候选」以免一口气铺满屏幕。

### 曲目表：拖动把手移到最前 + 时长列

列顺序改成：

```
排序(⠿) | 音轨 | 歌曲名 | 副标题 | 翻译歌名 | 艺人 | 语种 | 曲风 | 时长 | 操作
```

* 拖动把手放**最前面**，一眼能看到、按住就能拖
* **时长列加回来了** —— 之前只剩隐藏字段，界面上看不到也没法填

### 同一行等高的保证

「制作人员」和「外链」在同一行，高度可能差很多（1 条外链 vs 20 个制作人员）。
靠三层设置撑平：

```css
.edit-cards { align-items: stretch; }              /* 网格拉伸 */
.edit-card  { display: flex; flex-direction: column; }
.edit-card-body { flex: 1 1 auto; }                 /* 内容区撑开 */
.edit-card-credits .edit-card-body .rows,
.edit-card-links   .edit-card-body .rows { flex: 1 1 auto; }
```

### 选展会后自动填发售日

编辑页选好展会（如 `M3-2026秋`），发售日会自动填成该展会的举办日：

```
GET /event-date?name=M3-2026秋
→ {"name":"M3-2026秋","date":"2026-10-25","end":"2026-10-26","ok":true}
```

日期来自 `dnh/events.py` 里已有的展会推算（M3 春秋 / Comiket 届数），不是硬编码。

**不会覆盖手填日期**：JS 记住上次自动填的值（`lastAuto`），只有当发售日为空、或仍等于上次自动填的值时才覆盖 —— 你手动改过的日期不会被冲掉。

### 曲风按回车提示「请上传封面」—— 一个 `required` 的坑

现象：在**曲风**输入框按回车，浏览器弹出「请上传封面」，光标跳到封面上传区。

原因：封面的 file input 还带着 `required`，但封面上传早已改成**独立 JS 流程**（`fetch` 上传，不是表单提交的一部分）。浏览器原生校验不认这套，遍历表单时看到 `required` 的 file input 没选文件，就跳到那里并提示。

修法：把 `cover` / `background` 两个 file input 的 `required` 去掉。现在整张表单**只有标题(原题)是必填**，与后端真实校验一致。

> 教训：元素一旦脱离表单提交流程（改成 JS 上传），它身上的 `required` / `name` 就该一起清理。

### 编辑界面：曲目表（分碟 / 拖动排序 / 移动端卡片）

曲目表是编辑界面里最复杂的一块，踩过几个坑，这里记下来免得重蹈：

**HTML 结构：`<tr>` 里只能放 `<td>`/`<th>`**

隐藏字段（`track_disc` / `track_duration`）一开始直接放在 `<tr>` 里 —— 那是**非法 HTML**，
浏览器会把它们移到表格外并**重排整行**，表现为「每行被拆成两行、文字重叠、输入框叠在一起」。
修法：把隐藏字段塞进「排序」那一列的 `<td>` 内。JS 新增行时同理（`sortTd.appendChild`）。

**CSS：`.track-row` 不能再有 grid**

表格行被残留的 `.track-row { display: grid; grid-template-columns: … }` 强行改成 grid，
同样会挤在一起。曲目表 CSS 已整块重写，`tests/test_edit_head_layout.py` 里有一条
「`.track-row` 不能有 grid」的守卫。

**不补空行**

`rows_for(values, 3)` 会补足到 3 行 —— 20 首曲目会多出 3 个空行。现在有曲目就按实际数量渲染，
只有**空碟**才给 1 行占位。

**排序：按住拖动，不用上下移按钮**

「排序」列是 `⠿` 把手（HTML5 drag & drop，零第三方库）：

* **只能在同一碟内拖动**（跨碟会破坏分碟语义）
* 拖动时被拖行半透明，目标行显示蓝色上边框指示插入位置
* 松手后自动重排音轨号

**手机版（≤860px）：卡片式，每字段一行**

窄屏下 9 列表格无论怎么调列宽都得横向滚动 450–520px，体验很差。所以手机版彻底放弃表格：

```
┌─────────────────────────────┐
│ 音轨                        │
│ [ 1        ]                │
│ 歌曲名                      │
│ [ Intro                    ]│
│ 语种                        │
│ [ 日语                     ]│
│ 曲风                        │
│ [ Trance                   ]│
│ [删除]  [⠿]                 │
└─────────────────────────────┘
```

表格/行/单元格在媒体查询里都改成 `display: block`，字段名用 `td::before { content:
attr(data-label) }` 显示在输入框上方；输入框字号 14px（12px 在手机上太小）。
全局的 `width: 62px` / `min-width: 130px` 用 `!important` 压掉。桌面版布局完全不变。

### 曲目补充字段：副标题 / 翻译歌名 / 语种 / 曲风

曲目除了曲名、艺人、时长，还可以填 4 个补充字段。**默认不占版面**——详情页只显示
`# / 曲名 / 曲目艺人 / 时长`，有补充信息的行前面才有 `▸` 按钮，**点击后在曲名下方展开**：

```
  1  ▸ 曲名      ARForest   1:40
     └ 语种 日语   曲风 Trance          ← 点击后展开
```

**存储链路**（缺一环就白填，这轮补齐了全部）：

```
Track 模型新增 4 字段    dnh/models.py
  → parse_album_form 解析       dnh/public/site.py
  → update_album_fields 落库     dnh/db.py
  → make_view 带出              dnh/public/data.py
  → edit_values 回填            dnh/public/templates.py
  → 详情页展开显示
```

分碟时详情页显示 `Disc 01` / `Disc 02` 小标题，序号写成 `1-1` / `2-1`；单碟不显示碟标题。

### 详情页布局

```
[编辑这张专辑] [修改记录]        ← 按钮在卡片上方, 右对齐
┌──────────────────────────────────────┐
│ 封面   标题                           │
│        社团 · #ffffff Records [关注]  │
│        徽章 / 信息表 / 标签            │
└──────────────────────────────────────┘
制作人员  [艺人 · ARForest] [作曲 · 某人]   ← 纯 chip 按钮, 不含社团
简介
曲目表（可折叠, 默认展开）
外链（带「跳转站外」确认）
评论
```

几个约定：

* **社团只在标题后显示一次**（`detail-circle`，带关注按钮），**不进制作人员区块**
* 制作人员区块用 **chip 按钮**呈现，不再"文字一行 + 按钮一行"重复
* 「艺人与社团 · 把光标移到名字上…」这类解释文字已删掉
* 外链按钮带 `data-external`，点击弹 `window.confirm`；href 是 `/link/` 中转时提示
  「即将跳转到站外链接」而不是打印内部路径

### 搜索语义

搜索只覆盖标题 / 社团 / 艺人 / 曲目 / 编号，**不搜标签**；直接搜标签名（如 `女性ボーカル`）
会 0 命中，此时零结果页给出「也许你想找」建议块，把匹配的标签 / 展会 / 社团入口递过去。
用户端没有偷偷扩大搜索语义，免得搜索结果变得难以解释。

### 手动新增专辑：与编辑界面能力对齐

以前「手动新增」只有一个曲目 textarea 和逗号分隔的标签输入框，比编辑界面少一大截。
现在两边能力一致：

| 能力 | 手动新增 | 编辑界面 |
|---|---|---|
| 结构化曲目（标题 / 艺人 / 时长，增删行） | ✅ | ✅ |
| 标签胶囊（✕ 删除、回车添加、退格删末项） | ✅ | ✅ |
| **曲风**胶囊（独立于标签） | ✅ | ✅ |
| 自定义词条（职责名自由填写） | ✅ | ✅ |
| 链接（含**自定义名称**） | ✅ | ✅ |
| 简介 | ✅ | ✅ |

复用的是同一套纯 HTML 构建函数（`trackRowHtml` / `creditRowHtml` / `linkRowHtml`），
收集器改成可指定容器（`collectTracks('#a-tracks')` 等），默认参数仍指向编辑面板 ——
所以编辑界面行为不变，只有新增表单多了一份实例。

#### 两条清洗规则在两边一致

* **噪声标签**（`CD` / `Doujin` / `Indie` / `Link` / `同人音乐`…）一律不收
* **曲风与标签去重**：以曲风为准，标签里与曲风同名的项自动摘掉

#### 端到端实测（真实接口 `POST /api/albums/create`）

```
输入: genres=['インスト','CD']  tags=['インスト','東方Project']
      credits=[{作曲,某人A},{自定义职责,某人B}]
      urls={bandcamp:…, 我的自定义链接:…}
      tracks=[{第一首,['歌手X'],252},{第二首,[],61},{标题为空}]
落库: 曲风 ['インスト']            ← CD 被噪声规则去掉
      标签 ['東方Project']         ← インスト 与曲风重名被摘掉
      词条 [('作曲','某人A'),('自定义职责','某人B')]   ← 自定义职责名保留
      链接 ['bandcamp','我的自定义链接']              ← 自定义链接名保留
      曲目 [('第一首',['歌手X'],252),('第二首',[],61)] ← 空标题跳过
✓ 全部符合预期
```

### 编辑界面：自定义词条 / 简介位置上移 / 曲风与标签去重

**① 词条可以自定义了**

原来 credits 只能看不能改（`_EDITABLE_FIELDS` 里没有 `credits`）。现在：

* 编辑面板新增「词条（职责 / 人员）」编辑区，每行 `职责 | 人员 | ✕`
* **职责名自由填写**（作曲 / 编曲 / 作词 / 插画 / 设计 / 混音 / 母带，或任何自定义分类），
  下拉只是建议（`<datalist>`），不限制取值
* 后端 `credits` 进入 `_EDITABLE_FIELDS`，保存时重建 `Credit(role, name, source="manual")`

**② 简介移到「链接 & 试听」上方**

编辑面板的顺序改成：基本信息 → **简介** → 词条 → 链接 & 试听 → 曲目。
简介是最常改的字段，放在最需要滚动才能看到的位置并不合理。

**③ 曲风与标签去重**

专辑自带的 tags 里常常就含 genres（`Album, CD, Digital` 这类），两边都显示会重复、
筛选时也算两次。现在**以曲风为准**：保存时把 tags 里与 genres 同名的项摘掉。

> 注：`CD` 这类本身已由噪声标签规则过滤（见上一节），这里处理的是"同一词出现在两个字段"。

### VGMdb Notes：既不进简介，也不备份

Notes 是版权/出处/分工的堆砌（换行还会被压平），既不写进 `description`，
**也不留在 `raw` 里** —— 留着没人看，只会在扫描/排查时变成噪声。

但它**仍然参与展会识别**（`M3-2024秋` 这类信息就是从 Notes 里认出来的），所以解析
照旧提取，只是不再落库。

### 清掉"纯数字串"简介

解析事故会在 `description` 里留下 `6, 7, 9, 11, 13, 14, 16` 这种值。
判据：整串只由数字与标点（`,，、.-~〜()（）` 和空白）组成，且含数字 —— 直接清空。

实测：清掉 Notes 备份 24 张 · 清掉纯数字简介 2 张。

### 曲风去噪：介质/载体/版本形态都不是曲风

**用户端「曲风」里塞满无关词**。实测库里 `genres` 只有 14 种取值，全是 VGMdb 的
分类与介质，没有一个是音乐风格：

```
25 Digital   23 Original Work   17 Vocal   11 DL Card    8 Original Soundtrack
 8 EP         5 Album            4 Remix     4 Compilation 4 CD-R
 1 Single     1 8cm CD           1 Live Event 1 MD
```

修法：曲风走**更严的判据** `is_noise_genre()` —— 在标签噪声之上再排除
介质（CD / CD-R / 8cm CD / MD / DL Card / Digital / DVD / vinyl…）、
版本形态（Album / EP / Single / Compilation / Live Event / Reprint…）、
出处分类（Original Work / Original Soundtrack / OST…）。

**保留** `Vocal` / `Remix` / `Electronic` / `Rock` / `J-Pop` 这类真实风格。
**出处类**（Original Work / Original Soundtrack）不算曲风，但**作为标签保留**
（有索引价值）—— 这条线是我定的，你要改说一声。

清洗结果：

```
71 张 / 14 种 / 113 条   →   20 张 / 2 种 / 21 条(Vocal 17 · Remix 4)
```

### 真正的元凶：**逗号整串**标签

清完 genres 后页面仍有 `CD, Digital`。查出来是标签**整串存储**：

```
"CD, Digital"  ×9      "CD, Single, Digital" ×1
"CD-R, Digital" ×1     "Compilation, CD, Digital" ×2
```

那是 VGMdb 的 `media_format` 原文被当成**一个标签**存了；`is_noise_tag()` 把它
归一化成 `"cd, digital"`，自然匹配不到噪声集。

修法：新增 `split_tag_values()`，按 `,，、;；/` 拆成独立词后再过滤，
并且**收口在 `_write_album`**（所有数据源都走这一个写入口，一处过滤全部受益），
人工编辑路径同样处理。`album_tags` 全表按新规则重建。

#### 教训

同一个坑这轮踩了两次：**先修了 `genres` 的逗号拆分，却没意识到 `tags` 有同样的问题** ——
因为两次是不同字段、不同时间出现的同一种数据形态。多词字段的写入口必须统一拆分。

### 标签去噪：只留能用于索引的

**规则**（用户定）：标签用于索引**流派 / 展会**，不收「是什么东西」这类无索引价值的词。

```python
_NOISE_TAGS = {"cd", "doujin", "doujinindie", "indie", "link", "links",
               "同人", "同人音乐", "音乐", "music", "other", "その他"}
```

* 写入时过滤（`_write_album` / 手工编辑重标签）
* 一次性清洗全库：`Store.clean_noise_tags()`

实测全库清洗：

```
处理 221 张 · 删除 327 条标签记录 · 6 种噪声标签
剩余 350 种标签 → M3-2026春 / 女性ボーカル / インスト / VOCALOID / C108 ...
```

#### 差点造成的事故

`norm_key("M3-2026春")` 返回**空串**（它对"字母+数字+汉字"混排会切成没有有效词），
而空串被当成"噪声" → 会把 `M3-2026春` 这种**展会索引标签全删掉**。

现在规则是：

* 标签本身为空 → 丢弃
* 归一化失败（空串）→ **保留**（宁可多留，不可误删索引）
* 只有明确命中噪声集合才删

> 教训：写"删除类"规则时，先拿**想保留的样本**跑一遍，而不是只验证该删的能删掉。

### 详情抽屉：去掉冗长的链接说明

「试听 & 链接」为空时只显示一句「暂未添加任何相关链接。」，
那段「特设页 / 社团主页 / 三个来源合并」的解释文字已删除 —— 界面说明越短越好。

### 编辑面板：曲目结构化 / 链接自定义 / 职责 credits

**① 曲目可以逐首编辑了**

原来只有一个 textarea（一行一首标题），艺人和时长根本没法改。现在每首一行：

```
01 | 标题输入框 | 艺人(逗号分隔) | 4:12 | ✕
```

* 「＋ 添加曲目」增行、✕ 删行
* 时长支持 `4:12` / `1:02:03` / `252`，统一存秒
* 后端 `update_album_fields` 原本就支持 dict 形式的 `track_list`，只是 UI 没接

**② 链接名称可以自定义**

下拉里多了「自定义…」，选中后出现名称输入框。后端 `urls` 本来就是自由 dict，
之前只是被 UI 的下拉框限制住了。

**③ bgm.tv 的职责信息拆开了**

原来 infobox 只取「制作人/作曲/编曲/作词」，且**一律**标成 `arrange` —— 职责全混了。
现在各自映射：

| bgm 字段 | role |
|---|---|
| 作曲 / Composer | compose |
| 编曲 / Arranger | arrange |
| 作词 / Lyricist | lyric |
| 插图 / イラスト / Illustration | illustrator |
| 设计 / デザイン / Design | design |
| 混音 / ミックス / Mixing | mixing |
| 母带 / マスタリング / Mastering | mastering |
| 制作人 | produce |

抽屉里这些是**独立条目**，不存在就整行隐藏（不再有一整行「制作」把角色挤在一起）。

### VGMdb 数据清洗：`dnh db reparse`

解析器修好之后，**已经入库的数据不会自己变好**。整轮重采代价太大，但这些专辑的源 ID
都在 `source_ids` 里，按 id 重抓一页就能就地修正：

```bash
python -m dnh db reparse --source vgmdb --dry-run   # 先看会影响哪些
python -m dnh db reparse --source vgmdb             # 实际执行
```

实测（24 张 VGMdb 专辑）：

```
完成: 更新 24 · 跳过 0 · 失败 0
  #MATERIALIZERS        circle='HEKATONCHEIR BEATS, Yoohsic Roomz'
  ALTTERCORE Lux        circle='CONNEMO'
  [AM:28:00:01] 回路起動 circle='toumeikairo'
```

`replace_from_fresh()` 只覆盖**元数据字段**（标题/社团/艺人/credits/曲目等），
封面、标签、标记、匹配记录一概保留；`edited` 的专辑直接跳过（那是用户资产）。

### Credits：VGMdb 的职责信息落成结构化字段

VGMdb 的 credits 表本来就标好了谁做什么，现在解析成 `Album.credits`：

```python
Credit(name="Yooh", role="compose", source="vgmdb")
```

角色映射：`Composed by → compose`、`Arranged by → arrange`、`Lyrics → lyric`、
`Illustration → illustrator`、`Mastering → mastering`…未知角色归 `artist`。

面板详情抽屉里按职责分组显示：

```
制作: 作曲: Yooh, siromaru　｜　编曲: Yooh　｜　插画: Misa
```

实测清洗后 24 张 VGMdb 专辑**全部带上了 credits**（2~17 条/张）。

### 专辑库工具栏：精简 + 页面 logo

按使用习惯收掉了已经并进**卡片编辑界面**的操作：

| 按钮 | 处理 |
|---|---|
| 给选中项打标记 / 移除标记 | 移除（改在卡片编辑里做） |
| 删除封面 | 移除（卡片编辑界面的「删除封面」更精准） |
| 清理日期不符 | 移除（发售日不再由展会日期兜底，新数据不会再产生这类脏值） |
| ＋ 手动新增专辑 | 保留，**挪到工具栏最右**（用弹性占位顶过去） |

批量条现在只剩：`全选` · `已选 N 张` · `删除选中项` ·（右侧）`＋ 手动新增专辑`。

#### 一个必须做的配套改动：空值保护

元素被删掉后，JS 里的 `$('#btn-mark').addEventListener(...)` 会抛
`TypeError`，而它是**顶层初始化代码**——一抛错，后面所有绑定全部不执行，
整个页面的交互就"莫名其妙全坏了"（浏览器不会给出明显提示）。

所以加了 `on(sel, ev, fn)`：元素不存在就静默跳过。

```js
function on(sel, ev, fn) {
  var el = typeof sel === 'string' ? $(sel) : sel;
  if (el) el.addEventListener(ev, fn);
  return el;
}
```

#### logo

页面左上角换成了用户提供的 `DOUJIN MODE` 图（`static/logo.jpg`，30×30 圆角）。

### 专辑库工具栏：合并成一个容器

原来筛选在上一块、批量操作在下一块，看着散；`只看未匹配 / 只看未标记 / 全选`
前面的勾选框还特别占地方。现在：

* 两条 toolbar 收进同一个 `.lib-bar` 容器，批量条用虚线分隔
* 勾选框改回原生 13px，标签文案精简为「未标记 / 未匹配」
* 筛选项与批量操作在**同一个视觉块**里，不再是上下两个独立容器

### VGMdb 抓取报 `name 'SourceError' is not defined` —— 真实原因被掩盖

**现象**：日志里只有一句

```
WARNING VGMdb 展会页 https://vgmdb.net/event/366 失败: name 'SourceError' is not defined
```

**根因有两层**：

1. **`dnh/sources/vgmdb.py` 用了 `SourceError` 却没导入**（只有 `sources/__init__.py` 导入）。
   `_get()` 在 HTTP 403 时抛它，于是**真实错误被替换成 NameError** —— 看到的信息毫无用处。
   补上导入后，同一个请求立刻报出真正的原因：

   ```
   VGMdb 被 Cloudflare JS 挑战拦下 —— 请在 [sources.per_source.vgmdb]
   填浏览器的 cf_clearance 与配套 user_agent
   ```

2. **真正的故障是 `cf_clearance` 已失效**（实测 `GET /event/366` → 403 +
   `Just a moment...`）。处理办法见本文「在面板里直接更新 `cf_clearance`」一节。

### 顺带修掉一个会骗人的探针

`VgmdbSource.available()` 原来用 `use_cache=True, cache_ttl=3600` —— cookie 早已失效，
面板却因为**读到旧缓存**而显示「✅ 可用」。现在探针 `use_cache=False`，每次都真发请求。

实测对比：

```
修前: available = True   （缓存谎报）
修后: available = False  reason = Cloudflare JS 挑战未通过 —— cf_clearance 可能已过期…
```

### 全仓守卫：不许再出现「用了却没导入」

这类 bug 已经发生三次（`pipeline._event_tokens` 插错位置、`db.Credit` 漏导入、
`vgmdb.SourceError` 漏导入），共同点是**只在冷路径上炸，平时测试全绿，触发时还会掩盖真因**。

新增 `tests/test_undefined_globals.py`：用 `symtable` 静态扫描 `dnh/**/*.py`，
找出「被当全局引用但没有模块级定义、也不是内建」的名字。

**守卫有效性是验证过的**（不是摆设）：

```
✓ 缺导入(SourceError 那种)        期望 ['SourceError'] · 实得 ['SourceError']
✓ 缺导入(函数内层, Credit 那种)     期望 ['Credit']      · 实得 ['Credit']
✓ 正常导入不该误报                 期望 []              · 实得 []
✓ 内建名不该误报                   期望 []              · 实得 []
✓ 真实 vgmdb.py 拿掉 SourceError 后 -> ['SourceError']
```

### VGMdb 按展会页采集（原来一张都抓不到）

**现象**：跑 `M3-58`（= M3-2026秋，VGMdb `event/366`）结果是 **0 张**。

**原因**：VGMdb 源原来没有"按展会遍历"的能力，`list_by_event` 走的是基类实现——
拿展会的二十多个标签去**文本搜索**。而 VGMdb 的搜索是按专辑名/艺人匹配的，
搜 `M3-2026秋` 这种展会名当然一条都搜不到。

**修法**：VGMdb 有真正的**展会页** `/event/<id>`，里面就是专辑列表，还带分页
（实测 30 张/页、`Page 1 of 7`）。现在逐页取、去重、逐个抓详情。

配置里给出映射：

```toml
[sources.per_source.vgmdb]
event_ids = { "M3-2026秋" = 366, "M3-58" = 366 }
```

实测：

```
展会: M3-2026秋  event_ids={'M3-2026秋': '366', 'M3-58': '366'}
  页面标题 'M3-2026 Fall', 共 7 页
  [1] '[AM:28:00:01] Kairo Kidou / toumeikairo'  date=2026-10-25  tracks=5
  [2] '#MATERIALIZERS / Yooh, siromaru'          date=2026-10-25  tracks=10
  ...
  取到 5 张, 用时 17.2s
```

#### ⚠️ event id **不能靠规律猜**

VGMdb 的 event id 不是按时间递增的。我实测扫了 350–370：

```
353 = M3-1999 Spring     361 = Record Store Day 2026
354 = M3-1999 Fall       366 = M3-2026 Fall   ← 对的
```

我第一次把 `M3-2026春` 猜成 361，幸好验证了标题 —— 那是 Record Store Day。
所以**打开 VGMdb 展会页，从 URL 里取那个数字**，别推。

为了配错能被发现，第 1 页会把**页面标题**打进日志：

```
VGMdb 展会 M3-2026秋(event 366): 页面标题 'M3-2026 Fall', 共 7 页
```

看到 `'Record Store Day 2026'` 这种就是配错了。

没配映射的展会退回原来的标签搜索（不会报错，但基本搜不到）。

#### 事故复盘：粘贴 cURL 曾把 `config.toml` 写坏，整个程序起不来

**现场**：

```
用户把 PowerShell 版 cURL 粘进 cookie 框（^" 转义、^ 续行）
  → 解析器不认 PowerShell 转义 → 3510 字符多行原文被当成 cookie 值
  → save_source_options 未转义换行 → 多行被写成 TOML 字符串
  → config.toml 语法非法 → Config.load 抛 ConfigError
  → **整个程序无法启动**（不只是某个字段错，是全线不可用）
```

**三层修复**：

| 层 | 修复 |
|---|---|
| TOML 写入 | 换行/回车/制表转义（`\n` `\r` `\t`） |
| 解析器 | 认 PowerShell `^"` / `^` 续行、bash `\"` / `\` 续行 |
| 保存入口 | 先压成单行；cookie 框里粘的是 cURL 时自动解析出真正的 cookie 头 |

**回归守卫** `tests/test_config_write_safety.py`（15 个用例）不是只测"现在能跑"，而是
构成完整论证：

```
✓ 未转义的换行 → Config.load 抛 ConfigError   （证明事故原理成立）
✓ 转义后的换行 → 文件合法且值正确往返         （证明修复有效）
✓ 整段 PowerShell cURL 当值写入 → 文件仍可解析（真实事故场景）
✓ cookie 那行必须是单行                       （换行确实被转义了）
```

> **教训**：写配置文件前必须假设输入是**任意多行文本**。写错一个字段只是功能异常，
> 写坏配置文件是**整个程序起不来** —— 严重程度完全不同。

#### UA 不需要你手动更新了

`cf_clearance` 绑定申请它的浏览器 UA，但**用户未必知道自己的 UA**（浏览器自动升级、
拿不到、或懒得抄）。所以面板现在两种方式都不需要你操心 UA：

**① 粘贴请求头 / cURL（推荐）**

F12 → Network → 右键任意请求 → **Copy as cURL**，整段粘进面板：cookie 与
user-agent 会一起解析出来。`parse_request_headers()` 同时支持：

```
cookie: cf_clearance=xxx          # 请求头原文
user-agent: Mozilla/5.0 ...
```

```
curl 'https://vgmdb.net/event/366' -H 'cookie: cf_clearance=xxx' \
     -H 'user-agent: Mozilla/5.0 ...'      # bash / pwsh 两种 cURL 都认
```

也认 `-b 'cookie=...'` 形式。

**② 只给 cookie，UA 自动探测**

只填 cookie（UA 留空）也能用：保存时会拿一批常见 UA（Chrome/Edge/Firefox ×
Windows/macOS，共 11 个）逐个实探，**试出能过 Cloudflare 的那个**并写进配置。
`cf_clearance` 绑定 UA，所以"对的 UA"一定存在，只是需要找出来。

实测（当前 cookie 已过期，所以全部失败）：

```
耗时 10.5s · 尝试过的 UA 数: 8
ok=True  available=False  message=Cloudflare JS 挑战未通过
cookie_len=546  has_prefix=True
UA 未被覆盖: True        ← 没粘贴 UA 时不动配置里已有的 UA
```

前端会在成功时提示「cf_clearance 已保存，UA 已自动匹配」。

> 探针本身也修过：原来 `available()` 吃缓存（`cache_ttl=3600`），cookie 早失效还显示
> 「✅ 可用」。现在每次都实探。

### 在面板里直接更新 `cf_clearance`（VGMdb）

VGMdb 被 Cloudflare 的 JS 挑战挡着，纯 HTTP 客户端过不去，必须带上浏览器的
`cf_clearance` —— 而它会**过期**。以前只能改 `config.toml` 再重启面板，太重。

现在：**总览 → 数据源 → VGMdb 卡片 →「Cloudflare 凭据」**，填两个框，点「保存并测试」。

```
cookie      浏览器 F12 → Application → Cookies → 复制 cf_clearance 的值
User-Agent  同一个浏览器的 UA（必须一致，否则照样被拦）
```

保存后**立刻探测**并显示 `✅ 可用` / `⛔ 原因`。

#### 为什么"填了却不能用"

代码是把这个值**整串**当 `Cookie` 头发出去的：

```python
headers["Cookie"] = cookie
```

所以值必须是 `cf_clearance=xxxxx` 这种**完整 cookie 对**。只贴 `xxxxx` 就是非法
Cookie，Cloudflare 必然 403 —— 这不是 cookie 失效，是格式不对。

现在 `normalize_cookie()` 统一处理：裸 token 自动补前缀、`Cookie:` 前缀去掉、
cookie 名写错就纠正。实测：贴裸 token → 保存后 `available=True`。

#### 写回 config.toml 是逐行改的

`save_source_options()` 只改 `[sources.per_source.vgmdb]` 里对应键的那一行，
**注释、其它键、其它段全部原样保留**（用 `to_toml` 重新序列化会把注释全丢掉）。

### 歌单对照：改了专辑就同步

对照结果是**客户端模型**（`plState.result`）渲染出来的，不是一次性 HTML。所以：

| 操作 | 同步方式 |
|---|---|
| 标记为已添加 | 改模型里的 `marks` → 重渲染（导出用的 `lastPlAlbumIds` 一起更新） |
| 取消匹配 | 从 `db_only` 里摘掉（默认范围是「只看已匹配」，它已不属于这个列表） |
| 编辑信息 / 换封面 | `syncPlAlbum(key)` 重新拉这张专辑 → 替换模型里那条 → 重渲染 |
| 删除专辑 | 从 `db_only` 里摘掉 |

**不是重新对照整个歌单** —— 那要再拉一次歌单，慢，而且会把展开/折叠状态冲掉。
折叠状态按标题记住，重渲染后还原。

### 抽屉顶栏：从 sticky 改成布局的一部分

之前是 `position: sticky; top: 0`，滚动时**会盖住从下面滑过的正文**。
现在抽屉本身是纵向 flex：

```css
.drawer-inner { display:flex; flex-direction:column; padding:0; overflow:hidden }
.drawer-bar   { flex:0 0 auto }                    /* 头固定, 不参与滚动 */
#drawer-body  { flex:1 1 auto; overflow:auto }     /* 只有内容区滚动 */
```

顶栏占的是真实布局空间，所以**永远不会遮挡内容**。

### 专辑库：搜索 / 排序 / 编辑 的修正

#### 搜索原来只查了三个字段

SQL 是 `title / title_original / circle` —— 但输入框写着「搜索标题 / 社团 / 曲目」，
**曲目和艺人根本没查**；而且不做归一化，所以 `Something blue*` 只能靠库里恰好也带 `*` 才撞上。

现在覆盖 8 条路径：

```sql
标题 / 原名 / 社团 / 编号
归一化标题 / 归一化社团      -- norm_key(romaji=False): 全半角+标点+空白
艺人 / 曲目                  -- json_each 读 payload 里的数组
```

> 归一化**不能用 `romaji=True`**：`だってもっと眠っていたい` 会变成
> `dattemotto眠tteitai` 这种半截罗马字，反而搜不到原文。

#### 默认排序改成「最近更新」

`list_albums(order=)` / Web 接口 / 前端下拉三处默认值统一成 `updated`，
下拉文案也换成了人话（最近更新 / 发售日（新→旧）/ 社团 / 标题）。

#### 标签现在可以删了

编辑面板原来只有一个逗号分隔的输入框 —— 想删某个标签得手动找到那一段改掉，
很容易改坏，也看不出到底存了哪些。现在改成**胶囊**：每个标签带 ✕，回车/逗号添加，
退格删除最后一个。后端 `update_album_fields` 本来就支持替换/清空 `tags`，只是没接好 UI。

#### 其它几处别扭的地方

| 原来 | 现在 |
|---|---|
| 搜索必须按回车 | 输入即搜（300ms 防抖），Enter 立即搜 |
| 搜索框没有清空 | 右侧 ✕ 按钮；Esc 也能清空 |
| 筛选条件切页/刷新就丢 | 存 localStorage，回来还是原样 |
| 筛了半天想还原 | 工具栏「重置」按钮 |
| 详情抽屉很长，关掉要滚回顶部 | **顶部固定条**（标题 + 编辑 + ✕），Esc 也能关 |
| 保存一次就跳回顶部 | 重开抽屉时**保持滚动位置** |
| 每页张数写死 24 | 可选 24 / 48 / 96，并记住选择 |
| 不知道哪张刚改过 | 卡片显示「更新 3 分钟前」（既然默认按它排序） |
| 选中多张后一滚动就找不到操作条 | 选中时批量条**浮动在底部** |

> **改完要重启面板**：后端（`dnh/db.py`、`dnh/webui/server.py`）的改动不会热加载，
> 静态文件（JS/CSS/HTML）刷新页面即可。

### 发售日：拿不到就是「未知日期」，不拿展会日期顶替

**原来的坑**：`release_date` 优先取**展位记录的 `date`** —— 那是**展会当天**。
于是只要一张碟被挂进某个展会，它的发售日就自动变成展会日期。后果很直接：

```
一张 2006 年的老碟被挂进 M3-2026春
-> 发售日凭空变成 2026-04-26
-> "它到底是不是这次展会的"再也看不出来
```

**现在的规则**：发售日**只认 CD 自己记录的** `day_sale` / `sale_date` / `release_date`，
不读展位记录的 `date`。拿不到就是空。

空的时候**界面和报告都显式写「未知日期」**，而不是留白 —— 留白会让人以为"还没抓到"。

| 位置 | 显示 |
|---|---|
| 专辑卡片 | `未知日期 ｜ 目录号` |
| 详情抽屉 | `发售日: 未知日期` |
| 歌单对照卡片 | `社团 · 未知日期` |
| Markdown / HTML 报告 | `- **发售日**: 未知日期` |

> 库里**已有**的日期是在旧逻辑下写的，新旧无法区分（M3 的碟本来也确实在展会当天卖）。
> 想清理的话用 `dnh db prune`（按"发行日与所标展会不符"筛选），或配合网易云年份人工核对。

### 删除专辑（清理不该收进来的碟）

详情抽屉最下面有 **「删除这张专辑」**（危险操作，单独一块，二次确认）。

典型场景：dojin-music 上**发布日期未知**的条目，网易云显示它其实是**多年前的老碟**，
根本不在本展会发布。实测 `葉月ゆら` 的 `clover` / `Honey Bee` 就是这种 ——
挂在 M3-2026春 下、`release_date` 为空，网易云却是 2006 年。

```
POST /api/albums/delete  { "keys": ["<album key>"], "dry_run": false }
```

删掉会连同**匹配记录、标记**一起清（`delete_albums` 处理关联表）。
无 `keys` 也无 `filter` 时接口**拒绝执行**，避免误删全库。

> 注意：这是**彻底删除**，不是"取消匹配"。如果只是想让它不算已上线，用
> 上一节的「取消匹配」；只有确认这张碟根本不该在库里时才删除。

### 歌曲候选不能拿"歌名"去比"专辑名"（真误判）

实际碰到的一例：

```
本地 TUTORIAL（響叶's DTM，90 首）  ->  网易云《劲舞团游戏原声》score=1.000
```

那张 OST 的名字跟 TUTORIAL **毫无关系**，却拿到满分。为什么？

因为歌曲检索命中的候选，把 ``song.name``（那首叫 ``tutorial`` 的歌）当成了候选的"标题"，
去跟我们的**专辑标题**比 —— 这是**类别错误**：我们匹配的是「专辑 → 专辑」。

修法：**歌曲只当"指向专辑的线索"**，用**它所属专辑的名字**打分。

```python
name=song.album_name or song.name   # 原来是 song.name
```

顺带把同一张专辑的多首歌去重成一个候选（之前白算好几遍）。

#### 顺藤摸出的第二个 bug

曲目表那段的注释写着「**两边都**有成规模的曲目表却几乎不重合 → 打折 0.75」，
但代码只检查了我们这一侧：

```python
elif track_score <= 0.1 and len(album.tracks) >= 3:   # 没检查候选侧有没有曲目表
    score *= 0.75
```

结果**所有拉不到曲目表的候选都被无缘无故打 0.75**。现在加了 `cand.track_known`，
确认候选侧真的拿到了曲目表才打折。

#### 效果（12 张样本回归）

```
不变 10 · 改判 0 · 失去 2
```

- 失去的 `TUTORIAL` 就是那个**假阳性**，本来就该掉
- 失去的 `余白` 经连测 3 次确认是**网易云搜索结果顺序抖动**（第 3 次又匹配上，score 1.000），
  与本次改动无关，库里的匹配记录也没被动过

`余白` 那次抖动也顺手修了：浅层（深度 1）原来只取搜索结果的**前 10 条**，而深层取 15 条 ——
排在第 11~15 位的正确条目浅层看不见，顺序一抖就"时好时坏"。现在两边统一用 15 条
（`_ALBUM_SEARCH_LIMIT`），多问 5 条结果的代价可以忽略。

### 歌单对照：取消匹配 + 点卡片跳网易云

自动匹配一定有误判（同名不同碟、合辑、搬运）。网易云写操作已经去掉，用户能做的补救
就是从匹配里摘掉它 —— 而歌单对照界面正是"我以为能加进歌单、结果匹配错了"的地方。

**卡片整张可点**：已匹配的卡片是外链，点一下直接打开
`https://music.163.com/album?id=<id>` 去核对；没匹配的卡片点了打开本地详情。
信息行会带一个 `网易云` 小标签（含匹配分）。

**「取消匹配」按钮**出现在已匹配的卡片上：

```
POST /api/albums/unmatch  { "keys": ["<album key>"] }
-> { "ok": true, "removed": 1 }
```

点完这张卡片直接从「只看已匹配」列表里消失（因为默认范围就是已匹配），之后可以重跑匹配。

命令行也能用：

```bash
python -m dnh db unmatch "<album key>"   # 可一次给多个 key
```

> 取消匹配只是删掉 `netease_matches` 里那条记录，**不动专辑本身的数据**，
> 所以随时可以重新匹配回来。

### 歌单对照：默认只看「已匹配网易云」的专辑

没在网易云上线的碟**根本加不进歌单**，把它们列进「本地有、歌单未加入」只是噪音。
所以对照的专辑范围默认就是 `matched`：

| 范围 | 含义 |
|---|---|
| `matched`（**默认**） | 只对照已匹配网易云的专辑 |
| `unmatched` | 只看没匹配上的（查漏用） |
| `all` | 全部 |

面板上是一个下拉框（默认「只看已匹配网易云」），状态行会显示这次用的范围：

```
歌单 62 首 · 命中 41 张专辑 · 范围: 已匹配网易云，已标记 41 张为「已加入歌单」
```

```bash
python -m dnh playlist compare --url <歌单链接> --match-filter matched   # 默认
python -m dnh playlist compare --url <歌单链接> --match-filter all       # 想看全部
```

过滤逻辑抽成了 `playlist.filter_by_match()`（纯函数，CLI 与 Web 共用），
记录匹配状态查询失败时**当作未匹配**而不是崩掉。

实测当前库：`matched` 411 张 / `unmatched` 1349 张 / `all` 1760 张 —— 噪音少了一大截。

### 匹配任务：无上限 + 能真的停下

```bash
python -m dnh match --event M3-2026春 --limit 0     # 0 = 无上限, Ctrl+C 停
```

面板上的「匹配」快捷动作现在也发 `limit: 0` —— 一直跑到你点停止。

#### 「停止」按钮原来根本没生效

点了停止，任务照跑到结束。原因在 `Pipeline._emit`：

```python
try:
    self.on_event(kind, **data)
except Exception:      # 进度回调不应影响主流程
    pass               # <- 把用来中止的 JobCancelled 也一起吞了
```

取消是通过进度回调抛异常实现的，而这个 `except Exception` 把它吃掉了 ——
所以停止标志只是被设上，**没有代码去理会它**。

修法：让取消穿透，其它回调异常照旧吞掉。

```python
except Cancelled:
    raise
except Exception:
    pass
```

`dnh/errors.py` 新增 `Cancelled`，`webui` 的 `JobCancelled` 继承它 ——
pipeline 不需要 import webui 就能识别取消。

> 这一处修的是**所有**后台任务（采集/富化/匹配/报告），不只匹配。
> 实测：启动任务 → `stop()` → 状态变 `cancelled`，日志写「收到停止请求，正在收尾」→「任务已被用户停止」。

### 匹配成功后的回填 + 封面画质比对（新增）

匹配上不只记一条匹配记录 —— 还会**顺手把库里的空缺补上**，并处理封面。

```toml
match.backfill_from_match = true    # 用网易云条目补空缺字段
match.cover_prefer_netease = true   # 封面不同以网易云为准
```

**补什么**（只补空的，不覆盖已有值）：`release_date`（取 `publishTime`）、
`circle` / `artists`、`tracks`（连时长）。

**封面**：两边 URL 不同就以网易云为准；但库里那张**分辨率明显更高**时保留本地那张 ——
白扔一张高清封面没有意义。画质靠 `dnh/imageutil.py` 直接读文件头（纯 stdlib，不解码像素，
支持 JPEG/PNG/GIF/WebP/BMP）。

实测（真实数据）：

```
THE ARCHIVE OF NAYUTA  无封面 -> 补上网易云
時巡るLandscape        bgm 1193x1194 -> 换成网易云 1600x1600
BVR EP VOL.4           曲目 0 -> 补齐 8 首 + 封面
```

#### 这里也踩了两个坑

**① `upsert_album` 换不掉封面**。它是 trust 感知的合并，「值 → 另一个值」会保留旧的，
所以封面替换必须走 `set_cover_url()` 直接 UPDATE（列 + payload 都要写）。

**② `set_cover_url` 会清掉 `cover_local`**。第一版没判断"本地是否已经下载过封面"，
结果把落盘的高清封面引用直接抹掉了（文件还在磁盘上，但库里指不到了）。
现在：**本地有文件时先比分辨率**，读不到本地尺寸就保守地不动。

> **不要做"按文件名自动还原本地封面"这种补救**（我试过，已移除）：它无法区分
> "意外丢的引用"和"用户检查后主动删掉的错误封面"，实测把人工清洗掉的 33 张又挂回去了。
> 人工清洗的结果必须被尊重 —— 宁可少一张封面，也不要复活一张错的。

### 通贩页面也算封面来源（新增）

社团挂的通贩商品页上**一定**有商品图 —— 那是很可靠的封面来源。

```bash
python -m dnh covers --from-shop --event M3-2026春
```

```toml
shop_cover_budget = 200     # 每轮最多抓多少个通贩页
shop_cover_delay = 1.0      # 页间等待(秒)
shop_cover_pages = 2        # 每张碟最多试几个通贩页面
```

解析顺序：`og:image` → 商品图 CDN(`booth.pximg.net` 之类)→ 声明尺寸够大的 `<img>`。
购物车/星标/横幅/店铺头像这类平台 UI 一律排除。

#### 但「店铺首页」不是商品页（这是关键）

实测手里 189 条 BOOTH 链接：

```
129 条  https://49uamusic.booth.pm/          <- 店铺首页，不能用
 60 条  https://abcas.booth.pm/items/8165634 <- 真商品页，可用
```

店铺首页的 `og:image` 是**店铺横幅**，不是这张碟的封面 —— 跟之前「社团主页冒充特设页」
是同一类错误。所以 `shopcover.is_product_page()` 会按平台规则过滤：

| 平台 | 只认 |
|---|---|
| BOOTH | `/items/<数字>` |
| Melonbooks | `detail.php?product_id=` |
| Amazon | `/dp/` 或 `/gp/product/` |
| Diverse Direct | `/diverse-system/dvsp-0210/` 这类「分类/编号」路径 |
| TANO*C STORE | `/shopdetail/<数字>` |
| OmniColor | 商品/店铺路径 |
| 其他域名 | 路径非空即认 |

过滤后 M3-2026春 还能补 **30 张**（BOOTH 23 / Melonbooks 7 / 其他 1）。

### 封面必须是「封面」：比例与尺寸判定（新增）

采集回来的"封面"经常根本不是封面 —— 店铺页头横幅、平台 logo、艺人头像、
商品页截图。它们的共同特征是**比例不对**：

| 真实封面的比例 | 例子 |
|---|---|
| **1:1**（主流流媒体标准） | Apple Music / Spotify / 网易云 的专辑图 |
| **≈0.73**（CD 书型） | Diverse System 的 AD:TRANCE 系列，实测 470×641 ≈ 0.733 |

所以下载完封面会按文件头读尺寸，白名单之外的**直接删文件、不写进库**
（`cover_url` 一起清掉，避免下一轮反复重试同一张错图）。判据与实现见
`dnh/coverimage.py`，不依赖 Pillow，直接解析 JPEG / PNG / GIF / WebP / BMP 头。

```toml
[sources]
cover_aspect_bands = [[0.95, 1.05], [0.70, 0.78]]   # w/h 白名单
cover_min_side = 300                                # 再小就是图标/缩略图
cover_denied_sources = ["dojinmusic"]               # 哪些源不提供封面
```

**封面来源限定**（`cover_denied_sources`）：

- ✅ 其它采集数据源（bgm.tv / DoujinStyle / VGMdb / dizzylab / Discogs）自带封面；
- ✅ 通贩商品页（BOOTH / Melonbooks / 虎穴 / Diverse Direct / TANO*C STORE / OmniColor）；
- ✅ **明确挂着这张碟的特设页**（dojin-music 的 `link_url` 特设页）；
- ❌ dojin-music 的站内 `jacket` —— 实测常是社团主页图/占位图，默认不再当封面。

### 加载不出的封面（排查与清理）

"封面显示不出来"线上实测就三类：

| 类型 | 例子 |
|---|---|
| 本地文件不见了 | `cover_local` 指向的图被删 / 换了机器 |
| 链接失效 | 404（netlify 的 `og-image.jpg`）、403（Google Sites 防盗链） |
| **200 + HTML 反爬页** | Tumblr 的拦截页 —— 浏览器当图片解析必然失败 |

第三类最坑：`/api/cover` 以前只看状态码，把 HTML 当图片**写进了缓存**，那张图就永远坏了。
现在代理会按文件头校验响应是不是图片（不信任 `Content-Type`），不是就不缓存、直接 502；
磁盘缓存里的历史脏数据也会在读取时被丢弃。前端加载失败会退化成「无封面」占位块。
抓失败的 URL 会进 15 分钟负缓存，避免每开一页都对同一条死链等 8 秒。

另外两处兜底：

* `cover_local` 指向的文件不存在时，接口自动回退到 `cover_url`（不会给浏览器一个必然 404 的地址）；
* 因此"本地丢了但远程还在"不算加载不出 —— `covers --broken --clear` 只清那条失效的本地路径，
  保留还能用的远程地址。

```bash
python -m dnh covers --broken              # 列出加载不出的封面(默认最多联网检查 80 张)
python -m dnh covers --broken --budget 300 # 多查一些
python -m dnh covers --broken --clear      # 清掉它们, 之后可重新补
```

顺带修掉一个编码问题：URL 里带日文文件名（`.../CDケース.png`）时 `urllib` 会抛
`UnicodeEncodeError`，现在发起请求前统一做百分号编码。

> 这类「字段名像商品页、实际是店铺首页」的问题**已经是第三次**了
> （社团主页 vs 特设页、CD link_url 里的 X/YouTube、现在这个）。
> 结论：**任何"链接字段"都必须先验形状，再抓内容**。

### 封面的正确来源（按优先级）

封面是这个项目里**最容易抓错**的字段。经过几轮实测，来源优先级定为：

| 顺序 | 来源 | 说明 |
|---|---|---|
| 0 | **用户上传** | 最可靠。Web 面板详情页有「上传封面」按钮 |
| 1 | `bgm.tv` / `doujinstyle` / `VGMdb` 条目页 | 条目自带封面 |
| 2 | **已匹配的网易云条目** | `album.picUrl`，用现成匹配结果，零额外成本 |
| 3 | ~~dojin-music 的 `jacket`~~ | 只在 1085 张里有 60 张，聊胜于无 |
| ❌ | ~~从特设页/社团主页抓~~ | **已默认关闭**，见下方修正 |

```bash
# 用已匹配的网易云条目补封面(实测 M3-2026春 补到 10 张)
python -m dnh covers --from-netease --event M3-2026春
```

网易云补封面只处理 `cover_url` 与 `cover_local` **都为空**的行，不会覆盖已有封面。

### 用户上传封面（Web 端）

详情页有「上传封面」按钮，走 `POST /api/albums/cover`：

```json
{ "key": "<album key>", "data_url": "data:image/jpeg;base64,..." }
```

后端会校验**文件头魔数**（JPEG/PNG/GIF/WebP）、限制 12MB，
存到 `data/covers/upload/<slug>.<ext>`，写进 `cover_local` 并**清掉 `cover_url`**
（用户上传的优先，不该再被自动抓取覆盖）。

> 又一个 `upsert_album` 的坑：它**不会用空值覆盖已有值**，所以清 `cover_url` 必须走
> 直接的 UPDATE（`set_uploaded_cover` / `clear_cover`），否则旧的错误封面会一直挂着。

### 封面清理命令

```bash
python -m dnh covers                      # 查「多张专辑共用同一张封面」
python -m dnh covers --suspect            # 查「封面与社团主页同域且文件名很泛」
python -m dnh covers --clear              # 清掉共用封面
python -m dnh covers --suspect --clear    # 清掉可疑封面
python -m dnh covers --from-netease       # 从网易云补封面
```

**`--suspect` 的判据是收窄过的**：光"同域"不够 —— `fsx_diversion_kari_800.jpg`
这种同域但文件名带专辑名的往往就是真封面。所以还要求文件名**很泛**
（`og` / `ogp` / `og-image` / `logo` / `thumbnail` / `eyecatch` / `header` …）。
收紧后从 179 张降到 55 张，误判明显减少。

### VGMdb 接入（已打通，但 cookie 会过期）

`cf_clearance` 只要**出口 IP 与 UA 都对**就能过。实测结论：

* **有效的是较新的那个 cookie**，旧的已失效
* 浏览器与爬虫必须走**同一个代理**（出口 IP 一致），直连会回挑战页
* UA 必须**逐字一致**（差一个字符就 403）

解析上有两个坑，都是看过真实 HTML 才发现的：

| 现象 | 真实原因 |
|---|---|
| 封面是 `button-add-big.png` | `#coverart` 里**没有 `<img>`**，封面是 CSS `background-image` |
| 曲目标题永远是空/占位 | 标题是 `<td colspan="2">` 里的**裸文本**，不在 `span` 里 |

修好后实测：专辑 5000 → 27/27 首带标题，多碟（20000）碟号也正确。

> **cookie 几小时到一天就过期**，过期后 `available()` 返回 False 并提示重新配置。
> 所以 VGMdb 是「手动更新 cookie 后跑一轮」的源，不适合无人值守。

### ⚠️ 重要修正：`special` 其实是**社团主页**，不是专辑特设页

这一条是很难看出来的错，而且影响很大：

> doujin-music 里**紧跟在社团名后面的第一个链接**是**社团官方页面**，
> 不是这张碟的特设页。只有带「**新譜情報**」字样的条目才可能有真正的特设页。

原来的代码把社团的 `link_url` 直接写进了 `urls["special"]`，于是：

* 报告里的「特设页」链接大**部分是社团主页**
* 从那里抓来的"封面"**是社团主页图**，不是这张碟的封面

修正：

* 社团主页改记为 `urls["circle_site"]`，**不再占用 `special`**
* 真正的特设页只来自 **CD 记录自己**的 `link_url`（且需「新譜情報」）
* **`cover_from_special` 默认改为 `false`** —— 实测这是低效率且易错的做法

实测 M3-2026春：585 张有封面里，**170 张的封面与"社团主页"同域名** —— 高度可疑。

**封面的正确来源优先级**应该是：

1. `bgm.tv`、`VGMdb`、`doujinstyle` —— 条目页自带的封面
2. 已匹配的**网易云**条目
3. 都没有 -> **Web 端由用户上传**

### 共用封面检测：`dnh covers`

抓封面时最容易误抓的不是"没找到"，而是**抓到了社团主页图**。特征很好认：
**同一张图被同一个社团的多张专辑共用**。

```bash
python -m dnh covers [--event E] [--min-count 2] [--clear]
```

纯本地、不联网。实测在 M3-2026春 上查出 **18 组、45 张**，而且
**每一组都在同一个社团内** —— 这就是"抓了社团主页图"的铁证：

```
[1] 8 张共用: https://roundexrecord.com/.../Cover_small-300x300.jpg
      ROUNDEX RECORD  Beat Masher / Fraky / Fusillade / ...   (8 张全同)
[4] 3 张共用: https://yt3.googleusercontent.com/...=s900-c-k-...   ← YouTube 头像
      Readiness Record  Blossoms / Colors / Summer
```

YouTube 那种 `yt3.googleusercontent.com/...=s900-c-k-c0x00ffffff-no-rj` 就是**频道头像**。

`--clear` 会把这些封面清掉（下次 enrich 重新找）。已经清过一轮 45 张。

> **注意这是个"清完还会再回来"的问题**：enrich 重新抓时仍可能抓到同一张主页图。
> 真正的预防是在写入前判断"这个 cover 是不是同社团多张碟共用的" —— 目前只做了检测+清理，
> 预防还没做。

### 曲目表缺了怎么办：去特设页找

dojin-music 的 `songList` 与 bgm.tv 的曲目表**都缺**的情况不少（M3-2026春 实测
**814 / 1085** 张）。这时最后一招是去社团自己的**特设页**找 —— 很多"特設ページ"
上会排一行行 `01. 曲名`：

```toml
[sources.per_source.dojinmusic]
enrich_media = true
tracks_from_special = true   # 缺曲目表时, 从特设页的「01. 曲名」抽
```

HTML 结构千差万别，所以只看**「行首编号 + 文本」**这个共同点，并且**宁缺毋滥**：

* 至少 **3 条**编号（少于 3 条多半是页面上的别的列表）
* 编号大致递增（允许跳号），否则不是曲目表
* 过滤掉导航/版权/价格之类的行（`ホーム` / `お問い合わせ` / `カート` / `円` …）

行里若写成 `曲名 / 艺人`，后半段会写进 **`Track.artists`** ——
dojin-music 的 `songList` 本身也带逐曲的 vocal / word / music / arrange / mix，
这些都会进库，报告和面板的曲目表会**多出一列「艺人」**。

### 社团名对不上怎么办

展位表里的社团名与社团库里的写法常有差别（多余空格、`（新刊あり）` 这类括号注记、
全角、`A / B` 联合社团）。`_circle_name_variants()` 从"最像原名"到"最宽松"逐个变体试，
命中即止 —— 比放弃整个社团划算。

### 详情页并发抓取

CD 详情页只有 ~13KB，但**实测每次要 2~14 秒** —— 那几乎全是**网络等待**，不是站点在忙。
串行抓 1000 张要几个小时，所以详情页是并发的：

```toml
[sources.per_source.dojinmusic]
detail_workers = 4      # 1 = 串行
```

**并发不会给站点更大压力**：HTTP 层的 `_throttle` 用锁保证了
**同一 host 的最小请求间隔**（`[http] rate_limit`，默认 1 秒），
所以并发只是把网络等待重叠起来，请求**发起速率**不变。

实测（真实页面，6 张）：

| | 用时 |
|---|---|
| 串行 | 42.2s |
| 并发（workers=6） | **17.4s** |
| 结果 | **完全一致**（顺序、标题、社团、展示场、展位、曲目、链接） |

保持**提交顺序**产出，是为了让 `discover` 的日志与增量落库依然可读、可续跑。

> 为什么默认只给 4：配合同域 1 秒间隔，`4` 已经足够把网络等待重叠掉；
> 再大也不会更快（节流是硬上限），只会给这个小站点更多并发连接，没必要。

**社团枚举也用同一套并发**（社团之间并发，社团内部两次请求仍有依赖所以串行）。
真实网络实测（12 个社团、强制不吃缓存）：串行 34.1s → 并发(6) **21.5s**，**1.58×**，
结果与顺序完全一致。整轮采集里这一段的实际表现是 **2.84s/社团 → 1.57s/社团（约 1.8×）**。

> 社团不命中的比例不低（样本里 3/12）：那些社团**在 dojin-music 站上根本没有社团记录**
> （只有登记过 CD 的社团才有）。注意**不能因此放宽名称匹配** —— 实测
> `Capsule Instruments` 查不到，而前缀 `Capsule` 会命中**另一个社团** `CAPSULE MUSIC`，
> 放宽只会张冠李戴。

---

## 网易云接入（只读）

三种传输方式：

* `builtin`（默认）—— 自带 `weapi` 加密客户端：`AES-128-CBC` 双层加密 + RSA，
  AES 实现通过 **FIPS-197 官方向量**校验（见 `tests/test_aes_crypto.py`）。
* `api` —— 对接本机 NeteaseCloudMusicApi。
* `auto`（默认）—— 探测 `api_base` 可用则用 api，否则回落 builtin。

用到的接口只有：搜索（`/search/get/web`，**注意 `/cloudsearch/get/web` 已被网易云下线**）、
歌曲详情、专辑详情（含曲目表）、**艺人搜索（type=100）与艺人专辑列表**、
歌单详情与曲目（只读）。

---

## 匹配算法

**四个维度一起看**：专辑名 / 社团(艺人) / 发行年份 / 曲目表。
下面每一条都是被实测的假阳性或漏匹配逼出来的。

### 1. 按艺人专辑匹配（主路径）

拿**专辑标题**去网易云全站搜索会被大量无关的同名歌曲淹没 —— 实测
`Deeper, Deeper` / `Meridia` / `Blue Connect` / `Still In Progress` 这类常见标题
都能搜到**别人**的歌，而且标题分是满分 1.00，直接把总分抬过阈值。

所以改成**先认人、再在他的作品列表里找**：

```
社团/艺人名  →  /search/get/web (type=100) 找艺人
            →  按名字相似度认准(artist_min_similarity) 取 artist id
            →  /artist/albums/<id> 拉他名下的全部专辑
            →  在这些专辑里匹配标题
```

同人社团的作品列表是**有限且专属**的，所以这条路径既精确又便宜
（一次艺人检索 + 一次专辑列表就覆盖该社团全部作品，且按 artist id 缓存复用）。

实测效果：`StargazerContemporary Vol.2` 就是靠这条路径命中的（0.897），
而关键词路径给出的最佳候选是**错误的** `Stargazer Contemporary Vol.1`（0.877）。

**认人宁缺毋滥**：搜 `ちょこっとだけ` 时网易云**第一位是声优竹達彩奈**，
真正的社团排在第二 —— 所以按名字相似度取最优，而不是取第一条。
相似度不够就放弃这条路径（认错人比找不到更糟）。

### 2. 合集 / Various Artists 处理

网易云会把**艺人很多的合集**署名成 `Various Artists` / `V.A.` / `群星` /
`オムニバス` / `コンピレーション`。这类条目的 `artist` 跟社团名**永远对不上**，
以前会被艺人地板直接否决 —— 白扔掉一批本来能命中的同人 compi 专辑。

现在：

* **候选被署名为 V.A.** → 艺人维度没有意义：艺人权重归零并让给标题与曲目表，
  且**不适用**艺人地板
* **反过来**（我们这张是合集、对方是单一艺人）**不放行** —— 那往往正是
  「同名不同碟」的信号，艺人地板照常生效
* 我们这张是合集时，艺人分只用 `circle` 去比，不拿 9 个协作者名字去撞对方任何一个
  （那样很容易碰巧撞上，属于假证据）

### 3. 发行年份对照

2026 年的同人新作不可能对应网易云上一张 2011 年的碟。

* 两边都有年份且**差 ≥ 2 年 → 直接判负**（很便宜、很准的过滤）
* **同年 → +0.05** 加分
* 无法比较时不因此否决

日期来源：本地用 `release_date`，网易云用专辑的 `publishTime`（毫秒时间戳）。

### 4. 曲目表是最强的确认信号

* 重合度 ≥ 0.6 → 大幅加分
* 两边都有成规模的曲目表却几乎不重合（≤ 0.1 且本地 ≥ 3 首）→ 打折，
  因为那很像「同名不同碟」
* 曲目表只对最有希望的前 12 个候选计算（代价最高）；**艺人专辑路径与 V.A. 候选优先**
  —— V.A. 的标题与艺人分可能都不高，但曲目表往往能一击命中

### 4.5 分深浅匹配（深度 1 快速通道）

匹配不是一上来就把所有字段算一遍，而是**由浅入深**：

| 深度 | 比什么 | 代价 |
|---|---|---|
| **1** | **标题 + 发行年份** | 1~2 个请求（只做专辑检索） |
| 2 | + 艺人检索 / 艺人作品列表 | 多 2~3 个请求 |
| 3 | + 曲目表（拉专辑曲目详情） | 再多 1~4 个请求 |

深度 1 命中（标题 ≥ `fast_accept_title` **且** 发行年份一致）就**直接收工**，
不碰艺人检索也不拉曲目表 —— 每张专辑能省 **5~9 个请求**，这是整轮匹配里最值得省的部分。

```toml
fast_accept = true          # 深度 1 快速通道
fast_accept_title = 0.97    # 标题要多接近才算命中
```

> **艺人地板照旧生效**。要跳过的是**网络**环节，而艺人相似度是纯字符串比较、不花钱。
> 我第一版没加这道闸，测试立刻抓出一条假阳性回归 —— 为了快把之前辛苦清掉的
> 误判放回来是不划算的。
>
> 另：合集署名的候选（Various Artists）也一律交给深层，因为艺人维度对它无意义。

### 4.6 负缓存必须有 TTL

确认「网易云没有这个艺人/社团」之后，同一个社团后面的十几张专辑就不必再问一次
艺人检索 —— 匹配失败占多数时（实测库里 96% 的专辑最后匹配不上），这部分是主要开销。

但**不能永久记**：网易云的艺人/社团条目是**用户随时创建**的，
今天查不到的人，几个月后可能就有了条目并传了资源。永久缓存会让我们
**永久错过**后来才出现的条目。所以负缓存是**带时效的**：

```toml
negative_ttl_days = 14      # 过期后重新问一次; 0 = 不用负缓存
```

实现上分两层，别混在一起：

| 层 | 作用范围 | 失效方式 |
|---|---|---|
| 内存 `_artist_id_cache` | 同一轮匹配 | 进程结束 |
| 落库 `netease_misses` | 跨轮次 / 跨进程 | **TTL 到期** |

查到之后会**删掉**对应负记录（`clear_miss`），不必白等 14 天。
`prune_misses()` 清过期行。

### 4.7 别为注定失败的候选花请求（实测优化）

匹配的时间几乎全在网络上。**先量再改** —— `StageProfile` 常驻在 matcher 里
（开销可忽略），按阶段统计调用次数与耗时。20 张未匹配专辑的实测：

| 阶段 | 次数 | 总秒 | 占比 |
|---|---|---|---|
| `search_albums` | 88 | 303.0 | **45%** |
| `search_songs` | 51 | 181.4 | **27%** |
| `album_songs` | 88 | 105.5 | 16% |
| `search_artists` | 16 | 57.1 | **9%** |
| `artist_albums_of` | 16 | 19.3 | 3% |
| 合计 | 259 | 666.3 | 每张 12.9 次 / 33.3 秒 |

> 这张表推翻了我之前两个凭直觉的判断：**艺人检索只占 9%**（所以负缓存不是
> 主要收益），真正的大头是专辑检索（45%）。

据此做的两处优化，都实测过：

**1. 同一个关键词不搜两次。** 浅层（深度 1）搜过的关键词，深层原本又搜一遍 —— 纯白花。
加 `_search_albums()` 缓存后共用。缓存键**不含 limit**，否则浅层用 10、深层用 15
会各自查一次，缓存永不命中。

**2. 不为注定失败的候选拉曲目表。** 标题+艺人分低到「曲目表满分也过不了 `min_score`」
的候选直接跳过（`_TRACK_MAX_GAIN = 0.28` + `_YEAR_BONUS = 0.05` 是上限估算），
再加 `track_probe_limit`（默认 8）兜底。

**3. 只搜前两个关键词。** 这一条也是量出来的：在 24 张未匹配专辑上统计
「命中靠第几个关键词」，结果是 `{1: 5, 2: 4}` —— **前两个关键词解决了 100% 的命中**，
第 3~6 个一个都没命中过。而 `keywords()` 平均生成 3.9 个关键词，
等于**一半的检索请求花在了从没带来过结果的关键词上**。

```toml
keyword_search_limit = 2    # 想要更高召回(更慢)就调大
```

| | 原始 | +检索缓存 | +曲目探测 | +关键词收敛 |
|---|---|---|---|---|
| `album_songs` 次数 | 88 | 92 | 21 | **20** |
| `search_albums` | 88 | 52 | 52 | **39** |
| 总调用 | 259 | 228 | 157 | **129** |
| 总秒数 | 666.3 | 545.3 | 477.0 | **378.7** |
| 每张耗时 | 33.3s | 27.3s | 23.8s | **19.0s** |

**累计 −50% 调用、−43% 耗时**，样本里命中数不变（0 条 —— 这 20 张确实不在网易云）。

优化后的耗时结构：

| 阶段 | 次数 | 占比 |
|---|---|---|
| `search_albums` | 39 | 37% |
| `search_songs` | 38 | 37% |
| `search_artists` | 16 | 15% |
| `album_songs` | 20 | 6% |
| `artist_albums_of` | 16 | 5% |

**歌曲侧也量过了**（同样 24 张样本）：`{1: 7, 2: 2}` —— **前两个关键词同样解决 100%**。
而且 `keyword_search_limit` 是**卡在整个循环上**的，歌曲和专辑一起被它管住，
所以那次收敛对两边同时生效，不需要再单独加一道。

### 4.8 艺人优先：量了，但**收益接近 0**（如实记录）

思路：让艺人专辑路径先跑，命中就跳过关键词检索。用 **已匹配** 专辑实测（未匹配的没有意义，
那是循环论证）：

```
艺人路径已达到 min_score: 10 / 15
  其中关键词无增益: 10      关键词更优: 0
  => 可安全短路: 10/10 (100%)
```

看起来很美，但**接进真实流程后调用数一点没降（46 → 46）**。原因很关键：

> `_fast_accept`（深度 1）在 `match()` 最前面就拦住了这批"好匹配"的专辑 ——
> 它们根本走不到艺人路径。而真正走到深层的那 6 张里，艺人路径**一个候选都没产出**，
> 自然也无从短路。

所以结论是：**艺人优先实现了、结果完全一致（15/15 命中数相同）、但实测收益≈0**，
因为深度 1 已经把它能省的那部分提前省掉了。默认保留（对绕过浅层的场景无副作用），
但**不要指望它提速**。

### 4.9 负缓存的坑：模棱两可的结果不能缓存

实测发现负缓存里有 `gno4166×高城みよ` 这种条目，而 `Gno4166` 在网易云**是存在的** ——
说明"搜到了相似度尚可但没到阈值"被当成了"确定没有"，一次质量差的响应就能把
本来存在的艺人**压住整个 TTL**。

修正：只有最佳相似度 **< 0.5** 才算"确定没有"，才写负缓存；
0.5 ~ `artist_min_similarity` 之间属于**模棱两可，不缓存，下次再试**。
同时把已按旧规则产生的 15 条记录清掉了。

> 到这里，"关键词收敛"这条线**已经到顶**：再减到 1 个就会丢掉第 2 个关键词那部分命中
> （专辑 4/9、歌曲 2/9）。要再提速只能换轴 —— 比如让**艺人专辑路径先跑**，
> 命中了就跳过关键词检索。

### 4.8 片假名 ↔ 罗马字


**这条本来就已经通了**（实测，不是新加的）：`keywords()` 用 `title_variants()`
（内含罗马字与纯 ASCII 变体）去检索，而打分时 `combined_score` 两侧都做
`norm_key(romaji=True)` 归一，所以：

| 本地标题 | 网易云标题 | 相似度 |
|---|---|---|
| モノマニア | Monomania | **1.00** |
| モノマニア | MONOMANIA | **1.00** |
| モノマニア | Ｍｏｎｏｍａｎｉａ（全角） | **1.00** |
| モノマニア | Mono Mania（分词不同） | 0.95 |

**真正转不动的两种**，需要字典，stdib 做不到：

* **汉字**：`惨美なるブラッディドール` vs `Zanbi naru Bloody Doll` → 0.35
* **外来语意译**：`サンシャインガール` vs `Sunshine Girl` → 0.62
  （片假名按音译转出来是 `sanshaingaru`，不是 `sunshine`）

### 5. 艺人地板 `min_artist_score`

候选两边都有艺人/社团、但怎么都对不上时直接判负。实测这一条就把
7 条匹配里的 5 条假阳性一次清掉（真命中的两张艺人分是 0.80 / 1.00，
假阳性的五张是 0.38~0.55）。只在**非合集**场景适用（见第 2 条）。

### 6. 过期匹配清理

调严阈值或换算法后，**旧的高分假阳性不会自己消失** —— 会一直留在库里（面板按它筛
「已匹配/未匹配」时就会带偏）。现在匹配失败（无候选或低于阈值）时会主动删掉该专辑的历史匹配记录。

> 匹配结果**只存在库里**：面板上能看，**报告里不显示**（按用户要求）。

### 配置

```toml
[match]
min_score = 0.72            # 低于此分不算匹配
auto_score = 0.88           # 高置信度(仅用于内部分档)
min_artist_score = 0.6      # 艺人地板（合集除外）
use_artist_search = true    # 按艺人专辑匹配
artist_search_limit = 10
artist_album_limit = 50
artist_min_similarity = 0.82  # 认不准就不认
title_weight = 0.65
artist_weight = 0.25
track_weight = 0.10
```

匹配结果里带 `method`（`keyword` / `artist_album`）与每个候选的
`pub_year` / `va` / `note`，能看出是哪条路径、年份对不对上：

```bash
dnh match --event M3-2026秋 --force
```

---

## Web 面板

`dnh serve --open` → `http://127.0.0.1:8787`

**5 个标签页**（按反馈精简过，能并的都并了）：

| 标签 | 内容 |
|---|---|
| **总览** | 统计卡片、快速操作（采集/补全/匹配/报告）、近期与已入库展会、**展会 & 标签**、底部**数据源** |
| **专辑库** | 固定尺寸卡片 + **分页**（每页 24 张）、多维筛选（含**有无封面**）、多选批量操作、手动新增、**详情抽屉（编辑信息 / 链接编辑 / 封面上传与删除）** |
| **歌单对照** | 粘贴网易云歌单链接 → 反查差异 → 一键打标记 |
| **任务** | 实时**进度计数**（新增/更新/无变化/剔除 + 当前阶段）、分步日志、**可随时停止**、2.5 秒自动刷新 |
| **报告** | 生成并列出报告文件 |

合并说明（原来各自是独立标签页）：

* **展会 & 标签** → 并进总览（一个列表，点标签即复制）
* **数据源** → 并进总览底部（「可用」是真去探一次的结果，不是硬编码）
* **试听 & 链接** → 并进**专辑库 → 专辑详情抽屉**

### 专辑库：等高卡片 + 分页

卡片做成 **同一行内等高、但不裁内容**：

* `grid-auto-rows: 1fr` + `align-items: stretch` → 行高由该行里内容最多的那张决定，
  同一行每张卡尺寸完全一致（整齐），**但不会为了对齐而砍掉内容**
* **没有任何 `line-clamp` / `max-height`**：标题、meta、标签全部完整显示
* 标签**不再截断** —— 以前写死 `slice(0,5)`，而实测有专辑带 **11 个标签**，
  用户看到的就是「标签显示不全」
* 艺人显示前 4 位 + 「等 N 位」，鼠标悬停有完整名单（最多 13 位，全列会把卡片撑爆，
  用「等 N 位」既保留信息又不失控）
* 封面 80×80，列宽下限 340px，卡片 `min-height: 300px` 兜底
* **版面分两段**：上半是封面 + 标题 + meta（社团 / 展位 / 发售日 / 曲目数 / 艺人 / **数据源**），
  下半（`.bottom`，`margin-top:auto`）是**标签 + 匹配行**，一起靠卡片底部 ——
  标签不会紧贴上面的内容，空白落在「内容」与「标签」之间，视觉上更稳
* 数据源标签直接写名字（`dojin-music` / `bgm.tv` / `DoujinStyle`），
  多源确认时并列显示

> 中途踩过一次坑：为了「整齐」把高度写死成 212px 并给标题/meta/标签加行数截断，
> 结果标签多的专辑被砍掉一半、标题也被截。**整齐不能靠裁内容实现** ——
> 正确做法是行内等高 + `min-height` 兜底，内容该多高就多高。

分页走服务端：`GET /api/albums?limit=24&offset=48`，响应里的 `total` 是
**当前筛选条件下的总数**（`Store.count_albums()` 与 `list_albums()` 共用同一套 WHERE 子句，
所以页数不会算错）。翻页保留当前筛选条件。

### 专辑详情抽屉里的「试听 & 链接」

点开任意一张专辑，抽屉里会列出从 **dojin-music.info** 取到的所有相关链接：

| 链接 | 来源 |
|---|---|
| **特设页** | CD 记录的 `link_url`（社团自己的宣传页） |
| **X 原帖** | CD 记录的 `tweet_url`；没有时回落到**特设页里挂的推文**，再回落到社团的 `twitter_id` |
| **YouTube / niconico / SoundCloud / Bandcamp / Spotify / Apple Music / bilibili** | CD 记录里已有的优先；缺的会进**特设页**抓（`[sources.per_source.dojinmusic] enrich_media = true`） |
| Melonbooks / 通贩 / Amazon / iTunes | CD 记录里的 `shop_*` 字段 |

数据源条目页（dojin-music / bgm.tv / DoujinStyle）单独一行显示，不和试听链接混在一起。

> 只有 **dojin-music 已登记的 CD** 才有这些链接。社团是**展会前陆续登记**的，
> 所以展会当天及之后才齐全 —— 例如 M3-2026秋 是 10-25，在 10-16 时站点上只有 1 张 CD。
> 抽屉在取不到链接时会直接说明原因，而不是给个空白区。

**封面走服务端代理并逐层缓存**（本地文件 → 内存 → 磁盘 → 联网），
所以第二次打开是瞬时的。

---

## 网页设计走查（dsh-oil-ui / Oil UI）

网页端的改动有固定纪律：**先用 `oil_ui_explorer` 出同屏对比页定方向 → 再改实现 → 用 `oil_ui_shoot`
真浏览器截图复核**，中间用 `oil_ui_skill_status` 查插件状态、`oil_ui_media` / `oil_ui_image` 取素材、
`read_image` 亲自看图。适用对象是面板（`dnh/webui/static/index.html`、`style.css`、`app.js`）与
`dnh/report.py` 生成的报告页（`reports/*-latest.html`）。

**流程、前置条件（Chrome 截图 / ffmpeg 录屏 / 生图来源）、提示词模板、降级用法与交付清单**：
[docs/oil-ui-web-design.md](docs/oil-ui-web-design.md)

```bash
/oil-ui-explore 面板「专辑库」卡片：三个风格方向的同屏对比页
/oil-ui-review  dnh/webui/static/index.html
```

> 小改动（点名了元素和改法，如改文案、调间距、删指定元素、复用现有样式）不套这套流程，
> 直接改，改完在目标视口看一眼改动处和相邻元素即可。

---

## 代理配置（这里踩过一个坑）

同一份配置要同时满足：境外站点（数据源 + 封面 CDN）走代理、网易云直连。
默认用 **`bypass`** 模式：

```toml
[proxy]
enabled = true
url = "http://127.0.0.1:7897"
mode = "bypass"   # 除 no_proxy_hosts 外全部走代理
no_proxy_hosts = ["music.163.com", "163.com", "126.net", "netease.com", "localhost", "127.0.0.1"]
```

**为什么不用白名单**：以前是 `mode = "hosts"`，只代理名单里的站点。
某次名单里漏了 `lain.bgm.tv`（bgm 的封面 CDN）—— 于是每张封面都去**直连**、
每次都挂到系统超时，实测**单张封面 43 秒后 502**，页面上图片全显示不出来，
点开专辑也像卡死。而封面域名是会变的，白名单永远在追。

`bypass` 模式下只有 `no_proxy_hosts` 里那几个直连，其余（含将来新增的任何 CDN）
自动走代理，这类问题不会再出现。需要白名单语义时把 `mode` 改回 `"hosts"` 即可。

抓封面另有 8 秒硬超时兜底，绝不让一个坏域名把页面拖死。

---

## 项目结构

```
doujindigdb/
├── harvest.py                  便捷入口
├── config/  data/  reports/    配置与运行期产物
├── dnh/
│   ├── cli.py  config.py  models.py  textutil.py  htmlutil.py  qrcode.py
│   ├── httpclient.py  cache.py  db.py  events.py  pipeline.py  report.py  errors.py
│   ├── netease/                aes / crypto / client / matcher  (只读)
│   ├── sources/                base + doujinstyle / dojinmusic / bangumi / vgmdb / dizzylab
│   ├── public/                 data + templates + site + users + auth + static/  (独立用户端)
│   └── webui/                  server.py + static/   (数据采集面板)
├── scripts/demo_report.py
└── tests/                      1000+ 个测试, 全离线
```

---

## 测试

```bash
python -m unittest discover -s tests -t .     # 全量约 1050 个测试, 全离线
```

覆盖：AES FIPS-197 向量、weapi/eapi 往返、文本规范化/假名罗马字、M3 与 Comiket 日期推算、
展会窗口判定、HTML 解析与 CSS 选择器、模型合并、SQLite 持久层、匹配打分、**标记/删除/prune**、
**歌单对照与链接解析**、**dojin-music 的 CD JSON 解析**、**doujinstyle 的网格/详情解析与届数推算**、
报告渲染、各站点适配器离线解析、Pipeline 与 CLI 与 Web 面板端到端、**用户端的口令哈希/角色鉴权/
会话与 CSRF/403/编辑落库/三处展示修复**。

> 测试临时目录统一走 `tests/_tmp.py`（Windows 沙箱下 `tempfile.mkdtemp` 会建出写不进去的目录）。

---

## 部署到公网（免费 VPS）

这一节是把**用户端**（`dnh public`）真正挂到公网上的完整步骤，全部按实测过的命令写。
先明确三件事：

* **采集继续留在本机**。抓取依赖本地代理 `127.0.0.1:7897`（云主机上没有代理，
  直连 bgm.tv 封面 CDN / 部分站点会超时），所以云上只跑"只读浏览 + 登录编辑"。
* **站长端（`dnh serve`）不上公网**。它带着 VGMdb 的 `cf_clearance`、能直接写专辑库，
  而且没有公开端那套限速/反代加固 —— 只走 SSH 隧道。
* 本项目**零第三方依赖**，云上只需要系统自带的 Python 3.12，不用 pip、不用 venv 装包。

### 0. 本机先导出一份"可以上传的库"

```bash
python -m dnh db backup --out dist/dnh.sqlite3
# 已备份: dist/dnh.sqlite3
#   大小: 12.0MB   耗时: 3.42s
#   专辑数: 2205   integrity_check: ok
```

**不要直接 `cp data/dnh.sqlite3`**：库跑在 WAL 模式下，主文件里可能缺少还没
checkpoint 的事务，单独复制会得到一个"看着正常、其实撕裂"的库
（`db backup` 走 SQLite 的 online backup API，保证一致性快照）。

### 0. 上云前必须先定这两件事

#### ① 数据流的唯一方向：**云上为主**

云上的库是**唯一真相**（编辑者会在线改资料）。于是本机只有两种合法动作：

```
本机（采集 / 匹配 / 富化）     ┌──────────────► 云上（编辑者改资料）
   只"读云库" + "推新增"    │
                          └── 绝不做"整库覆盖上传"
```

| 动作 | 允许 | 说明 |
|---|---|---|
| 本机拉云库下来做采集/匹配 | ✅ | `rsync` 或 `db backup` 拉回一份副本 |
| 本机把**新增专辑**推上去 | ✅ | 走 merge 语义，不动已有条目 |
| 本机**整库覆盖**上传 | ⚠️ **必须停服务 + 先拉回云库备份** | 否则编辑者在线改的东西被抹掉（`edited=true` 的字段有 merge 保护，但**不是全部字段**） |
| 云上开编辑权限 + 本机定时整库推送 | ❌ | 两者互斥，会让编辑者的工作随机蒸发 |

> 实践提醒：**"整库覆盖"只在你自己独占操作时用**（例如你刚跑完大批量采集、确认没人正在编辑）。
> 平时发布只推差异，或者干脆把云库拉下来、改完再作为新版本推回去并立即备份。

#### ② 哪些文件必须传、哪些绝对不要传

实测本机 `data/` 的体积构成（这直接决定引导卷该给多大）：

| 路径 | 大小 | 上云 | 理由 |
|---|---|---|---|
| `data/dnh.sqlite3` | ~13 MB | ✅ **必传** | 主库。**用 `dnh db backup` 出快照，不要 `cp`**（WAL 下直接复制会撕裂） |
| `data/public_users.sqlite3` | ~24 KB | ✅ **必传** | 用户/邀请码/登录态。不传 = 云上没有任何账号，没人能登录或编辑 |
| `data/covers/` | **~874 MB** | ✅ **必传** | 不传则页面全是占位图（政策禁止 hotlink 源站，站内只能发本地图） |
| `dnh/`（代码） | ~6 MB | ✅ | 应用本体 |
| `tests/` | ~3 MB | 可选 | 云上不跑测试可省 |
| `data/cache/` | **~3.8 GB** | ❌ **禁传** | 本机采集用的 HTTP 缓存，云上采集不到任何东西，纯占盘 |
| `reports/` `.tmp/` | — | ❌ | 临时产物 |
| `config/config.toml` | — | ⚠️ 单独写 | 云上要**自己写一份**（见第 3 步），**不要**把本机这份传上去 —— 里面有 `cf_clearance` |

参考上传命令（本机执行）：

```bash
python -m dnh db backup --out dist/dnh.sqlite3 --force       # 一致性快照
rsync -av --exclude data --exclude reports --exclude .tmp --exclude __pycache__ \
      ./ dnh@<IP>:/srv/dnh/app/                              # 代码
scp dist/dnh.sqlite3              dnh@<IP>:/srv/dnh/data/dnh.sqlite3
scp data/public_users.sqlite3     dnh@<IP>:/srv/dnh/data/public_users.sqlite3
rsync -av data/covers/            dnh@<IP>:/srv/dnh/data/covers/   # 首次传完约 874MB
```

云上实际占用 ≈ **0.9 GB 数据 + 3 GB 系统 ≈ 4 GB**，所以**引导卷默认 47 GB 足够**（Always Free
存储额度是 200 GB）。别把引导卷拉到 200GB 以上、也别选非 Always Free 的 shape —— 那部分会真扣钱。

### 1. Oracle Cloud Always Free 开一台机

1. 注册 Oracle Cloud（Always Free 永久免费档），区域随便选一个**离你近**的。
2. `Compute → Instances → Create instance`：
   * **Image**：`Canonical Ubuntu 24.04`（自带 Python 3.12）；
   * **Shape**：`VM.Standard.E2.1.Micro`（AMD，1 OCPU / 1GB）—— Always Free 明确包含它，
     选它**不会**踩到"容量不足"；
     `VM.Standard.A1.Flex`（ARM Ampere，最多 4 OCPU / 24GB）也免费，但常年报
     `Out of host capacity`，可以考虑换 Availability Domain 或干脆用 Micro。
   * **Boot volume**：默认 46.6GB（Always Free 上限 200GB，够用）；
   * **SSH keys**：粘贴你本机的 `~/.ssh/id_ed25519.pub`；
   * 勾选 `Assign a public IPv4 address`。
3. **入站规则**：Oracle 默认只放行 22。到
   `Networking → VCN → Subnet → Security Lists → Default Security List` 里加
   `0.0.0.0/0 TCP 80` 与 `0.0.0.0/0 TCP 443`（ICMP 也建议放开，方便 ping）。

```bash
ssh ubuntu@<公网IP>          # Ubuntu 镜像的默认用户是 ubuntu
```

### 2. 装依赖（其实只有 Caddy 和 sqlite3）

```bash
sudo apt update
sudo apt install -y python3 python3-venv caddy sqlite3 rsync curl
python3 --version             # 期望 Python 3.12.x
```

Ubuntu 22.04/23.10 才需要额外装 3.12：

```bash
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt update && sudo apt install -y python3.12 python3.12-venv
```

### 3. 建一个**不用 root** 的服务账号，把代码和数据放上去

```bash
sudo adduser --system --group --home /srv/dnh dnh
sudo mkdir -p /srv/dnh/app /srv/dnh/data /srv/dnh/data/backups
sudo chown -R dnh:dnh /srv/dnh
```

在**本机**（仓库根目录）执行：

```bash
rsync -av --exclude data --exclude reports --exclude .tmp --exclude __pycache__ \
      ./ dnh@<公网IP>:/srv/dnh/app/
scp dist/dnh.sqlite3 dnh@<公网IP>:/srv/dnh/data/dnh.sqlite3
```

云上的 `/srv/dnh/app/config/config.toml`（只列需要改的键）：

```toml
[general]
data_dir = "/srv/dnh/data"
cache_dir = "/srv/dnh/data/cache"
reports_dir = "/srv/dnh/data/reports"

[proxy]
enabled = false            # 云上没有本地代理

[public]
base_url = "https://dnh.example.com"   # 换成你的域名
behind_proxy = true                    # Caddy 在前面 —— 不打开限速会把所有人当成一个 IP
sessions_db = "/srv/dnh/data/sessions.sqlite3"
rate_limit_enabled = true
```

> 只读打开 WAL 库这件事已经实测过：把 `dnh.sqlite3` 单独放到新目录、用
> `mode=ro` 打开可以正常读到 2205 张专辑；真的换了盘/换了机器再发现打不开时，
> 用 `sqlite3 data/dnh.sqlite3 "PRAGMA wal_checkpoint(TRUNCATE); PRAGMA journal_mode=delete;"`
> 收拾一下即可。

### 4. systemd 常驻 `dnh public`

`/etc/systemd/system/dnh-public.service`：

```ini
[Unit]
Description=dnh public (同人专辑档案 · 用户端)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=dnh
Group=dnh
WorkingDirectory=/srv/dnh/app
Environment=PYTHONUNBUFFERED=1
Environment=DNH_PROXY_ENABLED=false
Environment=DNH_PUBLIC_BASE_URL=https://dnh.example.com
Environment=DNH_PUBLIC_BEHIND_PROXY=true
ExecStart=/usr/bin/python3 -m dnh public \
    --host 127.0.0.1 --port 8940 --root /srv/dnh/app
Restart=on-failure
RestartSec=3
# 加固：只允许它写 data 目录，其余文件系统只读
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/srv/dnh/data

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now dnh-public
systemctl status dnh-public --no-pager
journalctl -u dnh-public -f          # 看启动日志(会打印会话库/限速/robots 状态)
```

公开端**允许** `--host 0.0.0.0`；这里仍然绑 `127.0.0.1` 是因为 Caddy 就在同一台机器上，
少暴露一个端口。会话落在 `sessions_db` 里，`systemctl restart dnh-public` **不会**把登录踢掉。

### 5. Caddy 反代 + 自动 HTTPS

`/etc/caddy/Caddyfile`：

```caddyfile
dnh.example.com {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8940 {
        # 让站内限速拿得到真实来源 IP（必须配合 behind_proxy = true）
        header_up X-Real-IP {remote_host}
        header_up X-Forwarded-For {remote_host}
        header_up X-Forwarded-Proto {scheme}
    }
    header {
        Strict-Transport-Security "max-age=31536000; includeSubDomains"
        X-Content-Type-Options nosniff
        Referrer-Policy no-referrer
    }
    log {
        output file /var/log/caddy/dnh-access.log
    }
}
```

```bash
sudo systemctl reload caddy
curl -I https://dnh.example.com/       # 第一次访问会自动签发 Let's Encrypt 证书
```

域名 A 记录指向那台机的公网 IP 即可，不需要自己折腾 certbot。

### 6. 定期备份（在云上，也在本机）

云上 `crontab -u dnh -e`：

```cron
17 4 * * * /usr/bin/python3 -m dnh db backup --root /srv/dnh/app \
  --out /srv/dnh/data/backups/dnh-$(date +\%F).sqlite3 --force \
  >> /srv/dnh/data/backups/backup.log 2>&1
```

本机定期把云上的备份拉回来（只读站点本身就在写会话库，备份是双保险）：

```bash
rsync -av dnh@<公网IP>:/srv/dnh/data/backups/ ./vps-backups/
```

恢复：停服务 → 把备份复制成 `data/dnh.sqlite3` → 删掉可能残留的 `-wal`/`-shm` → 起服务。

### 7. 站长端只走 SSH 隧道

```bash
# 本机开隧道（云上不用起任何额外的服务）
ssh -L 8787:127.0.0.1:8787 dnh@<公网IP>
# 云上(那条 ssh 会话里)跑站长端；也可以在本机跑本地的站长端连本地库
python3 -m dnh serve --port 8787
# 浏览器打开 http://127.0.0.1:8787/
```

`dnh serve` 默认绑定 `127.0.0.1`；一旦你传了 `--host 0.0.0.0` 之类的非本地地址，
它会打印**醒目警告**（带 `cf_clearance`、能写库、没有公开端加固）。要对外给人浏览，
用 `dnh public`，不要暴露站长端。

### 8. 上线验收（和本仓实测用的是同一套命令）

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://dnh.example.com/robots.txt   # 200
curl -s https://dnh.example.com/robots.txt                                    # 看到 Sitemap: 行
curl -s -o /dev/null -w '%{http_code}\n' https://dnh.example.com/sitemap.xml  # 200
curl -s https://dnh.example.com/sitemap-0.xml | head -n 5                     # 分片(库大时)
for i in $(seq 1 40); do curl -s -o /dev/null -w '%{http_code} ' https://dnh.example.com/about; done; echo
# 期望：前 30 次 200，之后出现 429（browse 档 30 令牌 + 10/s 补充）
curl -sI https://dnh.example.com/about | grep -i '^retry-after'
sudo systemctl restart dnh-public     # 重启后刷新页面，登录态不丢
```

本仓实测（`dnh public --port 8970`，真实库 2205 张专辑）：

```
/robots.txt            200  text/plain; charset=utf-8   (含 Sitemap / Disallow 编辑与账号路径)
/sitemap.xml           200  application/xml             (<sitemapindex> 2 个分片)
/sitemap-0.xml         200  <url> 2000 条
连打 38 次 /about      {200: 33, 429: 5}   首个 Retry-After=1
注册登录后 /account    200  logged_in=True
重启服务后再访问       /account 200  logged_in=True（带旧 Cookie，会话已落盘）
```

### 9. 免费 VPS 上的几个坑

* **入站规则是两层**：Oracle 的 Security List + 机器里的 `ufw`（Ubuntu 镜像默认 ufw 不启用，
  但如果你开了，别忘了 `sudo ufw allow 80,443/tcp`）。
* **1GB 内存的 Micro** 够跑只读站点（`http.server` + SQLite 只读查询），
  但别在它上面跑采集或匹配任务。
* **封面留在本机**也能用：本地没有封面文件时公开端会 302 到原始图源；
  想全本地就把 `data/covers` 一起 rsync 上去（体积可能几百 MB）。
* **限速按 IP 分四档**（`[public]` 里全部可调）：浏览页 30 令牌 + 10/s；
  封面/静态 120 令牌 + 10/s（一次页面就要几十张图，桶必须大）；
  检索（`/search`、`/albums?q=`）20 令牌 + 2/s；写接口（登录/注册/编辑）5 令牌 + 0.25/s。
  被限时返回 **429 + `Retry-After`**；`/robots.txt` 与 `/sitemap.xml` 完全不限速。
  ⚠️ 反代后面**必须**把 `behind_proxy = true`（见第 3 步），否则所有请求都来自
  Caddy 的 `127.0.0.1`，限速会把全站用户当成同一个人。
* **会话清理**：`data/sessions.sqlite3` 里的过期行会在启动与运行时自动删；
  想一次性踢掉所有登录，直接删这个文件再重启。
* **日志**：`journalctl -u dnh-public -f`；Caddy 的访问日志在 `/var/log/caddy/`，
  两者都要留意磁盘（Always Free 200GB，一般够）。

---

## 抓取合规（robots.txt / UA / 节流）

公网上线之后，"我们怎么访问别人的站点"同样要有据可查。这一节是本仓新增的能力。

### 站点自己也要有 robots.txt 与 sitemap.xml

公开端现在直接提供这两个文件，**从库里动态生成**：

```
GET /robots.txt    -> 允许抓浏览页；Disallow 编辑(/edit、/album/*/edit)、
                      账号(/account /login /register /logout)与 /search；
                      声明 Sitemap: 与 Crawl-delay: 1
GET /sitemap.xml   -> 首页/列表页 + 每张专辑 + 每个社团 + 每个展会；
                      条目数超过 [public] sitemap_max_urls(默认 2000)时
                      自动变成 <sitemapindex>，分片在 /sitemap-<n>.xml
```

绝对地址优先取 `[public] base_url`，其次按请求的 `Host` 拼（反代后面才信
`X-Forwarded-Proto`，所以 `behind_proxy = true` 时 sitemap 里就是 `https://`）。

### 采集侧：默认遵守 robots.txt

`python -m dnh robots <url>` 可以直接复核某个 URL 的判定：

```bash
python -m dnh robots https://bgm.tv/subject/1
# host: bgm.tv   路径: /subject/1
# robots 状态: 可读  HTTP 200
# 判定: 允许
# Crawl-delay: 0.0s   实际最小间隔: 1.0s   (rate_limit=1.0s, UA=dnh-harvester/0.1.0)
```

`python -m dnh robots --audit` 一次性列出**各数据源的 host + 最小间隔 + robots 状态**：

```
数据源           启用    host                  最小间隔  Crawl-delay robots 状态        许可
doujinstyle   是     doujinstyle.com          1.0s     0.0s     可读                不允许
dojinmusic    是     www.dojin-music.info     1.0s     0.0s     不可用 -> 不限制    允许
bangumi       是     bgm.tv                   1.0s     0.0s     可读                允许
vgmdb         是     vgmdb.net                1.0s     0.0s     豁免(不检查)        允许(豁免)
dizzylab      否     www.dizzylab.net         1.0s     0.0s     可读                允许
```

实现要点：

* **每个 host 只取一次** `/robots.txt`（内存 + `data/cache/robots/*.json` 落盘，
  默认 24h，符合 RFC 9309 §2.4 的 "不超过 24 小时"）；解析按 RFC 9309：
  `*` 通配、结尾 `$` 锚定、最长匹配优先、同长 `Allow` 优先、
  具名 UA 组优先于 `*`、解析上限 512KiB（RFC 要求 ≥500KiB）。
* **状态码语义按 RFC 9309**：`2xx` 按规则办；`4xx`（含 401/403/404）是
  "Unavailable" → **视为不限制**；`5xx`/网络故障/429 是 "Unreachable" →
  **暂时假设全站禁止**，但只记 `robots_error_ttl`（默认 300s），
  免得一次抖动让某个站点一整天都不能采。
* **`Crawl-delay` 真的生效**：该 host 的最小请求间隔 =
  `max(rate_limit, Crawl-delay, 单源 delay)`。
* **UA 身份**：默认发 `dnh-harvester/<版本> (+<[http] contact>)`（不伪装浏览器），
  所以机器人可以在 robots.txt 里直接写 `User-agent: dnh-harvester`。
  只有 Cloudflare 把 `cf_clearance` 与 UA 绑定的站点（VGMdb）才用浏览器 UA。
* **逃生舱**（两处，都会打 warning）：
  * `[http] robots_override_hosts = [...]` —— host 名单，默认只含 VGMdb
    （你已用浏览器凭据 + 显式授权访问它，一刀切会直接打断现有采集）；
  * `[sources.per_source.<源>] respect_robots = false` —— **单源豁免**，
    按各源声明的 homepage host 生效。
  * 想彻底合规：清空 `robots_override_hosts`，并把各源（含 vgmdb）设回 `true`。

> ⚠️ **实测提醒（会影响默认采集）**：`doujinstyle.com/robots.txt` 是
> `User-agent: * / Disallow: /`，**全站禁止**。默认 `respect_robots = true` 时
> DoujinStyle 这个源会被拦住（这正是"默认遵守"的含义）。
> 要继续用它，请在配置里**显式**写：
>
> ```toml
> [sources.per_source.doujinstyle]
> respect_robots = false      # 明确表示"我知道它禁止，我仍要用"
> ```
>
> `dojin-music` 的 `robots.txt` 返回 403，按 RFC 9309 属于 "Unavailable" →
> 视为不限制，**不影响**现有采集。

---

## 已知限制

1. **VGMdb 被 Cloudflare 的 JS 挑战挡住** —— 2026-10 换节点后重测：先用两个 IP 回显
   服务确认**代理是通的**（出口 IP `166.88.197.247`），但 VGMdb 返回
   `HTTP 403` + `server: cloudflare` + 响应体 `Just a moment...`。
   这是 **Cloudflare 的浏览器挑战，不是 IP 封禁** —— 纯 HTTP 客户端
   （Python urllib、curl）过不了 JS 挑战，**换节点也没用**。

   **解法（不引入新依赖）**：借用浏览器已经拿到的 `cf_clearance`：

   ```toml
   [sources.per_source.vgmdb]
   delay = 2.0
   cookie = "cf_clearance=xxxxx"                    # 浏览器 F12 → Cookies 复制
   user_agent = "Mozilla/5.0 ... Chrome/124 ..."     # 必须与申请 cookie 时一致
   ```

   cookie 会过期（几小时到几天），过期后重新复制。`dnh sources list` 会区分
   「JS 挑战」与「cookie 已过期」两种情况。拿到 cookie 之前 `vgmdb` 保持默认关闭，
   其解析路径**没能联网验证**，启用前请先小样本试。
2. **没有联网验证过全部解析路径**。已用真实请求核对过的有：dojin-music 的展会列表 /
   试听页 / CD 详情、doujinstyle 的标签索引与专辑页、bgm.tv 的小组页 / 话题页 /
   收藏接口 / 条目详情、网易云搜索 / 专辑详情 / 歌单详情 / **艺人检索与艺人专辑列表**、
   代理连通性、封面下载。
3. **封面依赖代理**。bgm.tv 的封面 CDN（`lain.bgm.tv`）在国内直连不通，必须让代理把它带上
   （默认的 `bypass` 模式已经覆盖）。代理没开时封面会是空的 —— 服务端 8 秒超时后返回 502，
   不会把页面卡住，但也拿不到图。
4. **bgm.tv 的展会数据依赖用户自愿录入**。若某个展会还没有人建「信息收集贴」，
   或贴主把收藏设成私密，该源就取不到数据（会写进日志，不会静默失败）。
5. **dojin-music 的封面常常没有**（实测那张 CD 的 `jacket` 就是 `null`，站点本身没传），
   适配器会依次回落到社团 `link_img` → 推文媒体图，都没有就留空，**不编造**。
6. **按艺人专辑匹配依赖网易云能搜到这个社团**。社团名太常见、或网易云没收录时，
   这条路径会认不准（此时按 `artist_min_similarity` 主动放弃），只剩关键词路径，精度会下降。
   匹配结果里的 `method` 字段能看出走的哪条路。
7. 多个数据源**共用同一个 SQLite 库**。跨源合并已经是「按可信度决定字段归属 + 可配严格度」，
   但**还没有做按源隔离的独立存储**。
8. 私密歌单需要登录态 Cookie 才能读；公开歌单可以直接对照。
9. Web 面板没有鉴权，只适合本机使用。`dnh serve` 因此**默认只绑 `127.0.0.1`**，
   传非本地地址（如 `--host 0.0.0.0`）会打印醒目警告 —— 它带 `cf_clearance`、
   能写专辑库，且没有公开端的限速/反代加固。对外浏览请用 `dnh public`。

---

## 后续计划

1. 数据源隔离：按源分开存储（各自一张表 / 各自一个库），再把合并结果物化成视图
2. `vgmdb`：等能访问的网络下再联网核对解析路径

---

## 合规与使用须知

本工具**仅供个人整理与检索同人音乐使用**。请遵守各目标站点的服务条款与 `robots.txt`，
保持默认限速。同人音乐是创作者的重要收入来源 —— 本工具只做检索、聚合与标记，
**不下载、不传播音频内容**，请通过 dizzylab / Bandcamp / Melonbooks / 虎穴 等正规渠道购买。

本仓现在把这件事做成了可复核的工具，而不是口头承诺：

* `python -m dnh robots <url>` —— 打印某个 URL 的 robots 判定（allowed / disallowed /
  Crawl-delay / 是否豁免）与命中的规则；
* `python -m dnh robots --audit` —— 一张表列出每个数据源的 host、最小请求间隔与 robots 状态；
* 默认 UA 是可识别的 `dnh-harvester/<版本> (+联系地址)`，站点可以在 robots.txt 里
  针对它写规则，也可以据此联系我们；
* `Crawl-delay` 会真正变成每 host 的最小间隔（与 `rate_limit` 取较大值）。

唯一的例外在 `[http] robots_override_hosts`（默认只有 VGMdb）—— 那是**用户显式授权**
的逃生舱，每次跳过都会写 warning；想彻底合规就把这个列表清空。

> 已知会打断的默认采集：`doujinstyle.com` 的 robots.txt 是全站禁止
> （见《抓取合规》一节的实测提醒）。要继续用该源，请显式设置
> `[sources.per_source.doujinstyle] respect_robots = false`。
---

## 用户端账号体系:角色、编辑者申请与注册验证(维护者说明)

> 面向维护者/部署者。代码在 `dnh/public/users.py`(账号与角色)、
> `dnh/public/auth.py`(会话/CSRF/鉴权)、`dnh/public/verify.py`(验证码与邮箱验证)。
> 账号库是**独立文件** `data/public_users.sqlite3`,不在专辑主库里。

### 角色与能力

| 角色 | 能浏览 | 能写专辑 | 能审核申请 / 管用户 | 怎么获得 |
|---|---|---|---|---|
| `viewer` 普通用户 | ✅ | ❌ | ❌ | 注册即得 |
| `editor` 编辑者 | ✅ | ✅ | ❌ | ① 邀请码 ② 自己申请 → 超级用户审核通过 |
| `superuser` 超级用户 | ✅ | ✅ | ✅ | 仅 CLI(`add --role superuser` / `set-role`) |

普通用户在「我的账号」页可以提交**编辑者申请**(`request_editor`),申请进入
`editor_requests` 表(状态 `pending` / `approved` / `rejected`);超级用户在审核队列里
通过(`approve_editor`)或拒绝(`reject_editor`),通过后该用户角色自动升到 `editor`。
`users` 表新增的 `email` / `email_verified` / `verified_at` / `is_active` 四列由
**自动迁移**追加(老库直接可用,老账号默认"邮箱已验证",不会被锁在门外)。

### 账号管理 CLI(`python -m dnh.public.users ...`)

```bash
python -m dnh.public.users init                       # 建表 + 迁移(幂等)
python -m dnh.public.users add <用户名> --role viewer|editor|superuser
python -m dnh.public.users list                       # 账号一览(角色/状态/最后登录)
python -m dnh.public.users show <用户名>               # 单账号详情(能力/最近申请)
python -m dnh.public.users request <用户名> [--reason "..."]   # 代用户提交申请
python -m dnh.public.users requests [--status pending|approved|rejected|all]
python -m dnh.public.users approve <用户名|申请id> [--actor <超管名>] [--note "..."]
python -m dnh.public.users reject  <用户名|申请id> [--actor <超管名>] [--note "..."]
python -m dnh.public.users set-role <用户名> --role <角色> [--actor <超管名>]
python -m dnh.public.users disable <用户名> / enable <用户名>   [--actor <超管名>]
python -m dnh.public.users verify <用户名>             # 人工把账号标成"邮箱已验证"(救急)
python -m dnh.public.users set-email <用户名> <邮箱> [--verify]
python -m dnh.public.users passwd <用户名> / remove <用户名>
python -m dnh.public.users invite [--note "..."](旧机制,一次性编辑者邀请码)
```

命令行里 `--actor` 留空表示"系统操作"(由操作系统权限兜底);网页接线时**必须**
把当前登录的超级用户名传进去,后端会真的校验。

页面需要"在线会话立刻生效"时:改完角色调 `SessionManager.update_user_role(user_id, role)`,
禁用/删号调 `SessionManager.drop_user(user_id)`。

### 注册验证之一:本地验证码(算术题)

* `verify.new_captcha()` 生成一道题,返回 `{id, question, kind, ttl, expires_at, max_attempts}`;
  **答案只存在服务端内存**,页面只拿 `id` 和题面。
* 表单里放两个字段:隐藏域 `captcha_id` = `id`,输入框(比如 `captcha_answer`)收答案;
  提交时 `verify.check_captcha(captcha_id, answer)` → `(ok, 原因字符串)`。
* 语义:**一次性**(答对即销毁)、**过期**(默认 600 秒)、**限次**(默认答错 3 次作废)、
  **无会话绑定**(令牌化,多标签页互不影响)、**不落库**(进程重启后未提交的题失效,刷新即可)。
* 纯表单(无 JS)刷新验证码:注册页**每次渲染都调一次 `new_captcha()`** 即可,给一个
  `GET /register?new=1` 或页内"换一道"链接重新渲染;旧的题**不会**被顶掉,但它自己会
  过期/答错作废,所以不用担心泄漏(每题都是独立的随机令牌,内存上限 2000 条,超出淘汰最老)。

### 注册验证之二:邮箱验证码(可开关)

**只在配置了 SMTP 时启用**;没配就自动降级为"只有验证码",账号建出来立即可用 ——
注册流程不会被邮件服务卡死。开关由环境变量控制(**不写进 config.toml**):

| 环境变量 | 必填 | 说明 |
|---|---|---|
| `DDDB_SMTP_HOST` | ✅ | SMTP 主机,如 `smtp.example.com` |
| `DDDB_SMTP_PORT` | — | 端口,默认 `587` |
| `DDDB_SMTP_USER` | — | 用户名;为空则不登录 |
| `DDDB_SMTP_PASSWORD` | — | 口令(配了 USER 才用) |
| `DDDB_SMTP_FROM` | ✅ | 发件人地址;缺省回退到 `DDDB_SMTP_USER` |
| `DDDB_SMTP_TLS` | — | `1`(默认)= 明文连接后 STARTTLS;`0` = 不用 TLS |
| `DDDB_SMTP_SSL` | — | `1` = 直接 SMTPS(常配 465) |
| `DDDB_SMTP_TIMEOUT` | — | 秒,默认 `10` |
| `DDDB_EMAIL_VERIFY` | — | 总开关:`0/false/off` = 即使配了 SMTP 也关闭邮箱验证 |

判定规则:`DDDB_SMTP_HOST` 与发件人都在 → 启用;**再加 `DDDB_EMAIL_VERIFY=0` 可强制关闭**。

页面接线:

```python
from dnh.public import verify as V
V.email_enabled()                    # 决定注册页是否显示"邮箱"输入框
V.email_verification_state(store, username)   # {"enabled","required","has_email","verified","email"(打码),"reason"}

# 启用时:建号(待验证) -> 发码
store.create_user(username, password, email=email, email_verified=False)
V.send_email_code(store, username)   # (ok, 原因) —— 60 秒内不能重发
# 用户收码后:
V.check_email_code(store, username, code)     # 通过则自动把账号标成已验证
```

* 验证码 6 位数字,**只存 PBKDF2 哈希**;默认 15 分钟过期、错 5 次作废、同账号 60 秒冷却。
* **未验证邮箱的账号不能登录**:`store.authenticate()` 返回 `reason="unverified_email"`
  (页面据此显示"请先验证邮箱"+ 重发入口);登录成功与否由 `LoginResult.ok` 决定。
* 没配 SMTP 时 `send_email_code()` 返回 `(False, "未配置 SMTP(...), 邮箱验证未启用")`,
  **不抛异常**;页面按 `email_enabled()` 走降级分支、以 `email_verified=True` 建号。
* 测试/离线环境可以注入 `verify.RecordingMailer()`(不联网,`last_code()` 取验证码),
  不必起真实 SMTP。

### 故障排查

* 所有人都登不上:`data/public_users.sqlite3` 被换掉/删了?`users list` 看账号还在不在。
* 某个账号登不上:`users show <用户名>` 看是"禁用"还是"邮箱未验证";
  `users enable <用户名>` / `users verify <用户名>` 两条命令就能救回来。
* 邮箱验证码收不到:`DDDB_SMTP_*` 配了吗(`verify.email_enabled()`);
  `verify.email_disabled_reason()` 会直接告诉你缺哪个变量。
* 降级验证:`python -m dnh.public.users list` 之后随便挑个账号,用
  `python -c "from dnh.public.users import UserStore; ... authenticate(...)"` 走一遍。

## 账号安全体系（邮箱为主验证路径）

### 1. 注册：必须点邮件里的链接才算绑定成功
- 启用邮箱验证时，注册会创建 `email_verified=0` 的账号并**发送一封含一次性链接的验证邮件**
  （`https://<站点>/verify?token=...`，24 小时有效、只能用一次；邮件里同时附一个 6 位验证码作备选）。
- 点链接后：`/verify?token=` 核销令牌 → `email_verified=1` 并记录当前登录环境；
  **验证前登录会被拒绝**（`reason=unverified_email`）。
- 链接令牌只存 `sha256` 哈希；用途（email / login）相互隔离，不能混用。

### 2. 敏感操作必须邮箱验证
| 操作 | 要求 |
|---|---|
| 修改密码 | 登录环境已确认 + 邮箱验证码（`purpose=password_change`）+ 当前密码正确；改完踢掉其它设备会话 |
| 注销账号 | 登录环境已确认 + 邮箱验证码（`purpose=account_delete`）+ 输入"注销账号"确认 |
| 新环境登录 | 登录环境指纹（`sha256(IP|UA)`）与上次不同 → 会话标记为**待确认**，发确认邮件；待确认会话只能浏览、敏感操作一律 403，点确认链接后才转正 |

入口：`/account?tab=security`。

### 3. 相关实现位置
- `dnh/public/users.py`：`issue_email_link / redeem_email_link / last_env / set_last_env`
- `dnh/public/verify.py`：`send_email_link / check_email_link`；`send_email_code / check_email_code` 支持 `purpose`
- `dnh/public/auth.py`：会话 `env_hash` / `confirmed`
- `dnh/public/site.py`：注册发链接、`/verify?token=`、登录环境检测、`/account/{password,delete,security-code}`

## 云端同步（站长端 → 云端）

```powershell
# 封面: 只推云端缺失/大小不一致的文件(只增不删)
python tools\sync_covers.py --dry-run
python tools\sync_covers.py

# 元数据: 把本地 updated_at 之后修改过的专辑 payload 推到云端
python tools\sync_cloud.py --dry-run
python tools\sync_cloud.py            # 同步一次
python tools\sync_cloud.py --watch    # 常驻, 每 120 秒检查一次
```

- 同步状态存在 `data/.cloud_sync_state.json`（只记上次同步时间，增量同步）。
- 云端落库用 `python -m dnh db import-payload --file <json> --apply`（不加 `--apply` 只演练）。
- 若本地 `cover_local` 是 Windows 路径，云端按文件名回退解析；批量导入后可运行归一化把
  `cover_local` 改写成 `/srv/dnh/data/covers/<相对路径>`（本项目已做过一次）。
- 想让同步"全自动"，可在 Windows 任务计划里注册 `sync_cloud.py`（每 5 分钟，脚本：`tools\install_sync_task.ps1`）或让 `--watch` 常驻。
- **站长端保存即同步**：本地 webui 保存专辑后会后台触发一次同步（开关文件 `data/.cloud_sync_enabled`，60 秒节流；删掉该文件即关闭）。
- 双向同步：`python tools\sync_cloud.py --pull` 把**云端**更新的专辑拉回本地
  （云端 `dnh db export-payload --since <ts> --out <json>`；本地
  `dnh db import-payload --file <json> --apply`；状态里分别记 push/pull 时间戳）。
- **部署注意**：打发布包时永远排除 `config/config.toml`（云端配置与本地不同，
  误覆盖会让 data_dir/代理指向本地，服务直接起不来）。

## 本轮：未验证/忘记密码/曲目表标题

- **曲目表标题**：「曲目表 · 共 N 首」已移到表格**外面**的 `.tracklist-head` 横排一行
  （原先在 `<caption>` 里会被窄表格挤成竖排）；表格本身包在 `.table-scroll` 里。
- **未验证也能登录**：注册后直接进「我的账号 → 总览」；页面顶部横幅提醒「邮箱还没有验证」，
  未验证只有**访客权限**（所有写操作 403），**超过 24 小时不验证会删除账号**
  （站点启动时清理 + 云端 cron 每小时跑 `tools/purge_unverified.py`）。
- **忘记密码**：登录页新增「忘记密码?」→ `/forgot` 发重置邮件（响应不泄漏账号是否存在）
  → `/reset?token=` 设置新密码（令牌一次性、24h）；改密码**不再需要当前密码**。
- **页脚在哪改**：用户端页脚在 `dnh/public/templates.py` 的 `layout()` 里
  （`<footer class="wrap footer">`，约 523-534 行），首行是 `SITE_TAGLINE`；
  站长端 webui 没有页脚。

## 本轮：名称去重 + 艺人与社团悬停卡片

- **曲目表标题去重**：只保留 `<h2 class="section-title">曲目表</h2>`，
  表格上方那行只剩「共 N 首 · 总时长 …」，不再出现两次「曲目表」。
- **制作人员名称去重**：来源合并后会得到 `"X / X / X"`、`"X、X"` 这类**同一个
  字符串里重复多遍**的名字 —— 现在先按 `、,，;；/|／` 拆开再保序去重，
  同 label 的多个分组也会合并。信息表里不再重复列社团/艺人。
- **艺人与社团区域 + 悬停卡片**：专辑页标题下方新增「艺人与社团」面板，
  每个社团/艺人是一个圆角 chip；**光标悬停（或键盘聚焦）弹出卡片**，
  卡片里显示封面/首字母图、专辑数、类型/成立年、简介、跳转链接，以及
  **关注/已关注按钮**（关注表单从页面底部搬进来了）。纯 CSS 实现
  （`:hover` / `:focus-within`），无额外脚本请求。
- 艺人过多时（大合辑）只显示前 **12 位** + `+N 位艺人`（悬停可见完整名单）。
- CSS 版本：`public.css?v=7`；服务端渲染，改文案不需要升版本。

## 本轮：评论点赞 / 安全设置弹窗 / 封面上传迁移

- **评论区**：显示头像（`/avatar/<user_id>`）、评论时间、点赞数；登录用户可点赞/取消，
  **不能给自己的评论点赞**（服务端 403）；自己评论只显示赞数，不出现点赞按钮。
- **封面上传**：从专辑页底部的评论区移到 **编辑专辑页**（`/album/<key>/edit` 的「封面」面板），
  上传后仍写 `covers/<key>.img`。
- **安全设置**：只保留「修改密码」「注销账号」两个按钮，各自在弹出的 `<dialog>` 里完成；
  页面上不再写实现说明（不显示当前登录环境、不解释验证码用途等）。
- **导航**：移除「邀请码」按钮（页面仍可直达 `/invites`，仅编辑者可见）；邀请码页删除说明性文案。
- **隐私设置里的「私信状态」**：它是"别人能否在个人主页看到『可通过私信联系』这一状态"的可见性档位
  （`vis_messages`，默认「所有人都不可见」），不影响实际收发私信。

## 本轮：新建条目 / 保存修复 / 来源库清理 / 实体 ID 化

- **新建条目**：编辑者在「工作队列」「浏览」「社团」「艺术家」「展会」「标签」页会看到对应
  的「新建…」按钮（`/new/album|circle|artist|event|tag`）。字段规格集中在
  `templates.NEW_ENTITY_SPECS` —— **加字段只改这一处**，表单自动出现。
- **保存无反应的修复**：编辑页的封面表单原先被嵌进主表单（HTML 不允许嵌套 `<form>`），
  导致保存按钮落到表单外；现在封面表单独立并**置顶**。另外 `parse_album_form` 曾把
  「原标题」误判为必填，只填标题会被静默回显 —— 已改为标题必填、原标题可选。
- **时长占位符**：不再显示 `3:42`，改为「分:秒」。
- **来源数据库清理**：`dnh db strip-source-links --apply` 会递归剔除专辑里的
  `source_ids`（vgmdb/bangumi/…）、`urls` 里的来源站链接、`credits`/`tracks` 的
  `source` 标记、`trust_by_source`、`raw`、来源 CDN 的 `cover_url`。
  **云端已清零（17257 条）**；本地站长端库保持原样。
- **实体 ID 化**：`entities(kind, id, name)` 给专辑/社团/艺人/展会/标签统一编号，
  并建立 `album_circles` / `album_artists` / `album_events` / `album_tag_ids` 关联表。
  `dnh db ids --build` 全量分配（幂等）。表单里可以直接输入名称，或输入编号
  （`#12` / `12` / `artistId:12`）绑定，候选通过 `<datalist>` 提供。
