"""从歌词里解析制作人员信息(仿 EXEC_WIKI=BGM 的 rkgk 思路).

EXEC_WIKI-BGM 的核心价值在于**职位识别规则**，而不是 UI。它要解决:

    网易云专辑的「歌词」区常常被社团当成信息板用 —— 那里是**自由文本**，
    中英日混排、带括号原名、有时只有人名没有职位。

    真实样本(来自 GIF 里的原神 OST):

        制作人：HOYO-MiX
        制作 Music Copyright：周干轲 Qianzhou Zhou (HOYO-MIX)
        指挥 Conductor: Robert Ziegler
        乐队 Orchestra: 伦敦交响乐团 London Symphony Orchestra
        钢琴 Piano: Polina Osetinskaya
        巴扬 Bayan: 许笑男 Xuanan Xu
        巴拉莱卡琴 Balalaika: Justin Quinn
        合唱 Choir: Budapest Scoring Choir
        录音师 Recording Studio: LSO St Luke's / AIR Lyndhurst Hall
        录音师 Recording Engineer: Lewis Jones / ... / Ashley Andrew-Jones
        音频编辑 Editing Engineer: 鸦雀有声 Anzol
        混音 Mixing Engineer: Lewis Jones
        母带制作: 鸦雀有声
        custom-制谁: HOYO-MIX

设计:
    * ``ROLE_ALIASES``: 中/日/英 -> 规范职位(``compose`` / ``lyric`` /
      ``mix`` / ``mastering`` / ``instrument:Piano`` ...)
    * ``parse_credit_lines``: 逐行解析成 :class:`ParsedCredit`
    * ``clean_person``: 拆出「中文名 + 原名」，处理 ``(HOYO-MIX)`` 这种别名
    * 认不出的职位 -> ``custom-<原文>``(与 EXEC_WIKI-BGM 一致, 不丢信息)

为什么不做成"猜"：
    制作人员信息**错了比缺了更糟**(会污染词条)。所以这里只做**确定性**
    映射: 认不出的原样保留成 custom-，让人在预览里改。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

#: 规范职位 -> 该职位的各种写法(中/日/英, 小写比较).
#:
#: 收录原则: 只收**语义明确**的。模棱两可的(如"制作")不进 —— 它可能是
#: producer 也可能是 copyright, 强行归类会错。
ROLE_ALIASES: dict[str, tuple[str, ...]] = {
    "producer": (
        "producer", "制作人", "製作人", "プロデューサー",
    ),
    "compose": (
        "compose", "composer", "作曲", "作編曲", "作曲编曲",
        "music", "music by",
    ),
    "arrange": (
        "arrange", "arranger", "编曲", "編曲", "arrangement",
    ),
    "lyric": (
        "lyric", "lyrics", "lyricist", "作词", "作詞", "word", "words",
        "words by",
    ),
    "vocal": (
        "vocal", "vocals", "vocalist", "演唱", "歌", "唱", "ボーカル",
        "vo", "cv",
    ),
    "chorus": (
        "chorus", "choir", "合唱", "コーラス",
    ),
    "mix": (
        "mix", "mixing", "mixing engineer", "混音", "ミックス",
        "mixed by",
    ),
    "mastering": (
        "mastering", "mastering engineer", "母带", "母带制作",
        "マスタリング", "mastered by",
    ),
    "recording": (
        "recording", "recording engineer", "录音", "录音师",
        "レコーディング", "recorded by",
    ),
    "editing": (
        "editing", "editing engineer", "音频编辑", "编辑", "編集",
    ),
    "director": (
        "director", "指挥", "指揮", "conductor", "conduced by",
    ),
    "orchestra": (
        "orchestra", "乐队", "樂隊", "管弦乐", "交响乐团",
    ),
    "illustrator": (
        "illustrator", "illustration", "插画", "插圖", "イラスト",
        "jacket", "artwork", "design",
    ),
    "circle": (
        "circle", "社团", "サークル", "同人社团",
    ),
}

#: 键 -> 规范职位 的反查表(小写)
_ROLE_LOOKUP: dict[str, str] = {}
for _canon, _names in ROLE_ALIASES.items():
    for _n in _names:
        _ROLE_LOOKUP[_n.lower()] = _canon

#: 乐器前缀: "钢琴 Piano: xxx" / "乐器-吉他: xxx" / "Guitar: xxx"
_INSTRUMENT_WORDS: dict[str, str] = {
    "钢琴": "Piano", "piano": "Piano",
    "吉他": "Guitar", "guitar": "Guitar",
    "贝斯": "Bass", "bass": "Bass", "ベース": "Bass",
    "鼓": "Drums", "drums": "Drums", "ドラム": "Drums",
    "小提琴": "Violin", "violin": "Violin",
    "大提琴": "Cello", "cello": "Cello",
    "中提琴": "Viola", "viola": "Viola",
    "巴扬": "Bayan", "bayan": "Bayan",
    "巴拉莱卡琴": "Balalaika", "balalaika": "Balalaika",
    "萨克斯": "Saxophone", "saxophone": "Saxophone", "sax": "Saxophone",
    "长笛": "Flute", "flute": "Flute",
    "小号": "Trumpet", "trumpet": "Trumpet",
    "合成器": "Synthesizer", "synthesizer": "Synthesizer",
    "synth": "Synthesizer",
    "键盘": "Keyboard", "keyboard": "Keyboard",
    "二胡": "Erhu", "erhu": "Erhu",
    "古筝": "Guzheng", "guzheng": "Guzheng",
    "琵琶": "Pipa", "pipa": "Pipa",
    "笛子": "Dizi", "dizi": "Dizi",
}

#: 一行的分隔符: 中英文冒号都认
_SEP = r"[:：]"
#: custom- 前缀(EXEC_WIKI-BGM 的约定)
_CUSTOM_PREFIX = re.compile(r"^custom\s*[-－]\s*(.+)$", re.I)
#: 乐器写法: "乐器-钢琴" / "乐器：钢琴"
_INSTRUMENT_PREFIX = re.compile(
    r"^(?:乐器|instrument)\s*[-－:：]\s*(.+)$", re.I)

#: 人名里的"原名"分隔: "许笑男 Xuanan Xu" / "周干轲 Qianzhou Zhou"
#: 保守起见不在这里拆 —— 中文名和罗马字之间常有空格, 但"Yoshida Yui"
#: 这种西文名本身就有空格。交给 :func:`split_name_original` 按规则判断。

#: 明显不是人名的词(职位残留/占位)
_NOT_A_NAME = frozenset({
    "无", "なし", "n/a", "na", "-", "—", "?", "？", "未知", "未定",
    "tba", "tbd", "未公开",
})


@dataclass
class ParsedCredit:
    """一条解析出来的制作人员."""

    role: str                       # 规范职位 或 "custom-<原文>" 或 "instrument:<乐器>"
    raw_role: str = ""              # 原文职位(便于人工核对)
    person: str = ""                # 人名(清洗后)
    original: str = ""              # 原名/罗马字(如果拆出来了)
    alias: str = ""                 # 括号里的别名, 如 (HOYO-MIX)
    source_line: str = ""           # 原始整行, 便于溯源

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "raw_role": self.raw_role,
            "name": self.person,
            "original": self.original,
            "alias": self.alias,
        }


@dataclass
class ParseResult:
    """一次解析的结果."""

    credits: list[ParsedCredit] = field(default_factory=list)
    #: 认不出职位但看起来像"某某: xxx"的行 —— 原样留着, 让人在预览里改
    unrecognized: list[str] = field(default_factory=list)
    #: 完全不像制作人员信息的行(歌词正文)
    skipped: int = 0

    @property
    def total(self) -> int:
        return len(self.credits)

    def custom_roles(self) -> list[str]:
        return sorted({c.role for c in self.credits
                       if c.role.startswith("custom-")})


def _norm(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip())


def _strip_brackets(text: str) -> tuple[str, str]:
    """拆出尾部括号内容: ``"周干轲 (HOYO-MIX)"`` -> ``("周干轲", "HOYO-MIX")``.

    只认**行尾**的半角/全角括号, 中间的不动(曲名里常有括号)。
    """
    m = re.search(r"\s*[（(]\s*([^（()）]{1,40})\s*[)）]\s*$", text)
    if not m:
        return text.strip(), ""
    inner = m.group(1).strip()
    head = text[:m.start()].strip()
    return head, inner


def split_name_original(text: str) -> tuple[str, str]:
    """把「中文名 罗马字」拆开.

    规则(保守, 宁可拆不出也不拆错):
        * 只有**两段**且第一段含 CJK、第二段是纯 ASCII 时才拆
        * ``"许笑男 Xuanan Xu"`` -> ``("许笑男", "Xuanan Xu")``
        * ``"Yoshida Yui"`` -> ``("Yoshida Yui", "")`` (全西文, 不拆)
        * ``"Lewis Jones"`` -> 同理不拆
    """
    text = _norm(text)
    if not text or not text.isascii():
        # 去掉开头的 CJK 部分, 看剩下的是不是纯 ASCII
        m = re.match(r"^([^\x00-\x7f][^\x00-\x7f\s]*)\s+(.+)$", text)
        if m and m.group(2).isascii() and len(m.group(2)) >= 2:
            return m.group(1).strip(), m.group(2).strip()
    return text, ""


def clean_person(text: str) -> tuple[str, str, str]:
    """人名清洗 -> ``(person, original, alias)``.

    ``"许笑男 Xuanan Xu"``        -> ``("许笑男", "Xuanan Xu", "")``
    ``"周干轲 Qianzhou Zhou (HOYO-MIX)"``
                                 -> ``("周干轲", "Qianzhou Zhou", "HOYO-MIX")``
    ``"HOYO-MiX"``                -> ``("HOYO-MiX", "", "")``
    """
    text = _norm(text)
    if not text:
        return "", "", ""
    body, alias = _strip_brackets(text)
    # 带 "&" / "/" / "、" 的是多个人 —— 这里不拆, 整体作为一个实体,
    # 由调用方按需 split(拆错会把人名切断)
    person, original = split_name_original(body)
    return person, original, alias


def _looks_like_credit_line(line: str) -> bool:
    """这一行像不像"职位: 人名"?

    只要求**有冒号**, 且冒号左边不太长(职位名不会很长)。
    不用力过猛: 歌词里 "作词：xxx" 也确实是制作信息, 收下是对的;
    而 "这是一句歌词: 内容" 这种冒号左边超过 20 字的多半不是职位。
    """
    m = re.match(r"^(.*?)" + _SEP, line)
    if not m:
        return False
    left = m.group(1).strip()
    return 0 < len(left) <= 24


def resolve_role(raw_role: str) -> str:
    """职位原文 -> 规范职位.

    顺序:
        ① ``custom-xxx``  -> ``custom-xxx``
        ② ``乐器-钢琴``   -> ``instrument:Piano``
        ③ 精确命中别名表  -> 规范职位
        ④ 含乐器词        -> ``instrument:<乐器>``
        ⑤ 认不出          -> ``custom-<原文>``
    """
    role = _norm(raw_role)
    if not role:
        return ""

    m = _CUSTOM_PREFIX.match(role)
    if m:
        return "custom-" + _norm(m.group(1))

    m = _INSTRUMENT_PREFIX.match(role)
    if m:
        inst = _norm(m.group(1))
        canon = _INSTRUMENT_WORDS.get(inst.lower(), inst)
        return "instrument:" + canon

    key = role.lower()

    # ① 整串精确命中(最快也最准)
    if key in _ROLE_LOOKUP:
        return _ROLE_LOOKUP[key]

    # ② 中英混排: "指挥 Conductor" / "录音师 Recording Engineer"
    #    按非字母数字切开, 逐个查表。
    #    注意先查**非乐器**职位: "钢琴 Piano" 里的 Piano 既可能是
    #    乐器也可能是职位, 但这一行整体是乐器行, 交给第 ③ 步更稳。
    tokens = [w for w in re.split(r"[\s/／()（）\[\]【】,，、]+", key)
              if w]
    for word in tokens:
        if word in _ROLE_LOOKUP:
            return _ROLE_LOOKUP[word]

    # ③ 含乐器词: "钢琴 Piano" / "Guitar" / "乐器-吉他"
    for word, canon in _INSTRUMENT_WORDS.items():
        if word in key:
            return "instrument:" + canon

    return "custom-" + role


def _split_people(value: str) -> list[str]:
    """把一个值拆成多个人.

    只看**明确的多人分隔**: `` / `` `` 、 `` ``;`` ``；``
    不按单斜杠拆 —— 名字里可能有 ``AC/DC`` 这种。
    """
    parts = re.split(r"\s+/\s+|\s*、\s*|[;；]", value)
    out = [_norm(p) for p in parts if _norm(p)]
    # 过滤掉明显不是人名的
    return [p for p in out if p.lower() not in _NOT_A_NAME]


def parse_credit_lines(text: Any, *,
                       allow_bare: bool = False) -> ParseResult:
    """解析一段文本里的制作人员信息.

    ``allow_bare=True`` 时, 没有冒号但能命中职位词的行
    (如单独一行 ``"作词"``) 也尝试解析 —— 默认关, 避免误吃歌词。
    """
    result = ParseResult()
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n")

    for line in raw.split("\n"):
        line = _norm(line)
        if not line:
            continue

        if not _looks_like_credit_line(line):
            if allow_bare:
                role = resolve_role(line)
                if not role.startswith("custom-"):
                    result.credits.append(ParsedCredit(
                        role=role, raw_role=line, person="",
                        source_line=line))
                    continue
            result.skipped += 1
            continue

        raw_role, _, value = re.split("(" + _SEP + ")", line, maxsplit=1)
        raw_role = _norm(raw_role)
        value = _norm(value)
        if not raw_role or not value:
            result.skipped += 1
            continue

        role = resolve_role(raw_role)
        if role.startswith("custom-"):
            # 认不出的职位: 保留原文, 但**不丢** —— 让人在预览里改
            result.unrecognized.append(line)

        people = _split_people(value)
        if not people:
            # 有职位没人名(如只有 "作词：" ) -> 记为一条空人名, 便于提示补全
            result.credits.append(ParsedCredit(
                role=role, raw_role=raw_role, person="", source_line=line))
            continue

        for one in people:
            person, original, alias = clean_person(one)
            if not person:
                continue
            result.credits.append(ParsedCredit(
                role=role, raw_role=raw_role, person=person,
                original=original, alias=alias, source_line=line))

    return result


def to_album_credits(result: ParseResult) -> list[dict[str, Any]]:
    """解析结果 -> dddb 库里的 ``credits`` 结构.

    dddb 的 Credit 目前是 ``{name, role, source}``; 这里额外带上
    ``original`` / ``alias`` / ``raw_role`` —— 库层是 JSON, 多字段不会破坏
    旧数据(旧记录没这些键, 读出来是空)。
    """
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for c in result.credits:
        if not c.person:
            continue
        key = (c.role, c.person)
        if key in seen:
            continue
        seen.add(key)
        item: dict[str, Any] = {
            "name": c.person,
            "role": c.role,
            "source": "lyrics",
        }
        if c.original:
            item["original"] = c.original
        if c.alias:
            item["alias"] = c.alias
        if c.raw_role and c.raw_role != c.role:
            item["raw_role"] = c.raw_role
        out.append(item)
    return out


__all__ = [
    "ROLE_ALIASES",
    "ParseResult",
    "ParsedCredit",
    "clean_person",
    "parse_credit_lines",
    "resolve_role",
    "split_name_original",
    "to_album_credits",
]
