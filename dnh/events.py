"""展会事件与 X 话题标签引擎.

日本同人专辑的信息主要围绕两个展会节奏发布:

* **M3 (Music-Mix Market)** — 每年两场, 春 (4 月下旬) / 秋 (10 月下旬),
  官方写法 ``M3-2026春`` / ``M3-2026秋``; X 上的话题标签写法极其自由, 例如
  ``#M3`` ``#M3秋`` ``#M3秋2026`` ``#M3-2026秋`` ``#M3_2026秋`` ``#M32026秋``
  ``#M3Autumn2026`` 等. 本模块会把它们全部枚举出来.
* **Comic Market** — ``C100`` 起, 偶数届 = 8 月夏コミ, 奇数届 = 12 月冬コミ;
  话题标签如 ``#C107`` ``#コミケ`` ``#コミックマーケット107`` ``#C107新刊``.

另外附带 例大祭 / 紅楼夢 等东方系展会的标签生成.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from . import textutil

SEASON_JA = {"spring": "春", "autumn": "秋", "summer": "夏", "winter": "冬"}
SEASON_EN = {"spring": "Spring", "autumn": "Autumn", "summer": "Summer", "winter": "Winter"}
SEASON_EN_ALT = {"autumn": "Fall"}

# Comiket 编号锚点: C100 = 2022-08 (夏)
_COMIKET_ANCHOR_NUMBER = 100
_COMIKET_ANCHOR_YEAR = 2022

# 允许的发行日窗口: 展会前 N 天 ~ 展会后 M 天.
# 同人专辑通常在展会当天首发, 但"会前通贩 / 事后补货 / 事后加印"都很常见, 所以两边留余量。
DEFAULT_RELEASE_WINDOW_BEFORE_DAYS = 45
DEFAULT_RELEASE_WINDOW_AFTER_DAYS = 21


@dataclass(slots=True)
class Event:
    """一个展会届次."""

    name: str                       # 规范名, 如 "M3-2026秋" / "C108"
    kind: str                       # m3 | comiket | reitaisai | kouroumu | other
    year: int = 0
    season: str = ""                # spring | summer | autumn | winter | ""
    number: int = 0                 # Comiket 届数 / M3 编号
    start_date: str = ""
    end_date: str = ""
    tags: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)

    @property
    def short(self) -> str:
        return self.name

    @property
    def topic_tags(self) -> list[str]:
        """用于 X 检索的话题标签(带 #)."""
        return [t for t in self.tags if t.startswith("#")]

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "kind": self.kind,
            "year": self.year,
            "season": self.season,
            "number": self.number,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "tags": list(self.tags),
            "aliases": list(self.aliases),
        }


# --------------------------------------------------------------------------
# 日期推算
# --------------------------------------------------------------------------


def _last_sunday(year: int, month: int) -> _dt.date:
    if month == 12:
        day = _dt.date(year, 12, 31)
    else:
        day = _dt.date(year, month + 1, 1) - _dt.timedelta(days=1)
    while day.weekday() != 6:
        day -= _dt.timedelta(days=1)
    return day


def _nth_sunday(year: int, month: int, n: int) -> _dt.date:
    day = _dt.date(year, month, 1)
    offset = (6 - day.weekday()) % 7
    return day + _dt.timedelta(days=offset + 7 * (n - 1))


def m3_date(year: int, season: str) -> tuple[str, str]:
    """M3 春/秋的大致举办日期(春: 4 月第 4 个周日; 秋: 10 月第 4 个周日)."""
    month = 4 if season == "spring" else 10
    start = _nth_sunday(year, month, 4)
    return start.isoformat(), (start + _dt.timedelta(days=1)).isoformat()


def comiket_date(number: int) -> tuple[str, str]:
    """Comiket 届数 -> 大致日期."""
    if number >= _COMIKET_ANCHOR_NUMBER:
        year = _COMIKET_ANCHOR_YEAR + (number - _COMIKET_ANCHOR_NUMBER) // 2
        if number % 2 == 0:  # 夏コミ: 8 月中
            start = _nth_sunday(year, 8, 3)
        else:                # 冬コミ: 12 月底
            start = _nth_sunday(year, 12, 4)
        return start.isoformat(), (start + _dt.timedelta(days=1)).isoformat()
    # 早期届次不做精确推算
    return "", ""


def comiket_number_for(year: int, season: str) -> int:
    """由年份 + 夏/冬反推 Comiket 届数."""
    base = _COMIKET_ANCHOR_NUMBER + (year - _COMIKET_ANCHOR_YEAR) * 2
    return base if season == "summer" else base + 1


# --------------------------------------------------------------------------
# 标签生成
# --------------------------------------------------------------------------


def m3_tags(year: int, season: str, *, variants: bool = True, generic: bool = True) -> list[str]:
    """M3 的全部话题标签写法."""
    ja = SEASON_JA.get(season, "")
    yy = str(year)[2:]
    tags: list[str] = []
    if generic:
        tags.append("#M3")
    if not ja:
        tags += [f"#M3{year}", f"#M3-{year}"]
        return _uniq(tags)
    tags += [
        f"#M3{ja}",
        f"#M3{ja}{year}",
        f"#M3{year}{ja}",
    ]
    if variants:
        tags += [
            f"#M3-{year}{ja}",
            f"#M3_{year}{ja}",
            f"#M3-{ja}{year}",
            f"#M3_{ja}{year}",
            f"#M3{ja}{yy}",
            f"#M3{yy}{ja}",
            f"#M3{year}",
            f"#M3-{year}",
            f"#M3{ja}新譜",
            f"#M3{ja}新作",
            f"#M3新譜",
            f"#M3新作",
            f"#M3告知",
        ]
        en = SEASON_EN.get(season, "")
        if en:
            tags += [f"#M3{en}{year}", f"#M3{en}", f"#M3{year}{en}"]
        alt = SEASON_EN_ALT.get(season)
        if alt:
            tags += [f"#M3{alt}{year}", f"#M3{alt}"]
    return _uniq(tags)


def comiket_tags(number: int, *, variants: bool = True, generic: bool = True, season: str = "") -> list[str]:
    """Comiket 的全部话题标签写法."""
    tags: list[str] = []
    if generic:
        tags += ["#Comiket", "#コミケ", "#コミックマーケット"]
    tags.append(f"#C{number}")
    if variants:
        tags += [
            f"#C{number}新刊",
            f"#C{number}新譜",
            f"#C{number}告知",
            f"#C{number}参加",
            f"#C{number}頒布",
            f"#C{number}新作",
            f"#コミケ{number}",
            f"#コミックマーケット{number}",
            f"#Comiket{number}",
        ]
        if season == "summer":
            tags += ["#夏コミ", f"#夏コミ{number}"]
        elif season == "winter":
            tags += ["#冬コミ", f"#冬コミ{number}"]
    return _uniq(tags)


def reitaisai_tags(number: int, *, variants: bool = True) -> list[str]:
    tags = ["#例大祭", f"#例大祭{number}", f"#博麗神社例大祭", f"#博麗神社例大祭{number}"]
    if variants:
        tags += [f"#例大祭{number}新譜", f"#Reitaisai{number}", "#同人音楽"]
    return _uniq(tags)


def kouroumu_tags(number: int, *, variants: bool = True) -> list[str]:
    tags = ["#紅楼夢", f"#紅楼夢{number}", "#東方紅楼夢", f"#東方紅楼夢{number}"]
    if variants:
        tags += [f"#紅楼夢{number}新譜"]
    return _uniq(tags)


def _uniq(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it and it not in seen:
            seen.add(it)
            out.append(it)
    return out


# --------------------------------------------------------------------------
# 发行日 vs 展会窗口
# --------------------------------------------------------------------------


def parse_release_date(value: str | None) -> _dt.date | None:
    """把 ``YYYY-MM-DD`` / ``YYYY-MM`` / ``YYYY`` 解析成日期.

    不完整的日期取中间值(``YYYY`` -> 7 月 1 日, ``YYYY-MM`` -> 当月 15 日), 因为这里只用来
    判断"在不在展会窗口里", 年份级别的偏差已经足够把 2011 年的专辑和 2026 年的展会分开。
    """
    if not value:
        return None
    text = str(value).strip()[:10]
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return _dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    m = re.match(r"^((?:19|20)\d{2})(?:[-/.](\d{1,2}))?", text)
    if not m:
        return None
    year = int(m.group(1))
    month = int(m.group(2)) if m.group(2) else 0
    if not 1 <= month <= 12:
        return _dt.date(year, 7, 1)
    return _dt.date(year, month, 15)


def event_day(event: "Event") -> _dt.date | None:
    """展会的举办日(取开始日)."""
    if event is None or not event.start_date:
        return None
    try:
        return _dt.date.fromisoformat(str(event.start_date)[:10])
    except ValueError:
        return None


def release_window(
    event: "Event",
    *,
    before_days: int = DEFAULT_RELEASE_WINDOW_BEFORE_DAYS,
    after_days: int = DEFAULT_RELEASE_WINDOW_AFTER_DAYS,
) -> tuple[_dt.date, _dt.date] | None:
    """该展会允许的发行日窗口 ``(最早, 最晚)``; 未知举办日时返回 None."""
    day = event_day(event)
    if day is None:
        return None
    return (
        day - _dt.timedelta(days=max(0, int(before_days))),
        day + _dt.timedelta(days=max(0, int(after_days))),
    )


def release_matches_event(
    release_date: str | None,
    event: "Event",
    *,
    before_days: int = DEFAULT_RELEASE_WINDOW_BEFORE_DAYS,
    after_days: int = DEFAULT_RELEASE_WINDOW_AFTER_DAYS,
) -> bool | None:
    """发行日是否属于该展会.

    返回:

    * ``None`` —— 无法判断(没有发行日, 或展会没有已知举办日)。调用方应自行决定策略。
    * ``True`` —— 明确落在窗口内。
    * ``False`` —— **明确不在**窗口内 —— 这类专辑不该被打上该展会的标签。

    这正是"数据库里 86% 的 M3-2026秋 专辑其实是 2011~2025 年已发售的老碟"的根因。
    """
    window = release_window(event, before_days=before_days, after_days=after_days)
    if window is None:
        return None
    day = parse_release_date(release_date)
    if day is None:
        return None
    return window[0] <= day <= window[1]


# --------------------------------------------------------------------------
# 构造展会
# --------------------------------------------------------------------------

#: M3 届数基准 —— M3-2023春 == M3-51, 之后每年春 +2、秋 +1。
#: 某站点 与 某站点 都用「缩写+序号」做标签/索引, 所以推算逻辑放在这里共用,
#: **不要写死某个具体序号**(每届都会变)。
M3_REF_YEAR = 2023
M3_REF_SPRING_EDITION = 51


def m3_edition(year: int, season: str) -> int:
    """算 M3 的届数: ``M3-2026春`` -> 57, ``M3-2026秋`` -> 58."""
    spring = M3_REF_SPRING_EDITION + 2 * (int(year) - M3_REF_YEAR)
    return spring + (1 if (season or "").lower() in ("autumn", "fall") else 0)


def edition_label(event: "Event | None") -> str:
    """展会的「缩写+序号」标签, 例如 ``M3-58`` / ``C108`` / ``例大祭13``.

    某站点 的标签、某站点 收藏条目的 tag 都是这个形式。
    """
    if event is None:
        return ""
    if event.kind == "m3":
        return f"M3-{m3_edition(event.year, event.season)}"
    if event.kind == "comiket":
        return f"C{int(event.number)}"
    if event.kind == "reitaisai":
        return f"例大祭{int(event.number)}"
    if event.kind == "kouroumu":
        return f"紅楼夢{int(event.number)}"
    return ""


def m3_event(year: int, season: str, *, variants: bool = True, generic: bool = True) -> Event:
    name = f"M3-{year}{SEASON_JA.get(season, '')}"
    start, end = m3_date(year, season)
    return Event(
        name=name,
        kind="m3",
        year=year,
        season=season,
        start_date=start,
        end_date=end,
        tags=m3_tags(year, season, variants=variants, generic=generic),
        aliases=_uniq([f"M3{season.upper()}{year}", f"M3 {year}{SEASON_JA.get(season, '')}", f"M3{year}{SEASON_JA.get(season, '')}"]),
    )


def comiket_event(number: int, *, variants: bool = True, generic: bool = True) -> Event:
    season = "summer" if number % 2 == 0 else "winter"
    start, end = comiket_date(number)
    year = int(start[:4]) if start else 0
    return Event(
        name=f"C{number}",
        kind="comiket",
        year=year,
        season=season,
        number=number,
        start_date=start,
        end_date=end,
        tags=comiket_tags(number, variants=variants, generic=generic, season=season),
        aliases=_uniq([f"Comiket{number}", f"コミケ{number}", f"コミックマーケット{number}"]),
    )


def reitaisai_event(number: int, year: int = 0, *, variants: bool = True) -> Event:
    return Event(
        name=f"例大祭{number}" if number else "例大祭",
        kind="reitaisai",
        year=year,
        number=number,
        tags=reitaisai_tags(number, variants=variants),
        aliases=[f"Reitaisai{number}"] if number else [],
    )


def kouroumu_event(number: int, year: int = 0, *, variants: bool = True) -> Event:
    return Event(
        name=f"紅楼夢{number}" if number else "紅楼夢",
        kind="kouroumu",
        year=year,
        number=number,
        tags=kouroumu_tags(number, variants=variants),
        aliases=[f"Kouroumu{number}"] if number else [],
    )


# --------------------------------------------------------------------------
# 解析 / 推断
# --------------------------------------------------------------------------

_M3_RE = re.compile(
    r"M3[\s\-_]*(?:(?P<y1>20\d{2})\s*(?P<s1>春|秋|夏|冬|spring|summer|autumn|fall)?"
    r"|(?P<s2>春|秋|夏|冬|spring|summer|autumn|fall)\s*(?P<y2>20\d{2})?)",
    re.IGNORECASE,
)
_COMIKET_RE = re.compile(r"(?:C|Comiket|コミケ|コミックマーケット)\s*[-_]?\s*(?P<n>9\d|1\d{2})", re.IGNORECASE)
_REITAISAI_RE = re.compile(r"例大祭\s*(?P<n>\d+)?|Reitaisai\s*(?P<n2>\d+)?", re.IGNORECASE)
_KOUROUMU_RE = re.compile(r"紅楼夢\s*(?P<n>\d+)?|Kouroumu\s*(?P<n2>\d+)?", re.IGNORECASE)


def _season_key(raw: str | None) -> str:
    if not raw:
        return ""
    raw = raw.strip().lower()
    return {
        "春": "spring", "spring": "spring",
        "夏": "summer", "summer": "summer",
        "秋": "autumn", "autumn": "autumn", "fall": "autumn",
        "冬": "winter", "winter": "winter",
    }.get(raw, "")


def default_year(now: _dt.date | None = None) -> int:
    """当前应关注的展会年份.

    M3 秋季在 10 月, 冬コミ在 12 月; 每年 1-2 月时仍应关注上一年度冬季刚结束的展会.
    """
    today = now or _dt.date.today()
    return today.year


def current_season(now: _dt.date | None = None) -> str:
    today = now or _dt.date.today()
    month = today.month
    if month in (3, 4, 5):
        return "spring"
    if month in (6, 7, 8):
        return "summer"
    if month in (9, 10, 11):
        return "autumn"
    return "winter"


def parse_event(text: str) -> Event | None:
    """从任意文本中解析出最可能的展会."""
    if not text:
        return None
    m = _M3_RE.search(text)
    if m:
        year = int(m.group("y1") or m.group("y2") or 0)
        season = _season_key(m.group("s1") or m.group("s2"))
        if not year:
            year = default_year()
        if not season:
            season = current_season()
        return m3_event(year, season)
    m = _COMIKET_RE.search(text)
    if m:
        number = int(m.group("n"))
        if number:
            return comiket_event(number)
    m = _KOUROUMU_RE.search(text)
    if m:
        number = int(m.group("n") or m.group("n2") or 0)
        return kouroumu_event(number)
    m = _REITAISAI_RE.search(text)
    if m:
        number = int(m.group("n") or m.group("n2") or 0)
        return reitaisai_event(number)
    return None


def detect_events(text: str) -> list[Event]:
    """返回文本中出现的所有展会(去重)."""
    found: dict[str, Event] = {}
    if not text:
        return []
    for m in _M3_RE.finditer(text):
        year = int(m.group("y1") or m.group("y2") or 0) or default_year()
        season = _season_key(m.group("s1") or m.group("s2")) or current_season()
        ev = m3_event(year, season)
        found.setdefault(ev.name, ev)
    for m in _COMIKET_RE.finditer(text):
        number = int(m.group("n"))
        if number:
            ev = comiket_event(number)
            found.setdefault(ev.name, ev)
    for m in _KOUROUMU_RE.finditer(text):
        number = int(m.group("n") or m.group("n2") or 0)
        ev = kouroumu_event(number)
        found.setdefault(ev.name, ev)
    for m in _REITAISAI_RE.finditer(text):
        number = int(m.group("n") or m.group("n2") or 0)
        ev = reitaisai_event(number)
        found.setdefault(ev.name, ev)
    return list(found.values())


def normalize_event_name(text: str) -> str:
    """把各种写法归一成规范名; 无法识别时返回原文本."""
    ev = parse_event(text)
    return ev.name if ev else textutil.clean_whitespace(text)


# --------------------------------------------------------------------------
# 事件解析入口
# --------------------------------------------------------------------------


def events_for_year(
    year: int,
    *,
    kinds: Sequence[str] = ("m3", "comiket"),
    variants: bool = True,
    generic: bool = True,
) -> list[Event]:
    """生成某一年度的全部候选展会(春/秋 M3 + 夏/冬 Comiket)."""
    out: list[Event] = []
    if "m3" in kinds:
        out.append(m3_event(year, "spring", variants=variants, generic=generic))
        out.append(m3_event(year, "autumn", variants=variants, generic=generic))
    if "comiket" in kinds:
        out.append(comiket_event(comiket_number_for(year, "summer"), variants=variants, generic=generic))
        out.append(comiket_event(comiket_number_for(year, "winter"), variants=variants, generic=generic))
    return out


def recent_events(
    now: _dt.date | None = None,
    *,
    lookback_days: int = 200,
    lookahead_days: int = 120,
    kinds: Sequence[str] = ("m3", "comiket"),
    variants: bool = True,
    generic: bool = True,
) -> list[Event]:
    """当前应当采集的展会集合(过去一段时间 + 未来一段时间)."""
    today = now or _dt.date.today()
    years = sorted({today.year - 1, today.year, today.year + 1})
    candidates: list[Event] = []
    for y in years:
        candidates.extend(events_for_year(y, kinds=kinds, variants=variants, generic=generic))
    out: list[Event] = []
    for ev in candidates:
        if not ev.start_date:
            continue
        try:
            start = _dt.date.fromisoformat(ev.start_date)
        except ValueError:
            continue
        delta = (start - today).days
        if -lookback_days <= delta <= lookahead_days:
            out.append(ev)
    out.sort(key=lambda e: e.start_date)
    return out


def resolve(spec: str | None, *, year: int = 0, kinds: Sequence[str] = ("m3", "comiket")) -> list[Event]:
    """把用户输入的 ``--event`` 规格解析为展会列表.

    支持: ``M3-2026秋`` / ``M3秋`` / ``C108`` / ``2026`` / ``all`` / 空(默认近期).
    """
    if not spec or spec.strip().lower() in ("auto", "recent", "近", "近期"):
        return recent_events(year=year) if not year else events_for_year(year, kinds=kinds)
    spec = spec.strip()
    if spec.lower() in ("all", "*"):
        return [*events_for_year(year or default_year(), kinds=kinds, generic=True)]
    if re.fullmatch(r"20\d{2}", spec):
        return events_for_year(int(spec), kinds=kinds)
    if re.fullmatch(r"(?:M3|m3)", spec):
        y = year or default_year()
        return [m3_event(y, "spring"), m3_event(y, "autumn")]
    if re.fullmatch(r"(?:C|c|comiket)", spec, re.IGNORECASE):
        return [comiket_event(comiket_number_for(year or default_year(), "summer")),
                comiket_event(comiket_number_for(year or default_year(), "winter"))]
    # 逗号分隔多值(必须在单值解析之前判断, 否则 "M3-2026秋,C107" 只会匹配到 M3)
    if "," in spec:
        out: list[Event] = []
        for part in spec.split(","):
            out.extend(resolve(part.strip(), year=year, kinds=kinds))
        return _dedupe_events(out)
    ev = parse_event(spec)
    if ev is not None:
        return [ev]
    return []


def _dedupe_events(events: Iterable[Event]) -> list[Event]:
    seen: set[str] = set()
    out: list[Event] = []
    for ev in events:
        if ev.name not in seen:
            seen.add(ev.name)
            out.append(ev)
    return out


def all_tags(events: Iterable[Event], *, extra: Sequence[str] = ()) -> list[str]:
    tags: list[str] = []
    for ev in events:
        tags.extend(ev.topic_tags)
    tags.extend(extra)
    return _uniq(tags)


def tag_to_event(tag: str, events: Sequence[Event]) -> str:
    """把一个具体话题标签映射回规范事件名(用于归类 X 动态)."""
    clean = textutil.to_halfwidth(tag).strip().lower().replace("#", "")
    for ev in events:
        for t in ev.topic_tags:
            if textutil.to_halfwidth(t).strip().lower().replace("#", "") == clean:
                return ev.name
        for alias in ev.aliases:
            if textutil.norm_key(alias) and textutil.norm_key(alias) == textutil.norm_key(clean):
                return ev.name
    parsed = parse_event(tag)
    return parsed.name if parsed else ""
