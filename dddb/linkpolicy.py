"""对外链接政策: 哪些站点的链接**不允许**出现在公开端.

**为什么单独放在 `dddb/` 而不是 `dddb/public/`**: 这份名单是"对其它站点"的合规政策,
不是用户端自己的数据。用户明确要求用户端代码/数据里**不出现数据来源名**
(`tests/test_public_events_labels.py` 有目录级守卫), 所以把名单放在 public 包外,
由 public 端 import 使用 —— 政策生效, 不变量也不破。

封面可以缓存到本地或外接对象存储后再分发, 但公开端**绝不能把访客导向**
这些站点的条目页。
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

#: **禁止对外跳转的数据类网站**。只按 host 判定(含子域), 与路径无关:
#: ``https://vgmdb.net/album/1``、``https://lain.bgm.tv/pic/cover/x.jpg``、
#: ``https://www.dojin-music.info/``、``https://chii.in/`` 全部命中。
BLOCKED_LINK_HOSTS: tuple[str, ...] = (
    "vgmdb.net",
    "bgm.tv",
    "bangumi.tv",
    "chii.in",
    "dojin-music.info",
    "doujinstyle.com",
    "dizzylab.net",
    "discogs.com",
    "touhoudb.com",
    "thwiki.cc",
    "vgm.io",
)

#: 一律拒的协议: ``javascript:`` / ``data:`` 是 XSS, 其余不是 http(s)。
BLOCKED_LINK_SCHEMES: tuple[str, ...] = (
    "javascript:",
    "jscript:",
    "livescript:",
    "vbscript:",
    "data:",
    "file:",
    "blob:",
    "about:",
)


def host_of(url: str) -> str:
    """取 URL 的 host(小写、去端口); 解析不了返回空串."""
    try:
        return (urlsplit(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""


def is_blocked_host(host: str) -> bool:
    """这个 host(或它的子域)是否属于被禁止跳转的数据站."""
    text = str(host or "").strip().lower().rstrip(".")
    if not text:
        return False
    return any(text == blocked or text.endswith("." + blocked)
               for blocked in BLOCKED_LINK_HOSTS)


def is_blocked_link(url: str) -> bool:
    """这个 URL 是否不允许出现在公开端(协议黑名单 或 host 在禁止名单里)."""
    raw = str(url or "").strip()
    if not raw:
        return True
    lowered = raw.lower()
    if any(lowered.startswith(scheme) for scheme in BLOCKED_LINK_SCHEMES):
        return True
    if not re.match(r"^https?://", lowered):
        return True                      # 只允许 http(s)
    return is_blocked_host(host_of(raw))


__all__ = [
    "BLOCKED_LINK_HOSTS", "BLOCKED_LINK_SCHEMES",
    "host_of", "is_blocked_host", "is_blocked_link",
]
