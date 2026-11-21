"""网易云歌词抓取.

EXEC_WIKI=BGM 的关键数据来源就是**歌词** —— 同人社团常把制作人员
写进歌词区。要把这个能力接进 dddb, 需要:

    client.lyric(song_id) -> {"lrc": ..., "tlyric": ...}

接口: ``/lyric`` (weapi), 参数 ``{"id": song_id, "lv": -1, "kv": -1, "tv": -1}``
返回 ``{"lrc": {"lyric": "..."}, "tlyric": {...}}``。

歌词里有两种内容:
    ① 带时间戳的真歌词 ``[00:12.34] 歌词``
    ② 纯文本的制作人员信息 ``制作人：xxx`` (我们要的)

这里负责**去掉时间戳**并保留文本, 再交给 ``lyrics_credits`` 解析。
"""

from __future__ import annotations

import re
from typing import Any

#: LRC 时间戳: [00:12.34] / [00:12] / [ar:xxx] 这类元信息标签
_TS = re.compile(r"\[\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?\]")
_META = re.compile(r"^\[(?:ar|ti|al|by|offset|re|ve|length)\s*:.*\]$", re.I)


def strip_lrc(text: Any) -> str:
    """去掉 LRC 时间戳与元信息行, 只留文本."""
    out: list[str] = []
    for line in str(text or "").replace("\r\n", "\n").split("\n"):
        line = _TS.sub("", line).strip()
        if not line:
            continue
        if _META.match(line):
            continue
        out.append(line)
    return "\n".join(out)


def lyric_text(payload: Any) -> str:
    """从 ``/lyric`` 的响应里取出**原文歌词**(已去时间戳).

    兼容几种返回形态:
        {"lrc": {"lyric": "..."}}
        {"lyric": "..."}
        "..." (直接是字符串)
    """
    if isinstance(payload, str):
        return strip_lrc(payload)
    if not isinstance(payload, dict):
        return ""
    lrc = payload.get("lrc")
    if isinstance(lrc, dict):
        return strip_lrc(lrc.get("lyric") or lrc.get("txt") or "")
    if isinstance(lrc, str):
        return strip_lrc(lrc)
    for key in ("lyric", "txt", "text"):
        v = payload.get(key)
        if isinstance(v, str) and v.strip():
            return strip_lrc(v)
    return ""


def has_credit_info(text: Any) -> bool:
    """这段文本里像不像有制作人员信息?

    判据(任一即可):
        * 有 2 行以上"职位: 人名"
        * 含有明确的职位词
    """
    from .lyrics_credits import parse_credit_lines

    result = parse_credit_lines(text)
    if result.total >= 2:
        return True
    if result.total >= 1 and result.skipped <= 2:
        return True
    return False


__all__ = ["has_credit_info", "lyric_text", "strip_lrc"]
