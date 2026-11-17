"""标签"有没有检索意义"的**唯一判据**(后端清理 + 用户端展示共用).

为什么单独一个模块: 规则只该有一份。以前出现过"改了一处、另一处成漏洞"
(`_URL_PRIORITY` 删了却仍在用、`Credit` 漏导入、`SourceError` 漏导入),
所以这里把判据抽出 `dddb/` 顶层, `dddb/db.py` 与 `dddb/public/data.py` 都 import 它。

分类依据(实测本库 391 种 / 5164 次):

    展会名/会次        11 种 · 2400 次   -> 结构化字段 `event` 已经存了, 标签是重复
    纯年份              1 种 ·  135 次   -> 结构化字段 `year` 已经存了
    虚拟歌姬/歌声合成   13 种 ·  190 次   -> 不是"流派", 也不是检索维度(用户明确要求去掉)
    平台/格式           4 种 ·   13 次   -> 介质/载体, 不是内容属性
    杂词                1 种 ·    9 次   -> "誤登録" 这类根本不是标签

保留: 风格流派 / 专辑性质 / 演唱形式 / 编曲乐器 / 年龄分级 / 题材(东方等) / 社团相关。
"""

from __future__ import annotations

import re
from typing import Any

# --------------------------------------------------------------------------
# ① 展会名 / 会次 —— `M3-2026秋` `M3-57` `C108` `例大祭` 之类(含 resale 后缀)
# --------------------------------------------------------------------------
_EVENT_RE = re.compile(
    r"^(?:"
    r"M3(?:[-\s]?\d{2,4})?(?:[春秋])?"          # M3 / M3-57 / M3-2026秋
    r"|C\d{2,3}"                                  # C108 (Comiket)
    r"|[春秋]?季?例大祭"                          # 例大祭 / 秋季例大祭
    r"|紅?楼夢"                                   # 紅楼夢
    r"|コミケ(?:ット)?\d*"                        # コミケ / コミケット
    r"|Comiket\s*\d*"
    r"|M3-\d{4}[春秋]"
    r")"
    r"(?:\s*[\(（](?:resale|再販|再版)[\)）])?$",
    re.I,
)

#: 纯年份(结构化字段 year 已存)
_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")

#: 平台 / 载体 / 版本形态 —— 不是内容属性
_FORMAT_WORDS = frozenset({
    "download code", "ダウンロードコード", "dl", "dtm", "mini cd", "cd-r", "cdr",
    "cd", "cds", "disc", "digital", "hires", "hi-res", "cassette", "vinyl", "dvd",
    "md", "8cm cd", "ep", "album", "single", "compilation", "omnibus",
})

#: 虚拟歌姬 / 歌声合成软件名 —— 用户明确说不算检索维度
_VOICE_SYNTH_WORDS = frozenset({
    "vocaloid", "vocaloid2", "synthesizer v", "synthv", "cevio", "voiceroid",
    "aiボーカル", "合成音声", "ボカロ",
    "初音ミク", "初音未來", "鏡音リン", "鏡音レン", "巡音ルカ", "meiko", "kaito",
    "重音テト", "ずんだもん", "東北ずん子", "可不", "flower", "gumi", "ia",
})

#: 根本不是标签的杂词
_MISC_WORDS = frozenset({"誤登録", "误登录", "誤登录", "自动生成", "占位"})


def _norm(tag: Any) -> str:
    text = str(tag or "").strip().strip("#").strip()
    return re.sub(r"\s+", " ", text).lower()


#: 组合标签的分隔符: `CD + Download Code` / `Rock / Metal` / `A & B`
_COMPOSITE_SEP_RE = re.compile(r"\s*(?:[+＋/&＆,、·・]|\band\b)\s*", re.I)


def _is_useless_atom(raw: str) -> bool:
    """单个词(不含分隔符)是否无用."""
    low = _norm(raw)
    if not low:
        return True
    if _YEAR_RE.match(low):
        return True
    if _EVENT_RE.match(raw):
        return True
    if low in _FORMAT_WORDS or low.replace("-", " ") in _FORMAT_WORDS:
        return True
    if low in _VOICE_SYNTH_WORDS or low.replace(" ", "") in _VOICE_SYNTH_WORDS:
        return True
    if raw in _MISC_WORDS:
        return True
    return False


def is_useless_tag(tag: Any) -> bool:
    """这个标签**没有检索意义**, 不该出现在用户端, 也不该留在库里.

    只做"整词"判断(`frozenset` 精确匹配 + 少量正则), 不做模糊包含 ——
    否则 ``Rock`` / ``Vocal`` 这类真流派会被误伤。
    """
    raw = str(tag or "").strip().strip("#").strip()
    if not raw:
        return True                                  # 空标签留着也没用
    # **组合标签**: `CD + Download Code` 这类要拆段看 ——
    # 所有段都无用才算无用; 只要有一段有意义(`Rock + Metal`)就保留整体。
    parts = [part for part in _COMPOSITE_SEP_RE.split(raw) if part.strip()]
    if len(parts) > 1:
        return all(_is_useless_atom(part) for part in parts)
    return _is_useless_atom(raw)


#: 给用户端展示/后端清理复用的全部规则(便于写文档与测试)
USELESS_TAG_RULES = {
    "event": _EVENT_RE,
    "year": _YEAR_RE,
    "format": _FORMAT_WORDS,
    "voice_synth": _VOICE_SYNTH_WORDS,
    "misc": _MISC_WORDS,
}

__all__ = ["is_useless_tag", "USELESS_TAG_RULES"]