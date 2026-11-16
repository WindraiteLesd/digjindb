"""文本规范化与相似度工具.

跨源聚合的核心难点是标题对齐: 同一个同人专辑在 doujinstyle 可能是英文罗马字,
在 vgmdb / bgm.tv 是日文原名, 在网易云又可能是 "标题 - 社团" 的拼接形式.
本模块提供统一的规范化管线与打分函数.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from typing import Iterable, Sequence

# --------------------------------------------------------------------------
# 全角 / 半角
# --------------------------------------------------------------------------

_FULLWIDTH_MAP = {i: i - 0xFEE0 for i in range(0xFF01, 0xFF5F)}
_FULLWIDTH_MAP[0x3000] = 0x20  # 全角空格
_FULLWIDTH_TABLE = str.maketrans(_FULLWIDTH_MAP)

# 各种引号 / 破折号 / 波浪号统一化
_PUNCT_MAP = {
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    "\u2013": "-", "\u2014": "-", "\u2015": "-", "\u2212": "-",
    "\uff5e": "~", "\u301c": "~", "\u223c": "~",
    "\u00b7": " ", "\u30fb": " ", "\u2022": " ",
    "\u3002": ".", "\uff0e": ".", "\u3001": ",",
}
_PUNCT_TABLE = str.maketrans(_PUNCT_MAP)

# 需要整体剔除的括号包裹内容(演出 / 会场 / 特典 / 价格等噪声)
_NOISE_BRACKETS = re.compile(
    r"[\(\[【（〔「『](?:"
    r"[^\)\(\[\]【】（）〔〕「」『』]*?"
    r"(?:"
    r"m3|comiket|コミケ|コミックマーケット|c\d{2,3}|例大祭|紅楼夢|紅魔郷|sunny\s*side|"
    r"event|会場|会场|限定|特典|初回|予約|価格|价格|税込|税抜|dx|demo|trial|体験版|"
    r"crossfade|試聴|试听|xfd|cm|pv|"
    r"\d{4}[/\-年]\d{1,2}|20\d{2}\s*年"
    r")[^\)\(\[\]【】（）〔〕「」『』]*?"
    r")[\)\]】）〕」』]",
    re.IGNORECASE,
)

# 纯年份 / 日期括号
_DATE_ONLY_BRACKET = re.compile(r"[\(\[【（]\s*(?:19|20)\d{2}(?:[./\-年]\d{1,2})?(?:[./\-月]\d{1,2}日?)?\s*[\)\]】）]")

# 事件标记: M3-2026春 / M3秋2026 / C107 / 例大祭
EVENT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("m3", re.compile(r"\bM3[\s\-_]*(?:20)?\d{2}\s*(?:春|秋|spring|autumn|fall)?\b|\bM3[\s\-_]*(?:春|秋|spring|autumn|fall)\s*(?:20)?\d{2}\b|\bM3\b", re.IGNORECASE)),
    ("comiket", re.compile(r"\bC\s?(?:9\d|1\d{2})\b|コミックマーケット\s*\d+|コミケ\s*\d*|comiket\s*\d*", re.IGNORECASE)),
    ("reitaisai", re.compile(r"例大祭\s*\d*|reitaisai\s*\d*", re.IGNORECASE)),
    ("kouroumu", re.compile(r"紅楼夢\s*\d*|紅魔郷\s*\d*|kouroumu\s*\d*", re.IGNORECASE)),
    ("autumn", re.compile(r"秋季例大祭\s*\d*|autumn\s*festival", re.IGNORECASE)),
    ("other", re.compile(r"\b(?:M\d+)\b|\b(?:C\d{2,3})\b", re.IGNORECASE)),
]

_WS_RE = re.compile(r"\s+")
_BRACKET_ANY = re.compile(r"[\(\)\[\]【】（）〔〕「」『』]")
_NON_WORD = re.compile(r"[^0-9a-z\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]+")

# --------------------------------------------------------------------------
# 假名 -> 罗马字
# --------------------------------------------------------------------------

_HIRA = (
    "あいうえお"
    "かきくけこがぎぐげご"
    "さしすせそざじずぜぞ"
    "たちつてとだぢづでど"
    "なにぬねの"
    "はひふへほばびぶべぼぱぴぷぺぽ"
    "まみむめも"
    "やゆよ"
    "らりるれろ"
    "わをん"
)
_ROMAJI = (
    "a i u e o "
    "ka ki ku ke ko ga gi gu ge go "
    "sa shi su se so za ji zu ze zo "
    "ta chi tsu te to da ji zu de do "
    "na ni nu ne no "
    "ha hi fu he ho ba bi bu be bo pa pi pu pe po "
    "ma mi mu me mo "
    "ya yu yo "
    "ra ri ru re ro "
    "wa wo n"
).split()

_KANA_ROMAJI: dict[str, str] = dict(zip(_HIRA, _ROMAJI))

# 拗音 / 特殊组合(先于单字匹配)
_DIGRAPHS: dict[str, str] = {
    "きゃ": "kya", "きゅ": "kyu", "きょ": "kyo", "ぎゃ": "gya", "ぎゅ": "gyu", "ぎょ": "gyo",
    "しゃ": "sha", "しゅ": "shu", "しょ": "sho", "じゃ": "ja", "じゅ": "ju", "じょ": "jo",
    "ちゃ": "cha", "ちゅ": "chu", "ちょ": "cho", "ぢゃ": "ja", "ぢゅ": "ju", "ぢょ": "jo",
    "にゃ": "nya", "にゅ": "nyu", "にょ": "nyo",
    "ひゃ": "hya", "ひゅ": "hyu", "ひょ": "hyo", "びゃ": "bya", "びゅ": "byu", "びょ": "byo",
    "ぴゃ": "pya", "ぴゅ": "pyu", "ぴょ": "pyo",
    "みゃ": "mya", "みゅ": "myu", "みょ": "myo",
    "りゃ": "rya", "りゅ": "ryu", "りょ": "ryo",
    "ふぁ": "fa", "ふぃ": "fi", "ふぇ": "fe", "ふぉ": "fo",
    "うぃ": "wi", "うぇ": "we", "うぉ": "wo",
    "てぃ": "ti", "でぃ": "di", "とぅ": "tu", "どぅ": "du",
    "しぇ": "she", "じぇ": "je", "ちぇ": "che",
    "ゔぁ": "va", "ゔぃ": "vi", "ゔぇ": "ve", "ゔぉ": "vo",
    "くぁ": "kwa", "くぃ": "kwi", "くぇ": "kwe", "くぉ": "kwo",
}

_SMALL_TSU = "っッ"


def _kata_to_hira(text: str) -> str:
    out = []
    for ch in text:
        code = ord(ch)
        if 0x30A1 <= code <= 0x30F6:  # 片假名 -> 平假名
            out.append(chr(code - 0x60))
        else:
            out.append(ch)
    return "".join(out)


def kana_to_romaji(text: str) -> str:
    """把平假名 / 片假名转写成赫本式罗马字(长音符省略)."""
    if not text:
        return ""
    src = _kata_to_hira(text)
    out: list[str] = []
    i = 0
    n = len(src)
    while i < n:
        pair = src[i : i + 2]
        if len(pair) == 2 and pair in _DIGRAPHS:
            out.append(_DIGRAPHS[pair])
            i += 2
            continue
        ch = src[i]
        if ch in _SMALL_TSU:
            i += 1
            nxt = src[i : i + 2]
            romaji = _DIGRAPHS.get(nxt) or _KANA_ROMAJI.get(src[i : i + 1], "")
            if romaji:
                out.append(romaji[0])
                continue
            i += 1
            continue
        if ch == "ー":  # 长音符: 重复前一个元音
            if out:
                last = out[-1]
                vowel = next((v for v in reversed(last) if v in "aiueo"), "")
                if vowel:
                    out.append(vowel)
            i += 1
            continue
        if ch in _KANA_ROMAJI:
            out.append(_KANA_ROMAJI[ch])
        else:
            out.append(ch)
        i += 1
    return "".join(out)


# --------------------------------------------------------------------------
# 规范化
# --------------------------------------------------------------------------


def to_halfwidth(text: str) -> str:
    """全角转半角 + 标点统一 + NFKC.

    容忍非字符串输入(适配器有时会把数字/None 混进来)。
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(_FULLWIDTH_TABLE).translate(_PUNCT_TABLE)
    return text


def strip_event_markers(text: str) -> str:
    """剔除 M3-2026秋 / C107 / 例大祭 等事件标记."""
    if not text:
        return ""
    out = text
    for _, pattern in EVENT_PATTERNS:
        out = pattern.sub(" ", out)
    return out


def clean_whitespace(text: str) -> str:
    return _WS_RE.sub(" ", text or "").strip()


def norm_key(text: str, *, romaji: bool = True, drop_noise: bool = True) -> str:
    """生成用于比较的强规范化 key.

    步骤: 半角化 -> 去事件标记 -> 去噪声括号/日期括号 -> 假名转罗马字(可选)
          -> 只保留字母数字与 CJK -> 小写化.
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    if not text:
        return ""
    s = to_halfwidth(text)
    s = strip_event_markers(s)
    if drop_noise:
        s = _NOISE_BRACKETS.sub(" ", s)
        s = _DATE_ONLY_BRACKET.sub(" ", s)
    if romaji:
        s = kana_to_romaji(s)
    s = s.lower()
    # 英文冠词 / 常见无意义词
    s = re.sub(r"\b(the|a|an|and|feat|ft|presents?|records?|music)\b", " ", s)
    s = _NON_WORD.sub(" ", s)
    return clean_whitespace(s)


def norm_title(title: str) -> str:
    """标题规范化: 去掉后缀括号但保留歌名主体."""
    if title is None:
        return ""
    if not isinstance(title, str):
        title = str(title)
    if not title:
        return ""
    s = to_halfwidth(title)
    s = strip_event_markers(s)
    # 去掉常见的 "Artist - Title" 前缀形式
    s = re.sub(r"^[^\-]{2,40}\s+-\s+", "", s) if s.count(" - ") == 1 else s
    s = _DATE_ONLY_BRACKET.sub(" ", s)
    s = _NOISE_BRACKETS.sub(" ", s)
    s = re.sub(r"[\(\[【（][^\)\]】）]{0,80}[\)\]】）]", " ", s)
    s = clean_whitespace(s)
    return s.strip(" -_~·.。:：")


def _has_kana(text: str) -> bool:
    return any(0x3040 <= ord(ch) <= 0x30FF for ch in text or "")


def title_variants(title: str) -> list[str]:
    """为检索生成多个候选写法(原始 -> 去括号 -> 罗马字 -> 纯 ASCII).

    只有在标题确实含假名时才生成罗马字 / 纯 ASCII 变体, 否则会产出
    ``幻想郷の記憶 -> "no"`` 这类毫无意义的检索词。
    """
    variants: list[str] = []
    seen: set[str] = set()

    def add(v: str) -> None:
        v = clean_whitespace(v)
        if v and v.lower() not in seen:
            seen.add(v.lower())
            variants.append(v)

    add(clean_whitespace(to_halfwidth(title)))
    stripped = norm_title(title)
    add(stripped)
    if not stripped or not _has_kana(stripped):
        return variants
    romaji = clean_whitespace(kana_to_romaji(stripped))
    add(romaji)
    ascii_only = clean_whitespace(re.sub(r"[^\x20-\x7e]", " ", romaji))
    # 丢掉「假名转换后只剩冠词」的残渣
    letters = re.sub(r"[^a-z0-9]", "", ascii_only.lower())
    if len(letters) >= 3 and len(letters) >= 0.5 * len(re.sub(r"[^a-z0-9]", "", romaji.lower())):
        add(ascii_only)
    return variants


def slugify(text: str, *, max_len: int = 80) -> str:
    """稳定 slug(用于 album key / 文件名)."""
    if not text:
        return "untitled"
    s = norm_key(text, romaji=False)
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    if len(s) > max_len:
        s = s[:max_len].rstrip("-")
    return s or "untitled"


# --------------------------------------------------------------------------
# 相似度
# --------------------------------------------------------------------------


def similarity(a: str, b: str) -> float:
    """0..1 的字符串相似度(基于规范化后的 SequenceMatcher)."""
    ka, kb = norm_key(a), norm_key(b)
    if not ka or not kb:
        return 0.0
    if ka == kb:
        return 1.0
    return difflib.SequenceMatcher(None, ka, kb, autojunk=False).ratio()


def token_set_ratio(a: str, b: str) -> float:
    """词集合 Jaccard 相似度, 对词序不敏感."""
    ta = set(norm_key(a, romaji=False).split())
    tb = set(norm_key(b, romaji=False).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def best_similarity(candidate: str, targets: Iterable[str]) -> float:
    best = 0.0
    for t in targets:
        if not t:
            continue
        s = max(similarity(candidate, t), token_set_ratio(candidate, t) * 0.95)
        if s > best:
            best = s
            if best >= 1.0:
                break
    return best


def combined_score(a: str, b: str) -> float:
    """两段文本的综合分: 序列相似度与词集相似度取加权最大."""
    return max(similarity(a, b), token_set_ratio(a, b) * 0.92)


def track_overlap(titles_a: Sequence[str], titles_b: Sequence[str], *, threshold: float = 0.85) -> float:
    """曲目表重合率(用于确认同一张专辑)."""
    if not titles_a or not titles_b:
        return 0.0
    na = [norm_key(t) for t in titles_a if t and norm_key(t)]
    nb = [norm_key(t) for t in titles_b if t and norm_key(t)]
    if not na or not nb:
        return 0.0
    small, large = (na, nb) if len(na) <= len(nb) else (nb, na)
    hits = 0
    for t in small:
        for u in large:
            if t == u or difflib.SequenceMatcher(None, t, u, autojunk=False).ratio() >= threshold:
                hits += 1
                break
    return hits / len(small)


def parse_duration(value: str | int | None) -> int | None:
    """'3:45' / '1:02:03' / 225 -> 秒."""
    if value is None:
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    s = str(value).strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    parts = s.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    return None


def format_duration(seconds: int | None) -> str:
    if seconds is None:
        return ""
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


def is_japanese(text: str) -> bool:
    return any(
        0x3040 <= ord(ch) <= 0x30FF or 0x4E00 <= ord(ch) <= 0x9FFF for ch in (text or "")
    )


def dedupe_preserve_order(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if not it:
            continue
        key = clean_whitespace(it).lower()
        if key and key not in seen:
            seen.add(key)
            out.append(clean_whitespace(it))
    return out
