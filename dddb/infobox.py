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
    "key", "title_original", "title", "title_romaji", "circle", "artists",
    "event", "release_date", "catalog_no", "price", "hall", "booth",
    "tags", "genres", "cover", "links", "credits", "tracks", "description",
)

#: 单值字段(其它是多行)
SINGLE_FIELDS = frozenset({
    "key", "title_original", "title", "title_romaji", "circle", "artists",
    "event", "release_date", "catalog_no", "price", "hall", "booth",
    "tags", "genres", "cover",
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
    """把 ImportDraft(或同形 dict) 导成 infobox 文本."""
    d = draft.to_dict() if hasattr(draft, "to_dict") else dict(draft or {})
    meta = d.get("meta") or {}
    lines: list[str] = [MARK]

    def put(name: str, value: Any) -> None:
        text = _clean(value)
        if not text:
            return
        lines.append("{} || {}".format(name, text))

    put("key", d.get("key") or meta.get("key") or "")
    for name in ("title_original", "title", "title_romaji", "circle",
                 "event", "release_date", "catalog_no", "price",
                 "hall", "booth"):
        put(name, meta.get(name))

    artists = meta.get("artists")
    if isinstance(artists, list):
        put("artists", " / ".join(_clean(a) for a in artists if _clean(a)))
    else:
        put("artists", artists)

    for name in ("tags", "genres"):
        val = meta.get(name)
        if isinstance(val, list):
            put(name, " / ".join(_clean(v) for v in val if _clean(v)))
        else:
            put(name, val)

    # 封面: 干部署时是远程 URL; 本地路径写 local: 前缀(便于识别)
    cover = _clean(d.get("cover_url"))
    if not cover and d.get("cover_file"):
        cover = "local:" + str(d.get("cover_file"))
    put("cover", cover)

    # 外链
    urls = d.get("urls") or {}
    if isinstance(urls, dict):
        for label, url in urls.items():
            if _clean(url):
                lines.append("links || {}: {}".format(_clean(label), _clean(url)))
    elif isinstance(urls, list):
        for it in urls:
            if isinstance(it, Mapping) and _clean(it.get("value")):
                lines.append("links || {}: {}".format(
                    _clean(it.get("key") or "链接"), _clean(it.get("value"))))

    # 词条
    for c in (d.get("credits") or []):
        if not isinstance(c, Mapping):
            continue
        name = _clean(c.get("name"))
        if not name:
            continue
        role = _clean(c.get("role")) or "artist"
        extra = ""
        if _clean(c.get("original")):
            extra += " ({})".format(_clean(c.get("original")))
        if _clean(c.get("alias")):
            extra += " [{}]".format(_clean(c.get("alias")))
        lines.append("credits || {}: {}{}".format(role, name, extra))

    # 曲目
    for i, tr in enumerate((d.get("tracks") or []), 1):
        if not isinstance(tr, Mapping):
            continue
        title = _clean(tr.get("title"))
        if not title:
            continue
        idx = _clean(tr.get("index")) or str(i)
        dur = _fmt_duration(tr.get("duration"))
        tail = ""
        ar = tr.get("artists")
        if isinstance(ar, list):
            ar = " / ".join(_clean(a) for a in ar if _clean(a))
        if _clean(ar):
            tail = " / " + _clean(ar)
        line = "tracks || {}. {}".format(idx, title)
        if dur:
            line += " [{}]".format(dur)
        lines.append(line + tail)

    # 简介(多行原样, 用 || 续行)
    desc = str(meta.get("description") or "")
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
                tr["artists"] = [x.strip() for x in re.split(r"\s*/\s*", ar)
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
