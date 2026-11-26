"""整段曲目表文本 -> 结构化曲目(**纯函数 / 零依赖 / 可单测**).

用户从各处(DoujinStyle / VGMdb / 社团官网 / Discord / 记事本)复制来的曲目表
格式五花八门, 这个模块把一大段文本拆成 :class:`ParsedTrack` 列表, 供管理面板与
用户端的「整段粘贴 → 智能识别 → 批量填入」入口回填结构化曲目编辑器。

支持的输入形态(逐条都有测试):

* 轨号前缀: ``01. 曲名`` / ``1 曲名`` / ``01 曲名`` / ``1. 曲名`` / ``1,曲名``
  / ``Tr.1`` / ``Track 1`` / ``#01`` / ``No.1`` / ``01タイトル``(紧贴)
* 标题 / 艺人: ``曲名 / 艺人``、``曲名 - 艺人``(只认**两侧带空格**的分隔符)
* 时长: ``4:12`` / ``1:02:03`` / ``4分12秒`` / ``252``(秒) / ``曲名 (4:12)``
  / ``曲名 [4:12]`` / ``曲名 4:12``
* 全角: ``０１．曲名　４：１２`` / ``曲名（４：１２）``
* 制表符表格: ``01\t曲名\t艺人\t4:12``(按列识别, 列顺序可以缺省)
* 空行 / 注释行(``#`` ``//`` ``;`` ``※``) / 表头(``曲目列表`` ``# 曲名 / アーティスト``)
  / 碟号行(``Disc 1``) 自动跳过
* 没有编号的行按出现顺序编号

设计原则是 **保守优先**: 拿不准就退化成「整行当标题」, 绝不把标题的一部分当成
艺人或时长。所有取舍都写在对应分支的注释里, 并配有"不该错拆"的测试。

约定:

* ``title`` 是曲名; 识别不出艺人时 ``artists`` 就是空列表, **不会**硬拆。
* ``duration`` 统一成秒(解析不出是 ``None``); ``duration_text`` 给 UI 显示 ``4:12``。
* ``index`` 是**解析结果内的顺序**(1 起, 与 :attr:`dddb.models.Track.index` 语义一致),
  原文本里显式写出的轨号放在 ``number`` 里(``None`` = 原文本没写)。
* ``raw`` 保留原行(只去掉首尾空白, 不做全角归一), 方便 UI 回显核对。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

__all__ = ["ParsedTrack", "format_duration", "parse_duration", "parse_tracklist"]


# --------------------------------------------------------------------------
# 字符归一化
# --------------------------------------------------------------------------

#: 全角 ASCII(！ 到 ～, 含全角数字/句点/冒号/斜杠/破折号)-> 半角。
#: 再加上全角空格、各种窄空格与零宽字符。归一化只影响**匹配**, ``raw`` 保留原样。
_TRANSLATE: dict[int, int | None] = {c: c - 0xFEE0 for c in range(0xFF01, 0xFF5F)}
_TRANSLATE.update({
    0x3000: 0x20,                                  # 全角空格
    0x00A0: 0x20,                                  # NBSP
    0x2002: 0x20, 0x2003: 0x20, 0x2004: 0x20, 0x2005: 0x20,
    0x2007: 0x20, 0x2008: 0x20, 0x2009: 0x20, 0x200A: 0x20,
    0x202F: 0x20, 0x205F: 0x20,
    0x200B: None, 0x200C: None, 0x200D: None,     # 零宽字符直接删
    0x2060: None, 0xFEFF: None,
})


def _norm(text: str) -> str:
    """全角 -> 半角(只用于匹配; 不做大小写变换, 保留原文风格)."""
    return (text or "").translate(_TRANSLATE)


def _tidy(text: str) -> str:
    """折叠连续空白(去掉时长后容易留下双空格)."""
    return re.sub(r"\s+", " ", text).strip()


# --------------------------------------------------------------------------
# 时长
# --------------------------------------------------------------------------

_DUR_BODY = r"(?:\d{1,3}:\d{2}(?::\d{2})?|\d{1,3}分\d{1,2}秒|\d{1,3}秒)"
"""一段"时长"的正则片段: ``4:12`` / ``1:02:03`` / ``4分12秒`` / ``252秒``。

注意**没有**裸整数 —— 裸秒数只在"整格/整行就是时长"或"表格列"里才认,
因为 ``曲名 10`` 这种把标题末尾数字当时长会误伤标题。
"""

_DUR_BRACKET_RE = re.compile(
    rf"[(\[【]\s*(?P<dur>{_DUR_BODY})\s*[)\]】]"
)
_TRAIL_DUR_RE = re.compile(rf"^(?P<body>.*\S)[ \t]+(?P<dur>{_DUR_BODY})[ \t]*$")
#: 尾随裸秒数: 只在**行内已经有 ` - ` / ` / ` 这种标题-艺人分隔符**时才当时长。
#: 否则 ``01. Track 10`` 会被拆成标题 ``Track`` + 时长 10 秒。
_TRAIL_SEC_RE = re.compile(r"^(?P<body>.*\S)[ \t]+(?P<sec>\d{2,5})[ \t]*$")
#: 标题 / 艺人分隔符的"有空白"判定(连字符/短破折号/长破折号都算)
_SEP_ANY_RE = re.compile(r"\s+/\s+|\s+[-–—]\s+")

_COLON_RE = re.compile(r"^(?P<a>\d{1,3}):(?P<b>\d{2})(?::(?P<c>\d{2}))?$")
_MIN_SEC_RE = re.compile(r"^(?P<m>\d{1,3})分(?P<s>\d{1,2})秒$")
_SEC_RE = re.compile(r"^(?P<s>\d{1,3})秒$")
_INT_RE = re.compile(r"^\d+$")
_DUR_FULL_RE = re.compile(rf"^(?:{_DUR_BODY}|\d+)$")
_BRACKET_PAIRS = (("[", "]"), ("(", ")"), ("【", "】"), ("{", "}"), ("〈", "〉"))


def parse_duration(text: Any) -> int | None:
    """把 ``4:12`` / ``1:02:03`` / ``4分12秒`` / ``252`` 归一成秒; 认不出返回 ``None``.

    允许连壳一起贴(``[4:12]`` / ``(4:12)`` / ``【4:12】``); 纯 ``0`` 视为无效。
    """
    raw = _norm(str(text if text is not None else "")).strip()
    for open_c, close_c in _BRACKET_PAIRS:
        if len(raw) >= 2 and raw.startswith(open_c) and raw.endswith(close_c):
            raw = raw[1:-1].strip()
    if not raw or not _DUR_FULL_RE.match(raw):
        return None
    match = _COLON_RE.match(raw)
    if match:
        a, b, c = int(match.group("a")), int(match.group("b")), match.group("c")
        #: 两段式 ``4:12`` = 分:秒; 三段式 ``1:02:03`` = 时:分:秒
        return a * 3600 + b * 60 + int(c) if c else a * 60 + b
    match = _MIN_SEC_RE.match(raw)
    if match:
        return int(match.group("m")) * 60 + int(match.group("s"))
    match = _SEC_RE.match(raw)
    if match:
        value = int(match.group("s"))
        return value if value > 0 else None
    value = int(raw)
    #: 裸秒数上限 24 小时; 超出说明这不是时长(更像年份之类的标题内容)
    return value if 0 < value <= 86399 else None


def format_duration(seconds: Any) -> str:
    """秒 -> ``4:12`` / ``1:02:03``(解析不出返回空串, 方便直接塞进 input value)."""
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return ""
    if total < 0:
        return ""
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


# --------------------------------------------------------------------------
# 结果对象
# --------------------------------------------------------------------------


@dataclass
class ParsedTrack:
    """一行曲目表解析出来的结果(可直接回填结构化曲目编辑器)."""

    index: int = 0                                  #: 解析结果内的顺序(1 起)
    title: str = ""                                 #: 曲名(必定非空)
    artists: list[str] = field(default_factory=list)
    duration: int | None = None                     #: 秒, 解析不出为 None
    raw: str = ""                                   #: 原行(便于 UI 回显核对)
    number: int | None = None                       #: 原文本里显式写出的轨号
    disc: int = 1                                   #: 属于第几碟(用户 m16568: 支持分碟)

    @property
    def duration_text(self) -> str:
        """给 UI 用的 ``4:12`` 文本(没有时长就是空串)."""
        return format_duration(self.duration)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "title": self.title,
            "artists": list(self.artists),
            "duration": self.duration,
            "duration_text": self.duration_text,
            "number": self.number,
            "raw": self.raw,
            "disc": self.disc,
        }


# --------------------------------------------------------------------------
# 行级识别
# --------------------------------------------------------------------------

#: 整行就是轨号前缀的写法。``(?=[^\d\s:.,)\]|\-])`` 保证紧贴形式后面接的是**文字**
#: 而不是 ``:``(那是 mm:ss)或逗号(那是 ``1,000``)。
_INDEX_RE = re.compile(
    r"^(?:"
    r"(?:(?:tr|trk|track|no|number)\s*[.:、)\]]?\s*|#\s*)(?P<wnum>\d{1,3})"
    r"|(?P<dnum>\d{1,3})\s*[.,、)]\s*(?!\d)"
    r"|(?P<snum>\d{1,3})[ \t]+"
    r"|(?P<gnum>0\d{1,2})(?=[^\d\s:.,)\]|\-])"
    r"|(?P<wnum2>\d{1,3})(?=[^\d\s:.,)\]|\-])"
    r")",
    re.IGNORECASE,
)

#: 行首项目符号(``・曲名`` / ``■ 収録曲`` / ``- 曲名``): 只是排版, 不是内容
_BULLET_RE = re.compile(r"^(?:[・•·‣■□●○◆◇★☆]|[*+]\s*|[-–—]\s+)")
#: 注释行(``#`` 后面跟数字的**不是**注释, 那是 ``#01 曲名``)
_COMMENT_RE = re.compile(r"^(?:#(?!\s*\d)|//|;|※|>\s)")
#: 碟号 / 卷号分隔行(用户 m16568: 要能识别分碟, 所以这里**捕获**碟号而不是丢掉)
_DISC_RE = re.compile(
    r"^(?:disc|disk|cd|vol\.?|volume|ディスク|碟)[ \t]*[-–—]?[ \t]*(\d+)$",
    re.IGNORECASE,
)
#: 只有碟号、没写 ``Disc`` 前缀的写法(``Disc 2`` 之外还有 ``2枚目`` / ``第2碟``)
_DISC_CN_RE = re.compile(r"^第?\s*(\d{1,2})\s*(?:枚目|碟|盘|張目|张目)$")
#: 兜底: 空行 / 分隔线
_RULE_RE = re.compile(r"^[-=_*~·—–\s]{3,}$")

#: 整行精确等于这些词 -> 是表头(中日文表头不会被当成曲名, 直接跳过)
_HEADER_EXACT = frozenset({
    "曲目", "曲目表", "曲目列表", "曲目清单", "曲名", "曲名リスト", "标题", "標題",
    "歌曲", "歌曲列表", "収録曲", "收録曲", "收录曲", "收录曲目", "トラックリスト",
    "トラック", "番号", "タイトル", "時間", "时长", "アーティスト", "アーティスト名",
    "歌手", "艺人", "tracklist", "tracks", "songs", "tracklisting", "songlist",
})
#: 多列表头(``# 曲名 / アーティスト / 時間``)里每一格都必须落在这个集合里才跳过。
#: 含英文单词是为了容忍 ``Title  Artist  Time`` 这种表头行, 但**单个**英文单词
#: 不跳过 —— ``Time``(Pink Floyd) 这种真曲名不该被吃掉。
_COLUMN_WORDS = _HEADER_EXACT | frozenset({
    "#", "no", "no.", "tr", "tr.", "track", "number", "title", "name",
    "time", "length", "duration", "artist", "artists", "song", "disc", "disk", "cd",
    "曲", "作曲", "作詞", "作词", "編曲", "编曲", "演唱", "演奏", "初出", "原曲",
    "时长", "碟",
})
_HEADER_SEP_RE = re.compile(r"[\s#.,:;|/\-–—()\[\]{}「」【】『』、]+")
_PIECE_SPLIT_RE = re.compile(r"[\s|/\-–—]+")

#: 标题 / 艺人分隔符: **两侧都要有空白**。``Re:Collection -X-`` 这种曲名里的
#: ``-`` 因此不会被误当分隔符; ``曲名/艺人``(无空格)也保守地不拆。
_SPLIT_RE = re.compile(r"\s+/\s+|\s+[-–—]\s+")
#: 同一行里分隔多个艺人. 除了 `` / `` 还要认全角/半角顿号与逗号 ——
#: infobox 与网易云导出的曲目行是 ``曲名 / A、B`` 这种写法, 只认斜杠会
#: 把 ``A、B`` 当成一个艺人名存进库。
_ARTIST_SPLIT_RE = re.compile(r"\s*/\s*|\s*[、,，;；]\s*")
_CELL_ARTIST_SPLIT_RE = re.compile(r"\s*/\s*|[,，、;；]\s*")
#: 表格首列的轨号(``01`` / ``01.`` / ``Tr.1`` / ``#01``)
_BARE_INDEX_RE = re.compile(
    r"^(?:(?:tr|trk|track|no|number|#)\s*[.:、)]?\s*)?(?P<num>\d{1,3})[.,、)]?$",
    re.IGNORECASE,
)


def split_title_artists(text: str) -> tuple[str, list[str]]:
    """把 ``曲名 / 艺人`` / ``曲名 - 艺人`` 拆成标题 + 艺人列表.

    * 只认 `` / `` 与 `` - ``(两侧有空白)这两种分隔符;
    * 标题或艺人有一侧为空 -> 不拆(整行当标题);
    * 取**最靠左**的分隔符, 之后的部分里再用 `` / `` 继续拆成多个艺人。
    """
    body = _tidy(text)
    match = _SPLIT_RE.search(body)
    if not match:
        return body, []
    title = body[:match.start()].strip()
    rest = body[match.end():].strip()
    if not title or not rest:
        return body, []
    artists = [part.strip() for part in _ARTIST_SPLIT_RE.split(rest) if part.strip()]
    if not artists:
        return body, []
    return title, artists


def _disc_of(line: str) -> int | None:
    """这一行是碟号分隔行吗? 是则返回碟号(用户 m16568: 支持分碟).

    ``Disc 2`` / ``CD1`` / ``Disc - 2`` / ``碟 2`` / ``第2碟`` / ``2枚目`` 都认。
    不是碟号行返回 ``None``。
    """
    match = _DISC_RE.match(line) or _DISC_CN_RE.match(line)
    if not match:
        return None
    try:
        number = int(match.group(1))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _is_header(line: str) -> bool:
    """整行是表头 / 栏目标题?"""
    token = _HEADER_SEP_RE.sub("", line).lower()
    if token and token in _HEADER_EXACT:
        return True
    pieces = [p for p in _PIECE_SPLIT_RE.split(line) if p]
    if len(pieces) >= 2:
        return all(_HEADER_SEP_RE.sub("", p).lower() in _COLUMN_WORDS for p in pieces)
    return False


def _extract_durations(text: str) -> tuple[str, int | None]:
    """摘掉 ``[4:12]`` / ``(4:12)`` / ``【4:12】``(可出现在任意位置), 返回剩余文本 + 秒数."""
    duration: int | None = None
    for match in _DUR_BRACKET_RE.finditer(text):
        if duration is None:
            duration = parse_duration(match.group("dur"))
    return _DUR_BRACKET_RE.sub(" ", text), duration


def _pure_duration(text: str) -> int | None:
    """整段(整行 / 整个单元格)就是时长? 是则返回秒数(裸秒数在这里也认)."""
    return parse_duration(text)


@dataclass(slots=True)
class _Pending:
    """中间态: 弱轨号(紧贴形式)要等知道"第几行"才能决定认不认."""

    raw: str
    duration: int | None = None
    number: int | None = None
    weak: bool = False
    title: str = ""
    artists: list[str] = field(default_factory=list)
    alt_title: str = ""                    #: 弱轨号被否决时: 整行当标题
    alt_artists: list[str] = field(default_factory=list)
    disc: int = 1                          #: 属于第几碟(碟号行之后的行都算新碟)


def _parse_table(cells: list[str], duration: int | None) -> _Pending:
    """制表符表格行: 按列识别(轨号 / 标题 / 艺人 / 时长), 缺列也能用."""
    rest = list(cells)
    number: int | None = None
    if len(rest) >= 2:
        match = _BARE_INDEX_RE.match(rest[0])
        if match:
            number = int(match.group("num"))
            rest = rest[1:]
    if duration is None:
        for i in range(len(rest) - 1, -1, -1):
            found = _pure_duration(rest[i])
            if found is not None:
                duration = found
                del rest[i]
                break
    title = ""
    artists: list[str] = []
    if rest:
        head, rest = rest[0], rest[1:]
        #: 标题格里可能还带括号时长或 `` - 艺人``
        head, inner_dur = _extract_durations(head)
        if duration is None:
            duration = inner_dur
        title, inner_artists = split_title_artists(head)
        artists.extend(inner_artists)
    for cell in rest:
        artists.extend(part for part in _CELL_ARTIST_SPLIT_RE.split(cell) if part.strip())
    return _Pending(raw="", duration=duration, number=number, title=title, artists=artists)


def _scan_line(normalized: str, raw: str) -> _Pending | None:
    """解析一行(已归一化); 返回 ``None`` 表示这行不是曲目(跳过)."""
    if not normalized or _RULE_RE.match(normalized):
        return None
    #: 先剥项目符号(``・`` / ``■`` / ``- ``): 它是排版, 剥完才看得清是不是表头
    #: (``■ 収録曲`` 该按表头跳过, 而不是变成一首叫"■ 収録曲"的曲目)
    body = _BULLET_RE.sub("", normalized).strip()
    if not body:
        return None
    if _COMMENT_RE.match(body) or _DISC_RE.match(body):
        return None
    if _is_header(body):
        return None

    body, duration = _extract_durations(body)
    body = body.strip()
    if not body:
        return None                                   # 只有时长行: 没有曲名, 跳过
    if _pure_duration(body) is not None:
        return None                                   # ``4:12`` / ``252`` 单行: 同样跳过
    if duration is None:
        match = _TRAIL_DUR_RE.match(body)
        if match:
            body = match.group("body").strip()
            duration = parse_duration(match.group("dur"))
    duration = duration or None                  # ``(0:00)`` 当没写时长
    if not body:
        return None

    # 制表符表格: 先按列拆(≥2 个非空单元格才算表格, 否则 tab 只是标题里的空白)
    if "\t" in body:
        cells = [cell.strip() for cell in body.split("\t")]
        cells = [cell for cell in cells if cell]
        if len(cells) >= 2:
            pending = _parse_table(cells, duration)
            pending.raw = raw
            if not pending.title:
                return None
            return pending

    number: int | None = None
    weak = False
    whole = body
    match = _INDEX_RE.match(body)
    if match:
        number = int(
            match.group("wnum") or match.group("dnum")
            or match.group("snum") or match.group("gnum")
            or match.group("wnum2")
        )
        #: 紧贴形式(``1タイトル``)可能是曲名开头(``4月の雨``), 先标弱, 后面用行号校验
        weak = match.group("gnum") is None and match.group("wnum2") is not None
        body = body[match.end():].strip()
        #: ``01 - 曲名``: 轨号后面的横杠只是排版
        body = re.sub(r"^[-–—][ \t]*", "", body).strip()
        if not body or _pure_duration(body) is not None:
            return None

    #: 尾部裸秒数: 只在行内已有 `` - `` / `` / `` 分隔符时当时长(保守)。
    #: 下限 20 秒 —— 现实里没有 20 秒以下的曲目, 这样 ``曲名 - 艺人 12`` 不会
    #: 被当成 12 秒, 而 ``曲名 - 艺人 252`` 能正常识别。
    match = _TRAIL_SEC_RE.match(body)
    if match and duration is None and _SEP_ANY_RE.search(match.group("body")):
        secs = int(match.group("sec"))
        if 20 <= secs <= 7200:
            body = match.group("body").strip()
            duration = secs

    title, artists = split_title_artists(body)
    pending = _Pending(raw=raw, duration=duration, number=number, weak=weak,
                       title=title, artists=artists)
    if weak:
        alt_title, alt_artists = split_title_artists(whole)
        pending.alt_title, pending.alt_artists = alt_title, alt_artists
    if not pending.title and not pending.alt_title:
        return None
    return pending


def parse_tracklist(text: str) -> list[ParsedTrack]:
    """把整段曲目表文本解析成 :class:`ParsedTrack` 列表(保持原顺序).

    ``text`` 为空 / 纯空白 / 只有表头与时长行 -> 返回空列表。
    """
    source = text if isinstance(text, str) else ("" if text is None else str(text))
    lines = source.splitlines()
    pending: list[_Pending] = []
    disc = 1                                  #: 当前碟号(遇到碟号行就换)
    for line in lines:
        normalized = _norm(line).strip()
        #: 用户 m16568: 碟号行不再是"噪声" —— 它是分碟的分界, 记下来给后面每行打标
        found = _disc_of(normalized)
        if found is not None:
            disc = found
            continue
        item = _scan_line(normalized, line.strip())
        if item is not None:
            item.disc = disc
            pending.append(item)

    tracks: list[ParsedTrack] = []
    for position, item in enumerate(pending, 1):
        number = item.number
        if item.weak and number != position:
            #: 紧贴形式且轨号跟行号对不上(``4月の雨`` 单独一行) -> 整行当标题
            number = None
            title, artists = item.alt_title, list(item.alt_artists)
        else:
            title, artists = item.title, list(item.artists)
        if not title:
            continue
        tracks.append(ParsedTrack(
            index=len(tracks) + 1,
            title=title,
            artists=artists,
            duration=item.duration,
            raw=item.raw,
            number=number,
            disc=item.disc,
        ))
    return tracks