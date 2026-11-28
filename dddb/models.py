"""领域模型.

所有数据源适配器都必须产出 / 消费这里的 :class:`Album`, 这样跨源合并才有统一语义.
"""

from __future__ import annotations

import calendar
import dataclasses
import datetime
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Mapping, Sequence

from . import textutil


def _clean_list(values: Iterable[str] | None) -> list[str]:
    return textutil.dedupe_preserve_order(values or [])


def _clean_str(value: Any) -> str:
    if value is None:
        return ""
    return textutil.clean_whitespace(str(value))


def _clean_multiline(value: Any) -> str:
    """多行文本(简介)规范化: **保留换行**, 只压掉行内重复空白.

    ``_clean_str`` 走 ``clean_whitespace``, 那个把 ``\\s+`` 一律换成空格 ——
    简介里的段落分隔会被抹平, 详情页就再也分不出段落
    (用户 m13077: 草稿保存的简介缺少换行符 / 恢复后换行全丢)。
    """
    if value is None:
        return ""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    lines = []
    for line in text.split("\n"):
        # 行内多空白收成一个, 但**行尾行首**只在需要时折
        lines.append(re.sub(r"[ \t\u3000]+", " ", line).strip())
    # 连续空行收成一个(段与段之间仍是空行)
    out: list[str] = []
    for line in lines:
        if not line and out and not out[-1]:
            continue
        out.append(line)
    return "\n".join(out).strip()


# --------------------------------------------------------------------------
# Track
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Track:
    """单曲目."""

    title: str = ""
    index: int = 1
    disc: int = 1
    artists: list[str] = field(default_factory=list)
    duration: int | None = None  # 秒
    source: str = ""
    # 编辑界面的补充字段(默认空; 详情页只在有值时展示)
    subtitle: str = ""
    title_zh: str = ""
    lang: str = ""
    genre: str = ""

    def __post_init__(self) -> None:
        self.title = _clean_str(self.title)
        self.artists = _clean_list(self.artists)
        self.subtitle = _clean_str(self.subtitle)
        self.title_zh = _clean_str(self.title_zh)
        self.lang = _clean_str(self.lang)
        self.genre = _clean_str(self.genre)

    @property
    def duration_text(self) -> str:
        return textutil.format_duration(self.duration)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "index": self.index,
            "disc": self.disc,
            "artists": list(self.artists),
            "duration": self.duration,
            "source": self.source,
            "subtitle": self.subtitle,
            "title_zh": self.title_zh,
            "lang": self.lang,
            "genre": self.genre,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Track":
        return cls(
            title=_clean_str(data.get("title")),
            index=int(data.get("index") or 1),
            disc=int(data.get("disc") or 1),
            artists=list(data.get("artists") or []),
            duration=data.get("duration"),
            source=_clean_str(data.get("source")),
            subtitle=_clean_str(data.get("subtitle")),
            title_zh=_clean_str(data.get("title_zh")),
            lang=_clean_str(data.get("lang")),
            genre=_clean_str(data.get("genre")),
        )


# --------------------------------------------------------------------------
# Credit
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Credit:
    """社团 / 艺人 / 制作人员.

    ``original`` / ``alias`` / ``raw_role`` 是**可选**的补充信息,
    主要来自歌词解析(见 :mod:`dddb.lyrics_credits`):

        name      中文名        许笑男
        original  原名/罗马字    Xuanan Xu
        alias     括号里的别名   (HOYO-MIX)
        raw_role  歌词原文职位   钢琴 Piano

    旧记录没有这些键, 读出来是空串; ``to_dict()`` 也不输出空值,
    所以库里的 JSON 不会因这次改动变大。
    """

    name: str = ""
    role: str = "artist"  # circle | artist | vocal | arrange | lyric | compose | illustrator | ...
    source: str = ""
    original: str = ""
    alias: str = ""
    raw_role: str = ""

    def __post_init__(self) -> None:
        self.name = _clean_str(self.name)
        self.role = _clean_str(self.role) or "artist"
        self.original = _clean_str(self.original)
        self.alias = _clean_str(self.alias)
        self.raw_role = _clean_str(self.raw_role)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name, "role": self.role, "source": self.source,
        }
        # 只输出有值的可选字段 —— 让旧数据保持原样
        for key in ("original", "alias", "raw_role"):
            val = getattr(self, key, "")
            if val:
                out[key] = val
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Credit":
        return cls(
            name=_clean_str(data.get("name")),
            role=_clean_str(data.get("role")) or "artist",
            source=_clean_str(data.get("source")),
            original=_clean_str(data.get("original")),
            alias=_clean_str(data.get("alias")),
            raw_role=_clean_str(data.get("raw_role")),
        )


# --------------------------------------------------------------------------
# Circle(社团)
# --------------------------------------------------------------------------

#: 社团主要活动类型 —— 东方 / 原创 / 两者 / **留空**(未填)。
#: 存的是**规范化后的英文 key**, 界面负责翻译成中文; 这样以后要加别的阵营
#: (舰娘 / VOCALOID ...) 只加一个 key, 不用改库里已有的数据。
CIRCLE_ACTIVITY_TYPES = ("touhou", "original", "both", "")

CIRCLE_ACTIVITY_LABELS: dict[str, str] = {
    "touhou": "东方",
    "original": "原创",
    "both": "两者",
    "": "未填",
}

#: 用户可能怎么写就怎么认(中文 / 英文 / 日文 / 带空格的大小写都行)
_CIRCLE_ACTIVITY_ALIASES: dict[str, str] = {
    "touhou": "touhou", "touhou project": "touhou", "touhouproject": "touhou",
    "东方": "touhou", "東方": "touhou", "东方project": "touhou", "東方project": "touhou",
    "东方project arranger": "touhou", "touhou arrange": "touhou", "とうほう": "touhou",
    "original": "original", "オリジナル": "original",
    "原创": "original", "原創": "original", "自创": "original",
    "both": "both", "两者": "both", "兩者": "both", "都有": "both", "混合": "both",
    "东方+原创": "both", "东方/原创": "both", "touhou/original": "both",
    "touhou+original": "both", "touhou & original": "both",
    "none": "", "unknown": "", "未知": "", "未填": "", "-": "", "其他": "",
}

#: 人员角色: 主催(owner) / 成员(member)。
CIRCLE_STAFF_ROLES = ("owner", "member")

CIRCLE_STAFF_LABELS: dict[str, str] = {"owner": "主催", "member": "成员"}

_CIRCLE_STAFF_ALIASES: dict[str, str] = {
    "owner": "owner", "主催": "owner", "主宰": "owner", "代表": "owner",
    "circle owner": "owner", "leader": "owner", "主宰者": "owner",
    "member": "member", "成员": "member", "成員": "member", "メンバー": "member",
    "staff": "member", "参加者": "member",
}

#: 「目前活跃」的阈值(天)。
#: 同人社团的节奏是「一年两次 M3」再叠上 Comiket / 例大祭, 常年两届起步;
#: 18 个月(≈540 天)没有新作, 基本可以认为这个社团停更了。
#: 阈值定太短会把"憋大招 / 只跑夏季 Comiket"的社团误判成沉寂, 定太长则列表里
#: 看不出谁已经不动了 —— 540 天是实测下来两边都还能接受的值。
CIRCLE_ACTIVE_WINDOW_DAYS = 540


def normalize_staff_role(value: Any) -> str:
    """把自由填写的角色归一到 ``owner`` / ``member``, 认不出来的**原样保留**.

    空值按 ``member``(成员)处理 —— 界面上新加一行人员时还没选角色, 不该直接报错。

    认不出来的词以前会抛 ``ValueError``(「非法的人员角色」), 于是用户想写
    「作曲」「编曲」这种具体职责时整个社团都保存失败(用户 m20229 要求成员
    也能自由填角色)。现在照 ``normalize_activity_type`` 的做法: 存原文,
    显示时 ``role_label`` 再兜底 —— 少一个用途标签, 总好过整次保存失败。
    """
    raw = _clean_str(value)
    if not raw:
        return "member"
    return _CIRCLE_STAFF_ALIASES.get(raw.lower(), raw)


def normalize_activity_type(value: Any) -> str:
    """把自由填写的主要活动类型归一到 :data:`CIRCLE_ACTIVITY_TYPES` 里的 key.

    认不出来的写法**原样保留**, 不再抛异常(用户 m19090): 新建社团页以前的
    提示写的是「同人社团 / 企业 等」, 用户照着填 ``同人社团`` 就撞
    ``ValueError: 无法识别的活动类型`` —— 结果是整个社团一条都没落库, 用户
    只看到"未能保存"却不知道错在哪。活动类型只是个展示标签, 存原文比让
    整次保存失败有用得多; 真正认识的写法仍然归一成 key(显示时走
    :data:`CIRCLE_ACTIVITY_LABELS`)。
    """
    raw = _clean_str(value)
    if not raw:
        return ""
    key = raw.lower().replace("＋", "+")
    if key in _CIRCLE_ACTIVITY_ALIASES:
        return _CIRCLE_ACTIVITY_ALIASES[key]
    return raw


#: 宽松日期解析: 2020 / 2020-03 / 2020/03/15 / 2020年3月 / 2020.3.15 都认。
_PARTIAL_DATE_RE = re.compile(
    r"(\d{4})\s*[-/.年]\s*(\d{1,2})(?:\s*[-/.月]\s*(\d{1,2}))?"
)
_PARTIAL_YEAR_RE = re.compile(r"(\d{4})\s*年?")
#: 全角数字与全角分隔符(中文输入法下很容易打出来)先换成半角再解析
_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９／．－", "0123456789/.-")


def normalize_partial_date(value: Any) -> str:
    """把成立/活动时间归一成 ``YYYY`` / ``YYYY-MM`` / ``YYYY-MM-DD``.

    宽松解析(全角数字、``年/月/日``、``/``、``.`` 都接受), 但**月份日期会校验**:
    ``2020-13`` 这种明显写错的要报错, 而不是静默截成 ``2020`` —— 静默吞掉会让用户
    以为自己填对了。
    """
    raw = _clean_str(value).translate(_FULLWIDTH_DIGITS)
    if not raw:
        return ""
    match = _PARTIAL_DATE_RE.search(raw)
    if match:
        year = int(match.group(1))
        month = int(match.group(2))
        day = int(match.group(3)) if match.group(3) else None
        if not 1 <= month <= 12:
            raise ValueError(f"月份不合法: {value!r}（月份要在 01~12 之间）")
        if day is not None and not 1 <= day <= 31:
            raise ValueError(f"日期不合法: {value!r}（日要在 01~31 之间）")
        if day is None:
            return f"{year:04d}-{month:02d}"
        return f"{year:04d}-{month:02d}-{day:02d}"
    match = _PARTIAL_YEAR_RE.fullmatch(raw)
    if match:
        return f"{int(match.group(1)):04d}"
    raise ValueError(
        f"时间格式无法识别: {value!r}（可用 2020 / 2020-03 / 2020-03-15）"
    )


def _period_end(value: str) -> datetime.date | None:
    """把 ``YYYY`` / ``YYYY-MM`` / ``YYYY-MM-DD`` 解析成**该区间的最后一天**.

    推算"多久没活动了"时必须取区间末尾: 只写到 ``2020`` 的社团, 它可能活动到
    2020-12-31 —— 拿 2020-01-01 去算会白白多算一年, 把还活着的社团判成沉寂。
    """
    text = normalize_partial_date(value)
    if not text:
        return None
    try:
        if len(text) == 4:
            return datetime.date(int(text), 12, 31)
        if len(text) == 7:
            year, month = int(text[:4]), int(text[5:7])
            return datetime.date(year, month, calendar.monthrange(year, month)[1])
        return datetime.date.fromisoformat(text)
    except ValueError:
        return None


def _as_date(value: Any) -> datetime.date:
    """把 "现在" 参数统一成 ``date``(测试里要能注入固定时间)."""
    if value is None:
        return datetime.date.today()
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, (int, float)):
        return datetime.datetime.fromtimestamp(float(value)).date()
    text = str(value)
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        parsed = _period_end(text)
        if parsed is None:
            raise ValueError(f"无法理解的日期: {value!r}") from None
        return parsed


def circle_activity_status(last_active: Any, *, now: Any = None) -> str:
    """由"上次活动时间"推算目前状态: ``活跃`` / ``沉寂`` / ``未知``.

    阈值见 :data:`CIRCLE_ACTIVE_WINDOW_DAYS`。留空 -> ``未知``(不猜)。
    """
    end = _period_end(str(last_active or ""))
    if end is None:
        return "未知"
    today = _as_date(now)
    return "活跃" if (today - end).days <= CIRCLE_ACTIVE_WINDOW_DAYS else "沉寂"


@dataclass(slots=True)
class CircleStaff:
    """社团成员: 主催(owner) 或 成员(member)."""

    name: str = ""
    role: str = "member"

    def __post_init__(self) -> None:
        self.name = _clean_str(self.name)
        if not self.name:
            raise ValueError("人员名字不能为空")
        self.role = normalize_staff_role(self.role)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "role": self.role}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CircleStaff":
        if isinstance(data, CircleStaff):
            return data
        return cls(name=_clean_str(data.get("name")),
                   role=data.get("role") or "member")


@dataclass(slots=True)
class Circle:
    """一个社团(一等公民数据; 与 ``Album.circle`` 字符串按 name 关联)."""

    name: str = ""
    #: logo 走和专辑封面一样的双字段: 上传的本地文件 -> ``logo_local``,
    #: 外部直链 -> ``logo_url``。两者都为空就是没有 logo。
    logo_url: str = ""
    logo_local: str = ""
    activity_type: str = ""             # touhou | original | both | ""
    founded: str = ""                   # YYYY | YYYY-MM | YYYY-MM-DD
    last_active: str = ""
    description: str = ""
    urls: dict[str, str] = field(default_factory=dict)   # 名称 -> url(名称允许自定义)
    staff: list[CircleStaff] = field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0

    def __post_init__(self) -> None:
        self.name = _clean_str(self.name)
        if not self.name:
            raise ValueError("社团名不能为空")
        self.logo_url = _clean_str(self.logo_url)
        self.logo_local = _clean_str(self.logo_local)
        self.activity_type = normalize_activity_type(self.activity_type)
        self.founded = normalize_partial_date(self.founded)
        self.last_active = normalize_partial_date(self.last_active)
        self.description = _clean_str(self.description)
        clean_urls: dict[str, str] = {}
        for key, value in (self.urls or {}).items():
            key_s = _clean_str(key)
            value_s = _clean_str(value)
            if key_s and value_s:
                clean_urls[key_s] = value_s
        self.urls = clean_urls
        built: list[CircleStaff] = []
        seen: set[tuple[str, str]] = set()
        for item in self.staff or []:
            person = item if isinstance(item, CircleStaff) else CircleStaff.from_dict(item)
            sig = (person.role, textutil.norm_key(person.name, romaji=False) or person.name.lower())
            if sig in seen:
                continue                # 同一角色 + 同一人只留一条(界面上重复添加不该存两份)
            seen.add(sig)
            built.append(person)
        self.staff = built

    # -- 便捷访问 -----------------------------------------------------------

    @property
    def logo(self) -> str:
        """本地文件优先(用户上传的就是最终稿), 否则用外链."""
        return self.logo_local or self.logo_url

    @property
    def activity_label(self) -> str:
        return CIRCLE_ACTIVITY_LABELS.get(self.activity_type, self.activity_type)

    def role_label(self, role: str) -> str:
        """角色的展示名: 已知的翻成中文, 自由填写的原样返回."""
        return CIRCLE_STAFF_LABELS.get(role, role)

    def staff_by_role(self, role: str) -> list[str]:
        role_key = normalize_staff_role(role)
        return [p.name for p in self.staff if p.role == role_key]

    def owners(self) -> list[str]:
        return self.staff_by_role("owner")

    def members(self) -> list[str]:
        return self.staff_by_role("member")

    def last_active_date(self) -> datetime.date | None:
        return _period_end(self.last_active)

    def days_since_active(self, *, now: Any = None) -> int | None:
        end = self.last_active_date()
        if end is None:
            return None
        return (_as_date(now) - end).days

    def activity_status(self, *, now: Any = None) -> str:
        return circle_activity_status(self.last_active, now=now)

    def is_active(self, *, now: Any = None) -> bool | None:
        status = self.activity_status(now=now)
        if status == "未知":
            return None
        return status == "活跃"

    # -- 序列化 -------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "logo_url": self.logo_url,
            "logo_local": self.logo_local,
            "activity_type": self.activity_type,
            "founded": self.founded,
            "last_active": self.last_active,
            "description": self.description,
            "urls": dict(self.urls),
            "staff": [p.to_dict() for p in self.staff],
            "created_at": float(self.created_at or 0.0),
            "updated_at": float(self.updated_at or 0.0),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Circle":
        return cls(
            name=_clean_str(data.get("name")),
            logo_url=_clean_str(data.get("logo_url")),
            logo_local=_clean_str(data.get("logo_local")),
            activity_type=data.get("activity_type") or "",
            founded=data.get("founded") or "",
            last_active=data.get("last_active") or "",
            description=_clean_str(data.get("description")),
            urls={str(k): str(v) for k, v in (data.get("urls") or {}).items()},
            staff=[CircleStaff.from_dict(p) for p in (data.get("staff") or [])],
            created_at=float(data.get("created_at") or 0.0),
            updated_at=float(data.get("updated_at") or 0.0),
        )

    @classmethod
    def from_json(cls, blob: str) -> "Circle":
        return cls.from_dict(json.loads(blob))


# --------------------------------------------------------------------------
# Album
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Album:
    """一张同人专辑(跨源合并后的聚合视图)."""

    title: str = ""
    title_original: str = ""
    title_romaji: str = ""
    circle: str = ""
    #: 全部出品社团(用户 m20229)。``circle`` 仍保留为主社团(第一个),
    #: 老代码和老数据全部按它工作; 合辑/合作碟有两个以上社团时,
    #: 以前会被 ``_clean_str`` 用空格拼成一个名字("甲 乙"), 索引里
    #: 就变成一条叫「甲 乙」的社团。这里单独留一份完整名单。
    circles: list[str] = field(default_factory=list)
    artists: list[str] = field(default_factory=list)
    release_date: str = ""          # ISO-8601 (YYYY-MM-DD 或 YYYY-MM)
    event: str = ""                 # "M3-2026秋" / "C107" / "例大祭23"
    event_kind: str = ""            # m3 | comiket | reitaisai | ...
    catalog_no: str = ""
    #: 发行公司/厂牌(网易云 album.company; 同人碟常是自己社团或委托厂牌)
    company: str = ""
    genres: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    cover_url: str = ""
    cover_local: str = ""
    #: 封面版本号(上传时刷新), 用作 URL 查询参数破 CDN 缓存
    cover_v: str = ""
    tracks: list[Track] = field(default_factory=list)
    credits: list[Credit] = field(default_factory=list)
    #: 附图(不参与封面): ``[{"url": ..., "path": ..., "note": ...}]``
    photos: list[dict[str, Any]] = field(default_factory=list)
    description: str = ""
    price: str = ""
    urls: dict[str, str] = field(default_factory=dict)       # source -> url
    source_ids: dict[str, str] = field(default_factory=dict)  # source -> native id
    raw: dict[str, Any] = field(default_factory=dict)         # source -> 原始 JSON
    confidence: float = 1.0
    #: 来源可信度 0~100(100 = 一手权威来源)。跨源合并取最大值,
    #: 用来区分「社团自己登记的一手信息」与「第三方转载的旁证」。
    trust: int = 0
    #: source -> 该来源自己的可信度。合并时逐个取最大值,
    #: 这样 :attr:`primary_source` 能回答「这条记录主要听谁的」。
    trust_by_source: dict[str, int] = field(default_factory=dict)
    #: 展会摊位列(如 ``F-07a``)。dojin-music 的 eventSpace 直接给 space_head + space_number。
    booth: str = ""
    #: 展示场(M3 的「第一展示場」「第二展示場①」等)。dojin-music 的 eventSpace.space_hall。
    #: 展位号本身(如 A-02a)只说明区域+编号, 场次要靠这个字段区分。
    hall: str = ""
    key: str = ""                    # 稳定聚合 key
    #: **人工编辑过**。置位后自动采集只补空缺字段, 不再覆盖任何已有值 ——
    #: 否则你手工改对的标题/社团, 会在下一次采集时被第三方来源改回去。
    edited: bool = False

    # -- 用户 m16568 新增 ----------------------------------------------------
    #: 各碟的名字(``{"1": "Nightmare", "2": "..."}``)。键是碟号字符串,
    #: 值是这一碟的名字; 没命名的碟不出现在这里。
    #: 用途: ① 详情页碟头显示; ② 被命名后能按这个名字搜到整张专辑
    #: (流媒体上两碟常以不同名字分别发行)。
    discs: dict[str, str] = field(default_factory=dict)
    #: 原作 IP(东方 / 蔚蓝档案 / 原神 …)。同人作品多半依附某个 IP,
    #: 单列出来比塞进 tags 更好搜。
    original_work: str = ""
    #: 制作人(同人展侧的第二个出品人字段, 与社团分开记)。
    producer: str = ""
    #: 发布平台(独立同人侧用: BOOTH / Bandcamp / Steam …)。
    platform: str = ""
    #: 出品类型: ``event``(同人展侧) / ``indie``(独立同人侧) / 空(未区分)。
    #: 编辑页据此切换「出品信息」卡片的两套字段。
    release_kind: str = ""

    def __post_init__(self) -> None:
        self.title = _clean_str(self.title)
        self.title_original = _clean_str(self.title_original)
        self.title_romaji = _clean_str(self.title_romaji)
        self.circle = _clean_str(self.circle)
        # 社团名单: 去空、去重(保持顺序); ``circle`` 一定在里面,
        # 且排在第一位 —— 只填了 circle 的老数据也自动补上。
        cleaned: list[str] = []
        for item in self.circles or []:
            text = _clean_str(item)
            if text and text not in cleaned:
                cleaned.append(text)
        if self.circle and self.circle not in cleaned:
            cleaned.insert(0, self.circle)
        if cleaned and not self.circle:
            self.circle = cleaned[0]
        self.circles = cleaned
        self.event = _clean_str(self.event)
        self.event_kind = _clean_str(self.event_kind) or event_kind(self.event)
        self.catalog_no = _clean_str(self.catalog_no)
        self.company = _clean_str(self.company)
        self.booth = _clean_str(self.booth)
        self.hall = _clean_str(self.hall)
        self.original_work = _clean_str(self.original_work)
        self.producer = _clean_str(self.producer)
        self.platform = _clean_str(self.platform)
        self.release_kind = _clean_str(self.release_kind).lower()
        if self.release_kind not in ("", "event", "indie"):
            self.release_kind = ""
        # 碟名: 键统一成字符串碟号, 值去空白, 空值丢掉
        cleaned_discs: dict[str, str] = {}
        for raw_key, raw_name in (self.discs or {}).items():
            try:
                number = int(str(raw_key).strip())
            except (TypeError, ValueError):
                continue
            name = _clean_str(raw_name)
            if number > 0 and name:
                cleaned_discs[str(number)] = name
        self.discs = cleaned_discs
        self.release_date = _clean_str(self.release_date)
        self.cover_url = _clean_str(self.cover_url)
        self.cover_local = _clean_str(self.cover_local)
        self.cover_v = _clean_str(self.cover_v)
        # 简介是唯一的多行字段: **保留换行**(段落靠空行分开), 不能用
        # _clean_str —— 那个把 \n 压成空格, 详情页就分不出段落了
        # (用户 m13077: 草稿恢复后简介的换行全丢)。
        self.description = _clean_multiline(self.description)
        self.price = _clean_str(self.price)
        self.artists = _clean_list(self.artists)
        self.genres = _clean_list(self.genres)
        self.tags = _clean_list(self.tags)
        if not self.title and self.title_original:
            self.title = self.title_original
        # 来源没显式给 trust_by_source 时, 用它自己的 trust 铺一遍 —— 这样单个适配器
        # 只要设 trust + source_ids 就够了, 不用每处都维护映射。
        if self.trust and self.source_ids and not self.trust_by_source:
            self.trust_by_source = {src: int(self.trust) for src in self.source_ids if src}
        if not self.key:
            self.key = self.compute_key()

    # -- 身份 ---------------------------------------------------------------

    def identity_titles(self) -> list[str]:
        return [t for t in (self.title, self.title_original, self.title_romaji) if t]

    def compute_key(self) -> str:
        """聚合 key: 规范化标题 + 社团 + 首发年份."""
        title = self.title_original or self.title
        base = textutil.slugify(title, max_len=60)
        circle = textutil.slugify(self.circle, max_len=30) if self.circle else ""
        year = (self.release_date or "")[:4]
        parts = [p for p in (circle, base, year) if p]
        return "|".join(parts) if parts else textutil.slugify(self.title or "untitled")

    @property
    def display_title(self) -> str:
        return self.title_original or self.title or "(untitled)"

    @property
    def primary_url(self) -> str:
        for src in ("doujinstyle", "vgmdb", "bangumi", "dizzylab", "discogs", "touhoudb", "thwiki", "dojinmusic", "vk"):
            if self.urls.get(src):
                return self.urls[src]
        return next(iter(self.urls.values()), "")

    def sources(self) -> list[str]:
        return sorted(self.source_ids.keys() or self.urls.keys())

    @property
    def primary_source(self) -> str:
        """这条记录**主要**来自哪个源(该源可信度最高).

        用于报告/面板显示 "一手来源: dojinmusic"。
        """
        if self.trust_by_source:
            return max(self.trust_by_source.items(), key=lambda kv: (int(kv[1]), kv[0]))[0]
        return next(iter(sorted(self.source_ids)), "")

    @property
    def trust_tier(self) -> str:
        """可信度分级标签(给报告与面板用)."""
        value = int(self.trust or 0)
        if value >= 90:
            return "一手来源"
        if value >= 50:
            return "权威站点"
        if value > 0:
            return "次要来源"
        return "未知来源"

    # -- 合并 ---------------------------------------------------------------

    def merge(self, other: "Album", *, prefer_other: bool = False) -> "Album":
        """把 ``other`` 的信息合并进本对象(就地修改并返回 self).

        **按来源可信度决定字段归属**(这是"一手来源说了算"的核心):

        * ``other.trust`` 更高 → 它的标题/社团/发售日/封面/曲目等**元数据字段胜出**,
          哪怕本对象已经有值。否则只补空缺。
        * 集合类字段(artists/genres/tags/urls/source_ids/credits)始终取并集。
        * ``trust`` 取最大值, ``trust_by_source`` 逐源取最大值。
        """
        other_wins = bool(prefer_other) or int(other.trust or 0) > int(self.trust or 0)
        # **人工编辑优先于任何来源可信度**: 用户改过的字段不许被自动采集覆盖
        if self.edited:
            other_wins = False

        def fill(attr: str) -> None:
            mine, theirs = getattr(self, attr), getattr(other, attr)
            if not theirs:
                return
            if not mine or other_wins:
                setattr(self, attr, theirs)

        for attr in (
            "title", "title_original", "title_romaji", "circle", "release_date",
            "event", "event_kind", "catalog_no", "company", "cover_url", "cover_local",
            "description", "price", "booth", "hall",
        ):
            fill(attr)

        self.artists = _clean_list([*self.artists, *other.artists])
        self.genres = _clean_list([*self.genres, *other.genres])
        self.tags = _clean_list([*self.tags, *other.tags])
        self.urls = {**other.urls, **self.urls}
        self.source_ids = {**other.source_ids, **self.source_ids}
        self.raw = {**other.raw, **self.raw}
        self.confidence = max(self.confidence, other.confidence)
        self.trust = max(int(self.trust or 0), int(other.trust or 0))

        # 逐源可信度: 每个来源只记得它自己声称的等级
        for source, value in (other.trust_by_source or {}).items():
            self.trust_by_source[source] = max(int(self.trust_by_source.get(source, 0)), int(value))
        if other.trust and other.source_ids:
            for source in other.source_ids:
                self.trust_by_source.setdefault(source, int(other.trust))
        if self.trust and self.source_ids:
            for source in self.source_ids:
                self.trust_by_source.setdefault(source, int(self.trust))

        # 曲目表: 可信度高的来源优先; 同级时条目多者胜
        if other.tracks and (other_wins or len(other.tracks) > len(self.tracks)):
            self.tracks = list(other.tracks)
        elif len(other.tracks) == len(self.tracks) and self.tracks:
            for a, b in zip(self.tracks, other.tracks):
                if not a.duration and b.duration:
                    a.duration = b.duration
                if not a.artists and b.artists:
                    a.artists = list(b.artists)

        # credits 去重合并
        seen = {(c.role, textutil.norm_key(c.name)) for c in self.credits}
        for c in other.credits:
            sig = (c.role, textutil.norm_key(c.name))
            if c.name and sig not in seen:
                seen.add(sig)
                self.credits.append(c)

        if not self.key:
            self.key = self.compute_key()
        return self

    # -- 便捷访问 -----------------------------------------------------------

    def circle_credits(self) -> list[Credit]:
        return [c for c in self.credits if c.role == "circle"]

    def track_titles(self) -> list[str]:
        return [t.title for t in self.tracks if t.title]

    # -- 序列化 -------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "title_original": self.title_original,
            "title_romaji": self.title_romaji,
            "circle": self.circle,
            "circles": list(self.circles),
            "artists": list(self.artists),
            "release_date": self.release_date,
            "event": self.event,
            "event_kind": self.event_kind,
            "catalog_no": self.catalog_no,
            "company": self.company,
            "genres": list(self.genres),
            "tags": list(self.tags),
            "cover_url": self.cover_url,
            "cover_local": self.cover_local,
            "cover_v": self.cover_v,
            "tracks": [t.to_dict() for t in self.tracks],
            "credits": [c.to_dict() for c in self.credits],
            "photos": [dict(p) for p in self.photos],
            "description": self.description,
            "price": self.price,
            "urls": dict(self.urls),
            "source_ids": dict(self.source_ids),
            "raw": dict(self.raw),
            "confidence": self.confidence,
            "trust": int(self.trust or 0),
            "trust_by_source": {str(k): int(v) for k, v in (self.trust_by_source or {}).items()},
            "booth": self.booth,
            "hall": self.hall,
            "discs": dict(self.discs),
            "original_work": self.original_work,
            "producer": self.producer,
            "platform": self.platform,
            "release_kind": self.release_kind,
            "edited": bool(self.edited),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Album":
        return cls(
            key=_clean_str(data.get("key")),
            title=_clean_str(data.get("title")),
            title_original=_clean_str(data.get("title_original")),
            title_romaji=_clean_str(data.get("title_romaji")),
            circle=_clean_str(data.get("circle")),
            circles=list(data.get("circles") or []),
            artists=list(data.get("artists") or []),
            release_date=_clean_str(data.get("release_date")),
            event=_clean_str(data.get("event")),
            event_kind=_clean_str(data.get("event_kind")),
            catalog_no=_clean_str(data.get("catalog_no")),
            company=_clean_str(data.get("company")),
            genres=list(data.get("genres") or []),
            tags=list(data.get("tags") or []),
            cover_url=_clean_str(data.get("cover_url")),
            cover_local=_clean_str(data.get("cover_local")),
            cover_v=_clean_str(data.get("cover_v")),
            tracks=[Track.from_dict(t) for t in (data.get("tracks") or [])],
            credits=[Credit.from_dict(c) for c in (data.get("credits") or [])],
            photos=[dict(p) for p in (data.get("photos") or []) if isinstance(p, dict)],
            description=_clean_multiline(data.get("description")),
            price=_clean_str(data.get("price")),
            urls=dict(data.get("urls") or {}),
            source_ids=dict(data.get("source_ids") or {}),
            raw=dict(data.get("raw") or {}),
            confidence=float(data.get("confidence") or 1.0),
            trust=int(data.get("trust") or 0),
            trust_by_source={str(k): int(v) for k, v in (data.get("trust_by_source") or {}).items()},
            booth=str(data.get("booth") or ""),
            hall=str(data.get("hall") or ""),
            discs=dict(data.get("discs") or {}),
            original_work=_clean_str(data.get("original_work")),
            producer=_clean_str(data.get("producer")),
            platform=_clean_str(data.get("platform")),
            release_kind=_clean_str(data.get("release_kind")),
            edited=bool(data.get("edited")),
        )

    @classmethod
    def from_json(cls, blob: str) -> "Album":
        return cls.from_dict(json.loads(blob))


def event_kind(event: str) -> str:
    """从事件字符串推断事件类型."""
    if not event:
        return ""
    low = event.lower()
    if "m3" in low:
        return "m3"
    if "comiket" in low or "コミケ" in event or low.startswith("c1") or low.startswith("c9"):
        return "comiket"
    if "例大祭" in event or "reitaisai" in low:
        return "reitaisai"
    if "紅楼夢" in event or "kouroumu" in low:
        return "kouroumu"
    return "other"


# --------------------------------------------------------------------------
# 网易云匹配结果
# --------------------------------------------------------------------------


@dataclass(slots=True)
class NeteaseSong:
    id: int = 0
    name: str = ""
    artists: list[str] = field(default_factory=list)
    album_id: int = 0
    album_name: str = ""
    duration: int | None = None
    fee: int = 0
    #: 所属专辑封面(歌单里的曲目也有, 用来在对照页显示)
    album_cover: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "NeteaseSong":
        return cls(
            id=int(data.get("id") or 0),
            name=_clean_str(data.get("name")),
            artists=list(data.get("artists") or []),
            album_id=int(data.get("album_id") or 0),
            album_name=_clean_str(data.get("album_name")),
            duration=data.get("duration"),
            fee=int(data.get("fee") or 0),
            album_cover=_clean_str(data.get("album_cover")),
        )


@dataclass(slots=True)
class NeteaseAlbum:
    id: int = 0
    name: str = ""
    artists: list[str] = field(default_factory=list)
    size: int = 0
    publish_time: int | None = None
    company: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "NeteaseAlbum":
        return cls(
            id=int(data.get("id") or 0),
            name=_clean_str(data.get("name")),
            artists=list(data.get("artists") or []),
            size=int(data.get("size") or 0),
            publish_time=data.get("publish_time"),
            company=_clean_str(data.get("company")),
        )


@dataclass(slots=True)
class MatchResult:
    """专辑 -> 网易云的匹配结论."""

    album_key: str = ""
    album_title: str = ""
    kind: str = "song"          # song | album
    target_id: int = 0          # song id 或 album id
    target_name: str = ""
    target_artists: list[str] = field(default_factory=list)
    target_album_id: int = 0
    target_album_name: str = ""
    score: float = 0.0
    title_score: float = 0.0
    artist_score: float = 0.0
    track_score: float = 0.0
    method: str = ""
    matched_at: str = ""
    candidates: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MatchResult":
        return cls(
            album_key=_clean_str(data.get("album_key")),
            album_title=_clean_str(data.get("album_title")),
            kind=_clean_str(data.get("kind")) or "song",
            target_id=int(data.get("target_id") or 0),
            target_name=_clean_str(data.get("target_name")),
            target_artists=list(data.get("target_artists") or []),
            target_album_id=int(data.get("target_album_id") or 0),
            target_album_name=_clean_str(data.get("target_album_name")),
            score=float(data.get("score") or 0.0),
            title_score=float(data.get("title_score") or 0.0),
            artist_score=float(data.get("artist_score") or 0.0),
            track_score=float(data.get("track_score") or 0.0),
            method=_clean_str(data.get("method")),
            matched_at=_clean_str(data.get("matched_at")),
            candidates=list(data.get("candidates") or []),
        )


def iter_missing(albums: Sequence[Album], attr: str) -> Iterator[Album]:
    for a in albums:
        if not getattr(a, attr, None):
            yield a

#: 常见同人展会识别(歌单/专辑名里出现即可猜测)
_EVENT_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"(?i)\bM3\s*[-–]?\s*(\d{4})(?P<season>[春夏秋冬]?)", "M3"),
    (r"(?i)\bC(\d{2,3})\b", "Comiket"),
    (r"(?i)comic\s*market\s*(\d{2,3})", "Comiket"),
    (r"(?i)例大祭\s*(\d{1,2})", "例大祭"),
    (r"(?i)紅楼夢\s*(\d{1,2})", "紅楼夢"),
    (r"(?i)\bM3\s*[-–]?\s*(\d{2})\b", "M3"),
)

#: 展会名前缀 -> 规范写法
_EVENT_PREFIX = {
    "M3": "M3",
    "Comiket": "C",
    "例大祭": "例大祭",
    "紅楼夢": "紅楼夢",
}


def guess_event(*texts: str) -> str:
    """从若干文本里猜展会名(猜不到返回空串).

    只做**保守**匹配: 命中 M3 / Comiket / 例大祭 / 紅楼夢 这类明确标记才返回,
    避免把普通数字误当展会。猜出来的名字会再交给调用方决定"新建还是复用"。
    """
    import re as _re

    joined = " ".join(str(t or "") for t in texts)
    if not joined.strip():
        return ""
    match = _re.search(r"(?i)\bM3\s*[-–—]?\s*(\d{4})\s*([春夏秋冬])?", joined)
    if match:
        return f"M3-{match.group(1)}{match.group(2) or ''}"
    match = _re.search(r"(?i)\bM3\s*[-–—]?\s*(\d{1,2})\b", joined)
    if match:
        return f"M3-{match.group(1)}"
    match = _re.search(r"(?i)(?:comic\s*market\s*|\bC)(\d{2,3})\b", joined)
    if match:
        return f"C{match.group(1)}"
    match = _re.search(r"例大祭\s*(\d{1,2})", joined)
    if match:
        return f"例大祭{match.group(1)}"
    match = _re.search(r"紅楼夢\s*(\d{1,2})", joined)
    if match:
        return f"紅楼夢{match.group(1)}"
    return ""


__all__ = ["guess_event"]
