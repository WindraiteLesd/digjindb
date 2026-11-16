"""极简 HTML DOM 与 CSS 选择器(纯标准库).

各数据源适配器都用它来解析页面. 支持的选择器子集:

* 类型 ``div`` / 通配 ``*`` / 类 ``.cls`` / ID ``#id``
* 属性 ``[href]`` ``[href=...]`` ``[class*=x]`` ``[class^=x]`` ``[class$=x]`` ``[class~=x]``
* 后代 ``a b`` / 子元素 ``a > b`` / 分组 ``a, b``
* 伪类 ``:first-child`` ``:last-child`` ``:nth-child(n)`` ``:empty`` ``:not(...)`` ``:contains("...")``
"""

from __future__ import annotations

import html as html_mod
import re
from html.parser import HTMLParser
from typing import Any, Iterable, Iterator, Sequence

VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}
RAW_TEXT_TAGS = {"script", "style", "textarea", "title"}

# 隐含闭合规则: 遇到 key 开始标签时, 若栈顶在 value 中则先闭合
_AUTO_CLOSE: dict[str, set[str]] = {
    "li": {"li"},
    "dt": {"dt", "dd"},
    "dd": {"dt", "dd"},
    "p": {"p"},
    "tr": {"tr", "td", "th"},
    "td": {"td", "th"},
    "th": {"td", "th"},
    "option": {"option"},
    "thead": {"tbody", "tfoot"},
    "tbody": {"tbody", "tfoot"},
    "tfoot": {"tbody"},
    "h1": {"h1", "h2", "h3", "h4", "h5", "h6"},
    "h2": {"h1", "h2", "h3", "h4", "h5", "h6"},
    "h3": {"h1", "h2", "h3", "h4", "h5", "h6"},
}

_WS_RE = re.compile(r"[ \t\r\n\f\v]+")


class Node:
    """DOM 节点(元素)."""

    __slots__ = ("tag", "attrs", "children", "parent", "text_parts")

    def __init__(self, tag: str = "", attrs: dict[str, str] | None = None, parent: "Node | None" = None) -> None:
        self.tag = (tag or "").lower()
        self.attrs: dict[str, str] = dict(attrs or {})
        self.children: list[Node] = []
        self.parent = parent
        self.text_parts: list[str] = []

    # -- 基础 ---------------------------------------------------------------

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Node {self.tag} attrs={list(self.attrs)[:3]} children={len(self.children)}>"

    def __iter__(self) -> Iterator["Node"]:
        return iter(self.children)

    def get(self, name: str, default: str = "") -> str:
        return self.attrs.get(name.lower(), default)

    def attr(self, name: str, default: str = "") -> str:
        return self.attrs.get(name.lower(), default)

    def has_attr(self, name: str) -> bool:
        return name.lower() in self.attrs

    @property
    def classes(self) -> list[str]:
        return (self.attrs.get("class") or "").split()

    @property
    def id(self) -> str:
        return self.attrs.get("id", "")

    def has_class(self, name: str) -> bool:
        return name in self.classes

    @property
    def text(self) -> str:
        """直接子文本(不含后代元素内文本)."""
        return _WS_RE.sub(" ", "".join(self.text_parts)).strip()

    def text_content(self, *, separator: str = " ", strip: bool = True) -> str:
        """递归取全部文本."""
        parts: list[str] = []
        self._collect_text(parts)
        joined = separator.join(p for p in parts if p)
        return _WS_RE.sub(" ", joined).strip() if strip else joined

    def _collect_text(self, out: list[str]) -> None:
        if self.tag in RAW_TEXT_TAGS:
            out.extend(self.text_parts)
            return
        out.extend(self.text_parts)
        for child in self.children:
            child._collect_text(out)
            if child.tag in ("br", "p", "div", "li", "tr"):
                out.append(" ")

    def stripped_strings(self) -> list[str]:
        text = self.text_content()
        return [t for t in (s.strip() for s in text.split(" ")) if t]

    def descendants(self) -> Iterator["Node"]:
        for child in self.children:
            yield child
            yield from child.descendants()

    def find_all(self, tag: str) -> list["Node"]:
        tag = tag.lower()
        return [n for n in self.descendants() if n.tag == tag]

    def find(self, tag: str) -> "Node | None":
        tag = tag.lower()
        for n in self.descendants():
            if n.tag == tag:
                return n
        return None

    def next_sibling(self) -> "Node | None":
        if self.parent is None:
            return None
        sibs = self.parent.children
        try:
            idx = sibs.index(self)
        except ValueError:
            return None
        return sibs[idx + 1] if idx + 1 < len(sibs) else None

    def siblings(self) -> list["Node"]:
        if self.parent is None:
            return []
        return [n for n in self.parent.children if n is not self]

    @property
    def index_in_parent(self) -> int:
        if self.parent is None:
            return 0
        try:
            return self.parent.children.index(self)
        except ValueError:
            return 0

    # -- 选择器 -------------------------------------------------------------

    def select(self, selector: str) -> list["Node"]:
        return select_nodes(self, selector)

    def select_one(self, selector: str) -> "Node | None":
        found = select_nodes(self, selector, limit=1)
        return found[0] if found else None

    # -- 输出 ---------------------------------------------------------------

    def inner_html(self) -> str:
        return "".join(c.outer_html() if isinstance(c, Node) else "" for c in self.children)

    def outer_html(self) -> str:
        attrs = "".join(f' {k}="{html_mod.escape(v, quote=True)}"' for k, v in self.attrs.items())
        open_tag = f"<{self.tag}{attrs}>"
        if self.tag in VOID_TAGS:
            return open_tag
        inner = html_mod.escape("".join(self.text_parts), quote=False) if self.tag in RAW_TEXT_TAGS else (
            html_mod.escape("".join(self.text_parts), quote=False)
            + "".join(c.outer_html() for c in self.children)
        )
        close = "" if self.tag in VOID_TAGS else f"</{self.tag}>"
        return f"{open_tag}{inner}{close}"


class Document(Node):
    """文档根节点."""

    def __init__(self) -> None:
        super().__init__("#document")
        self.url = ""
        self.title = ""
        self.meta: dict[str, str] = {}
        self._parsed = False

    def select(self, selector: str) -> list[Node]:
        return select_nodes(self, selector)

    def text_content(self, *, separator: str = " ", strip: bool = True) -> str:
        return super().text_content(separator=separator, strip=strip)


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------


class _TreeBuilder(HTMLParser):
    def __init__(self, document: Document) -> None:
        super().__init__(convert_charrefs=True)
        self.doc = document
        self.stack: list[Node] = [document]
        self._raw_tag: str | None = None

    # -- 栈操作 -------------------------------------------------------------

    def _open(self, tag: str, attrs: dict[str, str]) -> Node:
        # 隐含闭合
        closers = _AUTO_CLOSE.get(tag)
        if closers:
            while len(self.stack) > 1 and self.stack[-1].tag in closers:
                self.stack.pop()
        parent = self.stack[-1]
        node = Node(tag, attrs, parent)
        parent.children.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)
        return node

    def _close(self, tag: str) -> None:
        for idx in range(len(self.stack) - 1, 0, -1):
            if self.stack[idx].tag == tag:
                del self.stack[idx:]
                return
        # 未匹配的结束标签直接忽略

    # -- HTMLParser 回调 ----------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attr_map: dict[str, str] = {}
        for k, v in attrs:
            key = (k or "").lower()
            if key and key not in attr_map:
                attr_map[key] = v if v is not None else ""
        self._open(tag, attr_map)
        if tag in RAW_TEXT_TAGS:
            self._raw_tag = tag

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attr_map: dict[str, str] = {}
        for k, v in attrs:
            key = (k or "").lower()
            if key and key not in attr_map:
                attr_map[key] = v if v is not None else ""
        node = Node(tag, attr_map, self.stack[-1])
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._raw_tag == tag:
            self._raw_tag = None
        self._close(tag)

    def handle_data(self, data: str) -> None:
        if not data:
            return
        self.stack[-1].text_parts.append(data)

    def handle_comment(self, data: str) -> None:
        return


def parse_html(source: str | bytes, *, url: str = "") -> Document:
    """把 HTML 字符串解析为 :class:`Document`."""
    if isinstance(source, (bytes, bytearray)):
        source = source.decode("utf-8", "replace")
    doc = Document()
    doc.url = url
    parser = _TreeBuilder(doc)
    try:
        parser.feed(source)
        parser.close()
    except Exception:
        # HTMLParser 极少抛错; 出错时返回已解析的部分
        pass
    # 后处理: title / meta
    title_node = next((n for n in doc.descendants() if n.tag == "title"), None)
    if title_node is not None:
        doc.title = title_node.text_content()
    for node in doc.descendants():
        if node.tag != "meta":
            continue
        name = (node.get("name") or node.get("property") or "").strip().lower()
        content = node.get("content")
        if name and content and name not in doc.meta:
            doc.meta[name] = content
    return doc


# --------------------------------------------------------------------------
# CSS 选择器
# --------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"\s*([>,\s])")


def _split_groups(selector: str) -> list[str]:
    groups: list[str] = []
    depth = 0
    quote = ""
    current: list[str] = []
    for ch in selector:
        if quote:
            current.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
            current.append(ch)
            continue
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            groups.append("".join(current))
            current = []
            continue
        current.append(ch)
    groups.append("".join(current))
    return [g.strip() for g in groups if g.strip()]


def _tokenize_steps(selector: str) -> list[tuple[str, str]]:
    """拆分为 ``[(combinator, compound_selector), ...]``."""
    steps: list[tuple[str, str]] = []
    buf: list[str] = []
    combinator = " "
    depth = 0
    quote = ""
    i = 0
    n = len(selector)

    def flush(comb: str) -> None:
        compound = "".join(buf).strip()
        buf.clear()
        if compound:
            steps.append((comb, compound))

    while i < n:
        ch = selector[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in "\"'":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch in "([":
            depth += 1
            buf.append(ch)
            i += 1
            continue
        if ch in ")]":
            depth = max(0, depth - 1)
            buf.append(ch)
            i += 1
            continue
        if depth == 0:
            if ch == ">":
                flush(combinator)
                combinator = ">"
                i += 1
                while i < n and selector[i].isspace():
                    i += 1
                continue
            if ch.isspace():
                flush(combinator)
                combinator = " "
                i += 1
                continue
        buf.append(ch)
        i += 1
    flush(combinator)
    if steps:
        steps[0] = (" ", steps[0][1])
    return steps


_SIMPLE_RE = re.compile(
    r"""
    (?P<universal>\*)
  | (?P<tag>[a-zA-Z][a-zA-Z0-9_-]*)
  | \.(?P<cls>[a-zA-Z0-9_-]+)
  | \#(?P<id>[a-zA-Z0-9_-]+)
  | \[(?P<attrname>[a-zA-Z0-9_:-]+)
        (?:\s*(?P<op>[~*^$|]?=)\s*
           (?:"(?P<aval1>[^"]*)"|'(?P<aval2>[^']*)'|(?P<aval3>[^\]\s]*))
        )?\]
  | :(?P<pseudo>[a-zA-Z-]+)
        (?:\((?P<parg>(?:"[^"]*"|'[^']*'|[^()])*)\))?
    """,
    re.VERBOSE,
)


def _parse_compound(compound: str) -> list[tuple[str, Any]]:
    simples: list[tuple[str, Any]] = []
    pos = 0
    text = compound.strip()
    while pos < len(text):
        m = _SIMPLE_RE.match(text, pos)
        if not m:
            # 无法识别的字符, 跳过
            pos += 1
            continue
        pos = m.end()
        if m.group("universal"):
            simples.append(("tag", "*"))
        elif m.group("tag"):
            simples.append(("tag", m.group("tag").lower()))
        elif m.group("cls"):
            simples.append(("class", m.group("cls")))
        elif m.group("id"):
            simples.append(("id", m.group("id")))
        elif m.group("attrname"):
            value = m.group("aval1")
            if value is None:
                value = m.group("aval2")
            if value is None:
                value = m.group("aval3")
            simples.append(("attr", (m.group("attrname").lower(), m.group("op"), value)))
        elif m.group("pseudo"):
            name = m.group("pseudo").lower()
            arg = m.group("parg")
            if name == "not" and arg:
                simples.append(("not", _parse_compound(arg)))
            elif name == "contains" and arg is not None:
                simples.append(("contains", arg.strip("\"'")))
            else:
                simples.append(("pseudo", (name, arg)))
    return simples


def _match_compound(node: Node, simples: Sequence[tuple[str, Any]]) -> bool:
    for kind, value in simples:
        if kind == "tag":
            if value != "*" and node.tag != value:
                return False
        elif kind == "class":
            if value not in node.classes:
                return False
        elif kind == "id":
            if node.id != value:
                return False
        elif kind == "attr":
            name, op, expected = value
            if name not in node.attrs:
                return False
            if op is None:
                continue
            actual = node.attrs.get(name, "")
            if op == "=" and actual != expected:
                return False
            if op == "*=" and (expected or "") not in actual:
                return False
            if op == "^=" and not actual.startswith(expected or ""):
                return False
            if op == "$=" and not actual.endswith(expected or ""):
                return False
            if op == "~=" and (expected or "") not in actual.split():
                return False
            if op == "|=" and not (actual == expected or actual.startswith((expected or "") + "-")):
                return False
        elif kind == "not":
            if _match_compound(node, value):
                return False
        elif kind == "contains":
            if value.lower() not in node.text_content().lower():
                return False
        elif kind == "pseudo":
            name, arg = value
            sibs = node.parent.children if node.parent is not None else [node]
            try:
                idx = sibs.index(node)
            except ValueError:
                idx = 0
            if name == "first-child":
                if idx != 0:
                    return False
            elif name == "last-child":
                if idx != len(sibs) - 1:
                    return False
            elif name == "only-child":
                if len(sibs) != 1:
                    return False
            elif name == "empty":
                if node.children or node.text.strip():
                    return False
            elif name == "nth-child":
                try:
                    want = int(str(arg).strip())
                except (TypeError, ValueError):
                    return True
                if idx + 1 != want:
                    return False
            elif name in ("first-of-type", "last-of-type", "nth-of-type"):
                same = [n for n in sibs if n.tag == node.tag]
                try:
                    j = same.index(node)
                except ValueError:
                    j = 0
                if name == "first-of-type" and j != 0:
                    return False
                if name == "last-of-type" and j != len(same) - 1:
                    return False
                if name == "nth-of-type":
                    try:
                        if j + 1 != int(str(arg).strip()):
                            return False
                    except (TypeError, ValueError):
                        pass
            # 未识别的伪类: 不参与过滤
    return True


def _ancestors_match(node: Node, steps: Sequence[tuple[str, str]], idx: int) -> bool:
    if idx < 0:
        return True
    combinator = steps[idx + 1][0]
    simples = _parse_compound(steps[idx][1])
    if combinator == ">":
        parent = node.parent
        if parent is None or parent.tag == "#document":
            return False
        return _match_compound(parent, simples) and _ancestors_match(parent, steps, idx - 1)
    ancestor = node.parent
    while ancestor is not None and ancestor.tag != "#document":
        if _match_compound(ancestor, simples) and _ancestors_match(ancestor, steps, idx - 1):
            return True
        ancestor = ancestor.parent
    return False


def select_nodes(root: Node, selector: str, *, limit: int = 0) -> list[Node]:
    """在 ``root`` 的后代中执行 CSS 选择."""
    results: list[Node] = []
    seen: set[int] = set()
    for group in _split_groups(selector):
        steps = _tokenize_steps(group)
        if not steps:
            continue
        last_simples = _parse_compound(steps[-1][1])
        for node in root.descendants():
            if id(node) in seen:
                continue
            if not _match_compound(node, last_simples):
                continue
            if not _ancestors_match(node, steps, len(steps) - 2):
                continue
            seen.add(id(node))
            results.append(node)
            if limit and len(results) >= limit:
                return results
    return results


# --------------------------------------------------------------------------
# 常用提取
# --------------------------------------------------------------------------


def text_of(html: str) -> str:
    """粗暴去标签取文本."""
    doc = parse_html(html)
    return doc.text_content()


def strip_tags(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html or "")


def meta_tags(doc: Document) -> dict[str, str]:
    return dict(doc.meta)


def og_image(doc: Document) -> str:
    for key in ("og:image", "twitter:image", "twitter:image:src", "image"):
        if doc.meta.get(key):
            return doc.meta[key]
    return ""


def json_ld_blocks(doc: Document) -> list[Any]:
    """解析页面里的 ``application/ld+json`` 块."""
    import json as _json

    out: list[Any] = []
    for node in doc.descendants():
        if node.tag != "script":
            continue
        if "ld+json" not in (node.get("type") or "").lower():
            continue
        raw = "".join(node.text_parts).strip()
        if not raw:
            continue
        try:
            out.append(_json.loads(raw))
        except ValueError:
            continue
    return out


def table_rows(table: Node) -> list[list[str]]:
    """把 ``<table>`` 转成二维文本数组."""
    rows: list[list[str]] = []
    for tr in table.find_all("tr"):
        cells = [c.text_content() for c in tr.children if c.tag in ("td", "th")]
        if cells:
            rows.append(cells)
    return rows


def first_attr(nodes: Iterable[Node], attr: str) -> str:
    for n in nodes:
        value = n.get(attr)
        if value:
            return value
    return ""


def absolutize(url: str, base: str) -> str:
    from urllib.parse import urljoin

    if not url:
        return ""
    if url.startswith("//"):
        scheme = base.split(":", 1)[0] if "://" in base else "https"
        return f"{scheme}:{url}"
    return urljoin(base, url)
