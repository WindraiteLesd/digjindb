"""用户端站点的 HTTP 服务(``http.server``, 零依赖).

可以独立起服务::

    python -m dddb.public.site --port 8940

也可以被别的进程内嵌::

    from dddb.public.site import build_site, make_server
    httpd = make_server(build_site(), port=8940)
    httpd.serve_forever()

**读写边界**:

* 访客 / 普通用户的读路径走 ``mode=ro``(:class:`dddb.public.data.ReadOnlyStore`),
  任何写语句都会被 SQLite 拒绝;
* 只有**认证编辑者**的写路径才开一个可写连接(:class:`dddb.db.Store`, 按需创建),
  写操作一律走 POST, 并且同时校验「同源」与「CSRF token」;
* 非 GET/HEAD 而鉴权没过 -> **403**(不再笼统地回 405)。
* 用户账号存在**独立**的 ``data/public_users.sqlite3``(:mod:`dddb.public.users`),
  与专辑主库没有任何耦合。
"""

from __future__ import annotations

import json

from .embed import extract_embed as _extract_embed
from . import i18n as I18N

import argparse
import base64
import ipaddress
import logging
import math
import mimetypes
import os
import re
import secrets
import socket
import sqlite3
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from xml.sax.saxutils import escape as xml_escape

from .eventseries import (label_from_vol, parse_event_name,
                            season_from_date, vol_from_label,
                            year_from_date)
from . import auth
from . import templates as T

#: 个人中心页面片段要拼 HTML, 复用模板层的转义/隐藏域工具
esc = T.esc
_hidden = T._hidden
from . import verify as V
from .data import (
    DEFAULT_PER,
    DEFAULT_SORT,
    PublicData,
    PublicDataError,
    ReadOnlyStore,
    SORTS,
    coerce_per,
    outbound_link_reject_reason,
    parse_filters,
)
from .ratelimit import RateLimiter, classify_request, client_ip_from_headers
from ..imageutil import probe_image_size
from ..trackparse import parse_tracklist
from .. import creditparse
from .users import (
    LOGIN_INACTIVE,
    LOGIN_UNVERIFIED_EMAIL,
    REQUEST_APPROVED,
    REQUEST_PENDING,
    REQUEST_REJECTED,
    ROLE_EDITOR,
    ROLE_SUPERUSER,
    ROLE_VIEWER,
    ROLES,
    UserStore,
    UserStoreError,
    default_users_path,
    email_problem,
    env_users_path,
    normalize_email,
)

logger = logging.getLogger("dddb.public")

#: 静态资源目录(与模板同包)
STATIC_DIR = Path(__file__).with_name("static")

#: 允许直接发送的静态文件白名单 —— 不按用户输入拼路径, 从根上杜绝目录穿越
STATIC_FILES = ("public.css", "public.js", "cover-crop.js", "infobox.js",
                "favicon.svg", "logo.svg")

#: 用户端默认端口
DEFAULT_PORT = 8940

#: 兼容旧调用方(``dddb/cli.py`` 仍会 import 这个名字)。用户端**没有任何**外部
#: 管理面板跳转, 所以这里恒为空串 —— 就算旧参数传进来也不会渲染出链接。
DEFAULT_ADMIN_URL = ""

#: 分页/分组参数
_PER_GROUP = 8
_PER_GROUPS = 12
_PER_CIRCLES = 60

#: POST 请求体的上限.
#:
#: **不能设太小**: 编辑表单支持上传图片(单张上限 2MB), 而一个表单里可能有
#: 封面 / LOGO / 海报等多个文件字段。256KB 会让"传一张稍大的图"直接 413,
#: 表现为"提交内容过大, 内容被清空不保存, 图片不上传"(用户实测)。
#: 这里放到 8MB: 足够多张 2MB 图 + 文本, 同时仍是个有限护栏。
#: 用户 m18831: 单张图上限从 2MB 提到 8MB —— 很多导出封面本身就超过 2MB,
#: 2MB 会把它们整张拒掉。请求体上限跟着放大到 20MB(多图 + 文本)。
MAX_BODY = 20 * 1024 * 1024

#: 单张图片上限(与 _save_*_image 里的检查一致)
MAX_IMAGE_BODY = 8 * 1024 * 1024
#: 新建专辑时随表单带上的「其他照片」最多几张(防止一个请求塞一张相册)
MAX_NEW_PHOTOS = 12

#: infobox 里带的封面先"缓存"到服务端(不写库), 提交时再转正。
#: 超过这个时间的缓存图会被下一次缓存/提交顺手清掉。
PENDING_COVER_TTL = 24 * 3600
#: 缓存图最长边小于它 -> 前端提示"原图偏小, 建议替换"
PENDING_COVER_SMALL = 500

#: 表单里重复行(词条/外链/曲目)的上限
MAX_ROWS = 300

#: 个人中心的写路由 —— **登录即可**, 不走"编辑者"门槛(见 _handle_write 分档)
_MEMBER_WRITE_ROUTES = frozenset({
    "messages", "messages_send", "notifications_read", "feedback",
    "favorites_toggle", "follows_toggle",
    "account_privacy", "account_profile", "account_avatar",
    "album_delete", "album_comment", "comment_delete", "comment_report", "album_revert",
    "account_password", "account_delete", "account_security_code", "comment_like",
    "artist_comment", "circle_comment", "new_entity",
    "artist_edit", "circle_edit", "event_edit", "tag_edit",
})

#: 页面响应头: 公开端只有一份同源脚本(``script-src 'self'``), 没有内联脚本;
#: 封面/头像是图片, 允许 https 与 data:; 内联 style 仅用于数据条宽度。
_CSP = (
    # img-src 要允许 ``blob:`` —— 裁切弹窗用 createObjectURL 做本地预览.
    # 只给 data: 会让 blob 预览被拦(用户实测: "图片读取失败")。
    "default-src 'none'; img-src 'self' https: data: blob:;"
    " style-src 'self' 'unsafe-inline';"
    " script-src 'self'; font-src 'self'; form-action 'self'; base-uri 'none';"
    # frame-src: 详情页的 SoundCloud / YouTube 播放器走**本站** /embed/<key>/<n> 中转,
    # 所以只需要放行 'self'; 平台域名不进 CSP, 也不进页面 HTML(合规)。
    # frame-src: 外链 embed 播放器(用户粘贴的 <iframe>)要直连平台。
    #   **不能用本站 /embed 中转** —— 浏览器会在 302 之后的目标上再查一次 CSP,
    #   跨域就被拦("已阻止此内容")。所以这里直接放行这三个播放器域。
    " frame-src 'self' https://www.youtube.com https://www.youtube-nocookie.com"
    " https://w.soundcloud.com;"
    # connect-src: 前端用 fetch() 上传封面/附图/裁切结果。
    #   default-src 'none' 会**默认拒绝**一切 fetch —— 缺这一条时
    #   浏览器直接抛 "Failed to fetch"(用户实测: 上传失败)。
    #   只放行本站, 因为所有上传接口都是同源的 /api 或 /<entity>/<key>/cover。
    " connect-src 'self';"
    " frame-ancestors 'none'"
)

_DATE_RE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")
_TIME_RE = re.compile(r"^\d{1,3}:\d{2}(:\d{2})?$")

#: ``sitemap.xml`` / ``sitemap-<n>.xml``
_SITEMAP_RE = re.compile(r"sitemap(?:-(\d+))?\.xml")

#: 站点自己的 robots.txt 里声明不允许抓的路径(编辑 / 账号 / 验证 / 审核 / 搜索)
_ROBOTS_DISALLOW = (
    "/edit",
    "/album/*/edit",
    "/account",
    "/account/*",
    "/verify",
    "/forgot",
    "/reset",
    "/invites",
    "/captcha.svg",
    "/username-check",
    "/verify/*",
    "/review",
    "/users",
    "/login",
    "/register",
    "/logout",
    "/search",
    # 个人中心(全部需要登录, 不该被搜索引擎收录)
    "/messages",
    "/notifications",
    "/feedback",
    "/favorites",
    "/follows",
    "/avatar",
    "/u",
    "/u/*",
)

#: 动态页面的抓取间隔建议
_ROBOTS_CRAWL_DELAY = 1

# --------------------------------------------------------------------------
# 封面合规策略(只允许本地缓存 / 外接对象存储, 绝不 hotlink 源站)
# --------------------------------------------------------------------------

#: 封面落盘的后缀(沿用现有 ``data/covers/<key>.img`` 约定)
COVER_CACHE_SUFFIX = ".img"

#: 在请求线程里拉远程封面的超时上限(秒). 必须有上限 ——
#: 拉不到就给占位图, 不能让一次外部故障把页面拖死。
DEFAULT_COVER_TIMEOUT = 8.0

#: 单张封面允许落盘的最大字节数(超过按拉取失败处理)
MAX_COVER_BYTES = 8 * 1024 * 1024

#: 同一张封面拉取失败后的"负缓存"(秒): 一个列表页要几十张图,
#: 不能每张都在每次请求里重试打对方站。
COVER_FAILURE_TTL = 120.0

#: 响应头: 这张图是从哪来的(local / external / downloaded / cached / placeholder)
COVER_SOURCE_HEADER = "X-DNH-Cover-Source"

#: 外接对象存储/CDN 基址的环境变量(``config.py`` 加字段前的部署口子)
COVER_BASE_ENV = "DNH_PUBLIC_COVER_BASE_URL"

#: 是否允许服务端**联网拉取**远程封面。云端建议设 `0`: 只发本地缓存,
#: 拿不到就回占位图 —— 否则每张没缓存的封面都要等远程超时(实测 24s)。
COVER_FETCH_ENV = "DNH_PUBLIC_COVER_FETCH"


#: 头像允许的图片类型(与 verify/cover 的判定保持同一套嗅探)
_AVATAR_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})

#: 头像落盘/服务端校验的默认上限(``P.AVATAR_MAX_BYTES`` 可用时以它为准)
AVATAR_MAX_BYTES = 512 * 1024

#: 个人中心库文件名(与用户库一样放在 ``data/`` 下, 独立于专辑主库)
PROFILES_DB_FILENAME = "public_profiles.sqlite3"


def _cover_stem(key: Any) -> str:
    """专辑 key -> 封面文件名主干(**稳定且唯一**).

    不能用"把非字母数字替换成 -"那种 slug: 库里绝大多数专辑名是
    中日文, 那样会得到 ``"--"``, 于是**所有中文专辑共用一个封面文件**,
    后传的覆盖先传的(用户实测: 上传后显示的是别的碟的封面)。

    改用 key 的 SHA1 前 16 位: 纯 ASCII、天然唯一、不随系统 locale 变。
    """
    import hashlib

    text = str(key or "")
    if not text:
        return "cover"
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _env_flag(name: str, default: bool = True) -> bool:
    """读布尔型环境变量(``0/false/no/off`` 视为关)."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in ("0", "false", "no", "off", "")


#: 允许落盘的图片类型 —— **不含 svg**: 外站 SVG 可以带脚本, 而我们不做清洗
_COVER_IMAGE_TYPES = frozenset({
    "image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp", "image/x-icon",
})

#: 文件名里绝不允许出现的字符(路径分隔符 / 通配符 / 控制字符)
_UNSAFE_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

#: Windows 保留设备名 —— 带扩展名也算(``con.img`` 会直接打到控制台设备上)
_RESERVED_NAMES = frozenset({
    "con", "prn", "aux", "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
})


def _png_chunk(tag: bytes, payload: bytes) -> bytes:
    """一个 PNG chunk: 长度 + 类型 + 数据 + CRC32."""
    return (
        struct.pack(">I", len(payload)) + tag + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


def build_placeholder_png(size: int = 200) -> bytes:
    """内置封面占位图(纯 stdlib 现场生成, 不读外部文件、不联网).

    远程封面拉不到时必须回**一张真图片**: 404 会让 ``<img>`` 显示破图,
    而"暂无封面"文字块只在"库里根本没有封面记录"时才由模板渲染。
    图案: 深底 + 细外框 + 一条对角斜线(经典的"无图"记号)。
    """
    edge = 3
    background, frame, ink = (26, 27, 34), (86, 90, 104), (140, 145, 160)
    rows: list[bytes] = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            if x < edge or y < edge or x >= size - edge or y >= size - edge:
                row += bytes(frame)
            elif abs(x - y) <= 1:
                row += bytes(ink)
            else:
                row += bytes(background)
        rows.append(b"\x00" + bytes(row))          # 每行前面是 filter 字节
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)   # 8bit RGB
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
        + _png_chunk(b"IEND", b"")
    )


_placeholder_lock = threading.Lock()
_placeholder_png: bytes | None = None


def placeholder_cover() -> bytes:
    """占位图字节(懒生成 + 进程内缓存, 只算一次)."""
    global _placeholder_png
    with _placeholder_lock:
        if _placeholder_png is None:
            _placeholder_png = build_placeholder_png()
        return _placeholder_png


def cover_cache_name(key: Any) -> str:
    """``key -> "<key>.img"``(沿用现有落盘命名); 不安全/过长则返回空串.

    写盘路径绝不能由外部输入决定, 所以这里只接受"一个普通文件名":
    路径分隔符、盘符、通配符、控制字符、``..``、尾点/尾空格一律拒。
    """
    name = str(key or "").strip()
    if not name or len(name) > 200 or name.startswith("."):
        return ""
    if name.endswith((".", " ")) or name != Path(name).name:
        return ""
    if _UNSAFE_NAME_RE.search(name):
        return ""
    if name.split(".", 1)[0].lower() in _RESERVED_NAMES:
        return ""
    return name + COVER_CACHE_SUFFIX


# --------------------------------------------------------------------------
# 路由(纯函数, 便于单测)
# --------------------------------------------------------------------------

#: 一级路径 -> 路由名。``/circles`` 是索引页, ``/circle/<name>`` 是实体页。
_ROUTE_MAP = {
    "": "home",
    "albums": "albums",
    "album": "album",
    "circles": "circles",
    "circle": "circle",
    "artists": "artists",
    "artist": "artist",
    "events": "events",
    "event": "event",
    # 展会名 -> 日期(编辑页发售日自动匹配; 只读 JSON)
    "event-date": "event_date",
    "tags": "tags",
    "tag": "tag",
    "stats": "stats",
    "edit": "edit",
    "new": "new_entity",
    "new": "new",
    "about": "about",
    "static": "static",
    "cover": "cover",
    "photo": "photo",
    "search": "search",
    "favicon.ico": "favicon",
    "link": "link",
    # 外链嵌入播放器 -> 302 到 SoundCloud / YouTube 播放器
    "embed": "embed",
    # 展会词条的本地图片
    "event-logo": "event_logo",
    # 艺人头像 / 社团徽标(新建条目时 _save_new_image 写盘, 这里读回来)
    "logo": "logo",
    # infobox 封面: 先把远程图缓存到服务端(不写库), 提交时再转正。
    # POST /cover-cache/new 建缓存, GET /cover-cache/<token> 取图。
    "cover-cache": "pending_cover",
    # 展会**系列**: 详情页与图片
    # event-series 用**基础名** event_series: 下面的分支再细分
    # /<name> /<name>/edit /new, 直接把 name 设成最终路由会让 /edit 失效
    "event-series": "event_series",
    "event-series-logo": "event_series_logo",
    "event-series-poster": "event_series_poster",
    "like": "comment_like",
    "lang": "lang",
    "login": "login",
    "register": "register",
    "logout": "logout",
    "account": "account",
    # 账号相关: 邮箱验证 / 审核队列 / 用户管理
    "verify": "verify_email",
    "forgot": "forgot",
    "reset": "reset",
    "review": "review",
    "users": "users_admin",
    # 站务看板: 访问记录 + 编辑改动 + 服务器负载(仅超级用户)
    "dashboard": "dashboard",
    # 图形验证码(图片) / 邀请码页
    "username-check": "username_check",
    "captcha.svg": "captcha_svg",
    "invites": "invites",
    # 个人中心: 收藏 / 关注 / 私信 / 通知 / 反馈 / 头像 / 用户主页
    "favorites": "favorites_toggle",
    "follows": "follows_toggle",
    "messages": "messages",
    "comment": "comment",
    "notifications": "notifications",
    "feedback": "feedback",
    "avatar": "avatar",
    "background": "background",
    "u": "user_page",
}

#: 这些路由名必须有第二个路径段, 否则视为 index(或 404)
_ENTITY_ROUTES = frozenset({"album", "circle", "event", "tag", "cover", "static",
                              "event-date", "embed", "event_logo", "event_series_page",
                              "event_series_logo", "event_series_poster", "logo",
                              "pending_cover"})

#: 站务看板的访问日志**不记**这些路由: 它们不是"浏览页面"。
#: 一张专辑详情页会带出 css/js/封面/附图十几个请求, 全记下来以后
#: "最近访问"就成了资源清单, 看板也就没有意义了。
_NO_VISIT_ROUTES = frozenset({
    "static", "cover", "photo", "logo", "avatar", "background",
    "event_logo", "event_series_logo", "event_series_poster",
    "pending_cover", "favicon", "robots", "sitemap", "captcha_svg",
})

#: 索引页路由名 -> 实体路由名(``/circle/<x>`` 缺失时退化为 ``/circles``)
_INDEX_OF = {"album": "albums", "circle": "circles", "event": "events", "tag": "tags"}


def parse_route(path: str) -> dict[str, str]:
    """URL 路径 -> ``{"name": 路由名, "arg": 路径参数}``(纯函数).

    * 查询串会被忽略(由调用方单独解析);
    * 路径段做一次 percent-decode, 所以 ``/album/a%7Cb`` 与 ``/album/a|b`` 等价;
    * ``/album/<key>/edit`` 单独路由成 ``album_edit``(编辑表单 + 写接口);
    * 未知路径返回 ``{"name": "not_found"}``, 由上层渲染 404 页。
    """
    raw = str(path or "/")
    clean = urllib.parse.urlsplit(raw).path or "/"
    if not clean.startswith("/"):
        clean = "/" + clean
    parts = [urllib.parse.unquote(seg) for seg in clean.split("/") if seg != ""]
    if not parts:
        return {"name": "home", "arg": ""}
    head = parts[0].lower()
    if head == "robots.txt":
        return {"name": "robots", "arg": ""}
    sitemap = _SITEMAP_RE.fullmatch(head)
    if sitemap:
        return {"name": "sitemap", "arg": sitemap.group(1) or ""}
    name = _ROUTE_MAP.get(head, "not_found")
    if name == "favicon":
        return {"name": "favicon", "arg": ""}
    if name == "not_found":
        return {"name": "not_found", "arg": clean}
    rest = parts[1:]
    if name == "album" and len(rest) >= 2 and rest[-1].lower() == "edit":
        return {"name": "album_edit", "arg": "/".join(rest[:-1])}
    for kind_name in ("artist", "circle", "event", "tag"):
        if name == kind_name and len(rest) >= 2 and rest[-1].lower() == "edit":
            return {"name": f"{kind_name}_edit", "arg": "/".join(rest[:-1])}
    if name == "circle" and len(rest) >= 2 and rest[-1].lower() == "comment":
        return {"name": "circle_comment", "arg": "/".join(rest[:-1])}
    if name == "artist" and len(rest) >= 2 and rest[-1].lower() == "comment":
        return {"name": "artist_comment", "arg": "/".join(rest[:-1])}
    if name == "album" and len(rest) >= 2 and rest[-1].lower() == "delete":
        return {"name": "album_delete", "arg": "/".join(rest[:-1])}
    # 合并专辑: 把本条目并进另一条(保留目标, 本条目删除)
    if name == "album" and len(rest) >= 2 and rest[-1].lower() == "merge":
        return {"name": "album_merge", "arg": "/".join(rest[:-1])}
    if name == "album" and len(rest) >= 2 and rest[-1].lower() == "comment":
        return {"name": "album_comment", "arg": "/".join(rest[:-1])}
    if name == "album" and len(rest) >= 2 and rest[-1].lower() == "cover":
        return {"name": "album_cover", "arg": "/".join(rest[:-1])}
    # 给个图片地址, 服务端抓下来当封面(编辑页 infobox 的 cover 字段用)
    if name == "album" and len(rest) >= 2 and rest[-1].lower() == "cover-url":
        return {"name": "album_cover_url", "arg": "/".join(rest[:-1])}
    # 外链嵌入播放器: /embed/<album key>/<序号> -> 302 到平台播放器
    if name == "embed" and len(rest) >= 2:
        return {"name": "embed", "arg": "/".join(rest)}
    # 展会系列: /event-series/<name>/edit 与 /event-series/new
    if name == "event_series":
        if len(rest) == 1 and rest[0].lower() == "new":
            return {"name": "event_series_edit", "arg": "", "new": True}
        if len(rest) >= 2 and rest[-1].lower() == "edit":
            return {"name": "event_series_edit", "arg": "/".join(rest[:-1])}
        if rest:
            return {"name": "event_series_page", "arg": "/".join(rest)}
        return {"name": "events", "arg": ""}
    # 附图: /album/<key>/photo, /photo/del, /photo/note
    if name == "album" and len(rest) == 2 and rest[-1].lower() == "photo":
        return {"name": "album_photo", "arg": rest[0]}
    if name == "album" and len(rest) >= 2 and rest[-2].lower() == "photo":
        tail = rest[-1].lower()
        if tail == "del":
            return {"name": "album_photo_del",
                    "arg": "/".join(rest[:-2])}
        if tail == "note":
            return {"name": "album_photo_note",
                    "arg": "/".join(rest[:-2])}
        return {"name": "album_photo", "arg": "/".join(rest)}
    if name == "album" and len(rest) >= 2 and rest[-1].lower() == "history":
        return {"name": "album_history", "arg": "/".join(rest[:-1])}
    # 展会名 -> 日期(编辑页"发售日自动匹配"用, 只读)
    if name == "event-date":
        return {"name": "event_date", "arg": "/".join(rest)}
    if name == "album" and len(rest) == 1 and rest[0].lower() == "revert":
        return {"name": "album_revert", "arg": ""}
    # 个人中心的子路由(只认白名单, 其余一律 404)
    if rest and name in ("messages", "comment", "notifications", "favorites", "follows", "account"):
        sub = rest[0].lower()
        allowed = {
            "messages": {"send": "messages_send"},
            "comment": {"delete": "comment_delete", "report": "comment_report", "like": "comment_like"},
            "notifications": {"read": "notifications_read"},
            "favorites": {"toggle": "favorites_toggle"},
            "follows": {"toggle": "follows_toggle"},
            "account": {"profile": "account_profile", "privacy": "account_privacy",
                        "avatar": "account_avatar", "layout": "account_layout",
                         "background": "account_background", "password": "account_password",
                        "delete": "account_delete", "security-code": "account_security_code"},
        }[name]
        target = allowed.get(sub)
        if target is None or len(rest) > 1:
            return {"name": "not_found", "arg": clean}
        return {"name": target, "arg": ""}
    arg = "/".join(rest)
    if name == "avatar" and not arg:
        return {"name": "not_found", "arg": clean}
    if name == "user_page" and not arg:
        return {"name": "not_found", "arg": clean}
    if not arg and name in _ENTITY_ROUTES:
        return {"name": _INDEX_OF.get(name, "not_found"), "arg": ""}
    return {"name": name, "arg": arg}


def parse_query(query: str) -> dict[str, list[str]]:
    """查询串 -> ``{key: [values]}``(空值丢弃)."""
    if not query:
        return {}
    return {k: v for k, v in urllib.parse.parse_qs(query, keep_blank_values=False).items() if v}


#: 编辑历史对比里逐项检查的字段。
#:
#: **必须是 ``Album.to_dict()`` 里真实存在的键** —— 早期版本写的是
#: ``track_list``(Album 没有这个字段, 曲目在 ``tracks`` 里), 于是曲目改动
#: 永远比不出来(用户 m13077: "对比不上")。
_HISTORY_DIFF_FIELDS: tuple[str, ...] = (
    "title", "title_original", "title_romaji", "circle", "artists",
    "release_date", "event", "catalog_no", "company", "genres", "tags",
    "description", "price", "hall", "booth", "tracks", "credits", "photos",
    "urls", "cover_url", "cover_local",
    # 用户 m16568: 分碟命名 / 原作 / 制作人 / 发布平台 / 出品侧别
    "discs", "original_work", "producer", "platform", "release_kind",
)


def _plain_value(value: Any) -> Any:
    """把任意嵌套值规整成可比较的形式(dict 按键排序成元组)."""
    if isinstance(value, Mapping):
        return tuple(sorted((str(k), _plain_value(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_plain_value(one) for one in value)
    return value


def _urls_map(value: Any) -> tuple[tuple[str, str], ...]:
    """外链统一成 ``((名字, 地址), ...)``; 兼容 dict 与 ``[{name,url}]`` 两种存量."""
    items: list[tuple[str, str]] = []
    if isinstance(value, Mapping):
        pairs = list(value.items())
    elif isinstance(value, (list, tuple)):
        pairs = []
        for one in value:
            if isinstance(one, Mapping):
                name = one.get("name") or one.get("key") or one.get("text") or ""
                url = one.get("url") or one.get("value") or one.get("href") or ""
                pairs.append((name, url))
    else:
        pairs = []
    for name, url in pairs:
        text = str(url or "").strip()
        if not text:
            continue
        items.append((str(name or "").strip(), text))
    return tuple(sorted(items))


def _history_value(field: str, value: Any) -> Any:
    """对比用的规整值: 外链按名字排序, 其余递归规整."""
    if field == "urls":
        return _urls_map(value)
    return _plain_value(value)


def _first(values: Mapping[str, Any], key: str, default: str = "") -> str:
    value = values.get(key)
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    return str(value if value is not None else default).strip()


def _looks_like_entity_id(text: str) -> bool:
    """``#12`` / ``albumId:12`` 这种**明确要求**按编号查的写法(用户 m18302).

    纯数字**不算** —— 用户 m18302 搜 "2000" 想找一张叫 2000 的碟, 结果被当成
    ``entities.id = 2000`` 直接打开另一张专辑, 连搜索结果页都看不到。
    """
    value = str(text or "").strip()
    if not value:
        return False
    if value.startswith("#"):
        return value[1:].strip().isdigit()
    head, _, tail = value.partition(":")
    return head.strip().lower() == "albumid" and tail.strip().isdigit()


# --------------------------------------------------------------------------
# 表单解析(纯函数)
# --------------------------------------------------------------------------


#: 默认头像的配色(深色底 + 高对比文字, 与站内主题都不冲突)
_AVATAR_PALETTE: tuple[tuple[str, str], ...] = (
    ("#3b6fd4", "#ffffff"),
    ("#8b5cf6", "#ffffff"),
    ("#0ea5a4", "#ffffff"),
    ("#d97706", "#ffffff"),
    ("#db2777", "#ffffff"),
    ("#16a34a", "#ffffff"),
    ("#dc2626", "#ffffff"),
    ("#0891b2", "#ffffff"),
    ("#7c3aed", "#ffffff"),
    ("#ca8a04", "#ffffff"),
)


def default_avatar_svg(seed: Any, letter: str = "") -> bytes:
    """生成默认头像 SVG: 稳定配色 + 首字母(没有首字母就用人形剪影).

    * ``seed`` 决定配色(同一用户永远同色, 不同用户尽量不同);
    * 不依赖任何外部文件/字体 —— 纯 inline SVG, 任何浏览器都能画;
    * 尺寸 128x128, 圆形裁剪由 CSS 的 border-radius 负责(图片本身留方角,
      这样在需要方角的场景也能用)。
    """
    try:
        index = abs(int(seed or 0)) % len(_AVATAR_PALETTE)
    except (TypeError, ValueError):
        index = sum(ord(ch) for ch in str(seed or "")) % len(_AVATAR_PALETTE)
    background, foreground = _AVATAR_PALETTE[index]
    text = (letter or "").strip()[:1].upper()
    if text:
        glyph = (
            f'<text x="64" y="64" font-size="58" font-weight="600"'
            f' font-family="system-ui,-apple-system,Segoe UI,Roboto,'
            f'Helvetica Neue,Noto Sans CJK SC,sans-serif"'
            f' fill="{foreground}" text-anchor="middle"'
            f' dominant-baseline="central">{_xml_escape(text)}</text>'
        )
    else:
        # 没有人形数据: 画一个简单的肩像剪影
        glyph = (
            f'<g fill="{foreground}" opacity="0.92">'
            f'<circle cx="64" cy="48" r="22"/>'
            f'<path d="M20 116c0-24 20-38 44-38s44 14 44 38z"/>'
            f"</g>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128"'
        ' viewBox="0 0 128 128" role="img" aria-label="默认头像">'
        f'<rect width="128" height="128" fill="{background}"/>'
        f"{glyph}"
        "</svg>"
    ).encode("utf-8")


def _xml_escape(text: str) -> str:
    """极简 XML 转义(默认头像里要嵌进 SVG 文本节点)."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )



def parse_form_field(headers: Mapping[str, Any], body: bytes, field: str) -> str:
    """从 ``multipart/form-data`` 里取一个**普通文本字段**的值(头像表单的 csrf 用)."""
    ctype = str(headers.get("content-type") or headers.get("Content-Type") or "")
    match = re.search(r'boundary="?([^";]+)"?', ctype, re.I)
    if "multipart/form-data" not in ctype.lower() or not match:
        return ""
    boundary = ("--" + match.group(1)).encode("utf-8")
    raw = body if isinstance(body, bytes) else str(body or "").encode("utf-8")
    target = b'name="' + field.encode() + b'"'
    for part in raw.split(boundary):
        if target not in part:
            continue
        head, _, payload = part.partition(b"\r\n\r\n")
        if b"filename=" in head:                 # 文件字段, 跳过
            continue
        return payload.rstrip(b"\r\n-").decode("utf-8", "replace").strip()
    return ""


def parse_upload(headers: Mapping[str, Any], body: bytes,
                 field: str = "avatar") -> tuple[bytes, str] | None:
    """从 ``multipart/form-data`` 里取第一个文件字段 -> ``(bytes, content_type)``.

    纯 stdlib 的最小实现: 只支持单文件 + 少量文本字段, 够头像用了。
    """
    ctype = str(headers.get("content-type") or headers.get("Content-Type") or "")
    match = re.search(r'boundary="?([^";]+)"?', ctype, re.I)
    if "multipart/form-data" not in ctype.lower() or not match:
        return None
    boundary = ("--" + match.group(1)).encode("utf-8")
    raw = body if isinstance(body, bytes) else str(body or "").encode("utf-8")
    for part in raw.split(boundary):
        if b'name="' + field.encode() + b'"' not in part:
            continue
        head, _, payload = part.partition(b"\r\n\r\n")
        file_type = re.search(rb"Content-Type:\s*([^\r\n]+)", head, re.I)
        if file_type is None:
            continue
        blob = payload.rstrip(b"\r\n-")
        if not blob:
            continue
        return blob, file_type.group(1).decode("ascii", "replace").strip()
    return None


#: 「按 URL 抓封面」允许的图片类型(与上传一致, **不含 svg** —— svg 能跑脚本).
_URL_COVER_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}

#: 「按 URL 抓封面」的单图上限(与编辑页上传一致)
MAX_URL_COVER_BYTES = MAX_IMAGE_BODY

#: 抓图时用的 UA: 图床/CDN 对空 UA 常回 403
_COVER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


class CoverFetchError(ValueError):
    """按 URL 抓封面失败(消息直接给人看)."""


def is_public_image_url(url: str) -> bool:
    """这个 URL 能不能让**服务端**去抓.

    服务端抓用户给的地址 = SSRF 风险, 所以这里挡死: 只放行 ``http/https``,
    且解析出来的**每一个** IP 都必须是公网地址(回环/私网/链路本地/保留/
    组播/未指定全拒)。域名解析失败也拒 —— 抓不到就没必要继续。
    """
    raw = str(url or "").strip()
    if not raw:
        return False
    try:
        parts = urllib.parse.urlsplit(raw)
    except ValueError:
        return False
    if parts.scheme.lower() not in ("http", "https"):
        return False
    host = str(parts.hostname or "").strip().lower()
    if not host:
        return False
    if host in ("localhost", "localhost.localdomain") or host.endswith(".localhost"):
        return False
    try:
        infos = socket.getaddrinfo(host, parts.port or None, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        addr = str(info[4][0] or "").split("%")[0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False
    return True


def fetch_image_from_url(url: str, *, timeout: float = DEFAULT_COVER_TIMEOUT,
                         max_bytes: int = MAX_URL_COVER_BYTES) -> tuple[bytes, str]:
    """抓一张远程图片 -> ``(bytes, mime)``; 任何问题都抛 :class:`CoverFetchError`.

    用 ``urllib.request`` 而不是采集端的 HttpClient: 那套带 robots 与站点
    节流(是给来源站批量采集用的), 这里是编辑者手动触发的一次性动作,
    不该为了抓一张图去读 robots.txt 再排队。
    """
    raw = str(url or "").strip()
    if not is_public_image_url(raw):
        raise CoverFetchError("这个地址不允许抓取(只支持公网 http/https 图片)")
    request = urllib.request.Request(raw, headers={
        "User-Agent": _COVER_UA,
        "Accept": "image/avif,image/webp,image/png,image/jpeg,image/*;q=0.8",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200) or 200)
            if status >= 400:
                raise CoverFetchError(f"对方返回 HTTP {status}")
            ctype = str(response.headers.get("Content-Type") or "")
            # 多读 1 字节: 用来判断"是不是超过上限了", 而不是把超大图全读进内存
            blob = response.read(max_bytes + 1)
    except CoverFetchError:
        raise
    except urllib.error.HTTPError as exc:
        raise CoverFetchError(f"对方返回 HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise CoverFetchError(f"抓取失败: {exc.reason}") from exc
    except (TimeoutError, OSError) as exc:
        raise CoverFetchError(f"抓取失败: {exc}") from exc
    if not blob:
        raise CoverFetchError("对方返回了空内容")
    if len(blob) > max_bytes:
        raise CoverFetchError(
            f"图片超过 {max_bytes // (1024 * 1024)}MB, 请先压小")
    sniffed = sniff_image_type(blob)
    if sniffed not in _URL_COVER_TYPES:
        raise CoverFetchError(
            "这个地址不是 PNG / JPEG / WebP 图片, 抓回来的是 "
            + (ctype.split(";")[0].strip() or sniffed))
    return blob, sniffed


def form_values(body: bytes) -> dict[str, list[str]]:
    """``application/x-www-form-urlencoded`` 请求体 -> ``{字段: [值...]}``.

    重复字段(``credit_role`` / ``credit_name`` 这类)按出现顺序保存在列表里,
    调用方靠下标对齐。编码坏了也不抛 —— 退化成替换字符, 后面走校验错误。
    """
    if not body:
        return {}
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        text = body.decode("utf-8", "replace")
    return dict(urllib.parse.parse_qs(text, keep_blank_values=True))


def text_field(values: Mapping[str, Sequence[str]], name: str) -> str:
    raw = values.get(name) or []
    return str(raw[0] if raw else "").strip()


def raw_field(values: Mapping[str, Sequence[str]], name: str) -> str:
    """取原样文本(不 strip, 保留换行) —— 简介那种多行输入用."""
    raw = values.get(name) or []
    return str(raw[0] if raw else "")


def many_fields(values: Mapping[str, Sequence[str]], name: str) -> list[str]:
    return [str(item).strip() for item in (values.get(name) or [])]


def split_list(value: str) -> list[str]:
    """把 ``a、b, c`` 这类输入切成去重列表."""
    out: list[str] = []
    for part in re.split(r"[,，、;；\n|/]+", str(value or "")):
        text = part.strip()
        if text and text not in out:
            out.append(text)
    return out


# --------------------------------------------------------------------------
# multipart/form-data(头像上传) —— 极简解析, 纯函数
# --------------------------------------------------------------------------

#: 头像上传的请求体上限(头像本来就小; 大于这个值直接拒, 不给内存攻击留口子)
MAX_AVATAR_BODY = MAX_IMAGE_BODY

#: 从 Content-Type 里抠 boundary
_BOUNDARY_RE = re.compile(r"boundary=([\"']?)([^\"';,\s]+)\1", re.I)
_DISPOSITION_NAME_RE = re.compile(r'name="([^"]*)"', re.I)
_DISPOSITION_FILE_RE = re.compile(r'filename="([^"]*)"', re.I)


@dataclass(slots=True)
class FilePart:
    """multipart 里的一个文件字段(只保留前若干个, 见 ``max_files``)."""

    field: str = ""
    filename: str = ""
    content_type: str = "application/octet-stream"
    data: bytes = b""

    @property
    def size(self) -> int:
        return len(self.data)


class MultipartError(ValueError):
    """multipart 请求体不合规(缺 boundary / 太大 / 结构坏)."""


def parse_multipart(
    body: bytes,
    content_type: str,
    *,
    max_bytes: int = MAX_AVATAR_BODY,
    max_files: int = 1,
) -> tuple[dict[str, str], list[FilePart]]:
    """``multipart/form-data`` -> ``(普通字段, 文件字段)``.

    只做浏览器表单真正会用到的子集:

    * 必须带 ``boundary``, 否则直接拒(不存在"猜 boundary"这种用法);
    * 整个 body 有字节上限, 超过就拒 —— 头像只有几百 KB, 2 MiB 足够;
    * 文件字段最多取 ``max_files`` 个(只取第一个 ``avatar`` 就够);
    * 文件名只当日志/展示用, **绝不参与拼路径**(落盘由 ProfileStore 管)。

    解析失败不抛到路由层: 这里统一抛 :class:`MultipartError`, 调用方回 400。
    """
    if not body:
        raise MultipartError("请求体为空")
    if len(body) > int(max_bytes):
        raise MultipartError("上传内容过大")
    matched = _BOUNDARY_RE.search(str(content_type or ""))
    if not matched:
        raise MultipartError("缺少 multipart boundary")
    boundary = matched.group(2).encode("utf-8", "replace")
    if not boundary:
        raise MultipartError("multipart boundary 为空")

    fields: dict[str, str] = {}
    files: list[FilePart] = []
    marker = b"--" + boundary
    for chunk in body.split(marker):
        if not chunk:
            continue
        # 结尾标记 "--\r\n" / "--"
        if chunk.startswith(b"--"):
            continue
        chunk = chunk.lstrip(b"\r\n")
        if not chunk:
            continue
        if b"\r\n\r\n" in chunk:
            raw_headers, _, raw_data = chunk.partition(b"\r\n\r\n")
        elif b"\n\n" in chunk:
            raw_headers, _, raw_data = chunk.partition(b"\n\n")
        else:
            continue
        raw_data = raw_data.rstrip(b"\r\n")
        headers = raw_headers.decode("latin-1", "replace")
        name_match = _DISPOSITION_NAME_RE.search(headers)
        name = name_match.group(1) if name_match else ""
        if not name:
            continue
        file_match = _DISPOSITION_FILE_RE.search(headers)
        filename = file_match.group(1) if file_match else ""
        if filename:
            if len(files) >= max(1, int(max_files)):
                continue
            ctype = "application/octet-stream"
            for line in headers.splitlines():
                if line.lower().startswith("content-type:"):
                    ctype = line.split(":", 1)[1].strip() or ctype
                    break
            files.append(FilePart(field=name, filename=filename, content_type=ctype, data=raw_data))
        else:
            fields[name] = raw_data.decode("utf-8", "replace")
    return fields, files


def parse_duration(value: str) -> tuple[int | None, bool]:
    """``3:42`` / ``1:02:07`` / ``222`` -> ``(秒, 是否合法)``; 空串 -> ``(0, True)``."""
    text = str(value or "").strip()
    if not text:
        return 0, True
    if text.isdigit():
        seconds = int(text)
        return (seconds, True) if seconds <= 24 * 3600 else (None, False)
    if not _TIME_RE.match(text):
        return None, False
    pieces = [int(p) for p in text.split(":")]
    if len(pieces) == 2:
        seconds = pieces[0] * 60 + pieces[1]
    else:
        seconds = pieces[0] * 3600 + pieces[1] * 60 + pieces[2]
    return (seconds, True) if seconds <= 24 * 3600 else (None, False)


def normalize_resale_event(event: Any,
                           tags: Any) -> tuple[str, list[str]]:
    """把 event 里的再贩标记移到标签里.

    ``"C108(resale)"`` + ``["J-pop"]`` -> ``("C108", ["J-pop", "再贩"])``

    再贩不是新会次, 而是"这张碟在本会次再贩"这个属性 —— 所以归入标签。
    """
    from .eventseries import RESALE_TAG, split_resale

    base, was = split_resale(event)
    out = [str(t).strip() for t in (tags or ()) if str(t).strip()]
    if was and RESALE_TAG not in out:
        out.append(RESALE_TAG)
    return base, out


def parse_album_form(
    values: Mapping[str, Sequence[str]],
    current: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """编辑表单 -> ``(patch, errors)``.

    ``patch`` 直接喂给 :meth:`dddb.db.Store.update_album_fields`(它只接受白名单
    字段); ``errors`` 是 ``{字段: 提示}``, 非空时调用方把表单连同红字一起回显,
    绝不 500。纯函数, 可单测。
    """
    errors: dict[str, str] = {}

    title_original = text_field(values, "title_original")
    title = text_field(values, "title") or title_original
    # **必填的是标题**: title 为空时回退用 title_original(兼容旧表单/旧数据)。
    # 之前这里误把 title_original 当必填 —— 用户不填原标题点保存就会静默回显,
    # 表现成"点保存没反应"。
    if not title:
        errors["title"] = "标题不能为空"
    release_date = text_field(values, "release_date")
    if release_date and not _DATE_RE.match(release_date):
        errors["release_date"] = "发售日格式应为 YYYY-MM-DD 或 YYYY-MM"

    index = split_list(text_field(values, "tags"))
    if len(index) > MAX_ROWS:
        errors["tags"] = f"标签最多 {MAX_ROWS} 个"
    genres = split_list(text_field(values, "genres"))
    if len(genres) > MAX_ROWS:
        errors["genres"] = f"曲风最多 {MAX_ROWS} 个"

    # 词条: credit_role[i] 配 credit_name[i]
    credits: list[dict[str, str]] = []
    roles = many_fields(values, "credit_role")
    names = many_fields(values, "credit_name")
    if max(len(roles), len(names)) > MAX_ROWS:
        errors["credits"] = f"词条最多 {MAX_ROWS} 行"
    # 「原名」「别名」两列已从表单里去掉(用户要求恢复原界面), 但歌词解析
    # 导入的老数据里可能还有值。表单**完全不带**这两列时, 按 (职位, 人名)
    # 从现有数据里把值捡回来, 免得一次普通保存就把它们清空。
    originals = many_fields(values, "credit_original")
    aliases = many_fields(values, "credit_alias")
    raw_roles = many_fields(values, "credit_raw_role")
    _keep_extra = "credit_original" not in values and "credit_alias" not in values
    _late: dict[tuple[str, str], tuple[str, str]] = {}
    if _keep_extra and current:
        for _old in (current.get("credits") or []):
            if not isinstance(_old, Mapping):
                continue
            _extra = (str(_old.get("original") or ""), str(_old.get("alias") or ""))
            if not _extra[0] and not _extra[1]:
                continue
            _late[(str(_old.get("role") or ""), str(_old.get("name") or ""))] = _extra

    def _opt(seq: list[str], pos: int) -> str:
        return str(seq[pos]).strip() if pos < len(seq) else ""

    for position in range(min(MAX_ROWS, max(len(roles), len(names)))):
        role = roles[position] if position < len(roles) else ""
        name = names[position] if position < len(names) else ""
        if not name:
            continue  # 空行忽略(表单里有几行留白是正常的)
        original = _opt(originals, position)
        alias = _opt(aliases, position)
        if _keep_extra and not original and not alias:
            original, alias = _late.get((role, name), ("", ""))
        raw_role = _opt(raw_roles, position)
        for one in split_list(name):
            item: dict[str, Any] = {"role": role or "artist", "name": one}
            if original:
                item["original"] = original
            if alias:
                item["alias"] = alias
            if raw_role and raw_role != role:
                item["raw_role"] = raw_role
            credits.append(item)

    # 外链: url_key[i] 配 url_value[i]
    urls: dict[str, str] = {}
    keys = many_fields(values, "url_key")
    raw_urls = many_fields(values, "url_value")
    if max(len(keys), len(raw_urls)) > MAX_ROWS:
        errors["urls"] = f"外链最多 {MAX_ROWS} 行"
    for position in range(min(MAX_ROWS, max(len(keys), len(raw_urls)))):
        key = (keys[position] if position < len(keys) else "").strip()
        url = (raw_urls[position] if position < len(raw_urls) else "").strip()
        if not url:
            continue
        # 外链值可以是 **http(s) URL**, 也可以是用户粘贴的 **embed code**
        #   (<iframe src="https://...">)。embed code 不是 URL, 以前被这里
        #   直接拒掉, 用户根本存不进播放器。判定交给 dddb.public.embed:
        #   能抽出 http(s) 的 iframe src 就收; 被禁站点/非 http(s) 仍拒。
        _is_embed = _extract_embed(url) is not None
        if not _is_embed and not T.safe_url(url, allow_relative=False):
            errors["urls"] = (
                "外链地址必须是 http/https（也可以粘贴 <iframe> 嵌入代码）: "
                f"{url[:60]}")
            continue
        name = key or f"link{len(urls) + 1}"
        urls[name] = url

    # 曲目: track_disc / track_index / track_title / track_artists / track_duration 五列对齐
    track_disc = many_fields(values, "track_disc")
    track_index = many_fields(values, "track_index")
    track_title = many_fields(values, "track_title")
    track_artists = many_fields(values, "track_artists")
    track_duration = many_fields(values, "track_duration")
    # 编辑界面的补充字段(默认空, 有值才存)
    track_subtitle = many_fields(values, "track_subtitle")
    track_title_zh = many_fields(values, "track_title_zh")
    track_lang = many_fields(values, "track_lang")
    track_genre = many_fields(values, "track_genre")
    width = max(len(track_disc), len(track_index), len(track_title),
                len(track_artists), len(track_duration), len(track_subtitle),
                len(track_title_zh), len(track_lang), len(track_genre))
    if width > MAX_ROWS:
        errors["tracks"] = f"曲目最多 {MAX_ROWS} 首"
    tracks: list[dict[str, Any]] = []
    for position in range(min(MAX_ROWS, width)):
        name = (track_title[position] if position < len(track_title) else "").strip()
        if not name:
            continue
        raw_disc = (track_disc[position] if position < len(track_disc) else "").strip()
        disc = 1
        if raw_disc:
            try:
                disc = max(1, int(raw_disc))
            except ValueError:
                errors["tracks"] = f"碟号必须是数字: {raw_disc[:20]}"
                disc = 1
        raw_index = (track_index[position] if position < len(track_index) else "").strip()
        number = 0
        if raw_index:
            try:
                number = int(raw_index)
            except ValueError:
                errors["tracks"] = f"曲目序号必须是数字: {raw_index[:20]}"
                number = 0
        seconds, ok = parse_duration(track_duration[position] if position < len(track_duration) else "")
        if not ok:
            errors["tracks"] = f"曲目时长格式应为 3:42 或秒数: {name[:40]}"
            seconds = 0
        def _pick(seq: list[str]) -> str:
            return (seq[position].strip() if position < len(seq) else "")

        tracks.append({
            "disc": disc,
            "index": number or len(tracks) + 1,
            "title": name,
            "artists": split_list(track_artists[position] if position < len(track_artists) else ""),
            "duration": seconds or None,
            # 补充字段: 空值也原样存(空字符串), 详情页按"有值才显示"处理
            "subtitle": _pick(track_subtitle),
            "title_zh": _pick(track_title_zh),
            "lang": _pick(track_lang),
            "genre": _pick(track_genre),
        })
    if not tracks:
        # 全部留空 = 明确要清空曲目表(表单里原有曲目是预填的, 不会误删)
        pass

    # 再贩标记归入标签: "C108(resale)" -> event="C108" + tags += 再贩
    _ev, _tags = normalize_resale_event(text_field(values, "event"), index)

    patch: dict[str, Any] = {
        "title_original": title_original or title,
        "title": title or title_original,
        "title_romaji": text_field(values, "title_romaji"),
        "circle": text_field(values, "circle"),
        "artists": split_list(text_field(values, "artists")),
        "release_date": release_date,
        "event": _ev,  # (resale) 已归入标签, 见下方 _ev/_tags 计算
        "catalog_no": text_field(values, "catalog_no"),
        # 用户 m16568: 发行公司输入框已去掉(同人展侧把社团与发行公司合并)。
        # 表单不再提交这个字段, 所以只有真带了才写 —— 否则每次保存都把库里的值抹成空。
        **({"company": text_field(values, "company")} if "company" in values else {}),
        "price": text_field(values, "price"),
        "hall": text_field(values, "hall"),
        "booth": text_field(values, "booth"),
        "original_work": text_field(values, "original_work"),
        # 用户 m19622: 制作人改成多值输入(隐藏域用换行分隔), 这里合并回
        # 一个字符串存库 —— Album.producer 仍然是单值字段。
        "producer": "、".join(split_list(text_field(values, "producer"))),
        "platform": text_field(values, "platform"),
        # 用户 m19622: 侧别单选以前只在 form_display_values 里回填,
        # 保存时根本没写进 patch —— 选了也不生效。
        "release_kind": text_field(values, "release_kind").strip().lower(),
        "tags": _tags,
        "genres": genres,
        "description": raw_field(values, "description").strip(),
    }

    # 结构化三件套(词条 / 外链 / 曲目表)的写入规则 —— 这里是"保存清空数据"的核心:
    #
    #   表单这一块全空  +  库里原来有内容   -> **保留库里原值**(不写 patch)
    #   其它情况(表单有内容 / 库里本来也空) -> 按表单写入
    #
    # 为什么必须这样: 编辑页如果没把已有行渲染出来(历史上真的发生过 ——
    # 只显示了 3 个空行), 用户点一次保存就会把 36 首曲目、54 条词条、外链
    # 一次性清空。"表单全空"无法区分"用户想清空"和"页面没渲染出来",
    # 所以一律保护: 空表单永远不覆盖非空原值。
    def _keep_original(field: str, incoming: Any, *aliases: str) -> bool:
        """库里这一项有内容、而表单提交的是空 -> 保留原值.

        ``field`` 是 patch 里的键名(``track_list``), 但 ``album.to_dict()``
        用的是 ``tracks`` —— 所以允许传别名, 否则保护会静默失效。
        """
        if not current:
            return False
        existing = None
        for name in (field, *aliases):
            value = current.get(name)
            if value:
                existing = value
                break
        if not existing:
            return False
        return not incoming

    # 结构化三件套: **只要表单提交了该字段名, 就按用户所见写入**。
    # 字段名不存在(= 老表单没渲染这一块) -> 保留原值;
    # 字段存在但全空(= 用户看到并删光了)   -> 写入空, 真的清空。
    # 早先用"值为空就保留原值", 导致"删掉的又回来了"。
    if "credit_role" in values or "credit_name" in values:
        patch["credits"] = credits
    if "url_key" in values or "url_value" in values:
        patch["urls"] = urls
    # 曲目表: 判据用**实际曲目行**(track_title 等), **不含 track_paste**。
    # track_paste 是"整段粘贴"输入框, 每个编辑页都有, 用它当判据会变成
    # "永远认为用户提交了曲目表", 于是空曲目行一保存就清空 52 首曲目。
    _has_track_rows = any(
        name in values for name in
        ("track_title", "track_index", "track_artists", "track_duration"))
    if _has_track_rows:
        # 提交了曲目行 -> 以用户所见为准(允许删光)。
        # 之前这里还有 _keep_original 兜底("值为空就保留原值"), 结果
        # "把曲目删干净再保存"不会生效。真正的保护在别处:
        # 只有**表单里根本没有曲目字段**时才不动 track_list(见上面的 _has_track_rows)。
        patch["track_list"] = tracks

    # 碟名(用户 m16568): 字段名形如 ``disc_name_1`` ``disc_name_2``。
    # 只有表单真的提交了这类字段才写, 否则保留库里的名字 —— 否则任何一次
    # 没渲染曲目卡片的老表单都会把碟名清空。
    _disc_fields = [name for name in values if str(name).startswith("disc_name_")]
    if _disc_fields:
        discs: dict[str, str] = {}
        for field_name in _disc_fields:
            suffix = str(field_name)[len("disc_name_"):]
            try:
                number = int(suffix)
            except (TypeError, ValueError):
                continue
            name = text_field(values, field_name)
            if number > 0 and name:
                discs[str(number)] = name
        patch["discs"] = discs

    return patch, errors


def form_display_values(values: Mapping[str, Sequence[str]]) -> dict[str, Any]:
    """表单原值 -> 回显结构(与 :func:`dddb.public.templates.edit_values_from_album` 同构).

    校验失败时把用户刚填的东西原样还回去, 而不是把内部 ``patch`` 结构硬塞给模板。
    """
    roles = many_fields(values, "credit_role")
    names = many_fields(values, "credit_name")
    keys = many_fields(values, "url_key")
    urls_in = many_fields(values, "url_value")
    tracks_in = {
        "index": many_fields(values, "track_index"),
        "title": many_fields(values, "track_title"),
        "artists": many_fields(values, "track_artists"),
        "duration": many_fields(values, "track_duration"),
    }

    def pairs(left: Sequence[str], right: Sequence[str], key_a: str, key_b: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for position in range(max(len(left), len(right))):
            out.append({
                key_a: left[position] if position < len(left) else "",
                key_b: right[position] if position < len(right) else "",
            })
        return out

    width = max((len(item) for item in tracks_in.values()), default=0)
    tracks: list[dict[str, Any]] = []
    for position in range(width):
        tracks.append({
            field_name: (values_in[position] if position < len(values_in) else "")
            for field_name, values_in in tracks_in.items()
        })
    return {
        "title": text_field(values, "title"),
        "title_original": text_field(values, "title_original"),
        "title_romaji": text_field(values, "title_romaji"),
        "circle": text_field(values, "circle"),
        "artists": text_field(values, "artists"),
        "release_date": text_field(values, "release_date"),
        "event": text_field(values, "event"),
        "catalog_no": text_field(values, "catalog_no"),
        "price": text_field(values, "price"),
        "booth": text_field(values, "booth"),
        "hall": text_field(values, "hall"),
        "original_work": text_field(values, "original_work"),
        # 用户 m19622: 制作人是多值绑定字段, 隐藏域用换行分隔; 回填时统成
        # 「、」分隔 —— 模板里的 bind 组件按 /[\n、]+/ 拆, 两边都认。
        "producer": "、".join(split_list(text_field(values, "producer"))),
        "platform": text_field(values, "platform"),
        "release_kind": text_field(values, "release_kind"),
        "discs": {
            str(name)[len("disc_name_"):]: text_field(values, name)
            for name in values
            if str(name).startswith("disc_name_")
            and text_field(values, name)
        },
        "tags": text_field(values, "tags"),
        "genres": text_field(values, "genres"),
        "description": raw_field(values, "description"),
        "credits": pairs(roles, names, "role", "name"),
        "urls": pairs(keys, urls_in, "key", "url"),
        "tracks": tracks,
        # 粘贴框内容在重填时原样还回(粘一次就该够), 模式单选也记住
        "track_paste": raw_field(values, "track_paste"),
        "track_mode": normalize_track_mode(text_field(values, "track_mode")),
        # 用户 m17600: 曲目表"已暂存"标记, 重填时要还回去, 否则用户校验失败
        # 回来再点一次「保存」, 又会拿粘贴框原文覆盖他改过的表格。
        "track_staged": text_field(values, "track_staged"),
        # 制作人员粘贴框同理(用户 m14663)
        "credit_paste": raw_field(values, "credit_paste"),
        "credit_mode": creditparse.normalize_credit_mode(
            text_field(values, "credit_mode")),
    }


# --------------------------------------------------------------------------
# 「整段粘贴 -> 智能识别 -> 批量填入」(纯函数)
# --------------------------------------------------------------------------

#: 粘贴模式的合法取值(模板里的两个单选框)。缺失 / 非法一律当 ``replace``。
TRACK_MODES = ("replace", "append")


def parsed_track_items(parsed: Sequence[Any]) -> list[dict[str, Any]]:
    """解析结果 -> 曲目结构(与 :func:`parse_album_form` 的 ``track_list`` 同构).

    时长统一成**秒**(``None`` = 识别不出); 序号按解析顺序 1 起重排;
    空标题项直接丢掉(解析器理论上不会给, 这里兜底)。
    碟号取自 ``ParsedTrack.disc``(用户 m16568: 粘贴文本支持分碟),
    同一碟内的序号从 1 重新数。
    """
    items: list[dict[str, Any]] = []
    seen: dict[int, int] = {}
    for track in parsed:
        title = str(getattr(track, "title", "") or "").strip()
        if not title:
            continue
        duration = getattr(track, "duration", None)
        if isinstance(duration, bool) or not isinstance(duration, int) or duration <= 0:
            duration = None
        artists = [
            str(name).strip() for name in (getattr(track, "artists", None) or [])
            if str(name).strip()
        ]
        try:
            disc = max(1, int(getattr(track, "disc", 1) or 1))
        except (TypeError, ValueError):
            disc = 1
        seen[disc] = seen.get(disc, 0) + 1
        items.append({
            "disc": disc,
            "index": seen[disc],
            "title": title,
            "artists": artists,
            "duration": duration,
        })
    return items


def merge_pasted_tracks(
    tracks: Sequence[Mapping[str, Any]],
    parsed: Sequence[Any],
    mode: str = "replace",
) -> list[dict[str, Any]]:
    """把「整段粘贴」的解析结果并进**这次提交**的曲目列表.

    * ``replace``: 用解析结果替换现有列表, 序号重排 ``1..n``;
    * ``append``: 接在现有列表末尾, 序号接着往下排;
    * ``parsed`` 为空 -> **原样返回**现有列表(调用方据此提示"没识别出曲目",
      绝不因为一次粘错就把库里已有的曲目表清空)。

    ``mode`` 非法时按 ``replace`` 处理(单选框被伪造也不会有意外结果)。
    """
    current = [dict(item) for item in (tracks or [])]
    items = parsed_track_items(parsed)
    if not items:
        return current
    if mode != "append":
        merged = items
    else:
        #: 追加要分两种情况(用户 m16568 要求粘贴文本支持分碟):
        #:  * 粘贴里**只有一碟**(没写 Disc 行) -> 直接接在现有最后一碟屁股后面,
        #:    不能凭空多出一张碟;
        #:  * 粘贴里**有多碟** -> 整体挪到现有最大碟号之后, 免得「粘第二碟」
        #:    把它的曲目塞进第一碟。
        last_disc = max(
            [int(item.get("disc") or 1) for item in current] or [0]
        )
        paste_discs = {int(item.get("disc") or 1) for item in items}
        if len(paste_discs) <= 1:
            offset = 0
            target = max(1, last_disc or 1)
            tail = [{**item, "disc": target} for item in items]
        else:
            offset = last_disc if min(paste_discs) <= last_disc else 0
            tail = [
                {**item, "disc": item["disc"] + offset} for item in items
            ]
        merged = current + tail
    #: 序号按「碟内位置」重排(用户 m16568: 分碟后每碟都应从 1 开始)
    counters: dict[int, int] = {}
    out: list[dict[str, Any]] = []
    for item in merged:
        disc = item.get("disc") or 1
        counters[disc] = counters.get(disc, 0) + 1
        out.append({**item, "index": counters[disc]})
    return out


def normalize_track_mode(value: Any) -> str:
    """单选框值 -> 合法模式(空 / 伪造值 -> ``replace``)."""
    text = str(value or "").strip().lower()
    return text if text in TRACK_MODES else "replace"


def tracks_already_staged(values: Mapping[str, Sequence[str]]) -> bool:
    """粘贴框里的曲目是否已经"填入"过下面的表格(用户 m17600).

    前端点「填入曲目表（暂存）」时会把这个隐藏域置成 ``1``。置过之后,
    表格里的内容就是用户**看过并且改过**的权威版本 —— 服务端不能再拿
    识别框原文重新解析一遍盖回去(否则用户改的标题、删掉的行全白做)。
    """
    return text_field(values, "track_staged") == "1"


def merge_credits_from_paste(
    patch: dict[str, Any],
    values: Mapping[str, Sequence[str]],
) -> tuple[bool, bool]:
    """把「制作人员整段粘贴」并进这次的 ``patch["credits"]``(用户 m14663).

    返回 ``(has_paste, missed)``:

    * ``has_paste``: 用户确实往粘贴框里填了东西;
    * ``missed``: 填了但一条都没识别出来 —— 调用方据此提示一句, 但**不能**
      因此否决整次提交(其它字段照常落库, 词条保持表单里原有的那份)。

    解析器见 :mod:`dddb.creditparse`; 括号里的链接会被丢掉, 所以
    ``音乐: 甲 (https://x.com/a)`` 落到库里是 ``甲`` 而不是一串网址。
    """
    paste_text = str(raw_field(values, "credit_paste") or "")
    if not paste_text.strip():
        return False, False
    parsed = creditparse.parse_credits(paste_text)
    if not parsed:
        return True, True
    mode = creditparse.normalize_credit_mode(text_field(values, "credit_mode"))
    base = [dict(item) for item in (patch.get("credits") or [])
            if isinstance(item, Mapping)]
    patch["credits"] = creditparse.merge_pasted_credits(base, parsed, mode)
    return True, False


# --------------------------------------------------------------------------
# HTTP 响应
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Response:
    status: int = 200
    content_type: str = "text/html; charset=utf-8"
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    #: 多个 ``Set-Cookie`` 需要单独一条一条发(字典装不下同名字段)
    cookies: list[str] = field(default_factory=list)

    @classmethod
    def html(cls, body: str, *, status: int = 200) -> "Response":
        return cls(status=status, body=body.encode("utf-8"))

    @classmethod
    def redirect(cls, location: str, *, status: int = 302) -> "Response":
        return cls(status=status, content_type="text/plain; charset=utf-8",
                   body=b"", headers={"Location": location})

    @classmethod
    def binary(cls, data: bytes, content_type: str, *, status: int = 200) -> "Response":
        return cls(status=status, content_type=content_type, body=data)


def sniff_image_type(data: bytes) -> str:
    """按文件头判定图片类型.

    封面缓存用的是 ``.img`` 扩展名, ``mimetypes`` 猜不出来; 而浏览器只认
    Content-Type, 猜错就会把图当下载。
    """
    head = data[:16]
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"BM"):
        return "image/bmp"
    if head.startswith(b"\x00\x00\x01\x00"):
        return "image/x-icon"
    stripped = data[:256].lstrip()
    if stripped.startswith(b"<svg") or stripped.startswith(b"<?xml"):
        return "image/svg+xml"
    return "application/octet-stream"


class CoverFetcher:
    """远程封面 -> 本地缓存 ``<covers_dir>/<key>.img`` 的服务端拉取器.

    设计要点(对应"封面不得 hotlink 源站"的合规要求):

    * 一律走 :class:`dddb.httpclient.HttpClient` —— robots.txt、per-host 节流、
      代理/UA 策略都用采集端那一套, 拉封面也守规矩;
    * **每次拉取都有超时上限, 且不重试** —— 这是在一个页面请求线程里跑,
      宁可回占位图, 也不能把响应拖成几十秒;
    * 同一张封面并发只拉一次(按 key 加锁); 落盘先写 ``.part`` 再 ``os.replace``,
      半个文件不会被当成封面;
    * 失败进 :data:`COVER_FAILURE_TTL` 的负缓存, 免得一张坏图让每次页面浏览
      都去重试打对方站。
    """

    #: 并发锁表的容量上限(超过就整体清空, 最坏情况只是重复拉一次)
    MAX_LOCKS = 512

    def __init__(
        self,
        covers_dir: Path | str | None,
        *,
        http: Any = None,
        client_factory: Callable[[], Any] | None = None,
        timeout: float = DEFAULT_COVER_TIMEOUT,
        max_bytes: int = MAX_COVER_BYTES,
        failure_ttl: float = COVER_FAILURE_TTL,
    ) -> None:
        self.covers_dir = Path(covers_dir).resolve() if covers_dir else None
        self.timeout = max(0.5, float(timeout or DEFAULT_COVER_TIMEOUT))
        self.max_bytes = max(1024, int(max_bytes or MAX_COVER_BYTES))
        self.failure_ttl = max(0.0, float(failure_ttl or 0.0))
        self._http = http
        self._client_factory = client_factory
        self._client_failed = False
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._failures: dict[str, float] = {}

    # -- 依赖 ---------------------------------------------------------------

    def client(self) -> Any:
        """惰性拿到 HttpClient; 没有可用客户端时返回 ``None``(只能回占位图)."""
        if self._http is None and self._client_factory is not None and not self._client_failed:
            try:
                self._http = self._client_factory()
            except Exception as exc:  # 配置坏了也不该拦住浏览
                self._client_failed = True
                logger.warning("封面拉取客户端不可用, 之后一律回占位图: %s", exc)
        return self._http

    def path_for(self, key: Any) -> Path | None:
        """``key -> <covers_dir>/<key>.img``; key 不安全或没配目录时给 ``None``."""
        name = cover_cache_name(key)
        if not name or self.covers_dir is None:
            return None
        return self.covers_dir / name

    # -- 负缓存 -------------------------------------------------------------

    def recently_failed(self, name: str) -> bool:
        if not name or self.failure_ttl <= 0:
            return False
        with self._locks_guard:
            stamp = self._failures.get(name)
        if stamp is None:
            return False
        if (time.monotonic() - stamp) > self.failure_ttl:
            with self._locks_guard:
                self._failures.pop(name, None)
            return False
        return True

    def _mark_failed(self, name: str) -> None:
        with self._locks_guard:
            self._failures[name] = time.monotonic()

    def _key_lock(self, name: str) -> threading.Lock:
        with self._locks_guard:
            if len(self._locks) >= self.MAX_LOCKS:
                self._locks.clear()
            lock = self._locks.get(name)
            if lock is None:
                lock = threading.Lock()
                self._locks[name] = lock
            return lock

    # -- 取图 ---------------------------------------------------------------

    def fetch(self, key: Any, url: str) -> tuple[bytes, str, str] | None:
        """拉取并落盘; 成功回 ``(字节, content_type, 来源)``, 失败回 ``None``.

        来源是 ``"cached"``(命中已落盘的缓存)或 ``"downloaded"``(本次刚拉下来)。
        """
        target = self.path_for(key)
        name = cover_cache_name(key)
        if target is None or not name:
            return None
        hit = self._read_file(target)
        if hit is not None:
            return hit[0], hit[1], "cached"
        if self.recently_failed(name):
            return None
        with self._key_lock(name):
            # 等锁期间可能已经被别的线程拉好了
            hit = self._read_file(target)
            if hit is not None:
                return hit[0], hit[1], "cached"
            if self.recently_failed(name):
                return None
            got = self._download(url)
            if got is None:
                self._mark_failed(name)
                return None
            data, ctype = got
            if not self._write(target, data):
                # 磁盘写不进去也照样把这次拉到的东西发给浏览器(只是下次还要再拉)
                return data, ctype, "downloaded"
            with self._locks_guard:
                self._failures.pop(name, None)
            return data, ctype, "downloaded"

    def _download(self, url: str) -> tuple[bytes, str] | None:
        client = self.client()
        if client is None:
            logger.debug("没有可用的 HTTP 客户端, 封面走占位图: %s", url)
            return None
        try:
            response = client.get(
                url,
                timeout=self.timeout,
                retries=0,          # 请求线程里只试一次
                use_cache=False,    # 我们有自己的落盘缓存, 不必再存一份
                raise_for_status=False,
            )
        except Exception as exc:  # robots 禁止 / 超时 / 网络故障 -> 占位图
            logger.warning("拉取封面失败 %s: %s", url, exc)
            return None
        if not getattr(response, "ok", False):
            logger.warning("拉取封面失败 %s: HTTP %s", url, getattr(response, "status", "?"))
            return None
        data = bytes(getattr(response, "body", b"") or b"")
        if not data:
            logger.warning("拉取封面失败 %s: 响应为空", url)
            return None
        if len(data) > self.max_bytes:
            logger.warning("封面超过上限(%d 字节), 丢弃 %s", self.max_bytes, url)
            return None
        ctype = sniff_image_type(data)
        if ctype not in _COVER_IMAGE_TYPES:
            logger.warning("封面不是可用的位图, 丢弃 %s: %s", url, ctype)
            return None
        return data, ctype

    def _read_file(self, path: Path) -> tuple[bytes, str] | None:
        try:
            if not path.is_file():
                return None
            data = path.read_bytes()
        except OSError as exc:
            logger.debug("读封面缓存失败 %s: %s", path, exc)
            return None
        if not data:
            return None
        ctype = sniff_image_type(data)
        if ctype not in _COVER_IMAGE_TYPES:
            logger.warning("封面缓存不是可用位图, 当作未命中: %s (%s)", path, ctype)
            return None
        return data, ctype

    def _write(self, path: Path, data: bytes) -> bool:
        tmp = path.with_name(path.name + ".part")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(data)
            os.replace(tmp, path)       # 原子替换: 不会读到半个文件
            return True
        except OSError as exc:
            logger.warning("写封面缓存失败 %s: %s", path, exc)
            try:
                tmp.unlink()
            except OSError:
                pass
            return False


# --------------------------------------------------------------------------
# 站点
# --------------------------------------------------------------------------


def project_root() -> Path:
    """猜测项目根目录: 优先当前工作目录, 其次本文件所在的仓库根."""
    candidates = [Path.cwd(), Path(__file__).resolve().parents[2]]
    for cand in candidates:
        if (cand / "config" / "config.toml").exists() or (cand / "data").is_dir():
            return cand
    return Path.cwd()


def _public_toml_value(config: Any, name: str) -> str:
    """从 ``config.config_file`` 的 ``[public]`` 段里读一个字符串(读不到给空串).

    为什么要自己读一遍: :class:`dddb.config.PublicConfig` 还没有 ``cover_base_url``
    这个字段(改 ``config.py`` 不在本次授权范围), 所以顺手读原文 —— 部署方在
    ``config.toml`` 里写上 ``[public] cover_base_url = "https://cdn.example.com/covers"``
    就能生效, 不用改代码。
    """
    path = getattr(config, "config_file", None)
    if not path:
        return ""
    try:
        import tomllib

        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except (OSError, ValueError, TypeError) as exc:  # 配置坏了不该拦住站点启动
        logger.debug("读 %s 里的 [public] %s 失败: %s", path, name, exc)
        return ""
    section = data.get("public")
    if not isinstance(section, Mapping):
        return ""
    value = section.get(name)
    return str(value).strip() if isinstance(value, str) else ""


def resolve_cover_base_url(
    public_config: Any = None,
    *,
    explicit: str = "",
    config: Any = None,
    env: Mapping[str, str] | None = None,
) -> str:
    """外接封面基址(对象存储/CDN)的来源顺序.

    1. 显式参数(:func:`build_site` / 直接构造站点时传);
    2. 环境变量 ``DNH_PUBLIC_COVER_BASE_URL``;
    3. ``public_config.cover_base_url``(等 ``config.py`` 补上字段后自动生效);
    4. ``config.toml`` 的 ``[public] cover_base_url`` 原文。

    都为空 = 没配外接存储, 封面走本地缓存(拉取 + 落盘)。返回值已去掉尾部 ``/``。
    """
    environment = env if env is not None else os.environ
    candidates = (
        explicit,
        environment.get(COVER_BASE_ENV, ""),
        getattr(public_config, "cover_base_url", ""),
        getattr(public_config, "cover_base", ""),
        _public_toml_value(config, "cover_base_url"),
    )
    for value in candidates:
        text = str(value or "").strip()
        if text:
            return text.rstrip("/")
    return ""


class PublicSite:
    """把数据、模板、路由、账号粘在一起的站点对象(不依赖 socket, 可直接单测)."""

    def __init__(
        self,
        data: PublicData,
        *,
        users: UserStore | None = None,
        sessions: auth.SessionManager | None = None,
        db_path: Path | str | None = None,
        site_name: str = T.SITE_NAME,
        tagline: str = T.SITE_TAGLINE,
        footer_note: str = "",
        cover_cache_size: int = 64,
        limiter: RateLimiter | None = None,
        public_config: Any = None,
        base_url: str = "",
        cover_base_url: str = "",
        cover_timeout: float = DEFAULT_COVER_TIMEOUT,
        cover_max_bytes: int = MAX_COVER_BYTES,
        http_client: Any = None,
        http_client_factory: Callable[[], Any] | None = None,
        #: 注册验证: 发信器(默认按环境变量建 SMTP; 单测可注入 RecordingMailer)
        mailer: Any = None,
        #: 注册验证: 读 SMTP 配置的环境变量表(None = 读 ``os.environ``)
        environ: Mapping[str, str] | None = None,
        #: 个人中心: 注入一个 ProfileStore(单测用假实现; 生产走惰性打开)
        profiles: Any = None,
        #: 个人中心库路径(默认 ``<root>/data/public_profiles.sqlite3``)
        profiles_path: Path | str | None = None,
        #: 站务看板的访问日志库路径(默认与 users 库同目录 ``site_visits.sqlite3``)
        visits_path: Path | str | None = None,
    ) -> None:
        self.data = data
        #: 账号库(独立文件)。不传就开一个内存库, 于是单测零配置也有完整用户系统。
        self.users = users if users is not None else UserStore(":memory:")
        self.sessions = sessions if sessions is not None else auth.SessionManager()
        #: 可写主库路径; ``None`` 表示这个实例不允许写(编辑者的写操作会得到 503)
        self.db_path = Path(db_path) if db_path else None
        self.site_name = site_name
        self.tagline = tagline
        self.footer_note = footer_note
        self.cover_cache_size = max(4, int(cover_cache_size))
        #: 按 IP 令牌桶限速(``config.public`` 驱动)
        self.public_config = public_config
        if limiter is not None:
            self.limiter = limiter
        elif public_config is not None:
            self.limiter = RateLimiter.from_config(public_config)
        else:
            # 直接构造站点(单测 / 嵌入式)时**不给隐式限速**: 限速是一项部署策略,
            # 只应该由 ``[public]`` 配置或显式传入的 limiter 决定。
            # 真正上线走的是 build_site(), 它一定会按配置建一个启用的限速器。
            self.limiter = RateLimiter(enabled=False)
        self.base_url = str(base_url or getattr(public_config, "base_url", "") or "").rstrip("/")
        self.sitemap_max_urls = max(4, int(getattr(public_config, "sitemap_max_urls", 2000) or 2000))
        self.sitemap_cache_ttl = float(getattr(public_config, "sitemap_cache_ttl", 300.0) or 300.0)
        self.trust_proxy = bool(getattr(public_config, "behind_proxy", False))
        #: 外接封面基址(对象存储/CDN)。配了就 302 过去, 没配就走本地缓存。
        self.cover_base_url = resolve_cover_base_url(public_config, explicit=cover_base_url)
        #: 远程封面 -> 本地缓存 ``data/covers/<key>.img`` 的拉取器
        #: (走 HttpClient: robots + 节流 + 超时; 客户端惰性创建)
        #: **云端应关掉**(DNH_PUBLIC_COVER_FETCH=0): 只发本地/对象存储的图。
        self.cover_fetcher = (
            CoverFetcher(
                getattr(data, "covers_dir", None),
                http=http_client,
                client_factory=http_client_factory,
                timeout=cover_timeout,
                max_bytes=cover_max_bytes,
            )
            if _env_flag(COVER_FETCH_ENV, default=True) else None
        )
        self._cover_cache: dict[str, tuple[bytes, str, float]] = {}
        self._cover_lock = threading.Lock()
        self._write_store: Any = None
        self._write_lock = threading.Lock()
        #: 注册/邮箱验证用的发信器与配置来源(见 :mod:`dddb.public.verify`)
        self.mailer = mailer
        self.environ = dict(environ) if environ is not None else None
        #: 个人中心库(评论之外的收藏/关注/私信/通知/反馈/贡献/资料)。
        #: ``profile.py`` 由另一位队友交付; 未交付时这里保持 ``None``, 页面优雅降级。
        self._profiles = profiles
        self._profiles_path = Path(profiles_path) if profiles_path else None
        self._profiles_failed = False
        self._profiles_lock = threading.Lock()
        self._avatar_cache: dict[int, tuple[bytes, str, float]] = {}
        #: 站务看板的访问日志(用户 m14663)。**惰性打开**: 只有真的记一条
        #: 访问或有人打开看板时才建库, 单测/静态渲染完全不受影响。
        self._visits_path = Path(visits_path) if visits_path else None
        self._visits: Any = None
        self._visits_lock = threading.Lock()
        #: 每个请求线程的界面语言(由 Cookie 决定)
        self._thread_lang = threading.local()

    # -- 可写连接 -----------------------------------------------------------

    def writable_store(self) -> Any:
        """编辑者的写路径: 按需开一个可写 :class:`dddb.db.Store`.

        访客/普通用户的读路径永远不碰它 —— 读走 ``mode=ro`` 的 ReadOnlyStore。
        """
        if self.db_path is None:
            raise PublicDataError("未配置可写数据库路径, 无法保存修改")
        with self._write_lock:
            if self._write_store is None:
                from ..db import Store  # 延迟导入: 只读浏览时不必加载写栈

                self._write_store = Store(self.db_path)
            return self._write_store

    # -- 权限 ---------------------------------------------------------------
    #
    # 角色体系正在从 ``viewer | editor`` 扩成 ``viewer | editor | superuser``,
    # 所以**不要**把判断写死成 ``state.is_editor``: 只要"有编辑权限"就算 ——
    # 会话自称编辑者, 或者角色是 editor/superuser, 都放行。

    def can_edit(self, state: auth.AuthState | None) -> bool:
        """当前请求是否具备编辑权限(编辑者 / 超级用户).

        判据统一在 :func:`dddb.public.auth.can_edit`(内部走 ``users.can_write``,
        editor 与 superuser 都算), 这里只包一层 ``None`` 处理 —— 站点各处不要
        再各写一套"角色是不是 editor"的字符串判断。
        """
        if state is None:
            return False
        return bool(auth.can_edit(state))

    # -- 个人中心(ProfileStore) ----------------------------------------------
    #
    # ``dddb/public/profile.py`` 由另一位队友交付。**惰性导入 + 缺失降级**:
    # 文件还没到位时个人中心页面照常渲染(空列表 + 一句"暂不可用"),
    # 绝不因为后端没交付就让整站 500。

    def profile_store(self) -> Any:
        """惰性打开 ProfileStore; 后端未交付 / 打不开时返回 ``None``."""
        if self._profiles is not None:
            return self._profiles
        if self._profiles_failed:
            return None
        with self._profiles_lock:
            if self._profiles is not None:
                return self._profiles
            try:
                from . import profile as profile_mod
            except Exception as exc:  # pragma: no cover - 后端未交付时的降级分支
                self._profiles_failed = True
                logger.warning("个人中心后端不可用(profile.py 未就绪): %s", exc)
                return None
            path = self.profile_db_path()
            try:
                self._profiles = profile_mod.ProfileStore(path)
            except Exception as exc:  # pragma: no cover - 磁盘级故障
                self._profiles_failed = True
                logger.warning("打开个人中心库失败 %s: %s", path, exc)
                return None
            return self._profiles

    def profile_db_path(self) -> Path:
        """个人中心库的落盘位置.

        **不能放 app 目录**: 云端的 systemd 单元把 ``/srv/dddb/app`` 设成只读
        (ProtectSystem=strict + ReadWritePaths=/srv/dddb/data), 在那里建库会
        `Read-only file system`。所以默认与 users 库同目录(线上即 /srv/dddb/data)。
        """
        if self._profiles_path:
            return Path(self._profiles_path)
        users_path = getattr(self.users, "path", None)
        if users_path and str(users_path) not in (":memory:", "", "None"):
            return Path(str(users_path)).parent / "public_profiles.sqlite3"
        return (self.data.root or Path.cwd()) / "data" / "public_profiles.sqlite3"

    def profile_enabled(self) -> bool:
        """个人中心功能是否可用(后端已交付且库能打开)."""
        return self.profile_store() is not None

    # -- 站务看板 -----------------------------------------------------------

    def visits_db_path(self) -> Path:
        """访问日志库的落盘位置(与 users 同目录, 见 profile_db_path 的说明)."""
        if self._visits_path:
            return Path(self._visits_path)
        users_path = getattr(self.users, "path", None)
        if users_path and str(users_path) not in (":memory:", "", "None"):
            return Path(str(users_path)).parent / "site_visits.sqlite3"
        return (self.data.root or Path.cwd()) / "data" / "site_visits.sqlite3"

    def visit_store(self) -> Any:
        """访问日志库(惰性打开, 失败返回 ``None``, 页面降级)."""
        store = self._visits
        if store is not None:
            return store
        with self._visits_lock:
            if self._visits is not None:
                return self._visits
            try:
                from .visits import VisitStore

                store = VisitStore(self.visits_db_path())
            except Exception as exc:            # pragma: no cover - 极端环境
                logger.warning("打开访问日志库失败: %s", exc)
                return None
            self._visits = store
            return store

    def _record_visit(
        self,
        path: str,
        route: Mapping[str, Any] | None,
        state: Any,
        headers: Mapping[str, Any] | None,
        client_ip: str,
        method: str = "GET",
        status: int = 0,
    ) -> None:
        """记一行访问日志. **尽力而为**, 任何异常都不许冒到请求里.

        用户 m19090: 以前这里的 ``method`` 写死成 ``"GET"``、``status`` 根本没传,
        于是「新建社团保存失败」这种事在看板上**一点痕迹都没有** —— 全是 GET,
        分不清哪次是提交。现在 method/status 由调用方传真实值。
        """
        record = getattr(self.visit_store(), "record", None)
        if not callable(record):
            return
        try:
            user = getattr(state, "user", None) or {}
            record(
                path=str(path or "/")[:300],
                route=str((route or {}).get("name") or ""),
                method=str(method or "GET").upper(),
                status=int(status or 0),
                user_id=int(user.get("id") or 0),
                username=str(user.get("username") or ""),
                client_ip=str(client_ip or ""),
                ua=str((headers or {}).get("user-agent") or ""),
            )
        except Exception:                       # pragma: no cover
            logger.debug("记录访问失败 path=%s", path, exc_info=True)

    def dashboard_data(self) -> dict[str, Any]:
        """站务看板要的全部数据(访问统计 + 最近记录 + 服务器负载)."""
        from .visits import RECENT_LIMIT, VISIT_KEEP_DAYS, server_stats

        store = self.visit_store()
        disk_path = "/"
        if store is not None:
            try:
                store.purge(keep_days=VISIT_KEEP_DAYS)
            except Exception:                   # pragma: no cover
                logger.debug("清理访问日志失败", exc_info=True)
        recent = store.recent(RECENT_LIMIT) if store is not None else []
        totals = store.totals() if store is not None else {}
        routes = store.top_routes() if store is not None else []
        paths = store.top_paths(limit=10) if store is not None else []
        return {
            "enabled": bool(store is not None and getattr(store, "enabled", False)),
            "totals": totals,
            "routes": routes,
            "paths": paths,
            "recent": recent,
            "server": server_stats(disk_path=disk_path),
            "revisions": self.recent_revisions(40),
        }

    def recent_revisions(self, limit: int = 40) -> list[dict[str, Any]]:
        """最近的全站改动(站务看板用).

        ``Store.list_revisions`` 只按单张专辑查, 看板要的是全局时间线, 所以这里
        直接发一条 ``ORDER BY created_at DESC`` 的 SELECT。
        """
        query = getattr(self.data, "_query", None)
        if not callable(query):
            return []
        try:
            rows = query(
                "SELECT album_key, user_id, username, summary, created_at"
                " FROM album_revisions ORDER BY created_at DESC, id DESC LIMIT ?",
                (max(1, int(limit)),),
            )
        except Exception:                       # pragma: no cover
            logger.debug("读最近改动失败", exc_info=True)
            return []
        out: list[dict[str, Any]] = []
        for row in rows or ():
            try:
                out.append(dict(row))
            except Exception:                   # pragma: no cover
                continue
        return out

    def profile_call(self, name: str, *args: Any, default: Any = None, **kwargs: Any) -> Any:
        """调用 ProfileStore 上一个**可能还不存在**的方法(契约分阶段交付)."""
        store = self.profile_store()
        if store is None:
            return default
        fn = getattr(store, name, None)
        if not callable(fn):
            logger.debug("ProfileStore 还没有 %s(), 降级为空", name)
            return default
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # 后端内部错误不该把页面拖成 500
            logger.warning("个人中心调用 %s 失败: %s", name, exc)
            return default

    def profile_data(self, user_id: Any, username: str = "") -> dict[str, Any]:
        """``store.profile(...)`` 的安全封装(失败时回空字典)."""
        try:
            uid = int(user_id or 0)
        except (TypeError, ValueError):
            return {}
        data = self.profile_call("profile", uid, username or "", default=None)
        return dict(data) if isinstance(data, Mapping) else {}

    def avatar_bytes(self, user_id: Any) -> tuple[bytes, str] | None:
        """``user_id -> (图片字节, content_type)``; 没有头像回 ``None``."""
        try:
            uid = int(user_id or 0)
        except (TypeError, ValueError):
            return None
        if uid <= 0:
            return None
        stamp = time.time()
        hit = self._avatar_cache.get(uid)
        if hit is not None and stamp - hit[2] < 30.0:
            return hit[0], hit[1]
        path = self.profile_call("avatar_path", uid, default=None)
        if not path:
            return None
        target = Path(str(path))
        try:
            data = target.read_bytes()
        except OSError:
            return None
        ctype = sniff_image_type(data)
        if ctype not in _AVATAR_TYPES:
            return None
        self._avatar_cache[uid] = (data, ctype, stamp)
        return data, ctype

    def can_review(self, state: auth.AuthState | None) -> bool:
        """能不能进审核队列 / 用户管理(仅超级用户)."""
        if state is None:
            return False
        return bool(state.is_superuser)

    # -- 注册验证(验证码 + 邮箱) -------------------------------------------

    def email_verification_enabled(self) -> bool:
        """邮箱验证是否启用(配了 SMTP 才启用; 单测可注入 environ)."""
        return V.email_enabled(self.environ)

    def email_verification_reason(self) -> str:
        return V.email_disabled_reason(self.environ)

    def send_verification_code(self, username: Any) -> tuple[bool, str]:
        """给账号邮箱发验证码. 没配 SMTP 时返回 ``(False, 原因)`` 而不抛异常."""
        return V.send_email_code(
            self.users, username, mailer=self.mailer, environ=self.environ,
        )

    def email_state_for(self, username: Any) -> dict[str, Any]:
        return V.email_verification_state(self.users, username, environ=self.environ)

    def page_invites(self, state: auth.AuthState, *, notice: str = "",
                    error: str = "") -> str:
        """邀请码页: 编辑者看自己的, 超级用户看全部(可生成)."""
        user = self.users.get_by_id(state.session.user_id) if state.session else {}
        me = str((user or {}).get("username") or "")
        see_all = self.can_review(state)
        scope = None if see_all else me
        return T.invites_page(
            self.context(state, active="invites"),
            invites=self.users.list_invites(created_by=scope),
            stats=self.users.invite_stats(created_by=scope),
            can_see_all=see_all,
            notice=notice,
            error=error,
        )

    def _do_invite_create(self, values: Mapping[str, Any],
                          state: auth.AuthState, cookies: list[str]) -> Response:
        user = self.users.get_by_id(state.session.user_id) if state.session else {}
        me = str((user or {}).get("username") or "")
        try:
            code = self.users.create_invite(
                note=text_field(values, "note"), created_by=me)
        except Exception as exc:                       # noqa: BLE001 - 报给页面
            return self._html(self.page_invites(state, error=f"生成失败: {exc}"),
                              status=400, cookies=cookies)
        response = Response.redirect("/invites", status=303)
        if state.session is not None:
            state.session.flash = f"已生成邀请码: {code}"
        return response

    def register_view(
        self,
        state: auth.AuthState | None,
        *,
        error: str = "",
        username: str = "",
        invite: str = "",
        email: str = "",
    ) -> str:
        """渲染注册页 —— **每次都生成一道新的验证码**(旧的自己过期, 不互相顶掉)."""
        return T.register_page(
            self.context(state, active="register"),
            error=error,
            username=username,
            invite=invite,
            email=email,
            captcha=V.new_captcha(kind="chars", graphic=True),
            email_enabled=self.email_verification_enabled(),
        )

    # -- 上下文 -------------------------------------------------------------

    def profile_unread_counts(self, state: auth.AuthState | None) -> tuple[int, int]:
        """导航红点用: ``(未读私信, 未读通知)``."""
        uid = self._member_uid(state) if state is not None else 0
        if not uid:
            return 0, 0
        return (
            int(self.profile_call("unread_message_count", uid, default=0) or 0),
            int(self.profile_call("unread_notification_count", uid, default=0) or 0),
        )

    def _current_lang(self) -> str:
        """当前请求的语言(不碰 session / flash, 纯读线程局部)."""
        return getattr(getattr(self, "_thread_lang", None), "value", "zh") or "zh"

    def lang_from_headers(self, headers: Mapping[str, Any]) -> str:
        """从 Cookie / Accept-Language 解析页面语言(默认 zh).

        语言是三档之一(zh / ja / en), 影响文案与**时间戳时区**
        (zh=UTC+8, ja=UTC+9, en=UTC)。
        """
        raw = str((headers or {}).get("cookie") or "")
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name == self.LANG_COOKIE:
                return I18N.language_of(value)
        return I18N.language_of(
            str((headers or {}).get("accept-language") or ""))

    def context(
        self,
        state: auth.AuthState | None = None,
        *,
        active: str = "",
        search_value: str = "",
    ) -> T.SiteContext:
        try:
            totals = self.data.totals()
        except (sqlite3.Error, PublicDataError) as exc:
            logger.warning("统计页头计数失败: %s", exc)
            totals = {}
        flash = ""
        if state is not None and state.session is not None and state.session.flash:
            # 闪信读一次就清掉(它存在服务端会话里, 不会变成第二个 Cookie)
            flash = state.session.flash
            state.session.flash = ""
        msg_unread, note_unread = self.profile_unread_counts(state)
        lang = getattr(getattr(self, "_thread_lang", None), "value", "zh") or "zh"
        needs_verify = False
        if state is not None and state.session is not None and state.session.user_id:
            current = self.users.get_by_id(state.session.user_id) or {}
            needs_verify = bool(current.get("email"))
            if needs_verify:
                needs_verify = not bool(current.get("email_verified"))
        return T.SiteContext(
            site_name=self.site_name,
            tagline=self.tagline,
            totals=totals,
            search_value=search_value,
            active=active,
            footer_note=self.footer_note,
            user=state.user if state is not None else None,
            is_editor=self.can_edit(state),
            is_superuser=bool(state and state.is_superuser),
            can_review=self.can_review(state),
            csrf_token=state.csrf_token if state is not None else "",
            flash=flash,
            messages_unread=msg_unread,
            notifications_unread=note_unread,
            needs_email_verify=needs_verify,
            lang=lang,
        )

    # -- 入口 ---------------------------------------------------------------

    def render(self, path: str, query: Mapping[str, Any] | None = None) -> Response:
        """匿名 GET(兼容旧调用方 / 单测便捷入口)."""
        return self.handle("GET", path, query)

    def handle(
        self,
        method: str,
        path: str,
        query: Mapping[str, Any] | None = None,
        *,
        headers: Mapping[str, Any] | None = None,
        body: bytes = b"",
        client_ip: str = "",
    ) -> Response:
        """一次请求 -> :class:`Response`. 路由 / 限速 / 鉴权 / CSRF 全在这里收口."""
        verb = str(method or "GET").upper()
        head = verb == "HEAD"
        headers = {str(k).lower(): v for k, v in (headers or {}).items()}
        params: Mapping[str, Any] = query or {}
        # ---- 语言是**请求级**状态 ----
        # 在唯一入口设置, 结束时复位: HTTP 层的 _dispatch 也会设一遍(用于
        # 静态文件等不经 handle 的路径), 但测试常直接调 handle(), 所以这里
        # 必须自己设, 否则会读到上一个请求残留的语言。
        _lang_holder = getattr(self, "_thread_lang", None)
        _lang_prev = getattr(_lang_holder, "value", "zh") if _lang_holder else "zh"
        if _lang_holder is not None:
            _lang_holder.value = self.lang_from_headers(headers)
        try:
            route = parse_route(path)
            limited = self.rate_limit(verb, route, params, client_ip)
            if limited is not None:
                return limited
            cookies = auth.parse_cookie_header(headers.get("cookie") or "")
            state = self.sessions.state(cookies)
            if state.logged_in and state.session is not None:
                # 滑动续期: 用户只要还在用, 就不该被踢下线
                self.sessions.touch(state.session.sid)
                # 「最后活跃」= 最近一次**请求**的时间(用户 m14663)。
                # last_login 只是登录那一刻, 拿它当活跃时间是错的。
                self._touch_seen(state)
            extra_cookies: list[str] = []
            if state.set_anonymous_cookie:
                # 给匿名访客发一个 CSRF 绑定 Cookie(双重提交)
                extra_cookies.append(auth.set_cookie(
                    auth.CSRF_COOKIE, state.anonymous_cookie, max_age=auth.SESSION_TTL,
                ))
            if verb in ("GET", "HEAD"):
                response = self._handle_read(route, params, state, extra_cookies, headers)
            else:
                response = self._handle_write(verb, route, state, headers, body, extra_cookies)
            # 访问日志(站务看板): 只记页面级请求, 静态资源和图片不记,
            # 否则一张专辑页就会刷出十几个"访问"。
            # 用户 m19090: 把真实的 method/status 也记上 —— 以前一律 "GET",
            # 保存失败的 POST 在看板上完全看不出来。
            if not head and route.get("name") not in _NO_VISIT_ROUTES:
                self._record_visit(path, route, state, headers, client_ip,
                                   method=verb, status=int(getattr(response, "status", 0) or 0))
        except (sqlite3.Error, PublicDataError, UserStoreError) as exc:
            logger.warning("处理 %s %s 失败: %s", verb, path, exc)
            response = Response.html(
                T.error_page(self.context(), status=500, message=f"读取数据失败: {exc}"),
                status=500,
            )
        except Exception as exc:  # 兜底: 开发服务器不该 500 得毫无信息
            logger.warning("%s %s 异常: %s", verb, path, exc)
            response = Response.html(
                T.error_page(self.context(), status=500, message=f"内部错误: {exc}"),
                status=500,
            )
        if _lang_holder is not None:
            _lang_holder.value = _lang_prev
        return response

    # -- 限速 ---------------------------------------------------------------

    def rate_limit(
        self,
        method: str,
        route: Mapping[str, str],
        query: Mapping[str, Any] | None = None,
        client_ip: str = "",
    ) -> Response | None:
        """超限则返回 **429 + Retry-After**; 放行返回 ``None``.

        ``robots.txt`` / ``sitemap.xml`` 完全不限速(爬虫要能取到它们),
        否则按 :func:`dddb.public.ratelimit.classify_request` 分档。
        """
        if self.limiter is None or not self.limiter.enabled:
            return None
        klass = classify_request(method, route.get("name"), query)
        if klass == "exempt":
            return None
        name = str(route.get("name") or "")
        key = str(client_ip or "unknown")
        decision = self.limiter.check(key, klass)
        if decision.allowed:
            return None
        logger.warning("限速: %s 请求过多(%s 档), 需等待 %ds", key, decision.klass,
                       decision.retry_after_seconds)
        response = Response.html(
            T.error_page(
                self.context(None), status=429,
                message=(
                    f"请求过于频繁({decision.klass} 档), 请等待 {decision.retry_after_seconds} 秒后重试。"
                ),
            ),
            status=429,
        )
        response.headers["Retry-After"] = str(decision.retry_after_seconds)
        return response

    # -- 读 -----------------------------------------------------------------

    def _handle_read(
        self,
        route: Mapping[str, str],
        params: Mapping[str, Any],
        state: auth.AuthState,
        cookies: list[str],
        headers: Mapping[str, Any] | None = None,
    ) -> Response:
        name = route["name"]
        arg = route["arg"]

        if name == "robots":
            return Response.binary(self.robots_text(headers).encode("utf-8"),
                                   "text/plain; charset=utf-8", status=200)
        if name == "sitemap":
            return self.serve_sitemap(arg, headers)
        if name == "static":
            return self.serve_static(arg)
        if name == "embed":
            return self.serve_embed(arg)
        if name == "event_logo":
            return self.serve_event_logo(arg)
        if name == "logo":
            return self.serve_logo_image(arg)
        if name == "pending_cover":
            return self.serve_pending_cover(arg)
        if name == "event_series_page":
            return Response.html(self.page_event_series(
                str(arg or ""), params or {}, state))
        if name == "event_series_edit":
            return self.page_event_series_edit(arg, params or {}, state)
        if name == "event_series_logo":
            return self.serve_series_image(str(arg or ""), "logo")
        if name == "event_series_poster":
            return self.serve_series_image(str(arg or ""), "poster")
        if name == "cover":
            return self.serve_cover(arg)
        if name == "photo":
            return self.serve_photo(arg)
        if name == "favicon":
            return self.serve_static("favicon.svg")
        if name == "username_check":
            # 注册页的**实时查重**: 只回 JSON, 不做写操作(也不走写限速)
            from .users import registration_name_problem
            text = _first(params, "u").strip()
            # 与注册提交**同一套判据**(长度/字符集 -> 违规词 -> 查重)
            problem = registration_name_problem(
                text, taken=self.users.get(text) is not None)
            if problem:
                payload = {"ok": False, "available": False, "reason": problem}
            else:
                payload = {"ok": True, "available": True, "reason": ""}
            import json as _json
            body = _json.dumps(payload, ensure_ascii=False).encode("utf-8")
            return Response.binary(body, "application/json; charset=utf-8")
        if name == "captcha_svg":
            svg = V.captcha_svg(_first(params, "id"))
            if not svg:                       # 过期/伪造 id
                return Response.binary(b"", "text/plain; charset=utf-8", status=404)
            return Response.binary(svg.encode("utf-8"), "image/svg+xml; charset=utf-8")
        if name == "invites":
            if not self.can_edit(state):
                return self._html(
                    self.forbidden(state, "邀请码页面只对编辑者/超级用户开放。"),
                    status=403, cookies=cookies,
                )
            return self._html(self.page_invites(state), cookies=cookies)
        if name == "link":
            return self.serve_outbound_link(arg)
        if name == "search":
            q = _first(params, "q")
            target = "/albums" + (f"?q={urllib.parse.quote(q)}" if q else "")
            return Response.redirect(target)
        if name == "lang":
            return self._do_lang(arg, params or {}, cookies,
                                 {k.lower(): v for k, v in (headers or {}).items()})

        if name == "new":
            kind = str(arg or "").strip().strip("/").lower()
            if kind not in T.NEW_ENTITY_SPECS:
                return Response.redirect("/edit", status=303)
            if not self.can_edit(state):
                return self._html(self.forbidden(state, "只有编辑者可以新建条目。"),
                                  status=403, cookies=cookies)
            if kind == "album":
                # **与编辑页完全一致的卡片界面**(不是那张 19 个文本框的旧表单)
                return self._html(T.edit_album_page(
                    self.context(state, active="edit"),
                    album={},
                    values=T.edit_values_from_album({}),
                    bind_options=self.bind_options(),
                    creating=True), cookies=cookies)
            return self._html(T.new_entity_page(
                self.context(state, active="edit"), kind,
                bind_options=self.bind_options()), cookies=cookies)

        if name in ("artist_edit", "circle_edit", "event_edit", "tag_edit"):
            return self._render_entity_edit(name.split("_")[0], arg, state, cookies)

        if name == "new_entity":
            kind = str(arg or "").strip().strip("/").lower() or _first(params, "kind").lower()
            if kind not in T.NEW_ENTITY_SPECS:
                return Response.redirect("/edit", status=303)
            if not self.can_edit(state):
                return self._html(self.forbidden(state, "只有编辑者可以新建条目。"),
                                  status=403, cookies=cookies)
            return self._html(T.new_entity_page(self.context(state, active="edit"), kind),
                              cookies=cookies)
        if name == "artists":
            return self._html(self.page_artists(params or {}, state), cookies=cookies)
        if name == "artist":
            status, html = self.page_artist(arg, params or {}, state)
            return self._html(html, status=status, cookies=cookies)
        if name == "forgot":
            notice = ("如果这个账号存在并绑定了邮箱, 我们已发送重置密码的邮件, "
                      "请到邮箱点击链接。" if _first(params or {}, "sent") else "")
            return self._html(T.forgot_page(self.context(state, active="login"),
                                            notice=notice), cookies=cookies)
        if name == "reset":
            token = _first(params or {}, "token")
            ok, reason, uname = self.users.verify_link_token(
                token, purpose=V.PURPOSE_PASSWORD_RESET)
            if not ok:
                return self._html(T.reset_page(
                    self.context(state, active="login"), token="", error=reason),
                    status=400, cookies=cookies)
            return self._html(T.reset_page(
                self.context(state, active="login"), token=token, username=uname),
                cookies=cookies)
        if name == "login":
            if state.logged_in:
                return Response.redirect("/account", status=303)
            notice = ""
            if _first(params, "sent"):
                notice = "验证码已发送(如果该账号需要验证邮箱), 请查收"
            elif _first(params, "expired"):
                notice = "页面已过期（可能在其他标签页切换过账号），请重新输入后登录。"
            return self._html(T.login_page(
                self.context(state, active="login"),
                username=_first(params, "username"),
                next_url=_safe_next(_first(params, "next")),
                notice=notice,
            ), cookies=cookies)
        if name == "register":
            if state.logged_in:
                return Response.redirect("/account", status=303)
            return self._html(self.register_view(state), cookies=cookies)
        if name == "verify_email":
            token = _first(params, "token")
            if token:
                ok, reason, uname = V.check_email_link(self.users, token)
                purpose = V.PURPOSE_EMAIL_LINK
                if not ok:
                    ok, reason, uname = V.check_email_link(
                        self.users, token, purpose=V.PURPOSE_LOGIN_LINK)
                    purpose = V.PURPOSE_LOGIN_LINK
                if not ok:
                    return self._html(T.verify_page(
                        self.context(state, active="verify"), username="",
                        error=reason), status=400, cookies=cookies)
                if purpose == V.PURPOSE_EMAIL_LINK:
                    self.users.mark_email_verified(uname)
                    self.users.set_last_env(uname, self._env_hash(headers))
                    target, note = "/account", "邮箱验证成功, 账号已激活。"
                else:
                    if state.session is not None:
                        self.sessions.confirm(state.session.sid)
                    self.users.set_last_env(uname, self._env_hash(headers))
                    target, note = "/account", "登录环境已确认。"
                if state.session is not None:
                    state.session.flash = note
                response = Response.redirect(target + "?tab=security", status=303)
                return response
            # GET /verify[?username=]  邮箱验证页
            if state.logged_in and state.session is not None:
                current = self.users.get_by_id(state.session.user_id) or {}
                if current.get("email_verified"):
                    # 已经验证过了: 不用再看这张表单
                    state.session.flash = "邮箱已验证"
                    return Response.redirect("/account", status=303)
            return self._html(self.page_verify(state, params), cookies=cookies)
        if name == "review":
            if not self.can_review(state):
                return self._html(
                    self.forbidden(state, "审核队列只对超级用户开放。"),
                    status=403, cookies=cookies,
                )
            return self._html(self.page_review(state, params, None, None), cookies=cookies)
        if name == "users_admin":
            if not self.can_review(state):
                return self._html(
                    self.forbidden(state, "用户管理只对超级用户开放。"),
                    status=403, cookies=cookies,
                )
            return self._html(self.page_users_admin(state, None, None), cookies=cookies)
        if name == "dashboard":
            # 站务看板(用户 m14663): 访问记录 + 编辑改动 + 服务器负载,
            # 只对超级用户开放, 匿名访问一律 403(不泄露"这里有什么")。
            if not self.can_review(state):
                return self._html(
                    self.forbidden(state, "数据看板只对站务账号开放。"),
                    status=403, cookies=cookies,
                )
            return self._html(self.page_dashboard(state), cookies=cookies)
        if name == "logout":
            # 登出是写操作, 只接受 POST(导航里就是一个带 token 的小表单)。
            return self._html(T.error_page(
                self.context(state), status=405,
                message="登出请通过页面上的「登出」按钮提交(本站所有写操作都走 POST)。",
            ), status=405, cookies=cookies)
        if name == "account":
            if not state.logged_in or state.session is None:
                return Response.redirect("/login?next=/account")
            user = self.users.get_by_id(state.session.user_id) or state.user or {}
            return self._html(self.page_account(state, user,
                                                tab=_first(params, "tab")), cookies=cookies)
        if name == "avatar":
            return self.avatar_response(arg)
        if name == "background":
            return self.background_response(arg)
        if name == "album_history":
            if not self.can_edit(state):
                return self._html(self.forbidden(state, "只有编辑者才能查看编辑历史。"),
                                  status=403, cookies=cookies)
            return self._html(self.page_history(state, arg, params), cookies=cookies)
        if name in ("account_password", "account_delete", "account_security_code",
                    "album_delete", "album_cover", "album_comment",
                    "comment_delete", "comment_report",
                    "favorites_toggle", "follows_toggle", "messages_send",
                    "notifications_read", "account_privacy", "account_profile",
                    "account_avatar"):
            return self._html(T.error_page(
                self.context(state), status=405,
                message="这个地址只接受表单提交(POST)。",
            ), status=405, cookies=cookies)
        if name in ("messages", "notifications", "feedback"):
            if not state.logged_in or state.session is None:
                return Response.redirect(f"/login?next=/{name}")
            page = {
                "messages": self.page_messages,
                "notifications": self.page_notifications,
                "feedback": self.page_feedback,
            }[name]
            return self._html(page(state, params), cookies=cookies)
        if name == "user_page":
            return self._html(self.page_user(state, arg), cookies=cookies)
        if name == "album_edit":
            key = arg
            if not self.can_edit(state):
                return self._html(self.forbidden(state, "只有编辑者才能打开编辑界面。"),
                                  status=403, cookies=cookies)
            album = self.data.get_album(key)
            if album is None:
                return self._html(T.error_page(
                    self.context(state, active="albums"), status=404, message=f"没有这张专辑: {key}"
                ), status=404, cookies=cookies)
            # **编辑页必须用带外链名称的数据**: 公开端视图的 urls 只有
            # {"url","href"}(合规要求: HTML 里不出现来源站名), 拿它渲染
            # 编辑表单会让 url_key 全空 -> 保存后名称退化成 link1/link2。
            edit_album = self._album_with_url_names(key) or album
            # 编辑界面是**固定网格布局**(不拖动/不切大小), 不再读布局偏好
            return self._html(T.edit_album_page(
                self.context(state, active="albums"),
                album=edit_album,
                values=T.edit_values_from_album(edit_album),
                bind_options=self.bind_options(),
            ), cookies=cookies)

        # 「工作队列」是编辑工具: 普通用户/访客**连页面都不渲染**(403), 而不是
        # 渲染一张"你看到的是空队列"的页面 —— 后者会让人以为站内没有待办。
        if name == "edit" and not self.can_edit(state):
            return self._html(
                self.forbidden(state, "工作队列只对编辑者开放。"),
                status=403, cookies=cookies,
            )

        try:
            status, rendered = self.render_page(name, arg, params, state)
        except (sqlite3.Error, PublicDataError) as exc:
            logger.warning("渲染 %s 失败: %s", route["name"], exc)
            status, rendered = 500, T.error_page(
                self.context(state), status=500, message=f"读取数据失败: {exc}"
            )
        if status == 403:
            return self._html(rendered, status=403, cookies=cookies)
        if status >= 400:
            return self._html(rendered, status=status, cookies=cookies)
        return self._html(rendered, cookies=cookies)

    def render_page(
        self,
        name: str,
        arg: str,
        params: Mapping[str, Any],
        state: auth.AuthState | None = None,
    ) -> tuple[int, str]:
        """路由 -> ``(状态码, HTML)``. 独立出来方便单测直接调."""
        if name == "home":
            return 200, self.page_home(state)
        if name == "albums":
            return 200, self.page_albums(params, state)
        if name == "album":
            return self.page_album(arg, state)
        if name == "circles":
            return 200, self.page_circles(params, state)
        if name == "circle":
            return 200, self.page_circle(arg, params, state)
        if name == "events":
            return 200, self.page_events(state, params)
        if name == "event":
            return 200, self.page_event(arg, params, state)
        if name == "event_date":
            # 这里在 render_route 里(返回 (status, html)), 不能返回 Response;
            # 展会名字在 **query 参数**里(不是路径), 所以从 params 取。
            return 200, self.event_date_json(str(_first(params, "name") or ""))
        if name == "tags":
            return 200, self.page_tags(state)
        if name == "tag":
            return 200, self.page_tag(arg, params, state)
        if name == "stats":
            return 200, T.stats_page(self.context(state, active="stats"), stats=self.data.stats())
        if name == "edit":
            return 200, self.page_edit(arg, params, state)
        if name == "about":
            return 200, T.about_page(self.context(state, active="about"), stats=self.data.stats())
        return 404, T.error_page(self.context(state), status=404, message=f"页面不存在: {arg or '/'}")

    # -- 写 -----------------------------------------------------------------

    def _handle_write(
        self,
        verb: str,
        route: Mapping[str, str],
        state: auth.AuthState,
        headers: Mapping[str, Any],
        body: bytes,
        cookies: list[str],
    ) -> Response:
        """所有非 GET/HEAD 的入口.

        规则(任务要求): **非 GET/HEAD 且没有编辑者身份 -> 403**。
        登录/注册是唯一两个允许匿名提交的写入口, 但它们同样要做同源 + CSRF 校验。
        """
        name = route["name"]
        arg = route["arg"]

        # 写入口的鉴权门槛:
        # * 登录 / 注册: 谁都能提交(靠同源 + CSRF 防跨站), 表单里没有可伪造的角色;
        # * 登出: 必须**自己已登录**(否则无从登出; 它只销毁自己的会话, 不是数据写操作);
        # * 邮箱验证 / 重发验证码: 登录后可用; 重发也允许匿名(靠同名账号 + 60 秒冷却兜底,
        #   响应里不区分"账号是否存在");
        # * 我的账号: 任何已登录用户(普通用户要能**申请成为编辑者**);
        # * 审核队列 / 用户管理: **仅超级用户**;
        # * 其余(专辑编辑等): 编辑者 / 超级用户。
        if name in ("login", "register", "forgot", "reset"):
            allowed = verb == "POST"
        elif name == "logout":
            allowed = state.logged_in
        elif name == "verify_email":
            allowed = verb == "POST"
        elif name == "account":
            allowed = state.logged_in
        elif name in ("review", "users_admin"):
            allowed = self.can_review(state)
        elif name in _MEMBER_WRITE_ROUTES:
            # 个人中心: **登录即可**, 不需要编辑权限
            allowed = verb == "POST" and state.logged_in
        else:
            allowed = self.can_edit(state)
        if allowed and state.logged_in and state.session is not None:
            me = self.users.get_by_id(state.session.user_id) or {}
            if me.get("email") and not me.get("email_verified") and name not in (
                "logout", "verify_email", "verify_resend", "login", "register",
                "account", "account_security_code",
            ):
                logger.warning("写操作被拒(未验证邮箱): %s", name)
                return self._html(
                    self.forbidden(state, "请先完成邮箱验证(未验证账号只有访客权限)。"),
                    status=403, cookies=cookies)
        if not allowed:
            logger.warning(
                "写操作被拒(权限): %s %s · 登录=%s · origin=%r referer=%r host=%r",
                verb, name, bool(state.logged_in),
                (headers.get("origin") or "")[:120],
                (headers.get("referer") or "")[:120],
                headers.get("host") or "",
            )
            if name in ("review", "users_admin"):
                message = "需要超级用户权限才能执行这个操作。"
            elif name == "account":
                message = "请先登录。"
            else:
                message = "需要编辑者身份才能执行写操作。"
            return self._html(self.forbidden(state, message), status=403, cookies=cookies)

        if len(body) > MAX_BODY:
            return self._html(T.error_page(
                self.context(state), status=413,
                message="提交内容过大(上限 20MB)。"
                        "如果是图片，请压缩到 8MB 以内。"
            ), status=413, cookies=cookies)

        origin_ok, origin_message = auth.same_origin(
            headers.get("origin") or "", headers.get("referer") or "", headers.get("host") or ""
        )
        if not origin_ok:
            logger.warning(
                "写操作被拒(跨站): %s %s · origin=%r referer=%r host=%r",
                verb, name, (headers.get("origin") or "")[:160],
                (headers.get("referer") or "")[:160], headers.get("host") or "",
            )
            return self._html(self.forbidden(state, f"{origin_message}(跨站提交被拒绝)。"),
                              status=403, cookies=cookies)

        values = form_values(body)
        # 表单只要带 enctype="multipart/form-data"(有文件上传), 浏览器
        # 就一定用 multipart 提交 —— form_values 解析不出任何字段, values
        # 会全空, 保存时每个字段都读成空串(表现: 提交了但信息丢失)。
        # 这里**统一**补一次, 不再让每个编辑路由各补一遍。
        _ctype = str((headers or {}).get("content-type")
                     or (headers or {}).get("Content-Type") or "")
        if _ctype.lower().startswith("multipart/form-data") and body:
            try:
                values = {**dict(values),
                          **self._values_from_multipart(headers, body)}
            except Exception as _exc:          # noqa: BLE001
                logger.warning("multipart 解析失败: %s", _exc)
        csrf_given = text_field(values, auth.CSRF_FIELD)
        if not csrf_given:
            # multipart/form-data(头像上传) 解析不出普通表单字段, 单独取一次
            csrf_given = parse_form_field(headers, body, auth.CSRF_FIELD) or ""
        if not self.sessions.signer.check(state.csrf_binding, csrf_given):
            logger.warning(
                "写操作被拒(CSRF): %s %s · 登录=%s · 带了 token=%s · origin=%r host=%r",
                verb, name, bool(state.logged_in),
                bool(text_field(values, auth.CSRF_FIELD)),
                (headers.get("origin") or "")[:120], headers.get("host") or "",
            )
            # **登录/注册**是最容易"页面放了很久再提交"的场景:
            # 直接重渲染一张新表单(带新 token)并提示过期, 比 403 友好得多。
            expired = "页面已过期，请重新提交。"
            if name == "login":
                # 典型场景: 已登录(A) 却提交了更早渲染的登录表单(令牌属于旧会话)。
                # 直接退掉旧会话, 让用户拿到一张与新状态匹配的新表单。
                if state.session is not None:
                    self.sessions.destroy(state.session.sid)
                response = Response.redirect("/login?expired=1", status=303)
                return response
            if name == "register":
                return self._html(self.register_view(
                    state, error=expired,
                    username=text_field(values, "username"),
                    invite=text_field(values, "invite"),
                    email=text_field(values, "email"),
                ), cookies=cookies)
            return self._html(self.forbidden(state, "表单校验失败(token 缺失或不匹配), 请刷新页面后重试。"),
                              status=403, cookies=cookies)

        if name == "login":
            return self._do_login(values, state, headers, cookies)
        if name == "register":
            return self._do_register(values, state, cookies)
        if name == "forgot":
            return self._do_forgot(values, state, cookies)
        if name == "reset":
            return self._do_reset(values, state, cookies)
        if name == "invites":
            return self._do_invite_create(values, state, cookies)
        if name == "circle_comment":
            return self._do_circle_comment(values, state, cookies)

        if name == "event_series_edit":
            return self._do_series_save(str(arg or "").strip("/"), values,
                                        state, cookies, headers, body)
        if name == "new":
            return self._new_entity_with_fallback(
                str(arg or "").strip().strip("/").lower(),
                values, state, cookies, headers, body)

        if name in ("artist_edit", "circle_edit", "event_edit", "tag_edit"):
            return self._do_entity_update(name.split("_")[0], arg, values, state, cookies)

        if name == "new_entity":
            return self._new_entity_with_fallback(
                str(arg or "").strip().strip("/").lower(),
                values, state, cookies)

        if name == "artist_comment":
            return self._do_artist_comment(values, state, cookies)

        if name == "album_delete":
            return self._do_album_delete(arg, values, state, cookies)
        if name == "album_merge":
            return self._do_album_merge(arg, values, state, cookies)
        if name == "pending_cover":
            return self._do_pending_cover(arg, values, state, cookies)

        if name == "comment_like":
            return self._do_comment_like(text_field(values, "comment_id"), values, state, cookies)

        if name == "album_comment":
            return self._do_album_comment(values, state, cookies)
        if name == "account_layout":
            return self._do_account_layout(values, state, cookies)
        if name == "album_cover":
            return self._do_album_cover(route.get("arg") or "", state, body,
                                        headers, cookies, values)
        if name == "album_cover_url":
            return self._do_album_cover_url(route.get("arg") or "", state, values,
                                            cookies)
        if name == "album_photo":
            return self._do_album_photo(route.get("arg") or "", state, body,
                                        headers, cookies, values)
        if name == "album_photo_del":
            return self._do_album_photo_del(route.get("arg") or "", state,
                                            values, cookies)
        if name == "album_photo_note":
            return self._do_album_photo_note(route.get("arg") or "", state,
                                             values, cookies)
        if name == "album_revert":
            return self._do_album_revert(values, state, cookies)
        if name in ("comment_delete", "comment_report"):
            return self._do_comment_action(name, values, state, cookies)
        if name in _MEMBER_WRITE_ROUTES:
            return self._do_member_write(name, values, state, body, headers, cookies)
        if name == "logout":
            self.sessions.destroy(state.session.sid if state.session else "")
            response = Response.redirect("/", status=303)
            # 服务端会话已销毁, 浏览器上的 Cookie 也要立刻清掉
            response.cookies.append(auth.delete_cookie(auth.SESSION_COOKIE))
            response.cookies.append(auth.delete_cookie(auth.CSRF_COOKIE))
            return response
        if name == "album_edit":
            if verb != "POST":
                return self._html(T.error_page(
                    self.context(state), status=405, message="编辑接口只接受 POST。"
                ), status=405, cookies=cookies)
            # 编辑表单是 enctype="multipart/form-data" —— 浏览器一定会用 multipart
            # 提交(里面可能有封面/图片字段)。只解析 urlencoded 会让 values 全空,
            # 表现就是"标题有内容却报标题不能为空"。这里补一次 multipart 解析。
            _ctype = str((headers or {}).get("content-type")
                         or (headers or {}).get("Content-Type") or "")
            if _ctype.lower().startswith("multipart/form-data") and body:
                values = {**dict(values),
                          **self._values_from_multipart(headers, body)}
            return self._do_album_edit(arg, values, state, cookies,
                                       headers=headers, body=body)
        if name == "account":
            return self._do_account(values, state, cookies)
        if name == "verify_email":
            if verb != "POST":
                return self._html(T.error_page(
                    self.context(state), status=405, message="邮箱验证只接受 POST。"
                ), status=405, cookies=cookies)
            if str(arg).strip().lower() == "resend":
                return self._do_resend(values, state, cookies)
            return self._do_verify_code(values, state, cookies)
        if name == "review":
            return self._do_review(values, state, cookies)
        if name == "users_admin":
            return self._do_users_admin(values, state, cookies)
        # 编辑者但路径/方法不对: 405(方法/资源本身有问题, 不是权限问题)
        return self._html(T.error_page(
            self.context(state), status=405,
            message=f"不支持的写操作: {verb} {route.get('name') or '/'}",
        ), status=405, cookies=cookies)

    def send_login_link(self, username: str, purpose: str) -> tuple[bool, str]:
        """发一封带一次性链接的邮件(注册验证 / 新环境登录确认)."""
        try:
            return V.send_email_link(
                self.users, username, base_url=self.base_url or "",
                purpose=purpose, mailer=self.mailer, environ=self.environ,
            )
        except Exception as exc:                   # noqa: BLE001 - 发信失败不该 500
            logger.warning("发送验证邮件失败(%s): %s", username, exc)
            return False, f"邮件发送失败: {exc}"

    @staticmethod
    def _env_hash(headers: Mapping[str, Any]) -> str:
        """登录环境指纹: **只认设备特征**(User-Agent + 语言), 不含 IP.

        原先包含客户端 IP, 结果家用宽带/移动网络一换 IP 就被判成"新登录环境",
        用户会反复收到确认邮件 —— 那不是安全事件, 只是运营商重新分配地址。
        现在只看设备和语言: 换设备/换浏览器仍会被识别为陌生设备, 换网络不会。
        """
        import hashlib as _hashlib
        ua = str(headers.get("user-agent") or "").strip()
        lang = str(headers.get("accept-language") or "").strip()
        if not ua:
            # 没有 UA 说明不是正常浏览器, 退回宽松判定(不触发新环境)
            return ""
        return _hashlib.sha256(f"{ua}|{lang}".encode("utf-8", "replace")).hexdigest()

    def _do_forgot(self, values: Mapping[str, Any], state: auth.AuthState,
                   cookies: list[str]) -> Response:
        """忘记密码: 发重置链接; 无论账号是否存在都回同样的提示(不泄漏)."""
        identifier = text_field(values, "identifier").strip()
        user = self.users.get(identifier) if identifier else None
        if user is None and identifier:
            user = self.users.find_by_email(identifier)
        if user is not None:
            address = str(user.get("email") or "")
            if address:
                V.send_email_link(
                    self.users, str(user.get("username") or ""),
                    base_url=self.base_url or "", purpose=V.PURPOSE_PASSWORD_RESET,
                    mailer=self.mailer, environ=self.environ)
        return Response.redirect("/forgot?sent=1", status=303)

    def _do_reset(self, values: Mapping[str, Any], state: auth.AuthState,
                  cookies: list[str]) -> Response:
        """凭链接令牌设置新密码(令牌一次性), 并踢掉该账号的全部会话."""
        token = text_field(values, "token")
        new = text_field(values, "new_password")
        again = text_field(values, "new_password2")
        ok, reason, username = self.users.verify_link_token(
            token, purpose=V.PURPOSE_PASSWORD_RESET)
        if not ok:
            return self._html(T.reset_page(self.context(state, active="login"),
                                            token="", error=reason),
                              status=400, cookies=cookies)
        if new != again:
            return self._html(T.reset_page(self.context(state, active="login"),
                                            token=token, username=username,
                                            error="两次输入的新密码不一致"),
                              status=400, cookies=cookies)
        ok, message = self.users.set_password(username, new)
        if not ok:
            return self._html(T.reset_page(self.context(state, active="login"),
                                            token=token, username=username,
                                            error=message),
                              status=400, cookies=cookies)
        self.users.redeem_email_link(token, purpose=V.PURPOSE_PASSWORD_RESET)
        target_user = self.users.get(username) or {}
        if target_user:
            self.sessions.drop_user(int(target_user.get("id") or 0))
        return Response.redirect("/login?reset=1", status=303)

    def _do_login(
        self,
        values: Mapping[str, Sequence[str]],
        state: auth.AuthState,
        headers: Mapping[str, Any],
        cookies: list[str],
    ) -> Response:
        username = text_field(values, "username")
        password = text_field(values, "password")
        next_url = _safe_next(text_field(values, "next"))
        result = self.users.authenticate(username, password)
        if not result.ok:
            # 密码错: 不细分"用户不存在"; 禁用 / 邮箱未验证给**明确原因**,
            # 未验证时额外给「重新发送验证码」的入口。
            error = ("用户名或密码不正确"
                     if result.reason not in (LOGIN_INACTIVE, LOGIN_UNVERIFIED_EMAIL)
                     else result.message)
            page = T.login_page(
                self.context(state, active="login"),
                error=error,
                username=username,
                next_url=next_url,
                resend_username=(username if result.reason == LOGIN_UNVERIFIED_EMAIL else ""),
            )
            return self._html(page, cookies=cookies)
        user = result.user or {}
        if state.session is not None:
            # 换账号登录: 先销毁旧会话(别让同一个人占两个 sid)
            self.sessions.destroy(state.session.sid)
        # 未验证邮箱的账号可以登录, 但只有访客权限 -> 用闪信提示
        if user.get("email") and not user.get("email_verified"):
            try:
                self.users.purge_unverified()
            except Exception:                        # noqa: BLE001
                pass
        env_hash = self._env_hash(headers)
        last_env = self.users.last_env(str(user.get("username") or username))
        # 只有**设备不同**才算新环境; 空指纹/未验证邮箱/未配置发信都不拦截
        risky = bool(last_env and env_hash and last_env != env_hash
                     and user.get("email_verified") and self.email_verification_enabled()
                     and getattr(self, "require_new_device_confirm", True))
        session = self.sessions.create(user, env_hash=env_hash, confirmed=not risky)
        if risky:
            sent, send_message = self.send_login_link(
                str(user.get("username") or username), V.PURPOSE_LOGIN_LINK)
            session.flash = (
                "检测到新的登录环境, 我们已发送确认邮件到你的邮箱, "
                "点击邮件里的链接即可确认本次登录。"
                if sent else f"检测到新的登录环境, 但确认邮件发送失败: {send_message}"
            )
        else:
            if user.get("email") and not user.get("email_verified"):
                session.flash = (
                    f"欢迎回来, {user.get('username') or username}。"
                    "但你的邮箱还没有验证：未验证时只有浏览权限，"
                    "24 小时内不验证将删除账号，请查收验证邮件。"
                )
            else:
                session.flash = f"欢迎回来, {user.get('username') or username}"
            # 每次登录都刷新设备指纹: 老库里存的是"IP+UA"旧指纹, 这次登录后自愈
            if env_hash:
                self.users.set_last_env(str(user.get("username") or username), env_hash)
        target = next_url if next_url.startswith("/") else "/account"
        response = Response.redirect(target, status=303)
        response.cookies.append(auth.set_cookie(
            auth.SESSION_COOKIE, session.sid, max_age=auth.SESSION_TTL,
        ))
        return response

    def _do_register(
        self,
        values: Mapping[str, Sequence[str]],
        state: auth.AuthState,
        cookies: list[str],
    ) -> Response:
        username = text_field(values, "username")
        password = text_field(values, "password")
        again = text_field(values, "password2")
        invite = text_field(values, "invite")
        email = text_field(values, "email")

        def fail(message: str) -> Response:
            return self._html(self.register_view(
                state, error=message, username=username, invite=invite, email=email,
            ), cookies=cookies)

        # 1) **验证码先于一切**: 答错/过期都不该产生任何账号
        captcha_ok, captcha_reason = V.check_captcha(
            text_field(values, "captcha_id"), text_field(values, "captcha_answer"),
        )
        if not captcha_ok:
            return fail(captcha_reason)

        if password != again:
            return fail("两次输入的密码不一致")

        # 用户名判据(长度/字符集 -> 违规词): 只拦**用户自助注册**
        from .users import registration_name_problem
        name_problem = registration_name_problem(username)
        if name_problem:
            return fail(name_problem)

        # 2) 邮箱: 启用邮箱验证时**必填**, 否则注册就没有账号可验证了
        email_enabled = self.email_verification_enabled()
        address = ""
        if email_enabled:
            address = normalize_email(email)
            if not address:
                return fail("请填写邮箱(本站启用了邮箱验证)")
            problem = email_problem(address)
            if problem:
                return fail(problem)

        # 3) 角色**由服务端决定**: 邀请码有效且没用过才是 editor, 表单里没有 role 字段可伪造。
        info = self.users.invite_info(invite) if invite else None
        valid_invite = bool(info and not info.get("used"))
        if invite and not valid_invite:
            return fail("邀请码无效或已被使用(留空则注册为普通用户)")
        role = ROLE_EDITOR if valid_invite else ROLE_VIEWER
        ok, message = self.users.create_user(
            username, password, role=role,
            email=address if email_enabled else "",
            # 启用邮箱验证时账号先建成"待验证", 验证通过后才能登录
            email_verified=False if (email_enabled and address) else True,
        )
        if not ok:
            return fail(message)
        user = self.users.get(username) or {}
        if role == ROLE_EDITOR:
            self.users.redeem_invite(invite, user.get("username") or username)

        # 4) 发验证码: **发信失败不阻断建号**, 只把原因告诉用户
        send_failed = ""
        if email_enabled and address:
            sent, send_message = self.send_login_link(
                user.get("username") or username, V.PURPOSE_EMAIL_LINK)
            if not sent:
                send_failed = send_message

        session = self.sessions.create(user)
        if email_enabled and address:
            session.flash = (
                f"注册成功, 已登录。验证邮件已发送到 {address}: "
                "点击邮件里的链接即可完成验证(未验证时只有浏览权限, 24 小时内不验证将删除账号)"
                if not send_failed else
                f"注册成功, 已登录。但验证邮件发送失败: {send_failed}"
            )
            target = "/account?tab=overview"
        else:
            session.flash = (
                "注册成功, 已获得编辑权限" if role == ROLE_EDITOR else "注册成功, 欢迎"
            )
            target = "/account"
        response = Response.redirect(target, status=303)
        response.cookies.append(auth.set_cookie(
            auth.SESSION_COOKIE, session.sid, max_age=auth.SESSION_TTL,
        ))
        return response

    # -- 账号页 / 邮箱验证 / 审核 / 用户管理 --------------------------------

    def page_account(
        self,
        state: auth.AuthState,
        user: Mapping[str, Any],
        *,
        error: str = "",
        notice: str = "",
        tab: str = "overview",
    ) -> str:
        # 时间按**语言时区**显示(zh=UTC+8), 不依赖服务器时区
        _lang = self._current_lang()
        username = user.get("username") or ""
        request = self.users.get_editor_request(username) if username else None
        role = str(user.get("role") or ROLE_VIEWER)
        can_request = role == ROLE_VIEWER and (
            request is None or str(request.get("status") or "") == REQUEST_REJECTED
        )
        uid = int(user.get("id") or 0)
        profile = self.profile_data(uid, username)
        favorites = list(self.profile_call("list_favorites", uid, limit=50, default=[]) or [])
        for item in favorites:
            try:
                album = self.data.get_album(item.get("album_key") or "")
            except Exception:                      # noqa: BLE001
                album = None
            if album is not None:
                item["title"] = album.get("title") or ""
                if album.get("cover_local") or album.get("has_cover"):
                    # 带版本号, 否则换了封面这里还是旧图
                    item["cover_src"] = T.cover_src(
                        str(item.get("album_key") or ""), album)
        follows = list(self.profile_call("list_follows", uid, default=[]) or [])
        comments = list(self.data.comments_by(username) or [])
        for item in comments:
            item["created_at_text"] = _stamp_text(item.get("created_at"), _lang)
            try:
                album = self.data.get_album(item.get("target_key") or "")
            except Exception:                  # noqa: BLE001
                album = None
            item["album_key"] = item.get("target_key") or ""
            item["album_title"] = (album or {}).get("title") or ""
        contributions = list(self.profile_call("list_contributions", uid, limit=50,
                                               default=[]) or [])
        for item in contributions:
            item["created_at_text"] = _stamp_text(item.get("created_at"), _lang)
        feedbacks = list(self.profile_call("list_feedback", uid, default=[]) or [])
        for item in feedbacks:
            item["created_at_text"] = _stamp_text(item.get("created_at"), _lang)
        uid_for_tabs = int(user.get("id") or 0)
        inbox: list[dict[str, Any]] = []
        notifications: list[dict[str, Any]] = []
        if tab == "messages":
            inbox = list(self.profile_call("list_messages", uid_for_tabs,
                                           box="inbox", limit=10, default=[]) or [])
        elif tab == "notifications":
            notifications = list(self.profile_call("list_notifications", uid_for_tabs,
                                                   limit=10, default=[]) or [])
        return T.account_page(
            self.context(state, active="account"),
            user=user,
            created_text=_stamp_text(user.get("created_at"), _lang),
            last_login_text=_stamp_text(user.get("last_login"), _lang),
            editor_request=request,
            email_state=self.email_state_for(username) if username else None,
            can_request=can_request,
            error=error,
            notice=notice,
            profile=profile,
            favorites=favorites,
            follows=follows,
            comments=comments,
            contributions=contributions if role in ("editor", "superuser") else [],
            feedbacks=feedbacks,
            messages_meta={"unread": self.profile_call("unread_message_count", uid, default=0)},
            notifications_meta={"unread": self.profile_call("unread_notification_count",
                                                            uid, default=0)},
            inbox=inbox,
            notifications=notifications,
            tab=tab,
            visibility=self.profile_call("visibility", uid, default={}) or {},
            session_confirmed=bool(state.session.confirmed) if state.session else True,
        )

    # -- 个人中心: 页面与响应 ------------------------------------------------

    def _member_uid(self, state: auth.AuthState) -> int:
        try:
            return int(state.session.user_id) if state.session else 0
        except (TypeError, ValueError):
            return 0

    # -- 最后活跃 / 访问记录 -------------------------------------------------

    def _touch_seen(self, state: auth.AuthState) -> None:
        """登录用户每访问一次就刷新「最后活跃」(节流 5 分钟, 见 touch_seen)."""
        uid = self._member_uid(state)
        if not uid:
            return
        touch = getattr(self.users, "touch_seen", None)
        if touch is None:                      # pragma: no cover - 防御性
            return
        try:
            touch(uid)
        except Exception:                      # noqa: BLE001 - 记活跃失败不该影响页面
            logger.debug("刷新最后活跃失败 uid=%s", uid, exc_info=True)

    def _entity_image(self, kind: str, key: str) -> str:
        """社团徽标 / 艺人头像. 站内 ``/logo/...`` 与远程直链都原样返回."""
        name = str(key or "").strip()
        if not name:
            return ""
        table, column = ("circles", "logo_url") if kind == "circle" else ("artists", "avatar")
        try:
            rows = self.data._query(
                f"SELECT payload FROM {table} WHERE name = ? COLLATE NOCASE LIMIT 1",
                (name,))
        except (sqlite3.Error, Exception):     # noqa: BLE001 - 只读库可能没有这张表
            return ""
        if not rows:
            return ""
        try:
            record = json.loads(rows[0]["payload"] or "{}")
        except ValueError:
            return ""
        image = str(record.get(column) or "")
        if not image and kind == "circle":
            image = str(record.get("logo_local") or "")
        return image

    def _enrich_follows(self, follows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """给关注列表补站内链接与头像(用户 m14663: 要显示社团/艺人头像)."""
        out: list[dict[str, Any]] = []
        for item in follows or []:
            entry = dict(item)
            kind = str(entry.get("kind") or "")
            key = str(entry.get("key") or "")
            href = ""
            if kind == "circle" and key:
                href = "/circle/" + T.quote_segment(key)
            elif kind == "artist" and key:
                href = "/artist/" + urllib.parse.quote(key, safe="")
            elif kind == "event" and key:
                href = "/event/" + T.quote_segment(key)
            entry["href"] = href
            entry["image"] = (self._entity_image(kind, key)
                              if kind in ("circle", "artist") else "")
            out.append(entry)
        return out

    @staticmethod
    def _merge_contributions(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """同一 (kind, target) 的多条贡献并成一条并计数(用户 m14663).

        ``list_contributions`` 按时间倒序返回, 所以第一次出现的那条自带
        最新的时间与摘要 —— 保留它, 后面只累加次数。
        """
        merged: dict[tuple[str, str], dict[str, Any]] = {}
        order: list[tuple[str, str]] = []
        for item in items or []:
            kind = str(item.get("kind") or "")
            target = str(item.get("target") or "")
            slot = (kind, target)
            entry = merged.get(slot)
            if entry is None:
                entry = dict(item)
                entry["count"] = 1
                merged[slot] = entry
                order.append(slot)
            else:
                entry["count"] = int(entry.get("count") or 1) + 1
        return [merged[slot] for slot in order]

    def _do_comment_action(self, name: str, values: Mapping[str, Any],
                           state: auth.AuthState, cookies: list[str]) -> Response:
        """评论删除(仅本人/超级用户) 与 举报(任何登录用户)."""
        uid = self._member_uid(state)
        comment_id = text_field(values, "comment_id")
        comment = self.data.get_comment(comment_id)
        if comment is None:
            return self._html(self.forbidden(state, "这条评论不存在或已删除。"),
                              status=404, cookies=cookies)
        album_key = str(comment.get("target_key") or "")
        if name == "comment_delete":
            owner = str(comment.get("username") or "")
            me = state.username if hasattr(state, "username") else ""
            if not (me and owner == me) and not self.can_review(state):
                return self._html(self.forbidden(state, "只能删除自己的评论。"),
                                  status=403, cookies=cookies)
            if self.db_path is None:
                return self._html(self.forbidden(state, "本站当前不允许写入。"),
                                  status=503, cookies=cookies)
            from dddb.db import Store as _Store
            store = _Store(self.db_path)
            try:
                store.delete_comment(comment_id)
            finally:
                store.close()
            if state.session is not None:
                state.session.flash = "评论已删除"
            return Response.redirect("/album/" + T.quote_segment(album_key), status=303)

        # 举报: 记一条反馈 + 通知所有超级用户
        reason = text_field(values, "reason").strip()[:200]
        body = f"[评论举报 #{comment_id}] 专辑 {album_key} · 举报人 {state.username if hasattr(state, 'username') else ''}: {reason or '(未填写原因)'}"
        self.profile_call("add_feedback", uid, body, "comment-report", default=(False, ""))
        for admin in self.users.list_users():
            if str(admin.get("role") or "") == ROLE_SUPERUSER:
                self.profile_call("notify", int(admin.get("id") or 0), "report",
                                  body[:300], "/account", default=0)
        if state.session is not None:
            state.session.flash = "已收到举报, 管理员会尽快处理"
        return Response.redirect("/album/" + T.quote_segment(album_key), status=303)

    def _record_revision(self, state: auth.AuthState, key: str, summary: str) -> int:
        """把当前专辑快照记成一条修订(编辑者保存后调用)."""
        if self.db_path is None:
            return 0
        uid = self._member_uid(state)
        me = state.username if hasattr(state, "username") else ""
        try:
            from dddb.db import Store as _Store
            store = _Store(self.db_path)
            try:
                return store.add_revision(key, user_id=uid, username=me, summary=summary)
            finally:
                store.close()
        except Exception as exc:                   # noqa: BLE001 - 历史记录失败不该拖垮保存
            logger.warning("记录修订失败(%s): %s", key, exc)
            return 0

    def page_history(self, state: auth.AuthState, key: str,
                     params: Mapping[str, Any] | None = None) -> str:
        # 时间按**语言时区**显示(zh=UTC+8), 不依赖服务器时区
        _lang = self._current_lang()
        from dddb.db import Store as _Store
        album = self.data.get_album(key)
        if album is None:
            return T.error_page(self.context(state, active="albums"), status=404,
                                message=f"没有这张专辑: {key}")
        revisions: list[dict[str, Any]] = []
        selected: dict[str, Any] | None = None
        diff_rows: list[dict[str, Any]] = []
        # 注意: album 是**视图**(make_view 产物, urls 是列表 / credits 已分组 /
        # date 而非 release_date), 而修订快照是 Album 形状。直接比两者永远不等 ——
        # 用户 m13077 报"对比不上"就是这个原因。所以两边都走 Album.to_dict()。
        current: dict[str, Any] = {}
        if self.db_path is not None:
            store = _Store(self.db_path)
            try:
                revisions = store.list_revisions(key)
                rev_id = _first(params or {}, "rev")
                if rev_id:
                    selected = store.get_revision(rev_id)
                _alive = store.get_album(key)
                if _alive is not None:
                    current = _alive.to_dict()
            finally:
                store.close()
            if selected is not None:
                try:
                    from dddb.models import Album as _Album
                    snap = _Album.from_json(selected.get("payload") or "")
                    snap_dict = snap.to_dict() if hasattr(snap, "to_dict") else {}
                except Exception:                 # noqa: BLE001
                    snap_dict = {}
                for field in _HISTORY_DIFF_FIELDS:
                    before = snap_dict.get(field)
                    after = current.get(field)
                    if _history_value(field, before) != _history_value(field, after):
                        diff_rows.append({"field": field, "before": before, "after": after})
        for item in revisions:
            item["created_at_text"] = _stamp_text(item.get("created_at"), _lang)
        return T.history_page(self.context(state, active="albums"), album=album,
                              revisions=revisions, selected=selected, diff_rows=diff_rows)

    def _do_album_revert(self, values: Mapping[str, Any], state: auth.AuthState,
                         cookies: list[str]) -> Response:
        """POST /album/revert: 还原整版或单个字段(仅编辑者)."""
        uid = self._member_uid(state)
        me = state.username if hasattr(state, "username") else ""
        rev_id = text_field(values, "revision_id")
        key = text_field(values, "album_key")
        field = text_field(values, "field").strip()
        if self.db_path is None:
            return self._html(self.forbidden(state, "本站当前不允许写入。"),
                              status=503, cookies=cookies)
        from dddb.db import Store as _Store
        store = _Store(self.db_path)
        try:
            if field:
                ok, message = store.restore_field_from_revision(
                    rev_id, field, user_id=uid, username=me)
            else:
                ok, message = store.revert_to_revision(
                    rev_id, user_id=uid, username=me)
        finally:
            store.close()
        if not ok:
            return self._html(self.forbidden(state, message), status=400, cookies=cookies)
        if state.session is not None:
            state.session.flash = "已还原" + (f"字段 {field}" if field else "到该版本")
        return Response.redirect("/album/" + T.quote_segment(key) + "/history", status=303)

    def event_date_json(self, name: str) -> str:
        """``GET /event-date?name=M3-2026秋`` -> JSON 文本 ``{date, end, ok}``.

        编辑页选好展会后自动填发售日 —— 只读, 展会日期是公开信息。
        ``render_route`` 返回 ``(status, html)``, 所以这里直接给 JSON 字符串。

        参数 ``name`` 就是展会名(已由 ``_first(params, "name")`` 取出)。
        也兼容传了 ``?name=...`` 原始查询串的情况(便于直接调用测试)。
        """
        text = str(name or "").strip()
        if "=" in text:                       # 兼容 "name=M3-2026秋" 形式
            from urllib.parse import parse_qs
            query = parse_qs(text.split("?", 1)[-1])
            text = (query.get("name") or [""])[0].strip()
        name = text
        payload: dict[str, Any] = {"name": name, "date": "", "end": "",
                                   "ok": False}
        if name:
            try:
                from dddb import events as _events
                event = _events.parse_event(name)
                if event is not None:
                    start = str(getattr(event, "start_date", "") or "")
                    end = str(getattr(event, "end_date", "") or "")
                    payload.update({"date": start, "end": end,
                                    "ok": bool(start),
                                    "label": str(getattr(event, "name", "") or "")})
            except Exception as exc:            # noqa: BLE001
                logger.debug("展会日期解析失败 %r: %s", name, exc)
        return json.dumps(payload, ensure_ascii=False)

    def _do_account_layout(self, values: Mapping[str, Any],
                           state: auth.AuthState,
                           cookies: list[str]) -> Response:
        """``POST /account/layout``: 保存编辑界面卡片大小/顺序偏好.

        存进 ``user_prefs`` 表(key = ``edit.layout``), 值是 JSON 字符串。
        这里只做粗校验(合法 JSON + 长度限制) —— 布局是纯展示, 坏了不影响数据。
        """
        if state is None or state.user is None:
            return self._html(self.forbidden(state, "请先登录。"),
                              status=403, cookies=cookies)
        if not self.can_edit(state):
            return self._html(
                self.forbidden(state, "只有编辑者才能调整编辑界面布局。"),
                status=403, cookies=cookies)
        uid = int(state.user.get("id") or 0)
        payload = str(text_field(values, "layout") or "").strip()
        if not payload:
            return self._html(self.forbidden(state, "布局内容为空。"),
                              status=400, cookies=cookies)
        if len(payload) > 4000:
            return self._html(self.forbidden(state, "布局内容过长。"),
                              status=400, cookies=cookies)
        try:
            parsed = json.loads(payload)
        except ValueError:
            return self._html(self.forbidden(state, "布局内容不是合法 JSON。"),
                              status=400, cookies=cookies)
        if not isinstance(parsed, dict):
            return self._html(self.forbidden(state, "布局内容格式不对。"),
                              status=400, cookies=cookies)
        ok = bool(self.profile_call(
            "set_pref", uid, "edit.layout",
            json.dumps(parsed, ensure_ascii=False, sort_keys=True),
            default=False))
        if not ok:
            return self._html(self.forbidden(state, "保存布局失败。"),
                              status=500, cookies=cookies)
        return self._html(self.account_page(state, notice="布局偏好已保存。"),
                          cookies=cookies)

    def _save_album_cover_file(self, key: str,
                               values: Mapping[str, Any]) -> bool:
        """新建专辑时保存随表单上传的封面(``name="cover"``).

        ``values`` 里已经由 ``_values_from_multipart`` 拆出了文件字节 ——
        形如 ``{"cover": {"filename": ..., "content_type": ..., "data": b"..."}}``。
        失败返回 False(只记日志): 封面存不下来不该让整张专辑创建失败。
        """
        if not key:
            return False
        covers_dir = getattr(self.data, "covers_dir", None)
        if not covers_dir:
            return False
        raw = values.get("cover")
        if not isinstance(raw, Mapping):
            return False
        blob = raw.get("data") or b""
        if not blob:
            return False
        mime = str(raw.get("content_type") or "").split(";")[0].strip().lower()
        ext = {"image/png": ".png", "image/jpeg": ".jpg",
               "image/webp": ".webp"}.get(mime)
        if ext is None:
            logger.warning("新建专辑 %s: 封面格式不支持(%s), 已跳过", key, mime)
            return False
        if len(blob) > MAX_IMAGE_BODY:
            logger.warning("新建专辑 %s: 封面超过 2MB, 已跳过", key)
            return False
        stem = _cover_stem(key)
        target = Path(covers_dir) / f"{stem}.img"
        try:
            Path(covers_dir).mkdir(parents=True, exist_ok=True)
            (Path(covers_dir) / "upload").mkdir(parents=True, exist_ok=True)
            target.write_bytes(blob)
            (Path(covers_dir) / "upload" / f"{stem}{ext}").write_bytes(blob)
        except OSError as exc:                     # pragma: no cover
            logger.warning("新建专辑 %s: 封面落盘失败: %s", key, exc)
            return False
        if self.db_path is None:
            return True
        from dddb.db import Store as _Store

        import time as _time

        store = _Store(self.db_path)
        try:
            ok, message = store.update_album_fields(key, {
                "cover_local": str(target),
                "cover_v": str(int(_time.time())),
            })
        finally:
            store.close()
        if not ok:
            logger.warning("新建专辑 %s: 写 cover_local 失败: %s", key, message)
            return False
        try:
            self.data.invalidate()
        except Exception:                # noqa: BLE001
            pass
        return True

    def serve_photo(self, arg: str) -> Response:
        """``/photo/<album_key>/<name>``: 发附图文件."""
        from urllib.parse import unquote

        raw = unquote(str(arg or ""))
        if "/" not in raw:
            return self._plain_404()
        album_key, _, name = raw.rpartition("/")
        if not album_key or not name or "/" in name or ".." in name:
            return self._plain_404()
        covers = getattr(self.data, "covers_dir", None)
        if not covers:
            return self._plain_404()
        from .photos import photo_dir

        folder = photo_dir(covers, album_key)
        target = (folder / name).resolve()
        try:
            target.relative_to(folder.resolve())
        except ValueError:
            return self._plain_404()
        if not target.is_file():
            return self._plain_404()
        try:
            blob = target.read_bytes()
        except OSError:
            return self._plain_404()
        resp = Response.binary(blob, sniff_image_type(blob) or "image/jpeg",
                               status=200)
        resp.headers["Cache-Control"] = "public, max-age=3600"
        return resp

    def _do_album_photo(self, key: str, state: auth.AuthState, body: bytes,
                        headers: Mapping[str, Any], cookies: list[str],
                        values: Mapping[str, Any] | None = None) -> Response:
        """POST /album/<key>/photo: 上传一张**其他照片**(不参与封面)."""
        import time as _t
        import uuid

        from .photos import MAX_PHOTO_BYTES, add_photo, photo_dir

        key = str(key or "").strip().rstrip("/")
        if not key or self.db_path is None:
            return self._plain_404()
        if not self.can_edit(state):
            return self._html(self.forbidden(state, "只有编辑者才能上传照片。"),
                              status=403, cookies=cookies)
        album = self.data.get_album(key)
        if album is None:
            return self._html(T.error_page(self.context(state), status=404,
                                           message="没有这张专辑: " + key),
                              status=404, cookies=cookies)

        upload = parse_upload(headers, body, "photo")
        if upload is None:
            return self._html(self.forbidden(state, "没有收到图片。"),
                              status=400, cookies=cookies)
        blob, ctype = upload
        mime = ctype.split(";")[0].strip().lower()
        ext = {"image/png": ".png", "image/jpeg": ".jpg",
               "image/webp": ".webp"}.get(mime, ".jpg")
        if len(blob) > MAX_PHOTO_BYTES:
            return self._html(self.forbidden(state, "照片不能超过 4MB。"),
                              status=400, cookies=cookies)

        covers = getattr(self.data, "covers_dir", None)
        if not covers:
            return self._html(self.forbidden(state, "封面目录未配置。"),
                              status=503, cookies=cookies)
        folder = photo_dir(covers, key)
        try:
            folder.mkdir(parents=True, exist_ok=True)
            name = uuid.uuid4().hex[:16] + ext
            (folder / name).write_bytes(blob)
        except OSError as exc:
            return self._html(self.forbidden(state, "保存失败: " + str(exc)),
                              status=500, cookies=cookies)

        url = "/photo/{}/{}".format(T.quote_segment(key), name)
        note = text_field(values, "note")
        from dddb.db import Store as _Store

        store = _Store(self.db_path)
        try:
            row = store.query_one(
                "SELECT payload FROM albums WHERE key = ?", (key,))
            payload = str(row["payload"] or "{}") if row else "{}"
            new_payload, ok = add_photo(payload, url=url,
                                        path=str(folder / name), note=note)
            if not ok:
                try:
                    (folder / name).unlink()
                except OSError:
                    pass
                return self._html(
                    self.forbidden(state, "照片已达上限或重复。"),
                    status=400, cookies=cookies)
            store.execute("UPDATE albums SET payload = ? WHERE key = ?",
                          (json.dumps(new_payload, ensure_ascii=False), key))
        finally:
            store.close()
        try:
            self.data.invalidate()
        except Exception:                     # noqa: BLE001
            pass
        return Response.redirect(
            "/album/{}/edit?t={}".format(T.quote_segment(key),
                                         int(_t.time())), status=303)

    def _do_album_photo_del(self, key: str, state: auth.AuthState,
                            values: Mapping[str, Any],
                            cookies: list[str]) -> Response:
        """删掉一张附图(文件也删)."""
        from pathlib import Path as _P

        from dddb.db import Store as _Store

        from .photos import remove_photo

        key = str(key or "").strip().rstrip("/")
        url = text_field(values, "url").strip()
        if not key or not url or self.db_path is None:
            return self._plain_404()
        if not self.can_edit(state):
            return self._html(self.forbidden(state, "只有编辑者才能删除照片。"),
                              status=403, cookies=cookies)
        removed = ""
        store = _Store(self.db_path)
        try:
            row = store.query_one(
                "SELECT payload FROM albums WHERE key = ?", (key,))
            if row is None:
                return self._plain_404()
            new_payload, removed = remove_photo(row["payload"], url)
            store.execute("UPDATE albums SET payload = ? WHERE key = ?",
                          (json.dumps(new_payload, ensure_ascii=False), key))
        finally:
            store.close()
        if removed:
            try:
                _P(removed).unlink(missing_ok=True)
            except OSError:
                pass
        try:
            self.data.invalidate()
        except Exception:                     # noqa: BLE001
            pass
        return self._json({"ok": True})

    def _do_album_photo_note(self, key: str, state: auth.AuthState,
                             values: Mapping[str, Any],
                             cookies: list[str]) -> Response:
        """改某张附图的备注."""
        from dddb.db import Store as _Store

        from .photos import set_photo_note

        key = str(key or "").strip().rstrip("/")
        url = text_field(values, "url").strip()
        note = text_field(values, "note")
        if not key or not url or self.db_path is None:
            return self._plain_404()
        if not self.can_edit(state):
            return self._html(self.forbidden(state, "只有编辑者才能改备注。"),
                              status=403, cookies=cookies)
        hit = False
        store = _Store(self.db_path)
        try:
            row = store.query_one(
                "SELECT payload FROM albums WHERE key = ?", (key,))
            if row is None:
                return self._plain_404()
            new_payload, hit = set_photo_note(row["payload"], url, note)
            if hit:
                store.execute("UPDATE albums SET payload = ? WHERE key = ?",
                              (json.dumps(new_payload, ensure_ascii=False),
                               key))
        finally:
            store.close()
        try:
            self.data.invalidate()
        except Exception:                     # noqa: BLE001
            pass
        return self._json({"ok": bool(hit)})

    def _do_album_cover(self, key: str, state: auth.AuthState, body: bytes,
                        headers: Mapping[str, Any], cookies: list[str],
                        values: Mapping[str, Any] | None = None) -> Response:
        """POST /album/<key>/cover: 编辑者上传封面 -> covers/upload/<key>.<ext> + cover 贡献."""
        album = self.data.get_album(key) if key else None
        if album is None:
            return self._html(T.error_page(self.context(state), status=404,
                                           message=f"没有这张专辑: {key}"),
                              status=404, cookies=cookies)
        if not self.can_edit(state):
            return self._html(self.forbidden(state, "只有编辑者才能上传封面。"),
                              status=403, cookies=cookies)
        if self.db_path is None:
            return self._html(self.forbidden(state, "本站当前不允许写入。"),
                              status=503, cookies=cookies)
        covers_dir = getattr(self.data, "covers_dir", None)
        if not covers_dir:
            return self._html(self.forbidden(state, "封面目录未配置。"),
                              status=503, cookies=cookies)
        upload = parse_upload(headers, body, "cover")
        if upload is None:
            return self._html(self.forbidden(state, "没有收到图片文件。"),
                              status=400, cookies=cookies)
        blob, ctype = upload
        mime = ctype.split(";")[0].strip().lower()
        ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}.get(mime)
        if ext is None:
            return self._html(self.forbidden(state, "只支持 PNG / JPEG / WebP。"),
                              status=400, cookies=cookies)
        if len(blob) > MAX_IMAGE_BODY:
            return self._html(self.forbidden(state, "图片不能超过 2MB。"),
                              status=400, cookies=cookies)
        # 写入标准缓存路径 ``covers/<key>.img``(封面服务认这个位置),
        # 内部会写 cover_local + cover_v 并清掉两层缓存 —— 与
        # 「按 URL 抓封面」共用同一份实现, 免得两处行为走偏。
        self._record_revision(state, str(key), "更新封面前快照")
        ok, note = self._store_cover_bytes(str(key), blob, mime, ext)
        if not ok:
            return self._html(self.forbidden(state, note),
                              status=500, cookies=cookies)
        self.profile_call("add_contribution", self._member_uid(state), "cover", str(key),
                          str(album.get("title") or ""), default=0)
        self._record_revision(state, str(key), "更新封面")
        if state.session is not None:
            state.session.flash = "封面已更新"
        # 跳回表单里指定的 next(编辑页), 而不是专辑详情页 ——
        # 用户是在编辑页点上传的, 突然跳到详情页会以为"被直接提交了"。
        next_url = str(text_field(values, "next") or "").strip()
        if not next_url.startswith("/") or next_url.startswith("//"):
            next_url = "/album/" + T.quote_segment(str(key)) + "/edit"
        return Response.redirect(next_url, status=303)

    def _store_cover_bytes(self, key: str, blob: bytes, mime: str,
                           ext: str) -> tuple[bool, str]:
        """把一坨图片字节存成专辑封面(落盘 + 写库 + 清缓存) -> ``(ok, 说明)``.

        与 :meth:`_do_album_cover` 的写盘部分同一套行为: 先写标准缓存路径
        ``covers/<sha1(key)[:16]>.img``(封面服务认这个位置), 再在 ``upload/``
        留一份带扩展名的方便人工排查, 最后写 ``cover_local`` + ``cover_v``。
        """
        covers_dir = getattr(self.data, "covers_dir", None)
        if not covers_dir:
            return False, "封面目录未配置。"
        if self.db_path is None:
            return False, "本站当前不允许写入。"
        stem = _cover_stem(key)
        try:
            (Path(covers_dir) / "upload").mkdir(parents=True, exist_ok=True)
            target = Path(covers_dir) / f"{stem}.img"
            target.write_bytes(blob)
            (Path(covers_dir) / "upload" / f"{stem}{ext}").write_bytes(blob)
        except OSError as exc:                     # pragma: no cover - 磁盘故障
            return False, f"保存失败: {exc}"
        from dddb.db import Store as _Store
        store = _Store(self.db_path)
        try:
            import time as _time

            ok, message = store.update_album_fields(key, {
                "cover_local": str(target),
                "cover_v": str(int(_time.time())),
            })
        finally:
            store.close()
        if not ok:
            return False, f"写入专辑失败: {message}"
        try:
            self.data.invalidate()
        except Exception:                # noqa: BLE001
            pass
        try:
            self.drop_cover_cache(target)
        except Exception:                # noqa: BLE001
            pass
        return True, str(target)

    def _store_cover_from_data(self, key: str,
                               values: Mapping[str, Any]) -> bool:
        """把草稿带回来的 ``cover_data`` (data URL) 存成封面; 尽力而为.

        用户在新专辑页选过封面图, 之后刷新过页面 —— ``<input type="file">``
        的值恢复不回来, 只有草稿里那份 base64 还在。前端把它放在
        ``name="cover_data"`` 的隐藏域里随表单提交, 这里解出来落盘。
        """
        raw = str(text_field(values, "cover_data") or "").strip()
        if not raw or not raw.startswith("data:") or "," not in raw:
            return False
        header, _, payload = raw.partition(",")
        mime = header[5:].split(";")[0].strip().lower()
        if mime not in _URL_COVER_TYPES:
            logger.warning("草稿封面格式不支持(%s): %s", key, mime)
            return False
        try:
            blob = base64.b64decode(payload, validate=False)
        except (ValueError, TypeError):
            return False
        if not blob or len(blob) > MAX_URL_COVER_BYTES:
            return False
        # 用真实字节头复核一遍 —— 前半段 mime 只是浏览器自报
        sniffed = sniff_image_type(blob)
        if sniffed not in _URL_COVER_TYPES:
            return False
        ok, note = self._store_cover_bytes(key, blob, sniffed,
                                          _URL_COVER_TYPES[sniffed])
        if not ok:
            logger.warning("草稿封面落盘失败(%s): %s", key, note)
        return ok

    def _store_cover_from_url(self, key: str, url: str) -> bool:
        """抓 ``url`` 存成封面; **尽力而为**, 失败只记日志返回 False.

        新建专辑时用: 封面抓不下来不该让「新建」失败(整张碟的信息都在了)。
        """
        try:
            blob, mime = fetch_image_from_url(url)
        except CoverFetchError as exc:
            logger.warning("按 URL 抓封面失败(%s): %s", key, exc)
            return False
        ok, note = self._store_cover_bytes(key, blob, mime,
                                          _URL_COVER_TYPES.get(mime, ".jpg"))
        if not ok:
            logger.warning("按 URL 抓封面落盘失败(%s): %s", key, note)
        return ok

    def _do_album_cover_url(self, key: str, state: auth.AuthState,
                            values: Mapping[str, Any],
                            cookies: list[str]) -> Response:
        """POST /album/<key>/cover-url: 给个图片地址, 服务端抓下来当封面.

        返回 JSON(前端 ``fetch`` 用), 成功 ``{"ok": true, "cover_v": "..."}``。
        """
        album = self.data.get_album(key) if key else None
        if album is None:
            return self._json({"ok": False, "error": f"没有这张专辑: {key}"},
                              status=404, cookies=cookies)
        if not self.can_edit(state):
            return self._json({"ok": False, "error": "只有编辑者才能改封面。"},
                              status=403, cookies=cookies)
        if self.db_path is None:
            return self._json({"ok": False, "error": "本站当前不允许写入。"},
                              status=503, cookies=cookies)
        url = text_field(values, "url").strip()
        if not url:
            return self._json({"ok": False, "error": "没有收到图片地址。"},
                              status=400, cookies=cookies)
        try:
            blob, mime = fetch_image_from_url(url)
        except CoverFetchError as exc:
            return self._json({"ok": False, "error": str(exc)},
                              status=400, cookies=cookies)
        ext = _URL_COVER_TYPES.get(mime)
        if ext is None:
            return self._json({"ok": False, "error": "只支持 PNG / JPEG / WebP。"},
                              status=400, cookies=cookies)
        self._record_revision(state, str(key), "换封面前快照")
        ok, note = self._store_cover_bytes(key, blob, mime, ext)
        if not ok:
            return self._json({"ok": False, "error": note},
                              status=500, cookies=cookies)
        self.profile_call("add_contribution", self._member_uid(state), "cover",
                          str(key), str(album.get("title") or ""), default=0)
        self._record_revision(state, str(key), "按链接换封面")
        if state.session is not None:
            state.session.flash = "封面已更新"
        current = self.data.get_album(key) or album
        # 视图里没有 cover_v 字段, 只有带版本号的封面地址(/cover/<key>?v=NNN)
        cover_href = str(current.get("cover") or "")
        return self._json({
            "ok": True,
            "cover_v": cover_href.split("?v=")[-1] if "?v=" in cover_href else "",
            "message": "封面已更新",
        }, cookies=cookies)

    def _do_account_security(self, name: str, values: Mapping[str, Any],
                             state: auth.AuthState, cookies: list[str]) -> Response:
        """改密码 / 注销 / 发送安全验证码 —— 都要求邮箱验证, 且会话环境已确认."""
        uid = self._member_uid(state)
        user = self.users.get_by_id(uid) or {}
        username = str(user.get("username") or "")
        if not username:
            return self._html(self.forbidden(state, "请先登录。"), status=403, cookies=cookies)
        if state.session is not None and not state.session.confirmed:
            return self._html(self.forbidden(
                state, "当前登录环境尚未确认, 请先点击确认邮件里的链接。"),
                status=403, cookies=cookies)

        if name == "account_security_code":
            purpose = text_field(values, "purpose").strip() or "password_change"
            if purpose not in ("password_change", "account_delete"):
                purpose = "password_change"
            ok, message = V.send_email_code(
                self.users, username, mailer=self.mailer, environ=self.environ,
                purpose=purpose)
            if state.session is not None:
                state.session.flash = message if not ok else "验证码已发送到你的邮箱"
            return Response.redirect("/account?tab=security", status=303)

        def bad(message: str) -> Response:
            return self._html(self.page_account(
                state, user, error=message, tab="security"), status=400, cookies=cookies)

        if name == "account_password":
            code = text_field(values, "code")
            old = text_field(values, "old_password")
            new = text_field(values, "new_password")
            again = text_field(values, "new_password2")
            if new != again:
                return bad("两次输入的新密码不一致")
            result = self.users.authenticate(username, old)
            if not result.ok:
                return bad("当前密码不正确")
            ok, message = V.check_email_code(self.users, username, code,
                                             purpose="password_change")
            if not ok:
                return bad(message)
            ok, message = self.users.set_password(username, new)
            if not ok:
                return bad(message)
            # 改密码后其它设备的会话全部失效
            self.sessions.drop_user(int(user.get("id") or 0))
            current = self.sessions.create(user)
            response = Response.redirect("/account?tab=security", status=303)
            response.cookies.append(auth.set_cookie(
                auth.SESSION_COOKIE, current.sid, max_age=auth.SESSION_TTL))
            return response

        # account_delete
        code = text_field(values, "code")
        if text_field(values, "confirm").strip() != "注销账号":
            return bad("请按提示输入“注销账号”以确认")
        ok, message = V.check_email_code(self.users, username, code,
                                         purpose="account_delete")
        if not ok:
            return bad(message)
        self.sessions.drop_user(int(user.get("id") or 0))
        ok, message = self.users.remove(username)
        response = Response.redirect("/", status=303)
        response.cookies.append(auth.set_cookie(auth.SESSION_COOKIE, "", max_age=0))
        return response


    LANG_COOKIE = "dddb_lang"

    def _do_lang(self, code: str, params: Mapping[str, Any],
                 cookies: list[str],
                 headers: Mapping[str, Any] | None = None) -> Response:
        """切换界面语言: 写 Cookie 后回跳(优先 next, 其次 Referer 的同源路径)."""
        lang = I18N.language_of(code)
        target = _first(params, "next")
        if not target.startswith("/"):
            referer = str((headers or {}).get("referer") or "")
            parsed = urllib.parse.urlsplit(referer) if referer else None
            if parsed is not None and parsed.path.startswith("/"):
                target = parsed.path + (("?" + parsed.query) if parsed.query else "")
            else:
                target = "/"
        response = Response.redirect(target, status=303)
        response.cookies.append(f"{self.LANG_COOKIE}={lang}; Path=/; Max-Age=31536000;"
                                " SameSite=Lax; HttpOnly")
        return response

    def _do_comment_like(self, arg: Any, values: Mapping[str, Any],
                         state: auth.AuthState, cookies: list[str]) -> Response:
        """POST /comment/<id>/like: 点赞/取消点赞(登录用户, 不能给自己点赞)."""
        target = text_field(values, "next").strip() or "/"
        if not target.startswith("/"):
            target = "/"
        if not state.logged_in or state.session is None:
            return Response.redirect("/login", status=303)
        if self.db_path is None:
            return self._html(self.forbidden(state, "本站当前不允许写入。"),
                              status=503, cookies=cookies)
        from dddb.db import Store as _Store
        store = _Store(self.db_path)
        try:
            ok, _liked, _count, message = store.toggle_comment_like(
                arg, state.session.username)
        finally:
            store.close()
        if not ok:
            status = 403 if "不能给自己" in message else 400
            return self._html(self.forbidden(state, message), status=status, cookies=cookies)
        return Response.redirect(target, status=303)

    def _do_album_delete(self, key: str, values: Mapping[str, Any],
                         state: auth.AuthState, cookies: list[str]) -> Response:
        """删除专辑(仅编辑者): 连同关联表/评论/修订一起清掉."""
        if not self.can_edit(state):
            return self._html(self.forbidden(state, "只有编辑者可以删除专辑。"),
                              status=403, cookies=cookies)
        if self.db_path is None:
            return self._html(self.forbidden(state, "本站当前不允许写入。"),
                              status=503, cookies=cookies)
        from dddb.db import Store as _Store
        store = _Store(self.db_path)
        try:
            album = store.get_album(key)
            if album is None:
                return self._html(self.error_page(
                    self.context(state), status=404,
                    message=f"没有这张专辑: {key}"), status=404, cookies=cookies)
            for table in ("album_artists", "album_circles", "album_events",
                          "album_tag_ids"):
                try:
                    store.execute(f"DELETE FROM {table} WHERE album_key = ?", (key,))
                except Exception:          # noqa: BLE001
                    pass
            for table, column in (("comments", "target_key"),
                                  ("album_revisions", "album_key")):
                try:
                    store.execute(f"DELETE FROM {table} WHERE {column} = ?", (key,))
                except Exception:          # noqa: BLE001
                    pass
            store.execute("DELETE FROM albums WHERE key = ?", (key,))
        finally:
            store.close()
        if hasattr(self.data, "invalidate"):
            self.data.invalidate()
        if state.session is not None:
            state.session.flash = "专辑已删除"
        return Response.redirect("/albums", status=303)

    # ------------------------------------------------------------------
    # infobox 封面: 先缓存, 提交时转正(用户 m13899)
    # ------------------------------------------------------------------
    def _pending_cover_dir(self) -> Path | None:
        """缓存目录 ``<covers>/pending``. 拿不到封面目录时返回 None."""
        covers = getattr(self.data, "covers_dir", None)
        if covers:
            base = Path(covers)
        elif self.db_path is not None:
            base = Path(self.db_path).parent / "covers"
        else:
            return None
        return base / "pending"

    def _purge_pending_covers(self, folder: Path) -> None:
        """顺手清掉过期的缓存图(不单独起定时任务)."""
        try:
            now = time.time()
            for path in folder.iterdir():
                if not path.is_file():
                    continue
                try:
                    if now - path.stat().st_mtime > PENDING_COVER_TTL:
                        path.unlink()
                except OSError:
                    continue
        except OSError:
            return

    def _pending_cover_path(self, token: str) -> Path | None:
        """token -> 缓存文件路径(只认 16 位十六进制, 杜绝路径穿越)."""
        clean = str(token or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{16}", clean):
            return None
        folder = self._pending_cover_dir()
        if folder is None:
            return None
        for suffix in (".png", ".jpg", ".webp"):
            path = folder / (clean + suffix)
            if path.is_file():
                return path
        return None

    def _drop_pending_cover(self, values: Mapping[str, Any]) -> None:
        """把这次提交对应的缓存图删掉(用户已经自己传了图)."""
        token = text_field(values, "cover_cache").strip()
        path = self._pending_cover_path(token) if token else None
        if path is not None:
            try:
                path.unlink()
            except OSError:
                pass

    def _promote_cached_cover(self, key: str, values: Mapping[str, Any]) -> bool:
        """提交时把缓存的封面转成这张专辑的正式封面."""
        token = text_field(values, "cover_cache").strip()
        path = self._pending_cover_path(token) if token else None
        if path is None:
            return False
        try:
            blob = path.read_bytes()
        except OSError:
            return False
        if not blob:
            return False
        mime = sniff_image_type(blob) or "image/png"
        ext = {"image/png": ".png", "image/jpeg": ".jpg",
               "image/webp": ".webp"}.get(mime, "")
        if not ext:
            return False
        ok, _note = self._store_cover_bytes(str(key), blob, mime, ext)
        if ok:
            try:
                path.unlink()
            except OSError:
                pass
        return ok

    def _apply_pending_cover(self, key: str, values: Mapping[str, Any],
                             headers: Mapping[str, Any] | None,
                             body: bytes) -> bool:
        """提交表单时处理 infobox 缓存图(用户 m13899).

        * 用户这次**自己传了**封面文件 -> 缓存作废(删文件, 改成保存用户提交的);
        * 用户没传 -> 把缓存转成正式封面。
        返回是否真的写入了封面。
        """
        if not text_field(values, "cover_cache").strip():
            return False
        if headers is not None and body and parse_upload(headers, body, "cover") is not None:
            self._drop_pending_cover(values)
            return False
        return self._promote_cached_cover(key, values)

    def serve_pending_cover(self, token: str) -> Response:
        """``GET /cover-cache/<token>``: 只给编辑页预览用, 不缓存(no-store)."""
        path = self._pending_cover_path(token)
        if path is None:
            return self._plain_404()
        try:
            blob = path.read_bytes()
        except OSError:
            return self._plain_404()
        if not blob:
            return self._plain_404()
        ctype = sniff_image_type(blob) or "image/png"
        resp = Response.binary(blob, ctype, status=200)
        # 缓存图随时会被删/替换, 绝不能让浏览器长期留着
        resp.headers["Cache-Control"] = "no-store"
        return resp

    def _do_pending_cover(self, arg: str, values: Mapping[str, Any],
                          state: auth.AuthState, cookies: list[str]) -> Response:
        """``POST /cover-cache/new``: 把 infobox 里的封面地址抓下来缓存.

        为什么不在填表时就写库(用户 m13899): 编辑者可能只是粘了一段 infobox
        试试, 也可能觉得这张图不够清楚要换 —— 那就得留个"撤销"的余地。
        所以这里只落到 ``covers/pending/<token>.<ext>``, 页面立刻能预览,
        **真正的封面等提交表单时才由 :meth:`_promote_cached_cover` 写入**。
        """
        if not self.can_edit(state):
            return self._json({"ok": False, "error": "只有编辑者可以抓取封面"},
                              status=403, cookies=cookies)
        if str(arg or "").strip().lower() != "new":
            return self._json({"ok": False, "error": "未知操作"},
                              status=404, cookies=cookies)
        folder = self._pending_cover_dir()
        if folder is None:
            return self._json({"ok": False, "error": "本站没有可写的图片目录"},
                              status=503, cookies=cookies)
        url = text_field(values, "url").strip()
        if not url:
            return self._json({"ok": False, "error": "没有收到图片地址"},
                              status=400, cookies=cookies)
        try:
            blob, mime = fetch_image_from_url(url)
        except CoverFetchError as exc:
            return self._json({"ok": False, "error": str(exc)},
                              status=400, cookies=cookies)
        sniffed = sniff_image_type(blob) or ""
        if sniffed:
            mime = sniffed
        ext = {"image/png": ".png", "image/jpeg": ".jpg",
               "image/webp": ".webp"}.get(str(mime or ""), "")
        if not ext:
            return self._json({"ok": False, "error": "对方返回的不是 PNG/JPEG/WebP"},
                              status=400, cookies=cookies)
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            return self._json({"ok": False, "error": "缓存目录不可写"},
                              status=503, cookies=cookies)
        self._purge_pending_covers(folder)
        token = secrets.token_hex(8)
        try:
            (folder / (token + ext)).write_bytes(blob)
        except OSError:
            return self._json({"ok": False, "error": "写缓存文件失败"},
                              status=503, cookies=cookies)
        size = probe_image_size(blob)
        width, height = (int(size[0]), int(size[1])) if size else (0, 0)
        small = bool(width and height and max(width, height) < PENDING_COVER_SMALL)
        return self._json({
            "ok": True, "token": token, "url": "/cover-cache/" + token,
            "width": width, "height": height, "small": small,
        }, cookies=cookies)

    def _do_album_merge(self, key: str, values: Mapping[str, Any],
                        state: auth.AuthState, cookies: list[str]) -> Response:
        """把这张专辑并进另一张(仅编辑者).

        ``POST /album/<key>/merge`` + 表单字段 ``target``: 既接受另一种写法
        的 **key**, 也接受整条 ``/album/<key>`` / ``/album/<key>/edit`` 地址
        (用户多半是从地址栏直接粘过来的)。

        合并方向: **保留 target, 删掉当前 key** —— 与目标页的「合并进来」语义
        一致, 也符合用户在编辑页上的直觉(我在草稿条目上点合并, 留下的应该是
        正式那一条)。底层用 ``Store.merge_albums(keep_key=target, drop_key=key)``。
        """
        if not self.can_edit(state):
            return self._html(self.forbidden(state, "只有编辑者可以合并专辑。"),
                              status=403, cookies=cookies)
        if self.db_path is None:
            return self._html(self.forbidden(state, "本站当前不允许写入。"),
                              status=503, cookies=cookies)
        raw = text_field(values, "target").strip()
        target = self._album_key_from_input(raw)
        # 只合并用户勾选的部分(用户 m13899); 一个都没勾时按"什么都没选"处理,
        # 而不是偷偷全量合并。
        parts = [str(one).strip() for one in many_fields(values, "part")]
        parts = [one for one in parts if one]
        if not parts:
            if state.session is not None:
                state.session.flash = "请至少勾选一项要合并的内容"
            return Response.redirect(
                "/album/" + T.quote_segment(key) + "/edit", status=303)
        if not target:
            if state.session is not None:
                state.session.flash = "请填写要合并到的专辑(编号或 /album/... 地址)"
            return Response.redirect(
                "/album/" + T.quote_segment(key) + "/edit", status=303)
        if target == key:
            if state.session is not None:
                state.session.flash = "不能把专辑合并到自己"
            return Response.redirect(
                "/album/" + T.quote_segment(key) + "/edit", status=303)
        merged = False
        from dddb.db import Store as _Store
        store = _Store(self.db_path)
        try:
            if store.get_album(key) is None:
                return self._html(self.error_page(
                    self.context(state), status=404,
                    message=f"没有这张专辑: {key}"), status=404, cookies=cookies)
            if store.get_album(target) is None:
                if state.session is not None:
                    state.session.flash = f"没有要合并到的那张专辑: {target}"
                return Response.redirect(
                    "/album/" + T.quote_segment(key) + "/edit", status=303)
            merged = store.merge_albums(keep_key=target, drop_key=key,
                                        parts=parts)
            if merged:
                # 与删除一致: 关联表/评论/修订里属于**被并条目**的痕迹清掉,
                # 避免 /album/<key> 还能查到已经不存在的那张。
                for table in ("album_artists", "album_circles", "album_events",
                              "album_tag_ids"):
                    try:
                        store.execute(
                            f"DELETE FROM {table} WHERE album_key = ?", (key,))
                    except Exception:      # noqa: BLE001
                        pass
                for table, column in (("comments", "target_key"),
                                      ("album_revisions", "album_key")):
                    try:
                        store.execute(
                            f"DELETE FROM {table} WHERE {column} = ?", (key,))
                    except Exception:      # noqa: BLE001
                        pass
        finally:
            store.close()
        if hasattr(self.data, "invalidate"):
            self.data.invalidate()
        if state.session is not None:
            state.session.flash = (
                "已合并到 " + target + "（" + "、".join(parts) + "）"
            ) if merged else "合并失败"
        return Response.redirect("/album/" + T.quote_segment(target), status=303)

    @staticmethod
    def _album_key_from_input(raw: str) -> str:
        """把「合并到」输入框的内容变成专辑 key.

        接受 ``adore|nika-lenz---kaza|2026``、``/album/<key>``、
        ``https://www.digjindb.top/album/<key>/edit``。其它情况返回空串。
        """
        text = str(raw or "").strip()
        if not text:
            return ""
        if "/album/" in text or text.startswith("album/"):
            tail = text.split("/album/", 1)[-1] if "/album/" in text else text[6:]
            tail = tail.split("?", 1)[0].split("#", 1)[0]
            segments = [seg for seg in tail.split("/") if seg]
            if len(segments) >= 2 and segments[-1].lower() in ("edit", "merge"):
                segments = segments[:-1]
            return urllib.parse.unquote("/".join(segments)).strip()
        return urllib.parse.unquote(text).strip()

    def _album_key_from_entity_id(self, raw: str) -> str:
        """``#12`` / ``albumId:12`` -> 那张专辑的 key; 查不到返回空串.

        用户 m18302: 搜索框只认这两种**明确写法**, 纯数字不再当编号
        (否则搜 "2000" 会跳进无关专辑)。编号只在搜索一条都没命中时
        才当兜底试一次, 走的是这里。
        """
        text = str(raw or "").strip()
        if not text:
            return ""
        if text.startswith("#"):
            digits = text[1:].strip()
        else:
            head, _, tail = text.partition(":")
            if head.strip().lower() != "albumid":
                return ""
            digits = tail.strip()
        if not digits.isdigit():
            return ""
        number = int(digits)
        if number <= 0:
            return ""
        from dddb.db import Store as _Store
        store = _Store(self.db_path)
        try:
            entity_id, album_key = store.resolve_entity("album", str(number))
            if not entity_id or not album_key:
                return ""
            # entities.name 存的就是专辑 key; 再确认一下这张碟真的还在库里
            return album_key if store.get_album(album_key) is not None else ""
        finally:
            store.close()

    def _do_album_comment(self, values: Mapping[str, Any], state: auth.AuthState,
                          cookies: list[str]) -> Response:
        """POST /album/<key>/comment: 任何登录用户都能评论(内容有服务端校验)."""
        key = text_field(values, "album_key").strip()
        uid = self._member_uid(state)
        album = self.data.get_album(key) if key else None
        if album is None:
            return self._html(T.error_page(self.context(state), status=404,
                                           message=f"没有这张专辑: {key}"),
                              status=404, cookies=cookies)
        if self.db_path is None:
            return self._html(self.forbidden(state, "本站当前不允许写入评论。"),
                              status=503, cookies=cookies)
        from dddb.db import Store as _Store
        store = _Store(self.db_path)
        try:
            ok, message, _cid = store.add_comment(
                target_kind="album", target_key=key, user_id=uid,
                username=state.username if hasattr(state, "username") else "",
                body=text_field(values, "body"),
            )
        finally:
            store.close()
        if not ok:
            return self._html(self.forbidden(state, message), status=400, cookies=cookies)
        if state.session is not None:
            state.session.flash = "评论已发表"
        return Response.redirect("/album/" + T.quote_segment(key), status=303)

    def page_messages(self, state: auth.AuthState, params: Mapping[str, Any] | None = None) -> str:
        # 时间按**语言时区**显示(zh=UTC+8), 不依赖服务器时区
        _lang = self._current_lang()
        uid = self._member_uid(state)
        peer_name = _first(params or {}, "peer")
        if peer_name:
            peer = self.users.get(peer_name)
            if peer is None:
                return T.error_page(self.context(state, active="messages"), status=404,
                                    message=f"没有这个用户: {peer_name}")
            thread = list(self.profile_call(
                "list_thread", uid, int(peer.get("id") or 0), default=[]) or [])
            for item in thread:
                item["created_at_text"] = _stamp_text(item.get("created_at"), _lang)
            self.profile_call("mark_messages_read", uid, default=0)
            return T.message_thread_page(self.context(state, active="messages"),
                                         peer_name=str(peer.get("username") or peer_name),
                                         messages=thread)
        box = "outbox" if _first(params or {}, "box") == "outbox" else "inbox"
        items = list(self.profile_call("list_messages", uid, box=box, limit=100, default=[]) or [])
        for item in items:
            item["created_at_text"] = _stamp_text(item.get("created_at"), _lang)
        conversations: dict[str, dict[str, Any]] = {}
        if box == "inbox":
            for item in items:
                name = str(item.get("peer_name") or "?")
                conv = conversations.setdefault(name, {
                    "peer_name": name, "count": 0, "unread": 0, "last": "", "last_at": ""})
                conv["count"] += 1
                if not item.get("read"):
                    conv["unread"] += 1
                if not conv["last"]:
                    conv["last"] = str(item.get("body") or "")[:60]
                    conv["last_at"] = item.get("created_at_text") or ""
            shown = [int(item.get("id") or 0) for item in items]
            self.profile_call("mark_messages_read", uid, shown, default=0)
        return T.messages_page(self.context(state, active="messages"),
                               inbox=items if box == "inbox" else [],
                               outbox=items if box == "outbox" else [],
                               conversations=sorted(
                                   conversations.values(),
                                   key=lambda c: (-c["unread"], c["peer_name"])),
                               box=box)

    def page_notifications(self, state: auth.AuthState,
                           params: Mapping[str, Any] | None = None) -> str:
        # 时间按**语言时区**显示(zh=UTC+8), 不依赖服务器时区
        _lang = self._current_lang()
        uid = self._member_uid(state)
        items = list(self.profile_call("list_notifications", uid, limit=100, default=[]) or [])
        for item in items:
            item["created_at_text"] = _stamp_text(item.get("created_at"), _lang)
        return T.notifications_page(self.context(state, active="notifications"), items=items)

    def page_feedback(self, state: auth.AuthState,
                      params: Mapping[str, Any] | None = None) -> str:
        # 时间按**语言时区**显示(zh=UTC+8), 不依赖服务器时区
        _lang = self._current_lang()
        uid = self._member_uid(state)
        feedbacks = list(self.profile_call("list_feedback", uid, default=[]) or [])
        for item in feedbacks:
            item["created_at_text"] = _stamp_text(item.get("created_at"), _lang)
        # 反馈页复用账号页的反馈区(单独页面保持简单): 直接渲染一个最简列表 + 表单
        rows = "".join(
            f'<li><span class="muted">{esc(item.get("created_at_text") or "")}</span> '
            f'{esc((item.get("body") or "")[:160])}</li>' for item in feedbacks)
        body = (
            '<section class="panel">'
            '<p class="section-note">给管理员的反馈会在这里留档。</p>'
            + (f'<ul class="plain-list">{rows}</ul>' if rows
               else '<p class="section-note">还没有提交过反馈。</p>')
            + f'<form class="auth-form" method="post" action="/feedback">{_hidden("csrf_token", self.context(state).csrf_token)}'
            '<label class="field auth-field"><span>内容</span>'
            '<textarea name="body" rows="4" maxlength="2000" required></textarea></label>'
            '<div class="field-actions"><button type="submit">提交反馈</button></div></form>'
            '</section>'
        )
        return T.layout(self.context(state, active="account"), title="向管理员反馈", body=body)

    def page_user(self, state: auth.AuthState, username: str) -> str:
        # 时间按**语言时区**显示(zh=UTC+8), 不依赖服务器时区
        _lang = self._current_lang()
        """公开资料页 ``/u/<username>``: **尊重隐私开关**."""
        target = self.users.get(username) if username else None
        if target is None:
            return T.error_page(self.context(state), status=404,
                                message=f"没有这个用户: {username}")
        uid = int(target.get("id") or 0)
        profile = self.profile_data(uid, str(target.get("username") or ""))
        visibility = self.profile_call("visibility", uid, default={}) or {}
        viewer = "anon"
        viewer_uid = self._member_uid(state) if state is not None else 0
        if viewer_uid:
            viewer = "member"
            if viewer_uid != uid and self.profile_call(
                    "is_following", viewer_uid, "user", str(uid), default=False):
                viewer = "follower"
        favorites = list(self.profile_call("list_favorites", uid, limit=30, default=[]) or [])
        for item in favorites:
            try:
                album = self.data.get_album(item.get("album_key") or "")
            except Exception:                  # noqa: BLE001
                album = None
            item["title"] = (album or {}).get("title") or ""
            if album is not None and (album.get("cover_local") or album.get("has_cover")):
                item["cover_src"] = T.cover_src(
                    str(item.get("album_key") or ""), album)
        follows = list(self.profile_call("list_follows", uid, default=[]) or [])
        contributions = list(self.profile_call("list_contributions", uid, limit=30,
                                               default=[]) or [])
        for item in contributions:
            item["created_at_text"] = _stamp_text(item.get("created_at"), _lang)
        # 资料页要有"存在感": 统计数字 + 加入时间 + 粉丝数
        # 统计数字 + 时间: 资料页要有"存在感"
        # **不显示粉丝数** —— 本站关注只支持 社团/艺人/展会(FOLLOW_KINDS),
        # 用户之间不能互相关注, 显示那个人数永远是 0。
        counts = self.profile_call("summary", uid, default={}) or {}
        joined_text = _stamp_text(target.get("created_at"), _lang)
        # 「最后活跃」用 last_seen(每次请求刷新), 不是 last_login(登录那一刻)。
        # 老账号还没写过 last_seen 时 _user_dict 会回退到 last_login。
        active_stamp = target.get("last_seen") or target.get("last_login")
        active_text = _stamp_text(active_stamp, _lang)
        follows = self._enrich_follows(follows)
        contributions = self._merge_contributions(contributions)
        return T.public_profile_page(
            self.context(state), user=target, profile=profile,
            visibility=visibility, viewer=viewer,
            favorites=favorites, follows=follows, contributions=contributions,
            counts=counts, joined_text=joined_text, active_text=active_text,
        )

    def background_response(self, arg: str) -> Response:
        """读取用户上传的背景图(本地文件, 不走外链).

        **必须用 Response.binary()** —— ``Response`` 的字段顺序是
        ``(status, content_type, body, headers, cookies)``, 直接写
        直接给 ``Response`` 传位置参数(body 在前, headers 在后)会把图片
        字节当成 content_type, 于是 ``_send`` 里 content_type 的类型检查抛
        ``TypeError`` —— 整个请求 500(实测导致站点"挂了")。
        """
        uid = str(arg or "").strip()
        if not uid.isdigit():
            return self._plain_404()
        base = self._backgrounds_dir()
        if base is None:
            return self._plain_404()
        for ext in (".jpg", ".png", ".webp", ".gif"):
            path = base / f"{uid}{ext}"
            if path.is_file():
                ctype = {".jpg": "image/jpeg", ".png": "image/png",
                         ".webp": "image/webp", ".gif": "image/gif"}[ext]
                response = Response.binary(path.read_bytes(), ctype)
                response.headers["Cache-Control"] = "private, max-age=300"
                return response
        return self._plain_404()

    @staticmethod
    def _plain_404() -> Response:
        """纯文本 404(内部小工具, 避免再手写位置参数)."""
        response = Response.binary(b"not found", "text/plain; charset=utf-8",
                                  status=404)
        return response


    def _backgrounds_dir(self) -> "_Path | None":
        """背景图目录: <data>/backgrounds/, 不存在则创建."""
        from pathlib import Path as _Path

        if self.db_path is None:
            return None
        base = _Path(self.db_path).parent / "backgrounds"
        try:
            base.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
        return base

    def _do_account_background(self, headers: Mapping[str, Any], body: bytes,
                               state: auth.AuthState, cookies: list[str]) -> Response:
        """上传/删除个人背景图(存本地, 不存外链)."""
        uid = self._member_uid(state)
        if not uid:
            return self._html(self.forbidden(state, "请先登录。"),
                              status=403, cookies=cookies)
        action = (parse_form_field(headers, body, "action") or "upload").strip()
        base = self._backgrounds_dir()
        if base is None:
            return Response.redirect("/account?tab=appearance", status=303)

        if action == "delete":
            for ext in (".jpg", ".png", ".webp", ".gif"):
                try:
                    (base / f"{uid}{ext}").unlink(missing_ok=True)
                except OSError:
                    pass
            if state.session is not None:
                state.session.flash = "背景图已清除"
            return Response.redirect("/account?tab=appearance", status=303)

        upload = parse_upload(headers, body, "background")
        if upload is None:
            if state.session is not None:
                state.session.flash = "没有收到图片文件。"
            return Response.redirect("/account?tab=appearance", status=303)
        blob, ctype = upload
        if len(blob) > 8 * 1024 * 1024:
            if state.session is not None:
                state.session.flash = "背景图太大(上限 8MB)。"
            return Response.redirect("/account?tab=appearance", status=303)
        ext = {b"\xff\xd8": ".jpg", b"\x89PNG": ".png", b"RIFF": ".webp",
               b"GIF8": ".gif"}.get(blob[:4], "")
        if not ext:
            head = blob[:12]
            if head.startswith(b"\xff\xd8"):
                ext = ".jpg"
            elif head.startswith(b"\x89PNG"):
                ext = ".png"
            elif head.startswith(b"GIF8"):
                ext = ".gif"
            elif head.startswith(b"RIFF") and b"WEBP" in blob[:16]:
                ext = ".webp"
        if not ext:
            if state.session is not None:
                state.session.flash = "只接受 JPG/PNG/WEBP/GIF。"
            return Response.redirect("/account?tab=appearance", status=303)
        for old in (".jpg", ".png", ".webp", ".gif"):
            try:
                (base / f"{uid}{old}").unlink(missing_ok=True)
            except OSError:
                pass
        (base / f"{uid}{ext}").write_bytes(blob)
        if state.session is not None:
            state.session.flash = "背景图已更新"
        return Response.redirect("/account?tab=appearance", status=303)


    def _username_initial(self, uid: int) -> str:
        """``uid -> 用户名首字母``(默认头像用); 取不到返回空串."""
        if not uid:
            return ""
        # 1) 用户库的 username(权威来源, 且不需要额外参数)
        # 注意: UserStore.get() 是按用户名查, 按 id 查要用 get_by_id()。
        try:
            user = self.users.get_by_id(int(uid))
            if user is not None:
                if isinstance(user, Mapping):
                    name = str(user.get("username") or "")
                else:
                    name = str(getattr(user, "username", "") or "")
                name = name.strip()
                if name:
                    return name[0]
        except Exception:                     # noqa: BLE001
            pass
        # 2) profile 里的展示名兜底(profile() 需要 username, 所以放后面)
        # 注意: UserStore.get() 是按**用户名**查, 按 id 查要用 get_by_id()。
        try:
            user = self.users.get_by_id(int(uid))
            if user is not None:
                if isinstance(user, Mapping):
                    name = str(user.get("username") or "")
                else:
                    name = str(getattr(user, "username", "") or "")
                name = name.strip()
                if name:
                    return name[0]
        except Exception:                     # noqa: BLE001
            pass
        return ""

    def avatar_response(self, user_id: str) -> Response:
        """``/avatar/<uid>``: 有上传头像就发图片, 否则发**默认头像**。

        以前没有头像时回 404 —— 页面上就是一个个破图标。现在统一回一张
        生成的 SVG(首字母 + 稳定配色), 所有未设置头像的用户都有一个体面的默认头像。
        """
        found = self.avatar_bytes(user_id)
        if found:
            data, ctype = found
            return Response.binary(data, ctype)

        # 没有上传头像 -> 默认头像。首字母取用户名(ops 里能查到就用首字母,
        # 查不到就用剪影), 配色由 uid 决定(同一个用户永远同色)。
        letter = ""
        try:
            uid = int(user_id or 0)
        except (TypeError, ValueError):
            uid = 0
        if uid > 0:
            letter = self._username_initial(uid)
        data = default_avatar_svg(uid or user_id, letter)
        response = Response.binary(data, "image/svg+xml")
        response.headers["Cache-Control"] = "public, max-age=300"
        return response

    # -- 个人中心: 写操作 ----------------------------------------------------

    def _do_member_write(self, name: str, values: Mapping[str, Any],
                         state: auth.AuthState, body: bytes,
                         headers: Mapping[str, Any], cookies: list[str]) -> Response:
        uid = self._member_uid(state)
        if not uid:
            return self._html(self.forbidden(state, "请先登录。"), status=403, cookies=cookies)

        def back(target: str = "/account") -> Response:
            return Response.redirect(target or "/account", status=303)

        if name == "account_profile":
            birthday = text_field(values, "birthday").strip()
            bio = text_field(values, "bio")
            store = self.profile_store()
            problem = ""
            if store is not None:
                checker = getattr(store, "bio_problem", None)
                if callable(checker):
                    problem = checker(bio)
                if not problem:
                    checker_b = getattr(store, "birthday_problem", None)
                    if callable(checker_b):
                        problem = checker_b(birthday)
            if problem:
                return self._html(self.page_account(
                    state, self.users.get_by_id(uid) or {},
                    error=problem), status=400, cookies=cookies)
            ok, msg = self.profile_call("update_profile", uid, birthday=birthday,
                                        bio=bio, default=(False, "个人中心不可用"))
            if not ok:
                return self._html(self.page_account(
                    state, self.users.get_by_id(uid) or {}, error=msg,
                    tab="profile"), status=400, cookies=cookies)
            return back(text_field(values, "next") or "/account?tab=profile")

        if name in ("account_security_code", "account_password", "account_delete"):
            return self._do_account_security(name, values, state, cookies)

        if name == "account_privacy":
            levels = {}
            for key in ("vis_birthday", "vis_bio", "vis_favorites", "vis_follows",
                        "vis_comments", "vis_contributions"):
                raw = text_field(values, key)
                if raw != "":
                    levels[key] = raw
            self.profile_call("set_visibility", uid, levels, default=(False, ""))
            accept = text_field(values, "accept_messages")
            if accept != "":
                self.profile_call("set_accept_messages", uid, accept, default=(False, ""))
            return back(text_field(values, "next") or "/account?tab=privacy")

        if name == "account_background":
            return self._do_account_background(headers, body, state, cookies)
        if name == "account_avatar":
            upload = parse_upload(headers, body, "avatar")
            if upload is None:
                return self._html(self.page_account(
                    state, self.users.get_by_id(uid) or {},
                    error="没有收到图片文件"), status=400, cookies=cookies)
            blob, ctype = upload
            store = self.profile_store()
            if store is not None:
                checker = getattr(store, "avatar_problem", None)
                if callable(checker):
                    problem = checker(ctype, len(blob))
                    if problem:
                        return self._html(self.page_account(
                            state, self.users.get_by_id(uid) or {},
                            error=problem), status=400, cookies=cookies)
            ok, msg, _rel = self.profile_call("save_avatar", uid, blob, ctype,
                                              default=(False, "个人中心不可用", ""))
            if not ok:
                return self._html(self.page_account(
                    state, self.users.get_by_id(uid) or {}, error=msg, tab="profile"),
                    status=400, cookies=cookies)
            return back("/account?tab=profile")

        if name == "favorites_toggle":
            key = text_field(values, "album_key").strip()
            if key:
                self.profile_call("toggle_favorite", uid, key, default=False)
            return back(text_field(values, "next") or "/account")

        if name == "follows_toggle":
            kind = text_field(values, "kind").strip()
            key = text_field(values, "key").strip()
            if kind and key:
                self.profile_call("toggle_follow", uid, kind, key, default=False)
            return back(text_field(values, "next") or "/account")

        if name == "messages_send":
            to_name = text_field(values, "to").strip()
            recipient = self.users.get(to_name) if to_name else None
            if recipient is None:
                return self._html(self.page_messages(state, {}), status=400, cookies=cookies)
            accepts = self.profile_call(
                "accepts_messages", int(recipient.get("id") or 0), default=True)
            if not accepts:
                if state.session is not None:
                    state.session.flash = "对方不接受私信。"
                return back("/messages")
            ok, msg = self.profile_call(
                "send_message", uid, int(recipient.get("id") or 0),
                text_field(values, "subject"), text_field(values, "body"),
                default=(False, "私信功能不可用"))
            if ok:
                self.profile_call("notify", int(recipient.get("id") or 0), "message",
                                  "你收到了新的私信", "/messages", default=0)
            return back("/messages" + ("" if ok else ""))

        if name == "notifications_read":
            self.profile_call("mark_notifications_read", uid, default=0)
            return back("/notifications")

        if name == "feedback":
            text_body = text_field(values, "body").strip()
            contact = text_field(values, "contact").strip()
            if not text_body:
                if state.session is not None:
                    state.session.flash = "反馈内容不能为空。"
                return back("/account?tab=feedback")
            ok, message = self.profile_call(
                "add_feedback", uid, text_body, contact, default=(False, ""))
            if ok:
                # **必须同时推给超级用户的通知**: 管理员反馈如果只躺在 feedback 表里,
                # 超级用户不进「我的账号」就看不见, 实际等于没发出去。
                self._notify_superusers(
                    "feedback",
                    f"收到一条管理员反馈(来自 {state.username or '用户'}): "
                    f"{text_body[:60]}{'…' if len(text_body) > 60 else ''}",
                    "/account?tab=feedback",
                )
                if state.session is not None:
                    state.session.flash = "反馈已发送，谢谢！"
            elif state.session is not None:
                state.session.flash = message or "反馈发送失败。"
            return back("/account?tab=feedback")
        return self._html(self.forbidden(state, "未知操作。"), status=400, cookies=cookies)

    def page_verify(
        self,
        state: auth.AuthState,
        params: Mapping[str, Any] | None = None,
        *,
        error: str = "",
        notice: str = "",
    ) -> str:
        """邮箱验证页.

        已登录时**永远用会话里的账号**(不接受 URL 指定别人的账号); 未登录时
        允许用 ``?username=`` 指路(验证码本身是密钥, 由服务端校验)。
        """
        query = params or {}
        username = state.username if state.logged_in else _first(query, "username")
        user = self.users.get(username) if username else None
        if user is None:
            return T.verify_page(
                self.context(state, active="verify"), username="", error=error, notice=notice,
            )
        address = V.mask_email((user or {}).get("email") or "")
        wait = 0
        latest = self.users.latest_verify_code(username, purpose=V.PURPOSE_EMAIL)
        if latest is not None:
            left = float(latest["created_at"] or 0.0) + V.EMAIL_RESEND_INTERVAL - time.time()
            wait = max(0, int(left) + 1) if left > 0 else 0
        return T.verify_page(
            self.context(state, active="verify"),
            username=str(user.get("username") or username),
            email=address,
            error=error,
            notice=notice,
            resend_wait=wait,
        )

    def _notify_superusers(self, kind: str, text: str, link: str = "") -> int:
        """给所有超级用户发站内通知(反馈/举报等需要管理员关注的事件).

        返回成功通知的人数。用户库不可用时静默失败(不能因为通知失败
        让用户的操作报错)。
        """
        if self.users is None:
            return 0
        sent = 0
        try:
            accounts = self.users.list_users()
        except Exception:              # noqa: BLE001
            return 0
        for account in accounts or []:
            if str(account.get("role") or "") != "superuser":
                continue
            uid = int(account.get("id") or 0)
            if not uid:
                continue
            try:
                ok = self.profile_call("notify", uid, kind, text, link, default=0)
                if ok:
                    sent += 1
            except Exception:          # noqa: BLE001 - 单个失败不影响其它人
                continue
        return sent

    def _do_account(
        self,
        values: Mapping[str, Sequence[str]],
        state: auth.AuthState,
        cookies: list[str],
    ) -> Response:
        """账号页唯一的写操作: 普通用户申请成为编辑者."""
        user = self.users.get_by_id(state.session.user_id) if state.session else None
        if user is None:
            return self._html(self.forbidden(state, "请先登录。"), status=403, cookies=cookies)
        action = text_field(values, "action")
        if action != "request_editor":
            return self._html(T.error_page(
                self.context(state, active="account"), status=405,
                message="账号页只支持「申请成为编辑者」。",
            ), status=405, cookies=cookies)
        ok, message = self.users.request_editor(
            user.get("username"), reason=raw_field(values, "reason"),
        )
        if not ok:
            return self._html(self.page_account(state, user, error=message), cookies=cookies)
        if state.session is not None:
            state.session.flash = "申请已提交, 等待超级用户审核"
        return Response.redirect("/account", status=303)

    def _do_verify_code(
        self,
        values: Mapping[str, Sequence[str]],
        state: auth.AuthState,
        cookies: list[str],
    ) -> Response:
        """POST /verify: 校验邮箱验证码."""
        username = state.username if state.logged_in else text_field(values, "username")
        if not username:
            return self._html(self.page_verify(
                state, error="请先登录, 或在登录页点「去填验证码」。",
            ), cookies=cookies)
        ok, message = V.check_email_code(self.users, username, text_field(values, "code"))
        if not ok:
            return self._html(self.page_verify(state, error=message), cookies=cookies)
        if state.session is not None:
            state.session.flash = "邮箱验证成功"
        is_self = state.logged_in and state.username.lower() == str(username).lower()
        return Response.redirect("/account" if is_self else "/login?sent=1", status=303)

    def _do_resend(
        self,
        values: Mapping[str, Sequence[str]],
        state: auth.AuthState,
        cookies: list[str],
    ) -> Response:
        """POST /verify/resend: 重发验证码.

        登录后可用; 匿名也允许(登录页的「重新发送」按钮), 但**响应不区分账号是否存在**,
        免得变成用户名探测器。
        """
        anonymous = not state.logged_in
        username = state.username if state.logged_in else text_field(values, "username")
        if not username:
            return self._html(self.page_verify(state, error="请填写用户名。"), cookies=cookies)
        sent, message = self.send_verification_code(username)
        if anonymous:
            if not (not sent and ("未配置 SMTP" in message or "格式" in message)):
                message = "如果该账号需要验证邮箱, 验证码已经发出, 请查收"
            return self._html(T.login_page(
                self.context(state, active="login"),
                username=username,
                notice=message,
            ), cookies=cookies)
        if not sent:
            return self._html(self.page_verify(state, error=message), cookies=cookies)
        if state.session is not None:
            state.session.flash = message
        return Response.redirect("/verify", status=303)

    def page_review(
        self,
        state: auth.AuthState,
        params: Mapping[str, Any] | None = None,
        error: str | None = None,
        notice: str | None = None,
    ) -> str:
        query = params or {}
        status = _first(query, "status") or REQUEST_PENDING
        if status not in (REQUEST_PENDING, REQUEST_APPROVED, REQUEST_REJECTED, "all"):
            status = REQUEST_PENDING
        return T.review_page(
            self.context(state, active="review"),
            requests=self.users.list_editor_requests(status=status, limit=200),
            counts=self.users.editor_request_counts(),
            status=status,
            error=error or "",
            notice=notice or "",
        )

    def _do_review(
        self,
        values: Mapping[str, Sequence[str]],
        state: auth.AuthState,
        cookies: list[str],
    ) -> Response:
        """POST /review: 通过 / 拒绝一条编辑者申请(仅超级用户)."""
        action = text_field(values, "action")
        request_id = text_field(values, "request_id")
        note = raw_field(values, "note").strip()
        actor = state.username
        if action == "approve":
            ok, message = self.users.approve_editor(request_id, actor=actor, note=note)
        elif action == "reject":
            ok, message = self.users.reject_editor(request_id, actor=actor, note=note)
        else:
            return self._html(T.error_page(
                self.context(state, active="review"), status=405,
                message="审核接口只支持通过 / 拒绝。",
            ), status=405, cookies=cookies)
        if not ok:
            return self._html(self.page_review(state, error=message), cookies=cookies)
        handled = self.users.get_editor_request(request_id) or {}
        target_user = self.users.get(handled.get("username") or "")
        if target_user is not None:
            if action == "approve":
                # 通过后对方的**在线会话**要立刻变成编辑者, 否则他还得重新登录一次
                self.sessions.update_user_role(target_user["id"], ROLE_EDITOR)
            self.profile_call(
                "notify", int(target_user["id"]), "editor",
                ("你的编辑者申请已通过" if action == "approve"
                 else "你的编辑者申请被拒绝") + (f": {note}" if note else ""),
                "/account", default=0)
        if state.session is not None:
            state.session.flash = "已通过申请" if action == "approve" else "已拒绝申请"
        return Response.redirect("/review?status=" + REQUEST_PENDING, status=303)

    def page_users_admin(
        self,
        state: auth.AuthState,
        error: str | None = None,
        notice: str | None = None,
    ) -> str:
        users = self.users.list_users()
        counts = {
            "all": len(users),
            "viewer": self.users.count_by_role(ROLE_VIEWER),
            "editor": self.users.count_by_role(ROLE_EDITOR),
            "superuser": self.users.count_by_role(ROLE_SUPERUSER),
        }
        return T.users_admin_page(
            self.context(state, active="users_admin"),
            users=users,
            counts=counts,
            error=error or "",
            notice=notice or "",
        )

    def page_dashboard(self, state: auth.AuthState) -> str:
        """站务数据看板(用户 m14663). 只有 ``can_review`` 的账号能看到."""
        data = self.dashboard_data()
        return T.dashboard_page(
            self.context(state, active="dashboard"),
            data=data,
        )

    def _do_users_admin(
        self,
        values: Mapping[str, Sequence[str]],
        state: auth.AuthState,
        cookies: list[str],
    ) -> Response:
        """POST /users: 改角色 / 禁用 / 启用(仅超级用户)."""
        action = text_field(values, "action")
        name = text_field(values, "username")
        target = self.users.get(name)
        if target is None:
            return self._html(self.page_users_admin(state, error="找不到这个用户"),
                              cookies=cookies)
        actor = state.username
        ok = False
        message = ""
        if action == "set_role":
            role = text_field(values, "role")
            ok, message = self.users.set_role(name, role, actor=actor)
            if ok:
                # 角色变化要**立刻**作用于在线会话, 否则对方得重新登录
                self.sessions.update_user_role(target["id"], role)
        elif action in ("disable", "enable"):
            enabled = action == "enable"
            ok, message = self.users.set_active(name, enabled, actor=actor)
            if ok and not enabled:
                # 禁用 = 踢下线(已经发出去的会话不能继续用)
                self.sessions.drop_user(target["id"])
        else:
            return self._html(T.error_page(
                self.context(state, active="users_admin"), status=405,
                message="用户管理接口只支持改角色 / 禁用 / 启用。",
            ), status=405, cookies=cookies)
        if not ok:
            return self._html(self.page_users_admin(state, error=message), cookies=cookies)
        if state.session is not None:
            state.session.flash = f"已更新 {name}"
        return Response.redirect("/users", status=303)

    def _album_with_url_names(self, key: str) -> dict[str, Any] | None:
        """编辑页用的专辑数据: 公开端视图 + **外链名称**.

        公开端视图(:meth:`PublicData.get_album`)出于合规考虑把 urls 压成
        ``[{"url","href"}]`` —— 名称(来源标识)被丢掉了。编辑页需要名称,
        否则表单渲染出空的「链接名」, 用户一保存就变成 link1/link2
        (实测把用户辛苦填的 source 名全冲掉)。

        名称这里直接从 payload 原文读: 编辑页只有编辑者能打开, 不涉及
        公开端的合规限制。
        """
        view = self.data.get_album(key)
        if not isinstance(view, dict):
            return None
        links = view.get("urls") or []
        if not links:
            return view
        named = self._raw_url_map(key)
        if not named:
            return view
        ordered = []
        used: set[str] = set()
        for link in links:
            url = str((link or {}).get("url") or "")
            name = ""
            for candidate_key, candidate_url in named.items():
                if str(candidate_url or "") == url and str(candidate_key) not in used:
                    name = str(candidate_key)
                    used.add(name)
                    break
            ordered.append({"key": name, "url": url})
        merged = dict(view)
        merged["urls"] = ordered
        return merged

    def _raw_url_map(self, key: str) -> dict[str, str]:
        """从 payload 原文读 ``{名称: url}``(只给编辑页用)."""
        if not key or self.db_path is None:
            return {}
        try:
            import json as _json
            import sqlite3 as _sqlite3

            conn = _sqlite3.connect(str(self.db_path), timeout=5.0)
            try:
                row = conn.execute(
                    "SELECT payload FROM albums WHERE key = ?", (str(key),)
                ).fetchone()
            finally:
                conn.close()
            if not row or not row[0]:
                return {}
            payload = _json.loads(row[0])
        except Exception:                 # noqa: BLE001
            return {}
        urls = payload.get("urls") if isinstance(payload, dict) else None
        if not isinstance(urls, Mapping):
            return {}
        return {str(k): str(v) for k, v in urls.items() if v}


    def _do_album_edit(
        self,
        key: str,
        values: Mapping[str, Sequence[str]],
        state: auth.AuthState,
        cookies: list[str],
        headers: Mapping[str, Any] | None = None,
        body: bytes = b"",
    ) -> Response:
        """编辑者保存专辑: 可写连接 + 白名单字段 + 错误回显.

        ``headers`` / ``body`` 是为了判断"这次到底有没有带封面文件"
        —— infobox 缓存图要不要转正取决于它(用户 m13899)。
        """
        album = self.data.get_album(key)
        if album is None:
            return self._html(T.error_page(
                self.context(state, active="albums"), status=404, message=f"没有这张专辑: {key}"
            ), status=404, cookies=cookies)
        current_view = (album.to_dict() if hasattr(album, "to_dict")
                        else dict(album))
        patch, errors = parse_album_form(values, current=current_view)
        if errors:
            page = T.edit_album_page(
                self.context(state, active="albums"),
                album=album,
                values=form_display_values(values),
                errors=errors,
                notice="没有保存, 请修正下面的问题。",
            )
            return self._html(page, cookies=cookies)
        # 「整段粘贴 -> 智能识别 -> 批量填入」: 粘贴框就是表单的一个普通字段,
        # 随本次 POST 一起上来, 服务端解析后按 track_mode 替换 / 追加。
        #
        # **粘贴没识别出曲目不会否决整次提交**: 用户很可能只是顺手粘了一段格式不对的
        # 曲目, 却改了标题 —— 这时把整份表单丢掉, 他会以为"保存坏了"。所以其它字段
        # 照常落库, 曲目表用**表单里原有的曲目行**(用户可见的那份, 绝不被粘贴内容覆盖),
        # 再把粘贴内容留在框里 + 提示一句, 让他改完再存一次。
        paste_text = raw_field(values, "track_paste")
        mode = normalize_track_mode(text_field(values, "track_mode"))
        has_paste = bool(paste_text.strip())
        # 用户 m17600: 已经点过「填入曲目表（暂存）」就不再解析粘贴框 ——
        # 下面表格里的内容才是用户最后看到并改过的那份。
        staged = tracks_already_staged(values)
        parsed = parse_tracklist(paste_text) if (has_paste and not staged) else []
        paste_missed = has_paste and not parsed and not staged
        if parsed:
            patch["track_list"] = merge_pasted_tracks(patch.get("track_list") or [], parsed, mode)
        # 制作人员「整段粘贴」: 同一套语义 —— 解析出词条就按 credit_mode
        # 替换/追加; 没识别出来只提示一句, 不否决整次保存。
        credit_has_paste, credit_missed = merge_credits_from_paste(patch, values)
        store = self.writable_store()
        # **编辑前**先记一条快照: 否则"还原到上一版"只会回到改完之后的状态(无法撤销)
        self._record_revision(state, str(key), "编辑前快照")
        ok, message = store.update_album_fields(key, patch)
        if ok:
            # 名称/编号 -> 实体 id(表单里可输名称, 也可输 #12 / artistId:12)
            try:
                bound = self._resolve_bindings(store, values)
                ids = store.link_album_entities(
                    key, circle=bound["circle_name"], artists=bound["artist_names"],
                    event=bound["event_name"], tags=bound["tag_names"])
                patch["ids"] = ids
            except Exception:            # noqa: BLE001 - 绑定失败不该让保存报错
                pass
        if not ok:
            page = T.edit_album_page(
                self.context(state, active="albums"),
                album=album,
                values=form_display_values(values),
                notice=f"保存失败: {message}",
            )
            return self._html(page, cookies=cookies)
        self.data.invalidate()
        # infobox 封面: 前端已把图缓存到 /cover-cache(用户 m13899),
        # 这里把它转成正式封面; 用户自己传了图则缓存作废。
        self._apply_pending_cover(str(key), values, headers, body)
        # infobox 的 cover 地址: 前端正常会直接调 /album/<key>/cover-url,
        # 这里兜住"脚本没跑成、地址随主表单提交上来"的情况。
        # **只在本来就没封面时抓**: 已经有图就绝不覆盖用户的选择。
        _hint = str(text_field(values, "cover_url_hint") or "").strip()
        if _hint:
            _cur = self.data.get_album(key) or album
            _has = isinstance(_cur, Mapping) and bool(_cur.get("cover_local"))
            if not _has:
                self._store_cover_from_url(str(key), _hint)
        if paste_missed or credit_missed:
            # 已经存过了: 回显必须说清"这次其它修改生效了", 否则用户会以为保存失败。
            notes = []
            if paste_missed:
                notes.append("粘贴内容没识别出曲目, 曲目表保持原样")
            if credit_missed:
                notes.append("粘贴内容没识别出词条, 制作人员保持原样")
            page = T.edit_album_page(
                self.context(state, active="albums"),
                album=self.data.get_album(key) or album,
                values=form_display_values(values),
                notice="; ".join(notes) + "; 本次其它修改已保存。",
            )
            return self._html(page, cookies=cookies)
        if state.session is not None:
            note = ""
            if parsed:
                action = "追加" if mode == "append" else "替换"
                note = f" · 粘贴识别 {len(parsed)} 首({action})"
            if credit_has_paste and patch.get("credits"):
                note += f" · 词条 {len(patch['credits'])} 条"
            state.session.flash = f"已保存: {patch.get('title') or key}{note}"
            self._record_revision(state, str(key), f"编辑后{note}")
            # 编辑者贡献记录(用户要求"我的贡献: 含修改记录")
            self.profile_call(
                "add_contribution", self._member_uid(state), "edit", str(key),
                str(patch.get("title") or ""), default=0)
        return Response.redirect(f"/album/{T.quote_segment(key)}", status=303)

    # -- 辅助 ---------------------------------------------------------------

    def _json(self, payload: Mapping[str, Any], *, status: int = 200,
              cookies: Sequence[str] = ()) -> Response:
        """小 JSON 响应 —— 附图 / 封面这类前端 ``fetch`` 调用的接口用.

        **不要**用它渲染页面: 页面走 :meth:`_html`。
        """
        body = json.dumps(dict(payload), ensure_ascii=False).encode("utf-8")
        response = Response.binary(body, "application/json; charset=utf-8",
                                   status=status)
        response.cookies.extend(cookies)
        return response

    def _html(self, body: str, *, status: int = 200, cookies: Sequence[str] = ()) -> Response:
        response = Response.html(body, status=status)
        response.cookies.extend(cookies)
        return response

    def forbidden(self, state: auth.AuthState, message: str) -> str:
        return T.error_page(self.context(state), status=403, message=message)

    def _list_url_for(self, base: str, filters: Mapping[str, Any], per: Any) -> Any:
        url_params = dict(filters)
        if coerce_per(per) != DEFAULT_PER:
            url_params["per"] = coerce_per(per)
        return T.make_url_for(base, url_params)

    def filter_widgets(self, **kwargs: Any) -> str:
        action = kwargs.pop("action", "/albums")
        return T.filter_panel(
            kwargs.pop("filters"),
            action=action,
            events=self.data.events(),
            kinds=self.data.event_kinds(),
            years=[y["value"] for y in self.data.years(limit=40)],
            tags=self.data.tags(limit=60),
            sorts=SORTS,
            **kwargs,
        )

    # -- 各页面 -------------------------------------------------------------

    def page_home(self, state: auth.AuthState | None = None) -> str:
        latest, _ = self.data.query_albums(page=1, per=12, sort=DEFAULT_SORT)
        # 工作队列只给有编辑权限的人查: 普通用户/访客既不看这一块, 也不该为它
        # 付两次 COUNT 查询的代价(首页是访问量最大的页面)。
        inbox: dict[str, Any] = {}
        if self.can_edit(state):
            inbox = {"total": self.data.inbox_total(), "kinds": self.data.needs_counts()}
        return T.home_page(
            self.context(state, active="home"),
            latest=latest,
            events=self.data.events()[:12],
            circles=self.data.circles_all()[:12],
            tags=self.data.tags(limit=18),
            stats=self.data.stats(),
            inbox=inbox,
        )

    def page_albums(self, params: Mapping[str, Any], state: auth.AuthState | None = None) -> str:
        # 搜索框也接受**实体编号**, 但**必须写明确的前缀**(用户 m18302):
        #   #12  /  albumId:12   -> 直接打开那张专辑
        # 以前纯数字也算编号 —— 用户搜 "2000"(想找一张叫 2000 的碟) 时会被
        # 当成 entities.id=2000 直接甩进另一张专辑的详情页, 搜索结果页根本
        # 出不来。改成: 带前缀才直通; 纯数字照常搜索, 只有一条都没搜到
        # (total == 0) 时才回退成编号查一次, 算个兜底。
        raw_q = _first(params, "q").strip()
        explicit_id = _looks_like_entity_id(raw_q)
        if explicit_id and raw_q and self.db_path is not None:
            album_key = self._album_key_from_entity_id(raw_q)
            if album_key:
                _status, html = self.page_album(album_key, state)
                return html
        filters = parse_filters(params)
        page = _first(params, "page") or 1
        per = _first(params, "per") or DEFAULT_PER
        sort = filters.get("sort") or DEFAULT_SORT
        # ``sort`` 由 query_albums 单独接, 从筛选参数里剔掉(其余直接透传)
        query_args = {k: v for k, v in filters.items() if k != "sort"}
        views, pager = self.data.query_albums(page=page, per=per, sort=sort, **query_args)
        # 兜底(用户 m18302): 纯数字一条都没搜到时, 再当成实体编号试一次 ——
        # 老用户习惯直接敲 12 打开 12 号碟, 前面把「纯数字直通」关掉了,
        # 这里补回来, 但**只在搜索完全无结果时**生效, 不抢正常搜索的戏。
        if (raw_q and raw_q.isdigit() and not pager.get("total")
                and self.db_path is not None):
            album_key = self._album_key_from_entity_id("#" + raw_q)
            if album_key:
                _status, html = self.page_album(album_key, state)
                return html
        url_for = self._list_url_for("/albums", filters, per)
        subtitle = "共 {} 张专辑".format(pager["total"])
        suggestions: dict[str, Any] | None = None
        if filters.get("q"):
            subtitle = f"搜索 “{filters['q']}” · 命中 {pager['total']} 张"
            if pager["total"] == 0:
                suggestions = self.data.search_suggestions(str(filters["q"]))
        # 搜索时顺带列出**匹配的用户**(点击进个人主页)
        user_hits: list[dict[str, object]] = []
        keyword = str(filters.get("q") or "").strip()
        if keyword and self.users is not None:
            try:
                for user in (self.users.list_users()
                             if hasattr(self.users, "list_users") else []):
                    name = str(user.get("username") or "")
                    if keyword.lower() in name.lower():
                        user_hits.append({
                            "id": int(user.get("id") or 0),
                            "username": name,
                            "role_label": str(user.get("role_label") or ""),
                        })
            except Exception:            # noqa: BLE001 - 用户库异常不影响专辑搜索
                user_hits = []

        return T.albums_page(
            self.context(state, active="albums", search_value=str(filters.get("q") or "")),
            user_hits=user_hits,
            items=views,
            pager=pager,
            filters=filters,
            url_for=url_for,
            filter_html=self.filter_widgets(filters=filters),
            heading="浏览专辑",
            subtitle=subtitle,
            suggestions=suggestions,
        )

    def page_album(self, key: str, state: auth.AuthState | None = None) -> tuple[int, str]:
        album = self.data.get_album(key)
        if album is None:
            return 404, T.error_page(
                self.context(state, active="albums"), status=404, message=f"没有这张专辑: {key}"
            )
        related = self.data.related_albums(album)
        comments, _pager = self.data.list_comments("album", key, per=50)
        circle_name = str(album.get("circle") or "")
        uid = self._member_uid(state) if state is not None else 0
        following = bool(uid and circle_name and self.profile_call(
            "is_following", uid, "circle", circle_name, default=False))
        artists = [str(item) for item in (album.get("artists") or []) if str(item).strip()]
        followed_artists = [
            artist for artist in artists
            if uid and self.profile_call("is_following", uid, "artist", artist, default=False)
        ]
        cover_upload = bool(
            self.can_edit(state) and self.db_path is not None
            and getattr(self.data, "covers_dir", None)
        )
        # 这张专辑是不是被我收藏了(详情页「收藏专辑」按钮, 用户 m14663)
        is_favorite = bool(uid and self.profile_call(
            "is_favorite", uid, str(album.get("key") or ""), default=False))
        # 艺人与社团卡片: 悬停显示图片/统计/简介 + 关注按钮(替代底部关注块)
        comment_ids = [int(c.get("id") or 0) for c in comments if c.get("id")]
        like_counts: dict[int, int] = {}
        my_likes: set[int] = set()
        if self.db_path is not None and comment_ids:
            from dddb.db import Store as _Store
            _s = _Store(self.db_path)
            try:
                like_counts = _s.comment_like_counts(comment_ids)
                if uid:
                    my_likes = _s.liked_comment_ids(
                        str((state.session.username if state and state.session else "") or ""),
                        comment_ids)
            finally:
                _s.close()
        entities: list[dict[str, object]] = []
        if circle_name:
            meta: list[tuple[str, str]] = []
            image = ""
            description = ""
            try:
                count = int(self.data._query(
                    "SELECT COUNT(*) AS c FROM albums WHERE circle = ?",
                    (circle_name,))[0]["c"])
                meta.append(("专辑", f"{count} 张"))
            except (IndexError, KeyError, TypeError):
                pass
            try:
                rows = self.data._query(
                    "SELECT payload FROM circles WHERE name = ? COLLATE NOCASE LIMIT 1",
                    (circle_name,))
                if rows:
                    record = json.loads(rows[0]["payload"] or "{}")
                    image = str(record.get("logo_url") or "")
                    description = str(record.get("description") or "")
                    if record.get("activity_type"):
                        meta.append(("类型", str(record["activity_type"])))
                    if record.get("founded"):
                        meta.append(("成立", str(record["founded"])))
            except (ValueError, KeyError, TypeError, IndexError):
                pass
            entities.append({
                "kind": "circle", "name": circle_name,
                "href": str(album.get("circle_href") or ""),
                "image": image, "description": description, "meta": meta,
                "following": following,
            })
        for artist in artists[:12]:
            try:
                count = int(self.data._query(
                    "SELECT COUNT(*) AS c FROM albums WHERE payload LIKE ?",
                    (f'%"{artist}"%',))[0]["c"])
                artist_meta = [("专辑", f"{count} 张")]
            except (IndexError, KeyError, TypeError):
                artist_meta = []
            entities.append({
                "kind": "artist", "name": artist,
                # 艺人卡片要跳到**艺人页**, 不是"在专辑里搜这个名字"
                "href": "/artist/" + urllib.parse.quote(artist, safe=""),
                "meta": artist_meta, "following": artist in followed_artists,
            })
        return 200, T.album_detail_page(
            self.context(state, active="albums", search_value=album.get("title") or ""),
            album=album,
            related=related,
            comments=comments,
            circle_name=circle_name,
            following_circle=following,
            following_artists=followed_artists,
            cover_upload=cover_upload,
            entities=entities,
            entities_more=max(0, len(artists) - 12),
            entities_more_names="、".join(artists[12:]),
            comment_likes=like_counts,
            liked_ids=sorted(my_likes),
            favorite=is_favorite,
        )

    def _save_new_photos(self, key: str, values: Mapping[str, Any]) -> int:
        """新建专辑时带的「其他照片」-> 落盘 + 写进 payload.

        新建页还没有 key, 没法走编辑页那套异步 ``POST /album/<key>/photo``,
        所以前端把图缩成 data URL 放进隐藏域 ``photos_data``(JSON 数组,
        每项 ``{data, note}``), 等专辑建好由这里补写。返回值 = 成功张数。
        """
        import base64 as _b64
        import json as _json
        import uuid as _uuid

        from .photos import MAX_PHOTO_BYTES, add_photo, photo_dir

        key = str(key or "").strip()
        if not key or self.db_path is None:
            return 0
        raw = str(raw_field(values, "photos_data") or "").strip()
        if not raw.startswith("["):
            return 0
        try:
            items = _json.loads(raw)
        except ValueError:
            return 0
        if not isinstance(items, list) or not items:
            return 0
        covers = getattr(self.data, "covers_dir", None)
        if not covers:
            return 0
        folder = photo_dir(covers, key)
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            return 0
        from dddb.db import Store as _Store
        store = _Store(self.db_path)
        saved = 0
        try:
            row = store.query_one("SELECT payload FROM albums WHERE key = ?", (key,))
            if row is None:
                return 0
            payload = str(row["payload"] or "{}")
            for item in items[:MAX_NEW_PHOTOS]:
                if not isinstance(item, Mapping):
                    continue
                match = re.match(
                    r"data:image/(png|jpe?g|webp);base64,(.+)",
                    str(item.get("data") or ""), re.I | re.S)
                if match is None:
                    continue
                ext = {"png": ".png", "jpg": ".jpg", "jpeg": ".jpg",
                       "webp": ".webp"}[match.group(1).lower()]
                try:
                    blob = _b64.b64decode(match.group(2), validate=False)
                except Exception:             # noqa: BLE001
                    continue
                if not blob or len(blob) > MAX_PHOTO_BYTES:
                    continue
                name = _uuid.uuid4().hex[:16] + ext
                try:
                    (folder / name).write_bytes(blob)
                except OSError:
                    continue
                url = "/photo/{}/{}".format(T.quote_segment(key), name)
                payload, ok = add_photo(payload, url=url, path=str(folder / name),
                                        note=str(item.get("note") or "")[:200])
                if not ok:
                    try:
                        (folder / name).unlink()
                    except OSError:
                        pass
                    continue
                saved += 1
            if saved:
                store.execute(
                    "UPDATE albums SET payload = ? WHERE key = ?",
                    (json.dumps(_json.loads(payload) if isinstance(payload, str)
                                else payload, ensure_ascii=False), key))
        finally:
            store.close()
        return saved

    @staticmethod
    def _split_names(raw: str) -> list[str]:
        """多值拆分: **以换行为主**(名字里可能含"、"), 旧数据兼容"、"。"""
        import re as _re
        text = str(raw or "")
        parts = text.splitlines() if "\n" in text else _re.split(r"[、,，;；/|／]+", text)
        out: list[str] = []
        for part in parts:
            value = part.strip()
            if value and value not in out:
                out.append(value)
        return out

    @staticmethod
    def _parse_credits(raw: str) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        for line in str(raw or "").splitlines():
            line = line.strip()
            if not line:
                continue
            role, sep, names = line.partition(":")
            if not sep:
                role, sep, names = line.partition("：")
            out.append({"role": role.strip() or "制作",
                        "name": "、".join(PublicSite._split_names(names))})
        return out

    @staticmethod
    def _parse_urls(raw: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for index, line in enumerate(str(raw or "").splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            key, sep, url = line.partition(":")
            if not sep:
                key, sep, url = line.partition("：")
            url = url.strip()
            if url:
                out[(key.strip() or f"链接 {index}")] = url
        return out

    @staticmethod
    def _parse_tracks(raw: str) -> list[dict[str, object]]:
        import re as _re
        tracks: list[dict[str, object]] = []
        for line in str(raw or "").splitlines():
            text = line.strip()
            if not text:
                continue
            duration = ""
            match = _re.search(r"(\d{1,2}:\d{2}(?::\d{2})?)\s*$", text)
            if match:
                duration = match.group(1)
                text = text[: match.start()].strip()
            title, sep, artists = text.partition(" - ")
            if not sep:
                title, sep, artists = text.partition(" / ")
            tracks.append({"title": title.strip() or text,
                           "artists": PublicSite._split_names(artists),
                           "duration": duration})
        return tracks

    def _make_album_key(self, title: str, circle: str, date: str) -> str:
        """专辑 key 生成(统一走 Store, 避免多处实现分叉)."""
        from dddb.db import Store as _Store
        if self.db_path is None:
            return "new-album"
        store = _Store(self.db_path)
        try:
            return store.make_album_key(title, circle, date)
        finally:
            store.close()

    def _save_new_image(self, kind: str, name: str,
                        headers: Mapping[str, Any] | None, body: bytes) -> None:
        """新建表单里上传的图片 -> 云端目录, 并把地址写进条目."""
        if not name or not body:
            return
        upload = parse_upload(headers or {}, body, "cover")
        if upload is None:
            return
        data, content_type = upload
        ext = {"image/png": ".png", "image/jpeg": ".jpg",
               "image/webp": ".webp"}.get(str(content_type or "").lower(), "")
        if not ext or len(data) > MAX_IMAGE_BODY:
            return
        from dddb.db import Store as _Store
        if self.db_path is None:
            return
        if kind == "album":
            # 注意: PublicSite **没有** ``self.config``(踩过线上 500:
            # "'PublicSite' object has no attribute 'config'")。封面目录只在
            # ``self.data.covers_dir`` 上, 构建站点时由 build_site() 注入。
            covers = getattr(self.data, "covers_dir", None)
            if covers is None:
                return
            try:
                target = Path(covers) / f"{T.quote_segment(name).replace('%', '_')}{ext}"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            except OSError:
                return
            store = _Store(self.db_path)
            try:
                store.update_album_fields(name, {"cover_local": str(target)})
            finally:
                store.close()
            return
        if kind not in ("artist", "circle"):
            return
        # 同样不能读 self.config: 头像/社徽目录挂在封面目录下的 logos/,
        # 拿不到封面目录就退回数据库同级目录(与系列图片一致)。
        covers = getattr(self.data, "covers_dir", None)
        base = Path(covers) if covers else (
            Path(self.db_path).parent / "covers" if self.db_path else Path("."))
        logos = base / "logos"
        try:
            logos.mkdir(parents=True, exist_ok=True)
            safe = T.quote_segment(name).replace("%", "_")
            filename = f"{kind}-{safe}{ext}"
            (logos / filename).write_bytes(data)
        except OSError:
            return
        url = "/logo/" + T.quote_segment(filename)
        store = _Store(self.db_path)
        try:
            if kind == "artist":
                current = store.get_artist(name) or {"name": name}
                current["avatar"] = url
                current.pop("updated_at", None)
                store.upsert_artist(name, current)
            else:
                from dddb.models import Circle
                store.upsert_circle(Circle.from_dict({"name": name, "logo_url": url}))
        finally:
            store.close()

    def _save_new_photos(self, key: str, values: Mapping[str, Any]) -> int:
        """新建专辑时带的「其他照片」-> 落盘 + 写进 payload.

        新建页还没有 key, 没法走编辑页那套异步 ``POST /album/<key>/photo``,
        所以前端把图缩成 data URL 放进隐藏域 ``photos_data``(JSON 数组,
        每项 ``{data, note}``), 等专辑建好由这里补写。返回值 = 成功张数。
        """
        import base64 as _b64
        import json as _json
        import uuid as _uuid

        from .photos import MAX_PHOTO_BYTES, add_photo, photo_dir

        key = str(key or "").strip()
        if not key or self.db_path is None:
            return 0
        raw = str(raw_field(values, "photos_data") or "").strip()
        if not raw.startswith("["):
            return 0
        try:
            items = _json.loads(raw)
        except ValueError:
            return 0
        if not isinstance(items, list) or not items:
            return 0
        covers = getattr(self.data, "covers_dir", None)
        if not covers:
            return 0
        folder = photo_dir(covers, key)
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            return 0
        from dddb.db import Store as _Store
        store = _Store(self.db_path)
        saved = 0
        try:
            row = store.query_one("SELECT payload FROM albums WHERE key = ?", (key,))
            if row is None:
                return 0
            payload = str(row["payload"] or "{}")
            for item in items[:MAX_NEW_PHOTOS]:
                if not isinstance(item, Mapping):
                    continue
                match = re.match(
                    r"data:image/(png|jpe?g|webp);base64,(.+)",
                    str(item.get("data") or ""), re.I | re.S)
                if match is None:
                    continue
                ext = {"png": ".png", "jpg": ".jpg", "jpeg": ".jpg",
                       "webp": ".webp"}[match.group(1).lower()]
                try:
                    blob = _b64.b64decode(match.group(2), validate=False)
                except Exception:             # noqa: BLE001
                    continue
                if not blob or len(blob) > MAX_PHOTO_BYTES:
                    continue
                name = _uuid.uuid4().hex[:16] + ext
                try:
                    (folder / name).write_bytes(blob)
                except OSError:
                    continue
                url = "/photo/{}/{}".format(T.quote_segment(key), name)
                payload, ok = add_photo(payload, url=url, path=str(folder / name),
                                        note=str(item.get("note") or "")[:200])
                if not ok:
                    try:
                        (folder / name).unlink()
                    except OSError:
                        pass
                    continue
                saved += 1
            if saved:
                store.execute(
                    "UPDATE albums SET payload = ? WHERE key = ?",
                    (json.dumps(_json.loads(payload) if isinstance(payload, str)
                                else payload, ensure_ascii=False), key))
        finally:
            store.close()
        return saved

    @staticmethod
    def _split_names(raw: str) -> list[str]:
        """多值拆分: **以换行/回车为主**(名字本身可能含"、"), 单值时兼容"、"。

        表单的多值输入用回车分隔(前端也把每项写成换行), 所以这里优先按换行拆;
        只有确实是单行内容时才退回旧的"、"分隔, 兼容历史数据与手工输入。
        """
        import re as _re

        text = str(raw or "")
        if "\n" in text or "\r" in text:
            parts = _re.split(r"[\r\n]+", text)
        else:
            parts = _re.split(r"[、,，;；/|／]+", text)
        out: list[str] = []
        for part in parts:
            value = part.strip()
            if value and value not in out:
                out.append(value)
        return out

    @staticmethod
    def _parse_credits(raw: str) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        for line in str(raw or "").splitlines():
            line = line.strip()
            if not line:
                continue
            role, _, names = line.partition(":")
            if not names:
                role, _, names = line.partition("：")
            out.append({"role": role.strip() or "制作",
                        "name": "、".join(PublicSite._split_names(names))})
        return out

    @staticmethod
    def _parse_urls(raw: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for index, line in enumerate(str(raw or "").splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            key, _, url = line.partition(":")
            if not url:
                key, _, url = line.partition("：")
            url = url.strip()
            if url:
                out[(key.strip() or f"链接 {index}")] = url
        return out

    @staticmethod
    def _parse_tracks(raw: str) -> list[dict[str, object]]:
        tracks: list[dict[str, object]] = []
        import re as _re
        for line in str(raw or "").splitlines():
            text = line.strip()
            if not text:
                continue
            duration = ""
            match = _re.search(r"(\d{1,2}:\d{2}(?::\d{2})?)\s*$", text)
            if match:
                duration = match.group(1)
                text = text[: match.start()].strip()
            title, _, artists = text.partition(" - ")
            if not artists:
                title, _, artists = text.partition(" / ")
            tracks.append({"title": title.strip() or text, "artists": PublicSite._split_names(artists),
                           "duration": duration})
        return tracks

    @staticmethod
    @staticmethod
    @staticmethod
    def _values_from_multipart(headers: Mapping[str, Any] | None,
                               body: bytes) -> dict[str, Any]:
        """multipart 表单 -> ``{key: [values]}``, **文件字段另存为 dict**.

        **必须保留同名字段的全部值**: 编辑表单里 ``url_key`` / ``url_value`` /
        ``credit_role`` / ``track_title`` 这类字段都会重复出现, 每个表单行一组。
        早先借用 ``parse_multipart()`` 得到 ``dict[str, str]`` —— 同名只留最后一个,
        于是"外链 5 条 -> 保存后 0 条"(用户实测报告的数据丢失)。

        文件字段(带 ``filename=``)不要丢: 收成
        ``{"filename", "content_type", "data"}`` 放进同一个 dict, 否则
        ``_save_series_image`` / ``_save_album_cover_file`` 读不到上传内容,
        图片会被静默忽略(页面上就"上传了但没显示")。
        """
        out: dict[str, list[str]] = {}
        raw = body if isinstance(body, bytes) else b""
        if not raw:
            return out
        ctype = str((headers or {}).get("content-type")
                    or (headers or {}).get("Content-Type") or "")
        matched = re.search(r'boundary="?([^";]+)"?', ctype, re.I)
        if "multipart/form-data" not in ctype.lower() or not matched:
            return out
        boundary = ("--" + matched.group(1)).encode("utf-8")

        for chunk in raw.split(boundary):
            if not chunk or chunk.startswith(b"--"):
                continue
            chunk = chunk.lstrip(b"\r\n")
            if not chunk:
                continue
            if b"\r\n\r\n" in chunk:
                head, _, payload = chunk.partition(b"\r\n\r\n")
            elif b"\n\n" in chunk:
                head, _, payload = chunk.partition(b"\n\n")
            else:
                continue
            name_match = re.search(rb'name="([^"]*)"', head)
            if not name_match:
                continue
            name = name_match.group(1).decode("utf-8", "replace")
            data = payload.rstrip(b"\r\n-")
            filename_match = re.search(rb'filename="([^"]*)"', head)
            if filename_match or b"filename=" in head:
                filename = (filename_match.group(1).decode("utf-8", "replace")
                            if filename_match else "")
                if not filename:
                    # 空文件框(用户没选文件) -> 忽略, 不要当成"清空"
                    continue
                ctype_match = re.search(rb"Content-Type:\s*([^\r\n]+)",
                                        head, re.I)
                ctype = (ctype_match.group(1).decode("utf-8", "replace").strip()
                         if ctype_match else "")
                out[name] = {
                    "filename": filename,
                    "content_type": ctype,
                    "data": data,
                }
                continue
            value = data.decode("utf-8", "replace")
            existing = out.get(name)
            if isinstance(existing, list):
                existing.append(value)
            else:
                out[name] = [value]
        return out


    def _new_entity_with_fallback(self, kind: str, values: Mapping[str, Any],
                            state: auth.AuthState, cookies: list[str],
                            headers: Mapping[str, Any] | None = None,
                            body: bytes = b"") -> Response:
        """``_do_new_entity`` 的**兜底外壳**: 任何异常都回填新建表单, 绝不 500.

        用户 m13494: 新建专辑 adore 时报 ``500 内部错误: 'PublicSite' object
        has no attribute 'config'`` —— 通用错误页只有一个错误码, **填过的内容
        全没了**, 草稿也在提交那一刻被清掉, 等于白写两张表。
        所以这里把没预料到的异常也变成"回到新建页、内容原样还在"。
        """
        try:
            return self._do_new_entity(kind, values, state, cookies, headers, body)
        except Exception as exc:                  # noqa: BLE001 - 兜底
            logger.exception("新建条目失败 kind=%s", kind)
            return self._new_entity_failed(kind, values, state, cookies, exc)

    def _new_entity_failed(self, kind: str, values: Mapping[str, Any],
                           state: auth.AuthState, cookies: list[str],
                           exc: BaseException) -> Response:
        """把"没建成功"渲染成**带原值的新建表单**(而不是一个错误页)."""
        # 用户 m19090: 以前这里只说"没有保存成功", 具体原因只进了日志 ——
        # 用户(aslovt)看到的就是"社团建不了"却完全不知道为什么。异常里的
        # 一句话(message, 通常是 ValueError 的说明)要原样给他看。
        reason = str(exc or "").strip()
        if len(reason) > 200:
            reason = reason[:200] + "…"
        note = ("没有保存成功。下面是你刚才填的内容(服务端已原样保留), "
                "检查后可以直接再点一次保存。")
        if reason:
            note = f"没有保存成功：{reason}。下面是你刚才填的内容(服务端已原样保留)。"
        html = ""
        try:
            if kind == "album":
                patch, errors = parse_album_form(values)
                # 回显必须用 form_display_values(): 模板要的是"表单回显结构"
                # (``urls`` 是 ``[{key, url}]``、``credits`` 是 ``[{role, name}]``),
                # 而不是 parse_album_form 的 patch(那里 urls 是 {名字: 地址},
                # 直接塞给模板会 AttributeError: 'str' object has no attribute 'get')。
                display = form_display_values(values)
                # 解析出来但表单里没有的(比如曲目粘贴被识别出来的标题)补进去,
                # 用户看到的就是"他真要保存的样子"。
                for field in ("title", "title_original", "title_romaji", "circle",
                              "release_date", "event", "catalog_no", "price",
                              "description"):
                    if not str(display.get(field) or "").strip() and patch.get(field):
                        display[field] = patch[field]
                if patch.get("tracks") and not display.get("tracks"):
                    display["tracks"] = patch["tracks"]
                html = T.edit_album_page(
                    self.context(state, active="edit"),
                    album={},
                    values=display,
                    errors=errors,
                    notice=note,
                    bind_options=self.bind_options(),
                    creating=True,
                )
            else:
                html = T.new_entity_page(
                    self.context(state, active="edit"), kind,
                    values=dict(values or {}), notice=note,
                    bind_options=self.bind_options(),
                )
        except Exception:                         # noqa: BLE001
            logger.exception("回填新建表单也失败了 kind=%s", kind)
            html = ""
        if not html:
            html = T.error_page(
                self.context(state), status=500,
                message=f"内部错误: {exc}")
        # 提交失败时**保住草稿**: 前端看到本页仍有编辑表单就不会清 localStorage,
        # 所以这里刻意不写 data-draft-done 之类的完成标记。
        return self._html(html, status=500, cookies=cookies)

    def _do_new_entity(self, kind: str, values: Mapping[str, Any],
                       state: auth.AuthState, cookies: list[str],
                       headers: Mapping[str, Any] | None = None,
                       body: bytes = b"") -> Response:
        """POST /new/<kind>: 编辑者创建条目(支持 multipart 上传图片)."""
        if not self.can_edit(state):
            return self._html(self.forbidden(state, "只有编辑者可以新建条目。"),
                              status=403, cookies=cookies)
        if self.db_path is None:
            return self._html(self.forbidden(state, "本站当前不允许写入。"),
                              status=503, cookies=cookies)
        content_type = str((headers or {}).get("content-type") or "")
        if content_type.startswith("multipart/form-data") and body:
            values = {**dict(values), **self._values_from_multipart(headers, body)}

        def field(name: str) -> str:
            return text_field(values, name).strip()

        from dddb.db import Store as _Store
        from dddb.models import Album, Circle
        store = _Store(self.db_path)
        created_key = ""
        created_name = ""
        try:
            if kind == "album":
                # **与编辑页同一个解析器**: 新界面提交的是卡片的
                # track_* 多列 / credit_role+credit_name 配对 /
                # url_key+url_value 配对 / agents 隐藏字段,
                # 只认扁平 textarea 的旧解析会把这些字段全丢掉。
                # 注意: ``parse_album_form`` 在本模块里, 不在 templates 里 ——
                # 写成 ``T.parse_album_form`` 会让 /new/album 直接 500。
                patch, errors = parse_album_form(values)
                if errors:
                    # 有错就回填整张卡片界面(不是那张 19 个文本框的旧表单)。
                    # 回显用 form_display_values: patch 里的 urls 是 {名字: 地址},
                    # 模板要的是 [{key, url}] —— 直接塞 patch 会 500(踩过)。
                    return self._html(
                        T.edit_album_page(
                            self.context(state, active="edit"),
                            album={}, values=form_display_values(values),
                            errors=errors,
                            notice="没有保存，请修正下面的问题。",
                            bind_options=self.bind_options(),
                            creating=True,
                        ),
                        status=400, cookies=cookies)
                # 「整段粘贴」曲目: 编辑页由 _do_album_edit 处理, 新建页
                # 以前**完全没读** track_paste —— 粘贴进来的曲目全丢。
                # 这里与编辑页用同一套解析 + 合并。
                paste_text = raw_field(values, "track_paste")
                if paste_text.strip() and not tracks_already_staged(values):
                    parsed = parse_tracklist(paste_text)
                    if parsed:
                        patch["track_list"] = merge_pasted_tracks(
                            patch.get("track_list") or [], parsed,
                            normalize_track_mode(text_field(values, "track_mode")))
                # 制作人员「整段粘贴」: 与编辑页同一套解析
                merge_credits_from_paste(patch, values)
                title = str(patch.get("title_original")
                            or patch.get("title") or "").strip()
                if not title:
                    return self._html(self.forbidden(state, "标题不能为空。"),
                                      status=400, cookies=cookies)
                date = str(patch.get("release_date") or "")
                # 表单里给了 album_key 就用它, 否则按标题生成
                key = (str(text_field(values, "album_key") or "").strip()
                       or self._make_album_key(title, str(patch.get("circle")
                                                          or ""), date))
                payload = {
                    "key": key,
                    "title": str(patch.get("title") or title),
                    "title_original": title,
                    "title_romaji": str(patch.get("title_romaji") or ""),
                    "circle": str(patch.get("circle") or ""),
                    "artists": list(patch.get("artists") or []),
                    "release_date": date,
                    "event": str(patch.get("event") or ""),
                    "hall": str(patch.get("hall") or ""),
                    "booth": str(patch.get("booth") or ""),
                    "catalog_no": str(patch.get("catalog_no") or ""),
                    "price": str(patch.get("price") or ""),
                    "description": str(patch.get("description") or ""),
                    "tags": list(patch.get("tags") or []),
                    "genres": list(patch.get("genres") or []),
                    "credits": list(patch.get("credits") or []),
                    "urls": dict(patch.get("urls") or {}),
                    "tracks": list(patch.get("track_list") or []),
                    # 用户 m19622: 这几个字段 patch 里早就有, 但新建时没抄进
                    # payload —— 新建页填了「原作 / 制作人 / 发布平台 / 侧别」
                    # 保存后全丢(编辑页反而正常)。
                    "original_work": str(patch.get("original_work") or ""),
                    "producer": str(patch.get("producer") or ""),
                    "platform": str(patch.get("platform") or ""),
                    "release_kind": str(patch.get("release_kind") or ""),
                    "discs": dict(patch.get("discs") or {}),
                    "source_ids": {},
                }
                store.upsert_album(Album.from_dict(payload))
                store.link_album_entities(
                    key, circle=payload["circle"], artists=payload["artists"],
                    event=payload["event"], tags=payload["tags"])
                store.add_revision(key, user_id=self._member_uid(state),
                                   username=str(state.session.username
                                                if state.session else ""),
                                   summary="新建专辑")
                # 封面: 新建表单里的 <input type="file" name="cover">
                # 没传文件时按两级兜底 ——
                #   ① cover_data: 草稿里存着的 data URL(用户选过图但刷新过页面,
                #      file input 的值恢复不回来, 只有这份 base64 还在);
                #   ② cover_url_hint: infobox 带的图片地址, 服务端抓一次
                #      (新建页还没有 key, 前端没法调 /cover-url)
                # infobox 缓存图优先(用户 m13899): 用户没自带封面文件时,
                # 把填表时缓存下来的那张转正。
                if not self._apply_pending_cover(key, values, headers, body):
                    if not self._save_album_cover_file(key, values):
                        if not self._store_cover_from_data(key, values):
                            _hint = str(text_field(values, "cover_url_hint") or "").strip()
                            if _hint:
                                self._store_cover_from_url(key, _hint)
                # 其他照片: 新建页没有 key, 前端先把图缩成 data URL 塞进
                # ``photos_data``, 这里建完专辑再逐张落盘(与编辑页同一个目录)。
                self._save_new_photos(key, values)
                # 建完直接跳**详情页**(用户 m13494: 别再回编辑界面)。
                # ``?created=1`` 是给前端看的信号: 落地页没有编辑表单 -> 清草稿。
                created_key = created_name = key
                target = "/album/" + T.quote_segment(key) + "?created=1"
            elif kind == "circle":
                name = field("name")
                if not name:
                    return self._html(self.forbidden(state, "社团名不能为空。"),
                                      status=400, cookies=cookies)
                staff = []
                for line in field("staff").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    role, _, who = (line.partition(":") if ":" in line
                                    else line.partition("："))
                    if who.strip():
                        staff.append({"role": role.strip(), "name": who.strip()})
                store.upsert_circle(Circle.from_dict({
                    "name": name,
                    "activity_type": field("activity_type"),
                    "founded": field("founded"),
                    "last_active": field("last_active"),
                    "description": field("description"),
                    "urls": self._parse_urls(field("urls")),
                    "staff": staff,
                }))
                created_name = name
                target = "/circle/" + T.quote_segment(name)
            elif kind == "artist":
                name = field("name")
                if not name:
                    return self._html(self.forbidden(state, "艺名不能为空。"),
                                      status=400, cookies=cookies)
                store.upsert_artist(name, {
                    "name_en": field("name_en"), "name_ja": field("name_ja"),
                    "real_name": field("real_name"),
                    "real_name_en": field("real_name_en"),
                    "aliases": self._split_names(field("aliases")),
                    "gender": field("gender"), "birthday": field("birthday"),
                    "occupations_text": field("occupations_text"),
                    "nationality": field("nationality").upper()[:2],
                    "bio": field("bio"), "avatar": field("avatar"),
                    "links": [{"href": url, "text": label}
                              for label, url in
                              self._parse_urls(field("links")).items()],
                })
                created_name = name
                target = "/artist/" + T.quote_segment(name)
            elif kind == "event":
                name = field("name")
                if not name:
                    return self._html(self.forbidden(state, "展会名不能为空。"),
                                      status=400, cookies=cookies)
                try:
                    year = int(field("year") or 0)
                except ValueError:
                    year = 0
                # 会次号/系列: 表单只填「会次标签」, 会次号从标签反推
                # (避免同一件事填两遍 —— 用户反馈"会次号、标签和会次标签重复")
                _info = parse_event_name(name)
                _vol_label = (field("vol_label").strip()
                              or _info["vol_label"])
                _series = (field("series").strip() or _info["series"])
                _vol = _info["vol"] or vol_from_label(_vol_label)
                store.save_event(
                    name, kind="", year=year,
                    season=field("season"),
                    start_date=field("start_date"),
                    end_date=field("end_date"),
                    tags=self._split_names(field("tags")),
                    links=self._parse_urls(field("links")),
                    series=_series, vol=_vol, vol_label=_vol_label)
                created_name = name
                target = "/event/" + T.quote_segment(name)
                # 会次图: 随表单上传
                self._save_event_logo_file(name, values)
            elif kind == "tag":
                name = field("name")
                if not name:
                    return self._html(self.forbidden(state, "标签名不能为空。"),
                                      status=400, cookies=cookies)
                ok, message = store.save_tag(name, kind=field("kind"),
                                             note=field("note"))
                if not ok:
                    return self._html(self.forbidden(state, message), status=400,
                                      cookies=cookies)
                created_name = name
                target = "/tag/" + T.quote_segment(name)
            else:
                return Response.redirect("/edit", status=303)
        finally:
            store.close()

        # 条目**已经建好了**, 图片上传是最后一步的锦上添花 ——
        # 这里任何异常都只记日志(以前这里 500, 用户以为整次创建失败)。
        try:
            self._save_new_image(kind, created_key or created_name, headers, body)
        except Exception:                         # noqa: BLE001
            logger.exception("新建条目后保存图片失败 kind=%s", kind)
        if hasattr(self.data, "invalidate"):
            self.data.invalidate()
        if state.session is not None:
            state.session.flash = "已创建"
        return Response.redirect(target, status=303)

    def bind_options(self) -> dict[str, list[dict[str, Any]]]:
        """给编辑表单的候选(社团/艺人/展会/标签), 按名称排序取前若干.

        艺人候选带 ``id``(entities 表主键), 制作人员按钮用它锁定 **ArtistId** ——
        重名时靠 id 区分, 马甲(同一人的不同艺名)也各自是一个 id。
        """
        from dddb.db import Store as _Store
        if self.db_path is None:
            return {}
        store = _Store(self.db_path)
        try:
            out: dict[str, list[dict[str, Any]]] = {}
            # 艺人多给一些: 制作人员要能在本地候选里搜到具体艺人
            limits = {"circle": 500, "artist": 2000, "event": 500, "tag": 500}
            for kind in ("circle", "artist", "event", "tag"):
                out[kind] = store.entity_list(kind, limit=limits[kind])
            # 会次表单的「所属系列」下拉: 直接给系列名 + 缩写
            out["event_series"] = [
                {"name": x["name"], "abbr": str(x.get("abbr") or "")}
                for x in store.list_series()
            ]
            return out
        except Exception:                       # noqa: BLE001 - 只读库缺表时降级
            return {}
        finally:
            store.close()

    def _resolve_bindings(self, store: Any, values: Mapping[str, Any]) -> dict[str, Any]:
        """把表单里的"名称/编号"解析成实体, 返回正式名称与 id."""
        def one(kind: str, field: str) -> tuple[int, str]:
            return store.resolve_entity(kind, text_field(values, field).strip())

        circle_id, circle_name = one("circle", "circle")
        event_id, event_name = one("event", "event")
        artist_ids: list[int] = []
        artist_names: list[str] = []
        for part in self._split_names(text_field(values, "artists")):
            entity_id, name = store.resolve_entity("artist", part)
            artist_names.append(name or part)
            if entity_id:
                artist_ids.append(entity_id)
        tag_ids: list[int] = []
        tag_names: list[str] = []
        for part in self._split_names(text_field(values, "tags")):
            entity_id, name = store.resolve_entity("tag", part)
            tag_names.append(name or part)
            if entity_id:
                tag_ids.append(entity_id)
        return {
            "circle_id": circle_id, "circle_name": circle_name or text_field(values, "circle").strip(),
            "event_id": event_id, "event_name": event_name or text_field(values, "event").strip(),
            "artist_ids": artist_ids, "artist_names": artist_names,
            "tag_ids": tag_ids, "tag_names": tag_names,
        }

    def _entity_form_values(self, kind: str, name: str) -> dict[str, Any]:
        """把已有资料转成编辑表单的字段值."""
        if kind == "artist":
            detail = self.data.artist_detail(name) or {}
            links = "\n".join(f'{x.get("text")}: {x.get("href")}' for x in detail.get("links") or [])
            return {
                "name": detail.get("name") or name,
                "name_en": detail.get("name_en") or "",
                "name_ja": detail.get("name_ja") or "",
                "real_name": detail.get("real_name") or "",
                "real_name_en": detail.get("real_name_en") or "",
                "aliases": "、".join(detail.get("aliases") or []),
                "gender": detail.get("gender") or "",
                "birthday": detail.get("birthday") or "",
                "occupations_text": detail.get("occupations_text")
                or "、".join(detail.get("occupations") or []),
                "nationality": detail.get("nationality") or "",
                "bio": detail.get("bio") or "",
                "avatar": detail.get("avatar") or "",
                "links": links,
            }
        if kind == "circle":
            detail = self.data.circle_detail(name) or {}
            links = "\n".join(f'{x.get("text")}: {x.get("href")}' for x in detail.get("links") or [])
            return {
                "name": detail.get("name") or name,
                "activity_type": detail.get("activity_type") or "",
                "founded": detail.get("founded") or "",
                "last_active": detail.get("last_date") or detail.get("last_event") or "",
                "description": detail.get("description") or "",
                "urls": links,
                "staff": "",
            }
        if kind in ("event", "tag"):
            from dddb.db import Store as _Store
            if self.db_path is None:
                return {"name": name}
            store = _Store(self.db_path)
            try:
                if kind == "event":
                    for item in store.list_events():
                        if str(item.get("name") or "").lower() == str(name).lower():
                            # **每个表单字段都要回填**, 少一个用户就以为"没保存"
                            _links = "\n".join(
                                "{}: {}".format(k, v)
                                for k, v in (item.get("links") or {}).items())
                            return {
                                "name": item.get("name") or name,
                                "kind": item.get("kind") or "",
                                "series": item.get("series") or "",
                                "vol_label": item.get("vol_label") or "",
                                "year": str(item.get("year") or ""),
                                "season": item.get("season") or "",
                                "start_date": item.get("start_date") or "",
                                "end_date": item.get("end_date") or "",
                                "tags": "、".join(item.get("tags") or []),
                                "links": _links,
                            }
                else:
                    for item in store.list_tags():
                        if str(item.get("name") or "").lower() == str(name).lower():
                            return {"name": item.get("name") or name,
                                    "kind": item.get("kind") or "",
                                    "note": item.get("note") or ""}
            finally:
                store.close()
            return {"name": name}
        return {"name": name}

    def _render_entity_edit(self, kind: str, name: str,
                            state: auth.AuthState, cookies: list[str],
                            notice: str = "") -> Response:
        if not self.can_edit(state):
            return self._html(self.forbidden(state, "只有编辑者可以编辑资料。"),
                              status=403, cookies=cookies)
        values = self._entity_form_values(kind, name)
        back = f'/{kind}/' + T.quote_segment(name)
        # 会次表单要: 系列下拉 + 已有图的预览
        series_options: list[dict[str, Any]] = []
        has_logo = False
        if kind == "event":
            from dddb.db import Store as _Store

            if self.db_path is not None:
                store = _Store(self.db_path)
                try:
                    series_options = [
                        {"name": x["name"], "abbr": str(x.get("abbr") or "")}
                        for x in store.list_series()
                    ]
                    row = store.query_one(
                        "SELECT logo_local FROM events WHERE name = ?", (name,))
                    has_logo = bool(row is not None
                                    and str(row["logo_local"] or ""))
                except Exception:            # noqa: BLE001
                    pass
                finally:
                    store.close()
        return self._html(T.edit_profile_page(
            self.context(state, active="edit"), kind,
            values["name"] if values else name,
            values=values, notice=notice, back_url=back,
            series_options=series_options, has_logo=has_logo),
            cookies=cookies)

    def _do_entity_update(self, kind: str, name: str, values: Mapping[str, Any],
                          state: auth.AuthState, cookies: list[str]) -> Response:
        """云端保存艺人/社团/展会/标签资料."""
        if not self.can_edit(state):
            return self._html(self.forbidden(state, "只有编辑者可以编辑资料。"),
                              status=403, cookies=cookies)
        if self.db_path is None:
            return self._html(self.forbidden(state, "本站当前不允许写入。"),
                              status=503, cookies=cookies)
        from dddb.db import Store as _Store
        from dddb.models import Circle
        store = _Store(self.db_path)
        try:
            # 会次图: 先落盘(会写回 events.logo_local), 记下路径供后面复用
            _new_logo = ""
            if kind == "event" and isinstance(values.get("logo"), Mapping):
                _new_logo = self._save_event_logo_file(name, values)
            if kind == "artist":
                ok, message = store.upsert_artist(name, {
                    "name_en": text_field(values, "name_en").strip(),
                    "name_ja": text_field(values, "name_ja").strip(),
                    "real_name": text_field(values, "real_name").strip(),
                    "real_name_en": text_field(values, "real_name_en").strip(),
                    "aliases": self._split_names(text_field(values, "aliases")),
                    "gender": text_field(values, "gender").strip(),
                    "birthday": text_field(values, "birthday").strip(),
                    "occupations_text": text_field(values, "occupations_text").strip(),
                    "nationality": text_field(values, "nationality").strip().upper()[:2],
                    "bio": text_field(values, "bio").strip(),
                    "avatar": text_field(values, "avatar").strip(),
                    "links": [{"href": url, "text": label}
                              for label, url in
                              self._parse_urls(text_field(values, "links")).items()],
                })
                if not ok:
                    return self._html(self.forbidden(state, message), status=400,
                                      cookies=cookies)
            elif kind == "circle":
                store.upsert_circle(Circle.from_dict({
                    "name": name,
                    "activity_type": text_field(values, "activity_type").strip(),
                    "founded": text_field(values, "founded").strip(),
                    "last_active": text_field(values, "last_active").strip(),
                    "description": text_field(values, "description").strip(),
                    "urls": self._parse_urls(text_field(values, "urls")),
                }))
            elif kind == "event":
                try:
                    year = int(text_field(values, "year").strip() or 0)
                except ValueError:
                    year = 0
                # 图片/链接: 表单没带这两个字段时(老表单)保留库里的原值,
                # 否则一次普通保存会把已有的图和链接清掉。
                _prev = next((e for e in store.list_events()
                              if str(e.get("name")) == name), {}) or {}
                _has_links_field = "links" in values
                # 系列/会次号: 表单没带就保留原值
                _info = parse_event_name(name)
                _series = (text_field(values, "series").strip()
                           if "series" in values
                           else str(_prev.get("series") or _info["series"]))
                _vol_label = (text_field(values, "vol_label").strip()
                              if "vol_label" in values
                              else str(_prev.get("vol_label")
                                       or _info["vol_label"]))
                # 会次号: 优先用标签里的数字; 没标签就按名字解析
                _vol = (vol_from_label(_vol_label)
                        or _info["vol"]
                        or int(_prev.get("vol") or 0))
                # 会次标签/季节: 空则由系列 + 会次号**自动填**
                if not _vol_label:
                    _vol_label = (label_from_vol(_series, _vol)
                                  or _info["vol_label"])
                # 季节/年份: 表单没填就按**发售日月份**推(用户选的规则)
                _season = text_field(values, "season").strip()
                _start = text_field(values, "start_date").strip()
                if not _season:
                    _season = season_from_date(_start)
                if not year:
                    year = year_from_date(_start)
                # 图片: 有上传就用新路径, 否则保留原值
                _logo_local = _new_logo or str(_prev.get("logo_local") or "")
                store.save_event(
                    name,
                    # kind 已从表单移除(与系列重复), 保留库里原值
                    kind=str(_prev.get("kind") or ""),
                    year=year, season=_season,
                    start_date=text_field(values, "start_date").strip(),
                    end_date=text_field(values, "end_date").strip(),
                    # tags 已并入「会次标签」, 表单不再有这个字段 ——
                    # 没带就保留库里原值, 不能清空
                    tags=(self._split_names(text_field(values, "tags"))
                          if "tags" in values
                          else list(_prev.get("tags") or [])),
                    logo_url=str(_prev.get("logo_url") or ""),
                    logo_local=_logo_local,
                    links=(self._parse_urls(text_field(values, "links"))
                           if _has_links_field
                           else dict(_prev.get("links") or {})),
                    series=_series, vol=_vol, vol_label=_vol_label)
            elif kind == "tag":
                ok, message = store.save_tag(
                    name, kind=text_field(values, "kind").strip(),
                    note=text_field(values, "note").strip())
                if not ok:
                    return self._html(self.forbidden(state, message), status=400,
                                      cookies=cookies)
            else:
                return Response.redirect("/edit", status=303)
        finally:
            store.close()
        if hasattr(self.data, "invalidate"):
            self.data.invalidate()
        if state.session is not None:
            state.session.flash = "资料已保存"
        return Response.redirect(f'/{kind}/' + T.quote_segment(name), status=303)

    def page_artists(self, params: Mapping[str, Any],
                     state: auth.AuthState | None = None) -> str:
        """艺术家索引: 按参与专辑数排序 + 搜索."""
        query = _first(params, "q").strip()
        page = _first(params, "page") or 1
        per = 90
        entries = self.data.artists_all()
        if query:
            needle = query.lower()
            entries = [e for e in entries
                       if needle in e["name"].lower()
                       or any(needle in c.lower() for c in e["circles"])]
        try:
            page_no = max(1, int(page))
        except (TypeError, ValueError):
            page_no = 1
        total = len(entries)
        start = (page_no - 1) * per
        views = entries[start:start + per]
        # 关注数(只对当前页批量查)
        for view in views:
            view["followers"] = self.profile_call(
                "followers_count", "artist", view["name"], default=0)
        pager = {"page": page_no, "per": per, "total": total,
                 "pages": max(1, math.ceil(total / per))}
        return T.artists_index_page(
            self.context(state, active="artists"),
            artists=views, pager=pager, query=query,
        )

    def page_artist(self, name: str, params: Mapping[str, Any],
                    state: auth.AuthState | None = None) -> tuple[int, str]:
        """艺术家详情(三栏) + 关注 + 评论."""
        artist = self.data.artist_detail(name)
        if artist is None:
            return 404, T.error_page(
                self.context(state, active="artists"), status=404,
                message=f"没有这位艺术家: {name}")
        uid = self._member_uid(state) if state is not None else 0
        following = bool(uid and self.profile_call(
            "is_following", uid, "artist", artist["name"], default=False))
        followers = self.profile_call(
            "followers_count", "artist", artist["name"], default=0)
        comments, _pager = self.data.list_comments("artist", artist["name"], per=50)
        comment_ids = [int(c.get("id") or 0) for c in comments if c.get("id")]
        likes: dict[int, int] = {}
        mine: set[int] = set()
        if self.db_path is not None and comment_ids:
            from dddb.db import Store as _Store
            _s = _Store(self.db_path)
            try:
                likes = _s.comment_like_counts(comment_ids)
                if uid and state is not None and state.session is not None:
                    mine = _s.liked_comment_ids(state.session.username, comment_ids)
            finally:
                _s.close()
        return 200, T.artist_detail_page(
            self.context(state, active="artists", search_value=artist["name"]),
            artist=artist, following=following, followers=followers,
            comments=comments, comment_likes=likes, liked_ids=sorted(mine),
        )

    def _do_circle_comment(self, values: Mapping[str, Any], state: auth.AuthState,
                           cookies: list[str]) -> Response:
        """POST /circle/<name>/comment: 给社团留言."""
        name = text_field(values, "key").strip()
        uid = self._member_uid(state)
        circle = self.data.circle_detail(name) if name else None
        if circle is None:
            return self._html(self.error_page(self.context(state), status=404,
                                              message=f"没有这个社团: {name}"),
                              status=404, cookies=cookies)
        if self.db_path is None:
            return self._html(self.forbidden(state, "本站当前不允许写入评论。"),
                              status=503, cookies=cookies)
        from dddb.db import Store as _Store
        store = _Store(self.db_path)
        try:
            ok, message, _cid = store.add_comment(
                target_kind="circle", target_key=circle["name"], user_id=uid,
                username=state.session.username if state.session else "",
                body=text_field(values, "body"))
        finally:
            store.close()
        if not ok:
            return self._html(self.forbidden(state, message), status=400, cookies=cookies)
        if state.session is not None:
            state.session.flash = "评论已发表"
        return Response.redirect("/circle/" + T.quote_segment(circle["name"]), status=303)

    def _do_artist_comment(self, values: Mapping[str, Any], state: auth.AuthState,
                           cookies: list[str]) -> Response:
        """POST /artist/<name>/comment: 给艺术家留言."""

        name = text_field(values, "key").strip()
        uid = self._member_uid(state)
        artist = self.data.artist_detail(name) if name else None
        if artist is None:
            return self._html(self.error_page(self.context(state), status=404,
                                              message=f"没有这位艺术家: {name}"),
                              status=404, cookies=cookies)
        if self.db_path is None:
            return self._html(self.forbidden(state, "本站当前不允许写入评论。"),
                              status=503, cookies=cookies)
        from dddb.db import Store as _Store
        store = _Store(self.db_path)
        try:
            ok, message, _cid = store.add_comment(
                target_kind="artist", target_key=artist["name"], user_id=uid,
                username=state.session.username if state.session else "",
                body=text_field(values, "body"))
        finally:
            store.close()
        if not ok:
            return self._html(self.forbidden(state, message), status=400, cookies=cookies)
        if state.session is not None:
            state.session.flash = "评论已发表"
        return Response.redirect("/artist/" + T.quote_segment(artist["name"]), status=303)

    def page_circles(self, params: Mapping[str, Any], state: auth.AuthState | None = None) -> str:
        query = _first(params, "q")
        page = _first(params, "page") or 1
        entries, pager = self.data.circles(query=query, page=page, per=_PER_CIRCLES)
        url_for = T.make_url_for("/circles", {"q": query} if query else {})
        return T.circles_index_page(
            self.context(state, active="circles"),
            circles=entries,
            pager=pager,
            url_for=url_for,
            query=query,
        )

    def page_circle(self, name: str, params: Mapping[str, Any], state: auth.AuthState | None = None) -> str:
        detail = self.data.circle_detail(name)
        page = _first(params, "page") or 1
        per = _first(params, "per") or DEFAULT_PER
        sort = _first(params, "sort") or DEFAULT_SORT
        if sort not in dict(SORTS):
            sort = DEFAULT_SORT
        if detail is None:
            return T.error_page(
                self.context(state, active="circles"), status=404,
                message=f"没有这个社团: {name}")
        circle_name = str(detail["name"])
        uid = self._member_uid(state) if state is not None else 0
        following = bool(uid and self.profile_call(
            "is_following", uid, "circle", circle_name, default=False))
        followers = self.profile_call(
            "followers_count", "circle", circle_name, default=0)
        comments, _pager = self.data.list_comments("circle", circle_name, per=50)
        comment_ids = [int(c.get("id") or 0) for c in comments if c.get("id")]
        likes: dict[int, int] = {}
        mine: set[int] = set()
        if self.db_path is not None and comment_ids:
            from dddb.db import Store as _Store
            _s = _Store(self.db_path)
            try:
                likes = _s.comment_like_counts(comment_ids)
                if uid and state is not None and state.session is not None:
                    mine = _s.liked_comment_ids(state.session.username, comment_ids)
            finally:
                _s.close()
        return T.circle_detail_page(
            self.context(state, active="circles", search_value=circle_name),
            circle=detail, following=following, followers=followers,
            comments=comments, comment_likes=likes, liked_ids=sorted(mine),
        )

    def page_events(self, state: auth.AuthState | None = None,
                    params: Mapping[str, Any] | None = None) -> str:
        """展会索引. 支持 ``?sort=time|count &order=desc|asc``, 默认最新在前."""
        params = params or {}
        sort = str(_first(params, "sort") or "time").strip().lower()
        if sort not in ("time", "count"):
            sort = "time"
        order = str(_first(params, "order") or "desc").strip().lower()
        if order not in ("asc", "desc"):
            order = "desc"
        return T.events_index_page(
            self.context(state, active="events"),
            events=self.data.events(),
            kinds=self.data.event_kinds(),
            series=self.data.event_series(sort=sort, order=order),
            sort=sort,
            order=order,
        )

    def page_event_series_edit(self, name: str,
                               params: Mapping[str, Any],
                               state: auth.AuthState) -> Response:
        """GET ``/event-series/<name>/edit`` 与 ``/event-series/new``."""
        if not self.can_edit(state):
            return self._html(self.forbidden(state, "只有编辑者可以编辑展会系列。"),
                              status=403)
        creating = not str(name or "").strip()
        series: dict[str, Any] = {}
        if not creating:
            series = next((x for x in self.data.event_series()
                           if x["name"] == name), None) or {}
            if not series:
                return self._html(
                    T.error_page(self.context(state, active="events"),
                                 status=404,
                                 message=f"没有这个展会系列: {name}"),
                    status=404)
        return self._html(T.event_series_edit_page(
            self.context(state, active="events"), series=series,
            creating=creating))

    def _do_series_save(self, name: str, values: Mapping[str, Any],
                        state: auth.AuthState, cookies: list[str],
                        headers: Mapping[str, Any] | None = None,
                        body: bytes = b"") -> Response:
        """POST ``/event-series/<name>/edit`` / ``/new``: 保存系列(含图片上传)."""
        if not self.can_edit(state):
            return self._html(self.forbidden(state, "只有编辑者可以保存。"),
                              status=403, cookies=cookies)
        if self.db_path is None:
            return self._html(self.forbidden(state, "本站当前不允许写入。"),
                              status=503, cookies=cookies)
        content_type = str((headers or {}).get("content-type") or "")
        if content_type.startswith("multipart/form-data") and body:
            values = {**dict(values),
                      **self._values_from_multipart(headers, body)}

        from dddb.db import Store as _Store

        target_name = (text_field(values, "name").strip()
                       or str(name or "").strip())
        if not target_name:
            return self._html(self.forbidden(state, "系列名不能为空。"),
                              status=400, cookies=cookies)
        store = _Store(self.db_path)
        try:
            prev = (next((x for x in store.list_series()
                          if x["name"] == target_name), None) or {})
            # 图片: 新上传优先, 否则沿用原值
            logo_local = str(prev.get("logo_local") or "")
            poster_local = str(prev.get("poster_local") or "")
            for kind, key in (("logo", "logo_local"), ("poster", "poster_local")):
                saved = self._save_series_image(target_name, kind, values)
                if saved:
                    if key == "logo_local":
                        logo_local = saved
                    else:
                        poster_local = saved
            store.save_series(
                target_name,
                abbr=text_field(values, "abbr").strip(),
                full_name=text_field(values, "full_name").strip(),
                logo_url=text_field(values, "logo_url").strip(),
                logo_local=logo_local,
                poster_url=text_field(values, "poster_url").strip(),
                poster_local=poster_local,
                wiki=raw_field(values, "wiki"),
                links=self._parse_urls(text_field(values, "links")),
            )
        finally:
            store.close()
        if hasattr(self.data, "invalidate"):
            self.data.invalidate()
        if state.session is not None:
            state.session.flash = "展会系列已保存"
        return Response.redirect(
            "/event-series/" + T.quote_segment(target_name), status=303)

    def _save_event_logo_file(self, name: str,
                              values: Mapping[str, Any]) -> str:
        """保存会次图片(``name="logo"``), 写回 ``events.logo_local``.

        与会系列图片同一套路径(covers/ 下), 失败只记日志 ——
        图存不下来不该让会次创建失败。
        """
        raw = values.get("logo")
        if not isinstance(raw, Mapping):
            return ""
        blob = raw.get("data") or b""
        if not blob or len(blob) > MAX_IMAGE_BODY:
            return ""
        mime = str(raw.get("content_type") or "").split(";")[0].strip().lower()
        ext = {"image/png": ".png", "image/jpeg": ".jpg",
               "image/webp": ".webp"}.get(mime)
        if ext is None:
            return ""
        covers = getattr(self.data, "covers_dir", None)
        base = Path(covers) if covers else Path(self.db_path).parent / "covers"
        folder = base / "events"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            stem = re.sub(r"[^A-Za-z0-9._-]+", "-", str(name))[:80] or "event"
            target = folder / "{}{}".format(stem, ext)
            target.write_bytes(blob)
        except OSError:                      # pragma: no cover
            return ""
        if self.db_path is None:
            return str(target)
        from dddb.db import Store as _Store

        store = _Store(self.db_path)
        try:
            row = store.query_one(
                "SELECT * FROM events WHERE name = ?", (str(name),))
            if row is not None:
                keys = row.keys()

                def _v(key: str, default: Any = "") -> Any:
                    return row[key] if key in keys else default

                try:
                    _tags = json.loads(_v("tags", "[]") or "[]")
                except ValueError:
                    _tags = []
                try:
                    _links = json.loads(_v("links_json", "{}") or "{}")
                except ValueError:
                    _links = {}
                store.save_event(
                    str(name), kind=str(_v("kind")),
                    year=int(_v("year", 0) or 0),
                    season=str(_v("season")),
                    start_date=str(_v("start_date")),
                    end_date=str(_v("end_date")),
                    tags=_tags if isinstance(_tags, list) else [],
                    logo_url=str(_v("logo_url")),
                    logo_local=str(target),
                    links=_links if isinstance(_links, dict) else {},
                    series=str(_v("series")),
                    vol=int(_v("vol", 0) or 0),
                    vol_label=str(_v("vol_label")))
        finally:
            store.close()
        try:
            self.data.invalidate()
        except Exception:                    # noqa: BLE001
            pass
        return str(target)

    def _save_series_image(self, name: str, kind: str,
                           values: Mapping[str, Any]) -> str:
        """保存系列图片(logo/poster), 返回落盘路径; 没传返回空串."""
        raw = values.get(kind)
        if not isinstance(raw, Mapping):
            return ""
        blob = raw.get("data") or b""
        if not blob or len(blob) > MAX_IMAGE_BODY:
            return ""
        mime = str(raw.get("content_type") or "").split(";")[0].strip().lower()
        ext = {"image/png": ".png", "image/jpeg": ".jpg",
               "image/webp": ".webp"}.get(mime)
        if ext is None:
            return ""
        covers = getattr(self.data, "covers_dir", None)
        base = Path(covers) if covers else Path(self.db_path).parent / "covers"
        folder = base / "series"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            stem = re.sub(r"[^A-Za-z0-9._-]+", "-", name)[:80] or "series"
            target = folder / "{}-{}{}".format(stem, kind, ext)
            target.write_bytes(blob)
            return str(target)
        except OSError:                      # pragma: no cover
            return ""

    def page_event_series(self, name: str, params: Mapping[str, Any],
                          state: auth.AuthState | None = None) -> str:
        """``/event-series/<name>``: 系列详情(大图 + wiki + 会次网格)."""
        wanted = str(name or "").strip()
        if not wanted:
            return T.error_page(self.context(state, active="events"),
                                status=404, message="没有指定展会系列。")
        series = None
        for item in self.data.event_series():
            if item["name"] == wanted:
                series = item
                break
        if series is None:
            return T.error_page(self.context(state, active="events"),
                                status=404,
                                message=f"没有这个展会系列: {wanted}")
        # 会次补齐图片: 会次自己的 logo, 没有就继承系列的
        editions = []
        by_name = {e["value"]: e for e in self.data.events()}
        for ed in series.get("editions") or ():
            row = dict(ed)
            live = by_name.get(row["value"]) or {}
            row["logo"] = str(live.get("logo") or "") or str(series.get("logo") or "")
            editions.append(row)
        return T.event_series_page(
            self.context(state, active="events", search_value=wanted),
            series=series,
            editions=editions,
            can_edit=self.can_edit(state),
        )

    def serve_series_image(self, name: str, kind: str = "logo") -> Response:
        """``/event-series-logo/<name>`` / ``-poster``: 系列图片.

        ``kind``: ``logo`` 或 ``poster``。本地有文件就发字节, 否则 302 到远程 URL。
        """
        wanted = str(name or "").strip()
        if not wanted or self.db_path is None:
            return self._plain_404()
        local = ""
        remote = ""
        try:
            from dddb.db import Store as _Store

            store = _Store(self.db_path)
            try:
                row = store.query_one(
                    "SELECT * FROM event_series WHERE name = ?", (wanted,))
            finally:
                store.close()
            if row is not None:
                keys = row.keys()
                local_key = "{}_local".format(kind)
                url_key = "{}_url".format(kind)
                local = str(row[local_key] or "") if local_key in keys else ""
                remote = str(row[url_key] or "") if url_key in keys else ""
        except Exception:                    # noqa: BLE001
            local = remote = ""
        if local:
            path = Path(local)
            if path.is_file():
                try:
                    blob = path.read_bytes()
                except OSError:
                    blob = b""
                if blob:
                    resp = Response.binary(
                        blob, sniff_image_type(blob) or "image/png", status=200)
                    resp.headers["Cache-Control"] = "public, max-age=86400"
                    return resp
        safe = T.safe_url(remote, allow_relative=False)
        if safe:
            return Response.redirect(safe)
        return self._plain_404()

    def page_event(self, name: str, params: Mapping[str, Any], state: auth.AuthState | None = None) -> str:
        group_by = "date" if _first(params, "group") == "date" else "circle"
        page = _first(params, "page") or 1
        groups, pager, total = self.data.event_groups(
            name, group_by=group_by, page=page, per_groups=_PER_GROUPS, per_group=_PER_GROUP
        )
        base = "/event/" + T.quote_segment(name)
        url_for = T.make_url_for(base, {"group": group_by})
        kind = ""
        logo = ""
        links: dict[str, str] = {}
        for event in self.data.events():
            if event["value"] == name:
                kind = str(event.get("kind") or "")
                logo = str(event.get("logo") or "")
                links = dict(event.get("links") or {})
                break
        return T.event_page(
            self.context(state, active="events", search_value=name),
            name=name,
            kind=kind,
            logo=logo,
            links=links,
            items=[],
            groups=groups,
            group_by=group_by,
            total=total,
            url_for=url_for,
        )

    def page_tags(self, state: auth.AuthState | None = None) -> str:
        tags = self.data.tags(limit=400)
        return T.tags_index_page(
            self.context(state, active="tags"),
            tags=tags,
            limit_note="展示收录数量前 {} 个标签".format(len(tags)) if tags else "",
        )

    def page_tag(self, tag: str, params: Mapping[str, Any], state: auth.AuthState | None = None) -> str:
        page = _first(params, "page") or 1
        per = _first(params, "per") or DEFAULT_PER
        sort = _first(params, "sort") or DEFAULT_SORT
        if sort not in dict(SORTS):
            sort = DEFAULT_SORT
        views, pager = self.data.query_albums(page=page, per=per, sort=sort, tag=tag)
        url_for = self._list_url_for("/tag/" + T.quote_segment(tag), {"tag": tag}, per)
        return T.tag_page(
            self.context(state, active="tags", search_value=tag),
            tag=tag,
            items=views,
            pager=pager,
            url_for=url_for,
        )

    def page_edit(self, kind: str, params: Mapping[str, Any], state: auth.AuthState | None = None) -> str:
        page = _first(params, "page") or 1
        per = _first(params, "per") or DEFAULT_PER
        kinds = self.data.needs_counts()
        items, pager = self.data.inbox(kind, page=page, per=per)
        base = "/edit" + (f"/{kind}" if kind else "")
        url_for = self._list_url_for(base, {}, per)
        return T.inbox_page(
            self.context(state, active="edit"),
            kinds=kinds,
            items=items,
            pager=pager,
            active_kind=kind,
            url_for=url_for,
            # 编辑入口是**站内**的 /album/<key>/edit, 不再指向任何别的站点
            edit_url_for=lambda key: f"/album/{T.quote_segment(key)}/edit",
        )

    # -- 静态与封面 ---------------------------------------------------------

    def serve_static(self, name: str) -> Response:
        """只发白名单里的文件; ``name`` 里的路径部分一律丢弃(防目录穿越)."""
        plain = Path(str(name or "")).name
        if plain not in STATIC_FILES:
            return Response.html(self.static_missing_page(), status=404)
        target = STATIC_DIR / plain
        if not target.is_file():
            return Response.html(self.static_missing_page(), status=404)
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or plain.endswith((".svg", ".css")):
            ctype += "; charset=utf-8"
        return Response.binary(target.read_bytes(), ctype, status=200)

    def static_missing_page(self) -> str:
        return T.error_page(self.context(), status=404, message="静态资源不存在")

    # -- robots.txt / sitemap.xml -------------------------------------------

    def base_url_for(self, headers: Mapping[str, Any] | None = None) -> str:
        """站点对外绝对地址: 配置 ``[public] base_url`` 优先, 否则按请求头推.

        反代后面(Caddy)由 ``X-Forwarded-Proto`` 告诉我们真实协议; 只有
        ``behind_proxy = true`` 时才信它 —— 否则任何人都能让 sitemap 里出现
        伪造的 https 地址。Host 头做白名单校验, 不让 XML 里塞进脏字符。
        """
        if self.base_url:
            return self.base_url
        lowered = {str(k).lower(): v for k, v in (headers or {}).items()}
        host = str(lowered.get("host") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9._\-]+(:\d+)?", host or ""):
            return ""
        scheme = "http"
        if self.trust_proxy:
            proto = str(lowered.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
            if proto in ("http", "https"):
                scheme = proto
        return f"{scheme}://{host}"

    def robots_text(self, headers: Mapping[str, Any] | None = None) -> str:
        """本站自己的 robots.txt: 浏览页允许抓, 编辑/账号/搜索不允许."""
        base = self.base_url_for(headers)
        lines = [
            "# 同人专辑档案 —— 只读浏览页欢迎抓取; 编辑与账号路径不允许收录。",
            "User-agent: *",
            "Allow: /",
        ]
        lines.extend(f"Disallow: {path}" for path in _ROBOTS_DISALLOW)
        lines.append(f"Crawl-delay: {_ROBOTS_CRAWL_DELAY}")
        if base:
            lines.append("")
            lines.append(f"Sitemap: {base}/sitemap.xml")
        return "\n".join(lines) + "\n"

    def sitemap_entries(self) -> list[dict[str, Any]]:
        """sitemap 的 URL 条目(库变了会自动过期重建)."""
        cache_ttl = self.sitemap_cache_ttl

        def build() -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = [
                {"path": "/", "lastmod": "", "changefreq": "daily", "priority": "1.0"},
                {"path": "/albums", "lastmod": "", "changefreq": "daily", "priority": "0.9"},
                {"path": "/events", "lastmod": "", "changefreq": "weekly", "priority": "0.8"},
                {"path": "/circles", "lastmod": "", "changefreq": "weekly", "priority": "0.8"},
                {"path": "/tags", "lastmod": "", "changefreq": "weekly", "priority": "0.7"},
                {"path": "/stats", "lastmod": "", "changefreq": "monthly", "priority": "0.5"},
                {"path": "/about", "lastmod": "", "changefreq": "monthly", "priority": "0.3"},
            ]
            for key, updated in self.data.album_urls(limit=self.sitemap_max_urls):
                out.append({
                    "path": "/album/" + T.quote_segment(key),
                    "lastmod": _lastmod(updated),
                    "changefreq": "monthly",
                    "priority": "0.7",
                })
            for circle in self.data.circles_all():
                value = str(circle.get("value") or "").strip()
                if value:
                    out.append({
                        "path": "/circle/" + T.quote_segment(value),
                        "lastmod": "", "changefreq": "weekly", "priority": "0.6",
                    })
            for event in self.data.events():
                value = str(event.get("value") or "").strip()
                if value:
                    out.append({
                        "path": "/event/" + T.quote_segment(value),
                        "lastmod": "", "changefreq": "weekly", "priority": "0.6",
                    })
            return out

        return self.data.cached("sitemap_entries", build, ttl=cache_ttl)

    def serve_sitemap(self, index: str, headers: Mapping[str, Any] | None = None) -> Response:
        """``/sitemap.xml`` 或分片 ``/sitemap-<n>.xml``.

        条目数超过 :attr:`sitemap_max_urls` 时, ``/sitemap.xml`` 变成
        **sitemap 索引**, 各分片在 ``/sitemap-<n>.xml``(Google/必应都认这种写法)。
        """
        ctype = "application/xml; charset=utf-8"
        base = self.base_url_for(headers)
        if not base:
            return Response.binary(b"", ctype, status=404)
        entries = self.sitemap_entries()
        shards = max(1, math.ceil(len(entries) / self.sitemap_max_urls))
        plain = str(index or "") == ""
        if shards > 1:
            if plain:
                xml = self._sitemap_index_xml(base, shards)
            else:
                try:
                    number = int(str(index))
                except ValueError:
                    return Response.binary(b"", ctype, status=404)
                if number < 0 or number >= shards:
                    return Response.binary(b"", ctype, status=404)
                xml = self._urlset_xml(base, entries[number * self.sitemap_max_urls:
                                                      (number + 1) * self.sitemap_max_urls])
        else:
            if not plain:
                return Response.binary(b"", ctype, status=404)
            xml = self._urlset_xml(base, entries)
        return Response.binary(xml.encode("utf-8"), ctype, status=200)

    def _sitemap_index_xml(self, base: str, shards: int) -> str:
        parts = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
        ]
        for number in range(shards):
            loc = f"{base}/sitemap-{number}.xml"
            parts.append(f"  <sitemap><loc>{xml_escape(loc)}</loc></sitemap>")
        parts.append("</sitemapindex>")
        return "\n".join(parts) + "\n"

    def _urlset_xml(self, base: str, entries: Sequence[Mapping[str, Any]]) -> str:
        parts = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
        ]
        for entry in entries:
            loc = f"{base}{entry.get('path') or '/'}"
            parts.append("  <url>")
            parts.append(f"    <loc>{xml_escape(loc)}</loc>")
            lastmod = str(entry.get("lastmod") or "")
            if lastmod:
                parts.append(f"    <lastmod>{xml_escape(lastmod)}</lastmod>")
            changefreq = str(entry.get("changefreq") or "")
            if changefreq:
                parts.append(f"    <changefreq>{xml_escape(changefreq)}</changefreq>")
            priority = str(entry.get("priority") or "")
            if priority:
                parts.append(f"    <priority>{xml_escape(priority)}</priority>")
            parts.append("  </url>")
        parts.append("</urlset>")
        return "\n".join(parts) + "\n"

    def serve_logo_image(self, name: str) -> Response:
        """``/logo/<文件名>``: 艺人头像 / 社团徽标.

        新建条目时 ``_save_new_image`` 把图片写进 ``<covers>/logos/`` 并把
        ``/logo/<kind>-<name>.<ext>`` 写进条目; 这里把它读回来。
        只认**单段文件名**(没有 ``/``、不是 ``..``), 杜绝路径穿越。
        """
        wanted = str(name or "").strip()
        if not wanted:
            return self._plain_404()
        filename = wanted.rsplit("/", 1)[-1]
        if not filename or filename in (".", ".."):
            return self._plain_404()
        if "/" in filename or "\\" in filename or "%2f" in filename.lower():
            return self._plain_404()
        covers = getattr(self.data, "covers_dir", None)
        if covers:
            base = Path(covers)
        elif self.db_path is not None:
            base = Path(self.db_path).parent / "covers"
        else:
            base = Path("covers")
        path = base / "logos" / filename
        try:
            if not path.is_file():
                return self._plain_404()
            blob = path.read_bytes()
        except OSError:
            return self._plain_404()
        if not blob:
            return self._plain_404()
        ctype = sniff_image_type(blob) or "image/png"
        resp = Response.binary(blob, ctype, status=200)
        resp.headers["Cache-Control"] = "public, max-age=86400"
        return resp

    def serve_event_logo(self, name: str) -> Response:
        """``/event-logo/<展会名>``: 展会词条的本地图片.

        本地没有落盘图时 302 到 ``logo_url``(若配了), 两者都没有就 404。
        与封面一样: 页面上引用的是本站路径, 不是源站地址。
        """
        wanted = str(name or "").strip()
        if not wanted:
            return self._plain_404()
        logo_url = ""
        logo_local = ""
        if self.db_path is not None:
            try:
                from dddb.db import Store as _Store

                store = _Store(self.db_path)
                try:
                    row = store.query_one(
                        "SELECT logo_url, logo_local FROM events WHERE name = ?",
                        (wanted,))
                finally:
                    store.close()
                if row is not None:
                    keys = row.keys()
                    logo_url = str(row["logo_url"] or "") if "logo_url" in keys else ""
                    logo_local = (str(row["logo_local"] or "")
                                  if "logo_local" in keys else "")
            except Exception:                 # noqa: BLE001 - 老库没有 events 表
                logo_url = logo_local = ""
        # 优先本地文件
        if logo_local:
            path = Path(logo_local)
            if path.is_file():
                try:
                    blob = path.read_bytes()
                except OSError:
                    blob = b""
                if blob:
                    ctype = sniff_image_type(blob) or "image/png"
                    resp = Response.binary(blob, ctype, status=200)
                    resp.headers["Cache-Control"] = "public, max-age=86400"
                    return resp
        # 退回远程地址(302)
        safe = T.safe_url(logo_url, allow_relative=False)
        if safe:
            return Response.redirect(safe)
        return self._plain_404()

    def serve_embed(self, arg: str) -> Response:
        """``/embed/<album key>/<序号>`` -> 302 到 embed code 里的播放器地址.

        **只接管 embed code**: 纯链接没有播放器(用户要求), 所以这里也只在
        该外链的值里确实有 ``<iframe src=...>`` 时才跳转, 其余一律 404。

        **为什么走中转**: 与 ``/link/`` 同样的合规要求 —— 页面 HTML 里不能出现
        外部 URL / 域名, iframe 的 src 只能是本站路径。这里按 ``view.urls`` 的
        顺序取第 n 条, 从 embed code 里抽出 src 后 302 过去。

        被禁站点(dddb/linkpolicy.py)连嵌入也不行; 非 http(s) 的 src 一并拒绝 ——
        不给任意 URL 当开放重定向用。
        """
        from . import embed as _embed

        key, _, index_text = str(arg or "").rpartition("/")
        if not key or not index_text.isdigit():
            return self._plain_404()
        position = int(index_text)
        try:
            album = self.data.get_album(key)
        except Exception:                       # noqa: BLE001
            return self._plain_404()
        if not album:
            return self._plain_404()
        rows = list(album.get("urls") or [])
        if position < 1 or position > len(rows):
            return self._plain_404()
        target = rows[position - 1]
        raw = str((target or {}).get("url") or "")
        # 只认 embed code; 纯链接没有播放器
        info = _embed.extract_embed(raw)
        if not info:
            return self._plain_404()
        destination = _embed.embed_target(info.get("src"))
        if not destination:
            return self._plain_404()
        return Response.redirect(destination)


    def serve_outbound_link(self, arg: str) -> Response:
        """``/link/<album key>/<序号>`` -> 302 到专辑的某条外链.

        为什么绕这一下: 用户端承诺"页面 HTML 里不出现资料来源"。如果直接把
        ``https://<来源站>/...`` 写进 href, 等于把资料来源印在页面上; 走本站中转
        之后页面只有 ``/link/...``, 点出去仍然是真实外链。

        **合规(第二道闸门)**: 指向其它数据网站的一律拒绝(404)。
        具体名单在 ``dddb/linkpolicy.py`` —— 它刻意放在 public 包外, 免得用户端
        代码里出现来源名(``tests/test_public_events_labels.py`` 有目录级守卫)。
        封面可以缓存后再分发,
        但不得把访客导向那些站点的条目页。序号与详情页共用
        :meth:`PublicData.outbound_link_urls`, 所以两边永远对齐。
        """
        key, _, index_text = str(arg or "").rpartition("/")
        if not key or not index_text.isdigit():
            return self._link_refused("没有这条外部链接。")
        urls = self.data.outbound_link_urls(key)
        position = int(index_text)
        if position < 1 or position > len(urls):
            return self._link_refused("没有这条外部链接。")
        url = urls[position - 1]
        reason = outbound_link_reject_reason(url)
        if reason:
            # 拒绝理由只进日志 —— 不写进 HTML, 免得把被拒的域名印在页面上。
            logger.warning("拒绝外链跳转 %s #%d: %s", key, position, reason)
            return self._link_refused("这个外部链接指向其它数据网站, 本站不提供跳转。")
        return Response.redirect(url)

    def _link_refused(self, message: str) -> Response:
        """外链被拒时给一个解释性的 404 页(不暴露目标域名)."""
        return Response.html(
            T.error_page(self.context(), status=404, message=message), status=404,
        )

    # -- 封面 ---------------------------------------------------------------

    def external_cover_url(self, key: str) -> str:
        """外接对象存储/CDN 上的封面地址(没配或 key 不可用 -> 空串).

        基址里可以写 ``{key}`` 占位; 没写就按 ``<base>/<key>.img`` 拼
        (与本地缓存的落盘命名一致, 方便两边用同一套拷贝脚本)。
        """
        base = self.cover_base_url
        text = str(key or "").strip()
        if not base or not text or any(ch in text for ch in "/\\"):
            return ""
        quoted = urllib.parse.quote(text, safe="")
        target = base.replace("{key}", quoted) if "{key}" in base else f"{base}/{quoted}.img"
        parts = urllib.parse.urlsplit(target)
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            logger.warning("cover_base_url 不是合法的 http(s) 地址: %r", base)
            return ""
        return target

    def serve_cover(self, key: str) -> Response:
        """``/cover/<key>``: 本地缓存 -> 外接存储 -> 拉取落盘 -> 占位图.

        **绝不 302 到源站图片**(那是 hotlink 别人的图床)。用户明确的合规要求:
        封面只允许来自本地数据库缓存或我们自己的对象存储/CDN。

        * 本地缓存命中(``cover_local``, 或已经拉过盘的 ``<key>.img``) -> 200;
        * 配了 ``cover_base_url`` -> 302 到我们自己的对象存储;
        * 只有远程 ``cover_url`` -> 服务端拉取 + 落盘 ``<covers_dir>/<key>.img`` 后 200
          (走 HttpClient, 有超时; 失败回占位图);
        * 库里没有这张专辑 -> 404; 有专辑但没有可用封面 -> 内置占位图。
        """
        local, remote = self.data.cover_lookup(key)
        path = self.data.resolve_cover_path(local)
        if path is not None and path.is_file():
            data, ctype = self._read_cover(path)
            if data is not None:
                return self._cover_bytes(data, ctype, "local")
        external = self.external_cover_url(key)
        if external:
            return self._cover_redirect(external)
        remote_url = self._remote_cover_url(remote)
        if remote_url and self.cover_fetcher is not None:
            fetched = self.cover_fetcher.fetch(key, remote_url)
            if fetched is not None:
                data, ctype, source = fetched
                return self._cover_bytes(data, ctype, source)
            logger.info("封面拉取失败, 回占位图: %s <- %s", key, remote_url)
            return self._placeholder_response()
        if not self.data.album_exists(key):
            # 不存在的专辑(含 ``../../config/config.toml`` 这类穿越尝试)照旧 404
            return Response.binary(b"", "text/plain; charset=utf-8", status=404)
        return self._placeholder_response()

    def _remote_cover_url(self, remote: Any) -> str:
        """规范化 ``cover_url``: 只接受绝对的 http(s)(协议相对按 https 补)."""
        text = T.safe_url(remote, allow_relative=False)
        if not text:
            return ""
        if text.startswith("//"):
            text = "https:" + text
        parts = urllib.parse.urlsplit(text)
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            return ""
        return text

    def _cover_bytes(self, data: bytes, ctype: str, source: str) -> Response:
        """发封面字节, 带上"来源"标记(便于运维与测试断言)."""
        response = Response.binary(data, ctype, status=200)
        response.headers[COVER_SOURCE_HEADER] = source
        return response

    def _cover_redirect(self, target: str) -> Response:
        """302 到**我们自己的**对象存储/CDN(不是源站图片地址)."""
        response = Response.redirect(target)
        response.headers[COVER_SOURCE_HEADER] = "external"
        return response

    def _placeholder_response(self) -> Response:
        """内置占位图(200)。

        用 200 + 图片而不是 404: ``<img>`` 遇到 404 会显示破图; 这里明确
        "没有可用的封面", 同时 ``no-store`` 保证它**不会被当成真封面缓存**,
        下次请求仍会尝试重新拉取(拉取器那边还有 120 秒负缓存挡着)。
        """
        response = self._cover_bytes(placeholder_cover(), "image/png", "placeholder")
        response.headers["Cache-Control"] = "no-store"
        return response

    def drop_cover_cache(self, path: Path | str) -> None:
        """丢弃某个封面文件的字节缓存.

        **必须在上传/替换封面后调用**: 这台机器的文件系统 mtime 精度不足
        (实测连续写入的 mtime_ns 完全相同), 而 ``_read_cover`` 靠 mtime 判断
        缓存是否有效 —— 只依赖它会导致"换了封面页面还是旧图"。
        """
        try:
            key = str(path)
        except Exception:                # noqa: BLE001
            return
        lock = getattr(self, "_cover_lock", None)
        cache = getattr(self, "_cover_cache", None)
        if cache is None:
            return
        if lock is not None:
            with lock:
                cache.pop(key, None)
        else:
            cache.pop(key, None)

    def _read_cover(self, path: Path) -> tuple[bytes | None, str]:
        """读封面并缓存(按 mtime + 大小失效). 缓存字节, 省掉重复磁盘读.

        缓存签名用 ``(mtime_ns, size)`` 而不是只比 mtime —— 这台机器的
        文件系统 mtime 精度不足(实测连续写入 mtime_ns 相同), 只看 mtime
        会在"同尺寸覆盖"时命中旧缓存; 加上 size 能挡住绝大多数情况,
        真正的替换由上传逻辑显式调 :meth:`drop_cover_cache` 兜底。
        """
        try:
            stat = path.stat()
            mtime = stat.st_mtime_ns
            size = stat.st_size
        except OSError:
            return None, ""
        signature = (mtime, size)
        with self._cover_lock:
            hit = self._cover_cache.get(str(path))
            if hit is not None and hit[2] == signature:
                return hit[0], hit[1]
        try:
            data = path.read_bytes()
        except OSError as exc:
            logger.debug("读封面失败 %s: %s", path, exc)
            return None, ""
        ctype = sniff_image_type(data)
        with self._cover_lock:
            if len(self._cover_cache) >= self.cover_cache_size:
                self._cover_cache.clear()
            self._cover_cache[str(path)] = (data, ctype, signature)
        return data, ctype

    def close(self) -> None:
        """释放可写连接与会话库(测试收尾用)."""
        with self._write_lock:
            if self._write_store is not None:
                try:
                    self._write_store.close()
                except sqlite3.Error:  # pragma: no cover
                    pass
                self._write_store = None
        try:
            self.sessions.close()
        except (sqlite3.Error, auth.SessionStoreError) as exc:  # pragma: no cover
            logger.debug("关闭会话库失败: %s", exc)


def _safe_next(value: Any) -> str:
    """登录后的跳转目标: 只允许站内绝对路径(挡掉 ``//evil.com`` 这类开放跳转)."""
    text = str(value or "").strip()
    if not text.startswith("/") or text.startswith("//"):
        return ""
    return text


def _lastmod(value: Any) -> str:
    """时间戳 -> sitemap 用的 ``2026-10-24T15:03:00Z``(空值返回空串)."""
    try:
        stamp = float(value or 0)
    except (TypeError, ValueError):
        return ""
    if stamp <= 0:
        return ""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stamp))


def _stamp_text(value: Any, lang: Any = "zh") -> str:
    """时间戳 -> ``2026-10-24 15:03``(按**语言时区**格式化)."""
    from .data import human_time

    return human_time(value, lang)


# --------------------------------------------------------------------------
# 组装 / 起服务
# --------------------------------------------------------------------------


def resolve_sessions_path(
    root_path: Path,
    public_cfg: Any = None,
    override: Any = None,
) -> Path | None:
    """会话库路径: 命令行 > ``DNH_SESSIONS_DB`` > ``[public] sessions_db``.

    配置里显式留空(``sessions_db = ""``)= 内存会话(None)。
    """
    if override:
        return Path(override).resolve()
    env = os.environ.get("DNH_SESSIONS_DB", "").strip()
    if env:
        return Path(env).resolve()
    if public_cfg is None:
        # 没有配置对象(裸路径调用)时按默认位置来, 不静默退回内存会话
        return auth.env_sessions_path(root_path)
    value = str(getattr(public_cfg, "sessions_db", "") or "").strip()
    if not value:
        return None
    path = Path(value)
    return path.resolve() if path.is_absolute() else (root_path / path).resolve()


def build_site(
    config: Any = None,
    *,
    db_path: Path | str | None = None,
    root: Path | str | None = None,
    covers_dir: Path | str | None = None,
    users_db: Path | str | None = None,
    sessions_db: Path | str | None = None,
    visits_db: Path | str | None = None,
    admin_url: Any = None,
    limiter: RateLimiter | None = None,
    base_url: str | None = None,
    cover_base_url: str | None = None,
) -> PublicSite:
    """按配置(或裸路径)组装一个站点.

    ``admin_url`` 只是为了兼容 ``dddb/cli.py`` 里历史遗留的 ``--admin-url`` 参数
    (**已废弃**): 用户端不再有任何外部跳转, 传什么都会被忽略。

    ``cover_base_url``: 外接对象存储/CDN 的封面基址。留空则按
    :func:`resolve_cover_base_url` 从环境变量 / 配置里找; 都没配就走本地缓存
    (服务端拉取 + 落盘 ``data/covers/<key>.img``), 绝不 hotlink 源站。
    """
    if admin_url:  # pragma: no cover - 只有旧调用方会传
        logger.warning("admin_url 已废弃, 用户端不再显示任何外部入口")
    root_path = Path(root).resolve() if root else None
    if config is None:
        from ..config import Config  # 延迟导入: 只要渲染页面就不必读配置

        config = Config.load(root=root_path or project_root())
    if root_path is None:
        root_path = Path(getattr(config, "path", project_root())).resolve()
    public_cfg = getattr(config, "public", None)
    target_db = Path(db_path) if db_path else Path(config.db_path)
    if covers_dir is None:
        covers_dir = Path(getattr(config, "data_dir", root_path / "data")) / "covers"
    store = ReadOnlyStore(target_db)
    data = PublicData(store, root=root_path, covers_dir=covers_dir)
    users_path = Path(users_db).resolve() if users_db else env_users_path(root_path)
    # 会话落盘: 重启后登录不掉线。库坏了也不该拦住站点启动 -> 退回内存会话。
    sessions: auth.SessionManager | None = None
    sessions_path = resolve_sessions_path(root_path, public_cfg, sessions_db)
    if sessions_path is not None:
        try:
            sessions = auth.SessionManager(path=sessions_path)
        except (auth.SessionStoreError, sqlite3.Error) as exc:
            logger.warning("会话库不可用, 退回内存会话(重启会掉线): %s", exc)
            sessions = None
    else:
        logger.warning("未配置会话库, 使用内存会话(重启会掉线)")
    site = PublicSite(
        data,
        users=UserStore(users_path),
        sessions=sessions,
        db_path=target_db,
        limiter=limiter if limiter is not None else RateLimiter.from_config(public_cfg),
        public_config=public_cfg,
        base_url=base_url or "",
        cover_base_url=resolve_cover_base_url(public_cfg, explicit=cover_base_url or "", config=config),
        # 封面外链的客户端惰性创建: 平时浏览页面完全不必加载采集栈,
        # 第一次遇到"本地没有的远程封面"时才建(并且遵守 robots + 节流)。
        http_client_factory=lambda: _cover_http_client(config),
        # 访问日志库与 users 库同目录(线上 /srv/dddb/data), 见 visits_db_path 说明
        visits_path=visits_db,
    )
    logger.debug(
        "用户端数据: db=%s covers=%s users=%s sessions=%s cover_base=%s",
        target_db, covers_dir, site.users.path, sessions_path or "(内存)",
        site.cover_base_url or "(本地缓存)",
    )
    return site


def _cover_http_client(config: Any) -> Any:
    """封面拉取用的 :class:`dddb.httpclient.HttpClient`(与采集端同一套配置)."""
    from ..httpclient import HttpClient

    return HttpClient(config)


def make_server(
    site: PublicSite,
    *,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    logger: logging.Logger | None = None,
) -> ThreadingHTTPServer:
    """构造(但不启动)用户端服务; 传 ``port=0`` 可拿到随机端口(测试用)."""
    log = logger or logging.getLogger("dddb.public.http")
    handler = type("BoundPublicHandler", (PublicHandler,), {"site": site, "logger": log})
    httpd = ThreadingHTTPServer((host, int(port)), handler)  # type: ignore[arg-type]
    httpd.daemon_threads = True
    return httpd


class PublicHandler(BaseHTTPRequestHandler):
    """用户端请求处理器.

    * ``GET`` / ``HEAD``: 任何人;
    * ``POST``: 登录 / 注册(匿名可用)与编辑者写操作;
    * ``PUT`` / ``DELETE`` / ``PATCH``: 一律走鉴权 -> 非编辑者 **403**, 编辑者 405。
    """

    server_version = "dddb-public/2"
    protocol_version = "HTTP/1.1"
    site: PublicSite
    logger: logging.Logger = logger

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        self.logger.debug("%s - %s", self.address_string(), fmt % args)

    # -- 方法 ---------------------------------------------------------------

    def _lang_from_headers(self, headers: Mapping[str, Any]) -> str:
        raw = str((headers or {}).get("cookie") or "")
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "dddb_lang":
                return I18N.language_of(value)
        return I18N.language_of(str((headers or {}).get("accept-language") or ""))

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch("HEAD")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST", with_body=True)

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT", with_body=True)

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch("PATCH", with_body=True)

    def _dispatch(self, method: str, *, with_body: bool = False) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        headers = {key.lower(): value for key, value in self.headers.items()}
        # 语言是**请求级**状态: 记录进入前的值, 请求结束复位,
        # 免得线程被复用时把语言带到下一个请求(测试间也会互相污染)。
        previous_lang = "zh"
        try:
            holder = self.site._thread_lang
            previous_lang = getattr(holder, "value", "zh") or "zh"
            holder.value = self._lang_from_headers(headers)
        except AttributeError:  # pragma: no cover - 极端情况
            pass
        remote = ""
        try:
            remote = str(self.client_address[0] or "")
        except (AttributeError, IndexError, TypeError):  # pragma: no cover - 极端情况
            remote = ""
        client_ip = client_ip_from_headers(
            headers, remote, trust_proxy=bool(getattr(self.site, "trust_proxy", False))
        )
        body = b""
        if with_body:
            try:
                length = int(headers.get("content-length") or 0)
            except (TypeError, ValueError):
                length = 0
            if length > MAX_BODY:
                self.close_connection = True
                try:
                    self._send(Response.html(
                        T.error_page(self.site.context(), status=413,
                                     message="提交内容过大(上限 8MB)。"),
                        status=413,
                    ))
                    return
                finally:
                    try:
                        self.site._thread_lang.value = previous_lang
                    except AttributeError:  # pragma: no cover
                        pass
            if length > 0:
                body = self.rfile.read(length)
        try:
            response = self.site.handle(
                method, parsed.path, parse_query(parsed.query), headers=headers, body=body,
                client_ip=client_ip,
            )
        except Exception as exc:  # pragma: no cover - site.handle 已兜底
            self.logger.warning("%s %s 失败: %s", method, parsed.path, exc)
            response = Response.html(
                T.error_page(self.site.context(), status=500, message=f"内部错误: {exc}"),
                status=500,
            )
        try:
            self._send(response, head=method == "HEAD")
        finally:
            # 语言是请求级状态: 复位, 避免线程复用把语言带到下一个请求
            try:
                self.site._thread_lang.value = previous_lang
            except AttributeError:  # pragma: no cover
                pass

    # -- 发送 ---------------------------------------------------------------

    def _send(self, response: Response, *, head: bool = False) -> None:
        body = b"" if head else response.body
        try:
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(response.body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", _CSP)
            # 默认缓存策略: HTML 不缓存, 其它(封面/静态)可以长缓存。
            # 响应自己指定了 Cache-Control 就以它为准(占位图必须是 no-store)。
            explicit = {str(key).lower() for key in response.headers}
            if "cache-control" not in explicit:
                if response.content_type.startswith("text/html"):
                    self.send_header("Cache-Control", "no-store")
                else:
                    self.send_header("Cache-Control", "public, max-age=300")
            for key, value in response.headers.items():
                self.send_header(key, value)
            for cookie in response.cookies:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            if body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover - 浏览器提前断开
            pass


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m dddb.public.site",
        description="同人专辑档案 · 用户端站点(浏览 / 搜索 / 详情 / 社团 / 展会 / 看板 / 账号 / 编辑)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="监听地址(默认 127.0.0.1)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"端口(默认 {DEFAULT_PORT})")
    parser.add_argument("--db", default="", help="SQLite 路径(默认取配置里的 data/dddb.sqlite3)")
    parser.add_argument("--root", default="", help="项目根目录(默认自动探测)")
    parser.add_argument("--config", default="", help="配置文件路径(默认 <root>/config/config.toml)")
    parser.add_argument("--users-db", default="",
                        help="用户库路径(默认 <root>/data/public_users.sqlite3, 可用 DNH_USERS_DB 覆盖)")
    parser.add_argument("--log-level", default="INFO", help="日志级别(默认 INFO)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    root = Path(args.root).resolve() if args.root else project_root()
    config = None
    try:
        from ..config import Config

        if args.config:
            config = Config.load(args.config, root=root)
        elif (root / "config" / "config.toml").exists():
            config = Config.load(root=root)
    except Exception as exc:  # 配置坏了也不该拦住浏览
        logger.warning("读取配置失败, 回退到默认路径: %s", exc)
        config = None

    try:
        site = build_site(
            config,
            db_path=args.db or None,
            root=root,
            users_db=args.users_db or None,
        )
    except (PublicDataError, UserStoreError) as exc:
        print(f"启动失败: {exc}", file=sys.stderr)
        return 2

    httpd = make_server(site, host=args.host, port=args.port)
    host, port = httpd.server_address[0], httpd.server_address[1]
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    print(f"站点已启动: http://{shown}:{port}/")
    print(f"  专辑库(只读浏览 / 编辑者可写): {site.data.store.path}")
    print(f"  用户库(独立): {site.users.path}")
    print(f"  会话库(重启不掉线): {site.sessions.store.path if site.sessions.persistent else '内存(重启会掉线)'}")
    print(f"  限速: {'开启' if site.limiter.enabled else '关闭'}"
          f"  robots/sitemap: /robots.txt /sitemap.xml")
    print(f"  封面: {'外接存储 ' + site.cover_base_url if site.cover_base_url else '本地缓存(服务端拉取落盘, 不 hotlink 源站)'}"
          f"  缓存目录: {site.data.covers_dir or '(未配置)'}")
    print(f"  静态资源: {STATIC_DIR}")
    print("  Ctrl+C 停止")
    sys.stdout.flush()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
        site.close()
    return 0


if __name__ == "__main__":  # pragma: no cover - 手动起服务时才走到
    raise SystemExit(main())