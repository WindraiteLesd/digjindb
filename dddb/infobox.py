"""dddb infobox: 一张专辑的**纯文本**完整描述.

用户要求:
    "把缓存的包括封面在内的所有线上需要的元数据导出为线上 infobox 格式，
     允许在线上新建专辑页面中放置一个编辑 infobox 按钮，会直接弹出一个
     输入框，复制现在的 dddb infobox 进去就可以一键填入所有词条，
     并在输入框下方额外放置帮助按钮弹出二级窗口的格式说明，
     编辑专辑页也可以点击按钮查看专辑的 infobox 并进行修改"

格式设计(仿 Bangumi infobox, 但用 dddb 自己的字段名):

    [dddb]
    key              || 社团|专辑名|2026
    title_original   || BLOOD SNOW
    title            || 血雪
    title_romaji     || Blood Snow
    circle           || Winter Bouquet
    artists          || Winter Bouquet / N_dog
    event            || M3-2026秋
    release_date     || 2026-10-25
    catalog_no       || WB-001
    price            || 1100
    hall             || 第一展示場
    booth            || A-01a
    tags             || 东方 / 同人
    genres           || J-Pop
    cover            || https://.../cover.jpg
    links            || 官网: https://example.com/
                     || Booth: https://booth.pm/xxx
    credits          || 作曲: 某人 (Someone)
                     || 编曲: 某乙
                     || 钢琴: 某丙
    tracks           || 1. Title A [3:57]
                     || 2. Title B [3:20] / 艺人
    description      || 多行简介...

为什么用 ``字段 || 值`` 而不是 YAML/JSON:
    ① 一眼能看懂, 便于手改
    ② 不依赖缩进(YAML 缩进错了很难查)
    ③ 复用行可以多行(``||`` 开头继续), 曲目/词条/外链都是变长的
    ④ 和 Bangumi infobox 的 ``|`` 风格接近, 用户有肌肉记忆
"""

from __future__ import annotations

import re
from typing import Any, Mapping

#: 标记这是一段 dddb infobox(解析时用来定位起点)
MARK = "[dddb]"

#: 字段顺序(导出时按这个顺序写, 解析时也按这个认)
FIELDS = (
    "key", "title_original", "title", "title_romaji", "circle", "company",
    "artists", "event", "release_date", "catalog_no", "price", "hall",
    "booth", "tags", "genres", "cover", "links", "credits", "tracks",
    "description",
)

#: 单值字段(其它是多行)
SINGLE_FIELDS = frozenset({
    "key", "title_original", "title", "title_romaji", "circle", "company",
    "artists", "event", "release_date", "catalog_no", "price", "hall",
    "booth", "tags", "genres", "cover",
})

#: 多行字段
MULTI_FIELDS = frozenset({"links", "credits", "tracks", "description"})

#: 分隔符: 字段名 || 值 (两侧空格可选)
_SEP = re.compile(r"^\s*([a-z_]+)\s*\|\|\s?(.*)$")
_CONT = re.compile(r"^\s*\|\|\s?(.*)$")

#: 曲目写法: "1. 曲名 [3:57]" 或 "1. 曲名 [3:57] / 艺人"
_TRACK = re.compile(
    r"^\s*(\d+)\s*[.、)]\s*(.+?)\s*"
    r"(?:\[(\d{1,2}:\d{2}(?::\d{2})?)\])?\s*"
    r"(?:/\s*(.+))?$")

#: 词条写法: "职位: 人名" 或 "职位: 人名 (原名)"
_CREDIT = re.compile(r"^\s*([^:：]+)\s*[:：]\s*(.+?)\s*$")

#: 外链写法: "标签: URL" / "标签 || URL"
_LINK = re.compile(r"^\s*([^:：|]+?)\s*(?:[:：]|\|\|)\s*(\S+)\s*$")


#: 三种输入形状 —— ``build()`` 必须都能认, 否则线上编辑页会导出一段空壳。
#:
#: * ``draft``: 导入草稿(``ImportDraft`` / :func:`parse` 的产物), 字段在 ``meta`` 子字典里
#: * ``flat``: 扁平 payload(``Album.to_dict()`` —— 采集与线上编辑页用的就是这一种)
#: * ``view``: 详情视图(:func:`dddb.public.data.PublicData.make_view`), 键名不同
SHAPE_DRAFT = "draft"
SHAPE_FLAT = "flat"
SHAPE_VIEW = "view"

#: 视图键 -> dddb 字段名(视图里没有"原题/译名"这组名字, 只有 title/title_alt)
_VIEW_KEYS = {
    "title_original": "title",
    "title": "title_alt",
    "title_romaji": "romaji",
    "release_date": "date",
}


def _shape_of(d: Mapping[str, Any], meta: Mapping[str, Any]) -> str:
    """判断输入是草稿 / 扁平 payload / 详情视图."""
    if meta:
        return SHAPE_DRAFT
    if "title_alt" in d or "romaji" in d:
        return SHAPE_VIEW
    if "date" in d and "release_date" not in d:
        return SHAPE_VIEW
    return SHAPE_FLAT


def _value_of(d: Mapping[str, Any], meta: Mapping[str, Any],
              shape: str, name: str) -> Any:
    """按形状取一个单值字段."""
    if shape == SHAPE_DRAFT:
        return meta.get(name)
    if shape == SHAPE_VIEW:
        # 视图换了名字的字段只认映射里的键(否则 title 会跟 title_original 重复)
        key = _VIEW_KEYS.get(name)
        return d.get(key) if key else d.get(name)
    return d.get(name)


def _link_rows(urls: Any) -> list[tuple[str, str]]:
    """外链的三种写法: dict / ``[{key,value}]`` / ``[{name,url}]``(视图)."""
    rows: list[tuple[str, str]] = []
    if isinstance(urls, Mapping):
        for label, url in urls.items():
            if _clean(url):
                rows.append((_clean(label) or "链接", _clean(url)))
    elif isinstance(urls, (list, tuple)):
        for item in urls:
            if not isinstance(item, Mapping):
                continue
            url = item.get("value") or item.get("url")
            if not _clean(url):
                continue
            label = item.get("key") or item.get("name") or "链接"
            rows.append((_clean(label), _clean(url)))
    return rows


def _credit_rows(credits: Any,
                 artist_names: set[str]) -> list[tuple[str, str, str, str]]:
    """词条 -> ``(职位, 人名, 原名, 别名)``.

    * ``circle`` 职责与 ``circle`` 字段重复, 丢掉
    * 视图里的词条是 :func:`group_credits` 分组后的 ``{role,label,names,count}``
    * 人名已经在 ``artists`` 里的 ``artist`` 词条也丢掉(同一份数据的两种表示)
    """
    rows: list[tuple[str, str, str, str]] = []
    if not isinstance(credits, (list, tuple)):
        return rows
    for item in credits:
        if not isinstance(item, Mapping):
            continue
        role = _clean(item.get("role")) or "artist"
        if role == "circle":
            continue
        names = item.get("names")
        if isinstance(names, (list, tuple)):
            for name in names:
                if _clean(name):
                    rows.append((role, _clean(name), "", ""))
            continue
        name = _clean(item.get("name"))
        if not name:
            continue
        if role == "artist" and name in artist_names:
            continue
        rows.append((role, name, _clean(item.get("original")),
                     _clean(item.get("alias"))))
    return rows


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _fmt_duration(seconds: Any) -> str:
    try:
        n = int(seconds or 0)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    return "{}:{:02d}".format(n // 60, n % 60)


def _parse_duration(text: Any) -> int:
    raw = str(text or "").strip()
    if not raw:
        return 0
    parts = raw.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return 0
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    return 0


def build(draft: Any) -> str:
    """把三种输入形状导成 infobox 文本.

    ``draft`` 可以是 :class:`ImportDraft` / 扁平 payload / 详情视图 ——
    线上编辑页传的是扁平 payload, 以前只认草稿形状, 于是整段只剩 key 与外链。
    """
    d = draft.to_dict() if hasattr(draft, "to_dict") else dict(draft or {})
    meta = d.get("meta") or {}
    shape = _shape_of(d, meta)
    lines: list[str] = [MARK]

    def put(name: str, value: Any) -> None:
        text = _clean(value)
        if not text:
            return
        lines.append("{} || {}".format(name, text))

    def field(name: str) -> Any:
        return _value_of(d, meta, shape, name)

    put("key", d.get("key") or meta.get("key") or "")
    # 先放原题, 再放译名 —— 两者相同时跳过译名, 免得 infobox 里
    # 同一条标题写两遍(线上导出的数据里 title 常常就是 title_original)。
    _orig = _clean(field("title_original"))
    _disp = _clean(field("title"))
    put("title_original", _orig)
    if _disp and _disp != _orig:
        put("title", _disp)
    for name in ("title_romaji", "circle", "event",
                 "release_date", "catalog_no", "price", "hall", "booth"):
        put(name, field(name))
    # 发行公司: 只在**与社团不同**时才输出.
    # 网易云的厂牌常常就是社团名, 导出后会变成 ``circle || N_dog`` 与
    # ``company || N_dog`` 两条重复; 而且线上编辑页「发行公司」是独立字段,
    # 用户明确说"circle 应该对应社团而不是发行公司, 大部分专辑都不会用到
    # 发行公司这个条目"。同值时省略, 避免它被当成社团的另一种写法。
    _company = _clean(field("company"))
    if _company and _company != _clean(field("circle")):
        put("company", _company)

    artists_value = field("artists")
    if isinstance(artists_value, (list, tuple)):
        ordered = [_clean(a) for a in artists_value if _clean(a)]
        put("artists", " / ".join(ordered))
    else:
        ordered = [_clean(artists_value)] if _clean(artists_value) else []
        put("artists", artists_value)
    artist_names = set(ordered)

    for name in ("tags", "genres"):
        val = field(name)
        if isinstance(val, (list, tuple)):
            put(name, " / ".join(_clean(v) for v in val if _clean(v)))
        else:
            put(name, val)

    # 封面: 远程 URL 优先; 只有本地文件时写 local: 前缀
    cover = _clean(d.get("cover_url"))
    if not cover:
        local = d.get("cover_local") or d.get("cover_file")
        if local:
            cover = "local:" + str(local)
    put("cover", cover)

    for label, url in _link_rows(d.get("urls")):
        lines.append("links || {}: {}".format(label, url))

    for role, name, original, alias in _credit_rows(d.get("credits"),
                                                   artist_names or set()):
        extra = ""
        if original:
            extra += " ({})".format(original)
        if alias:
            extra += " [{}]".format(alias)
        lines.append("credits || {}: {}{}".format(role, name, extra))

    tracks = d.get("tracks")
    if isinstance(tracks, (list, tuple)):
        for i, tr in enumerate(tracks, 1):
            if not isinstance(tr, Mapping):
                continue
            title = _clean(tr.get("title"))
            if not title:
                continue
            idx = _clean(tr.get("index")) or str(i)
            dur = _fmt_duration(tr.get("duration"))
            tail = ""
            ar = tr.get("artists")
            if isinstance(ar, (list, tuple)):
                ar = " / ".join(_clean(a) for a in ar if _clean(a))
            if _clean(ar):
                tail = " / " + _clean(ar)
            line = "tracks || {}. {}".format(idx, title)
            if dur:
                line += " [{}]".format(dur)
            lines.append(line + tail)

    desc = str((meta.get("description") if shape == SHAPE_DRAFT
                else d.get("description")) or "")
    if desc.strip():
        first, *rest = desc.strip().split("\n")
        lines.append("description || {}".format(first.rstrip()))
        for ln in rest:
            lines.append("|| {}".format(ln.rstrip()))

    return "\n".join(lines)


def parse(text: Any) -> dict[str, Any]:
    """infobox 文本 -> ImportDraft 形状的 dict.

    认不出的行**忽略**(不报错) —— 用户可能粘了带额外说明的文本。
    """
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    out: dict[str, Any] = {
        "key": "", "source": "infobox", "meta": {}, "cover_url": "",
        "cover_file": "", "credits": [], "urls": {}, "tracks": [],
        "warnings": [], "unrecognized_roles": [],
    }
    meta = out["meta"]
    current = ""

    for line in raw.split("\n"):
        if not line.strip() or line.strip() == MARK:
            continue

        m = _SEP.match(line)
        if m:
            current = m.group(1).lower()
            value = m.group(2)
        else:
            m2 = _CONT.match(line)
            if not m2:
                continue
            value = m2.group(1)
            if not current:
                continue

        if current in SINGLE_FIELDS:
            if current == "key":
                out["key"] = _clean(value)
            elif current == "cover":
                cv = _clean(value)
                if cv.startswith("local:"):
                    out["cover_file"] = cv[6:]
                else:
                    out["cover_url"] = cv
            elif current in ("artists", "tags", "genres"):
                meta[current] = [x.strip() for x in re.split(r"\s*/\s*", value)
                                 if x.strip()]
            else:
                meta[current] = _clean(value)

        elif current == "links":
            lm = _LINK.match(value)
            if lm:
                out["urls"][_clean(lm.group(1))] = _clean(lm.group(2))

        elif current == "credits":
            # 职位里可能自带冒号(instrument:Piano), 所以按**最后一个**冒号切
            parts = re.split(r"[:：]", value)
            if len(parts) < 2:
                continue
            role = _clean(":".join(parts[:-1]))
            rest = _clean(parts[-1])
            name, original, alias = _split_credit_person(rest)
            if not name:
                continue
            c: dict[str, Any] = {"role": role, "name": name}
            if original:
                c["original"] = original
            if alias:
                c["alias"] = alias
            out["credits"].append(c)

        elif current == "tracks":
            tm = _TRACK.match(value)
            if not tm:
                continue
            index = tm.group(1)
            title = _clean(tm.group(2))
            dur = _parse_duration(tm.group(3))
            ar = _clean(tm.group(4))
            if not title:
                continue
            tr: dict[str, Any] = {
                "disc": 1, "index": int(index) if index.isdigit() else 0,
                "title": title, "duration": dur,
            }
            if ar:
                # 网易云导出的是「N_dog、Letsu」, 手写习惯是「A / B」——
                # 两种都要拆, 否则整串当成一个艺人名。
                tr["artists"] = [x.strip() for x in
                                 re.split(r"\s*/\s*|\s*[、,，;；]\s*", ar)
                                 if x.strip()]
            out["tracks"].append(tr)

        elif current == "description":
            meta.setdefault("description", "")
            meta["description"] += (("\n" if meta["description"] else "")
                                    + value.rstrip())

    # artists 可能是字符串(单艺人) -> 统一成列表
    for name in ("artists", "tags", "genres"):
        if name not in meta:
            meta[name] = []

    # 标题至少要有一个
    if not meta.get("title_original") and meta.get("title"):
        meta["title_original"] = meta["title"]
    if not meta.get("title") and meta.get("title_original"):
        meta["title"] = ""

    if not out["key"]:
        circle = (meta.get("circle") or [""])
        if isinstance(circle, list):
            circle = circle[0] if circle else ""
        out["key"] = "{}|{}".format(
            _clean(circle), _clean(meta.get("title_original")))

    # 缺什么提示什么
    if not meta.get("release_date"):
        out["warnings"].append("缺发售日")
    if not meta.get("event"):
        out["warnings"].append("缺展会")
    if not out["credits"]:
        out["warnings"].append("没有制作人员")
    return out


def _split_credit_person(text: str) -> tuple[str, str, str]:
    """``"某人 (Someone) [别名]"`` -> ``(某人, Someone, 别名)``."""
    raw = _clean(text)
    alias = ""
    m = re.search(r"\s*\[([^\]]+)\]\s*$", raw)
    if m:
        alias = _clean(m.group(1))
        raw = raw[:m.start()].rstrip()
    original = ""
    m = re.search(r"\s*[（(]([^（()）]+)[)）]\s*$", raw)
    if m:
        original = _clean(m.group(1))
        raw = raw[:m.start()].rstrip()
    return _clean(raw), original, alias


#: 帮助文本(线上「帮助」按钮弹出的格式说明)
HELP_TEXT = """\
# dddb infobox 格式

每行 `字段 || 值`。`||` 前是字段名（全小写），后面是值。
以 `||` 单独开头的行表示**续上一条**（用于多行字段）。

## 单值字段

| 字段 | 说明 | 示例 |
|---|---|---|
| key | 唯一标识，可留空自动生成 | 社团\\|专辑名\\|2026 |
| title_original | 原题（必填） | BLOOD SNOW |
| title | 译名/显示名 | 血雪 |
| title_romaji | 罗马字 | Blood Snow |
| circle | 社团 | Winter Bouquet |
| company | 发行公司 | Winter Records |
| artists | 艺人，多个用 `/` 分隔 | A / B |
| event | 展会 | M3-2026秋 |
| release_date | 发售日 | 2026-10-25 |
| catalog_no | 编号 | WB-001 |
| price | 价格 | 1100 |
| hall | 展馆 | 第一展示場 |
| booth | 展位 | A-01a |
| tags | 标签，`/` 分隔 | 东方 / 同人 |
| genres | 曲风，`/` 分隔 | J-Pop |
| cover | 封面地址（可 `local:` 开头表示本地文件） | https://... |

## 多行字段

**links**（相关链接）——每行 `标签: URL`

```
links || 官网: https://example.com/
links || Booth: https://booth.pm/xxx
```

**credits**（制作人员）——每行 `职位: 人名`，可带原名 `( )` 与别名 `[ ]`

```
credits || 作曲: 某人 (Someone)
credits || 编曲: 某乙
credits || 钢琴: 某丙 (Somebody) [马甲名]
```

**tracks**（曲目）——每行 `序号. 曲名 [时长] / 艺人`

```
tracks || 1. Luciphobia [3:57]
tracks || 2. Hallucination [4:12] / MALVA.
```

**description**（简介）——首行正常写，后续行用 `|| ` 开头续

```
description || 第一段
|| 第二段
```

## 最短可用例子

```
[dddb]
title_original || BLOOD SNOW
circle || Winter Bouquet
event || M3-2026秋
release_date || 2026-10-25
tracks || 1. Luciphobia [3:57] / MALVA.
```
"""


__all__ = [
    "FIELDS",
    "HELP_TEXT",
    "MARK",
    "MULTI_FIELDS",
    "SINGLE_FIELDS",
    "build",
    "parse",
]
