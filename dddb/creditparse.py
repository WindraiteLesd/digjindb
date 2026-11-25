"""制作人员「整段粘贴」解析(用户 m14663).

编辑者在别处看到的制作人员表往往长这样::

    制作: Nika Lenz (https://x.com/nika)
    作曲: Nika Lenz / kaza
    插画: かにくみーとい （https://example.com/illust）
    演唱: kaza

要解决三个麻烦:

1. **括号里的链接**不是名字的一部分 —— ``音楽: 甲 (https://x.com/a)``
   里的 ``https://...`` 必须被丢掉, 否则会变成艺人名 "甲 https://x.com/a"。
2. **一个职责多个名字** —— ``/``、``、``、``,`` 都要能拆开。
3. **职责本身可能带冒号** —— ``instrument:Piano: 某丙`` 按**最后一个**冒号切
   (与 :mod:`dddb.infobox` 的 credits 解析保持一致)。

解析结果直接是 ``credits`` 行的形状(``{"role": ..., "name": ...}``), 由调用方
合并进本次提交的制作人员列表。
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "ParsedCredit",
    "CREDIT_MODES",
    "parse_credits",
    "merge_pasted_credits",
    "normalize_credit_mode",
    "strip_links",
]

#: 合并方式 —— 与曲目表粘贴保持同一套语义
CREDIT_MODES: tuple[str, ...] = ("replace", "append")

#: 括号: 半角与全角都要认(中文编辑者混用)
_PAREN_RE = re.compile(r"[(（][^()（）]*[)）]")

#: 行内链接(没被括号包住的裸链接也要去掉)
_URL_RE = re.compile(r"https?://\S+", re.I)

#: 冒号: 半角/全角
_COLON_RE = re.compile(r"[:：]")

#: 一个职责里多个名字的分隔符
_NAME_SPLIT_RE = re.compile(r"\s*[/、,，;；]\s*")

#: 行首的项目符号 / 编号(``1.`` ``-`` ``·``), 去掉
_BULLET_RE = re.compile(r"^\s*(?:[-*·•]|\d+\s*[.、)）])\s*")

#: 明显是表头的行(整行只有这些词)直接跳过
_HEADER_WORDS = frozenset({
    "职责", "职位", "角色", "担当", "人员", "名字", "姓名", "staff", "credit",
    "credits", "role", "roles", "name", "names",
})


def strip_links(text: str) -> str:
    """去掉括号内的内容与行内裸链接, 只留名字部分.

    ``甲 (https://x.com/a)`` -> ``甲``;
    ``甲 （说明）`` -> ``甲``;
    ``甲 https://x.com/a`` -> ``甲``。
    """
    source = str(text or "")
    # 括号可能嵌套(``甲 (a (b))``), 多轮直到稳定
    for _ in range(4):
        cleaned = _PAREN_RE.sub(" ", source)
        if cleaned == source:
            break
        source = cleaned
    source = _URL_RE.sub(" ", source)
    return re.sub(r"\s+", " ", source).strip()


class ParsedCredit:
    """一条解析出来的制作人员记录."""

    __slots__ = ("role", "name", "raw")

    def __init__(self, role: str, name: str, raw: str = "") -> None:
        self.role = role
        self.name = name
        self.raw = raw

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "name": self.name}

    def __repr__(self) -> str:                       # pragma: no cover
        return f"ParsedCredit(role={self.role!r}, name={self.name!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ParsedCredit):
            return NotImplemented
        return (self.role, self.name) == (other.role, other.name)

    def __hash__(self) -> int:
        return hash((self.role, self.name))


def _is_header_line(text: str) -> bool:
    """``职责: 名字`` 这种表头行不算数据."""
    bare = strip_links(text).strip().lower()
    if not bare:
        return True
    # 整行按分隔符切开, 每一块都得是表头词才算表头;
    # ``职责: 名字`` 这类带冒号的表头要把冒号也当分隔符。
    pieces = [
        piece.strip().lower()
        for piece in re.split(r"\s*[/、,，;；:：]\s*", bare)
        if piece.strip()
    ]
    return bool(pieces) and all(p in _HEADER_WORDS for p in pieces)


def parse_credits(text: Any) -> list[ParsedCredit]:
    """把整段文本解析成 :class:`ParsedCredit` 列表(保持原顺序, 同条去重).

    * 空输入 / 只有表头 -> ``[]``
    * 没有冒号的行 -> 忽略(不猜)
    * 职责或名字为空的行 -> 忽略
    * 同一 ``(职责, 名字)`` 重复出现 -> 只留第一条
    """
    source = text if isinstance(text, str) else ("" if text is None else str(text))
    out: list[ParsedCredit] = []
    seen: set[tuple[str, str]] = set()
    for raw_line in source.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        line = _BULLET_RE.sub("", line)
        if not line or _is_header_line(line):
            continue
        if not _COLON_RE.search(line):
            continue
        # 先把链接去掉再找冒号 —— 否则 ``制作: 甲 (https://x.com/a)`` 的
        # **最后一个冒号**是 ``https:`` 里的那个, 职责会变成 ``制作: 甲 (https``。
        line = strip_links(line)
        if not line or not _COLON_RE.search(line):
            continue
        # 按**最后一个**冒号切: ``instrument:Piano: 某丙`` 的职责是
        # ``instrument:Piano``(与 infobox 的 credits 解析同一约定)
        cut = max(m.start() for m in _COLON_RE.finditer(line))
        role = line[:cut].strip()
        names = line[cut + 1:].strip()
        if not role or not names:
            continue
        for name in _NAME_SPLIT_RE.split(names):
            person = name.strip()
            if not person:
                continue
            marker = (role, person)
            if marker in seen:
                continue
            seen.add(marker)
            out.append(ParsedCredit(role=role, name=person, raw=line))
    return out


def normalize_credit_mode(value: Any) -> str:
    """单选值 -> 合法模式(空 / 伪造值 -> ``replace``)."""
    text = str(value or "").strip().lower()
    return text if text in CREDIT_MODES else "replace"


def _credit_rows(credits: Iterable[Any]) -> list[dict[str, str]]:
    """把各种形状的 credits 归一成 ``[{"role","name"}]``."""
    rows: list[dict[str, str]] = []
    for item in credits or []:
        if isinstance(item, Mapping):
            role = str(item.get("role") or "").strip()
            name = str(item.get("name") or "").strip()
        else:
            role = str(getattr(item, "role", "") or "").strip()
            name = str(getattr(item, "name", "") or "").strip()
        if role or name:
            rows.append({"role": role, "name": name})
    return rows


def merge_pasted_credits(
    credits: Sequence[Mapping[str, Any]],
    parsed: Sequence[ParsedCredit],
    mode: str = "replace",
) -> list[dict[str, str]]:
    """把粘贴解析结果并进**本次提交**的制作人员列表.

    * ``replace``: 用解析结果替换现有列表;
    * ``append``: 接在后面, 按 ``(职责, 名字)`` 去重;
    * ``parsed`` 为空 -> **原样返回**现有列表(粘错一次不能清空已有词条)。
    """
    current = _credit_rows(credits)
    items = [c.to_dict() for c in (parsed or [])]
    if not items:
        return current
    if normalize_credit_mode(mode) != "append":
        return items
    merged = list(current)
    seen = {(row["role"], row["name"]) for row in merged}
    for item in items:
        marker = (item["role"], item["name"])
        if marker in seen:
            continue
        seen.add(marker)
        merged.append(item)
    return merged


def to_infobox_text(parsed: Sequence[ParsedCredit]) -> str:
    """解析结果 -> infobox 的 ``credits`` 段文本(便于"转译为infobox文本").

    真实格式是每行 ``credits || 职责: 名字``(见 :func:`dddb.infobox.build`),
    不是 ``credits`` 标题 + 裸 ``职责: 名字`` —— 后者会被 ``parse()`` 当成
    未知字段而全部丢失。
    """
    lines = ["[dddb]"]
    for item in parsed or []:
        lines.append(f"credits || {item.role}: {item.name}")
    return "\n".join(lines)
