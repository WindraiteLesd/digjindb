"""MD 语法的 wiki 渲染(纯标准库).

用户要求: "允许编辑展会（MD语法的wiki界面，需要支持图片和外链）"

支持:
    # 标题        ## / ###
    **粗体**  *斜体*  `代码`
    [文字](链接)            外链
    ![说明](图片地址)        图片
    无序列表 - / *           有序列表 1.
    空行分段
    行内裸链接自动识别

安全:
    * 所有文本先转义
    * 链接只允许 http(s) 与站内相对路径
    * 图片同样过 safe_url, 拒绝 javascript:/data:
"""

from __future__ import annotations

import re
from typing import Any

_RE_IMG = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")
_RE_LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")
_RE_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_RE_ITALIC = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")
_RE_CODE = re.compile(r"`([^`]+)`")
_RE_BARE_URL = re.compile(r"(?<![(\[\"'])(https?://[^\s<>\"']+)")


def _esc(text: Any) -> str:
    s = str(text if text is not None else "")
    return (s.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _safe_link(url: Any) -> str:
    """只允许 http(s) 与站内路径."""
    text = str(url or "").strip()
    low = text.lower()
    if low.startswith(("http://", "https://")):
        return text
    if text.startswith("/") and not text.startswith("//"):
        return text
    return ""


def _inline(text: str) -> str:
    """行内元素: 图片 -> 外链 -> 粗体 -> 斜体 -> 代码 -> 裸链接."""
    out = _esc(text)

    def _img(m: re.Match[str]) -> str:
        alt, url = m.group(1), m.group(2)
        safe = _safe_link(url)
        if not safe:
            return _esc(alt)
        return ('<img class="md-img" src="{}" alt="{}" loading="lazy">'
                .format(_esc(safe), _esc(alt)))

    # 图片先处理(它的语法是链接的超集)
    out = _RE_IMG.sub(_img, out)

    def _link(m: re.Match[str]) -> str:
        label, url = m.group(1), m.group(2)
        safe = _safe_link(url)
        if not safe:
            return label
        external = ' data-external="1" rel="nofollow noopener"' if \
            safe.lower().startswith(("http://", "https://")) else ""
        return '<a href="{}"{}>{}</a>'.format(_esc(safe), external, label)

    out = _RE_LINK.sub(_link, out)
    out = _RE_BOLD.sub(r"<strong>\1</strong>", out)
    out = _RE_ITALIC.sub(r"<em>\1</em>", out)
    out = _RE_CODE.sub(r"<code>\1</code>", out)

    def _bare(m: re.Match[str]) -> str:
        url = m.group(1)
        return ('<a href="{}" data-external="1" rel="nofollow noopener">'
                "{}</a>").format(_esc(url), url)

    out = _RE_BARE_URL.sub(_bare, out)
    return out


def markdown_to_html(source: Any) -> str:
    """Markdown -> HTML 片段(已转义, 可安全嵌入页面)."""
    text = str(source or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        return ""
    blocks: list[str] = []
    para: list[str] = []
    items: list[str] = []
    ordered = False

    def flush_para() -> None:
        if para:
            blocks.append("<p>{}</p>".format("<br>".join(para)))
            para.clear()

    def flush_list() -> None:
        if items:
            tag = "ol" if ordered else "ul"
            blocks.append("<{0}>{1}</{0}>".format(
                tag, "".join("<li>{}</li>".format(x) for x in items)))
            items.clear()

    for raw_line in text.split("\n"):
        line = raw_line.rstrip()
        stripped = line.strip()

        if not stripped:
            flush_para()
            flush_list()
            continue

        # 标题
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            flush_para()
            flush_list()
            level = min(6, len(m.group(1)) + 1)   # # -> h2(页面里 h1 是标题)
            blocks.append("<h{0}>{1}</h{0}>".format(level, _inline(m.group(2))))
            continue

        # 水平线
        if re.match(r"^(-{3,}|\*{3,}|_{3,})$", stripped):
            flush_para()
            flush_list()
            blocks.append("<hr>")
            continue

        # 引用
        if stripped.startswith(">"):
            flush_para()
            flush_list()
            blocks.append("<blockquote>{}</blockquote>".format(
                _inline(stripped.lstrip("> ").strip())))
            continue

        # 列表
        m = re.match(r"^[-*+]\s+(.*)$", stripped)
        if m:
            flush_para()
            if ordered:
                flush_list()
                ordered = False
            items.append(_inline(m.group(1)))
            continue
        m = re.match(r"^\d+[.)]\s+(.*)$", stripped)
        if m:
            flush_para()
            if not ordered:
                flush_list()
                ordered = True
            items.append(_inline(m.group(1)))
            continue

        # 普通段落
        flush_list()
        para.append(_inline(stripped))

    flush_para()
    flush_list()
    return "\n".join(blocks)
