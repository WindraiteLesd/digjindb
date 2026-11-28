"""外链「链接名」的标准识别名.

编辑页每个外链行有「链接名」和「网址」两栏。用户 m13077 要求: 填完网址能
自动按域名认出标准名(Apple Music / SoundCloud / Spotify / X (Twitter) ...),
包括整段 embed code 也能认成 ``Embed Link (Youtube)`` 这类; 同时保留下拉候选
和自定义名称。

规则按**后缀**匹配(``music.apple.com`` 比 ``apple.com`` 更具体, 所以更长的
排前面), 只看 hostname 的结尾 —— ``on.soundcloud.com`` 也归 SoundCloud。
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

#: (域名后缀, 标准链接名)。**顺序即匹配优先级**, 长后缀必须在前。
LINK_NAME_RULES: tuple[tuple[str, str], ...] = (
    ("music.apple.com", "Apple Music"),
    ("open.spotify.com", "Spotify"),
    ("play.spotify.com", "Spotify"),
    ("on.soundcloud.com", "SoundCloud"),
    ("soundcloud.com", "SoundCloud"),
    ("music.youtube.com", "YouTube"),
    ("youtube-nocookie.com", "YouTube"),
    ("youtu.be", "YouTube"),
    ("youtube.com", "YouTube"),
    ("x.com", "X (Twitter)"),
    ("twitter.com", "X (Twitter)"),
    ("bandcamp.com", "Bandcamp"),
    ("nicovideo.jp", "niconico"),
    ("nico.ms", "niconico"),
    ("bilibili.com", "bilibili"),
    ("instagram.com", "Instagram"),
    ("booth.pm", "BOOTH"),
    ("melonbooks.co.jp", "Melonbooks"),
    ("amazon.co.jp", "Amazon"),
    ("amazon.com", "Amazon"),
    ("linktr.ee", "linktr.ee"),
    ("lit.link", "lit.link"),
    ("note.com", "note"),
    ("tunecore.co.jp", "TuneCore"),
    ("diverse.direct", "DIVERSE DIRECT"),
    # 用户 m17600
    ("shop.geekjack.net", "Geek Jack"),
    ("geekjack.net", "Geek Jack"),
    ("tanocstore.net", "TANO*C Store"),
    # 用户 m20229: linkco.re 是日本同人常用的「多平台试听聚合」,
    # 一个链接里挂着 Apple Music / Spotify / YouTube 等。
    ("linkco.re", "Stream Links"),
)

#: 下拉候选: 上面那些名字去重后按出现顺序, 外加 embed 两种。
LINK_NAME_CHOICES: tuple[str, ...] = tuple(
    dict.fromkeys([name for _, name in LINK_NAME_RULES]
                  + ["Embed Link (Youtube)", "Embed Link (SoundCloud)"])
)

#: embed code 里出现这些字样就认成对应播放器
_EMBED_HINTS: tuple[tuple[str, str], ...] = (
    ("soundcloud", "Embed Link (SoundCloud)"),
    ("youtube", "Embed Link (Youtube)"),
    ("youtu.be", "Embed Link (Youtube)"),
    ("youtube-nocookie", "Embed Link (Youtube)"),
)

_IFRAME_RE = re.compile(r"<iframe\b", re.I)
_SRC_RE = re.compile(r"""\bsrc\s*=\s*["']([^"']+)["']""", re.I)


def _host_of(url: str) -> str:
    """取出 hostname(小写); 相对路径/坏串返回空串."""
    text = (url or "").strip()
    if not text:
        return ""
    if "//" not in text:
        text = "https://" + text.lstrip("/")
    try:
        host = urlparse(text).hostname or ""
    except ValueError:
        return ""
    return host.lower().strip(".")


def guess_link_name(value: Any) -> str:
    """按网址/embed code 猜一个标准链接名; 猜不出返回空串.

    >>> guess_link_name("https://music.apple.com/jp/album/1")
    'Apple Music'
    >>> guess_link_name('<iframe src="https://w.soundcloud.com/player/...">')
    'Embed Link (SoundCloud)'
    >>> guess_link_name("https://example.com/x")
    ''
    """
    text = str(value or "").strip()
    if not text:
        return ""
    # 整段 embed code: 先看是不是 iframe, 再按出现的平台字样判断
    if _IFRAME_RE.search(text):
        low = text.lower()
        src_match = _SRC_RE.search(text)
        src = src_match.group(1) if src_match else ""
        host = _host_of(src)
        for needle, label in _EMBED_HINTS:
            if needle in host or needle in low:
                return label
        return ""
    host = _host_of(text)
    if not host:
        return ""
    for suffix, label in LINK_NAME_RULES:
        if host == suffix or host.endswith("." + suffix):
            return label
    return ""


def name_choices() -> list[str]:
    """下拉候选(去重后)."""
    return list(LINK_NAME_CHOICES)