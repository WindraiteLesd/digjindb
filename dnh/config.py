"""配置系统.

使用标准库 ``tomllib`` 读取 TOML 配置(Python 3.11+), 支持:

* 配置文件 ``config/config.toml`` (可由 ``dnh init`` 生成)
* 同目录 ``.env`` 文件与进程环境变量覆盖(``DNH_SECTION_KEY``)
* 缺省值完整, 不写配置文件也能跑

示例::

    DNH_PROXY_URL=http://127.0.0.1:7897
    DNH_NETEASE_COOKIE="MUSIC_U=...; __csrf=..."
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Mapping

from .errors import ConfigError

DEFAULT_CONFIG_RELPATH = Path("config") / "config.toml"

DEFAULT_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
]

DEFAULT_NITTER_INSTANCES: list[str] = []   # X/Nitter 支持已移除, 仅保留常量名以免旧脚本 import 报错

#: 需要**浏览器 User-Agent** 的站点。
#:
#: 只有 Cloudflare 这类把 ``cf_clearance`` 与申请时的 UA **绑定**的站点才需要;
#: 其余站点一律用可识别的 ``dnh-harvester/<version>`` —— 伪装成浏览器既不诚实,
#: 也让站长无法在日志里认出我们、无法针对我们放行或联系我们。
DEFAULT_BROWSER_UA_HOSTS: list[str] = ["vgmdb.net", "www.vgmdb.net"]

#: robots.txt 豁免名单(后缀匹配)。默认只有 VGMdb, 原因见 HttpConfig 里的注释。
DEFAULT_ROBOTS_OVERRIDE_HOSTS: list[str] = ["vgmdb.net", "www.vgmdb.net"]


# --------------------------------------------------------------------------
# 子配置
# --------------------------------------------------------------------------


@dataclass
class GeneralConfig:
    data_dir: str = "data"
    cache_dir: str = "data/cache"
    reports_dir: str = "reports"
    log_level: str = "INFO"
    locale: str = "zh"
    timezone: str = "Asia/Shanghai"
    user_agents: list[str] = field(default_factory=lambda: list(DEFAULT_USER_AGENTS))


@dataclass
class HttpConfig:
    timeout: float = 25.0
    connect_timeout: float = 10.0
    retries: int = 3
    backoff: float = 1.6
    cache_ttl: int = 86400           # 秒; 0 = 禁用缓存
    rate_limit: float = 1.0          # 同一 host 两次请求的最小间隔(秒)
    verify_tls: bool = True
    max_redirects: int = 5
    accept_language: str = "ja,en-US;q=0.8,en;q=0.7,zh-CN;q=0.6"

    # -- 身份(UA) ----------------------------------------------------------
    #: 署名联系地址(邮箱 / 站点 URL)。会拼进默认 UA:
    #: ``dnh-harvester/<version> (+<contact>)``, 让站长能联系到我们。
    contact: str = ""
    #: 显式指定 UA(留空 = 自动生成可识别的 ``dnh-harvester/<version>``)。
    user_agent: str = ""
    #: 这些 host 使用 ``general.user_agents`` 里的浏览器 UA(cf_clearance 与 UA 绑定)
    browser_ua_hosts: list[str] = field(default_factory=lambda: list(DEFAULT_BROWSER_UA_HOSTS))

    # -- robots.txt ---------------------------------------------------------
    #: 是否遵守各站点的 robots.txt(默认 true)。**不要**轻易关掉。
    respect_robots: bool = True
    #: 豁免名单(后缀匹配): 名单里的 host 跳过 robots 检查, 并在日志里 warning 说明。
    #:
    #: 默认含 VGMdb —— 用户在浏览器通过 Cloudflare 验证后**显式授权**我们用它自己
    #: 的 cf_clearance 访问, 而 vgmdb.net 的 robots.txt 对通用爬虫是收紧的; 一刀切
    #: 会直接打断现有采集, 所以这里保留一条明确、可审计的逃生舱。
    #: 想彻底合规: 清空这个列表即可(单源也可用
    #: ``[sources.per_source.<名字>] respect_robots = false`` 单独豁免)。
    robots_override_hosts: list[str] = field(default_factory=lambda: list(DEFAULT_ROBOTS_OVERRIDE_HOSTS))
    #: robots.txt 缓存有效期(秒): 每个 host 在这个窗口内只取一次。
    robots_cache_ttl: int = 86400
    #: robots.txt 取不到(5xx / 网络故障)时的判定缓存(秒)。按 RFC 9309 视为"全站禁止",
    #: 但只记 5 分钟 —— 一次网络抖动不该让某个站点一整天都不能采。
    robots_error_ttl: int = 300
    #: Crawl-delay 上限(秒); 0 = 不封顶, robots 说多少就是多少。
    crawl_delay_max: float = 0.0


@dataclass
class PublicConfig:
    """用户端(``dnh public``)部署相关设置 —— 免费 VPS 路线上线要用的旋钮."""

    #: 会话库(重启不掉线)。空串 = 内存会话(仅测试/临时用)。
    sessions_db: str = "data/sessions.sqlite3"
    #: 站点对外地址(如 ``https://dnh.example.com``)。留空则按请求的
    #: ``X-Forwarded-Proto`` + ``Host`` 拼 robots/sitemap 里的绝对地址。
    base_url: str = ""
    #: 是否信任反向代理(Caddy/Nginx)传来的 ``X-Forwarded-For`` / ``X-Real-IP``。
    #: 只有**确实**在反代后面才打开 —— 否则任何人都能伪造 IP 绕过限速。
    behind_proxy: bool = False
    #: 按 IP 的令牌桶限速(超限 429 + Retry-After)。
    rate_limit_enabled: bool = True
    #: 浏览(HTML 页面): 每秒补充令牌 / 桶容量
    rate_limit_rate: float = 10.0
    rate_limit_burst: float = 30.0
    #: 重活(封面 / 静态资源): 页面一次要抓几十张图, 桶要更大
    rate_limit_heavy_rate: float = 10.0
    rate_limit_heavy_burst: float = 120.0
    #: 检索(/search 与 /albums?q=): 每次都是一次全库查询, 收紧
    rate_limit_search_rate: float = 2.0
    rate_limit_search_burst: float = 20.0
    #: 写接口(登录/注册/编辑): 正常用户一分钟也提交不了几次
    rate_limit_write_rate: float = 0.25
    rate_limit_write_burst: float = 5.0
    #: 空闲多久回收令牌桶 / 最多同时记住多少来源
    rate_limit_idle_ttl: float = 600.0
    rate_limit_max_keys: int = 20000
    #: sitemap 单个文件最多多少个 URL(超出则输出 sitemap 索引 + 分片文件)
    sitemap_max_urls: int = 2000
    #: sitemap / robots 的渲染缓存(秒)
    sitemap_cache_ttl: float = 300.0


@dataclass
class ProxyConfig:
    enabled: bool = True
    url: str = "http://127.0.0.1:7897"
    #: bypass = 除 no_proxy_hosts 外**全部**走代理(推荐: 只有网易云必须直连)
    #: hosts  = 只代理 hosts 名单里的站点(名单缺一个 CDN 就会直连超时, 易踩坑)
    #: all    = 全走代理  |  off = 关闭
    mode: str = "bypass"
    # 需要走代理的站点(后缀匹配) —— 仅 mode="hosts" 时使用
    hosts: list[str] = field(
        default_factory=lambda: [
            "doujinstyle.com", "www.doujinstyle.com",
            "dojin-music.info", "www.dojin-music.info",
            "bgm.tv", "api.bgm.tv", "lain.bgm.tv",
            "vgmdb.net", "www.vgmdb.net",
            "dizzylab.net", "www.dizzylab.net", "api.dizzylab.net",
            "bandcamp.com", "melonbooks.co.jp", "toranoana.jp", "pixiv.net",
            "youtube.com", "www.youtube.com", "soundcloud.com",
        ]
    )
    # 明确直连的站点(优先于 hosts / bypass)
    no_proxy_hosts: list[str] = field(
        default_factory=lambda: ["music.163.com", "163.com", "126.net", "netease.com", "localhost", "127.0.0.1"]
    )

    def should_proxy(self, url: str) -> bool:
        if not self.enabled or self.mode == "off" or not self.url:
            return False
        host = _host_of(url)
        if not host:
            return False
        for suffix in self.no_proxy_hosts:
            if host == suffix or host.endswith("." + suffix):
                return False
        if self.mode == "all":
            return True
        if self.mode == "bypass":
            # 直连名单已经在上面的循环里排除了, 剩下的全走代理。
            # 这样新增数据源 / 换 CDN 都不用再维护名单 —— 之前就是漏了
            # lain.bgm.tv(bgm 的封面 CDN), 导致封面全部直连超时 43 秒。
            return True
        for suffix in self.hosts:
            if host == suffix or host.endswith("." + suffix):
                return True
        return False


@dataclass
class NeteaseConfig:
    mode: str = "auto"               # auto | builtin | api
    api_base: str = "http://127.0.0.1:3000"
    cookie: str = ""
    cookie_file: str = "data/netease_cookie.txt"
    phone: str = ""
    password: str = ""
    email: str = ""
    country_code: str = "86"
    # 行为
    like_album: bool = True          # 收藏专辑
    like_songs: bool = False         # 同时红心曲目
    playlist_prefix: str = "同人"    # 歌单名前缀
    playlist_privacy: int = 0        # 0 私密 / 10 公开
    playlist_mode: str = "per_event" # per_event | per_tag | single
    add_tracks: bool = True
    search_limit: int = 30
    search_timeout: float = 20.0
    # 防盗链 / 接口
    api_domain: str = "https://music.163.com"
    use_eapi: bool = False
    request_delay: float = 1.2       # 网易云接口调用间隔, 防止风控
    dry_run: bool = False


@dataclass
class XConfig:
    """已废弃: X (Twitter) 数据源与全部 X 相关后端已移除.

    保留这个空壳只是为了让旧的 ``config.toml`` 里残留的 ``[x]`` 段不报错,
    它在代码里不再被任何模块读取。新配置请不要再写这一段。
    """

    backends: list[str] = field(default_factory=list)
    bearer_token: str = ""
    nitter_instances: list[str] = field(default_factory=list)
    manual_dir: str = "data/x_manual"
    max_posts_per_tag: int = 200
    include_replies: bool = False
    include_retweets: bool = False
    languages: list[str] = field(default_factory=list)
    since_days: int = 120
    request_delay: float = 2.0
    save_raw: bool = True


@dataclass
class SourcesConfig:
    enabled: list[str] = field(
        default_factory=lambda: [
            "dojinmusic", "bangumi", "doujinstyle",
        ]
    )
    #: 跨源合并的严格程度:
    #:   "balanced" —— 社团相等就合并; 没有社团时只和同样没有社团的候选合并
    #:   "strict"   —— 只有两边社团都存在且相等才合并(宁可留重复, 不要并错)
    #:   "off"      —— 除了站点原生 ID, 一律不合并
    dedupe_mode: str = "balanced"
    #: 富化时要不要再拿标题去**别的**源搜一遍。
    #: 默认关闭: 又慢又容易错(而且 DoujinStyle 的 search 会为每个命中再进详情页)。
    #: 主力路径是"按原生 ID 回自己的源要详情", 那个始终开着。
    #: 采集/补全时顺带搜 B 站搬运的试听视频(接口有风控, 默认关)
    bilibili_audition: bool = False
    bilibili_budget: int = 40
    bilibili_delay: float = 2.0
    #: 从通贩页面补封面: 每轮最多抓几页 / 页间等待 / 每碟试几个页面
    shop_cover_budget: int = 200
    shop_cover_delay: float = 1.0
    shop_cover_pages: int = 2
    #: 封面比例白名单(w/h): 正方形是流媒体标准(Apple Music / Spotify / 网易云),
    #: 0.73 附近是 CD 书型(Diverse System 的 AD:TRANCE 系列实测 470x641≈0.733)。
    #: 比例落在白名单之外的图不当封面(横幅、截图、头像)。
    cover_aspect_bands: list[list[float]] = field(
        default_factory=lambda: [[0.95, 1.05], [0.70, 0.78]]
    )
    #: 封面最短边像素下限 —— 再小就是图标/缩略图
    cover_min_side: int = 300
    #: 不作为封面来源的数据源。dojin-music 的 ``jacket`` 是站内缩略图, 经常不是
    #: 这张碟的封面(它的**特设页**封面仍然算, 见 dojinmusicinfo 的 special page 分支)。
    cover_denied_sources: list[str] = field(default_factory=lambda: ["dojinmusic"])
    enrich_cross_search: bool = False
    #: 富化时先把 dojin-music(一手来源)的同展会内容并进已有专辑。
    #: 从 DoujinStyle / bgm.tv 抓到的专辑不会自动带特设页 / 试听链接 / 展位号,
    #: 这一步负责回填, 并补齐只有 dojin-music 登记了的 CD。
    link_dojinmusic: bool = True
    #: 富化的总时间预算(秒), 0 = 不限
    enrich_time_budget: float = 300.0
    per_source: dict[str, dict[str, Any]] = field(default_factory=dict)

    def options(self, source: str) -> dict[str, Any]:
        return self.per_source.get(source, {}) or {}

    def opt(self, source: str, key: str, default: Any = None) -> Any:
        return self.options(source).get(key, default)


@dataclass
class EventConfig:
    year: int = 0                    # 0 = 自动按当前日期推断
    kinds: list[str] = field(default_factory=lambda: ["m3", "comiket"])
    extra_tags: list[str] = field(default_factory=list)
    # 兼容标签写法
    tag_variants: bool = True
    include_generic: bool = True
    # 发行日窗口: 只有落在这个范围内的专辑才会被打上该展会的标签。
    # 同人专辑一般在展会当天首发, 但也常见"会前通贩 / 事后补货", 所以两边留余量。
    release_window_before_days: int = 45
    release_window_after_days: int = 21
    # True = 发行日明确不在窗口内时直接丢弃该线索(避免把已发售多年的老碟归到新展会)
    reject_mismatched_release: bool = True


@dataclass
class MatchConfig:
    min_score: float = 0.72          # 低于此分不算匹配
    auto_score: float = 0.88         # 高于此分自动执行收藏
    #: 艺人地板: 专辑与候选**都有**艺人/社团时, 艺人相似度低于此值直接判负。
    #: 防止 "Deeper, Deeper" 这种常见标题匹配到网易云上完全无关的同名歌曲。
    min_artist_score: float = 0.6
    #: **深度 1 快速通道**: 标题近乎逐字一致 **且** 发行年份一致时直接判定, 跳过
    #: 艺人检索与曲目表(每张能省 5~9 个请求, 是整轮匹配里最值得省的部分)。
    #: 达不到就走深度 2/3(艺人 + 曲目表)兜底。
    fast_accept: bool = True
    fast_accept_title: float = 0.97
    #: **负缓存有效期(天)**: 网易云确认"没有这个艺人/社团"之后记住多久。
    #: 必须是**带时效**的 —— 网易云的艺人条目是用户随时创建的, 今天没有不代表
    #: 下个月还没有; 永久缓存会让我们永久错过后来才出现的条目。0 = 不用负缓存。
    #: 匹配成功后用网易云条目**补库里空缺的字段**(发行日/社团/艺人/曲目表)
    backfill_from_match: bool = True
    #: 封面不同时以网易云为准; 但库里的明显更高清就保留(比较分辨率)
    cover_prefer_netease: bool = True
    #: 匹配分数达到这个值才允许用网易云的图**替换**数据源已有的封面。
    #: 低分匹配换封面 = 用可能错配的专辑图覆盖掉正确的原图(实测踩过 vol.2 -> vol.1)。
    cover_replace_min_score: float = 0.9
    #: **标题硬底线**: 标题分低于它就判负, 艺人/曲目都救不回来。
    #: 防的是"同艺人不同专辑"——旧加权允许 0.65*0.375 + 0.25*1.0 凑过阈值。
    title_hard_min: float = 0.5
    #: **曲目表硬底线**: 两边曲目数都 ≥ track_hard_min_count 且重合率低于它就判负。
    #: 实测 30 张真实专辑里, 所有正确匹配的重合度都 ≥0.8, 落在 0.2~0.3 的正是误配。
    track_hard_min_overlap: float = 0.25
    #: "两边都成规模"的曲目数门槛(低于此数不做曲目判据, 免得单曲碟被误杀)
    track_hard_min_count: int = 3
    #: 两边都有年份但不相等时的折扣 —— 使其难以越过 auto_score。
    #: 0.75 不是拍的: 用全库 372 条匹配统计, 差一年的只有 10 条;
    #: 0.75 只多丢 1 条(「紙上文学 - 乱丁版 -」→「紙上文学」, 本来就是另一版本),
    #: 0.70 会丢 4 条、其中两条三个维度全满(多半只是"我们记展会日 / 网易云记发行日")。
    #: 效果: 证据全满的差一年候选 1.000 → 0.75, 低于自动线 0.88, 必然要人工确认。
    year_gap_penalty: float = 0.75
    #: **曲目数量级硬底线**: 候选曲目数 ≥ 我们 × ratio **且** ≥ 我们 + gap 时判负。
    #: 为什么必须另比条数: `track_overlap` 按**较小一侧**归一, 所以"我们这张的每首
    #: 都在对方那张里"会得 1.0 —— 但**包含 ≠ 相同**(一张 20 首合集把 5 首的碟整个收进去,
    #: 两张碟差一个量级)。只看重合率永远发现不了。两个条件都成立才判负,
    #: 所以 10→12 这类多一两首 bonus 不会被误杀。
    track_count_ratio: float = 2.0
    track_count_min_gap: int = 5
    #: **浅层二次验证**: 年份允许的差值 —— 但**仅当曲目表已验证通过**时才放宽到它,
    #: 否则要求年份一致。防的是"同名不同碟"被年份近似放过。
    fast_accept_year_gap: int = 1
    #: 浅层最多为几个候选做二次验证(每个候选多 1 次拉曲目表请求)。
    #: 实测浅层命中约 2 次网络调用, 仍远低于深层的 6.5~11 次。
    fast_accept_verify_limit: int = 3
    negative_ttl_days: float = 14.0
    #: 最多为多少个候选去拉曲目表(那一刻是匹配里最贵的网络调用之一, 约占 20%)。
    #: 另外: 标题+艺人分已经注定过不了 min_score 的候选会**直接跳过**, 不为它花请求。
    track_probe_limit: int = 8
    #: 最多用几个关键词做检索。**实测**: 在 24 张未匹配专辑上, 命中位置分布是
    #: ``{1: 5, 2: 4}`` —— 前两个关键词解决了 100% 的命中, 第 3~6 个一个都没命中过,
    #: 而 ``keywords()`` 平均生成 3.9 个, 等于一半检索请求白花。故默认只搜前 2 个。
    keyword_search_limit: int = 2
    #: **艺人优先**: 先跑艺人专辑路径, 若它已经给出 >= min_score 的候选就跳过关键词检索。
    #: 实测(15 张已匹配专辑)艺人路径达标的 10 张里, 关键词检索 **10/10 都没给出更高分**;
    #: 但艺人路径失败的 5 张里有 2 张靠关键词以 1.000 救回 —— 所以关键词检索必须保留,
    #: 只是不该在艺人路径已经赢的时候还跑。
    artist_first: bool = True
    #: **按艺人专辑匹配**: 先把社团/艺人定位到网易云条目, 再在他的作品列表里找这张专辑。
    #: 比拿专辑标题全站搜索精确得多(后者会被大量同名歌曲干扰), 而且一次就能覆盖
    #: 该社团的全部作品。实测这是同人专辑最可靠的匹配路径。
    use_artist_search: bool = True
    artist_search_limit: int = 10
    artist_album_limit: int = 50
    #: 艺人名字相似度低于此值就不认这个人(认错人比找不到更糟)
    artist_min_similarity: float = 0.82
    require_track_overlap: bool = False
    min_track_overlap: float = 0.5
    artist_weight: float = 0.25
    title_weight: float = 0.65
    track_weight: float = 0.10
    use_album_search: bool = True


@dataclass
class Config:
    general: GeneralConfig = field(default_factory=GeneralConfig)
    http: HttpConfig = field(default_factory=HttpConfig)
    proxy: ProxyConfig = field(default_factory=ProxyConfig)
    netease: NeteaseConfig = field(default_factory=NeteaseConfig)
    x: XConfig = field(default_factory=XConfig)
    sources: SourcesConfig = field(default_factory=SourcesConfig)
    events: EventConfig = field(default_factory=EventConfig)
    match: MatchConfig = field(default_factory=MatchConfig)
    public: PublicConfig = field(default_factory=PublicConfig)
    path: Path = field(default_factory=Path.cwd)
    config_file: Path | None = None

    # -- 路径 ---------------------------------------------------------------

    @property
    def root(self) -> Path:
        return self.path

    def _resolve(self, value: str) -> Path:
        p = Path(value)
        return p if p.is_absolute() else (self.root / p)

    @property
    def data_dir(self) -> Path:
        return self._resolve(self.general.data_dir)

    @property
    def cache_dir(self) -> Path:
        return self._resolve(self.general.cache_dir)

    @property
    def reports_dir(self) -> Path:
        return self._resolve(self.general.reports_dir)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "dnh.sqlite3"

    @property
    def cookie_path(self) -> Path:
        return self._resolve(self.netease.cookie_file)

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.cache_dir, self.reports_dir, self.data_dir / "covers"):
            d.mkdir(parents=True, exist_ok=True)

    # -- 载入 ---------------------------------------------------------------

    @classmethod
    def default(cls, root: Path | str | None = None) -> "Config":
        root_path = Path(root).resolve() if root else Path.cwd()
        return cls(path=root_path)

    @classmethod
    def load(cls, path: Path | str | None = None, *, root: Path | str | None = None) -> "Config":
        """载入配置.

        ``path`` 指向 TOML 文件; 为 None 时按 root/config/config.toml 查找.
        """
        root_path = Path(root).resolve() if root else Path.cwd()
        cfg_file: Path | None = None
        if path is not None:
            cfg_file = Path(path)
            if not cfg_file.is_absolute():
                cfg_file = (root_path / cfg_file).resolve()
            if not cfg_file.exists():
                raise ConfigError(f"配置文件不存在: {cfg_file}")
        else:
            candidate = root_path / DEFAULT_CONFIG_RELPATH
            if candidate.exists():
                cfg_file = candidate

        data: dict[str, Any] = {}
        if cfg_file is not None:
            try:
                with cfg_file.open("rb") as fh:
                    data = tomllib.load(fh)
            except tomllib.TOMLDecodeError as exc:
                raise ConfigError(f"TOML 解析失败 {cfg_file}: {exc}") from exc
            root_path = cfg_file.parent.parent if cfg_file.parent.name == "config" else cfg_file.parent

        cfg = cls.from_dict(data)
        cfg.path = root_path
        cfg.config_file = cfg_file

        # .env 与进程环境变量覆盖
        env = _load_env_files(root_path)
        env.update({k: v for k, v in os.environ.items() if k.startswith("DNH_")})
        _apply_env(cfg, env)
        return cfg

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Config":
        cfg = cls()
        for f in fields(cls):
            if f.name in ("path", "config_file"):
                continue
            section = data.get(f.name)
            if section is None:
                continue
            current = getattr(cfg, f.name)
            if is_dataclass(current) and isinstance(section, Mapping):
                _update_dataclass(current, section)
        return cfg

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in fields(self):
            if f.name in ("path", "config_file"):
                continue
            value = getattr(self, f.name)
            out[f.name] = asdict(value) if is_dataclass(value) else value
        return out

    def to_toml(self) -> str:
        return dumps_toml(self.to_dict())

    # -- 便捷 ---------------------------------------------------------------

    def source_enabled(self, name: str) -> bool:
        return name in self.sources.enabled

    def effective_cookie(self) -> str:
        if self.netease.cookie:
            return self.netease.cookie.strip()
        p = self.cookie_path
        if p.exists():
            return p.read_text(encoding="utf-8").strip()
        return ""

    def save_source_options(self, source: str, **pairs: Any) -> Path:
        """把若干选项写回 ``config.toml`` 的 ``[sources.per_source.<source>]`` 段.

        为什么是**逐行改**而不是整份重写: ``config.toml`` 里有大量注释是给人看的,
        用 :meth:`to_toml` 重新序列化会把注释全丢掉。这里只动指定键对应的行,
        其它内容(包括注释、其它源、其它段)原样保留。

        段不存在时会追加; 键不存在时会插到段首。
        """
        # ``self.path`` 是**工作根目录**, 真正的配置文件在 ``self.config_file``
        # (用 ``--config`` 指定时可能不在默认位置)。
        path = getattr(self, "config_file", None) or (self.path / DEFAULT_CONFIG_RELPATH)
        section = f"[sources.per_source.{source}]"
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        lines = text.splitlines()

        def render(key: str, value: Any) -> str:
            if isinstance(value, bool):
                literal = "true" if value else "false"
            elif isinstance(value, (int, float)):
                literal = str(value)
            else:
                escaped = str(value)
                # **换行/回车/制表必须转义** —— 直接写进去会产出非法 TOML,
                # 而 Config.load 一旦解析失败, 整个程序就起不来(实测踩到过)。
                escaped = (escaped.replace("\\", "\\\\").replace('"', '\\"')
                           .replace("\n", "\\n").replace("\r", "\\r")
                           .replace("\t", "\\t"))
                literal = f'"{escaped}"'
            return f"{key} = {literal}"

        # 定位段范围
        start = end = -1
        for i, line in enumerate(lines):
            if line.strip() == section:
                start = i
                for j in range(i + 1, len(lines)):
                    stripped = lines[j].strip()
                    if stripped.startswith("[") and stripped.endswith("]"):
                        end = j
                        break
                if end < 0:
                    end = len(lines)
                break

        if start < 0:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append(section)
            for key, value in pairs.items():
                lines.append(render(key, value))
        else:
            for key, value in pairs.items():
                found = -1
                for j in range(start + 1, end):
                    if lines[j].strip().startswith(f"{key} =") or lines[j].strip().startswith(f"{key}="):
                        found = j
                        break
                if found >= 0:
                    lines[found] = render(key, value)     # 行尾注释一并丢弃(值变了注释会误导)
                else:
                    lines.insert(start + 1, render(key, value))
                    start += 1
                    end += 1

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def save_cookie(self, cookie: str) -> Path:
        self.ensure_dirs()
        p = self.cookie_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(cookie.strip(), encoding="utf-8")
        return p


# --------------------------------------------------------------------------
# 辅助
# --------------------------------------------------------------------------


def _host_of(url: str) -> str:
    from urllib.parse import urlsplit

    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _update_dataclass(obj: Any, data: Mapping[str, Any], *, strict: bool = False) -> None:
    valid = {f.name for f in fields(obj)}
    for key, value in data.items():
        if key not in valid:
            if strict:
                raise ConfigError(f"未知配置项: {type(obj).__name__}.{key}")
            continue
        current = getattr(obj, key)
        if is_dataclass(current) and isinstance(value, Mapping):
            _update_dataclass(current, value, strict=strict)
        else:
            setattr(obj, key, value)


_ENV_MAP: dict[str, tuple[str, str]] = {
    "DNH_DATA_DIR": ("general", "data_dir"),
    "DNH_REPORTS_DIR": ("general", "reports_dir"),
    "DNH_LOG_LEVEL": ("general", "log_level"),
    "DNH_HTTP_TIMEOUT": ("http", "timeout"),
    "DNH_HTTP_CACHE_TTL": ("http", "cache_ttl"),
    "DNH_HTTP_CONTACT": ("http", "contact"),
    "DNH_HTTP_USER_AGENT": ("http", "user_agent"),
    "DNH_HTTP_RESPECT_ROBOTS": ("http", "respect_robots"),
    "DNH_HTTP_ROBOTS_OVERRIDE_HOSTS": ("http", "robots_override_hosts"),
    "DNH_PROXY_URL": ("proxy", "url"),
    "DNH_PROXY_ENABLED": ("proxy", "enabled"),
    "DNH_PROXY_MODE": ("proxy", "mode"),
    "DNH_PROXY_HOSTS": ("proxy", "hosts"),
    "DNH_NETEASE_MODE": ("netease", "mode"),
    "DNH_NETEASE_API_BASE": ("netease", "api_base"),
    "DNH_NETEASE_COOKIE": ("netease", "cookie"),
    "DNH_EVENT_YEAR": ("events", "year"),
    "DNH_MIN_SCORE": ("match", "min_score"),
    "DNH_AUTO_SCORE": ("match", "auto_score"),
    # 公开端部署(免费 VPS 路线)
    "DNH_PUBLIC_SESSIONS_DB": ("public", "sessions_db"),
    "DNH_PUBLIC_BASE_URL": ("public", "base_url"),
    "DNH_PUBLIC_BEHIND_PROXY": ("public", "behind_proxy"),
    "DNH_PUBLIC_RATE_LIMIT_ENABLED": ("public", "rate_limit_enabled"),
    "DNH_PUBLIC_RATE_LIMIT_RATE": ("public", "rate_limit_rate"),
    "DNH_PUBLIC_RATE_LIMIT_BURST": ("public", "rate_limit_burst"),
    "DNH_PUBLIC_RATE_LIMIT_HEAVY_BURST": ("public", "rate_limit_heavy_burst"),
    "DNH_PUBLIC_RATE_LIMIT_WRITE_BURST": ("public", "rate_limit_write_burst"),
}

_BOOL_KEYS = {"enabled", "dry_run", "save_raw", "tag_variants", "include_generic",
              "use_eapi", "verify_tls", "use_album_search", "require_track_overlap",
              "reject_mismatched_release", "merge_across_sources", "enrich_cross_search",
              "respect_robots", "behind_proxy", "rate_limit_enabled"}


def _coerce(attr: str, raw: str) -> Any:
    if attr in _BOOL_KEYS:
        return raw.strip().lower() in ("1", "true", "yes", "on", "y")
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        return [p.strip().strip("'\"") for p in inner.split(",") if p.strip()] if inner else []
    try:
        if "." in raw:
            return float(raw)
        return int(raw)
    except ValueError:
        return raw


def _apply_env(cfg: Config, env: Mapping[str, str]) -> None:
    for key, raw in env.items():
        target = _ENV_MAP.get(key.upper())
        if not target or raw is None:
            continue
        section, attr = target
        obj = getattr(cfg, section, None)
        if obj is None:
            continue
        setattr(obj, attr, _coerce(attr, str(raw)))
    # 数据源专有 token: DNH_SOURCE_<NAME>_<KEY>
    for key, raw in env.items():
        up = key.upper()
        if not up.startswith("DNH_SOURCE_"):
            continue
        rest = up[len("DNH_SOURCE_"):]
        if "_" not in rest:
            continue
        name, opt = rest.split("_", 1)
        cfg.sources.per_source.setdefault(name.lower(), {})[opt.lower()] = _coerce(opt.lower(), str(raw))


def _load_env_files(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for candidate in (root / ".env", root / "config" / ".env"):
        if not candidate.exists():
            continue
        try:
            for line in candidate.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                out[k.strip()] = v
        except OSError:
            continue
    return out


# --------------------------------------------------------------------------
# 极简 TOML 序列化(只用于生成示例配置)
# --------------------------------------------------------------------------


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        inner = ", ".join(_toml_value(v) for v in value)
        return f"[{inner}]"
    if isinstance(value, dict):
        inner = ", ".join(f"{k} = {_toml_value(v)}" for k, v in value.items())
        return "{ " + inner + " }"
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def dumps_toml(data: Mapping[str, Any], *, _prefix: str = "") -> str:
    lines: list[str] = []
    scalars: list[tuple[str, Any]] = []
    tables: list[tuple[str, Any]] = []
    for k, v in data.items():
        if isinstance(v, Mapping) and not _is_flat_mapping(v):
            tables.append((k, v))
        else:
            scalars.append((k, v))
    for k, v in scalars:
        lines.append(f"{k} = {_toml_value(v)}")
    for k, v in tables:
        header = f"{_prefix}{k}" if not _prefix else f"{_prefix}.{k}"
        if lines:
            lines.append("")
        lines.append(f"[{header}]")
        lines.append(dumps_toml(v, _prefix=header).strip("\n"))
    return "\n".join(lines) + "\n"


def _is_flat_mapping(value: Mapping[str, Any]) -> bool:
    """字典里全是标量 -> 用内联表; 否则展开成子表."""
    return all(not isinstance(v, Mapping) for v in value.values()) and len(value) <= 4


EXAMPLE_CONFIG_TEXT = """\
# 同人音乐自动检索 / 网易云匹配 — 配置文件
# (收藏专辑 / 歌单 / 登录等写操作已移除, 只做检索与匹配)
# 所有项均可省略, 省略则使用内置默认值。
# 也可用环境变量覆盖, 例如 DNH_NETEASE_COOKIE / DNH_X_BEARER_TOKEN / DNH_PROXY_URL

[general]
data_dir = "data"
cache_dir = "data/cache"
reports_dir = "reports"
log_level = "INFO"
locale = "zh"
timezone = "Asia/Shanghai"

[http]
timeout = 25.0
retries = 3
cache_ttl = 86400          # 页面缓存秒数, 0 = 关闭
rate_limit = 1.0           # 同一域名最小请求间隔(秒)
verify_tls = true
# 署名联系地址: 会拼进默认 UA `dnh-harvester/<版本> (+<这里>)`。
# 站长能看到我们是谁、怎么联系 —— 比伪装成浏览器诚实, 也更容易被放行。
contact = ""
# 需要浏览器 UA 的 host(cf_clearance 与 UA 绑定); 其余站点一律用可识别 UA
browser_ua_hosts = ["vgmdb.net", "www.vgmdb.net"]
# robots.txt: 遵守各站点的抓取许可(默认 true, 不要关)。host 每 24 小时只取一次。
respect_robots = true
robots_cache_ttl = 86400
# 判定严格按 RFC 9309: 2xx 按规则办; 4xx(含 401/403/404) 属于 "Unavailable"
# -> 视为不限制; 5xx / 网络故障 / 429 属于 "Unreachable" -> 暂时假设全站禁止,
# 但只记 robots_error_ttl(默认 300 秒), 免得一次抖动让某站一整天都不能采。
# 豁免名单(**显式授权的逃生舱**), 后缀匹配。默认只含 VGMdb:
# 用户在浏览器通过 Cloudflare 验证后明确授权我们带着自己的 cf_clearance 访问,
# 一刀切会直接打断现有采集。每次跳过都会打 warning。想彻底合规就清空这里。
robots_override_hosts = ["vgmdb.net", "www.vgmdb.net"]

[public]
# --- 用户端站点(dnh public)部署到公网的旋钮 ---
# 会话库: 重启不掉线(空串 = 内存会话, 只有测试用)
sessions_db = "data/sessions.sqlite3"
# 站点对外地址; 留空则按请求的 X-Forwarded-Proto + Host 自动拼
base_url = ""
# 反代(Caddy/Nginx)后面才打开 —— 否则别人可以伪造 IP 绕过限速
behind_proxy = false
# 按 IP 令牌桶限速; 超限返回 429 + Retry-After
rate_limit_enabled = true
rate_limit_rate = 10.0            # 浏览页: 每秒补充令牌数
rate_limit_burst = 30.0           # 浏览页: 桶容量
rate_limit_heavy_rate = 10.0      # 封面/静态资源
rate_limit_heavy_burst = 120.0
rate_limit_search_rate = 2.0      # 检索(/search、/albums?q=)
rate_limit_search_burst = 20.0
rate_limit_write_rate = 0.25      # 写接口(登录/注册/编辑)
rate_limit_write_burst = 5.0
# sitemap 单文件 URL 上限(超出自动切成 sitemap 索引 + 分片)
sitemap_max_urls = 2000

[proxy]
enabled = true
url = "http://127.0.0.1:7897"
mode = "hosts"             # hosts = 只代理下面 hosts 中的站点; all = 全部; off = 关闭
hosts = [
  "doujinstyle.com", "dojin-music.info", "bgm.tv", "api.bgm.tv", "lain.bgm.tv",
  "vgmdb.net", "dizzylab.net", "api.dizzylab.net",
  "bandcamp.com", "melonbooks.co.jp", "toranoana.jp", "pixiv.net",
  "youtube.com", "soundcloud.com",
]
no_proxy_hosts = ["music.163.com", "163.com", "126.net", "netease.com", "localhost", "127.0.0.1"]

[netease]
mode = "auto"              # auto | builtin | api
api_base = "http://127.0.0.1:3000"   # mode=api 或本机已部署 NeteaseCloudMusicApi 时使用
cookie = ""                # 可选: 带上登录态能提高检索配额; 不填也能匿名检索
cookie_file = "data/netease_cookie.txt"
search_limit = 30
request_delay = 1.2
# 注意: 收藏专辑 / 新建歌单 / 批量加入曲目 / 红心 / 扫码登录 已按要求移除。
# 本程序只做「检索 + 匹配打分」, 匹配结果写进本地库, 供报告与 Web 面板查看,
# 由你自己在网易云 App 里手动加歌单。


[sources]
enabled = [
  "dojinmusic",    # 一手: 展会社团名单 + CD 详情 + 特设页试听
  "bangumi",       # 同人音乐小组「信息收集贴」贴主的收藏(带 M3-58 这类展会标签)
  "doujinstyle",   # 次要来源: 第三方转载站, 只作「这张碟是否已流出」的旁证
  # "vgmdb",       # 实测直连与代理都是 Cloudflare 403, 当前网络下不可用; 换网络可打开
  # "dizzylab",    # 中国同人专辑平台, 默认不参与 M3/Comiket 采集, 需要单独爬时再打开
]

# 跨源合并的严格程度(标题相同但社团不同的两张碟要不要并成一条):
#   "balanced" 社团相等就合并; 没有社团时只和同样没有社团的候选合并(默认)
#   "strict"   只有两边社团都存在且相等才合并 —— 宁可留重复, 也不要并错
#   "off"      除站点原生 ID 外一律不合并
dedupe_mode = "balanced"

# 封面比例白名单(w/h): 正方形是流媒体标准, 0.73 附近是 CD 书型
# (Diverse System 的 AD:TRANCE 系列实测 470x641≈0.733)。白名单外的图不当封面。
cover_aspect_bands = [[0.95, 1.05], [0.70, 0.78]]
cover_min_side = 300
# 不作为封面来源的数据源: dojin-music 的站内 jacket 常是社团主页图/占位图。
# 它的**特设页**封面仍然有效(那是明确挂着这张碟的页面)。
cover_denied_sources = ["dojinmusic"]


[sources.per_source.vgmdb]
# 展会名 -> VGMdb event id, 例: event_ids = { "M3-2026秋" = 366 }
delay = 2.0
# 该源已获用户**显式授权**(见 [http] robots_override_hosts): 单独豁免 robots 检查。
# 想彻底合规: 把这里改成 true, 并清空 [http] robots_override_hosts。
respect_robots = false
# VGMdb 挂在 Cloudflare 后面, 匿名请求会被 **JS 挑战** 拦下(HTTP 403 + "Just a moment...")。
# 纯 HTTP 客户端过不了 JS 挑战, 唯一不引入新依赖的办法是借用浏览器已有的 cf_clearance:
#   1) 浏览器打开 https://vgmdb.net/ 并通过验证
#   2) F12 -> Application/存储 -> Cookies -> 复制 cf_clearance 的值
#   3) 把**当时的 User-Agent** 也一起填进来(必须与申请 cookie 时一致, 否则照样被拦)
# cookie 会过期(几小时到几天), 过期后重新复制一次即可。
cookie = ""
user_agent = ""

[sources.per_source.doujinstyle]
delay = 1.2
fetch_details = true        # 进详情页才能拿到标题/社团; 关掉只能拿到 id 与封面
max_details = 200           # 每次运行最多进多少张详情页
max_pages = 8
# ⚠ doujinstyle.com 的 robots.txt 是 "User-agent: * / Disallow: /"(**全站禁止**)。
# 默认 respect_robots = true, 所以本源会被 robots 拦住(用 `dnh robots --audit` 可复核)。
# 要恢复采集, 需要你**显式**豁免它(去掉下面这行的注释):
# respect_robots = false

[sources.per_source.bangumi]
# 从哪里找该展会的专辑:
#   1) 小组 /group/doujinmusic 里标题匹配的信息收集贴 -> 自动取贴主
#   2) 也可以在这里写死 bgm 用户名(留空则自动发现)
# 数据走用户**公开收藏** /v0/users/<user>/collections, 用展会标签(M3-58)本地过滤。
# 注意: 帖子里的 /index/<id> 目录实测对匿名访客不可读(「数据库中没有查询到该目录的信息」),
# 所以这里不用目录, 改用收藏接口。
collectors = []
use_collections = true
collection_max_pages = 8    # 收藏按更新倒序, 连续 2 页无命中就提前停
delay = 1.0

[events]
year = 0                   # 0 = 按当前日期自动推断
kinds = ["m3", "comiket"]
tag_variants = true
extra_tags = []
# 发行日窗口: 只有落在这个范围内的专辑才会被打上该展会的标签
release_window_before_days = 45
release_window_after_days = 21
reject_mismatched_release = true

[match]
min_score = 0.72           # 低于此分不认为匹配
auto_score = 0.88          # 高于此分自动执行收藏/加歌单
title_weight = 0.65
artist_weight = 0.25
track_weight = 0.10
use_album_search = true
"""


def write_example_config(path: Path | str) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(EXAMPLE_CONFIG_TEXT, encoding="utf-8")
    return p
