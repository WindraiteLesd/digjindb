"""外链 -> 嵌入式播放器.

**只认用户自己粘贴的 embed code**, 不对「纯链接」自动生成播放器:

    纯 URL           -> 只显示外链按钮(详情页不出现播放器)
    embed code       -> 抽出 iframe 的 src / 高度, 在按钮**下方**渲染播放器

为什么不做「识别到 YouTube 链接就自动嵌入」:
    用户明确要求不要 —— 2123 条外链里有大量纯链接(频道页 / 个人主页),
    那些根本没东西可播, 自动嵌入只会塞满一排空播放器。

合规约束(与 /link 完全一致):
    页面 HTML 里**不出现外部 URL / 域名**。播放器的 src 仍然走本站
    ``/embed/<key>/<n>``, 由服务端 302 到 embed code 里那个真实地址。
    embed code 里的 <a> 归属行/文字(如 "标题 · 作者")一律丢弃。

支持的平台(其余 embed code 也照收, 只要 iframe src 是 http(s)):
    * YouTube   (youtube.com / youtu.be / youtube-nocookie.com)
    * SoundCloud (w.soundcloud.com / soundcloud.com)
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit


#: 平台 key
EMBED_YOUTUBE = "youtube"
EMBED_SOUNDCLOUD = "soundcloud"
EMBED_OTHER = "other"

#: 默认高度(px)。embed code 里自带 height 时优先用它。
EMBED_HEIGHT: dict[str, int] = {
    EMBED_YOUTUBE: 232,
    EMBED_SOUNDCLOUD: 166,
    EMBED_OTHER: 200,
}

#: 高度上限, 防止用户塞一个 2000px 的 iframe 把页面撑爆
MAX_HEIGHT = 480
MIN_HEIGHT = 80

def _host_of(url: str) -> str:
    """URL 的 host(小写, 去端口); 解析不了返回空串."""
    try:
        return (urlsplit(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""


_IFRAME_RE = re.compile(r"<iframe\b[^>]*>", re.IGNORECASE)
_SRC_RE = re.compile(
    r"""\bsrc\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.IGNORECASE)
_HEIGHT_RE = re.compile(
    r"""\bheight\s*=\s*(?:"(\d+)"|'(\d+)'|(\d+))""", re.IGNORECASE)
_WIDTH_RE = re.compile(
    r"""\bwidth\s*=\s*(?:"(\d+)"|'(\d+)'|(\d+))""", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")

_HOST_PLATFORM: tuple[tuple[str, str], ...] = (
    ("youtube.com", EMBED_YOUTUBE),
    ("youtube-nocookie.com", EMBED_YOUTUBE),
    ("youtu.be", EMBED_YOUTUBE),
    ("soundcloud.com", EMBED_SOUNDCLOUD),
)


def looks_like_embed(value: Any) -> bool:
    """这个值是不是一整段 embed code(含 ``<iframe``)."""
    return bool(_IFRAME_RE.search(str(value or "")))


def _pick(match: re.Match[str] | None) -> str:
    if match is None:
        return ""
    for group in match.groups():
        if group:
            return group.strip()
    return ""


def platform_of_url(url: Any) -> str:
    """host -> 平台 key(用于显示标题, 不做任何嵌入决策)."""
    host = _host_of(str(url or "").strip())
    if not host:
        return ""
    for suffix, platform in _HOST_PLATFORM:
        if host == suffix or host.endswith("." + suffix):
            return platform
    return ""


def extract_embed(value: Any) -> dict[str, Any] | None:
    """从 embed code 里抽 ``{"src", "height", "platform"}``; 不是 embed 返回 ``None``.

    取第一条 ``<iframe>``; ``src`` 必须是 http(s)(拒绝 javascript:/data:)。
    """
    text = str(value or "")
    if not text:
        return None
    match = _IFRAME_RE.search(text)
    if match is None:
        return None
    tag = match.group(0)
    src = _pick(_SRC_RE.search(tag))
    if not src or not src.lower().startswith(("http://", "https://")):
        return None

    platform = platform_of_url(src) or EMBED_OTHER
    default_height = EMBED_HEIGHT.get(platform, EMBED_HEIGHT[EMBED_OTHER])
    height_text = _pick(_HEIGHT_RE.search(tag))
    if platform == EMBED_YOUTUBE:
        # **YouTube 一律用 16:9 自适应**, 不采用 code 里的 height。
        # 原因: code 里的 height="315" 是对 560px 宽算的; 我们面板 ~1200px 宽,
        # 固定 315px 会变成一条很扁的横条, 视频上下大片黑边(用户反馈"太矮")。
        height = 0
    elif height_text.isdigit():
        height = int(height_text)
        # 常见 embed code 会写 height="300"; 夹到合理区间
        height = max(MIN_HEIGHT, min(MAX_HEIGHT, height))
    else:
        height = default_height

    return {
        "src": src,
        "height": height,
        "platform": platform,
        # 用户 embed code 里的 <a>文字(标题·作者) 丢弃, 但保留纯文本给 aria
        "plain_text": _plain_text(text),
    }


def _plain_text(value: str) -> str:
    """embed code 里的可见文字(用于播放器 aria-label 补充), 去标签/压缩空白."""
    text = _TAG_RE.sub(" ", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()


#: 旧的 API 名字保留(模板/数据层已经改用 extract_embed)
def embed_info(value: Any) -> dict[str, Any] | None:
    return extract_embed(value)


def platform_label(platform: Any) -> str:
    return {
        EMBED_YOUTUBE: "YouTube",
        EMBED_SOUNDCLOUD: "SoundCloud",
    }.get(str(platform or ""), "嵌入式")


def embed_target(src: Any, *, soundcloud_url: Any = "") -> str:
    """``/embed`` 中转的目标地址.

    SoundCloud 的 embed code 常常直接给 ``w.soundcloud.com/player/?url=...``,
    这种原样用; 若给的是 ``soundcloud.com/...`` 轨道页, 转成官方 widget 地址。
    """
    text = str(src or "").strip()
    if not text:
        return ""
    host = _host_of(text)
    if host.endswith("soundcloud.com") and not host.startswith("w."):
        return (
            "https://w.soundcloud.com/player/?url="
            + quote(text, safe="")
            + "&color=%23ff5500&auto_play=false&hide_related=true"
            "&show_comments=false&show_user=false&show_reposts=false"
            "&visual=false"
        )
    return text
