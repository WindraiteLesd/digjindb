"""公开站点的 HTML 模板(纯函数, 不做任何 I/O).

设计约束:

* **只输出字符串**。除调用方传进来的 :class:`SiteContext` 与视图字典外不碰任何
  全局状态, 于是每个函数都能被单元测试直接调用;
* **一切用户数据都经过 :func:`esc`**。标题/社团/曲目/简介/标签全部来自抓取,
  属于不可信输入, 直接拼进 HTML 就是一个 XSS 洞;
* **链接一律过 :func:`safe_url`**, 拒绝 ``javascript:`` / ``data:`` 这类协议;
* 样式全部写在 ``static/public.css``(同源, 离线可用), 模板里不引用任何 CDN。

视图字典(由 :mod:`dddb.public.data` 产出)的字段约定见 :func:`album_view` 的文档。
"""

from __future__ import annotations

from . import i18n
from .markdown import markdown_to_html

import json
import html
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

#: 站点名 / 副标题 —— 用户端自己的门面
SITE_NAME = "DoujinDigDatabase"
SITE_TAGLINE = "同人音乐专辑挖掘数据库"

#: 封面缺失时卡片上显示的文字(标题首字)
_PLACEHOLDER_CHARS = "♪"

#: 职责的中文名。抓来的角色名往往是英文小写, 直接显示不友好。
ROLE_LABELS: dict[str, str] = {
    "circle": "社团",
    "label": "厂牌",
    "artist": "艺人",
    "performer": "演奏",
    "vocal": "演唱",
    "vocals": "演唱",
    "voice": "配音",
    "compose": "作曲",
    "composition": "作曲",
    "music": "作曲",
    "arrange": "编曲",
    "arrangement": "编曲",
    "lyric": "作词",
    "lyrics": "作词",
    "illustrator": "插画",
    "illustration": "插画",
    "jacket": "封面设计",
    "artwork": "美术",
    "design": "设计",
    "mix": "混音",
    "mixing": "混音",
    "mastering": "母带",
    "master": "母带",
    "guitar": "吉他",
    "bass": "贝斯",
    "drums": "鼓",
    "piano": "钢琴",
    "violin": "小提琴",
    "produce": "制作",
    "producer": "制作",
    "other": "其他",
}

#: credits 分组的显示顺序 —— 让"谁做了这张碟"按主流层级排, 而不是随机序。
ROLE_ORDER: tuple[str, ...] = (
    "circle", "label", "artist", "performer",
    "compose", "arrange", "lyric", "vocal", "voice",
    "illustrator", "jacket", "artwork", "design",
    "mix", "mastering", "guitar", "bass", "drums", "piano", "violin",
    "produce", "other",
)

#: 展会类型显示名
EVENT_KIND_LABELS: dict[str, str] = {
    "m3": "M3",
    "comiket": "Comiket",
    "reitaisai": "例大祭",
    "kouroumu": "紅楼夢",
    "other": "其他展会",
}

#: "信息缺口" 的显示名 —— 编辑者队列用。
NEEDS_LABELS: dict[str, str] = {
    "no_cover": "缺封面",
    "no_tracks": "缺曲目表",
    "no_desc": "缺简介",
    "no_circle": "缺社团",
    "no_date": "缺发售日",
    "no_catalog": "缺编号",
    "no_credits": "缺制作人员",
    "no_source_url": "缺外链",
}

#: 允许出现在 href/src 里的协议
_ALLOWED_SCHEMES = ("http", "https")

#: 所有 POST 表单里隐藏域的名字(与 :mod:`dddb.public.auth` 共用同一个常量)
CSRF_FIELD = "csrf_token"

_WS_RE = re.compile(r"[\s\u0000-\u001f\u007f]+")


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------


def esc(value: Any) -> str:
    """HTML 转义(含引号). ``None`` -> 空串.

    ``html.escape(..., quote=True)`` 会把 ``"`` ``'`` 都转掉, 所以既能用在文本
    节点也能直接用在被引号包住的属性值里。
    """
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    return html.escape(text, quote=True)


def safe_url(url: Any, *, allow_relative: bool = True) -> str:
    """只放行 http / https(以及可选的站内相对路径)的 URL.

    抓来的 ``cover_url`` / ``urls`` 是外部数据, 直接写进 ``href`` 会让
    ``javascript:alert(1)`` 变成可点的 XSS; ``data:text/html`` 同理。
    这里做三件事: 去控制字符与空白前缀、剥协议看白名单、返回原始串(已转义由调用方负责)。
    """
    if url is None:
        return ""
    text = str(url).strip()
    # 制表符/换行/零宽字符插入是绕过协议黑名单的经典手法: "java\tscript:..."
    text = _WS_RE.sub("", text)
    if not text:
        return ""
    lowered = text.lower()
    if lowered.startswith(("javascript:", "data:", "vbscript:", "file:")):
        return ""
    if text.startswith("//"):            # 协议相对 URL 按 https 处理
        return text
    scheme = urllib.parse.urlsplit(text).scheme.lower()
    if scheme in _ALLOWED_SCHEMES:
        return text
    if not scheme and allow_relative and text.startswith("/"):
        return text
    if not scheme and allow_relative and not text.startswith("?"):
        # 形如 "album/xxx" 的相对跳转: 只在允许相对路径时放行
        return text if "/" in text or "." in text else ""
    return ""


def quote_segment(value: Any) -> str:
    """把一段文本编成 URL path 片段(用于 ``/album/<key>`` 这类路由)."""
    return urllib.parse.quote(str(value if value is not None else ""), safe="")


def outbound_link_allowed(url: Any) -> bool:
    """外链 host 白名单判定(策略本体在 :mod:`dddb.public.data`).

    用户明确的合规要求: 公开端**不得把访客导向其它数据网站**
    (名单见 :mod:`dddb.linkpolicy` —— 放在 public 包外, 免得用户端代码出现来源名)。
    这里是渲染前的第一道闸门, ``/link/`` 路由会独立再拒一次 ——
    两道闸门调的是同一个判定函数。

    ``data`` 在模块级 import 本模块, 所以这里**延迟导入**避免循环。
    """
    try:
        from .data import outbound_link_allowed as allowed
    except Exception:  # pragma: no cover - 导入失败时保守拒绝
        return False
    return bool(allowed(url))


def duration_text(seconds: Any) -> str:
    """秒 -> ``3:42`` / ``1:02:07``; 无效值返回空串."""
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return ""
    total = int(seconds)
    if total <= 0:
        return ""
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def total_duration_text(tracks: Sequence[Mapping[str, Any]]) -> str:
    """曲目总时长, 形如 ``48 分 12 秒``."""
    total = 0
    for track in tracks:
        value = track.get("duration")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            total += int(value)
    if not total:
        return ""
    minutes, secs = divmod(total, 60)
    if minutes >= 60:
        hours, minutes = divmod(minutes, 60)
        return f"{hours} 小时 {minutes} 分"
    return f"{minutes} 分 {secs} 秒"


def format_date(value: Any) -> str:
    """发售日尽量友好: ``2026-10-25`` -> ``2026-10-25``, 空 -> ``—``."""
    text = str(value or "").strip()
    if not text:
        return "—"
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", text)
    if match:
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    match = re.match(r"^(\d{4})-(\d{2})$", text)
    if match:
        return f"{match.group(1)}-{match.group(2)}"
    return text


def label_for(mapping: Mapping[str, str], key: Any, *, fallback: str = "") -> str:
    text = str(key or "")
    if not text:
        return fallback
    return mapping.get(text.lower(), mapping.get(text, text))


def role_label(role: Any) -> str:
    return label_for(ROLE_LABELS, role, fallback=str(role or "其他"))


def event_kind_label(kind: Any) -> str:
    return label_for(EVENT_KIND_LABELS, kind)


def sort_role(role: str) -> tuple[int, str]:
    """职责排序键: 先按 :data:`ROLE_ORDER`, 未知职责排最后并按字母序."""
    try:
        return (ROLE_ORDER.index(role), "")
    except ValueError:
        return (len(ROLE_ORDER), role)


def placeholder_text(title: Any) -> str:
    """无封面时卡片上的代替文字: 取标题第一个非空白字符."""
    text = str(title or "").strip()
    for char in text:
        if not char.isspace():
            return char
    return _PLACEHOLDER_CHARS


def percentage(part: Any, whole: Any, *, digits: int = 1) -> float:
    try:
        part_v = float(part or 0)
        whole_v = float(whole or 0)
    except (TypeError, ValueError):
        return 0.0
    if whole_v <= 0:
        return 0.0
    return round(part_v * 100.0 / whole_v, digits)


def build_url(base: str, params: Mapping[str, Any]) -> str:
    """把参数字典拼成查询串, 丢掉空值, 并固定参数顺序(便于断言与缓存)."""
    pairs: list[tuple[str, str]] = []
    for key in sorted(params):
        value = params[key]
        if value is None:
            continue
        if isinstance(value, bool):
            pairs.append((key, "1" if value else "0"))
            continue
        if isinstance(value, (list, tuple, set)):
            for item in value:
                if item is not None and str(item) != "":
                    pairs.append((key, str(item)))
            continue
        text = str(value)
        if text == "":
            continue
        pairs.append((key, text))
    if not pairs:
        return base
    return f"{base}?{urllib.parse.urlencode(pairs)}"


def make_url_for(base: str, params: Mapping[str, Any]) -> Callable[..., str]:
    """构造 ``url_for(**overrides)`` —— 在原有参数上覆盖/删除若干项.

    ``overrides`` 里值为 ``None`` 表示"删掉这个参数"(例如点掉筛选 chip),
    否则覆盖。分页链接 ``url_for(page=3)`` 也走这里。
    """

    def url_for(**overrides: Any) -> str:
        merged: dict[str, Any] = dict(params)
        for key, value in overrides.items():
            if value is None:
                merged.pop(key, None)
            else:
                merged[key] = value
        return build_url(base, merged)

    return url_for


# --------------------------------------------------------------------------
# 站点上下文
# --------------------------------------------------------------------------


@dataclass(slots=True)
class SiteContext:
    """渲染一页所需的站点级信息(不含具体数据)."""

    site_name: str = SITE_NAME
    tagline: str = SITE_TAGLINE
    #: 顶部小统计(专辑/社团/展会), 由 data 层算好。
    totals: Mapping[str, Any] = field(default_factory=dict)
    #: 搜索框当前值
    search_value: str = ""
    #: 当前高亮的导航项
    active: str = ""
    #: 页脚附加说明
    footer_note: str = ""
    #: 当前登录用户(未登录为 ``None``): ``{username, role, role_label}``
    user: Mapping[str, Any] | None = None
    #: 是不是编辑者(决定导航/详情页是否给编辑入口)。superuser 也算编辑者。
    is_editor: bool = False
    #: 是不是超级用户(决定「审核队列」「用户管理」入口是否出现)
    is_superuser: bool = False
    #: 能不能审核 / 管用户(目前等价于 ``is_superuser``, 单独留字段表达模板意图)
    can_review: bool = False
    #: 本页表单要用的 CSRF token(空则表单不带 token)
    csrf_token: str = ""
    #: 一次性提示(登录成功 / 保存成功)
    flash: str = ""
    #: 导航上的未读小红点(私信 / 站内通知), 由站点层填
    messages_unread: int = 0
    notifications_unread: int = 0
    #: 已登录但邮箱还没验证(未验证账号只有访客权限, 页面会提示)
    needs_email_verify: bool = False
    #: 编辑界面布局偏好(卡片大小/顺序); 来自 user_prefs('edit.layout')。
    #: 只在编辑页有意义, 其它页面留 ``None``。
    edit_layout: Mapping[str, Any] | None = None
    #: 界面语言(zh / en / ja)
    lang: str = "zh"

    @property
    def username(self) -> str:
        return str((self.user or {}).get("username") or "")


def _nav(active: str, *, lang: str = "zh", is_editor: bool = False,
         is_superuser: bool = False,
         logged_in: bool = False, csrf_token: str = "",
         messages_unread: int = 0, notifications_unread: int = 0) -> str:
    items = [
        ("albums", "/albums", i18n.translate("nav.albums", lang)),
        ("events", "/events", i18n.translate("nav.events", lang)),
        ("circles", "/circles", i18n.translate("nav.circles", lang)),
        ("artists", "/artists", i18n.translate("nav.artists", lang)),
        ("tags", "/tags", i18n.translate("nav.tags", lang)),
        ("stats", "/stats", i18n.translate("nav.stats", lang)),
        ("edit", "/edit", i18n.translate("nav.edit", lang)),
    ]
    # 管理入口只对超级用户出现; 普通用户连链接都看不到(直接访问也会 403)。
    if is_superuser:
        items.append(("review", "/review", "审核队列"))
        items.append(("users_admin", "/users", "用户管理"))
    items.append(("about", "/about", i18n.translate("nav.about", lang)))
    if not is_editor:
        # 「工作队列」是编辑工具: 普通用户/访客连入口都不该看到
        # (直接访问 /edit 也会 403, 见 PublicSite 的路由鉴权)。
        items = [item for item in items if item[0] != "edit"]
    links = []
    for key, href, text in items:
        current = ' aria-current="page"' if active == key else ""
        cls = "nav-link is-active" if active == key else "nav-link"
        links.append(f'<a class="{cls}" href="{esc(href)}"{current}>{esc(text)}</a>')
    if logged_in:
        account_cls = "nav-link is-active" if active == "account" else "nav-link"
        badge = ""
        if messages_unread or notifications_unread:
            total = int(messages_unread or 0) + int(notifications_unread or 0)
            badge = f'<span class="nav-badge" title="未读私信/通知">{esc(total)}</span>'
        links.append(f'<a class="{account_cls}" href="/account">我的账号{badge}</a>')
        # 登出已移到右上角身份栏(见 auth_strip), 导航里不再重复
    else:
        login_cls = "nav-link is-active" if active == "login" else "nav-link"
        links.append(f'<a class="{login_cls}" href="/login">登录</a>')
        register_cls = "nav-link is-active" if active == "register" else "nav-link"
        links.append(f'<a class="{register_cls} nav-cta" href="/register">注册</a>')
    return "".join(links)


#: 身份标识: 让"名字看起来像官方"的普通用户和真站务一眼可分。
#: (用户要求: 不禁止冒名类用户名, 改用标识区分)
IDENTITY_LABELS: dict[str, tuple[str, str]] = {
    "superuser": ("站务", "ident-staff"),
    "editor": ("编辑者", "ident-editor"),
}


def identity_badge(role: Any) -> str:
    """返回用户身份徽章 HTML(用户 / 编辑者 / 站务)."""
    key = str(role or "").strip().lower()
    label, cls = IDENTITY_LABELS.get(key, ("用户", "ident-user"))
    return f'<span class="ident-badge {cls}" title="用户身份标识">{label}</span>'


def auth_strip(ctx: SiteContext) -> str:
    """页头右侧的**身份提示**(只显示"你是谁", 不放登录/注册/我的账号).

    **为什么只留身份**: 实测用户反馈"右上角登录注册重复出现" —— 主导航
    ``_nav`` 已经渲染了「登录/注册」(未登录) 或「我的账号/登出」(已登录),
    这里再放一遍就会出现两份。登出也只能在导航里(它是 POST + CSRF 小表单)。
    """
    if not ctx.user:
        return ""                                  # 未登录: 导航里已有入口
    role = str((ctx.user or {}).get("role") or "")
    # 用户要求: 只显示**用户名 + 身份徽章**, 登出按钮放在这一栏最右侧
    return (
        '<span class="auth-chip"><b>{}</b>{badge}</span>'
        '<form class="auth-logout" method="post" action="/logout">'
        '<input type="hidden" name="' + CSRF_FIELD + '" value="{tok}">'
        '<button type="submit" class="auth-link auth-logout-btn">登出</button></form>'
    ).format(esc(ctx.username), badge=identity_badge(role), tok=esc(ctx.csrf_token))


def _totals_strip(ctx: SiteContext) -> str:
    totals = ctx.totals or {}
    parts: list[str] = []
    for label, key in (("专辑", "albums"), ("社团", "circles"), ("展会", "events"), ("曲目", "tracks")):
        value = totals.get(key)
        if value is None:
            continue
        parts.append(
            '<span class="total-chip"><b>{}</b>{}</span>'.format(esc(value), esc(label))
        )
    return "".join(parts)


#: 国家/地区 -> emoji 国旗. **台湾一律显示中国国旗**(用户明确要求, 不得使用青天白日旗).
_COUNTRY_FLAGS = {
    "CN": "🇨🇳", "TW": "🇨🇳", "HK": "🇨🇳", "MO": "🇨🇳",
    "JP": "🇯🇵", "KR": "🇰🇷", "US": "🇺🇸", "GB": "🇬🇧", "FR": "🇫🇷",
    "DE": "🇩🇪", "IT": "🇮🇹", "ES": "🇪🇸", "RU": "🇷🇺", "CA": "🇨🇦",
    "AU": "🇦🇺", "BR": "🇧🇷", "SE": "🇸🇪", "NO": "🇳🇴", "FI": "🇫🇮",
    "DK": "🇩🇰", "NL": "🇳🇱", "PL": "🇵🇱", "UA": "🇺🇦", "TR": "🇹🇷",
    "IN": "🇮🇳", "ID": "🇮🇩", "TH": "🇹🇭", "VN": "🇻🇳", "PH": "🇵🇭",
    "MY": "🇲🇾", "SG": "🇸🇬", "MX": "🇲🇽", "AR": "🇦🇷", "NZ": "🇳🇿",
}


def country_flag(code: Any) -> str:
    """国籍代码 -> 国旗 emoji(未知返回空串)."""
    key = str(code or "").strip().upper()
    if len(key) != 2:
        return ""
    return _COUNTRY_FLAGS.get(key, "")


_ACTIVE_NEW_KINDS: dict[str, tuple[str, ...]] = {
    "edit": ("album", "circle", "artist", "event", "tag"),
    "albums": ("album",),
    "circles": ("circle",),
    "artists": ("artist",),
    "events": ("event",),
    "tags": ("tag",),
}


def _entity_buttons(ctx: SiteContext, active: str) -> str:
    kinds = _ACTIVE_NEW_KINDS.get(str(active or ""))
    if not kinds or not ctx.is_editor:
        return ""
    return '<div class="wrap">' + entity_new_buttons(ctx, kinds) + "</div>"


def _title_actions_html(explicit: str, ctx: SiteContext, active: str) -> str:
    """标题行右侧的动作按钮(新建… / 编辑资料), 与标题同排且紧跟其后.

    详情页会显式传入「新建 X + 编辑资料」, 这时**不再**由分类自动补一遍,
    否则同一个「新建 X」会出现两次。
    """
    if explicit:
        return '<span class="title-actions">' + explicit + "</span>"
    auto = _entity_buttons(ctx, active)
    if not auto:
        return ""
    return '<span class="title-actions">' + auto + "</span>"


def layout(
    ctx: SiteContext,
    *,
    title: str,
    body: str,
    breadcrumbs: Sequence[tuple[str, str]] = (),
    subtitle: str = "",
    wide: bool = False,
    active: str | None = None,
    title_actions: str = "",
) -> str:
    """整页外壳(header / main / footer). 所有片段都应当已经转义.

    ``active`` 覆盖 :attr:`SiteContext.active`(导航高亮), 传 ``None`` 时用上下文里的值。
    """
    active_key = ctx.active if active is None else active
    crumb_html = ""
    if breadcrumbs:
        crumbs = ['<a href="/">首页</a>']
        for text, href in breadcrumbs:
            if href:
                crumbs.append(f'<a href="{esc(href)}">{esc(text)}</a>')
            else:
                crumbs.append(f'<span aria-current="page">{esc(text)}</span>')
        crumb_html = '<nav class="crumbs" aria-label="面包屑">' + "".join(
            f"<span>{c}</span>" for c in crumbs
        ) + "</nav>"

    flash_html = ""
    if ctx.flash:
        flash_html = f'<div class="flash" role="status">{esc(ctx.flash)}</div>'

    verify_banner = ""
    if ctx.user and ctx.needs_email_verify:
        verify_banner = (
            '<div class="notice notice-warn" role="alert">你的邮箱还没有验证：'
            '未验证时只有浏览权限，<b>请在邮件发出后的 24 小时内验证，否则您的账号将被删除</b>。'
            '<form class="inline-form" method="post" action="/verify/resend">'
            f'<input type="hidden" name="{CSRF_FIELD}" value="{esc(ctx.csrf_token)}">'
            '<button class="btn-ghost btn-xs" type="submit">重新发送验证邮件</button></form>'
            ' <a class="btn-ghost btn-xs" href="/verify">去验证</a></div>'
        )

    subtitle_html = f'<p class="page-subtitle">{esc(subtitle)}</p>' if subtitle else ""
    main_cls = "wrap main is-wide" if wide else "wrap main"
    search_value = esc(ctx.search_value)
    return f"""<!doctype html>
<html lang="{i18n.HTML_LANGS.get(ctx.lang, 'zh-CN')}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>{esc(title)} · {esc(ctx.site_name)}</title>
<link rel="stylesheet" href="/static/public.css?v=73">
<!-- ?v=2: 图标换过, 但静态资源被 CF/浏览器缓存 1 天, 加版本号强制刷新 -->
<link rel="icon" href="/static/favicon.svg?v=2" type="image/svg+xml">
<!-- 唯一的脚本: 同源外链, 只做渐进增强(CSP 只给 script-src 'self') -->
<script src="/static/public.js?v=62" defer></script>
<script src="/static/cover-crop.js?v=6" defer></script>
<script src="/static/infobox.js?v=8" defer></script>
</head>
<body{_bg_attr(ctx)}>
<a class="skip" href="#content">跳到正文</a>
<header class="topbar">
  <div class="wrap topbar-inner">
    <a class="brand" href="/">
      <img class="brand-logo" src="/static/logo.svg?v=2" alt="{esc(ctx.site_name)}"
           width="34" height="34" decoding="async"
           style="display:block;width:34px;height:34px;object-fit:contain">
      <span class="brand-text">{esc(ctx.site_name)}<small>{esc(ctx.tagline)}</small></span>
    </a>
    <form class="searchbar" action="/albums" method="get" role="search">
      <input type="search" name="q" value="{search_value}" placeholder="搜索标题 / 社团 / 艺人 / 曲目 / 编号"
             aria-label="搜索专辑" autocomplete="off">
      <button type="submit">搜索</button>
    </form>
    <nav class="nav" aria-label="主导航">{_nav(active_key, lang=ctx.lang, is_editor=ctx.is_editor, is_superuser=ctx.is_superuser,
          logged_in=bool(ctx.user), csrf_token=ctx.csrf_token,
          messages_unread=ctx.messages_unread,
          notifications_unread=ctx.notifications_unread)}</nav>
  </div>
  <div class="wrap totals-strip">{_totals_strip(ctx)}<span class="auth-strip">{auth_strip(ctx)}<span class="lang-switch" aria-label="语言"><a href="/lang/zh">中</a><a href="/lang/en">EN</a><a href="/lang/ja">日</a></span></span></div>
</header>
<main id="content" class="{main_cls}">
{crumb_html}
<div class="title-row">
<h1 class="page-title">{esc(title)}</h1>
{_title_actions_html(title_actions, ctx, active_key)}
</div>
{subtitle_html}
{flash_html}{verify_banner}
{body}
</main>
<footer class="wrap footer">
  <p>{esc(ctx.footer_note or SITE_TAGLINE)}</p>
  <p class="footer-meta">
    本站数据由<b>第三方与用户共同提供</b>, 可能存在错漏或过时, 仅供参考, 请以发布者官方信息为准;
    专辑封面与曲目等内容的权利归各发布者 / 社团所有, 本站仅作收录与展示。
  </p>
  <p class="footer-meta">
    所有数据遵从
    <a href="https://creativecommons.org/licenses/by-sa/4.0/" rel="license nofollow noreferrer" target="_blank">Creative Commons Attribution-ShareAlike 4.0 International</a>
    许可协议 (CC BY-SA 4.0)。
  </p>
</footer>
{_appearance_fab(ctx)}
</body>
</html>
"""


# --------------------------------------------------------------------------
# 组件
# --------------------------------------------------------------------------


def badges_for(item: Mapping[str, Any]) -> str:
    """卡片右上角/详情页的状态徽标(缺信息 / 人工编辑过 / 无封面 / 无曲目)."""
    chips: list[str] = []
    missing = list(item.get("missing") or [])
    if missing:
        chips.append(
            '<span class="badge badge-warn" title="{}">待补充 {}</span>'.format(
                esc("、".join(NEEDS_LABELS.get(m, m) for m in missing)), esc(len(missing))
            )
        )
    if item.get("edited"):
        chips.append('<span class="badge badge-edit" title="已被人工编辑">已校订</span>')
    if not item.get("has_cover"):
        chips.append('<span class="badge badge-muted">无封面</span>')
    if not item.get("track_count"):
        chips.append('<span class="badge badge-muted">无曲目</span>')
    return "".join(chips)


def cover_html(item: Mapping[str, Any], *, size: str = "card", lazy: bool = True) -> str:
    """封面图或占位块。``item["cover"]`` 已由数据层做过 URL 白名单过滤。"""
    title = item.get("title") or ""
    cover = safe_url(item.get("cover") or "")
    loading = ' loading="lazy"' if lazy else ""
    if cover:
        return (
            f'<img class="cover cover-{esc(size)}" src="{esc(cover)}" alt="{esc(title)} 封面"'
            f'{loading} decoding="async" referrerpolicy="no-referrer">'
        )
    return (
        f'<div class="cover cover-{esc(size)} cover-fallback" aria-hidden="true">'
        f'<span>{esc(placeholder_text(title))}</span></div>'
    )


def album_card(item: Mapping[str, Any]) -> str:
    """专辑卡片: 封面 + 标题 + 社团 + 展会/日期 + 标签 + 徽标."""
    href = safe_url(item.get("href") or "")
    title = item.get("title") or ""
    alt = item.get("title_alt") or ""
    meta_bits: list[str] = []
    if item.get("event"):
        meta_bits.append(f'<span class="meta-event">{esc(item["event"])}</span>')
    if item.get("date"):
        meta_bits.append(f'<span class="meta-date">{esc(format_date(item["date"]))}</span>')
    meta = ' · '.join(meta_bits)
    circle = item.get("circle") or ""
    circle_html = ""
    if circle:
        circle_href = safe_url(item.get("circle_href") or "")
        if circle_href:
            circle_html = f'<a class="card-circle" href="{esc(circle_href)}">{esc(circle)}</a>'
        else:
            circle_html = f'<span class="card-circle">{esc(circle)}</span>'
    tags = ""
    # 标签**换行显示**(不再裁掉): 卡片高度跟着内容长, 所以 card-body 里的
    # tagrow 用 tagrow-wrap, 并且 .album-card 不再 overflow:hidden。
    tag_items = list(item.get("tags") or [])[:5]
    if tag_items:
        tags = '<div class="tagrow tagrow-wrap card-tags">' + "".join(
            '<a class="tag" href="/tag/{}">{}</a>'.format(esc(quote_segment(t)), esc(t))
            for t in tag_items
        ) + "</div>"
    alt_html = f'<p class="card-alt">{esc(alt)}</p>' if alt else ""
    track_html = ""
    if item.get("track_count"):
        track_html = f'<span class="card-tracks" title="曲目数">{esc(item["track_count"])} 曲</span>'
    return f"""<article class="album-card">
  <a class="card-link" href="{esc(href)}">
    <div class="card-cover">{cover_html(item)}{badges_for(item)}</div>
    <div class="card-body">
      <h3 class="card-title">{esc(title)}</h3>
      {alt_html}
      <p class="card-meta">{circle_html}<span class="card-dot">·</span>{meta if meta else '<span class="muted">未标注展会</span>'}</p>
      {tags}
    </div>
  </a>
  <div class="card-foot">{track_html}</div>
</article>"""


def album_grid(
    items: Sequence[Mapping[str, Any]],
    *,
    empty_text: str = "没有找到符合条件的专辑。",
    suggestions: Mapping[str, Any] | None = None,
) -> str:
    if items:
        cards = "".join(album_card(item) for item in items)
        return f'<div class="grid">{cards}</div>'
    return (
        f'<div class="empty"><p class="empty-title">空</p><p>{esc(empty_text)}</p>'
        + suggestions_html(suggestions or {})
        + "</div>"
    )


def suggestions_html(suggestions: Mapping[str, Any]) -> str:
    """搜不到专辑时给出的"你可能是想找这个" —— 标签 / 展会 / 社团的近似命中.

    搜索本身只查标题/社团/艺人/曲目/编号, **不查标签**; 但访客很容易直接
    拿标签或展会名来搜, 所以零结果时在这里兜一把。
    """
    rows: list[str] = []
    for key, label in (("tags", "标签"), ("events", "展会"), ("circles", "社团")):
        for entry in (suggestions.get(key) or [])[:6]:
            value = str(entry.get("value") or "")
            if not value:
                continue
            href = str(entry.get("href") or "")
            count = entry.get("count")
            count_html = f'<span>{esc(count)} 张</span>' if count else ""
            rows.append(
                '<a class="pill" href="{}"><b>{}</b><span class="pill-kind">{}</span>{}</a>'.format(
                    esc(safe_url(href) or "#"), esc(value), esc(label), count_html
                )
            )
    if not rows:
        return ""
    return (
        '<div class="suggest"><p class="suggest-title">没有匹配的专辑。也许你想找:</p>'
        f'<div class="pills">{"".join(rows)}</div></div>'
    )


def pagination(pager: Mapping[str, Any], url_for: Callable[..., str]) -> str:
    """分页条。``pager`` 由 :func:`dddb.public.data.paginate` 产出."""
    pages = int(pager.get("pages") or 1)
    page = int(pager.get("page") or 1)
    if pages <= 1:
        return ""
    parts: list[str] = []
    if pager.get("has_prev"):
        parts.append(f'<a class="page-link" href="{esc(url_for(page=page - 1))}" rel="prev">上一页</a>')
    else:
        parts.append('<span class="page-link is-disabled">上一页</span>')
    for number in pager.get("window") or []:
        if number == "...":
            parts.append('<span class="page-gap">…</span>')
            continue
        if int(number) == page:
            parts.append(f'<span class="page-link is-current" aria-current="page">{esc(number)}</span>')
        else:
            parts.append(f'<a class="page-link" href="{esc(url_for(page=number))}">{esc(number)}</a>')
    if pager.get("has_next"):
        parts.append(f'<a class="page-link" href="{esc(url_for(page=page + 1))}" rel="next">下一页</a>')
    else:
        parts.append('<span class="page-link is-disabled">下一页</span>')
    summary = (
        f'<span class="page-summary">共 {esc(pager.get("total", 0))} 条 · 第 {esc(page)}/{esc(pages)} 页</span>'
    )
    return f'<nav class="pager" aria-label="分页">{"".join(parts)}{summary}</nav>'


def active_filter_chips(filters: Mapping[str, Any], url_for: Callable[..., str]) -> str:
    """已生效的筛选条件, 每个都带一个"点掉"的链接."""
    label_map = {
        "q": "搜索",
        "event": "展会",
        "kind": "类型",
        "year": "年份",
        "tag": "标签",
        "circle": "社团",
        "artist": "艺人",
        "needs": "缺口",
        "has_cover": "有封面",
        "sort": "排序",
    }
    chips: list[str] = []
    for key in sorted(filters):
        value = filters[key]
        if value in (None, "", 0) and key not in ("has_cover",):
            continue
        if key in ("page", "per"):
            continue
        text = label_map.get(key, key)
        if key == "has_cover":
            shown = "仅看有封面"
        elif key == "needs":
            values = value if isinstance(value, (list, tuple, set)) else [value]
            shown = "、".join(NEEDS_LABELS.get(str(item), str(item)) for item in values)
        elif isinstance(value, (list, tuple, set)):
            shown = "、".join(str(item) for item in value)
        else:
            shown = str(value)
        chips.append(
            '<a class="chip" href="{}" title="取消该筛选">{}: <b>{}</b><span aria-hidden="true">×</span></a>'.format(
                esc(url_for(**{key: None, "page": None})), esc(text), esc(shown)
            )
        )
    if not chips:
        return ""
    clear_href = url_for(
        q=None, event=None, kind=None, year=None, tag=None, circle=None,
        artist=None, needs=None, has_cover=None, sort=None, page=None, per=None,
    )
    return (
        '<div class="chips"><span class="chips-label">筛选</span>'
        + "".join(chips)
        + f'<a class="chip chip-clear" href="{esc(clear_href)}">清空</a></div>'
    )


def narrow_events_by_kind(
    events: Sequence[Mapping[str, Any]], kind: Any, current: Any = ""
) -> list[Mapping[str, Any]]:
    """「具体展会次」下拉项: 按已选**展会类型**收窄(纯函数).

    为什么当前选中项会被强行保留: URL 可能是 ``?kind=comiket&event=M3-2026春``
    (用户手改链接 / 旧收藏), 若把当前项从选项里筛掉, 浏览器会显示成"全部",
    用户看到的结果就与他给的 URL 不一致 —— 宁可选项里多一条, 也不悄悄改条件。
    """
    text = str(kind or "").strip()
    items = list(events)
    if not text:
        return items
    narrowed = [e for e in items if str(e.get("kind") or "") == text]
    picked = str(current or "")
    if picked and not any(str(e.get("value") or "") == picked for e in narrowed):
        extra = [e for e in items if str(e.get("value") or "") == picked]
        return extra + narrowed
    return narrowed


def filter_panel(
    filters: Mapping[str, Any],
    *,
    action: str,
    events: Sequence[Mapping[str, Any]] = (),
    kinds: Sequence[Mapping[str, Any]] = (),
    years: Sequence[str] = (),
    tags: Sequence[Mapping[str, Any]] = (),
    sorts: Sequence[tuple[str, str]] = (),
    show_query: bool = True,
    extra_hidden: Mapping[str, Any] = {},
) -> str:
    """筛选表单. 纯 GET 表单 —— 不需要 JavaScript, 离线也能用.

    筛选顺序是**递进**的: 展会类型 → 具体展会次(只列前者下的场次) → 年份。

    * 选定具体展会次后**不再渲染年份**: 展会次已经把年份唯一确定下来, 再放一个
      年份下拉只会让人以为"还能在这届展会里换年份";
    * 递进收窄在**模板里**做(不是 ``site.py``): 表单是纯函数, 在这里过滤可以让
      "选项与当前 URL 一致"这件事只有一个实现、只测一次;
    * 这里的收窄**只影响下拉选项**, 不影响查询 —— 旧链接 ``?year=2026`` /
      ``?event=xxx`` 依旧按原语义工作(见 :func:`dddb.public.data.parse_filters`)。
    """
    hidden = "".join(
        f'<input type="hidden" name="{esc(k)}" value="{esc(v)}">'
        for k, v in sorted(extra_hidden.items())
        if v not in (None, "")
    )

    def options(items: Sequence[Mapping[str, Any]], current: str) -> str:
        out = ['<option value="">全部</option>']
        for entry in items:
            value = str(entry.get("value") or "")
            label = str(entry.get("label") or value)
            selected = ' selected' if value == current else ""
            out.append(f'<option value="{esc(value)}"{selected}>{esc(label)}</option>')
        return "".join(out)

    def plain_options(items: Sequence[Any], current: str) -> str:
        out = ['<option value="">全部</option>']
        for value in items:
            text = str(value)
            selected = ' selected' if text == current else ""
            out.append(f'<option value="{esc(text)}"{selected}>{esc(text)}</option>')
        return "".join(out)

    current_kind = str(filters.get("kind") or "")
    current_event = str(filters.get("event") or "")
    event_items = narrow_events_by_kind(events, current_kind, current_event)

    query_field = ""
    if show_query:
        query_field = (
            '<label class="field"><span>关键词</span>'
            f'<input type="search" name="q" value="{esc(filters.get("q") or "")}"'
            ' placeholder="标题 / 社团 / 艺人 / 曲目 / 编号"></label>'
        )

    kind_field = (
        '<label class="field"><span>展会类型</span>'
        f'<select name="kind">{options(kinds, current_kind)}</select></label>'
    )
    event_field = (
        '<label class="field"><span>具体展会次</span>'
        f'<select name="event">{options(event_items, current_event)}</select></label>'
    )
    # 选了具体展会次 = 年份已确定 -> 年份下拉整块不渲染
    year_field = ""
    if not current_event:
        year_field = (
            '<label class="field"><span>年份</span>'
            f'<select name="year">{plain_options(years, str(filters.get("year") or ""))}</select></label>'
        )

    sort_field = ""
    if sorts:
        current_sort = str(filters.get("sort") or "")
        opts = "".join(
            f'<option value="{esc(value)}"{" selected" if value == current_sort else ""}>{esc(label)}</option>'
            for value, label in sorts
        )
        sort_field = f'<label class="field"><span>排序</span><select name="sort">{opts}</select></label>'

    cover_field = ""
    if "has_cover" in filters:
        checked = " checked" if filters.get("has_cover") else ""
        cover_field = (
            '<label class="field field-check">'
            f'<input type="checkbox" name="has_cover" value="1"{checked}><span>只看有封面</span></label>'
        )

    return f"""<form class="filters" method="get" action="{esc(action)}">
{hidden}
{query_field}
{kind_field}
{event_field}
{year_field}
<label class="field"><span>标签</span><select name="tag">{options(tags, str(filters.get("tag") or ""))}</select></label>
{cover_field}
{sort_field}
<div class="field-actions">
  <button type="submit">应用筛选</button>
  <a class="btn-ghost" href="{esc(action)}">重置</a>
</div>
</form>"""


def album_results(
    ctx: SiteContext,
    *,
    items: Sequence[Mapping[str, Any]],
    pager: Mapping[str, Any],
    filters: Mapping[str, Any],
    url_for: Callable[..., str],
    empty_text: str = "换个关键词, 或者去掉几个筛选条件试试。",
    suggestions: Mapping[str, Any] | None = None,
) -> str:
    """列表主体: 结果计数 + chip + 卡片网格 + 分页."""
    chips = active_filter_chips(filters, url_for)
    header = (
        f'<div class="results-head"><p class="results-count">命中 <b>{esc(pager.get("total", 0))}</b> 张专辑</p>'
        f'<p class="results-hint">每页 {esc(pager.get("per", 24))} 条</p></div>'
    )
    grid = album_grid(items, empty_text=empty_text, suggestions=suggestions)
    pager_html = pagination(pager, url_for)
    return header + chips + grid + pager_html


# --------------------------------------------------------------------------
# 页面
# --------------------------------------------------------------------------


def home_page(
    ctx: SiteContext,
    *,
    latest: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
    circles: Sequence[Mapping[str, Any]],
    tags: Sequence[Mapping[str, Any]],
    stats: Mapping[str, Any],
    inbox: Mapping[str, Any] = {},
    url_for: Callable[..., str] | None = None,
) -> str:
    url_for = url_for or make_url_for("/albums", {})
    total = stats.get("albums") or 0
    # 工作队列是编辑工具: 普通用户/访客的首页上连按钮都不出现。
    queue_action = ""
    if ctx.is_editor:
        queue_label = "工作队列"
        if inbox.get("total"):
            queue_label += f" ({esc(inbox.get('total'))})"
        queue_action = f'<a class="btn-ghost" href="/edit">{queue_label}</a>'
    hero = f"""<section class="hero">
  <p class="hero-line">已收录 <b>{esc(total)}</b> 张专辑 · <b>{esc(stats.get("circles") or 0)}</b> 个社团 · <b>{esc(stats.get("events") or 0)}</b> 场展会</p>
  <div class="hero-actions">
    <a class="btn" href="/albums">浏览全部专辑</a>
    <a class="btn-ghost" href="/stats">统计看板</a>
    {queue_action}
  </div>
</section>"""

    event_links = "".join(
        '<a class="pill" href="/event/{}"><b>{}</b><span>{} 张</span></a>'.format(
            esc(quote_segment(e.get("value") or "")),
            esc(e.get("value") or ""),
            esc(e.get("count") or 0),
        )
        for e in list(events)[:12]
    )
    circle_links = "".join(
        '<a class="pill" href="/circle/{}">{}</a>'.format(
            esc(quote_segment(c.get("value") or "")), esc(c.get("value") or "")
        )
        for c in list(circles)[:12]
    )
    tag_links = "".join(
        '<a class="tag" href="/tag/{}">{}</a>'.format(esc(quote_segment(t.get("value") or "")), esc(t.get("value") or ""))
        for t in list(tags)[:18]
    )
    inbox_html = ""
    if ctx.is_editor and inbox.get("kinds"):
        rows = "".join(
            '<a class="inbox-row" href="/edit/{}"><span class="inbox-label">{}</span>'
            '<span class="inbox-count">{}</span></a>'.format(
                esc(k.get("value") or ""), esc(k.get("label") or ""), esc(k.get("count") or 0)
            )
            for k in inbox["kinds"]
        )
        inbox_html = f"""<section class="panel">
  <h2 class="section-title">工作队列<span class="section-sub">需要补资料的专辑</span></h2>
  <p class="section-note">这些专辑还缺关键信息, 点进去可以看到具体缺哪一项。</p>
  <div class="inbox-rows">{rows}</div>
</section>"""

    body = f"""{hero}
<section class="panel">
  <h2 class="section-title">最近更新<span class="section-sub">按入库时间</span></h2>
  {album_grid(latest, empty_text="数据库里还没有专辑。")}
  <p class="more"><a href="/albums">查看全部 →</a></p>
</section>
<section class="panel">
  <h2 class="section-title">展会<span class="section-sub">{esc(len(events))} 场</span></h2>
  <div class="pills">{event_links}</div>
</section>
<section class="panel">
  <h2 class="section-title">活跃社团<span class="section-sub">按收录数量</span></h2>
  <div class="pills">{circle_links or '<span class="muted">暂无</span>'}</div>
</section>
<section class="panel">
  <h2 class="section-title">标签<span class="section-sub">最常出现的 {esc(len(list(tags)[:18]))} 个</span></h2>
  <div class="tagrow tagrow-wrap">{tag_links or '<span class="muted">暂无</span>'}</div>
</section>
{inbox_html}"""
    return layout(ctx, title="首页", body=body, subtitle="", active="home")


def albums_page(
    ctx: SiteContext,
    *,
    items: Sequence[Mapping[str, Any]],
    pager: Mapping[str, Any],
    filters: Mapping[str, Any],
    url_for: Callable[..., str],
    filter_html: str,
    heading: str = "浏览专辑",
    subtitle: str = "",
    suggestions: Mapping[str, Any] | None = None,
    user_hits: Sequence[Mapping[str, object]] = (),) -> str:
    body = f"""<div class="layout-split">
  <aside class="sidebar">{filter_html}</aside>
  <section class="content">{album_results(ctx, items=items, pager=pager, filters=filters,
                                        url_for=url_for, suggestions=suggestions)}</section>
</div>"""
    user_hits_html = ""
    if user_hits:
        cards = "".join(
            '<a class="user-hit" href="/u/{name}/">'
            '<img class="user-hit-avatar" src="/avatar/{uid}" alt="" loading="lazy"'
            ' onerror="this.style.display=\'none\'">'
            '<span class="user-hit-name">{name}</span>'
            '<span class="user-hit-role">{role}</span></a>'.format(
                name=esc(quote_segment(str(item.get("username") or ""))),
                uid=esc(item.get("id") or ""),
                role=esc(str(item.get("role_label") or "")),
            )
            for item in user_hits
        )
        user_hits_html = (
            '<section class="panel user-hits"><h2 class="section-title">匹配的用户</h2>'
            f'<div class="user-hit-grid">{cards}</div></section>'
        )

    return layout(ctx, title=heading, body=user_hits_html + body, subtitle=subtitle, active="albums", breadcrumbs=[(heading, "")])


def _comment_action_html(ctx: SiteContext, item: Mapping[str, Any], me: str) -> str:
    """评论小操作: 自己的可删, 别人的可举报(都走 POST)."""
    if not ctx.user:
        return ""
    cid = str(item.get("id") or "")
    if not cid:
        return ""
    if me and str(item.get("username") or "") == me:
        return (
            '<form class="inline-form comment-action" method="post" action="/comment/delete">'
            '<input type="hidden" name="csrf_token" value="%s">'
            '<input type="hidden" name="comment_id" value="%s">'
            '<button class="btn-ghost btn-xs" type="submit">删除</button></form>'
        ) % (esc(ctx.csrf_token), esc(cid))
    return (
        '<form class="inline-form comment-action" method="post" action="/comment/report">'
        '<input type="hidden" name="csrf_token" value="%s">'
        '<input type="hidden" name="comment_id" value="%s">'
        '<input type="hidden" name="reason" value="举报不当评论">'
        '<button class="btn-ghost btn-xs" type="submit">举报</button></form>'
    ) % (esc(ctx.csrf_token), esc(cid))


def comments_section(
    ctx: SiteContext,
    *,
    album: Mapping[str, Any],
    comments: Sequence[Mapping[str, Any]] = (),
    circle_name: str = "",
    following_circle: bool = False,
    following_artists: Sequence[str] = (),
    cover_upload: bool = False,
    comment_likes: Mapping[Any, int] | None = None,
    liked_ids: Sequence[Any] = (),
    target: str = "album",
    comment_action: str = "",
    next_url: str = "",
) -> str:
    """评论区: 头像 + 时间 + 点赞(不能自赞) + 发表框."""
    # 入参可能是 Album 对象, 也可能是详情视图 dict —— 统一成 dict 再往下走,
    # 否则 album.get(...) 会 AttributeError, 页面渲染失败。
    if not isinstance(album, Mapping):
        album = album.to_dict() if hasattr(album, "to_dict") else dict(album)
    key = str(album.get("key") or "")
    likes = {int(k): int(v) for k, v in (comment_likes or {}).items() if str(k).isdigit()}
    liked = {int(i) for i in liked_ids if str(i).isdigit()}
    me_name = str((ctx.user or {}).get("username") or "")
    action = comment_action or ("/album/" + quote_segment(key) + "/comment")
    back = next_url or "/album/" + quote_segment(key)

    def _avatar(item: Mapping[str, Any]) -> str:
        name = str(item.get("username") or "")
        uid = item.get("user_id")
        if uid:
            return (f'<img class="comment-avatar" src="/avatar/{esc(uid)}"'
                    f' alt="{esc(name)}的头像" width="36" height="36" loading="lazy">')
        return (f'<span class="comment-avatar comment-avatar-text" aria-hidden="true">'
                f"{esc(name[:1] or '?')}</span>")

    rows = []
    for item in comments:
        cid = int(item.get("id") or 0)
        author = str(item.get("username") or "")
        count = likes.get(cid, 0)
        mine = cid in liked
        if me_name and author.lower() != me_name.lower():
            like_html = (
                                '<form class="inline-form comment-like" method="post" action="/like">'
                f'<input type="hidden" name="csrf_token" value="{esc(ctx.csrf_token)}">'
                f'<input type="hidden" name="comment_id" value="{cid}">'
                f'<input type="hidden" name="next" value="{esc(back)}">'
                f'<button type="submit" class="btn-ghost btn-xs{" is-liked" if mine else ""}">'
                f'{"已赞" if mine else "点赞"} {count}</button></form>'
            )
        else:
            like_html = f'<span class="comment-like-static">赞 {count}</span>'
        rows.append(
            '<li class="comment-item">'
            f'<div class="comment-head">{_avatar(item)}'
            f'<div class="comment-meta"><b>{esc(author)}</b>'
            f'<span class="muted">{esc(item.get("created_at_text") or "")}</span></div>'
            f'{like_html}</div>'
            f'<div class="comment-body">{esc(item.get("body") or "")}</div>'
            f'{_comment_action_html(ctx, item, me_name)}</li>'
        )
    form_html = ""
    if ctx.user:
        form_html = (
            '<form class="auth-form" method="post" action="' + esc(action) + '">'
            '<input type="hidden" name="csrf_token" value="' + esc(ctx.csrf_token) + '">'
            '<input type="hidden" name="album_key" value="' + esc(key) + '">'
            '<label class="field auth-field"><span>发表评论</span>'
            '<textarea name="body" rows="3" maxlength="2000" required></textarea></label>'
            '<div class="field-actions"><button type="submit">发表</button></div></form>'
        )
    return (
        f'<section class="panel"><h2 class="section-title">{i18n.translate("section.comments", ctx.lang)}</h2>'
        + ('<ul class="plain-list comments">' + "".join(rows) + "</ul>" if rows
           else f'<p class="section-note">{i18n.translate("detail.no_comments", ctx.lang)}</p>')
        + form_html
        + "</section>"
    )

def _link_label(link: Mapping[str, Any], index: int) -> str:
    """外链按钮的文案: 优先用户填的链接名, 退回"外部链接 N".

    ``link["name"]`` 是编辑页里用户填的标签(circle_site / x / youtube ...),
    不是来源站名 —— 显示它既有用又不违反"不暴露资料来源"的承诺。
    """
    name = str((link or {}).get("name") or "").strip()
    if name:
        return name
    return f"外部链接 {index}"

def _detail_tracks_html(
    disc_map: Mapping[int, list[Mapping[str, Any]]],
    multi_disc: bool,
    tracks_all: list[Mapping[str, Any]],
    ctx: "SiteContext",
    album: Mapping[str, Any],
) -> str:
    """详情页曲目表: 按碟分组; **点击曲目展开**语种/曲风等补充信息.

    默认只显示 # / 曲名 / 曲目艺人 / 时长 —— 语种、曲风、副标题、翻译这些
    "补充信息"默认不占版面, 点击某一行后在曲名下方展开(有值才显示)。
    """
    if not tracks_all:
        return ('<div class="empty"><p>{}</p></div>').format(
            i18n.translate("detail.no_tracks", ctx.lang))

    duration_text = total_duration_text(tracks_all)
    meta_parts = ["共 {} 首".format(len(tracks_all))]
    if duration_text:
        meta_parts.append("总时长 {}".format(duration_text))
    if multi_disc:
        meta_parts.append("{} 张碟".format(len(disc_map)))
    head = ('<div class="tracklist-head"><span class="caption-meta">{}</span>'
            "</div>").format(esc(" · ".join(meta_parts)))

    def _extra_rows(track: Mapping[str, Any]) -> str:
        """展开区: 只列**有值**的补充字段."""
        items: list[tuple[str, str]] = []
        subtitle = str(track.get("subtitle") or "").strip()
        if subtitle:
            items.append(("副标题", subtitle))
        title_zh = str(track.get("title_zh") or "").strip()
        if title_zh:
            items.append(("翻译歌名", title_zh))
        lang = str(track.get("lang") or "").strip()
        if lang:
            items.append(("语种", lang))
        genre = str(track.get("genre") or "").strip()
        if genre:
            items.append(("曲风", genre))
        if not items:
            return ""
        return '<div class="track-extra">{}</div>'.format(
            "".join('<span class="track-extra-item"><b>{}</b>{}</span>'.format(
                esc(label), esc(value)) for label, value in items))

    blocks: list[str] = []
    for disc_no in sorted(disc_map):
        rows_html: list[str] = []
        for pos, track in enumerate(disc_map[disc_no], 1):
            index = track.get("index")
            if index in (None, ""):
                index = pos
            number = ("{}-{}".format(disc_no, index) if multi_disc
                      else str(index))
            artists = track.get("artists") or []
            extra = _extra_rows(track)
            row_class = "track-item" + (" has-extra" if extra else "")
            # 有补充信息才可点击展开; 无补充信息就是个普通行
            toggle = ('<button type="button" class="track-toggle"'
                      ' aria-label="展开曲目信息">▸</button>' if extra else "")
            rows_html.append(
                '<tr class="{}"{}>'
                '<td class="num">{}</td>'
                '<td class="track-title">{}{}</td>'
                '<td class="track-artists">{}</td>'
                '<td class="num dur">{}</td></tr>'.format(
                    row_class,
                    ' data-track-expand="1"' if extra else "",
                    esc(number),
                    toggle,
                    esc(track.get("title") or "(未命名)"),
                    esc("、".join(str(a) for a in artists)),
                    esc(track.get("duration_text") or "—"),
                )
            )
            if extra:
                rows_html.append(
                    '<tr class="track-extra-row" hidden><td></td>'
                    '<td colspan="3">{}</td></tr>'.format(extra))
        disc_title = ""
        if multi_disc:
            disc_title = ('<div class="disc-subtitle">Disc {:02d}'
                          '<span class="disc-sub-count">{} 首</span></div>'
                          .format(disc_no, len(disc_map[disc_no])))
        blocks.append(
            disc_title
            + '<div class="table-scroll"><table class="tracklist" data-key="{}">'
              '<thead><tr><th class="col-num">#</th><th class="col-title">曲名</th>'
              '<th class="col-artist">曲目艺人</th><th class="col-dur">时长</th>'
              "</tr></thead><tbody>{}</tbody></table></div>".format(
                  esc(album.get("key") or ""), "".join(rows_html))
        )
    return head + "".join(blocks)



def _album_updated_text(album: Mapping[str, Any], lang: Any = "zh") -> str:
    """专辑"最后更新"文本: 优先用时间戳按**页面语言**格式化."""
    from .timefmt import format_stamp

    stamp = album.get("updated_at")
    text = format_stamp(stamp, lang)
    if text:
        return text
    # 老数据没有时间戳: 退回数据层给好的文本
    return str(album.get("updated_text") or "")


def album_detail_page(
    ctx: SiteContext,
    *,
    album: Mapping[str, Any],
    can_edit: bool | None = None,
    related: Sequence[Mapping[str, Any]] = (),
    comments: Sequence[Mapping[str, Any]] = (),
    circle_name: str = "",
    following_circle: bool = False,
    following_artists: Sequence[str] = (),
    cover_upload: bool = False,
    entities: Sequence[Mapping[str, Any]] = (),
    entities_more: int = 0,
    entities_more_names: str = "",
    comment_likes: Mapping[Any, int] | None = None,
    liked_ids: Sequence[Any] = (),
) -> str:
    """专辑详情.

    * **不显示"来源"/"可信度"**: 用户端不该暴露资料是怎么收集来的;
    * ``can_edit`` 决定右上角有没有"编辑这张专辑"(只有编辑者才有), 入口是站内的
      ``/album/<key>/edit`` —— 用户端自己的写界面, 不再往外跳。
    """
    editor = ctx.is_editor if can_edit is None else bool(can_edit)
    title = album.get("title") or "(无标题)"
    #: 站内编辑入口: /album/<key>/edit(路径已经过 safe_url 白名单)
    album_path = safe_url(album.get("href") or "")
    edit_href = f"{album_path}/edit" if album_path else ""
    alt_parts: list[str] = []
    if album.get("title_alt") and album["title_alt"] != title:
        alt_parts.append(str(album["title_alt"]))
    if album.get("romaji") and album["romaji"] not in (title, album.get("title_alt")):
        alt_parts.append(str(album["romaji"]))
    alt_html = "".join(f'<p class="alt-title">{esc(p)}</p>' for p in alt_parts if p)

    def row(label: str, value: Any, *, href: str = "", cls: str = "") -> str:
        if not value:
            return ""
        safe_href = safe_url(href) if href else ""
        if safe_href:
            inner = f'<a href="{esc(safe_href)}">{esc(value)}</a>'
        else:
            inner = esc(value)
        return f'<div class="info-row {esc(cls)}"><dt>{esc(label)}</dt><dd>{inner}</dd></div>'

    info_rows = "".join([
        # 社团/艺人不放在信息表里(上方实体卡片展示, 避免同名重复)
        row(i18n.translate("detail.release_date", ctx.lang), format_date(album.get("date")) if album.get("date") else "", cls="date"),
        row(i18n.translate("field.event", ctx.lang), album.get("event"), href=album.get("event_href") or ""),
        row(i18n.translate("field.booth", ctx.lang), " ".join(p for p in (album.get("hall"), album.get("booth")) if p)),
        row(i18n.translate("field.catalog_no", ctx.lang), album.get("catalog_no")),
        row(i18n.translate("field.price", ctx.lang), album.get("price")),
        # 用时间戳 + 页面语言格式化, 避免数据层默认时区(zh)与页面不一致
        row(i18n.translate("detail.updated", ctx.lang),
            _album_updated_text(album, ctx.lang)),
    ])
    if album.get("year"):
        pass  # 年份已包含在发售日里, 不再单独占一行

    missing = list(album.get("missing") or [])
    missing_html = ""
    if missing:
        # 只有编辑者才拿得到"去补充"的按钮; 访客看到的是提示 + 登录入口。
        if editor and edit_href:
            edit_link = f'<a class="btn" href="{esc(edit_href)}">补充这些信息 →</a>'
        elif editor:
            edit_link = ""
        else:
            edit_link = '<a class="btn-ghost" href="/login">登录后可补充</a>'
        missing_html = (
            '<div class="notice notice-warn"><p class="notice-title">待补充信息</p><p>{}</p><p>{}</p></div>'.format(
                esc("、".join(NEEDS_LABELS.get(m, m) for m in missing)), edit_link
            )
        )

    # ① 社团: 紧跟标题(用户要求"社团置入首个容器, 位于专辑标题后方或下方")
    _circle_name = str(album.get("circle") or "").strip()
    # 社团显示在**标题后**(用户要求); 制作人员区块里不再重复它
    # 社团紧跟标题; **保留关注表单**(用户要能关注社团)
    _circle_follow = ""
    if _circle_name and ctx.user:
        _circle_follow = (
            '<form class="circle-follow" method="post" action="/follows/toggle">'
            f'<input type="hidden" name="csrf_token" value="{esc(ctx.csrf_token)}">'
            '<input type="hidden" name="kind" value="circle">'
            f'<input type="hidden" name="key" value="{esc(_circle_name)}">'
            f'<input type="hidden" name="next" value="/album/{esc(quote_segment(str(album.get("key") or "")))}">'
            '<button type="submit" class="btn-ghost btn-xs">'
            f'{"已关注 ✓" if following_circle else "关注"}</button></form>'
        )
    circle_line = (
        '<p class="detail-circle"><span class="circle-label">社团</span>'
        '<a class="circle-link" href="{}">{}</a>{}</p>'.format(
            esc(album.get("circle_href") or "#"), esc(_circle_name),
            _circle_follow)
        if _circle_name else ""
    )

    # 曲目按碟分组(详情页也要看得出分碟)
    _tracks_all = list(album.get("tracks") or [])
    track_total = len(_tracks_all)
    _disc_map: dict[int, list[dict[str, Any]]] = {}
    for _t in _tracks_all:
        _disc_map.setdefault(int(_t.get("disc") or 1), []).append(_t)
    _multi_disc = len(_disc_map) > 1

    tracks_html = _detail_tracks_html(
        _disc_map, _multi_disc, _tracks_all, ctx, album)

    credits = list(album.get("credits") or [])
    credits_html = ""
    if credits:
        def _uniq(names):
            """去重且保序.

            来源合并后常出现 ``"X / X / X"``、``"X、X"`` 这种**一个字符串里
            重复好几遍**的名字：先按分隔符拆开，再逐个去重。
            """
            seen: list[str] = []
            for name in names or []:
                for part in re.split(r"[、,，;；/|／]+", str(name or "")):
                    item = part.strip()
                    if item and item not in seen:
                        seen.append(item)
            return seen

        # 同一 label 的组也可能重复(多来源合并), 先合并再渲染
        merged_groups: list[tuple[str, list[str]]] = []
        for group in credits:
            label = str(group.get("label") or "").strip()
            names = _uniq(group.get("names"))
            if not names:
                continue
            for index, (old_label, old_names) in enumerate(merged_groups):
                if old_label == label:
                    merged_groups[index] = (label, _uniq(old_names + names))
                    break
            else:
                merged_groups.append((label, names))
        groups = "".join(
            '<div class="credit-group"><dt>{}</dt><dd>{}</dd></div>'.format(
                esc(label), esc("、".join(names)),
            )
            for label, names in merged_groups
        )
        credits_html = f'<dl class="credits">{groups}</dl>'
    else:
        credits_html = f'<div class="empty"><p>{i18n.translate("common.none", ctx.lang)}</p></div>'

    def _entity_card(item: Mapping[str, Any]) -> str:
        name = str(item.get("name") or "")
        kind = "circle" if str(item.get("kind") or "") == "circle" else "artist"
        kind_label = "社团" if kind == "circle" else "艺人"
        # chip 上的前缀可以用 role_label 覆盖(制作人员要显示「职责 · 人名」)
        chip_label = str(item.get("role_label") or kind_label)
        href = safe_url(item.get("href") or "")
        image = safe_url(item.get("image") or "")
        if image:
            thumb = (f'<img class="entity-logo" src="{esc(image)}" alt=""'
                     ' loading="lazy" decoding="async" referrerpolicy="no-referrer">')
        else:
            thumb = (f'<span class="entity-logo entity-logo-text" aria-hidden="true">'
                     f'{esc(name[:1] or "?")}</span>')
        meta_rows = "".join(
            f'<span class="entity-meta-item"><b>{esc(label)}</b>{esc(value)}</span>'
            for label, value in (item.get("meta") or []) if value
        )
        desc = str(item.get("description") or "").strip()
        desc_html = f'<p class="entity-desc">{esc(desc[:110])}</p>' if desc else ""
        open_link = (f'<a class="entity-open" href="{esc(href)}">查看{kind_label}页 →</a>'
                     if href else "")
        following = bool(item.get("following"))
        follow_form = (
            '<form class="entity-follow" method="post" action="/follows/toggle">'
            f'<input type="hidden" name="csrf_token" value="{esc(ctx.csrf_token)}">'
            f'<input type="hidden" name="kind" value="{esc(kind)}">'
            f'<input type="hidden" name="key" value="{esc(name)}">'
            f'<input type="hidden" name="next" value="/album/{esc(quote_segment(str(album.get("key") or "")))}">'
            f'<button type="submit" class="btn-ghost btn-xs">'
            f'{"已关注 ✓" if following else "关注"}</button></form>'
        )
        card = (
            f'<span class="entity-item" data-kind="{esc(kind)}" tabindex="0">'
            f'<span class="entity-chip">{esc(chip_label)} · <b>{esc(name)}</b></span>'
            f'<span class="entity-card" role="group" aria-label="{esc(name)} 信息">'
            f"{thumb}"
            '<span class="entity-main">'
            f'<span class="entity-name">{esc(name)}<em>{kind_label}</em></span>'
            f'<span class="entity-meta">{meta_rows}</span>'
            f"{desc_html}{open_link}</span>"
            f"{follow_form}</span></span>"
        )
        return card

    # 「艺人与社团」不再单独成区块, 实体卡片(可关注)**内嵌进 people 区块**
    # (用户要求: 艺人与社团应当与制作人员合并并排第二)。
    # entities 里已在 people-chips 渲染过的艺人不再重复输出;
    # 只有"people 没覆盖到"的条目才额外补一块(通常为空)。

    # ② 制作人员: **艺人 + 词条, 全部用 chip 按钮呈现**
    #    用户要求: 不要"上面一行文字 + 下面一个按钮"的重复结构,
    #    直接把名字做成按钮即可。社团不在此列(它在标题后)。
    # ② 制作人员: **只有「职责 · 人名」按钮**, 与编辑页**完全一致**。
    #
    # 规则与编辑页 `_chip_specs` 保持同源(唯一真相):
    #   1. 先按 album.artists 的顺序放入 (人名, 职责="")
    #   2. 再遍历词条: 人名已在且还没职责 -> 补上职责(仍是一条);
    #      已在且有职责 -> 跳过; 不在 -> 追加
    #   => **每个人只有一条按钮**, 顺序由 artists 打头。
    _artist_names = [str(a).strip() for a in (album.get("artists") or [])
                     if str(a).strip()]
    _artist_entities = {
        str((it or {}).get("name") or ""): it for it in (entities or [])
        if str((it or {}).get("kind") or "") == "artist"
    }
    _circle_tokens = {"circle", "社团", "社團", "サークル"}
    # 收集 (人名, 职责) 词条对(circle 类跳过: 社团已在标题后)
    _credit_pairs: list[tuple[str, str]] = []
    if credits:
        for _label, _names in merged_groups:
            _text_label = str(_label).strip()
            if (_text_label.lower() in _circle_tokens
                    or _text_label in _circle_tokens):
                continue
            for _one in _names:
                _person = str(_one).strip()
                if _person:
                    _credit_pairs.append((_person, _text_label))

    # ---- 与编辑页完全相同的合并算法 ----
    _chip_specs: list[tuple[str, str]] = [
        (_name, "") for _name in _artist_names]
    for _person, _role in _credit_pairs:
        _hit = next((_i for _i, (_a, _r) in enumerate(_chip_specs)
                     if _a == _person), -1)
        if _hit >= 0:
            if not _chip_specs[_hit][1] and _role:
                _chip_specs[_hit] = (_person, _role)   # 补职责, 仍一条
            continue                                    # 已有职责 -> 跳过
        _chip_specs.append((_person, _role))

    def _person_chip(person: str, role: str) -> str:
        """一个制作人员按钮.

        有实体卡片(entities 里有这个艺人)时用 ``_entity_card`` —— 它自带
        **悬停详情 + 关注表单**(点按钮可以关注); 没有实体的人才退化成只读 chip。
        外观由 CSS 统一, 两种都是同一种胶囊按钮。
        """
        _kind = role if role else "艺人"
        _item = _artist_entities.get(person)
        if _item is not None:
            # 把职责塞进卡片, 让 chip 显示「职责 · 人名」
            _card_item = dict(_item)
            _card_item["role_label"] = _kind
            return _entity_card(_card_item)
        return (
            '<span class="credit-chip"><span class="credit-chip-kind">'
            '{kind}</span> <b>{person}</b></span>'.format(
                kind=esc(_kind), person=esc(person)))

    _people_chips_parts = [_person_chip(_p, _r) for _p, _r in _chip_specs]
    people_html = (
        '<div class="people-chips">{}</div>'.format(
            "".join(_people_chips_parts))
        if _people_chips_parts
        else '<div class="empty"><p>{}</p></div>'.format(
            i18n.translate("common.none", ctx.lang))
    )

    # **保留原始 position**: 过滤掉禁用站后序号不能重新编号,
    # 否则按钮的 /link/<key>/<n> 会指向错的一条(实测第 2 个按钮 404)。
    # 外链过滤: 要么是**允许的普通外链**, 要么是**用户粘贴的 embed code**
    #   embed code 不是 URL, outbound_link_allowed() 会判 False 并整个丢掉,
    #   结果播放器永远出不来 —— 所以这里单独放行能抽出 iframe src 的条目。
    from .embed import extract_embed as _extract_embed

    def _link_usable(_link: Mapping[str, Any]) -> bool:
        """**所有外链都显示** —— 不再按来源站点过滤."""
        return bool(safe_url(_link.get("href")))

    links = [link for link in list(album.get("urls") or [])
             if _link_usable(link)]
    if links:
        # 外链显示用户自己填的链接名(circle_site / x / youtube ...)。
        # **所有站点的链接都显示**(没有域名黑名单)。
        # 仍走本站 /link/ 中转; 序号用数据层的原始 position, 与 /link 一致。
        # **data-external**: 让 public.js 的 setupExternalLinks 弹"跳转站外"确认框。
        # 之前漏了这个属性, 外链点击没有任何提示(用户反馈"警告没了")。
        _album_key = str(album.get("key") or "")
        # 嵌入式播放器: **只渲染用户自己粘贴的 embed code**
        #   (纯链接不生成播放器 —— 用户明确要求)
        #   src 是**本站** /embed/<key>/<n>, 服务端 302 到 embed code 里的地址;
        #   页面 HTML 里不出现外部 URL/域名(与 /link 同一套合规约束)。
        _embeds: list[str] = []
        for _index, _link in enumerate(links, 1):
            _platform = str(_link.get("embed") or "")
            if not _platform:
                continue
            _position = int(_link.get("position") or _index)
            _height = int(_link.get("embed_height") or 0)
            _style = (' style="height:{}px"'.format(_height)
                      if _height else "")
            _src = safe_url(str(_link.get("embed_src") or ""))
            if not _src:
                continue
            # iframe **直连平台**: 走本站 /embed 中转会被 CSP 在跳转目标上拦掉
            # (显示"已阻止此内容")。CSP frame-src 已放行 youtube / soundcloud。
            _embeds.append(
                '<figure class="embed-player" data-embed="{}">'
                '<iframe src="{}" loading="lazy"{style}'
                ' frameborder="0" allowfullscreen scrolling="no"'
                ' allow="autoplay; encrypted-media; picture-in-picture; fullscreen"'
                ' referrerpolicy="strict-origin-when-cross-origin"'
                ' title="{} 播放器"></iframe>'
                "</figure>".format(
                    esc(_platform),
                    esc(_src),
                    esc(_platform),
                    style=_style,
                ))
        # 播放器**单独一块, 放在链接按钮下方**(不要塞进 .links —— 那是 flex
        # 容器, 会把按钮和 iframe 拉成同一行, 按钮被 iframe 高度撑长)。
        embeds_html = ('<div class="embed-list">' + "".join(_embeds)
                       + "</div>") if _embeds else ""
        # **embed code 的条目不出按钮**: 它已经有下方播放器承载,
        #   再把用户填的名字(如 "Embed Code (Youtube)")显示出来既冗余又暴露实现细节。
        #   所以只渲染普通外链按钮; embed 条目靠 embed-list 呈现。
        links_html = "".join(
            '<a class="link-pill" href="{}" data-external="1"'
            ' rel="nofollow noopener">{}</a>'.format(
                # href 里的序号来自 data 层的原始 position, 不受过滤影响
                esc(safe_url(link.get("href"))),
                esc(_link_label(link, int(link.get("position") or index))),
            )
            for index, link in enumerate(links, 1)
            if not str(link.get("embed") or "")
        )
        # 全是 embed code 时按钮区为空: 不要留一个空 div, 直接不输出
        links_block = ('<div class="links">{}</div>'.format(links_html)
                       if links_html else "")
    else:
        links_html = ""
        links_block = '<span class="muted">没有外链记录。</span>'
        embeds_html = ""

    description = album.get("description") or ""
    desc_html = ""
    if description:
        paragraphs = "".join(
            f"<p>{esc(chunk)}</p>" for chunk in re.split(r"\r?\n+", description) if chunk.strip()
        )
        desc_html = f'<div class="description">{paragraphs}</div>'
    else:
        desc_html = f'<div class="empty"><p>{i18n.translate("detail.no_description", ctx.lang)}</p></div>'

    tag_html = "".join(
        '<a class="tag" href="/tag/{}">{}</a>'.format(esc(quote_segment(t)), esc(t))
        for t in (album.get("tags") or [])
    )
    genre_html = "".join(
        '<a class="tag tag-genre" href="/albums?q={}">{}</a>'.format(esc(quote_segment(g)), esc(g))
        for g in (album.get("genres") or [])
    )

    related_html = ""
    if related:
        related_html = (
            f'<section class="panel"><h2 class="section-title">{i18n.translate("section.related", ctx.lang)}</h2>'
            + album_grid(related)
            + "</section>"
        )

    entities_html = ""
    _shown_names = set(_artist_names)
    _leftover = [
        it for it in (entities or [])
        if str((it or {}).get("kind") or "") == "artist"
        and str((it or {}).get("name") or "") not in _shown_names
    ]
    if _leftover:
        _left_cards = "".join(_entity_card(it) for it in _leftover)
        entities_html = (
            '<div class="entity-panel"><div class="entities">'
            + _left_cards
            + "</div></div>"
        )

    edit_btn = ""
    if editor and edit_href:
        # 编辑 + 修改记录 成对出现: 用户改完能直接点进去看历史/回滚
        history_href = edit_href[: -len("/edit")] + "/history"
        edit_btn = (
            f'<a class="btn-ghost" href="{esc(edit_href)}">编辑这张专辑</a>'
            f'<a class="btn-ghost" href="{esc(history_href)}">修改记录</a>'
        )

    # 详情页区块顺序(用户要求):
    #   ① 社团(紧跟标题) ② 艺人/社团 + 制作人员(合并) ③ 简介 ④ 曲目表(可折叠) ⑤ 外链
    # 编辑/修改记录按钮: **放在卡片上方**(用户要求), 不再挤在标题行里。
    detail_actions = (
    f'<div class="detail-actions">{edit_btn}</div>' if edit_btn else ""
    )
    body = f"""<div class="detail-wrap">
{detail_actions}
<div class="detail">
  <div class="detail-cover">{cover_html(album, size="detail", lazy=False)}</div>
  <div class="detail-info">
    <div class="detail-head">
      <h2 class="detail-title">{esc(title)}</h2>
      {alt_html}
      {circle_line}
      <div class="badges">{badges_for(album)}</div>
    </div>
    <dl class="info">{info_rows}</dl>
    <div class="tagrow tagrow-wrap">{tag_html}</div>
    <div class="tagrow tagrow-wrap">{genre_html}</div>
    {missing_html}
  </div>
</div>
</div>
<section class="panel">
  <h2 class="section-title">{i18n.translate("section.credits", ctx.lang)}</h2>
  {people_html}
  {entities_html}
</section>
<section class="panel">
  <h2 class="section-title">{i18n.translate("section.description", ctx.lang)}</h2>
  {desc_html}
</section>
<details class="panel fold-panel" open>
  <summary class="section-title fold-title">
    {i18n.translate("section.tracks", ctx.lang)}
    <span class="fold-count">{track_total} 首</span>
    <span class="fold-hint">点击展开/收起</span>
  </summary>
  <div class="fold-body">{tracks_html}</div>
</details>
<section class="panel"><h2 class="section-title">{i18n.translate("section.links", ctx.lang)}</h2>{links_block}{embeds_html}</section>
{related_html}"""
    body = body + comments_section(
        ctx, album=album, comments=comments,
        circle_name=circle_name, following_circle=following_circle,
        following_artists=following_artists, cover_upload=cover_upload,
        comment_likes=comment_likes, liked_ids=liked_ids,
    )
    return layout(
        ctx,
        title=title,
        body=body,
        active="albums",
        breadcrumbs=[("浏览", "/albums"), (title, "")],
    )


def circle_page(
    ctx: SiteContext,
    *,
    name: str,
    items: Sequence[Mapping[str, Any]],
    pager: Mapping[str, Any],
    url_for: Callable[..., str],
    variants: Sequence[str] = (),
    following: bool = False,
) -> str:
    variant_html = ""
    if len(variants) > 1:
        variant_html = '<p class="section-note">库中这个社团出现过 {} 种写法: {}</p>'.format(
            esc(len(variants)), esc(" / ".join(variants))
        )
    follow_html = ""
    if name:
        follow_html = (
            '<p class="section-note"><form class="inline-form" method="post" action="/follows/toggle">'
            + _hidden("csrf_token", ctx.csrf_token)
            + _hidden("kind", "circle") + _hidden("key", name)
            + _hidden("next", "/circle/" + quote_segment(name))
            + '<button type="submit" class="btn-ghost btn-xs">'
            + ("取消关注" if following else "关注这个社团") + "</button></form></p>"
        )
    body = (
        follow_html
        + f'{variant_html}<p class="results-count">共 <b>{esc(pager.get("total", 0))}</b> 张专辑</p>'
        + album_grid(items, empty_text="这个社团名下暂时没有专辑。")
        + pagination(pager, url_for)
    )
    return layout(
        ctx, title=name or "(未知社团)", body=body, active="circles",
        breadcrumbs=[("社团", "/circles"), (name or "", "")],
    )


def event_series_edit_page(
    ctx: SiteContext,
    *,
    series: Mapping[str, Any],
    errors: Mapping[str, Any] | None = None,
    notice: str = "",
    creating: bool = False,
) -> str:
    """展会系列编辑: LOGO/海报上传 + MD wiki + 相关链接."""
    errs = errors or {}
    name = str(series.get("name") or "")
    notice_html = (f'<div class="form-error" role="alert">{esc(notice)}</div>'
                   if notice else "")
    messages = [str(v) for v in errs.values() if str(v or "").strip()]
    summary = (
        '<div class="form-error" role="alert">有 {} 处需要修正:<ul '
        'class="form-error-list">{}</ul></div>'.format(
            len(messages),
            "".join(f"<li>{esc(m)}</li>" for m in messages))
        if messages else "")

    action = ("/event-series/new" if creating else
              "/event-series/{}/edit".format(quote_segment(name)))

    def _img_preview(kind: str, label: str, url_key: str, local_key: str) -> str:
        local = str(series.get(local_key) or "")
        remote = str(series.get(url_key) or "")
        src = ("/event-series-{}/{}".format(kind, quote_segment(name))
               if local and not creating else (safe_url(remote) or ""))
        preview = ('<img class="series-edit-thumb" src="{}" alt="{}">'
                   .format(esc(src), esc(label)) if src else
                   '<span class="series-edit-thumb series-edit-thumb-text">'
                   '无图</span>')
        return (
            '<div class="field auth-field series-edit-image">'
            '<span>{label}</span>'
            '<div class="series-edit-image-row">{preview}'
            '<div class="series-edit-image-form">'
            '<input type="file" name="{kind}" accept="image/png,image/jpeg,image/webp">'
            '<input type="text" name="{url_key}" value="{remote}"'
            ' placeholder="或填图片 URL">'
            '<p class="field-hint">PNG / JPEG / WebP，≤2MB；上传优先于 URL</p>'
            '</div></div></div>'
        ).format(label=esc(label), preview=preview, kind=esc(kind),
                 url_key=esc(url_key), remote=esc(remote))

    links_text = "\n".join(
        "{}: {}".format(k, v) for k, v in (series.get("links") or {}).items())

    body = (
        '{notice}{summary}'
        '<p class="section-note">{intro}</p>'
        '<form class="edit-form" method="post" action="{action}"'
        ' enctype="multipart/form-data" data-entity-form="event_series">'
        '<input type="hidden" name="csrf_token" value="{token}">'
        '<section class="edit-card" data-card="series-info">'
        '<div class="edit-card-head"><h2 class="section-title">基本信息</h2></div>'
        '<div class="edit-card-body"><div class="form-grid">'
        '<label class="field auth-field"><span>系列名<span class="field-hint">'
        '唯一标识</span></span>'
        '<input type="text" name="name" value="{name}"{name_ro} required></label>'
        '<label class="field auth-field"><span>缩写</span>'
        '<input type="text" name="abbr" value="{abbr}" placeholder="如 M3 / CM"></label>'
        '<label class="field auth-field"><span>全称</span>'
        '<input type="text" name="full_name" value="{full}"'
        ' placeholder="如 Comic Market"></label>'
        '</div></div></section>'
        '<section class="edit-card" data-card="series-image">'
        '<div class="edit-card-head"><h2 class="section-title">图片</h2></div>'
        '<div class="edit-card-body">{logo}{poster}</div></section>'
        '<section class="edit-card" data-card="series-wiki">'
        '<div class="edit-card-head"><h2 class="section-title">介绍'
        '<span class="section-sub">Markdown：支持图片与外链</span></h2></div>'
        '<div class="edit-card-body">'
        '<label class="field auth-field"><textarea name="wiki" rows="12"'
        ' placeholder="# 标题\n\n正文，可以写 **粗体**、[链接](https://...)、'
        '![图片](https://...)">'
        '{wiki}</textarea></label></div></section>'
        '<section class="edit-card" data-card="series-links">'
        '<div class="edit-card-head"><h2 class="section-title">相关链接</h2></div>'
        '<div class="edit-card-body">'
        '<label class="field auth-field"><textarea name="links" rows="4"'
        ' placeholder="每行: 标签: URL">'
        '{links}</textarea></label></div></section>'
        '<div class="field-actions edit-actions">'
        '<button type="submit">保存</button>'
        '<a class="btn-ghost" href="{cancel}">取消</a>'
        '</div></form>'
    ).format(
        notice=notice_html, summary=summary,
        intro=("新建一个展会系列。建好后可以在系列页里添加会次。"
               if creating else
               "编辑展会系列信息。会次在「编辑 → 新建展会」里创建，"
               "并把系列填成本系列名。"),
        action=esc(action), token=esc(ctx.csrf_token),
        name=esc(name), name_ro="" if creating else " readonly",
        abbr=esc(series.get("abbr") or ""),
        full=esc(series.get("full_name") or ""),
        logo=_img_preview("logo", "LOGO", "logo_url", "logo_local"),
        poster=_img_preview("poster", "海报", "poster_url", "poster_local"),
        wiki=esc(series.get("wiki") or ""),
        links=esc(links_text),
        cancel=esc("/events" if creating else
                   "/event-series/{}".format(quote_segment(name))),
    )
    return layout(
        ctx, title=("新建展会系列" if creating else "编辑展会系列"),
        body=body, active="events", wide=True,
        breadcrumbs=[("展会", "/events"),
                     (str(series.get("full_name") or name or "新建"), ""),
                     ("编辑" if not creating else "新建", "")],
    )


def event_series_page(
    ctx: SiteContext,
    *,
    series: Mapping[str, Any],
    editions: Sequence[Mapping[str, Any]] = (),
    can_edit: bool = False,
    notice: str = "",
) -> str:
    """展会**系列**详情: 大图 + MD wiki + 会次图片网格.

    ``editions`` 每项: ``{"value", "display", "count", "logo", "vol_label"}``。
    点会次图 -> ``/event/<name>``(该会次的专辑筛选)。
    """
    name = str(series.get("name") or "")
    abbr = str(series.get("abbr") or "")
    full = str(series.get("full_name") or name)
    logo = str(series.get("logo") or "")
    poster = str(series.get("poster") or "")
    title = "[{}] {}".format(esc(abbr), esc(full)) if abbr else esc(full)

    # 顶部大图: 优先用海报, 没有就用 LOGO
    big = poster or logo
    if big:
        hero = ('<img class="series-hero" src="{}" alt="{}" loading="lazy">'
                .format(esc(safe_url(big)), esc(full)))
    else:
        hero = ('<div class="series-hero series-hero-text">{}</div>'
                .format(esc((abbr or full or "?")[:3])))

    # MD wiki -> HTML(与编辑页同一套渲染)
    wiki_html = ""
    wiki_src = str(series.get("wiki") or "")
    if wiki_src.strip():
        wiki_html = ('<section class="panel"><h2 class="section-title">介绍</h2>'
                     '<div class="md-body">{}</div></section>'
                     .format(markdown_to_html(wiki_src)))
    elif can_edit:
        wiki_html = ('<section class="panel"><h2 class="section-title">介绍</h2>'
                     '<p class="section-note">还没有介绍。点右上角「编辑」用 '
                     'Markdown 写一段，支持图片与外链。</p></section>')

    # 会次: 图片卡片网格
    cards = []
    for item in editions:
        ev_name = str(item.get("value") or "")
        display = str(item.get("display") or ev_name)
        count = int(item.get("count") or 0)
        pic = str(item.get("logo") or "")
        if pic:
            thumb = ('<img class="edition-thumb" src="{}" alt="{}"'
                     ' loading="lazy">').format(esc(safe_url(pic)), esc(display))
        else:
            tail = str(item.get("vol_label") or ev_name)
            thumb = ('<span class="edition-thumb edition-thumb-text">{}</span>'
                     .format(esc(tail[:8])))
        cards.append(
            '<a class="edition-card" href="/event/{}">{}{}'
            '<span class="edition-count">{} 张</span></a>'.format(
                esc(quote_segment(ev_name)), thumb,
                '<span class="edition-name">{}</span>'.format(esc(display)),
                esc(count)))
    editions_html = (
        '<div class="edition-grid">{}</div>'.format("".join(cards))
        if cards else '<p class="section-note">这个系列还没有会次。</p>')

    edit_btn = ('<p class="section-note"><a class="btn-ghost btn-xs"'
                ' href="/event-series/{}/edit">编辑系列</a></p>'
                .format(esc(quote_segment(name)))) if can_edit else ""

    # 相关链接: 之前**完全没渲染** —— 用户填了也看不到。
    _links = series.get("links") or {}
    link_html = "".join(
        '<a class="link-pill" href="{}" data-external="1"'
        ' rel="nofollow noopener">{}</a>'.format(
            esc(safe_url(url)), esc(label or url))
        for label, url in _links.items()
        if safe_url(url)
    ) if isinstance(_links, Mapping) else ""
    links_block = ('<div class="links series-links">{}</div>'.format(link_html)
                   if link_html else "")

    body = (
        '{notice}'
        '<section class="panel series-head">'
        '<div class="series-hero-wrap">{hero}</div>'
        '<h2 class="series-title">{title}</h2>'
        '{edit_btn}'
        '<p class="section-note">共 <b>{total}</b> 个会次 · <b>{albums}</b> 张专辑</p>'
        '{links_block}'
        '</section>'
        '{wiki}'
        '<section class="panel"><h2 class="section-title">会次<span '
        'class="section-sub">点图进入该会次的专辑筛选</span></h2>'
        '{editions}</section>'
    ).format(
        notice=(f'<div class="form-error" role="alert">{esc(notice)}</div>'
                if notice else ""),
        hero=hero, title=title, edit_btn=edit_btn,
        total=esc(len(list(editions))),
        albums=esc(sum(int(e.get("count") or 0) for e in editions)),
        links_block=links_block,
        wiki=wiki_html, editions=editions_html,
    )
    return layout(
        ctx, title=f"{abbr or full} · 展会系列", body=body, active="events",
        breadcrumbs=[("展会", "/events"), (full or name, "")],
    )


def event_page(
    ctx: SiteContext,
    *,
    name: str,
    kind: str,
    items: Sequence[Mapping[str, Any]],
    groups: Sequence[Mapping[str, Any]],
    group_by: str,
    total: int,
    url_for: Callable[..., str],
    logo: str = "",
    links: Mapping[str, str] | None = None,
) -> str:
    toggle = (
        '<div class="toggle">'
        '<a class="{a}" href="{ha}">按社团</a>'
        '<a class="{b}" href="{hb}">按日期</a>'
        "</div>"
    ).format(
        a="toggle-link is-active" if group_by == "circle" else "toggle-link",
        b="toggle-link is-active" if group_by == "date" else "toggle-link",
        ha=esc(url_for(group="circle")),
        hb=esc(url_for(group="date")),
    )
    blocks: list[str] = []
    for group in groups:
        group_items = list(group.get("items") or [])
        blocks.append(
            '<section class="group"><h3 class="group-title">{}<span class="group-count">{} 张</span></h3>{}</section>'.format(
                esc(group.get("label") or "(未标注)"),
                esc(group.get("count", len(group_items))),
                album_grid(group_items, empty_text=""),
            )
        )
    if group_by != "none" and not blocks:
        blocks.append(album_grid(items, empty_text="这场展会名下暂时没有专辑。"))
    # 展会词条自带的图片与相关链接(可都没有)
    _links = links or {}
    _link_html = "".join(
        '<a class="link-pill" href="{}" data-external="1"'
        ' rel="nofollow noopener">{}</a>'.format(
            esc(safe_url(url)), esc(label or url))
        for label, url in _links.items()
        if safe_url(url)
    )
    _media_html = ""
    if logo or _link_html:
        _logo_html = (
            '<img class="event-logo" src="{}" alt="{}" loading="lazy">'.format(
                esc(safe_url(logo)), esc(name))
            if logo else
            '<span class="event-logo event-logo-text">{}</span>'.format(
                esc((name or "?")[:1]))
        )
        _media_html = (
            '<div class="event-media">'
            '<div class="event-media-logo">{}</div>'
            '<div class="event-media-links">{}</div>'
            '</div>'.format(_logo_html, _link_html or
                            '<span class="muted">还没有相关链接。</span>')
        )
    body = (
        _media_html
        + '<div class="event-head"><p class="results-count">共 <b>{}</b> 张专辑 · {}</p>{}</div>'.format(
            esc(total), esc(group_by_label(group_by)), toggle
        )
        + "".join(blocks)
    )
    _title_actions = ""
    if ctx.is_editor:
        _title_actions = (
            f'<a class="btn-ghost btn-xs" href="/new/event">{i18n.translate("new.event", ctx.lang)}</a>'
            + f'<a class="btn-ghost btn-xs" href="/event/'
            + quote_segment(name) + f'/edit">{i18n.translate("edit.profile", ctx.lang)}</a>'
        )
    return layout(
        ctx, title_actions=_title_actions,
        title=name or "(未知展会)",
        subtitle=event_kind_label(kind),
        body=body,
        active="events",
        breadcrumbs=[("展会", "/events"), (name or "", "")],
        wide=True,
    )


def group_by_label(group_by: str) -> str:
    return {"circle": "按社团分组", "date": "按发售日分组", "none": "不分组"}.get(group_by, "按社团分组")


def tag_page(
    ctx: SiteContext,
    *,
    tag: str,
    items: Sequence[Mapping[str, Any]],
    pager: Mapping[str, Any],
    url_for: Callable[..., str],
) -> str:
    body = album_results(ctx, items=items, pager=pager, filters={"tag": tag}, url_for=url_for)
    _title_actions = ""
    if ctx.is_editor:
        _title_actions = (
            f'<a class="btn-ghost btn-xs" href="/new/tag">{i18n.translate("new.tag", ctx.lang)}</a>'
            + f'<a class="btn-ghost btn-xs" href="/tag/'
            + quote_segment(tag) + f'/edit">{i18n.translate("edit.profile", ctx.lang)}</a>'
        )
    return layout(
        ctx, title_actions=_title_actions, title=f"标签: {tag}", body=body, active="tags",
        breadcrumbs=[("标签", "/tags"), (tag, "")],
    )


def circles_index_page(
    ctx: SiteContext, *, circles: Sequence[Mapping[str, Any]], pager: Mapping[str, Any], url_for: Callable[..., str],
    query: str = "",
) -> str:
    # 每行 = 名字(左, 自动占满) + 数量(右, 固定右对齐列); 结构对齐见 .index-row
    rows = "".join(
        '<li class="index-row"><a class="index-name" href="/circle/{}">{}</a>'
        '<span class="index-count">{} 张</span></li>'.format(
            esc(quote_segment(c.get("value") or "")), esc(c.get("value") or ""), esc(c.get("count") or 0)
        )
        for c in circles
    )
    form = (
        '<form class="filters filters-inline" method="get" action="/circles">'
        '<label class="field"><span>社团名</span>'
        f'<input type="search" name="q" value="{esc(query)}" placeholder="输入社团名筛选"></label>'
        '<div class="field-actions"><button type="submit">筛选</button>'
        '<a class="btn-ghost" href="/circles">重置</a></div></form>'
    )
    body = (
        f"{form}<p class=\"results-count\">共 <b>{esc(pager.get('total', 0))}</b> 个社团</p>"
        f'<ul class="index-list">{rows}</ul>'
        + pagination(pager, url_for)
    )
    return layout(ctx, title="社团索引", body=body, active="circles", subtitle="按收录数量排序")


def events_index_page(
    ctx: SiteContext,
    *,
    events: Sequence[Mapping[str, Any]] = (),
    kinds: Sequence[Mapping[str, Any]] = (),
    series: Sequence[Mapping[str, Any]] = (),
    sort: str = "time",
    order: str = "desc",
) -> str:
    """展会索引: **每个系列一张卡片**, 两列网格.

    卡片结构::

        ┌──────────────┐
        │   LOGO/海报    │   ← 点进系列详情
        ├──────────────┤
        │ [M3] M3-2026秋 (M3-58) │
        │ [M3] M3-2026春 (M3-57) │
        │ ...（默认 5 条）        │
        │        展开             │
        └──────────────┘

    ``series`` 是 :meth:`PublicData.event_series` 的结果(已带会次列表);
    ``events``/``kinds`` 只在没有系列数据时兜底(老库)。
    """
    _no_logo = ('<span class="series-card-logo series-card-logo-text">'
                '{}</span>')

    def _card(item: Mapping[str, Any]) -> str:
        name = str(item.get("name") or "")
        abbr = str(item.get("abbr") or "")
        full = str(item.get("full_name") or name)
        logo = str(item.get("logo") or "")
        editions = list(item.get("editions") or ())
        # 卡片标题: 有缩写显示 "[M3] M3", 否则只显示全称
        title = "[{}] {}".format(esc(abbr), esc(full)) if abbr else esc(full)
        thumb = (
            '<img class="series-card-logo" src="{}" alt="{}" loading="lazy">'
            .format(esc(safe_url(logo)), esc(name))
            if logo else _no_logo.format(esc((abbr or full or "?")[:2]))
        )
        rows = "".join(
            '<li><a class="series-edition" href="/event/{}">{}</a>'
            '<span class="series-edition-count">{} 张</span></li>'.format(
                esc(quote_segment(str(e.get("value") or ""))),
                esc(str(e.get("display") or e.get("value") or "")),
                esc(e.get("count") or 0),
            )
            for e in editions
        )
        hidden = max(0, len(editions) - 5)
        more = ('<button type="button" class="btn-ghost btn-xs series-more"'
                ' data-series-more="1" aria-expanded="false">'
                '展开 {} 条</button>'.format(hidden)) if hidden else ""
        return (
            '<article class="series-card" data-series-card="1">'
            '<a class="series-card-head" href="/event-series/{}">{thumb}'
            '<span class="series-card-title">{title}</span></a>'
            '<ul class="series-editions" data-series-editions="1">{rows}</ul>'
            '<div class="series-card-foot">{more}</div>'
            '</article>'
        ).format(
            esc(quote_segment(name)),
            thumb=thumb,
            title=title,
            rows=rows or '<li class="muted">还没有会次</li>',
            more=more,
        )

    cards = "".join(_card(s) for s in series)
    if not cards:
        # 没有任何系列: 退回列表(老库/空库)
        rows = "".join(
            '<li><a class="index-name" href="/event/{}">{}</a>'
            '<span class="index-count">{} 张</span></li>'.format(
                esc(quote_segment(str(e.get("value") or ""))),
                esc(str(e.get("value") or "")),
                esc(e.get("count") or 0),
            )
            for e in events
        )
        cards = ('<p class="section-note">还没有展会系列。</p>'
                 '<ul class="index-list">{}</ul>'.format(
                     rows or "<li>暂无展会数据</li>"))
    else:
        cards = '<div class="series-grid">{}</div>'.format(cards)

    kind_pills = "".join(
        '<a class="pill" href="/albums?kind={}"><b>{}</b><span>{} 张</span></a>'.format(
            esc(k.get("value") or ""), esc(k.get("label") or ""),
            esc(k.get("count") or 0))
        for k in kinds
    )
    # 排序控件: 两个维度 × 两个方向, 当前状态高亮
    _sort = "count" if str(sort).lower() == "count" else "time"
    _order = "asc" if str(order).lower() == "asc" else "desc"

    def _sort_link(label: str, want_sort: str) -> str:
        """点当前维度 -> 翻转方向; 点另一维度 -> 切过去(默认倒序)."""
        if want_sort == _sort:
            next_order = "asc" if _order == "desc" else "desc"
        else:
            next_order = "desc"
        active = " is-active" if want_sort == _sort else ""
        arrow = ""
        if want_sort == _sort:
            arrow = " ↑" if _order == "asc" else " ↓"
        return ('<a class="sort-pill{}{}" href="/events?sort={}&order={}">'
                '{}{}</a>'.format(
                    active, "",
                    want_sort, next_order, esc(label), arrow))

    sort_html = (
        '<div class="sort-bar"><span class="sort-label">排序</span>'
        + _sort_link("时间", "time")
        + _sort_link("专辑数量", "count")
        + "</div>"
    )

    # 标题行的「新建展会」由 layout 自动渲染(active="events"),
    # 这里**不再重复**; 只把「新建展会系列」作为额外的 title action 传上去。
    body = (sort_html
            + (f'<div class="pills">{kind_pills}</div>' if kind_pills else "")
            + cards)
    # 标题行按钮: 「新建展会」+「新建展会系列」并排。
    # 传了 title_actions 就**完全由这里控制**(layout 不会再自动补一遍),
    # 所以必须把两个都写全, 否则会丢一个。
    title_actions = ""
    if ctx.is_editor:
        title_actions = (
            '<a class="btn-ghost btn-xs" href="/new/event">新建展会</a>'
            '<a class="btn-ghost btn-xs" href="/event-series/new">'
            '＋ 新建展会系列</a>')
    return layout(ctx, title="展会", body=body, active="events",
                  title_actions=title_actions)

def tags_index_page(
    ctx: SiteContext, *, tags: Sequence[Mapping[str, Any]], limit_note: str = ""
) -> str:
    max_count = max((int(t.get("count") or 0) for t in tags), default=1)
    clouds = []
    for tag in tags:
        count = int(tag.get("count") or 0)
        level = 1 + min(4, int(count * 4 / max_count)) if max_count else 1
        clouds.append(
            '<a class="tag tag-l{}" href="/tag/{}" title="{} 张专辑">{}<span class="tag-n">{}</span></a>'.format(
                level,
                esc(quote_segment(tag.get("value") or "")),
                esc(count),
                esc(tag.get("value") or ""),
                esc(count),
            )
        )
    note = f'<p class="section-note">{esc(limit_note)}</p>' if limit_note else ""
    body = (
        f'{note}<div class="tagcloud">{"".join(clouds) or "<span class=\"muted\">暂无标签</span>"}</div>'
    )
    return layout(ctx, title="标签索引", body=body, active="tags", subtitle="字号大小 = 收录数量")


def stats_page(ctx: SiteContext, *, stats: Mapping[str, Any]) -> str:
    def stat_card(label: str, value: Any, hint: str = "") -> str:
        hint_html = f'<span class="stat-hint">{esc(hint)}</span>' if hint else ""
        return (
            '<div class="stat-card"><span class="stat-value">{}</span>'
            '<span class="stat-label">{}</span>{}</div>'.format(esc(value), esc(label), hint_html)
        )

    cards = "".join([
        stat_card("专辑", stats.get("albums")),
        stat_card("社团", stats.get("circles")),
        stat_card("展会", stats.get("events")),
        stat_card("标签", stats.get("tags")),
        stat_card("曲目", stats.get("tracks")),
        stat_card("已校订", stats.get("edited")),
    ])

    def bars(rows: Sequence[Mapping[str, Any]], *, href_base: str = "", unit: str = "张") -> str:
        if not rows:
            return '<div class="empty"><p>暂无数据</p></div>'
        top = max((int(r.get("count") or 0) for r in rows), default=1) or 1
        out = []
        for row in rows:
            count = int(row.get("count") or 0)
            width = max(2.0, round(count * 100.0 / top, 1))
            label = str(row.get("label") if row.get("label") is not None else row.get("value") or "")
            value = str(row.get("value") or "")
            name = (
                f'<a href="{esc(href_base + quote_segment(value))}">{esc(label)}</a>'
                if href_base else esc(label)
            )
            out.append(
                '<div class="bar-row"><span class="bar-label">{}</span>'
                '<span class="bar-track"><span class="bar-fill" style="width:{}%"></span></span>'
                '<span class="bar-value">{} {}</span></div>'.format(name, width, esc(count), esc(unit))
            )
        return "".join(out)

    coverage = stats.get("coverage") or {}
    cover_bars = bars(
        [
            {"value": "local", "label": "已缓存封面(本地)", "count": coverage.get("local", 0)},
            {"value": "remote", "label": "仅有远程封面链接", "count": coverage.get("remote", 0)},
            {"value": "none", "label": "完全无封面", "count": coverage.get("none", 0)},
        ]
    )
    cover_rate = coverage.get("rate")
    completeness = stats.get("completeness") or {}
    compl_bars = bars([
        {"value": "no_tracks", "label": NEEDS_LABELS["no_tracks"], "count": completeness.get("no_tracks", 0)},
        {"value": "no_desc", "label": NEEDS_LABELS["no_desc"], "count": completeness.get("no_desc", 0)},
        {"value": "no_cover", "label": NEEDS_LABELS["no_cover"], "count": completeness.get("no_cover", 0)},
        {"value": "no_circle", "label": NEEDS_LABELS["no_circle"], "count": completeness.get("no_circle", 0)},
        {"value": "no_date", "label": NEEDS_LABELS["no_date"], "count": completeness.get("no_date", 0)},
    ])

    # 「封面覆盖率」「信息完整度」是**编辑者的工作指标**(缺多少要补多少),
    # 普通用户看板只保留内容统计, 不出现这两块。
    editor_blocks = ""
    if ctx.is_editor:
        editor_blocks = f"""<section class="panel">
  <h2 class="section-title">封面覆盖率<span class="section-sub">{esc(cover_rate if cover_rate is not None else 0)}%</span></h2>
  {cover_bars}
</section>
<section class="panel">
  <h2 class="section-title">信息完整度<span class="section-sub">缺口越少越好</span></h2>
  {compl_bars}
  <p class="more"><a href="/edit">打开工作队列 →</a></p>
</section>"""

    body = f"""<section class="stat-grid">{cards}</section>
{editor_blocks}
<section class="panel">
  <h2 class="section-title">年份趋势<span class="section-sub">按发售年份</span></h2>
  {bars(stats.get("years") or [])}
</section>
<section class="panel">
  <h2 class="section-title">展会分布<span class="section-sub">Top 12</span></h2>
  {bars(stats.get("event_top") or [], href_base="/event/")}
</section>
<section class="panel">
  <h2 class="section-title">展会类型</h2>
  {bars(stats.get("kinds") or [])}
</section>
<section class="panel">
  <h2 class="section-title">曲风 Top 20</h2>
  {bars(stats.get("genres") or [])}
</section>
<section class="panel">
  <h2 class="section-title">标签 Top 20</h2>
  <div class="tagcloud">{"".join('<a class="tag" href="/tag/%s">%s<span class="tag-n">%s</span></a>' % (esc(quote_segment(t.get("value"))), esc(t.get("value")), esc(t.get("count"))) for t in (stats.get("tags_top") or [])) or '<span class="muted">暂无</span>'}</div>
  <p class="more"><a href="/tags">全部标签 →</a></p>
</section>
<section class="panel">
  <h2 class="section-title">社团 Top 10</h2>
  {bars(stats.get("circles_top") or [], href_base="/circle/")}
</section>"""
    # 副标题只说明这是站内数据的统计; 不再提"后台/歌单"这类与用户无关的东西。
    return layout(ctx, title="统计看板", body=body, active="stats",
                  subtitle="站内已收录资料的统计", wide=True)


def inbox_page(
    ctx: SiteContext,
    *,
    kinds: Sequence[Mapping[str, Any]],
    items: Sequence[Mapping[str, Any]],
    pager: Mapping[str, Any],
    active_kind: str = "",
    url_for: Callable[..., str],
    edit_url_for: Callable[[str], str] | None = None,
) -> str:
    """工作队列: 先看各缺口规模, 再逐条查缺哪一项.

    页面文案只保留"工作队列"本身; 不出现外部面板 / 数据来源之类的东西。
    """
    tabs = "".join(
        '<a class="tab{a}" href="/edit/{v}">{l}<span class="tab-count">{c}</span></a>'.format(
            a=" is-active" if str(k.get("value")) == active_kind else "",
            v=esc(k.get("value") or ""),
            l=esc(k.get("label") or ""),
            c=esc(k.get("count") or 0),
        )
        for k in kinds
    )
    if not active_kind:
        body = f'<div class="tabs">{tabs}</div>'
        return layout(ctx, title="工作队列", body=body, active="edit",
                      subtitle="按缺口分类查看还需要补充的专辑")

    def rows() -> str:
        out = []
        for item in items:
            href = safe_url(item.get("href") or "")
            missing = item.get("missing") or []
            badges = "".join(
                '<span class="badge badge-warn">{}</span>'.format(esc(NEEDS_LABELS.get(m, m))) for m in missing
            )
            edit_href = ""
            if edit_url_for is not None and ctx.is_editor:
                # 编辑入口一律指向站内的 /album/<key>/edit; 非编辑者拿不到这个按钮
                edit_href = safe_url(edit_url_for(str(item.get("key") or "")))
            edit_link = (
                f'<a class="btn-ghost btn-xs" href="{esc(edit_href)}">编辑 →</a>'
                if edit_href else ""
            )
            out.append(
                '<li class="queue-row"><a class="queue-main" href="{}"><span class="queue-title">{}</span>'
                '<span class="queue-sub">{}{}</span></a><span class="queue-badges">{}</span>{}</li>'.format(
                    esc(href),
                    esc(item.get("title") or "(无标题)"),
                    esc(item.get("circle") or "未标注社团"),
                    (" · " + esc(item.get("event"))) if item.get("event") else "",
                    badges,
                    edit_link,
                )
            )
        return "".join(out) or '<li class="queue-row"><span class="muted">这一类没有缺口, 挺好。</span></li>'

    body = (
        f'<div class="tabs">{tabs}</div>'
        f'<p class="results-count">命中 <b>{esc(pager.get("total", 0))}</b> 张</p>'
        f'<ul class="queue">{rows()}</ul>'
        + pagination(pager, url_for)
    )
    label = next((k.get("label") for k in kinds if str(k.get("value")) == active_kind), active_kind)
    return layout(
        ctx, title=f"工作队列 · {label}", body=body, active="edit",
        breadcrumbs=[("工作队列", "/edit"), (str(label), "")],
    )


def about_page(ctx: SiteContext, *, stats: Mapping[str, Any] = {}) -> str:
    """「关于本站」.
    """
    # 「工作队列」入口只给**有编辑权限**的人: 普通用户点进去只会拿到 403,
    # 所以这里对非编辑者渲染成纯文字说明, 不做成死链接。
    queue_link = ('<a href="/edit">工作队列</a>' if ctx.is_editor
                  else "工作队列(需要编辑权限)")
    board_extra = "; 另有封面覆盖率与资料完整度两块仅编辑者可见" if ctx.is_editor else ""
    body = f"""<section class="panel prose">
  <h2 class="section-title">DoujinDigDatabase是一个同人音乐专辑挖掘数据库。</h2>
  <h2 class="section-title">怎么用</h2>
  <ul>
    <li><a href="/albums">浏览</a>: 可按展会、类型、年份、标签、封面分类进行筛选, 或者搜索标题 / 社团 / 艺人 / 曲目 / 编号;</li>
    <li><a href="/events">展会</a> 与 <a href="/circles">社团</a>: 按展会或社团成批查看;</li>
    <li><a href="/tags">标签</a>: 字号越大表示收录越多;</li>
    <li><a href="/stats">看板</a>: 数量、年份与展会分布{board_extra}。</li>
  </ul>
  <h2 class="section-title">账号与权限</h2>
  <ul>
    <li><b>访客</b>: 仅浏览、搜索、看详情;</li>
    <li><b>普通用户</b>(<code>viewer</code>): 除访客基本功能外，可对专辑进行评论;</li>
    <li><b>编辑者</b>(<code>editor</code>): 可以进入 {queue_link},
        并在专辑详情页直接修改专辑信息，也可添加新条目 —— 保存后立即生效。</li>
  </ul>
  <p>编辑者身份: 注册时填入编辑者或管理员发放的<b>邀请码</b>即可获得，普通用户也可以提交申请，管理员审核通过后获得。</p>
  <h2 class="section-title">数据规模</h2>
  <ul>
    <li>{esc(stats.get("albums") or 0)} 张专辑 · {esc(stats.get("tracks") or 0)} 首曲目</li>
    <li>{esc(stats.get("circles") or 0)} 个社团 · {esc(stats.get("events") or 0)} 场展会 · {esc(stats.get("tags") or 0)} 个标签</li>
  </ul>
  <h2 class="section-title">账号与隐私声明</h2>
  <ul>
    <li>密码以加盐哈希(pbkdf2-hmac-sha256)保存, 本站不保存明文密码;</li>
    <li>登录状态用 HttpOnly 的会话 Cookie 维持, 以识别身份与防止跨站请求伪造;</li>
    <li>页面只加载同源脚本，不加载任何外部资源。</li>
  </ul>
</section>"""
    return layout(ctx, title="关于本站", body=body, active="about")


# --------------------------------------------------------------------------
# 账号页(注册 / 登录 / 我的账号)
# --------------------------------------------------------------------------


def _auth_card(title: str, subtitle: str, body: str) -> str:
    sub = f'<p class="page-subtitle">{esc(subtitle)}</p>' if subtitle else ""
    return f'<div class="auth-wrap"><section class="panel auth-card"><h2 class="section-title">{esc(title)}</h2>{sub}{body}</section></div>'


def auth_field(name: str, label: str, value: str = "", *, kind: str = "text",
               autocomplete: str = "", required: bool = True, hint: str = "",
               placeholder: str = "", field_id: str = "") -> str:
    """一个输入框(已经转义). ``hint`` 显示在标签右侧当说明."""
    req = " required" if required else ""
    ac = f' autocomplete="{esc(autocomplete)}"' if autocomplete else ""
    ph = f' placeholder="{esc(placeholder)}"' if placeholder else ""
    fid = f' id="{esc(field_id)}"' if field_id else ""
    hint_html = f'<span class="field-hint">{esc(hint)}</span>' if hint else ""
    return (
        f'<label class="field auth-field"><span>{esc(label)}{hint_html}</span>'
        f'<input type="{esc(kind)}" name="{esc(name)}" value="{esc(value)}"{req}{ac}{ph}{fid}></label>'
    )



#: 新建条目字段规格: kind -> (标题, [(key, 标签, 类型, 提示)])
#: **这里是唯一来源**: 以后给实体加字段, 只改 spec 与后端映射, 表单自动出现。
NEW_ENTITY_SPECS: dict[str, tuple[str, list[tuple[str, str, str, str]]]] = {
    "album": ("新建专辑", [
        ("key", "编号 / 短名", "text", "留空自动生成"),
        ("title", "标题", "text", ""),
        ("title_original", "原标题", "text", ""),
        ("title_romaji", "罗马音", "text", ""),
        ("circle", "社团", "text", ""),
        ("artists", "艺人", "text", "多人用、分隔"),
        ("release_date", "发售日", "text", "YYYY-MM-DD"),
        ("event", "展会", "text", "如 M3-2026春 / C102"),
        ("hall", "展馆", "text", ""),
        ("booth", "展位", "text", ""),
        ("catalog_no", "编号", "text", ""),
        ("price", "价格", "text", ""),
        ("cover_url", "封面 URL", "text", ""),
        ("tags", "标签", "text", "、分隔"),
        ("genres", "曲风", "text", "、分隔"),
        ("credits", "制作人员", "textarea", "每行: 角色: 名字1、名字2"),
        ("urls", "外链", "textarea", "每行: 标签: URL"),
        ("tracks", "曲目", "textarea", "每行: 曲名 - 艺人 3:42"),
    ]),
    "circle": ("新建社团", [
        ("name", "社团名", "text", ""),
        ("activity_type", "活动类型", "text", "同人社团 / 企业 等"),
        ("founded", "成立时间", "text", ""),
        ("last_active", "最后活动", "text", ""),
        ("urls", "相关链接", "textarea", "每行: 标签: URL"),
        ("staff", "成员", "textarea", "每行: 角色: 名字"),
    ]),
    "artist": ("新建艺人", [
        ("name", "艺名", "text", ""),
        ("name_en", "艺名(英)", "text", ""),
        ("name_ja", "艺名(日)", "text", ""),
        ("real_name", "本名", "text", ""),
        ("real_name_en", "本名(译)", "text", ""),
        ("aliases", "别名", "text", "、分隔"),
        ("gender", "性别", "text", ""),
        ("birthday", "生日", "text", "YYYY-MM-DD"),
        ("occupations_text", "职业", "text", "、分隔（作曲/绘画等）"),
        ("nationality", "国籍", "text", "两位国家码, 如 JP/CN"),
        ("avatar", "头像 URL", "text", ""),
        ("links", "相关链接", "textarea", "每行: 标签: URL"),
    ]),
    # 会次: 字段刻意精简 —— 「类型」= 所属系列(不重复),
    # 「会次号」从「会次标签」反推(不重复填)
    # 会次: 提示文字全部去掉(用户要求「如 ...」一律删除);
    # tags 与 vol_label 概念重叠 -> 合并, 只留会次标签
    "event": ("新建会次", [
        ("name", "会次名", "text", ""),
        ("series", "所属系列", "text", ""),
        ("vol_label", "会次标签", "text", ""),
        ("year", "年份", "text", ""),
        ("season", "季节", "text", ""),
        ("start_date", "开始日期", "date", ""),
        ("end_date", "结束日期", "date", ""),
        ("logo", "会次图", "file", ""),
        ("links", "相关链接", "textarea", ""),
    ]),
    "tag": ("新建标签", [
        ("name", "标签名", "text", ""),
        ("kind", "类型", "text", ""),
    ]),
}


def entity_new_buttons(ctx: SiteContext, kinds: Sequence[str] = ()) -> str:
    """编辑者可见的「新建条目」按钮行."""
    if not ctx.is_editor:
        return ""
    allowed = list(kinds) or list(NEW_ENTITY_SPECS)
    order = [k for k in ("album", "circle", "artist", "event", "tag") if k in allowed]
    labels = {"album": "新建专辑", "circle": "新建社团", "artist": "新建艺人",
              "event": "新建展会", "tag": "新建标签"}
    links = "".join(
        f'<a class="btn-ghost btn-xs" href="/new/{kind}">{esc(labels.get(kind, kind))}</a>'
        for kind in order if kind in NEW_ENTITY_SPECS
    )
    return links




def edit_profile_page(
    ctx: SiteContext,
    kind: str,
    name: str,
    *,
    values: Mapping[str, Any] | None = None,
    notice: str = "",
    back_url: str = "",
    series_options: Sequence[Mapping[str, Any]] = (),
    has_logo: bool = False,
) -> str:
    """云端资料录入: 复用 NEW_ENTITY_SPECS 的字段表, 预填当前值.

    ``file`` 类型渲染成**上传控件**(带已有图预览); ``series`` 渲染成下拉。
    表单是 ``multipart/form-data`` —— 否则文件不会随 POST 发出。
    """
    spec_title, fields = NEW_ENTITY_SPECS[kind]
    title = i18n.translate(f"new.{kind}", ctx.lang, default=spec_title)
    values = values or {}
    rows = []
    for key, label, field_type, help_text in fields:
        value = str(values.get(key) or "")
        hint = f'<span class="field-hint">{esc(help_text)}</span>' if help_text else ""
        locked = " readonly" if key == "name" else ""
        if field_type == "file":
            # 已有图就预览, 让用户知道现在用的是哪张
            preview = ""
            if has_logo and kind == "event":
                preview = ('<img class="series-edit-thumb" '
                           'src="/event-logo/{}" alt="{}">'
                           .format(esc(quote_segment(name)), esc(label)))
            input_html = (
                '<div class="series-edit-image-row">'
                '{preview}'
                '<div class="series-edit-image-form">'
                '<input type="file" name="{key}"'
                ' accept="image/png,image/jpeg,image/webp">'
                '<p class="field-hint">上传后替换当前图；留空则不变</p>'
                '</div></div>'
            ).format(preview=preview, key=esc(key))
        elif key == "series" and series_options:
            opts = "".join(
                '<option value="{v}"{sel}>{v}{a}</option>'.format(
                    v=esc(item.get("name") or ""),
                    sel=" selected" if value == str(item.get("name") or "")
                    else "",
                    a=(" · " + str(item.get("abbr") or ""))
                    if item.get("abbr") else "")
                for item in series_options)
            input_html = (
                '<select name="{key}">'
                '<option value="">(按会次名自动识别)</option>{opts}</select>'
            ).format(key=esc(key), opts=opts)
        elif field_type == "textarea":
            input_html = f'<textarea name="{esc(key)}" rows="4"{locked}>{esc(value)}</textarea>'
        else:
            itype = esc(field_type) if field_type == "date" else "text"
            input_html = (f'<input type="{itype}" name="{esc(key)}"'
                          f' value="{esc(value)}"{locked}>')
        # 名字是必填 -> 红色 *(不用"必填"两个汉字)
        star = ('<span class="req" aria-hidden="true">*</span>'
                if key == "name" else "")
        rows.append(
            f'<label class="field auth-field"><span>{star}{esc(label)}'
            f"{hint}</span>"
            f"{input_html}</label>"
        )
    action = "/{}/{}/edit".format(kind, quote_segment(name))
    body = (
        f"{_notice_html('', notice)}"
        f'<p class="section-note">正在编辑: <a href="{esc(back_url or action[:-5])}">'
        f"{esc(name)}</a></p>"
        f'<form class="auth-form new-form" method="post" action="{action}"'
        ' enctype="multipart/form-data">'
        + _hidden("csrf_token", ctx.csrf_token)
        + "".join(rows)
        + '<div class="field-actions"><button type="submit">保存资料</button>'
        f'<a class="btn-ghost" href="{esc(back_url or action[:-5])}">{i18n.translate("btn.cancel", ctx.lang)}</a></div></form>'
    )
    return layout(ctx, title=f"编辑 {name}", body=body, active="edit",
                  breadcrumbs=[("工作队列", "/edit"), (name, back_url or ""),
                               ("编辑资料", "")])


def new_entity_page(
    ctx: SiteContext,
    kind: str,
    *,
    values: Mapping[str, Any] | None = None,
    notice: str = "",
    bind_options: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
) -> str:
    """新建条目: 布局与交互**与编辑专辑保持一致**.

    * 上传(封面/头像)与标题一样在最顶部;
    * 不再有"编号/短名"输入(创建时数据库按序生成);
    * 不再有 cover_url(封面一律存云端, 通过上传写入);
    * 多值字段(艺人/标签/曲风)用**回车分隔**, 不依赖"、"以免名字本身含"、";
    * 候选只在有输入时出现, 且带头像; 选中后自动成一项;
    * 发售日/成立日等用 date 控件; 展会输入后自动匹配展会名与日期.
    """
    spec_title, fields = NEW_ENTITY_SPECS[kind]
    # 标题按当前语言渲染(词条: new.album / new.circle / ...)
    title = i18n.translate(f"new.{kind}", ctx.lang, default=spec_title)
    values = values or {}
    bind_options = bind_options or {}
    errs: Mapping[str, Any] = {}
    key = ""

    def field(name: str) -> str:
        return str(values.get(name) or "")

    # ---- 各实体的"顶部上传区" ----
    upload_html = ""
    if kind in ("album", "artist", "circle", "event"):
        label = {"album": "封面", "artist": "头像", "circle": "LOGO",
                 "event": "会次图"}[kind]
        field_name = "logo" if kind == "event" else "cover"
        upload_html = (
            '<section class="panel"><h2 class="section-title">' + label + "</h2>"
            '<p class="section-note">图片保存在本站云端。</p>'
            '<label class="field"><span>上传' + label + '(PNG/JPEG/WebP, ≤2MB)</span>'
            '<input type="file" name="' + field_name + '"'
            ' accept="image/png,image/jpeg,image/webp">'
            "</label></section>"
        )

    def bind(name: str, label: str, option_kind: str, *,
             placeholder: str = "", suggest_all: bool = False) -> str:
        """多值/单选绑定输入: 回车分隔, 有输入才出候选, 候选带首字母头像.

        ``suggest_all=True``: **不输入也显示全部候选**(展会用它)。
        """
        current = field(name)
        options = "".join(
            '<button type="button" class="suggest-item" data-value="{v}"'
            ' data-kind="{k}" hidden>'
            '<span class="suggest-avatar">{letter}</span>{v}'
            "<em>#{i}</em></button>".format(
                v=esc(item.get("name") or ""), k=esc(option_kind),
                letter=esc((str(item.get("name") or "")[:1]) or "?"),
                i=esc(item.get("id") or ""))
            for item in bind_options.get(option_kind, ())
        )
        return (
            f'<label class="field auth-field bind-field" data-bind="{esc(option_kind)}"'
            f'{"" if not suggest_all else " data-suggest-all=\"1\""}'
            ">"
            f'<span>{esc(label)}<span class="field-hint">回车分隔多个</span></span>'
            '<div class="bind-wrap">'
            '<div class="bind-chips" data-name="' + esc(name) + '"></div>'
            f'<input type="text" class="bind-input" data-name="{esc(name)}"'
            f' value="{esc(current)}" placeholder="{esc(placeholder)}" autocomplete="off">'
            f'<input type="hidden" name="{esc(name)}" value="{esc(current)}">'
            f'<div class="suggest-list">{options}</div>'
            "</div></label>"
        )

    def text(name: str, label: str, *, input_type: str = "text",
             hint: str = "", required: bool = False) -> str:
        hint_html = f'<span class="field-hint">{esc(hint)}</span>' if hint else ""
        star = ('<span class="req" aria-hidden="true">*</span>'
                if required else "")
        return (
            f'<label class="field auth-field"><span>{star}{esc(label)}'
            f'{hint_html}</span>'
            f'<input type="{esc(input_type)}" name="{esc(name)}"'
            f' value="{esc(field(name))}"'
            f'{"" if not required else " required"}></label>'
        )

    def area(name: str, label: str) -> str:
        return (f'<label class="field auth-field"><span>{esc(label)}</span>'
                f'<textarea name="{esc(name)}" rows="5">{esc(field(name))}</textarea></label>')

    if kind == "album":
        body_main = (
            '<section class="panel"><h2 class="section-title">标题</h2><div class="form-grid">'
            + text("title", "标题", required=True)
            + text("title_original", "原标题")
            + "</div></section>"
            '<section class="panel"><h2 class="section-title">出品信息</h2>'
            '<div class="form-grid">'
            + bind("circle", "社团", "circle")
            + bind("artists", "艺人", "artist")
            + text("release_date", "发售日", input_type="date")
            + bind("event", "展会", "event")
            + text("hall", "展馆")
            + text("booth", "展位")
            + text("catalog_no", "编号")
            + text("price", "价格")
            + "</div></section>"
            '<section class="panel"><h2 class="section-title">分类</h2><div class="form-grid">'
            + bind("tags", "标签", "tag")
            + bind("genres", "曲风", "tag")
            + "</div></section>"
            f'<section class="panel"><h2 class="section-title">{i18n.translate("section.description", ctx.lang)}</h2>'
            + area("description", "简介") + "</section>"
            '<section class="panel"><h2 class="section-title">词条<span class="section-sub">'
            "角色: 名字(可多人, 回车分隔)</span></h2>"
            + area("credits", "制作人员") + "</section>"
            f'<section class="panel"><h2 class="section-title">{i18n.translate("section.links", ctx.lang)}</h2>'
            + area("urls", "外链(每行: 标签: URL)") + "</section>"
            '<section class="panel"><h2 class="section-title">曲目表'
            '<span class="section-sub">每行: 曲名 - 艺人 3:42</span></h2>'
            + area("tracks", "曲目") + "</section>"
        )
    elif kind == "artist":
        body_main = (
            '<section class="panel"><h2 class="section-title">艺名</h2><div class="form-grid">'
            + text("name", "艺名", required=True)
            + text("name_en", "艺名(英)")
            + text("name_ja", "艺名(日)")
            + "</div></section>"
            '<section class="panel"><h2 class="section-title">本名与别名</h2>'
            '<div class="form-grid">'
            + text("real_name", "本名")
            + text("real_name_en", "本名(译)")
            + bind("aliases", "别名", "artist")
            + "</div></section>"
            '<section class="panel"><h2 class="section-title">资料</h2><div class="form-grid">'
            + text("gender", "性别")
            + text("birthday", "生日", input_type="date")
            + text("nationality", "国籍", hint="两位代码, 如 JP/CN")
            + text("occupations_text", "职业", hint="回车分隔")
            + "</div></section>"
            f'<section class="panel"><h2 class="section-title">{i18n.translate("section.description", ctx.lang)}</h2>'
            + area("bio", "简介") + "</section>"
            '<section class="panel"><h2 class="section-title">相关链接</h2>'
            + area("links", "每行: 标签: URL") + "</section>"
            '<section class="panel"><h2 class="section-title">头像地址</h2>'
            + text("avatar", "头像 URL", hint="留空则用首字母") + "</section>"
        )
    elif kind == "circle":
        body_main = (
            '<section class="panel"><h2 class="section-title">社团名</h2><div class="form-grid">'
            + text("name", "社团名", required=True)
            + text("activity_type", "活动类型", hint="同人社团 / 企业")
            + "</div></section>"
            '<section class="panel"><h2 class="section-title">活动</h2><div class="form-grid">'
            + text("founded", "成立时间", input_type="date")
            + text("last_active", "最后活动", input_type="date")
            + "</div></section>"
            f'<section class="panel"><h2 class="section-title">{i18n.translate("section.description", ctx.lang)}</h2>'
            + area("description", "简介") + "</section>"
            '<section class="panel"><h2 class="section-title">相关链接</h2>'
            + area("urls", "每行: 标签: URL") + "</section>"
            '<section class="panel"><h2 class="section-title">成员</h2>'
            + area("staff", "每行: 角色: 名字") + "</section>"
        )
    elif kind == "event":
        # 所属系列的候选(下拉), 避免用户手打系列名对不上
        _series_opts = "".join(
            '<option value="{v}"{sel}>{v}{a}</option>'.format(
                v=esc(item.get("name") or ""),
                sel=" selected" if field("series") == str(item.get("name") or "")
                else "",
                a=(" · " + str(item.get("abbr") or ""))
                if item.get("abbr") else "")
            for item in bind_options.get("event_series", ())
        )
        # 提示文字全部去掉(用户要求「如 ...」一律删除);
        # tags 与会次标签概念重叠 -> 已合并, 不再单独出「分类」区
        _series_select = (
            '<label class="field auth-field"><span>所属系列</span>'
            '<select name="series">'
            '<option value="">(按会次名自动识别)</option>'
            '{}</select></label>'.format(_series_opts)
        )
        body_main = (
            '<section class="panel"><h2 class="section-title">会次</h2>'
            '<div class="form-grid">'
            + text("name", "会次名", required=True)
            + _series_select
            + text("vol_label", "会次标签")
            + text("year", "年份")
            + text("season", "季节")
            + text("start_date", "开始日期", input_type="date")
            + text("end_date", "结束日期", input_type="date")
            + "</div></section>"
            f'<section class="panel"><h2 class="section-title">相关链接</h2>'
            + area("links", "相关链接") + "</section>"
        )
    else:  # tag
        body_main = (
            '<section class="panel"><h2 class="section-title">标签</h2><div class="form-grid">'
            + text("name", "标签名", required=True)
            + text("kind", "类型")
            + "</div></section>"
        )

    body = (
        f"{_notice_html('', notice)}"
        f'<form class="auth-form new-form" method="post" action="/new/{esc(kind)}"'
        ' enctype="multipart/form-data" data-entity-form="' + esc(kind) + '">'
        + _hidden("csrf_token", ctx.csrf_token)
        + upload_html
        + body_main
        + f'<div class="field-actions"><button type="submit">{i18n.translate("btn.create", ctx.lang)}</button>'
        f'<a class="btn-ghost" href="/edit">{i18n.translate("btn.cancel", ctx.lang)}</a></div></form>'
    )
    return layout(ctx, title=title, body=body, active="edit", wide=True,
                  breadcrumbs=[("工作队列", "/edit"), (title, "")])


def _notice_html(error: str = "", notice: str = "") -> str:
    """错误(红) / 提示(绿) 两块, 都只在非空时渲染."""
    parts = []
    if error:
        parts.append(f'<div class="form-error" role="alert">{esc(error)}</div>')
    if notice:
        parts.append(f'<div class="flash" role="status">{esc(notice)}</div>')
    return "".join(parts)


def _hidden(name: str, value: Any) -> str:
    return f'<input type="hidden" name="{esc(name)}" value="{esc(value)}">'


def captcha_block(ctx: SiteContext, captcha: Mapping[str, Any] | None, *,
                  refresh_href: str = "/register?new=1") -> str:
    """验证码: 题面 + 答案输入框 + 「换一道」链接(纯表单, 零 JS).

    ``captcha`` 是 :func:`dddb.public.verify.new_captcha` 的返回值; 传 ``None``
    表示不启用(测试/关闭场景)。
    """
    if not captcha:
        return ""
    # **图形验证码**: 只给 <img>, 答案不进 HTML(服务端渲染 SVG)。
    captcha_id = esc(captcha.get("id") or "")
    return (
        '<div class="captcha-box">'
        + _hidden("captcha_id", captcha.get("id") or "")
        + '<div class="captcha-graphic">'
        + f'<img src="/captcha.svg?id={captcha_id}" alt="图形验证码" '
        + 'width="160" height="56" loading="eager">'
        + auth_field("captcha_answer", "请填写图中字符", "", autocomplete="off",
                     hint=f"{esc(captcha.get('ttl') or 600)} 秒内有效")
        + f'<p class="captcha-alt"><a href="{esc(refresh_href)}">看不清？换一张</a></p>'
        + "</div></div>"
    )


def login_page(
    ctx: SiteContext,
    *,
    error: str = "",
    username: str = "",
    next_url: str = "",
    notice: str = "",
    resend_username: str = "",
) -> str:
    """登录表单(POST /login). token 藏在隐藏域里 —— 不需要一行 JS.

    ``resend_username`` 非空时(登录被 "请先验证邮箱" 挡下)额外给一个
    「重新发送验证码」按钮 + 「去填验证码」链接。
    """
    resend = ""
    if resend_username:
        resend = (
            '<div class="verify-hint">'
            f'<p class="section-note">账号 <b>{esc(resend_username)}</b> 还没有验证邮箱。</p>'
            f'<form class="inline-form" method="post" action="/verify/resend">'
            f'{_hidden(CSRF_FIELD, ctx.csrf_token)}'
            f'{_hidden("username", resend_username)}'
            '<button type="submit" class="btn-ghost btn-xs">重新发送验证码</button></form> '
            f'<a class="btn-ghost btn-xs" href="/verify?username={esc(quote_segment(resend_username))}">'
            "去填验证码</a></div>"
        )
    body = f"""{_notice_html(error, notice)}<form class="auth-form" method="post" action="/login">
<input type="hidden" name="csrf_token" value="{esc(ctx.csrf_token)}">
<input type="hidden" name="next" value="{esc(next_url)}">
{auth_field("username", "用户名", username, autocomplete="username")}
{auth_field("password", "密码", "", kind="password", autocomplete="current-password")}
<div class="field-actions"><button type="submit">登录</button></div>
</form>
{resend}
<p class="auth-alt"><a href="/forgot">忘记密码?</a></p>
<p class="auth-alt">还没有账号? <a href="/register">注册一个</a></p>"""
    return layout(ctx, title="登录", body=_auth_card(
        "登录", "", body,
    ), active="login")


def forgot_page(
    ctx: SiteContext,
    *,
    error: str = "",
    notice: str = "",
    value: str = "",
) -> str:
    """忘记密码: 填用户名或邮箱 -> 发送重置链接(响应不泄漏账号是否存在)."""
    body = f"""{_notice_html(error, notice)}
<form class="auth-form" method="post" action="/forgot">
{_hidden("csrf_token", ctx.csrf_token)}
<label class="field auth-field"><span>用户名或邮箱</span>
  <input type="text" name="identifier" value="{esc(value)}" required maxlength="120"></label>
<div class="field-actions"><button type="submit">发送重置邮件</button></div>
</form>
<p class="auth-alt">想起来了? <a href="/login">返回登录</a></p>"""
    return layout(ctx, title="忘记密码", body=_auth_card("忘记密码", "", body), active="login")


def reset_page(
    ctx: SiteContext,
    *,
    token: str = "",
    username: str = "",
    error: str = "",
    notice: str = "",
) -> str:
    """重置密码: 凭邮件里的链接令牌设置新密码."""
    body = f"""{_notice_html(error, notice)}
<form class="auth-form" method="post" action="/reset">
{_hidden("csrf_token", ctx.csrf_token)}
{_hidden("token", token)}
<label class="field auth-field"><span>新密码</span>
  <input type="password" name="new_password" required autocomplete="new-password"></label>
<label class="field auth-field"><span>确认新密码</span>
  <input type="password" name="new_password2" required autocomplete="new-password"></label>
<div class="field-actions"><button type="submit">设置新密码</button></div>
</form>
<p class="auth-alt">账号: {esc(username or "—")} · <a href="/login">返回登录</a></p>"""
    return layout(ctx, title="重置密码", body=_auth_card("重置密码", "", body), active="login")


def register_page(
    ctx: SiteContext,
    *,
    error: str = "",
    username: str = "",
    invite: str = "",
    email: str = "",
    captcha: Mapping[str, Any] | None = None,
    email_enabled: bool = False,
) -> str:
    """注册表单(POST /register).

    * **验证码必填**(``captcha`` 非空时): 服务端一次性校验, 见 :mod:`dddb.public.verify`;
    * **邮箱可选/可开关**: 只有配了 SMTP(``email_enabled``)才显示邮箱框;
      填了就要求验证, 没配就只在验证码通过后直接建号;
    * ``邀请码`` 是可选的旧机制: 填对一次性邀请码直接成为编辑者。
      角色**由服务端决定**, 表单里没有可伪造的 role 字段。
    """
    email_html = ""
    if email_enabled:
        email_html = auth_field(
            "email", "邮箱", email, kind="email", autocomplete="email",
        )
    else:
        email_html = (
            '<p class="section-note">本站未启用邮箱验证, 注册后即可直接登录。</p>'
        )
    body = f"""{_notice_html(error)}<form class="auth-form" method="post" action="/register">
<input type="hidden" name="csrf_token" value="{esc(ctx.csrf_token)}">
{auth_field("username", "用户名", username, autocomplete="username", hint="3-32 字符",
            field_id="username")}
<p class="field-hint" id="username-hint" aria-live="polite"></p>
{auth_field("password", "密码", "", kind="password", autocomplete="new-password",
            field_id="password")}
<ul class="pwd-rules" id="password-rules">
  <li data-rule="len">至少 8 位</li>
  <li data-rule="digit">包含数字</li>
  <li data-rule="letter">包含英文字母</li>
</ul>
{auth_field("password2", "确认密码", "", kind="password", autocomplete="new-password",
            field_id="password2")}
<p class="field-error" id="password2-error" role="alert" aria-live="polite" hidden
   style="display:none"></p>
{email_html}
{captcha_block(ctx, captcha)}
{auth_field("invite", "邀请码(可选)", invite, required=False,
            hint="填写邀请码无需审核即可成为编辑者")}
<div class="field-actions"><button type="submit">注册</button></div>
</form>
<p class="auth-alt">已经有账号? <a href="/login">直接登录</a></p>"""
    subtitle = ("注册后默认为普通用户，参与编辑需要申请编辑者身份或填写邀请码，"
                "密码为至少 8 位数字加英文字母的组合")
    return layout(ctx, title="注册", body=_auth_card("注册", subtitle, body), active="register")


def invites_page(
    ctx: SiteContext,
    *,
    invites: Sequence[Mapping[str, Any]] = (),
    stats: Mapping[str, int] | None = None,
    can_see_all: bool = False,
    notice: str = "",
    error: str = "",
) -> str:
    """邀请码页(编辑者/超级用户): 生成 + 列出自己(超管还能看全部)."""
    stats = stats or {}
    rows = []
    for item in invites:
        code = str(item.get("code") or "")
        used = bool(item.get("used"))
        owner = str(item.get("created_by") or "")
        rows.append(
            '<tr>'
            f'<td><code class="invite-code">{esc(code)}</code></td>'
            f'<td>{"已使用" if used else "未使用"}</td>'
            f'<td>{esc(str(item.get("used_by") or "") or "-")}</td>'
            f'<td>{esc(str(item.get("note") or "") or "-")}</td>'
            + (f'<td>{esc(owner or "-")}</td>' if can_see_all else "")
            + '</tr>'
        )
    table = (
        '<table class="invite-table"><thead><tr>'
        '<th>邀请码</th><th>状态</th><th>使用者</th><th>备注</th>'
        + ('<th>生成者</th>' if can_see_all else '')
        + '</tr></thead><tbody>' + ("".join(rows) or
                                     f'<tr><td colspan="{5 if can_see_all else 4}">'
                                     '还没有邀请码</td></tr>') + '</tbody></table>'
    )
    body = f"""{_notice_html(error, notice)}
<div class="stat-row">
  <span class="stat"><b>{esc(stats.get("total", 0))}</b> 全部</span>
  <span class="stat"><b>{esc(stats.get("unused", 0))}</b> 未使用</span>
  <span class="stat"><b>{esc(stats.get("used", 0))}</b> 已使用</span>
</div>
{table}
<form class="invite-form" method="post" action="/invites">
<input type="hidden" name="csrf_token" value="{esc(ctx.csrf_token)}">
<label class="field auth-field"><span>备注(可选)</span>
  <input type="text" name="note" maxlength="200" placeholder="例如：给某某社的编辑"></label>
<div class="field-actions"><button type="submit">生成邀请码</button></div>
</form>"""
    return layout(ctx, title="邀请码", body=body, active="invites")


def render_bio(text: str) -> str:
    """简介渲染: **纯文本 + URL 自动识别**(不渲染 HTML), 外链带 data-external.

    外链点击由 public.js 弹确认框(用户要求:"点击后应提示正在跳转站外链接")。
    """
    raw = str(text or "")
    if not raw:
        return ""
    import re as _re
    url_re = _re.compile(r"https?://[^\s<>\"']+", _re.I)
    parts: list[str] = []
    cursor = 0
    for match in url_re.finditer(raw):
        parts.append(esc(raw[cursor:match.start()]))
        url = match.group(0)
        parts.append(
            f'<a class="ext-link" data-external="1" href="{esc(url)}"'
            f' rel="nofollow noopener noreferrer" target="_blank">{esc(url)}</a>')
        cursor = match.end()
    parts.append(esc(raw[cursor:]))
    return "".join(parts).replace("\n", "<br>")


def _profile_section(ctx: SiteContext, profile: Mapping[str, Any], user: Mapping[str, Any]) -> str:
    avatar = str(profile.get("avatar") or "")
    avatar_html = (
        f'<img class="avatar-img" src="/avatar/{esc(user.get("id") or "")}"'
        f' alt="{esc(user.get("username") or "")}的头像" width="72" height="72">'
        if avatar else
        f'<span class="avatar-fallback" aria-hidden="true">'
        f'{esc((str(user.get("username") or "?"))[:1].upper())}</span>'
    )
    bio = str(profile.get("bio") or "")
    return f"""<section class="panel account-section" id="profile">
<h3 class="account-h">我的资料</h3>
<div class="profile-head">{avatar_html}
  <form class="inline-form" method="post" action="/account/avatar" enctype="multipart/form-data">
    {_hidden("csrf_token", ctx.csrf_token)}
    <label class="field"><span>头像(PNG/JPEG/WebP, ≤512KB)</span>
      <input type="file" name="avatar" accept="image/png,image/jpeg,image/webp" required></label>
    <button type="submit" class="btn-ghost btn-xs">上传头像</button>
  </form>
</div>
<form class="auth-form" method="post" action="/account/profile">
{_hidden("csrf_token", ctx.csrf_token)}
<label class="field auth-field"><span>生日<span class="field-hint">可为空</span></span>
  <input type="date" name="birthday" value="{esc(profile.get("birthday") or "")}"></label>
<label class="field auth-field"><span>简介<span class="field-hint">最多 500 字, 可含自己的外链</span></span>
  <textarea name="bio" rows="5" maxlength="500" id="bio-input">{esc(bio)}</textarea></label>
<div class="bio-preview">{render_bio(bio) if bio else ""}</div>
<div class="field-actions"><button type="submit">保存资料</button></div>
</form>
</section>"""


#: 隐私档位(单选): 0=所有人 1=站内用户 2=仅关注者 3=都不可见
VIS_LEVELS = (
    (0, "对所有人公开"),
    (1, "对站内用户公开"),
    (2, "仅对关注的用户公开"),
    (3, "所有人都不可见"),
)
VIS_ITEMS = (
    ("vis_birthday", "生日"),
    ("vis_bio", "简介"),
    ("vis_favorites", "收藏"),
    ("vis_follows", "关注"),
    ("vis_comments", "评论"),
    ("vis_contributions", "贡献"),
)


def visible_to(level: Any, *, viewer: str = "anon") -> bool:
    """档位 -> 这位访客能不能看. ``viewer``: anon / member / follower."""
    try:
        value = int(level if level is not None else 0)
    except (TypeError, ValueError):
        value = 0
    if value == 0:
        return True
    if value == 1:
        return viewer in ("member", "follower")
    if value == 2:
        return viewer == "follower"
    return False


def _security_section(ctx: SiteContext, user: Mapping[str, Any],
                      email_state: Mapping[str, Any] | None,
                      confirmed: bool = True) -> str:
    """安全设置: 两个入口按钮, 各自在弹出的对话框里完成."""
    email_state = email_state or {}
    address = str(email_state.get("email") or "")
    verified = bool(email_state.get("verified"))
    email_line = (
        f'<p class="account-line">邮箱: <b>{esc(address)}</b>'
        + ("（已验证）" if verified else "（未验证）") + "</p>"
        if address else ""
    )
    code_form = (
        '<form class="inline-form" method="post" action="/account/security-code">'
        + _hidden("csrf_token", ctx.csrf_token)
        + '<input type="hidden" name="purpose" value="{purpose}">'
        '<button class="btn-ghost btn-xs" type="submit">发送验证码</button></form>'
    )
    password_dialog = (
        '<dialog class="app-dialog" id="dlg-password" aria-labelledby="dlg-password-title">'
        '<form method="post" action="/account/password" class="auth-form">'
        + _hidden("csrf_token", ctx.csrf_token)
        + '<h2 class="dialog-title" id="dlg-password-title">修改密码</h2>'
        '<label class="field auth-field"><span>邮箱验证码</span>'
        '<input type="text" name="code" maxlength="6" required>'
        f'</label><div class="dialog-row">{code_form.format(purpose="password_change")}</div>'
        '<label class="field auth-field"><span>新密码</span>'
        '<input type="password" name="new_password" required autocomplete="new-password"></label>'
        '<label class="field auth-field"><span>确认新密码</span>'
        '<input type="password" name="new_password2" required autocomplete="new-password"></label>'
        '<div class="field-actions"><button type="submit">修改密码</button>'
        '<button type="button" class="btn-ghost" data-dialog-close>取消</button></div>'
        "</form></dialog>"
    )
    delete_dialog = (
        '<dialog class="app-dialog" id="dlg-delete" aria-labelledby="dlg-delete-title">'
        '<form method="post" action="/account/delete" class="auth-form">'
        + _hidden("csrf_token", ctx.csrf_token)
        + '<h2 class="dialog-title" id="dlg-delete-title">注销账号</h2>'
        '<label class="field auth-field"><span>邮箱验证码</span>'
        '<input type="text" name="code" maxlength="6" required>'
        f'</label><div class="dialog-row">{code_form.format(purpose="account_delete")}</div>'
        '<label class="field auth-field"><span>输入「注销账号」确认</span>'
        '<input type="text" name="confirm" required placeholder="注销账号"></label>'
        '<div class="field-actions"><button type="submit">注销账号</button>'
        '<button type="button" class="btn-ghost" data-dialog-close>取消</button></div>'
        "</form></dialog>"
    )
    return (
        '<section class="panel account-section" id="security">'
        f'<h2 class="section-title">{i18n.translate("section.security", ctx.lang)}</h2>'
        + email_line
        + '<div class="security-actions">'
        '<button type="button" class="btn" data-dialog-open="dlg-password">修改密码</button>'
        '<button type="button" class="btn-ghost" data-dialog-open="dlg-delete">注销账号</button>'
        '</div>'
        + password_dialog + delete_dialog
        + "</section>"
    )

#: 可选主题(名称, 主色, 次色, 背景色)
THEMES: tuple[tuple[str, str, str, str], ...] = (
    ("dark", "暗色", "#4f8cff", "#2ec4b6", "#0f1319"),
    ("light", "明亮", "#2f6fdf", "#1f5fce", "#f6f7f9"),
    ("warm", "暖色", "#e08a3c", "#d0761f", "#1b1512"),
    ("contrast", "高对比", "#ffe100", "#ffd000", "#000000"),
    ("forest", "森林", "#4fbf7f", "#35a866", "#0e1712"),
    ("sakura", "樱花", "#ff8ab0", "#f56f9c", "#1d1520"),
)


def _bg_attr(ctx: SiteContext) -> str:
    """给 <body> 加上用户背景图地址(来自本站上传, 不是外链)."""
    uid = 0
    try:
        user = getattr(ctx, "user", None) or {}
        uid = int((user or {}).get("id") or 0)
    except (TypeError, ValueError, AttributeError):
        uid = 0
    if not uid:
        uid = int(getattr(ctx, "user_id", 0) or 0)
    if not uid:
        return ""
    return f' data-bg-src="/background/{int(uid)}"'


def _appearance_fab(ctx: SiteContext) -> str:
    """常驻右下角的外观按钮 + 浮层面板(任何页面可用, 不必进我的账号)."""
    swatches = "".join(
        '<button type="button" class="theme-swatch" data-theme-value="{value}"'
        ' aria-pressed="false"><span class="swatches">'
        '<i style="background:{main}"></i><i style="background:{second}"></i>'
        '<i style="background:{bgc}"></i></span>{label}</button>'.format(
            value=esc(value), label=esc(label), main=esc(main),
            second=esc(second), bgc=esc(bgc))
        for value, label, main, second, bgc in THEMES
    )
    return (
        '<button type="button" class="appearance-fab" id="appearance-fab"'
        ' aria-label="外观设置" title="外观设置">🎨</button>'
        '<div class="appearance-pop" id="appearance-pop" hidden>'
        '<h3>外观</h3>'
        f'<div class="theme-swatches">{swatches}</div>'
        '<form class="appearance-row" method="post" action="/account/background"'
        ' enctype="multipart/form-data">'
        f'<input type="hidden" name="csrf_token" value="{esc(ctx.csrf_token)}">'
        '<input type="file" name="background" accept="image/*">'
        '<button type="submit" class="btn-ghost btn-xs">上传背景</button></form>'
        '<form class="appearance-row" method="post" action="/account/background">'
        f'<input type="hidden" name="csrf_token" value="{esc(ctx.csrf_token)}">'
        '<input type="hidden" name="action" value="delete">'
        '<button type="submit" class="btn-ghost btn-xs">清除</button></form>'
        '<div class="appearance-row"><label>遮罩</label>'
        '<input type="range" id="fab-bg-dim" min="0" max="95" step="5" value="60"></div>'
        '<div class="appearance-row"><label>模糊</label>'
        '<input type="range" id="fab-bg-blur" min="0" max="16" step="1" value="0"></div>'
        '<div class="appearance-row"><label>密度</label>'
        '<select id="fab-density"><option value="compact">紧凑</option>'
        '<option value="normal" selected>标准</option>'
        '<option value="relaxed">宽松</option></select></div>'
        '<div class="appearance-row"><label>圆角</label>'
        '<select id="fab-radius"><option value="sharp">直角</option>'
        '<option value="default" selected>默认</option>'
        '<option value="round">圆润</option></select></div>'
        '<div class="appearance-row"><label>字号</label>'
        '<select id="fab-font"><option value="small">小</option>'
        '<option value="normal" selected>标准</option>'
        '<option value="large">大</option></select></div>'
        '<div class="pop-actions"><button type="button" class="btn-ghost btn-xs"'
        ' id="fab-reset">恢复默认</button></div>'
        "</div>"
    )


def _appearance_section(ctx: SiteContext, prefs: Mapping[str, Any] | None = None) -> str:
    """外观设置: 配色 / 背景图 / 背景模糊与遮罩 / 密度 / 圆角 / 字号."""
    prefs = prefs or {}
    current = str(prefs.get("theme") or "dark")
    swatches = "".join(
        '<button type="button" class="theme-swatch" data-theme-value="{value}"'
        ' aria-pressed="{pressed}">'
        '<span class="swatches">'
        '<i style="background:{main}"></i><i style="background:{second}"></i>'
        '<i style="background:{bgc}"></i></span>'
        "{label}</button>".format(
            value=esc(value), label=esc(label),
            pressed="true" if value == current else "false",
            main=esc(main), second=esc(second), bgc=esc(bgc),
        )
        for value, label, main, second, bgc in THEMES
    )
    return (
        '<section class="panel account-section" id="appearance">'
        f'<h2 class="section-title">{i18n.translate("section.appearance", ctx.lang)}</h2>'
        '<p class="section-note">选择配色，也可以用自己的图片当背景；设置保存在本机浏览器。</p>'
        '<div class="appearance-grid">'
        '<div><h3 class="account-h">配色</h3>'
        f'<div class="theme-swatches">{swatches}</div></div>'
        '<div><h3 class="account-h">背景图片</h3>'
        '<p class="section-note">上传后保存在本站(不使用外部链接)。支持 JPG/PNG/WEBP/GIF，'
        "上限 8MB。</p>"
        '<form class="appearance-row" method="post" action="/account/background" '
        'enctype="multipart/form-data">'
        f'<input type="hidden" name="csrf_token" value="{esc(ctx.csrf_token)}">'
        '<input type="file" name="background" accept="image/*" required>'
        '<button type="submit" class="btn-ghost btn-xs">上传</button></form>'
        '<form class="appearance-row" method="post" action="/account/background">'
        f'<input type="hidden" name="csrf_token" value="{esc(ctx.csrf_token)}">'
        '<input type="hidden" name="action" value="delete">'
        '<button type="submit" class="btn-ghost btn-xs">清除背景</button></form>'
        '<div class="appearance-preview" id="pref-bg-preview"></div></div>'
        '<div><h3 class="account-h">参数</h3>'
        f'<div class="appearance-row"><label>背景遮罩 '
        f'<input type="range" id="pref-bg-dim" min="0" max="95" step="5" '
        f'value="{esc(str(prefs.get("bg_dim") or 60))}"></label>'
        f'<label>背景模糊 <input type="range" id="pref-bg-blur" min="0" max="16" step="1" '
        f'value="{esc(str(prefs.get("bg_blur") or 0))}"></label></div>'
        '<div class="appearance-row">'
        f'<label>密度 <select id="pref-density">'
        + "".join(
            f'<option value="{v}"{" selected" if str(prefs.get("density") or "normal") == v else ""}>'
            f"{label_}</option>"
            for v, label_ in (("compact", "紧凑"), ("normal", "标准"), ("relaxed", "宽松"))
        )
        + "</select></label>"
        f'<label>圆角 <select id="pref-radius">'
        + "".join(
            f'<option value="{v}"{" selected" if str(prefs.get("radius") or "default") == v else ""}>'
            f"{label_}</option>"
            for v, label_ in (("sharp", "直角"), ("default", "默认"), ("round", "圆润"))
        )
        + "</select></label>"
        f'<label>字号 <select id="pref-font">'
        + "".join(
            f'<option value="{v}"{" selected" if str(prefs.get("font") or "normal") == v else ""}>'
            f"{label_}</option>"
            for v, label_ in (("small", "小"), ("normal", "标准"), ("large", "大"))
        )
        + "</select></label>"
        "</div></div>"
        '<div class="appearance-row">'
        '<button type="button" class="btn-ghost btn-xs" id="pref-reset">恢复默认</button></div>'
        "</div></section>"
    )


def _privacy_section(ctx: SiteContext, visibility: Mapping[str, Any],
                     accepts_messages: bool = True) -> str:
    """隐私设置: 每一项用**单选**选公开范围(所有人/站内/关注者/不可见)."""
    accept_html = (
        '<div class="vis-row"><span class="vis-label">接受他人私信</span>'
        '<div class="vis-options">'
        '<label class="radio"><input type="radio" name="accept_messages" value="1"'
        + (" checked" if accepts_messages else "") + '> 接受</label>'
        '<label class="radio"><input type="radio" name="accept_messages" value="0"'
        + ("" if accepts_messages else " checked") + '> 不接受</label>'
        '</div></div>'
    )
    blocks = []
    for key, label in VIS_ITEMS:
        try:
            current = int(visibility.get(key) if visibility.get(key) is not None else 0)
        except (TypeError, ValueError):
            current = 0
        options = "".join(
            '<label class="radio"><input type="radio" name="{}" value="{}"{}> {}</label>'.format(
                key, level, " checked" if current == level else "", esc(text_))
            for level, text_ in VIS_LEVELS
        )
        blocks.append(
            f'<div class="vis-row"><span class="vis-label">{esc(label)}</span>'
            f'<div class="vis-options">{options}</div></div>'
        )
    return (
        '<section class="panel account-section" id="privacy">'
        f'<h2 class="section-title">{i18n.translate("section.privacy", ctx.lang)}</h2>'
        '<p class="section-note">每一项可选公开范围;'
        '「仅对关注的用户公开」需要对方关注你(否则按站内用户处理)。</p>'
        '<form class="vis-form" method="post" action="/account/privacy">'
        + _hidden("csrf_token", ctx.csrf_token)
        + "".join(blocks) + accept_html
        + '<div class="field-actions"><button type="submit" class="btn-ghost">保存隐私设置</button></div>'
        '</form></section>'
    )


def _list_section(title: str, anchor: str, rows_html: str, empty: str) -> str:
    return f"""<section class="panel account-section" id="{esc(anchor)}">
<h3 class="account-h">{esc(title)}</h3>
{rows_html or f'<p class="section-note">{esc(empty)}</p>'}</section>"""


def _apply_dialog(ctx: SiteContext, can_request: bool) -> str:
    """编辑者申请弹窗: 只有弹窗里的「关闭」按钮能关(禁 Esc / 禁点遮罩), 自动存草稿."""
    if not can_request:
        return ""
    return f"""<dialog class="apply-dialog" id="apply-dialog" aria-labelledby="apply-title"
        data-draft-key="dddb:apply-draft">
  <form method="post" action="/account" class="apply-form">
    {_hidden("csrf_token", ctx.csrf_token)}
    <input type="hidden" name="action" value="request_editor">
    <h3 class="account-h" id="apply-title">申请成为编辑者</h3>
    <p class="section-note">提交后由超级用户审核; 草稿会自动保存在本机浏览器。</p>
    <label class="field auth-field"><span>申请理由<span class="field-hint">选填</span></span>
      <textarea name="reason" id="apply-reason" rows="5" maxlength="500"></textarea></label>
    <p class="draft-hint" id="apply-draft-hint" aria-live="polite"></p>
    <div class="field-actions">
      <button type="submit" name="submit" value="1">提交申请</button>
      <button type="button" class="btn-ghost" id="apply-close">关闭</button>
    </div>
  </form>
</dialog>"""


def _pc_tab(key: str, label: str, active: str, href: str = "") -> str:
    cls = "pc-tab is-active" if active == key else "pc-tab"
    target = href or f"/account?tab={key}"
    return f'<a class="{cls}" href="{esc(target)}">{esc(label)}</a>'


def fav_grid(items: Sequence[Mapping[str, Any]]) -> str:
    """收藏专辑的**大封面**卡片网格(总览与公开资料页共用)."""
    cards = []
    for item in items or []:
        key = str(item.get("album_key") or "")
        cover = str(item.get("cover_src") or "")
        title = str(item.get("title") or key)
        img = (f'<img class="fav-card-cover" src="{esc(cover)}" alt="{esc(title)}" loading="lazy">'
               if cover else
               f'<span class="fav-card-cover cover-fallback">{esc(title[:1] or "?")}</span>')
        cards.append(
            f'<a class="fav-card" href="/album/{esc(quote_segment(key))}">{img}'
            f'<span class="fav-card-title">{esc(title)}</span></a>')
    return ('<div class="fav-grid">' + "".join(cards) + '</div>') if cards else ""


def account_page(
    ctx: SiteContext,
    *,
    user: Mapping[str, Any],
    created_text: str = "",
    last_login_text: str = "",
    editor_request: Mapping[str, Any] | None = None,
    email_state: Mapping[str, Any] | None = None,
    can_request: bool = False,
    error: str = "",
    notice: str = "",
    profile: Mapping[str, Any] | None = None,
    favorites: Sequence[Mapping[str, Any]] = (),
    follows: Sequence[Mapping[str, Any]] = (),
    comments: Sequence[Mapping[str, Any]] = (),
    contributions: Sequence[Mapping[str, Any]] = (),
    feedbacks: Sequence[Mapping[str, Any]] = (),
    messages_meta: Mapping[str, Any] | None = None,
    notifications_meta: Mapping[str, Any] | None = None,
    inbox: Sequence[Mapping[str, Any]] = (),
    notifications: Sequence[Mapping[str, Any]] = (),
    tab: str = "overview",
    visibility: Mapping[str, Any] | None = None,
    session_confirmed: bool = True,
) -> str:
    """个人中心: **左侧选项卡 + 右侧内容**(不再挤在一个卡片里)."""
    role = str(user.get("role") or "viewer")
    profile = profile or {}
    messages_meta = messages_meta or {}
    notifications_meta = notifications_meta or {}
    def _tab(key: str, label: str) -> tuple[str, str]:
        return key, i18n.translate(label, ctx.lang)

    tabs = [
        _tab("overview", "account.overview"),
        _tab("profile", "account.profile"),
        _tab("favorites", "account.favorites"),
        _tab("follows", "account.follows"),
        _tab("comments", "account.comments"),
    ]
    if role in ("editor", "superuser"):
        tabs.append(_tab("contributions", "account.contributions"))
    tabs += [
        _tab("security", "account.security"),
        _tab("messages", "account.messages"),
        _tab("notifications", "account.notifications"),
        _tab("feedback", "account.feedback"),
        _tab("privacy", "account.privacy"),
        _tab("appearance", "account.appearance"),
    ]

    # -- 侧栏 ---------------------------------------------------------------
    avatar_html = (
        f'<img class="avatar-img" src="/avatar/{esc(user.get("id") or "")}"'
        f' alt="{esc(user.get("username") or "")}的头像" width="56" height="56">'
        if profile.get("avatar") else
        f'<span class="avatar-fallback avatar-sm">'
        f'{esc((str(user.get("username") or "?"))[:1].upper())}</span>'
    )
    unread_msg = int(messages_meta.get("unread") or 0)
    unread_note = int(notifications_meta.get("unread") or 0)
    side_tabs = "".join(
        _pc_tab(key, label, tab) for key, label in tabs
    )
    sidebar = (
        '<aside class="pc-side">'
        f'<div class="pc-user">{avatar_html}'
        f'<div><b>{esc(user.get("username") or "")}</b>{identity_badge(role)}'
        f'<div class="muted">注册于 {esc(created_text or "—")}</div></div></div>'
        f'<nav class="pc-tabs" aria-label="个人中心导航">{side_tabs}</nav>'
        + ('<a class="pc-side-link" href="/invites">邀请码</a>'
           if role in ("editor", "superuser") else "")
        + ('<a class="pc-side-link" href="/review">审核队列</a>'
           '<a class="pc-side-link" href="/users">用户管理</a>' if ctx.is_superuser else "")
        + '</aside>'
    )

    # -- 各分区 -------------------------------------------------------------
    def rows_html(pairs: Sequence[tuple[str, str]]) -> str:
        return "".join(
            f'<div class="info-row"><dt>{esc(k)}</dt><dd>{v}</dd></div>' for k, v in pairs)

    overview = (
        '<section class="panel account-section"><h2 class="section-title">总览</h2>'
        + '<div class="stat-row">'
        + f'<span class="stat"><b>{esc(len(favorites))}</b>收藏</span>'
        + f'<span class="stat"><b>{esc(len(follows))}</b>关注</span>'
        + f'<span class="stat"><b>{esc(len(comments))}</b>评论</span>'
        + (f'<span class="stat"><b>{esc(len(contributions))}</b>贡献</span>'
           if role in ("editor", "superuser") else "")
        + f'<span class="stat"><b>{esc(unread_msg)}</b>未读私信</span>'
        + f'<span class="stat"><b>{esc(unread_note)}</b>未读通知</span>'
        + '</div>'
        + f'<dl class="info account-info">{rows_html([
            ("用户名", esc(user.get("username") or "")),
            ("角色", identity_badge(role)),
            ("上次登录", esc(last_login_text or "—")),
        ])}</dl></section>'
        + (('<section class="panel account-section"><h2 class="section-title">我的收藏</h2>'
            + fav_grid(favorites) + '</section>') if favorites else "")
        + (('<section class="panel account-section"><h2 class="section-title">我的关注</h2><ul class="plain-list">'
            + "".join(f'<li>{esc(item.get("name") or item.get("key") or "")}</li>' for item in follows)
            + '</ul></section>') if follows else "")
    )

    apply_btn = ""
    if role == "viewer" and can_request:
        apply_btn = (' <button type="button" class="btn-ghost btn-xs" id="apply-open">'
                     '申请成为编辑者</button>')
    request_html = ""
    if editor_request:
        request_html = (
            '<p class="account-line">最近一次申请: <b>'
            + esc(editor_request.get("status_label") or "") + '</b>'
            + f' <span class="muted">#{esc(editor_request.get("id") or "")} · '
            + esc(editor_request.get("created_at_text") or "") + '</span></p>'
            + '<p class="section-note">理由: '
            + esc(editor_request.get("reason") or "(未填写)")
            + ((' · 备注: ' + esc(editor_request.get("note"))) if editor_request.get("note") else "")
            + '</p>')

    email_html = ""
    if email_state:
        address = str(email_state.get("email") or "")
        if not email_state.get("enabled"):
            email_html = '<p class="section-note">本站未启用邮箱验证。</p>'
        elif email_state.get("verified"):
            email_html = f'<p class="account-line">✅ 已验证: <b>{esc(address)}</b></p>'
        elif address:
            email_html = (f'<p class="account-line">⏳ 待验证: <b>{esc(address)}</b></p>'
                          '<form class="inline-form" method="post" action="/verify/resend">'
                          + _hidden("csrf_token", ctx.csrf_token)
                          + '<button type="submit" class="btn-ghost btn-xs">重新发送验证码</button></form>')
        else:
            email_html = '<p class="section-note">这个账号没有填写邮箱。</p>'

    profile_section = _profile_section(ctx, profile, user)
    visibility = visibility or {}
    accepts_messages = bool(int(profile.get("accept_messages", 1) or 1)) if isinstance(profile, Mapping) else True
    privacy_section = _privacy_section(ctx, visibility, accepts_messages=accepts_messages)
    # 外观偏好保存在浏览器本地; 服务端只渲染默认值, 前端用自己的设置覆盖
    appearance_prefs: Mapping[str, Any] = {}

    def list_block(title: str, anchor: str, rows: str, empty: str) -> str:
        return _list_section(title, anchor, rows, empty)

    fav_rows = "".join(
        f'<li><a href="/album/{esc(item.get("album_key") or "")}">'
        f'{esc(item.get("title") or item.get("album_key") or "")}</a>'
        '<form class="inline-form" method="post" action="/favorites/toggle">'
        + _hidden("csrf_token", ctx.csrf_token)
        + _hidden("album_key", item.get("album_key") or "")
        + _hidden("next", "/account?tab=favorites")
        + '<button class="btn-ghost btn-xs" type="submit">取消收藏</button></form></li>'
        for item in favorites)
    fol_rows = "".join(
        f'<li>{esc({"circle": "社团", "artist": "艺人", "event": "展会"}.get(str(item.get("kind")), str(item.get("kind"))))}: '
        f'{esc(item.get("name") or item.get("key") or "")}'
        '<form class="inline-form" method="post" action="/follows/toggle">'
        + _hidden("csrf_token", ctx.csrf_token)
        + _hidden("kind", item.get("kind") or "") + _hidden("key", item.get("key") or "")
        + _hidden("next", "/account?tab=follows")
        + '<button class="btn-ghost btn-xs" type="submit">取消关注</button></form></li>'
        for item in follows)
    com_rows = "".join(
        f'<li><a href="/album/{esc(item.get("album_key") or "")}">'
        f'{esc(item.get("album_title") or item.get("album_key") or "")}</a>: '
        f'{esc((item.get("body") or "")[:80])}'
        f'<span class="muted"> · {esc(item.get("created_at_text") or "")}</span></li>'
        for item in comments)

    def _contrib_target(item: Mapping[str, Any]) -> str:
        target = str(item.get("target") or "")
        summary = esc(item.get("summary") or "")
        if target and str(item.get("kind") or "") in ("edit", "cover", "new_album"):
            return f'<a href="/album/{esc(quote_segment(target))}">{summary or esc(target)}</a>'
        return summary or esc(target)

    con_rows = "".join(
        f'<li><span class="muted">{esc(item.get("created_at_text") or "")}</span> '
        f'[{esc(item.get("kind") or "")}] {_contrib_target(item)}</li>'
        for item in contributions)
    fb_rows = "".join(
        f'<li><span class="muted">{esc(item.get("created_at_text") or "")}</span> '
        f'{esc((item.get("body") or "")[:120])}</li>' for item in feedbacks)

    inbox_rows = "".join(
        f'<li class="{"is-unread" if not item.get("read") else ""}">'
        f'<b>{esc(item.get("peer_name") or "?")}</b> · '
        f'{esc((item.get("body") or "")[:60])}</li>' for item in inbox)
    note_rows = "".join(
        f'<li class="{"is-unread" if not item.get("read") else ""}">'
        f'{esc(item.get("text") or "")}</li>' for item in notifications)

    sections: dict[str, str] = {
        "overview": overview,
        "profile": profile_section + (
            '<section class="panel account-section"><h2 class="section-title">账号</h2>'
            f'<dl class="info">{rows_html([("邮箱", email_html or "—")])}</dl>'
            + (('<h3 class="account-h">编辑者申请</h3>' + request_html + apply_btn)
               if (request_html or apply_btn) else "")
            + '</section>'),
        "favorites": list_block("我的收藏", "favorites",
                                f'<ul class="plain-list">{fav_rows}</ul>' if fav_rows else "",
                                "还没有收藏任何专辑。"),
        "follows": list_block("我关注的社团 / 艺人", "follows",
                              f'<ul class="plain-list">{fol_rows}</ul>' if fol_rows else "",
                              "还没有关注任何社团或艺人。"),
        "comments": list_block("我的评论", "comments",
                               f'<ul class="plain-list">{com_rows}</ul>' if com_rows else "",
                               "还没有发表过评论。"),
        "contributions": list_block("我的贡献", "contributions",
                                    f'<ul class="plain-list">{con_rows}</ul>' if con_rows else "",
                                    "还没有记录: 编辑/上传专辑后会出现在这里。"),
        "messages": (
            '<section class="panel account-section"><h2 class="section-title">私信</h2>'
            + (f'<ul class="plain-list">{inbox_rows}</ul>' if inbox_rows
               else '<p class="section-note">没有未读私信。</p>')
            + '<p class="account-line"><a class="btn-ghost" href="/messages">打开完整私信</a></p></section>'),
        "notifications": (
            '<section class="panel account-section"><h2 class="section-title">站内通知</h2>'
            + (f'<ul class="plain-list">{note_rows}</ul>' if note_rows
               else '<p class="section-note">暂时没有通知。</p>')
            + '<p class="account-line"><a class="btn-ghost" href="/notifications">查看全部</a></p></section>'),
        "feedback": (
            '<section class="panel account-section"><h2 class="section-title">向管理员反馈</h2>'
            + (f'<ul class="plain-list">{fb_rows}</ul>' if fb_rows else "")
            + '<form class="auth-form" method="post" action="/feedback">'
            + _hidden("csrf_token", ctx.csrf_token)
            + '<label class="field auth-field"><span>内容</span>'
            '<textarea name="body" rows="4" maxlength="2000" required></textarea></label>'
            '<label class="field auth-field"><span>联系方式<span class="field-hint">选填</span></span>'
            '<input type="text" name="contact" maxlength="120"></label>'
            '<div class="field-actions"><button type="submit">提交反馈</button></div></form></section>'),
        "security": _security_section(ctx, user, email_state, confirmed=session_confirmed),
        "privacy": privacy_section,
        "appearance": _appearance_section(ctx, appearance_prefs),
    }
    if tab not in sections:
        tab = "overview"
    content = _notice_html(error, notice) + sections[tab] + (
        _apply_dialog(ctx, can_request) if tab == "profile" else "")

    body = (f'<div class="pc-layout">{sidebar}'
            f'<section class="pc-main">{content}</section></div>')
    return layout(ctx, title="我的账号", body=body, active="account")


def public_profile_page(
    ctx: SiteContext,
    *,
    user: Mapping[str, Any],
    profile: Mapping[str, Any],
    visibility: Mapping[str, Any] | None = None,
    viewer: str = "anon",
    favorites: Sequence[Mapping[str, Any]] = (),
    follows: Sequence[Mapping[str, Any]] = (),
    contributions: Sequence[Mapping[str, Any]] = (),
    counts: Mapping[str, Any] | None = None,
    joined_text: str = "",
    active_text: str = "",
) -> str:
    """公开资料页: 逐项按**隐私档位**决定是否展示.

    布局: 头像 + 名字 + 身份 + 加入时间/统计 -> 简介 -> 收藏 -> 关注 -> 贡献。
    """
    visibility = visibility or {}
    counts = counts or {}
    username = str(user.get("username") or "")
    uid = int(user.get("id") or 0)

    def allowed(key: str) -> bool:
        return visible_to(visibility.get(key), viewer=viewer)

    # ---- 头部: 头像 + 名字 + 元信息 ----
    avatar_html = (
        f'<img class="pub-profile-avatar-img" src="/avatar/{uid}"'
        f' alt="{esc(username)}" loading="lazy">'
        if uid else ""
    )
    meta_bits: list[str] = []
    if joined_text:
        meta_bits.append(f'加入于 {esc(joined_text)}')
    if active_text:
        meta_bits.append(f'最后活跃 {esc(active_text)}')
    # 只列**大于 0** 的项, 避免一排 "收藏 0 · 关注 0 · 贡献 0"
    stat_bits = []
    for label, value in (("收藏", counts.get("favorites")),
                         ("关注", counts.get("follows")),
                         ("贡献", counts.get("contributions"))):
        try:
            number = int(value or 0)
        except (TypeError, ValueError):
            number = 0
        if number > 0:
            stat_bits.append(f"{label} {number}")
    if stat_bits:
        meta_bits.append(" · ".join(stat_bits))
    # layout() 会自己渲染 <h1 class="page-title">{title}</h1>, 这里**不再重复**
    # 输出 h1; 头像 + 元信息放卡片里。
    head = (
        '<section class="panel pub-profile-head-card">'
        '<div class="pub-profile-head">'
        f'<div class="pub-profile-avatar">{avatar_html}</div>'
        '<div class="pub-profile-head-main">'
        f'<p class="pub-profile-name">{esc(username)} '
        f'{identity_badge(str(user.get("role") or ""))}</p>'
        f'<p class="pub-profile-meta">{esc(" · ".join(meta_bits))}</p>'
        "</div></div></section>"
    )
    parts: list[str] = [head]

    if allowed("vis_bio") and profile.get("bio"):
        parts.append(
            f'<section class="panel"><h2 class="section-title">'
            f'{i18n.translate("section.description", ctx.lang)}</h2>'
            f'<div class="bio-text">{render_bio(profile.get("bio") or "")}</div>'
            "</section>"
        )
    if allowed("vis_birthday") and profile.get("birthday"):
        parts.append(
            f'<section class="panel"><p class="account-line">'
            f'生日: {esc(profile.get("birthday"))}</p></section>'
        )

    if allowed("vis_favorites") and favorites:
        cards = []
        for item in favorites:
            key = str(item.get("album_key") or "")
            cover = str(item.get("cover_src") or "")
            title = str(item.get("title") or key)
            img = (f'<img class="fav-card-cover" src="{esc(cover)}"'
                   f' alt="{esc(title)}" loading="lazy">'
                   if cover else
                   f'<span class="fav-card-cover cover-fallback">'
                   f'{esc(title[:1] or "?")}</span>')
            cards.append(
                f'<a class="fav-card" href="/album/{esc(quote_segment(key))}">'
                f'{img}<span class="fav-card-title">{esc(title)}</span></a>')
        parts.append(
            '<section class="panel"><h2 class="section-title">收藏</h2>'
            f'<div class="fav-grid">{"".join(cards)}</div></section>')

    if allowed("vis_follows") and follows:
        kinds = {"circle": "社团", "artist": "艺人", "event": "展会",
                 "user": "用户"}
        rows = []
        for item in follows:
            kind = str(item.get("kind") or "")
            key = str(item.get("key") or "")
            label = kinds.get(kind, kind)
            # 社团/艺人能给站内链接就给
            if kind == "circle" and key:
                href = f"/circle/{quote_segment(key)}"
                rows.append(f'<li><span class="muted">{esc(label)}</span> '
                            f'<a href="{esc(href)}">{esc(key)}</a></li>')
            elif kind == "artist" and key:
                href = "/albums?q=" + quote_segment(key)
                rows.append(f'<li><span class="muted">{esc(label)}</span> '
                            f'<a href="{esc(href)}">{esc(key)}</a></li>')
            else:
                rows.append(f'<li><span class="muted">{esc(label)}</span> '
                            f'{esc(key)}</li>')
        parts.append(
            '<section class="panel"><h2 class="section-title">关注</h2>'
            f'<ul class="plain-list">{"".join(rows)}</ul></section>')

    if allowed("vis_contributions") and contributions:
        rows = "".join(
            f'<li><span class="muted">{esc(item.get("created_at_text") or "")}</span> '
            f'[{esc(item.get("kind") or "")}] '
            f'{esc(item.get("summary") or item.get("target") or "")}</li>'
            for item in contributions)
        parts.append(
            '<section class="panel"><h2 class="section-title">贡献</h2>'
            f'<ul class="plain-list">{rows}</ul></section>')

    # 只有头部 + 无其它内容时给个提示(头部永远有统计, 所以不判断 parts[1:])
    if len(parts) <= 1:
        parts.append(
            '<p class="section-note">该用户还没有公开其它资料'
            '(简介 / 收藏 / 关注)。</p>')
    if ctx.user and str((ctx.user or {}).get("username") or "") == username:
        parts.append('<p class="section-note">'
                     '<a href="/account">在我的账号里修改资料</a></p>')
    return layout(ctx, title=username, body="".join(parts),
                  subtitle="公开资料")



def _short(value: Any, limit: int = 80) -> str:
    if value is None:
        return "(空)"
    if isinstance(value, (list, tuple)):
        text = "、".join(str(item) for item in value)
    else:
        text = str(value)
    text = text.strip() or "(空)"
    return text[:limit] + ("…" if len(text) > limit else "")


#: 同一个人在这段时间内(秒)的连续修改会被折叠成一条记录
HISTORY_GROUP_GAP: float = 10 * 60


def group_revisions(
    revisions: "Sequence[Mapping[str, Any]]",
    *,
    gap: float = HISTORY_GROUP_GAP,
) -> list[dict[str, Any]]:
    """把「同一人 + 时间相邻」的修订折叠成组.

    ``revisions`` 必须已按 **id 倒序**(最新在前, 与 list_revisions 一致)。

    返回每个组一个 dict::

        {"first": 首条(最新), "last": 末条(最旧), "items": [逐条],
         "count": n, "span": 首末时间差, "folded": 是否真折叠}

    **只折叠真正连续的同一人修改**: 中间插进来别人改的, 就断开。
    """
    groups: list[dict[str, Any]] = []
    for item in revisions or ():
        stamp = _stamp_value(item.get("created_at"))
        who = str(item.get("username") or "").strip()
        if groups:
            prev = groups[-1]
            prev_stamp = _stamp_value(prev["last"].get("created_at"))
            same_who = who == str(prev["last"].get("username") or "").strip()
            close = (prev_stamp > 0 and stamp > 0
                     and abs(prev_stamp - stamp) <= gap)
            if same_who and close:
                prev["items"].append(item)
                prev["last"] = item
                prev["count"] += 1
                prev["span"] = abs(
                    _stamp_value(prev["first"].get("created_at")) - stamp)
                prev["folded"] = True
                continue
        groups.append({
            "first": item, "last": item, "items": [item],
            "count": 1, "span": 0.0, "folded": False,
        })
    return groups


def _stamp_value(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _history_row(ctx: SiteContext, key: str,
                 group: Mapping[str, Any]) -> str:
    """渲染一条(或一折叠组)修订记录.

    折叠组: 版本号显示 ``#最旧–#最新``, 说明写「N 次 · 约 M 分钟内」,
    并给一个 ``<details>`` 可以逐条展开(每条仍可单独对比)。
    """
    first = group.get("first") or {}
    last = group.get("last") or {}
    count = int(group.get("count") or 1)
    folded = bool(group.get("folded"))
    latest_id = first.get("id") or ""
    oldest_id = last.get("id") or ""

    if folded:
        version = "#{}–#{}".format(esc(oldest_id), esc(latest_id))
        time_text = esc(first.get("created_at_text") or "")
        span_minutes = max(1, int(round(float(group.get("span") or 0) / 60)))
        summaries: list[str] = []
        for item in group.get("items") or ():
            one = str(item.get("summary") or "").strip()
            if one and one not in summaries:
                summaries.append(one)
        detail = "；".join(summaries[:3])
        if len(summaries) > 3:
            detail += " 等"
        summary_html = (
            '<span class="hist-fold-badge">{} 次</span>'
            '<span class="hist-fold-span">约 {} 分钟内</span>'
            '<span class="hist-fold-detail">{}</span>'.format(
                count, span_minutes, esc(detail))
        )
        items_html = "".join(
            '<li><span class="muted">#{}</span> {} '
            '<span class="muted">{}</span> '
            '<a class="btn-ghost btn-xs" href="/album/{}/history?rev={}">'
            "对比</a></li>".format(
                esc(item.get("id") or ""),
                esc(item.get("created_at_text") or ""),
                esc(item.get("summary") or ""),
                esc(quote_segment(key)), esc(item.get("id") or ""))
            for item in (group.get("items") or ()))
        inner = ('<details class="hist-fold-items">'
                 "<summary>展开 {} 条</summary>"
                 '<ul class="plain-list">{}</ul></details>'.format(
                     count, items_html))
    else:
        version = "#{}".format(esc(latest_id))
        time_text = esc(first.get("created_at_text") or "")
        summary_html = esc(first.get("summary") or "")
        inner = ""

    return (
        "<tr>"
        '<td class="num">{}</td>'
        "<td>{}</td>"
        '<td>{}</td>'
        "<td>{}{}</td>"
        '<td class="history-actions">'
        '<a class="btn-ghost btn-xs" href="/album/{}/history?rev={}">对比</a>'
        '<form class="inline-form" method="post" action="/album/revert">'
        + _hidden("csrf_token", ctx.csrf_token)
        + _hidden("album_key", key)
        + _hidden("revision_id", latest_id)
        + '<button class="btn-ghost btn-xs" type="submit">还原整版</button>'
        "</form></td></tr>"
    ).format(
        version, time_text, esc(first.get("username") or "—"),
        summary_html, inner, esc(quote_segment(key)), esc(latest_id))


def history_page(
    ctx: SiteContext,
    *,
    album: Mapping[str, Any],
    revisions: Sequence[Mapping[str, Any]] = (),
    selected: Mapping[str, Any] | None = None,
    diff_rows: Sequence[Mapping[str, Any]] = (),
) -> str:
    """编辑历史: 版本列表 + 与当前版本的差异 + 整版/单字段还原."""
    key = str(album.get("key") or "")
    title = str(album.get("title") or key)
    # 同一人 10 分钟内的连续修改折叠成一条(时间戳已由 site 层格式化)
    groups = group_revisions(revisions)
    rows = "".join(_history_row(ctx, key, group) for group in groups)
    table = (
        '<table class="history-table"><thead><tr>'
        '<th>版本</th><th>时间</th><th>编辑者</th><th>说明</th><th>操作</th>'
        f'</tr></thead><tbody>{rows or "<tr><td colspan=5>还没有修订记录</td></tr>"}</tbody></table>'
    )
    diff_html = ""
    if selected is not None:
        diff_cells = "".join(
            '<tr>'
            f'<td><code>{esc(item.get("field") or "")}</code></td>'
            f'<td class="diff-old">{esc(_short(item.get("after")))}</td>'
            f'<td class="diff-new">{esc(_short(item.get("before")))}</td>'
            '<td><form class="inline-form" method="post" action="/album/revert">'
            + _hidden("csrf_token", ctx.csrf_token)
            + _hidden("album_key", key)
            + _hidden("revision_id", selected.get("id") or "")
            + _hidden("field", item.get("field") or "")
            + '<button class="btn-ghost btn-xs" type="submit">还原此字段</button></form></td>'
            '</tr>'
            for item in diff_rows)
        diff_html = (
            '<section class="panel"><h2 class="section-title">与修订 '
            f'#{esc(selected.get("id") or "")} 的差异</h2>'
            '<p class="section-note">左为当前值，右为该修订里的值；可以只还原本行的字段。</p>'
            '<table class="history-table diff-table"><thead><tr>'
            '<th>字段</th><th>当前</th><th>该修订</th><th>操作</th></tr></thead>'
            f'<tbody>{diff_cells or "<tr><td colspan=4>这个修订与当前内容没有差异</td></tr>"}'
            '</tbody></table></section>'
        )
    body = (
        '<p class="section-note"><a class="btn-ghost btn-xs" href="/album/'
        + quote_segment(key) + '">← 返回专辑</a></p>'
        '<section class="panel"><h2 class="section-title">编辑历史 · '
        + esc(title) + '</h2>'
        '<p class="section-note">每次保存都会留一条修订；「对比」看差异，'
        '「还原整版」回到该版本，「还原此字段」只改一个字段。</p>'
        + table + '</section>' + diff_html
    )
    return layout(ctx, title=f"编辑历史 · {title}", body=body, active="albums")


def messages_page(
    ctx: SiteContext,
    *,
    inbox: Sequence[Mapping[str, Any]] = (),
    outbox: Sequence[Mapping[str, Any]] = (),
    conversations: Sequence[Mapping[str, Any]] = (),
    box: str = "inbox",
    error: str = "",
    notice: str = "",
) -> str:
    items = inbox if box == "inbox" else outbox
    rows = "".join(
        f'<li class="msg-row{" is-unread" if not item.get("read") else ""}">'
        f'<div class="msg-head"><b>{esc(item.get("peer_name") or "?")}</b>'
        f'<span class="muted"> · {esc(item.get("created_at_text") or "")}</span></div>'
        f'<div class="msg-subject">{esc(item.get("subject") or "(无主题)")}</div>'
        f'<div class="msg-body">{esc(item.get("body") or "")}</div></li>'
        for item in items)
    convs = "".join(
        f'<li><a href="/messages?peer={esc(quote_segment(str(item.get("peer_name") or "")))}">'
        f'<b>{esc(item.get("peer_name") or "?")}</b>'
        + (f'<span class="nav-badge">{esc(item.get("unread") or 0)}</span>'
           if item.get("unread") else "")
        + f'</a><div class="muted">{esc(item.get("last") or "")} · '
        f'{esc(item.get("last_at") or "")}</div></li>'
        for item in conversations)
    conv_html = ""
    if box == "inbox" and conversations:
        conv_html = (f'<section class="panel"><h3 class="account-h">会话</h3>'
                     f'<ul class="plain-list msg-convs">{convs}</ul></section>')
    tabs = (
        f'<p class="tabs"><a class="tab{" is-active" if box == "inbox" else ""}" href="/messages">收件箱</a>'
        f'<a class="tab{" is-active" if box == "outbox" else ""}" href="/messages?box=outbox">发件箱</a></p>'
    )
    body = (
        _notice_html(error, notice)
        + tabs
        + conv_html
        + (f'<ul class="plain-list msg-list">{rows}</ul>' if rows
           else '<p class="section-note">没有消息。</p>')
        + '<section class="panel account-section"><h3 class="account-h">发一条私信</h3>'
        + '<form class="auth-form" method="post" action="/messages/send">'
        + _hidden("csrf_token", ctx.csrf_token)
        + '<label class="field auth-field"><span>收件人用户名</span>'
        '<input type="text" name="to" required maxlength="32"></label>'
        '<label class="field auth-field"><span>主题<span class="field-hint">选填</span></span>'
        '<input type="text" name="subject" maxlength="100"></label>'
        '<label class="field auth-field"><span>正文</span>'
        '<textarea name="body" rows="4" maxlength="2000" required></textarea></label>'
        '<div class="field-actions"><button type="submit">发送</button></div></form></section>'
    )
    return layout(ctx, title="私信", body=body, active="messages")


def message_thread_page(
    ctx: SiteContext,
    *,
    peer_name: str,
    messages: Sequence[Mapping[str, Any]] = (),
) -> str:
    """与某个用户的私信往来(时间正序) + 回复框."""
    rows = "".join(
        f'<li class="msg-row {"is-mine" if item.get("direction") == "out" else ""}">'
        f'<span class="muted">{esc(item.get("created_at_text") or "")}</span> '
        f'<div>{esc(item.get("body") or "")}</div></li>'
        for item in messages)
    body = (
        f'<p class="section-note"><a class="btn-ghost btn-xs" href="/messages">← 返回会话列表</a></p>'
        '<section class="panel"><h3 class="account-h">与 '
        + esc(peer_name) + ' 的私信</h3>'
        + (f'<ul class="plain-list msg-thread">{rows}</ul>' if rows
           else '<p class="section-note">还没有往来消息。</p>')
        + '<form class="auth-form" method="post" action="/messages/send">'
        + f'<input type="hidden" name="csrf_token" value="{esc(ctx.csrf_token)}">'
        + f'<input type="hidden" name="to" value="{esc(peer_name)}">'
        + '<label class="field auth-field"><span>回复</span>'
        '<textarea name="body" rows="3" maxlength="2000" required></textarea></label>'
        '<div class="field-actions"><button type="submit">发送</button></div></form></section>'
    )
    return layout(ctx, title=f"与 {peer_name} 的私信", body=body, active="messages")


def notifications_page(
    ctx: SiteContext,
    *,
    items: Sequence[Mapping[str, Any]] = (),
) -> str:
    rows = "".join(
        f'<li class="{"is-unread" if not item.get("read") else ""}">'
        f'<span class="muted">{esc(item.get("created_at_text") or "")}</span> '
        f'{esc(item.get("text") or "")}'
        + (f' <a class="btn-ghost btn-xs" href="{esc(item.get("link"))}">查看</a>'
           if item.get("link") else "")
        + "</li>"
        for item in items)
    body = (
        '<section class="panel">'
        '<p class="section-note">站内通知: 审核结果、系统提示等。</p>'
        + (f'<ul class="plain-list">{rows}</ul>' if rows
           else '<p class="section-note">暂时没有通知。</p>')
        + '<form class="inline-form" method="post" action="/notifications/read">'
        + _hidden("csrf_token", ctx.csrf_token)
        + '<button class="btn-ghost btn-xs" type="submit">全部标为已读</button></form>'
        '</section>'
    )
    return layout(ctx, title="站内通知", body=body, active="notifications")


def verify_page(
    ctx: SiteContext,
    *,
    username: str = "",
    email: str = "",
    error: str = "",
    notice: str = "",
    resend_wait: int = 0,
) -> str:
    """邮箱验证页: 输入 6 位验证码(POST /verify), 或重新发送(POST /verify/resend).

    ``username`` 为空时(案例: 直接访问且未登录)提示先登录 —— 由路由层保证不常见。
    """
    if not username:
        body = (
            '<p class="section-note">请先登录, 再回到本页验证邮箱。</p>'
            '<p><a class="btn" href="/login?next=/verify">去登录</a></p>'
        )
        return layout(ctx, title="邮箱验证", body=_auth_card("邮箱验证", "", body), active="verify")
    where = f"验证码已发送到 <b>{esc(email)}</b>" if email else "请输入邮箱验证码"
    wait_html = (
        f'<p class="section-note">刚刚发送过, 请 {esc(resend_wait)} 秒后再试。</p>'
        if resend_wait else ""
    )
    body = f"""{_notice_html(error, notice)}
<p class="section-note">正在验证账号 <b>{esc(username)}</b>: {where}。</p>
<form class="auth-form" method="post" action="/verify">
<input type="hidden" name="csrf_token" value="{esc(ctx.csrf_token)}">
<input type="hidden" name="username" value="{esc(username)}">
{auth_field("code", "6 位验证码", "", autocomplete="one-time-code",
            hint="15 分钟内有效")}
<div class="field-actions"><button type="submit">验证</button></div>
</form>
{wait_html}
<form class="inline-form" method="post" action="/verify/resend">
<input type="hidden" name="csrf_token" value="{esc(ctx.csrf_token)}">
<input type="hidden" name="username" value="{esc(username)}">
<button type="submit" class="btn-ghost">重新发送验证码</button>
</form>
<p class="auth-alt"><a href="/login">返回登录</a> · <a href="/account">我的账号</a></p>"""
    return layout(ctx, title="邮箱验证", body=_auth_card("邮箱验证", "验证后即可正常登录。", body),
                  active="verify")


def review_page(
    ctx: SiteContext,
    *,
    requests: Sequence[Mapping[str, Any]],
    counts: Mapping[str, Any],
    status: str = "pending",
    error: str = "",
    notice: str = "",
) -> str:
    """审核队列(仅超级用户): 待审核/已通过/已拒绝 tab + 通过/拒绝表单."""
    tabs = (("pending", "待审核"), ("approved", "已通过"), ("rejected", "已拒绝"), ("all", "全部"))
    tab_html = "".join(
        '<a class="tab{a}" href="/review?status={s}">{label}'
        '<span class="tab-count">{n}</span></a>'.format(
            a=" is-active" if key == status else "",
            s=esc(key), label=esc(label),
            n=esc(counts.get(key if key != "all" else "all", 0)),
        )
        for key, label in tabs
    )
    rows: list[str] = []
    for item in requests:
        request_id = item.get("id")
        state = str(item.get("status") or "pending")
        actions = ""
        if state == "pending":
            actions = (
                '<div class="review-actions">'
                '<form class="inline-form" method="post" action="/review">'
                f'<input type="hidden" name="csrf_token" value="{esc(ctx.csrf_token)}">'
                f'<input type="hidden" name="request_id" value="{esc(request_id)}">'
                '<input type="hidden" name="action" value="approve">'
                '<button type="submit" class="btn-ghost btn-xs">通过</button></form>'
                '<form class="inline-form" method="post" action="/review">'
                f'<input type="hidden" name="csrf_token" value="{esc(ctx.csrf_token)}">'
                f'<input type="hidden" name="request_id" value="{esc(request_id)}">'
                '<input type="hidden" name="action" value="reject">'
                '<button type="submit" class="btn-ghost btn-xs">拒绝</button></form>'
                "</div>"
            )
        rows.append(
            '<li class="review-row">'
            f'<span class="review-who"><b>{esc(item.get("username") or "")}</b>'
            f'<span class="muted">#{esc(request_id)} · {esc(item.get("created_at_text") or "")}</span></span>'
            f'<span class="review-reason">{esc(item.get("reason") or "(未填写理由)")}</span>'
            f'<span class="status-chip status-{esc(state)}">{esc(item.get("status_label") or "")}</span>'
            + (
                '<span class="review-meta">处理人 '
                + esc(item.get("handled_by") or "")
                + ((" · " + esc(item.get("note"))) if item.get("note") else "")
                + "</span>" if item.get("handled_by") else ""
            )
            + actions
            + "</li>"
        )
    body = (
        _notice_html(error, notice)
        + f'<div class="tabs">{tab_html}</div>'
        + f'<p class="results-count">共 <b>{esc(counts.get("all", len(requests)))}</b> 条申请, '
          f'待审核 <b>{esc(counts.get("pending", 0))}</b> 条</p>'
        + (
            '<ul class="review-list">' + "".join(rows) + "</ul>"
            if rows else '<div class="empty"><p>这里没有申请。</p></div>'
        )
    )
    return layout(ctx, title="审核队列", body=body, active="review",
                  subtitle="通过后对方立即成为编辑者, 可以订正专辑资料。")


def users_admin_page(
    ctx: SiteContext,
    *,
    users: Sequence[Mapping[str, Any]],
    counts: Mapping[str, Any],
    error: str = "",
    notice: str = "",
) -> str:
    """用户管理(仅超级用户): 改角色 / 禁用启用."""
    role_options = (("viewer", "普通用户"), ("editor", "编辑者"), ("superuser", "超级用户"))
    rows: list[str] = []
    for user in users:
        name = str(user.get("username") or "")
        options = "".join(
            f'<option value="{esc(value)}"{" selected" if user.get("role") == value else ""}>'
            f"{esc(label)}</option>"
            for value, label in role_options
        )
        active = bool(user.get("is_active", True))
        profile_href = "/u/" + quote_segment(name) + "/"
        avatar = (
            f'<img class="user-row-avatar" src="/avatar/{esc(user.get("id") or "")}"'
            f' alt="{esc(name)}的头像" loading="lazy"'
            " onerror=\"this.classList.add('is-fallback')\" />"
        )
        rows.append(
            '<li class="user-row">'
            f'<a class="user-row-id" href="{esc(profile_href)}"'
            f' title="查看 {esc(name)} 的主页">{avatar}</a>'
            f'<span class="user-name"><a href="{esc(profile_href)}">'
            f'<b>{esc(name)}</b></a>'
            f'<span class="muted"> #{esc(user.get("id") or "")}</span></span>'
            f'<span class="status-chip status-{("active" if active else "inactive")}">'
            f'{"启用" if active else "已禁用"}</span>'
            f'<span class="user-email">{esc(user.get("email") or "—")}'
            f'{" ✓" if user.get("email_verified") else ""}</span>'
            '<form class="inline-form" method="post" action="/users">'
            f'<input type="hidden" name="csrf_token" value="{esc(ctx.csrf_token)}">'
            f'<input type="hidden" name="username" value="{esc(name)}">'
            '<input type="hidden" name="action" value="set_role">'
            f'<select name="role">{options}</select>'
            '<button type="submit" class="btn-ghost btn-xs">改角色</button></form>'
            '<form class="inline-form" method="post" action="/users">'
            f'<input type="hidden" name="csrf_token" value="{esc(ctx.csrf_token)}">'
            f'<input type="hidden" name="username" value="{esc(name)}">'
            f'<input type="hidden" name="action" value="{"disable" if active else "enable"}">'
            f'<button type="submit" class="btn-ghost btn-xs">{"禁用" if active else "启用"}</button>'
            "</form>"
            "</li>"
        )
    summary = " · ".join(
        [
            f"共 {esc(counts.get('all', len(users)))} 个账号",
            f"超级用户 {esc(counts.get('superuser', 0))}",
            f"编辑者 {esc(counts.get('editor', 0))}",
            f"普通用户 {esc(counts.get('viewer', 0))}",
        ]
    )
    body = (
        _notice_html(error, notice)
        + f'<p class="results-count">{summary}</p>'
        + '<ul class="user-list">' + "".join(rows) + "</ul>"
        + '<p class="section-note">改角色会立即作用于对方的在线会话; '
          "禁用会把对方踢下线并且无法再登录。</p>"
    )
    return layout(ctx, title="用户管理", body=body, active="users_admin")


# --------------------------------------------------------------------------
# 编辑者: 专辑编辑表单
# --------------------------------------------------------------------------

def _format_duration_text(value: Any) -> str:
    """秒数 -> ``分:秒``; 已经是字符串(含冒号)就原样返回."""
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ""
        # 已经是 3:00 这种形式
        if ":" in text:
            return text
        try:
            value = int(float(text))
        except (TypeError, ValueError):
            return text
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return ""
    if seconds < 0:
        return ""
    return "{}:{:02d}".format(seconds // 60, seconds % 60)


def edit_values_from_album(album: Any) -> dict[str, Any]:
    """详情视图 / Album 对象 -> 编辑表单的初值(纯函数, 便于单测).

    两种入参都支持:

    * ``dict``: :meth:`Album.to_dict` 那种视图(``title_alt`` / ``romaji`` / ``date``);
    * ``Album`` 对象: 直接读属性(``title`` / ``title_original`` / ``release_date`` ...)。

    早先只按 dict 写, 而编辑页传的是 Album 对象, 于是 ``album.get`` 抛
    AttributeError, 调用方兜底成空 dict —— 表单全空, 再提交就把数据清掉并报
    「没有保存」。这里做归一化, 从根上避免。
    """
    def _attr(obj: Any, *names: str, default: Any = "") -> Any:
        for name in names:
            if isinstance(obj, Mapping):
                value = obj.get(name)
            else:
                value = getattr(obj, name, None)
            if value not in (None, "", [], {}):
                return value
        return default

    def _text_list(value: Any, key: str = "") -> list[str]:
        """把 list[dict] / list[str] / dict 统一成字符串列表."""
        if not value:
            return []
        result: list[str] = []
        if isinstance(value, Mapping):
            for item_key, item_value in value.items():
                if key and isinstance(item_value, Mapping):
                    text = str(item_value.get(key) or "").strip()
                else:
                    text = str(item_value if item_value else item_key).strip()
                if text:
                    result.append(text)
            return result
        for item in value:
            if isinstance(item, Mapping):
                text = str(item.get(key or "name") or item.get("title")
                           or item.get("source") or "").strip()
            else:
                text = str(item or "").strip()
            if text:
                result.append(text)
        return result

    # 曲目: 既支持 dict 视图的 duration_text, 也支持 Track 对象的 duration
    tracks_raw = _attr(album, "tracks", default=[])
    tracks: list[dict[str, Any]] = []
    for index, track in enumerate(tracks_raw or [], start=1):
        if isinstance(track, Mapping):
            title = str(track.get("title") or "")
            artists = track.get("artists") or []
            # duration_text 优先; 只有裸秒数时格式化成分:秒
            duration = str(track.get("duration_text") or "")
            if not duration:
                raw_duration = track.get("duration")
                duration = _format_duration_text(raw_duration)
            position = int(track.get("index") or index)
            disc = int(track.get("disc") or 1)
        else:
            title = str(getattr(track, "title", "") or "")
            artists = getattr(track, "artists", []) or []
            duration = _format_duration_text(getattr(track, "duration", None))
            position = int(getattr(track, "index", index) or index)
            disc = int(getattr(track, "disc", 1) or 1)
        # 补充字段: dict 视图和 Track 对象都要兼容
        if isinstance(track, Mapping):
            subtitle = str(track.get("subtitle") or "")
            title_zh = str(track.get("title_zh") or "")
            lang = str(track.get("lang") or "")
            genre = str(track.get("genre") or "")
        else:
            subtitle = str(getattr(track, "subtitle", "") or "")
            title_zh = str(getattr(track, "title_zh", "") or "")
            lang = str(getattr(track, "lang", "") or "")
            genre = str(getattr(track, "genre", "") or "")
        tracks.append({
            # 碟号: 单碟留空(表单里空 = 1), 多碟才显示数字
            "disc": disc if disc > 1 else "",
            "index": position,
            "title": title,
            "artists": "、".join(str(one) for one in (artists or [])),
            "duration": duration,
            "subtitle": subtitle,
            "title_zh": title_zh,
            "lang": lang,
            "genre": genre,
        })

    # 词条: dict 视图是 [{"role","name"}], 库里可能是分组结构
    credits_raw = _attr(album, "credits", default=[])
    credits: list[dict[str, str]] = []
    for group in credits_raw or []:
        if isinstance(group, Mapping):
            if group.get("names"):
                name_text = "、".join(str(n) for n in group["names"] if n)
            else:
                name_text = str(group.get("name") or group.get("who") or "")
            credits.append({
                "role": str(group.get("role") or ""),
                "name": name_text,
            })
        else:
            names = getattr(group, "names", None)
            if names:
                name_text = "、".join(str(n) for n in names if n)
            else:
                name_text = str(getattr(group, "name", "") or "")
            credits.append({
                "role": str(getattr(group, "role", "") or ""),
                "name": name_text,
            })

    # 外链: dict 视图是 [{"source","url"}], 库里是 {"名称": "url"}
    urls_raw = _attr(album, "urls", default=[])
    urls: list[dict[str, str]] = []
    if isinstance(urls_raw, Mapping):
        urls = [{"key": str(k), "url": str(v)} for k, v in urls_raw.items()]
    else:
        for link in urls_raw or []:
            if isinstance(link, Mapping):
                urls.append({"key": str(link.get("source") or link.get("key") or ""),
                             "url": str(link.get("url") or "")})


    return {
      "title": str(_attr(album, "title_alt", "title", default="") or ""),
      "title_original": str(_attr(album, "title", "title_original",
                                  default="") or ""),
      "title_romaji": str(_attr(album, "romaji", "title_romaji", default="") or ""),
      "circle": str(_attr(album, "circle", default="") or ""),
      "artists": "、".join(str(a) for a in (_attr(album, "artists",
                                                 default=[]) or []) if a),
      "release_date": str(_attr(album, "date", "release_date", default="") or ""),
      "event": str(_attr(album, "event", default="") or ""),
      "catalog_no": str(_attr(album, "catalog_no", default="") or ""),
      "company": str(_attr(album, "company", default="") or ""),
      "price": str(_attr(album, "price", default="") or ""),
      "booth": str(_attr(album, "booth", default="") or ""),
      "hall": str(_attr(album, "hall", default="") or ""),
      "tags": "、".join(_text_list(_attr(album, "tags", default=[]))),
      "genres": "、".join(_text_list(_attr(album, "genres", default=[]))),
      "description": str(_attr(album, "description", default="") or ""),
      "credits": credits,
      "urls": urls,
      "tracks": tracks,
      # 「整段粘贴」是本次编辑的临时输入, 不从库里带值出来
      "track_paste": "",
      "track_mode": "replace",
    }


def cover_src(key: str, album: Mapping[str, Any] | None = None) -> str:
    """封面 URL, 带版本参数防止 CDN/浏览器缓存旧图.

    版本取 ``cover_v``(上传封面时写入) 或 cover_local 的文件 mtime;
    换图后 URL 会变, Cloudflare 就不会再给旧副本。
    """
    base = "/cover/" + quote_segment(str(key))
    album = album or {}
    version = str(album.get("cover_v") or "")
    if not version:
        local = str(album.get("cover_local") or "")
        if local:
            try:
                from pathlib import Path as _Path

                # 与 data.py 的 cover_href 保持同一口径: 只用秒级 mtime,
                # 否则同一张图在编辑页和详情页会有两个不同 URL(重复下载)。
                version = str(int(_Path(local).stat().st_mtime))
            except (OSError, ValueError):
                version = ""
    return f"{base}?v={version}" if version else base


def _text_input(values: Mapping[str, Any], errors: Mapping[str, Any], name: str, label: str,
                *, kind: str = "text", hint: str = "", required: bool = False,
                lang: str = "zh") -> str:
    # 标签允许传 i18n key("field.title"), 统一走 translate; 传中文原样显示
    label = i18n.translate(label, lang, default=label) if "." in label else label
    value = values.get(name) or ""
    error = str(errors.get(name) or "")
    cls = "field auth-field has-error" if error else "field auth-field"
    hint_html = f'<span class="field-hint">{esc(hint)}</span>' if hint else ""
    req = " required" if required else ""
    # 必填项用 * 标记(用户要求: 编辑界面用 * 显示该字段是否必填)
    star = '<em class="req-star" title="">*</em>' if required else ""
    err_html = f'<span class="field-error">{esc(error)}</span>' if error else ""
    return (
        f'<label class="{cls}"><span>{esc(label)}{hint_html}{star}</span>'
        f'<input type="{esc(kind)}" name="{esc(name)}" value="{esc(value)}"{req}></label>{err_html}'
    )


def _bind_input(values: Mapping[str, Any], errors: Mapping[str, Any], name: str,
                label: str, *, kind: str = "text", hint: str = "",
                options: Sequence[Mapping[str, Any]] = ()) -> str:
    """可绑定实体的输入框: 手输名称, 或输入编号(#12 / artistId:12).

    候选来自 ``options``(``[{id, name}]``), 用 ``<datalist>`` 提供, 不依赖 JS。
    """
    value = values.get(name) or ""
    error = str(errors.get(name) or "")
    cls = "field auth-field has-error" if error else "field auth-field"
    hint_text = hint or "可输名称, 或编号(#12)"
    hint_html = f'<span class="field-hint">{esc(hint_text)}</span>'
    err_html = f'<span class="field-error">{esc(error)}</span>' if error else ""
    list_id = f"dl-{esc(name)}"
    options_html = "".join(
        f'<option value="{esc(item.get("name") or "")}">'
        f'#{esc(item.get("id") or "")}</option>'
        for item in options
    )
    list_html = f'<datalist id="{list_id}">{options_html}</datalist>' if options_html else ""
    return (
        f'<label class="{cls}"><span>{esc(label)}{hint_html}</span>'
        f'<input type="{esc(kind)}" name="{esc(name)}" value="{esc(value)}"'
        f' list="{list_id}" autocomplete="off"></label>{err_html}{list_html}'
    )


def _textarea(values: Mapping[str, Any], errors: Mapping[str, Any], name: str, label: str,
              *, hint: str = "") -> str:
    value = values.get(name) or ""
    error = str(errors.get(name) or "")
    cls = "field auth-field" + (" has-error" if error else "")
    hint_html = f'<span class="field-hint">{esc(hint)}</span>' if hint else ""
    err_html = f'<span class="field-error">{esc(error)}</span>' if error else ""
    return (
        f'<label class="{cls}"><span>{esc(label)}{hint_html}</span>'
        f'<textarea name="{esc(name)}" rows="6">{esc(value)}</textarea></label>{err_html}'
    )

def _track_row_html(row: Mapping[str, Any], disc: int, position: int) -> str:
    """曲目表的一行(表格行). 每行带全部 track_* 字段名, 解析按名字对齐.

    列顺序: 排序(⠿) | 音轨 | 歌曲名 | 副标题 | 翻译歌名 | 艺人 | 语种 | 曲风 | 时长 | 操作
    拖动把手放在**最前面**, 方便一眼看到并按住拖动。
    """
    def _val(name: str) -> str:
        value = row.get(name)
        if value is None:
            return ""
        # artists 可能是列表
        if isinstance(value, (list, tuple)):
            return esc("、".join(str(one) for one in value if one))
        return esc(str(value))

    index_value = row.get("index")
    if index_value in (None, ""):
        index_value = position
    return (
        '<tr class="track-row" data-disc="{disc}">'
        # ① 排序(拖动把手放最前)
        '<td class="trk-col-sort" data-label="排序">'
        '<button type="button" class="trk-drag" data-track-drag="1" '
        'draggable="true" title="按住拖动排序" aria-label="拖动排序">⠿</button>'
        # **隐藏字段必须放在 td 里** —— <tr> 的直接子元素只能是 td/th,
        # 直接塞 input 是非法 HTML, 浏览器会把它们移到表格外并重排整行。
        '<input type="hidden" name="track_disc" value="{disc}">'
        "</td>"
        # ② 音轨
        '<td class="trk-col-idx" data-label="音轨">'
        '<input type="text" name="track_index" value="{index}" inputmode="numeric">'
        "</td>"
        # ③ 歌曲名
        '<td data-label="歌曲名">'
        '<input type="text" name="track_title" value="{title}"></td>'
        # ④ 副标题
        '<td data-label="副标题">'
        '<input type="text" name="track_subtitle" value="{subtitle}" '
        'placeholder="可选"></td>'
        # ⑤ 翻译歌名
        '<td data-label="翻译歌名">'
        '<input type="text" name="track_title_zh" value="{title_zh}" '
        'placeholder="可选"></td>'
        # ⑥ 艺人
        '<td data-label="艺人">'
        '<input type="text" name="track_artists" value="{artists}" '
        'placeholder="、分隔"></td>'
        # ⑦ 语种
        '<td data-label="语种">'
        '<input type="text" name="track_lang" value="{lang}" '
        'placeholder="可选"></td>'
        # ⑧ 曲风
        '<td data-label="曲风">'
        '<input type="text" name="track_genre" value="{genre}" '
        'placeholder="可选"></td>'
        # ⑨ 时长(新增回来)
        '<td data-label="时长">'
        '<input type="text" name="track_duration" value="{duration}" '
        'inputmode="numeric" placeholder="3:42"></td>'
        # ⑩ 操作
        '<td class="trk-col-ops" data-label="操作">'
        '<button type="button" class="btn-ghost btn-xs" data-track-del="1" '
        'title="删除这一首">删除</button>'
        "</td>"
        "</tr>"
    ).format(
        disc=disc,
        index=esc(str(index_value)),
        title=_val("title"),
        subtitle=_val("subtitle"),
        title_zh=_val("title_zh"),
        artists=_val("artists"),
        lang=_val("lang"),
        genre=_val("genre"),
        duration=_val("duration"),
    )



def _infobox_button() -> str:
    """标题行里的「编辑 infobox」按钮.

    用户要求把按钮放在「新建专辑」旁边, 并且**不要**那句粘贴提示 ——
    提示文案占位又啰嗦, 按钮自己说明了一切。
    """
    return ('<button type="button" class="btn-ghost btn-xs"'
            ' data-infobox-open="1">编辑 infobox</button>')


def _infobox_widget(album: Mapping[str, Any]) -> str:
    """infobox 弹窗(纯前端, 不请求服务端).

    弹窗是 ``position: fixed``, 放在页面哪里都一样; 但**按钮**在标题行,
    所以这里只输出两个 modal —— 不输出 .infobox-bar 那一行。
    """
    from .. import infobox as IB

    # 当前专辑的 infobox(编辑页预填; 新建页为空)
    try:
        current = IB.build(album) if album else ""
    except Exception:                         # noqa: BLE001
        current = ""

    return (
        '<div class="infobox-modal" data-infobox-modal="1" hidden>'
        '<div class="infobox-panel">'
        '<div class="infobox-head"><strong>dddb infobox</strong>'
        '<button type="button" class="infobox-x" data-infobox-close="1"'
        ' aria-label="关闭">✕</button></div>'
        '<textarea class="infobox-text" data-infobox-text="1" rows="16"'
        ' spellcheck="false" placeholder="[dddb]\ntitle_original || 标题">'
        '{current}</textarea>'
        '<div class="infobox-acts">'
        '<button type="button" class="btn-ghost btn-xs"'
        ' data-infobox-help="1">格式说明</button>'
        '<span class="infobox-msg" data-infobox-msg="1"></span>'
        '<button type="button" class="btn-ghost btn-xs"'
        ' data-infobox-close="1">取消</button>'
        '<button type="button" class="btn" data-infobox-apply="1">填入</button>'
        '</div></div></div>'
        '<div class="infobox-modal" data-infobox-help-modal="1" hidden>'
        '<div class="infobox-panel infobox-help-panel">'
        '<div class="infobox-head"><strong>infobox 格式</strong>'
        '<button type="button" class="infobox-x" data-infobox-help-close="1"'
        ' aria-label="关闭">✕</button></div>'
        '<pre class="infobox-help-text">{help_text}</pre>'
        '</div></div>'
    ).format(current=esc(current), help_text=esc(IB.HELP_TEXT))


def _photos_html(key: Any, ctx: SiteContext,
                 album: Mapping[str, Any]) -> str:
    """「其他照片」区: 与封面分开, 可加备注.

    用户要求: "右侧加号是添加其他照片，其他照片不含封面，可以配备注"

    照片**不参与 cover_local** —— 它只是附加图, 不影响封面。
    """
    from .photos import photos_of

    photos = photos_of(album.get("photos") or [])
    cards = []
    for ph in photos:
        url = esc(ph.get("url") or "")
        note = esc(ph.get("note") or "")
        cards.append(
            '<figure class="photo-card">'
            '<img src="{}" alt="照片" loading="lazy">'
            '<input type="text" class="photo-note" value="{}"'
            ' placeholder="备注（可选）" maxlength="200"'
            ' data-photo-note="{}">'
            '<button type="button" class="photo-del" title="删除"'
            ' aria-label="删除这张照片" data-photo-del="{}">✕</button>'
            '</figure>'.format(url, note, url, url))
    # **只露照片网格**: 标题与「＋ 添加照片」按钮都去掉了
    # (用户要求: 添加途径统一用左侧大加号)
    if not cards:
        return ""
    return (
        '<div class="photo-block" data-photo-block="1">'
        '<div class="photo-grid">{}</div>'
        '</div>'.format("".join(cards))
    )


def edit_album_page(
    ctx: SiteContext,
    *,
    album: Mapping[str, Any],
    values: Mapping[str, Any],
    errors: Mapping[str, Any] | None = None,
    notice: str = "",
    bind_options: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    creating: bool = False,
) -> str:
    """编辑者专用的专辑编辑表单(纯 HTML 表单 + 隐藏域 CSRF token, 零 JS).

    逐项覆盖任务要求: 标题 / 社团 / 艺人 / 发售日 / 展会 / 编号 / 简介 / 标签 /
    曲风 / 词条(credits) / 链接(urls) / 曲目(tracks)。
    """
    errs: Mapping[str, Any] = errors or {}
    bind_options: Mapping[str, Sequence[Mapping[str, Any]]] = bind_options or {}
    # 入参可能是 Album 对象, 也可能是详情视图 dict —— 统一成 dict 再往下走,
    # 否则 album.get(...) 会 AttributeError, 整个编辑页渲染失败。
    if not isinstance(album, Mapping):
        album = album.to_dict() if hasattr(album, "to_dict") else dict(album)
    key = str(album.get("key") or "")
    key_html = esc(key)
    # 新建模式: 与编辑页**同一套卡片界面**, 只是提交目标/按钮/面包屑不同
    _form_action = "/new/album" if creating else "/album/{}/edit".format(
        quote_segment(key))
    _page_title = ("新建专辑" if creating
                   else i18n.translate("edit.album", ctx.lang))
    _cancel_href = "/edit" if creating else "/album/{}".format(
        quote_segment(key))
    _key_field = (
        '<label class="field auth-field"><span>编号 / 短名'
        '<span class="field-hint">留空自动生成</span></span>'
        '<input type="text" name="album_key" value="{}"'
        ' placeholder="留空自动生成"></label>'.format(esc(key))
        if creating else ""
    )
    notice_html = f'<div class="form-error" role="alert">{esc(notice)}</div>' if notice else ""
    # 校验错误统一列在这里: 有些错误(词条/外链/曲目行)没有对应的单个输入框,
    # 只靠红字会看不见 —— 列出来保证用户知道到底哪儿不对。
    messages: list[str] = []
    for value in errs.values():
        text = str(value or "")
        if text and text not in messages:
            messages.append(text)
    error_count = len(messages)
    summary = (
        '<div class="form-error" role="alert">有 {} 处需要修正:<ul class="form-error-list">{}</ul></div>'.format(
            error_count, "".join(f"<li>{esc(item)}</li>" for item in messages)
        )
        if error_count else ""
    )

    def rows_for(items: Sequence[Mapping[str, Any]], blanks: int) -> list[Mapping[str, Any]]:
        rows = list(items)
        while len(rows) < len(items) + blanks:
            rows.append({})
        return rows

    # **不补空行**: 有数据就只显示数据行; **完全没有**时给 1 行占位,
    # 免得看到一片空白还得先点「添加词条」才能填。
    credit_rows = list(values.get("credits") or []) or [{}]
    # 词条行: 职位 / 人员 / **原名** / **别名**(后两个从歌词解析来时才会填)
    credits_html = "".join(
        '<div class="pair-row" data-pair-row="credit">'
        '<label class="field auth-field"><span>词条</span>'
        '<input type="text" name="credit_role" value="{}"'
        ' placeholder="如 编曲 / 演唱 / 钢琴"></label>'
        '<label class="field auth-field"><span>人员</span>'
        '<input type="text" name="credit_name" value="{}"'
        ' placeholder="多人用、分隔"></label>'
        '<input type="hidden" name="credit_raw_role" value="{}">'
        '<button type="button" class="pair-del" data-pair-del="credit"'
        ' title="删除这一条" aria-label="删除这一条">✕</button>'
        "</div>".format(
            esc(row.get("role") or ""), esc(row.get("name") or ""),
            esc(row.get("raw_role") or ""))
        for row in credit_rows
    )
    credits_add_html = (
        '<div class="pair-add">'
        '<button type="button" class="btn-ghost btn-xs" data-pair-add="credit">'
        "＋ 添加词条</button></div>"
    )
    # 同上: 外链也不补空行, 全空时给 1 行占位
    url_rows = list(values.get("urls") or []) or [{}]
    urls_html = "".join(
        '<div class="pair-row" data-pair-row="url">'
        '<label class="field auth-field"><span>链接名</span>'
        '<input type="text" name="url_key" value="{}" placeholder="自定义标签"></label>'
        '<label class="field auth-field"><span>网址</span>'
        '<input type="text" name="url_value" value="{}" placeholder="https://..."></label>'
        '<button type="button" class="pair-del" data-pair-del="url"'
        ' title="删除这一条" aria-label="删除这一条">✕</button>'
        "</div>".format(esc(row.get("key") or ""), esc(row.get("url") or ""))
        for row in url_rows
    )
    urls_add_html = (
        '<div class="pair-add">'
        '<button type="button" class="btn-ghost btn-xs" data-pair-add="url">'
        "＋ 添加外链</button></div>"
    )
    # 曲目「整段粘贴」区: 用户端零脚本, 所以粘贴框就是**表单的一个普通字段**
    # (name="track_paste"), 跟着表单一起 POST, 由服务端 dddb/trackparse.py 解析。
    # 校验失败重填时把用户粘过的内容原样还回去(并把折叠区展开), 别让人白粘一次。
    paste_text = str(values.get("track_paste") or "")
    track_mode = str(values.get("track_mode") or "replace")
    if track_mode not in ("replace", "append"):
        track_mode = "replace"
    replace_checked = " checked" if track_mode == "replace" else ""
    append_checked = " checked" if track_mode == "append" else ""
    paste_html = (
        '<details class="trk-paste"' + (" open" if paste_text else "") + ">"
        "<summary>从别处整段粘贴曲目(自动识别 曲名 / 艺人 / 时长)</summary>"
        '<div class="trk-paste-body">'
        '<label class="field auth-field"><span>粘贴内容'
        '<span class="field-hint">每行一首, 识别不出的行会被忽略</span></span>'
        '<textarea name="track_paste" rows="6" spellcheck="false" '
        'placeholder="01. 曲名 - 艺人 4:12&#10;02. 曲名 / 艺人&#10;Tr.3 曲名 4分12秒">'
        f"{esc(paste_text)}</textarea>"
        "</label>"
        '<div class="trk-paste-modes">'
        f'<label class="trk-mode"><input type="radio" name="track_mode" value="replace"{replace_checked}>'
        " 替换现有曲目</label>"
        f'<label class="trk-mode"><input type="radio" name="track_mode" value="append"{append_checked}>'
        " 追加到末尾</label>"
        "</div>"
        '<p class="trk-paste-hint muted">保存时随表单一起提交并自动填入下方曲目表; '
        "没识别出曲目时曲目表保持原样(表单里其它字段照常保存)。</p>"
        "</div>"
        "</details>"
    )
    # **不补空行**: 有曲目就按实际数量渲染(以前 rows_for(...,3) 会补到 3 行,
    # 2 首曲目会多出 3 个空行, 用户看到"大量空行")。空碟才给 1 行占位。
    _track_values = list(values.get("tracks") or [])
    track_rows = _track_values if _track_values else [{}]
    # 曲目区: **按碟分组**(照实体唱片的分碟习惯)。
    # 每碟一个 block, 自带表头/操作/增删; 解析仍按 track_* 字段名对齐。
    _disc_groups: dict[int, list[dict[str, Any]]] = {}
    for _row in track_rows:
        _disc_no = int(_row.get("disc") or 1)
        _disc_groups.setdefault(_disc_no, []).append(_row)
    if not _disc_groups:
        _disc_groups = {1: []}
    disc_blocks: list[str] = []
    for _disc_no in sorted(_disc_groups):
        _rows_html = []
        for _pos, _row in enumerate(_disc_groups[_disc_no], 1):
            _rows_html.append(_track_row_html(_row, _disc_no, _pos))
        disc_blocks.append(
            '<section class="disc-block" data-disc="{}">'
            '<div class="disc-head">'
            '<span class="disc-title">Disc {}</span>'
            '<span class="disc-tools">'
            '<button type="button" class="btn-ghost btn-xs" data-disc-renumber="1"'
            ' title="按当前顺序重排音轨号">调整曲轨符</button>'
            '<button type="button" class="btn-ghost btn-xs" data-disc-remove="1"'
            ' title="删除整张碟">删除光盘</button>'
            "</span>"
            "</div>"
            '<div class="track-table-wrap">'
            '<table class="track-table"><thead><tr>'
            "<th>排序</th><th>音轨</th><th>歌曲名</th><th>副标题</th>"
            "<th>翻译歌名</th><th>艺人</th><th>语种</th><th>曲风</th>"
            "<th>时长</th><th>操作</th>"
            "</tr></thead>"
            '<tbody data-disc-body="{}">{}</tbody>'
            "</table>"
            "</div>"
            '<div class="disc-empty"{}>暂无数据</div>'
            '<div class="disc-add"><button type="button" class="btn-ghost btn-xs"'
            ' data-track-add="1">＋ 添加专辑歌曲</button></div>'
            "</section>".format(
                _disc_no, "{:02d}".format(_disc_no), _disc_no,
                "".join(_rows_html),
                "" if _rows_html else "",
            )
        )
    tracks_html = "".join(disc_blocks)
    tracks_add_disc_html = (
        '<div class="disc-add-row">'
        '<button type="button" class="btn-ghost btn-xs" data-disc-add="1">'
        "＋ 添加光盘</button>"
        '<span class="field-hint">如果专辑分为多张光盘，可以点击添加光盘</span>'
        "</div>"
    )

    def _bind(name: str, label: str, option_kind: str, *,
              hint: str = "回车分隔多个", suggest_all: bool = False) -> str:
        """与新建页一致的绑定输入(多值 + 有输入才出候选 + 带头像)."""
        current = str(values.get(name) or "")
        options = "".join(
            '<button type="button" class="suggest-item" data-value="{v}"'
            ' data-kind="{k}" hidden>'
            '<span class="suggest-avatar">{letter}</span>{v}<em>#{i}</em></button>'.format(
                v=esc(item.get("name") or ""), k=esc(option_kind),
                letter=esc((str(item.get("name") or "")[:1]) or "?"),
                i=esc(item.get("id") or ""))
            for item in (bind_options.get(option_kind) or ())
        )
        return (
            f'<label class="field auth-field bind-field" data-bind="{esc(option_kind)}"'
            f'{"" if not suggest_all else " data-suggest-all=\"1\""}'
            ">"
            f'<span>{esc(label)}<span class="field-hint">{esc(hint)}</span></span>'
            '<div class="bind-wrap">'
            f'<div class="bind-chips" data-name="{esc(name)}"></div>'
            f'<input type="text" class="bind-input" data-name="{esc(name)}"'
            f' value="{esc(current)}" autocomplete="off">'
            f'<input type="hidden" name="{esc(name)}" value="{esc(current)}">'
            f'<div class="suggest-list">{options}</div>'
            "</div></label>"
        )

    # 封面缩略图(有封面就显示, 没有给个占位)
    _cover_src = (f'/cover/{quote_segment(key)}' if album.get("has_cover")
                  else "")
    if album.get("cover_local") or album.get("has_cover"):
        _cover_url = esc(cover_src(key, album))
        # 编辑页的封面预览要**每次都拉新图**: 上传后立刻刷新时,
        # 浏览器可能拿缓存里的旧封面(响应是长缓存)。
        _ver = str(album.get("cover_v") or "")
        _bust = "?v={}".format(_ver) if _ver else "?t=edit"
        if _cover_url and "?" not in _cover_url:
            _cover_url = _cover_url + _bust
        cover_thumb_html = (
            f'<img class="edit-cover-img" loading="lazy"'
            f' src="{_cover_url}" alt="封面" data-zoom-src="{_cover_url}">'
            '<button type="button" class="edit-cover-zoom" data-cover-zoom="1">'
            '放大</button>'
        )
    else:
        cover_thumb_html = (
            '<div class="edit-cover-img is-empty" aria-hidden="true">无封面</div>')
    # 多图缩略图条由 public.js 的 setupCoverGallery() 动态构建
    # (当前封面 + 本次选中的多张), 服务端不需要提供文件列表。

    # 编辑界面: **固定网格布局**(不拖动、不切大小)。
    #   ┌──────────────┬──────────────┐
    #   │ 封面 & 标题   │ 出品信息      │  ← 出品信息纵向跨两行
    #   ├──────────────┤              │
    #   │ 分类          │              │
    #   ├──────────────┴──────────────┤
    #   │ 简介(长卡)                    │
    #   ├──────────────┬──────────────┤
    #   │ 制作人员      │ 外链          │  ← 同一行等高
    #   └──────────────┴──────────────┘
    def _card(name: str, inner: str, title: str = "",
              extra_class: str = "") -> str:
        cls = "edit-card" + ((" " + extra_class) if extra_class else "")
        return (
            f'<section class="{cls}" data-card="{name}">'
            f'<div class="edit-card-head">'
            f'<h2 class="section-title">{title}</h2>'
            f'</div><div class="edit-card-body">{inner}</div></section>'
        )

    # 名称 -> entity id(让制作人员 chip 锁定 **ArtistId**):
    # 重名靠 id 区分; 同一人的多个艺名/马甲各自是一个 id, 互不覆盖。
    bind_ids: dict[str, dict[str, int]] = {}
    for _kind, _items in (bind_options or {}).items():
        _mapping: dict[str, int] = {}
        for _item in (_items or ()):
            _nm = str((_item or {}).get("name") or "").strip()
            try:
                _id = int((_item or {}).get("id") or 0)
            except (TypeError, ValueError):
                _id = 0
            if _nm and _id > 0:
                _mapping[_nm] = _id
        bind_ids[str(_kind)] = _mapping

    # 封面区: 编辑模式走异步上传(需要已有 key); 新建模式随主表单一起提交
    if creating:
        # 新建模式: 封面随主表单一起提交, 不能走异步上传(还没有 key),
        # 也就不能进裁切弹窗。所以这里给一个**只选图 + 本地预览**的按钮,
        # 选区由 public.js 的 setupCoverPick 接管(不拦截 change)。
        _cover_inner = (
            '<div class="edit-cover-row">'
            '<div class="edit-cover-thumb">'
            '<div class="cover cover-fallback edit-cover-img">'
            '<span>新建</span>'
            '<img class="cover-preview-img" alt="" hidden>'
            '</div></div>'
            '<div class="cover-form">'
            '<input type="file" name="cover" id="cover-input"'
            ' accept="image/png,image/jpeg,image/webp" class="cover-file">'
            # infobox 里带了图片地址时, 前端把地址放这儿 —— 新建时还没有 key,
            # 没法直接调 /cover-url, 由服务端建完专辑后兜底抓一次。
            '<input type="hidden" name="cover_url_hint" id="cover-url-hint">'
            # 草稿要能记住"选过哪张图"(用户 m12280): file input 的值既存不进
            # localStorage, 也恢复不回来。所以选完图额外把 data URL 放这儿 ——
            # 带 name 是有意的, 它会随表单一起提交, 服务端在没有 cover 文件
            # 时用它兜底落盘。于是刷过页面之后封面不但看得见, 也还提交得上。
            '<input type="hidden" name="cover_data" id="cover-data">'
            '<p class="cover-picked" id="cover-picked"></p>'
            '<button type="button" class="btn-ghost btn-xs cover-upload-btn"'
            ' data-cover-pick="1">上传封面</button>'
            '<p class="field-hint">PNG / JPEG / WebP，≤2MB；'
            '保存后随专辑一起创建</p>'
            '</div></div>'
        )
    else:
        _cover_inner = (
            f'<div class="edit-cover-row">'
            f'<div class="edit-cover-thumb">{cover_thumb_html}</div>'
            # 多图槽位: 第 1 格固定是**当前封面**, 后面是"加号"和其它图
            f'<div class="cover-slots" data-cover-slots="1">'
            f'<button type="button" class="cover-slot cover-slot-add"'
            f' data-cover-add="1" title="添加图片">＋</button>'
            f'</div>'
            f'<div id="cover-form" class="cover-form"'
            f' data-cover-action="/album/{esc(quote_segment(key))}/cover"'
            f' data-cover-next="/album/{esc(quote_segment(key))}/edit"'
            # 封面地址由服务端直接给: 前端不必从 next 反推(反推错就图裂)
            f' data-cover-src="/cover/{esc(quote_segment(key))}"'
            # 附图(其他照片)的三个接口: 上传 / 删除 / 改备注
            f' data-photo-action="/album/{esc(quote_segment(key))}/photo"'
            f' data-photo-del-action="/album/{esc(quote_segment(key))}/photo/del"'
            f' data-photo-note-action="/album/{esc(quote_segment(key))}/photo/note"'
            f' data-cover-token="{esc(ctx.csrf_token)}">'
            # 抓取封面用: 前端优先直接 POST /cover-url(见 infobox.js),
            # 这个隐藏域是兜底(比如脚本失败时随主表单提交)。
            f'<input type="hidden" name="cover_url_hint" id="cover-url-hint">'
            f'<input type="file" name="cover" id="cover-input"'
            f' accept="image/png,image/jpeg,image/webp" class="cover-file">'
            f'<p class="cover-picked" id="cover-picked"></p>'
            f'<button type="button" class="btn-ghost btn-xs cover-upload-btn"'
            f' data-cover-upload="1">上传封面</button>'
            f'<p class="field-hint">PNG / JPEG / WebP，≤2MB</p>'
            f'</div></div>'
            # **其他照片**: 不含封面, 可加备注
            + _photos_html(key, ctx, album)
        )
    _cards: dict[str, str] = {}
    _cards["cover"] = _card(
        "cover",
        _cover_inner
        + '<div class="form-grid">'
        + _text_input(values, errs, "title_original", "标题(原题)", required=True)
        + _text_input(values, errs, "title", "标题(译名/显示名)", hint="留空则用原题")
        + _text_input(values, errs, "title_romaji", "罗马字")
        + "</div>",
        "封面 & 标题",
        "edit-card-cover",
    )
    _cards["info"] = _card(
        "info",
        '<div class="form-grid">'
        + _bind("circle", "社团/制作人", "circle")
        + _text_input(values, errs, "company", "发行公司", hint="留空则同上")
        + _bind("event", "展会", "event", suggest_all=True)
        + _text_input(values, errs, "release_date", "发售日",
                      kind="date", hint="选展会后自动填入")
        + _text_input(values, errs, "catalog_no", "品番")
        + _text_input(values, errs, "price", "价格")
        + _text_input(values, errs, "hall", "展馆")
        + _text_input(values, errs, "booth", "展位")
        + "</div>",
        "出品信息",
        "edit-card-info",
    )
    _cards["category"] = _card(
        "category",
        '<div class="form-grid">'
        + _bind("tags", "标签", "tag")
        + _text_input(values, errs, "genres", "曲风", hint="用、或逗号分隔")
        + "</div>",
        "分类",
        "edit-card-category",
    )
    _cards["description"] = _card(
        "description",
        _textarea(values, errs, "description", "简介", hint="空行分段"),
        i18n.translate("section.description", ctx.lang),
        "edit-card-description",
    )
    # 制作人员: **上方的按钮区是人员的唯一列表**
    #   * 按钮 = album.artists ∪ 所有词条里的名字(去重)
    #   * 词条行只有「职责」+「人员选择」, 人员从上方按钮里点选
    #   * 选中的人若还不在上方, JS 会自动补一个按钮
    #   * 保存: artists = 全部按钮; credit_role[i] / credit_name[i] = 每行
    _credit_split = re.compile(r"[、,，/]")
    _all_people: list[str] = []
    for _name in [str(a).strip() for a in (album.get("artists") or [])
                  if str(a).strip()]:
        if _name not in _all_people:
            _all_people.append(_name)
    for _row in credit_rows:
        for _part in _credit_split.split(str(_row.get("name") or "")):
            _one = _part.strip()
            if _one and _one not in _all_people:
                _all_people.append(_one)

    def _people_chip(name: str, role: str = "") -> str:
        """**只读**人员按钮: 显示「职责 · 人名」(与详情页一致).

        用户要求(m11474): 上方只显示下方词条添加后的**效果**, 不是输入区。
        所以这里不带 × 删除按钮, 也不参与添加 —— 编辑入口只有下方词条行。
        """
        _eid = 0
        try:
            _eid = int((bind_ids.get("artist") or {}).get(name) or 0)
        except (TypeError, ValueError):
            _eid = 0
        _role_text = str(role or "").strip()
        _kind = _role_text if _role_text else "艺人"
        return (
            '<span class="person-chip is-static" data-person="{name}"'
            ' data-role="{role}" data-entity-id="{eid}">'
            '<span class="person-kind">{kind}</span>'
            '<b>{name}</b></span>'.format(
                name=esc(name), eid=_eid, role=esc(_role_text),
                kind=esc(_kind)))

    # 上方按钮 = 艺人 + 每条词条的「职责 · 人名」(与详情页**同源规则**)
    #   * circle 类词条跳过 —— 社团已在标题后显示, 详情页也这么做
    _chip_circle_tokens = {"circle", "社团", "社團", "サークル"}
    _chip_specs: list[tuple[str, str]] = []
    for _name in [str(a).strip() for a in (album.get("artists") or [])
                  if str(a).strip()]:
        _chip_specs.append((_name, ""))
    for _row in credit_rows:
        _role_text = str(_row.get("role") or "").strip()
        if (_role_text.lower() in _chip_circle_tokens
                or _role_text in _chip_circle_tokens):
            continue        # 社团不进制作人员按钮(与详情页一致)
        for _part in _credit_split.split(str(_row.get("name") or "")):
            _one = _part.strip()
            if not _one:
                continue
            # 同一人不重复(优先保留已有职责)
            if any(_one == _a and not _r for _a, _r in _chip_specs):
                _chip_specs = [(_a, _role_text if _a == _one and not _r else _r)
                               for _a, _r in _chip_specs]
                continue
            if any(_one == _a for _a, _r in _chip_specs):
                continue
            _chip_specs.append((_one, _role_text))
    _people_chips = "".join(_people_chip(_n, _r) for _n, _r in _chip_specs)

    # 词条行: 职责输入 + 人员选择按钮(点开从上方按钮里选)
    def _credit_row(row: Mapping[str, Any]) -> str:
        """一条词条: 职责(文本框) + 人员(文本框, **输入回车加入*).

        人员不再依赖点选: 直接敲名字回车即可; 输入时仍会给库内艺人候选。
        """
        _role = str(row.get("role") or "")
        _names = [p.strip() for p in _credit_split.split(
            str(row.get("name") or "")) if p.strip()]
        _label = "、".join(_names)
        _chips = "".join(
            '<span class="credit-person-chip" data-name="{}">{}'
            '<button type="button" class="credit-person-x"'
            ' data-credit-person-del="1" aria-label="移除 {}">×</button>'
            "</span>".format(esc(_one), esc(_one), esc(_one))
            for _one in _names)
        _row_raw = str(_row.get("raw_role") or "")
        # 一行只有 职责 / 人员 / 删除 —— 与「＋ 添加词条」新建的行完全一致
        # (以前这里多渲染了「原名」「别名」两列, 新加的行却没有, 同一张卡里
        #  两种长相, 看着像坏了)
        return (
            '<div class="pair-row credit-row" data-pair-row="credit">'
            '<label class="field auth-field"><span>职责</span>'
            '<input type="text" name="credit_role" value="{}"'
            ' placeholder="如 编曲 / 演唱"></label>'
            '<div class="field auth-field credit-people-field">'
            '<span>人员<span class="field-hint">输入后回车加入</span></span>'
            '<div class="credit-people-box">'
            '<div class="credit-people-chips">{}</div>'
            '<input type="text" class="credit-person-input"'
            ' placeholder="输入艺人名后回车" autocomplete="off">'
            "</div>"
            '<input type="hidden" name="credit_name" value="{}">'
            "</div>"
            '<input type="hidden" name="credit_raw_role" value="{}">'
            '<button type="button" class="pair-del" data-pair-del="credit"'
            ' title="删除这一条" aria-label="删除这一条">✕</button>'
            "</div>".format(
                esc(_role), _chips, esc(_label), esc(_row_raw))
        )

    _credit_rows = "".join(_credit_row(_row) for _row in credit_rows)
    _artist_options = "".join(
        '<button type="button" class="person-suggest" data-name="{name}"'
        ' data-entity-id="{eid}">{name}<em>#{eid}</em></button>'.format(
            name=esc(item.get("name") or ""),
            eid=esc(item.get("id") or ""))
        for item in (bind_options.get("artist") or ())
    )
    # 上方: **只读汇总**(下方词条合并出来的效果), 没有任何添加入口。
    # 用户要求(m11474): 「上面的部分应该只显示下方词条添加后的效果而不是
    # 作为具有添加功能的一部分」。所以这里既没有搜索框, 也没有 × 删除。
    #   汇总规则与详情页同源: 艺人打头(职责空 -> 显示「艺人」), 词条补职责。
    _base_people = "\n".join(_all_people)
    _cards["credits"] = _card(
        "credits",
        '<p class="field-hint summary-hint">下方词条合并后的结果（只读）</p>'
        # data-people 必须**最后**一个属性: 多个测试用
        # 'data-people="artists">' 这段字面量来截取 chip 区。
        + '<div class="people-chips is-summary" data-artists="{}"'
          ' data-people="artists">'.format(esc(_base_people))
        + _people_chips
        + "</div>"
        + '<input type="hidden" name="artists" value="{}">'.format(
            esc(_base_people))
        # 艺人候选池: 只给下方词条行的输入框做联想, 界面上看不见。
        + '<div class="people-suggest-list" hidden>'
        + _artist_options + "</div>"
        + '<h3 class="edit-sub-title">词条</h3>'
        + '<div class="rows">' + _credit_rows + "</div>"
        + credits_add_html,
        "制作人员",
        "edit-card-credits",
    )
    _cards["links"] = _card(
        "links",
        f'<div class="rows">{urls_html}</div>{urls_add_html}',
        i18n.translate("section.links", ctx.lang),
        "edit-card-links",
    )
    # 固定顺序(不可拖动): 版面按 CSS grid-area 排, 这里按阅读顺序输出
    _cards_html = (_cards["cover"] + _cards["info"] + _cards["category"]
                   + _cards["description"] + _cards["credits"]
                   + _cards["links"])

    # 新建时没有"删除这张专辑"按钮, 也没有删除表单
    if creating:
        _delete_btn = ""
        _delete_form = ""
    else:
        _delete_btn = (
            '<!-- 删除按钮: 表单在**主表单之外**(HTML 不允许嵌套 form,'
            ' 浏览器会丢弃内层表单, 导致点了没反应); 这里用 form 属性关联。'
            ' 确认弹窗由 public.js 的 setupConfirm 处理'
            '(不能写内联 onclick, CSP 会拦)。 -->'
            '<button type="submit" class="btn-danger"'
            ' form="delete-album-form">删除这张专辑</button>'
        )
        _delete_form = (
            '<!-- 删除表单: 必须在主表单之外(HTML 不允许嵌套 form) -->'
            '<form id="delete-album-form" method="post"'
            ' action="/album/{}/delete"'
            ' data-confirm="确定删除这张专辑？此操作不可恢复。">'
            '<input type="hidden" name="csrf_token" value="{}">'
            '</form>'
        ).format(esc(quote_segment(key)), esc(ctx.csrf_token))

    if creating:
        _intro = ('<p class="section-note">新建专辑: 填好后点保存, '
                  '会创建一张新专辑并进入它的编辑页。</p>')
    else:
        _intro = ('<p class="section-note">正在编辑: '
                  '<a href="/album/{0}">{1}</a>'
                  ' <span class="muted">· 键 {2}</span></p>').format(
                      esc(quote_segment(key)), esc(album.get("title") or key),
                      key_html)
    _infobox_html = _infobox_widget(album)
    body = f"""{_intro}
    {notice_html}{summary}
    <form class="edit-form" method="post" action="{esc(_form_action)}" enctype="multipart/form-data" data-entity-form="album">
    <input type="hidden" name="csrf_token" value="{esc(ctx.csrf_token)}">{_key_field}
    <div class="edit-draft-bar" data-draft-bar="1">
  <span class="draft-status">编辑内容会自动存草稿</span>
  <button type="button" class="btn-ghost btn-xs" data-draft-save="1">保存草稿</button>
  <button type="button" class="btn-ghost btn-xs" data-draft-clear="1">清除草稿</button>
</div>
<div class="edit-cards" id="edit-cards">
    {_cards_html}
    </div>
    <section class="edit-card edit-tracks-card" data-card="tracks">
      <div class="edit-card-head">
        <h2 class="section-title">曲目表<span class="section-sub">留空的行会被忽略</span></h2>
      </div>
      <div class="edit-card-body">
      {paste_html}
      <div class="rows" id="track-rows">{tracks_html}</div>
      {tracks_add_disc_html}
      </div>
    </section>
    <div class="field-actions edit-actions">
      <button type="submit">{i18n.translate("btn.save", ctx.lang)}</button>
      <a class="btn-ghost" href="{esc(_cancel_href)}">{i18n.translate("btn.cancel", ctx.lang)}</a>
      {_delete_btn}
    </div>
    <div class="field-actions edit-actions" hidden>
    </div>
    </form>{_delete_form}{_infobox_html}"""
    if creating:
        _crumbs = [("浏览", "/albums"), ("编辑", "/edit"), ("新建专辑", "")]
    else:
        _crumbs = [("浏览", "/albums"),
                   (str(album.get("title") or key),
                    "/album/{}".format(quote_segment(key))),
                   ("编辑", "")]
    # 标题行动作: 「新建专辑」后面紧跟「编辑 infobox」
    # (显式传入后 layout 不再自动补, 所以「新建专辑」要自己带上)
    _title_actions = entity_new_buttons(ctx, ("album",))
    if ctx.is_editor:
        _title_actions += _infobox_button()
    return layout(
        ctx, title=_page_title, body=body, active="albums", wide=True,
        breadcrumbs=_crumbs, title_actions=_title_actions,
    )


def error_page(ctx: SiteContext, *, status: int, message: str) -> str:
    extra = ""
    if status in (401, 403):
        extra = (
            '<p class="error-extra">需要登录或更高的权限才能做这件事。</p>'
            '<p><a class="btn" href="/login">登录</a> <a class="btn-ghost" href="/register">注册</a></p>'
        )
    body = (
        '<div class="empty empty-error">'
        f'<p class="empty-title">{esc(status)}</p><p>{esc(message)}</p>'
        f'{extra}'
        '<p class="error-actions">'
        '<button type="button" class="btn" data-go-back="1">‹ 回到上一页</button>'
        '<a class="btn-ghost" href="/">回到首页</a>'
        "</p>"
        "</div>"
    )
    return layout(ctx, title=f"{status}", body=body)


#: 供调用方复用的公开符号
__all__ = [
    "SITE_NAME",
    "forgot_page",
    "new_entity_page",
    "edit_profile_page",
    "entity_new_buttons",
    "artists_index_page",
    "artist_detail_page",
    "circle_detail_page",
    "country_flag",
    "reset_page",
    "history_page",
    "SITE_TAGLINE",
    "identity_badge",
    "ROLE_LABELS",
    "ROLE_ORDER",
    "NEEDS_LABELS",
    "EVENT_KIND_LABELS",
    "SiteContext",
    "esc",
    "safe_url",
    "quote_segment",
    "duration_text",
    "total_duration_text",
    "format_date",
    "role_label",
    "event_kind_label",
    "placeholder_text",
    "percentage",
    "build_url",
    "make_url_for",
    "layout",
    "album_card",
    "album_grid",
    "suggestions_html",
    "badges_for",
    "cover_html",
    "pagination",
    "active_filter_chips",
    "filter_panel",
    "album_results",
    "home_page",
    "albums_page",
    "album_detail_page",
    "circle_page",
    "event_page",
    "tag_page",
    "circles_index_page",
    "events_index_page",
    "tags_index_page",
    "stats_page",
    "inbox_page",
    "about_page",
    "auth_strip",
    "auth_field",
    "captcha_block",
    "login_page",
    "register_page",
    "account_page",
    "messages_page",
    "public_profile_page",
    "notifications_page",
    "message_thread_page",
    "render_bio",
    "verify_page",
    "review_page",
    "users_admin_page",
    "edit_values_from_album",
    "edit_album_page",
    "error_page",
]


def _profile_card(
    ctx: SiteContext,
    *,
    title: str,
    kind: str,
    image: str = "",
    flag: str = "",
    followers: int = 0,
    following: bool = False,
    next_url: str = "",
    fields: Sequence[tuple[str, str]] = (),
    aliases: Sequence[str] = (),
    links: Sequence[Mapping[str, Any]] = (),
    subtitle: str = "",
) -> str:
    """左栏资料卡(艺术家/社团共用): 头像/LOGO + 名字 + 关注数 + 字段."""
    if image:
        avatar = (f'<img class="profile-avatar" src="{esc(image)}"'
                  ' alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer">')
    else:
        avatar = (f'<span class="profile-avatar profile-avatar-text" aria-hidden="true">'
                  f'{esc(title[:1] or "?")}</span>')
    rows = "".join(
        f'<div class="profile-row"><dt>{esc(label)}</dt><dd>{esc(value)}</dd></div>'
        for label, value in fields if value
    )
    alias_html = ""
    if aliases:
        alias_html = (
            f'<div class="profile-row"><dt>{i18n.translate("field.aliases", ctx.lang)}</dt><dd>'
            + "、".join(esc(a) for a in aliases) + "</dd></div>"
        )
    link_html = ""
    if links:
        link_html = (
            '<div class="profile-links">'
            + "".join(
                f'<a class="link-pill" href="{esc(safe_url(link.get("href")))}"'
                ' rel="nofollow noopener">'
                f'{esc(link.get("text") or "链接")}</a>'
                for link in links if safe_url(link.get("href")))
            + "</div>"
        )
    follow_form = (
        '<form class="profile-follow" method="post" action="/follows/toggle">'
        + _hidden("csrf_token", ctx.csrf_token)
        + _hidden("kind", kind) + _hidden("key", title)
        + _hidden("next", next_url)
        + f'<button type="submit" class="btn{" is-following" if following else ""}">'
        + ("已关注" if following else "关注") + "</button></form>"
    )
    return (
        '<aside class="profile-card">'
        f'<div class="profile-avatar-wrap">{avatar}</div>'
        f'<h1 class="profile-name">{esc(title)}'
        + (f'<span class="profile-flag" title="国籍">{flag}</span>' if flag else "")
        + "</h1>"
        + (f'<p class="profile-subtitle">{esc(subtitle)}</p>' if subtitle else "")
        + f'<p class="profile-followers">关注者 <b>{followers}</b></p>'
        + follow_form
        + '<dl class="profile-rows">' + rows + alias_html + "</dl>"
        + link_html
        + "</aside>"
    )


def artists_index_page(
    ctx: SiteContext,
    *,
    artists: Sequence[Mapping[str, Any]] = (),
    pager: Mapping[str, Any] | None = None,
    query: str = "",
) -> str:
    """艺术家列表: 卡片(首字母头像 + 名字 + 专辑数 + 关注数)."""
    pager = pager or {}
    cards = "".join(
        '<a class="artist-card" href="/artist/{enc}">'
        '<span class="artist-avatar" aria-hidden="true">{letter}</span>'
        '<span class="artist-name">{name}</span>'
        '<span class="artist-meta">{albums} 张专辑 · {followers} 关注</span>'
        "</a>".format(
            enc=esc(quote_segment(item.get("name") or "")),
            letter=esc((str(item.get("name") or "")[:1]) or "?"),
            name=esc(item.get("name") or ""),
            albums=esc(item.get("album_count") or 0),
            followers=esc(item.get("followers") or 0),
        )
        for item in artists
    )
    pager_html = ""
    page_no = int(pager.get("page") or 1)
    pages = int(pager.get("pages") or 1)
    if pages > 1:
        query_part = "&q=" + esc(quote_segment(query)) if query else ""
        links = []
        if page_no > 1:
            links.append(f'<a class="page-link" href="/artists?page={page_no - 1}'
                         f'{query_part}">上一页</a>')
        links.append(f'<span class="page-current">{page_no} / {pages}</span>')
        if page_no < pages:
            links.append(f'<a class="page-link" href="/artists?page={page_no + 1}'
                         f'{query_part}">下一页</a>')
        pager_html = '<nav class="pager">' + "".join(links) + "</nav>"
    form = (
        '<form class="searchbar" action="/artists" method="get" role="search">'
        f'<input type="search" name="q" value="{esc(query)}" placeholder="搜索艺人名字">'
        '<button type="submit">搜索</button></form>'
    )
    body = (
        f'<div class="list-head">{form}'
        f'<p class="section-note">共 {int(pager.get("total") or len(artists))} 位艺术家</p></div>'
        + (f'<div class="artist-grid">{cards}</div>' if cards
           else '<p class="section-note">没有找到艺术家。</p>')
        + pager_html
    )
    return layout(ctx, title="艺术家", body=body, active="artists",
                  subtitle="从专辑与制作人员聚合出的艺人索引")


def artist_detail_page(
    ctx: SiteContext,
    *,
    artist: Mapping[str, Any],
    following: bool = False,
    followers: int = 0,
    comments: Sequence[Mapping[str, Any]] = (),
    comment_likes: Mapping[Any, int] | None = None,
    liked_ids: Sequence[Any] = (),
) -> str:
    """艺术家详情: 左资料卡 / 右简介·相关社团·参与专辑·人际关系 / 底部评论."""
    name = str(artist.get("name") or "")
    flag = country_flag(artist.get("nationality"))
    occupations = list(artist.get("occupations_text") or artist.get("occupations") or [])
    occupations_text = "、".join(str(o) for o in occupations)
    fields: list[tuple[str, str]] = [
        ("本名", str(artist.get("real_name") or "")),
        ("本名(译)", str(artist.get("real_name_en") or "")),
        ("艺名(英)", str(artist.get("name_en") or "")),
        ("艺名(日)", str(artist.get("name_ja") or "")),
        ("性别", str(artist.get("gender") or "")),
        ("生日", str(artist.get("birthday") or "")),
        ("职业", occupations_text),
        ("国籍", flag and f"{flag} {str(artist.get('nationality') or '').upper()}"),
    ]
    card = _profile_card(
        ctx, title=name, kind="artist", image=str(artist.get("avatar") or ""),
        flag=flag, followers=int(followers or 0), following=following,
        next_url="/artist/" + quote_segment(name), fields=fields,
        aliases=[str(a) for a in artist.get("aliases") or []],
        links=list(artist.get("links") or []),
        subtitle=f"{artist.get('album_count') or len(artist.get('albums') or [])} 张参与专辑",
    )
    bio = str(artist.get("bio") or "")
    bio_html = ('<div class="description"><p>' + esc(bio) + "</p></div>" if bio
               else f'<p class="section-note">{i18n.translate("detail.no_description", ctx.lang)}</p>')
    circles = list(artist.get("circles") or [])
    circles_html = ("".join(
        f'<a class="tag" href="/circle/{esc(quote_segment(c))}">{esc(c)}</a>'
        for c in circles
    ) or f'<p class="section-note">{i18n.translate("common.none", ctx.lang)}</p>')
    albums = list(artist.get("albums") or [])
    albums_html = album_grid(albums[:12]) if albums else f'<p class="section-note">{i18n.translate("detail.no_albums", ctx.lang)}</p>'
    relations = list(artist.get("relations") or [])[:24]
    relations_html = ("".join(
        f'<a class="tag" href="/artist/{esc(quote_segment(r))}">{esc(r)}</a>'
        for r in relations
    ) or f'<p class="section-note">{i18n.translate("common.none", ctx.lang)}</p>')
    right = (
        f'<section class="panel"><h2 class="section-title">{i18n.translate("section.description", ctx.lang)}</h2>' + bio_html + "</section>"
        f'<section class="panel"><h2 class="section-title">{i18n.translate("section.circles", ctx.lang)}</h2>'
        f'<div class="tagrow tagrow-wrap">{circles_html}</div></section>'
        '<section class="panel"><h2 class="section-title">参与专辑</h2>'
        + albums_html + "</section>"
        f'<section class="panel"><h2 class="section-title">{i18n.translate("section.relations", ctx.lang)}</h2>'
        f'<div class="tagrow tagrow-wrap">{relations_html}</div></section>'
    )
    comments_html = comments_section(
        ctx, album={"key": name}, comments=comments,
        comment_likes=comment_likes, liked_ids=liked_ids,
        comment_action="/artist/" + quote_segment(name) + "/comment",
        next_url="/artist/" + quote_segment(name),
    )
    body = (f'<div class="profile-layout">{card}'
            f'<div class="profile-main">{right}</div></div>{comments_html}')
    _title_actions = ""
    if ctx.is_editor:
        _title_actions = (
            f'<a class="btn-ghost btn-xs" href="/new/artist">{i18n.translate("new.artist", ctx.lang)}</a>'
            + f'<a class="btn-ghost btn-xs" href="/artist/'
            + quote_segment(name) + f'/edit">{i18n.translate("edit.profile", ctx.lang)}</a>'
        )
    return layout(ctx, title=name, body=body, active="artists", wide=True,
                  title_actions=_title_actions,
                  breadcrumbs=[("艺术家", "/artists"), (name, "")])


def circle_detail_page(
    ctx: SiteContext,
    *,
    circle: Mapping[str, Any],
    following: bool = False,
    followers: int = 0,
    comments: Sequence[Mapping[str, Any]] = (),
    comment_likes: Mapping[Any, int] | None = None,
    liked_ids: Sequence[Any] = (),
) -> str:
    """社团详情: 左资料卡 / 右简介·相关人士(成员/过往/客串)·专辑 / 底部评论."""
    name = str(circle.get("name") or "")
    flag = country_flag(circle.get("nationality"))
    status_bits = []
    if circle.get("activity_type"):
        status_bits.append(str(circle["activity_type"]))
    if circle.get("latest_year"):
        status_bits.append(f"最近活动 {circle['latest_year']}")
    status = " · ".join(status_bits)
    fields: list[tuple[str, str]] = [
        ("活动状态", status),
        ("首次活动", str(circle.get("first_event") or circle.get("first_date") or "")),
        ("末次活动", str(circle.get("last_event") or circle.get("last_date") or "")),
        ("成立", str(circle.get("founded") or "")),
        ("国籍", flag and f"{flag} {str(circle.get('nationality') or '').upper()}"),
    ]
    card = _profile_card(
        ctx, title=name, kind="circle", image=str(circle.get("logo") or ""),
        flag=flag, followers=int(followers or 0), following=following,
        next_url="/circle/" + quote_segment(name), fields=fields,
        links=list(circle.get("links") or []),
        subtitle=f"{len(circle.get('albums') or [])} 张专辑",
    )
    bio = str(circle.get("description") or "")
    bio_html = ('<div class="description"><p>' + esc(bio) + "</p></div>" if bio
               else f'<p class="section-note">{i18n.translate("detail.no_description", ctx.lang)}</p>')

    def people_block(title: str, people: Sequence[Mapping[str, Any]]) -> str:
        if not people:
            return ""
        chips = "".join(
            '<a class="tag" href="/artist/{enc}" title="{roles}">{name}</a>'.format(
                enc=esc(quote_segment(p.get("name") or "")),
                roles=esc("、".join(p.get("roles") or [])),
                name=esc(p.get("name") or ""),
            )
            for p in people
        )
        return (f'<section class="panel"><h2 class="section-title">{title}</h2>'
                f'<div class="tagrow tagrow-wrap">{chips}</div></section>')

    albums = list(circle.get("albums") or [])
    albums_html = album_grid(albums[:24]) if albums else f'<p class="section-note">{i18n.translate("detail.no_albums", ctx.lang)}</p>'
    right = (
        f'<section class="panel"><h2 class="section-title">{i18n.translate("section.description", ctx.lang)}</h2>' + bio_html + "</section>"
        + people_block("相关人士", circle.get("members") or [])
        + people_block("过往成员", circle.get("former_members") or [])
        + people_block("客串", circle.get("guests") or [])
        + f'<section class="panel"><h2 class="section-title">{i18n.translate("section.albums", ctx.lang)}</h2>' + albums_html + "</section>"
    )
    comments_html = comments_section(
        ctx, album={"key": name}, comments=comments,
        comment_likes=comment_likes, liked_ids=liked_ids,
        comment_action="/circle/" + quote_segment(name) + "/comment",
        next_url="/circle/" + quote_segment(name),
    )
    body = (f'<div class="profile-layout">{card}'
            f'<div class="profile-main">{right}</div></div>{comments_html}')
    _title_actions = ""
    if ctx.is_editor:
        _title_actions = (
            f'<a class="btn-ghost btn-xs" href="/new/circle">{i18n.translate("new.circle", ctx.lang)}</a>'
            + f'<a class="btn-ghost btn-xs" href="/circle/'
            + quote_segment(name) + f'/edit">{i18n.translate("edit.profile", ctx.lang)}</a>'
        )
    return layout(ctx, title=name, body=body, active="circles", wide=True,
                  title_actions=_title_actions,
                  breadcrumbs=[("社团", "/circles"), (name, "")])