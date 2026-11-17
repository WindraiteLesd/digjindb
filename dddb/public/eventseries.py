"""展会名解析: 从现有名字拆出「系列 + 会次」.

现有数据只有这些形态:
    M3-2026春 / M3-2026秋 / M3-2021春 / M3-2024秋   -> 系列 M3,  会次 M3-58 形式
    C108 / C102 / C105                              -> 系列 CM,  会次 C108
    例大祭21                                         -> 系列 例大祭, 会次 例大祭21
    M3-2026秋(resale)                               -> 同 M3-2026秋 (转售)

解析规则(保守优先, 认不出就整个当系列名):
    ① C<数字>            -> CM / Comic Market, 会次 = C<数字>
    ② <字母>-<年><季>     -> 系列 <字母>, 会次需要另行映射(这里只给系列 + 年份季)
    ③ <汉字/假名><数字>   -> 系列 = 前缀, 会次 = 原名
"""

from __future__ import annotations

import re
from typing import Any

#: 系列名 -> (缩写, 全称)
KNOWN_SERIES: dict[str, tuple[str, str]] = {
    "M3": ("M3", "Music Media-Mix Market"),
    "CM": ("CM", "Comic Market"),
    "C": ("CM", "Comic Market"),
    "例大祭": ("例大祭", "博丽神社例大祭"),
    "秋例": ("例大祭", "博丽神社秋季例大祭"),
    "DM": ("DM", "电则市场DizzyMart"),
    "Comiket": ("CM", "Comic Market"),
    "M3-": ("M3", "M3"),
}

#: 会次号 -> 官方会次名(只有 M3 有稳定的连续编号)
#: M3-2026秋 = M3-58 (依据官方届次推算; 用户示例给的就是这个)
M3_VOL_BY_YEAR_SEASON: dict[tuple[int, str], int] = {
    (2026, "春"): 57,
    (2026, "秋"): 58,
    (2024, "秋"): 53,
    (2021, "春"): 46,
}

_RE_C = re.compile(r"^(?:C|C\d{2,3})(\d{2,3})$")
_RE_LETTER_YEAR = re.compile(r"^([A-Za-z][A-Za-z0-9]{0,5})[-_]?(\d{4})\s*(春|秋|夏|冬)?$")
_RE_CJK_NUM = re.compile(r"^([^\d]+?)\s*(\d{1,4})$")


#: 再贩标记 —— 属于**标签**, 不是会次。形如 "C108(resale)" / "M3-2026秋(再販)"
_RESALE_RE = re.compile(
    r"\s*[（(]\s*(?:re-?sale|再販|再贩)\s*[)）]\s*$", re.IGNORECASE)
_RESALE_TAIL_RE = re.compile(r"\s*(?:再販|再贩)\s*$")

#: 剥出再贩标记后打的标签
RESALE_TAG = "再贩"


def split_resale(event: Any) -> tuple[str, bool]:
    """把 event 名里的再贩标记剥出来.

    "C108(resale)" -> ("C108", True)

    再贩的含义是"这张专辑不是在本会次首发, 而是已发行过的碟在该会次再贩" ——
    它是**专辑的属性(标签)**, 不是新的会次。所以 event 要去掉标记归到原会次,
    tags 加上 :data:。
    """
    text = str(event or "").strip()
    if not text:
        return "", False
    cleaned = _RESALE_RE.sub("", text)
    if cleaned != text:
        return cleaned.strip(), True
    cleaned = _RESALE_TAIL_RE.sub("", text)
    if cleaned != text:
        return cleaned.strip(), True
    return text, False


def vol_from_label(label: Any) -> int:
    """从会次标签里取**最后一组数字**作为会次号.

    ``"M3-58"`` -> 58 (不是 358), ``"C108"`` -> 108, ``"例大祭21"`` -> 21。

    为什么取最后一组: 系列名本身可能带数字(如 M3), 把全部数字拼起来
    会得到 358 —— 那是错的。
    """
    groups = re.findall(r"(\d+)", str(label or ""))
    if not groups:
        return 0
    try:
        return int(groups[-1])
    except ValueError:
        return 0


def parse_event_name(name: Any) -> dict[str, Any]:
    """把展会名拆成 ``{series, abbr, full_name, vol, vol_label}``.

    认不出来时 ``series`` 就用原名, ``abbr``/``full_name`` 也填原名,
    ``vol``/``vol_label`` 留空 —— 展示层会退化成只显示名字。
    """
    raw = str(name or "").strip()
    empty = {"series": raw, "abbr": "", "full_name": raw,
             "vol": 0, "vol_label": ""}
    if not raw:
        return empty

    # 去掉 "(resale)" 之类的后缀来做判断, 但**不改原名**
    base = re.sub(r"[（(]\s*re?sal?e\s*[)）]\s*$", "", raw, flags=re.I).strip()
    base = re.sub(r"\s*再販\s*$", "", base).strip() or raw

    # ① C108 / C102 ...
    m = _RE_C.match(base)
    if m:
        num = m.group(1)
        abbr, full = KNOWN_SERIES["CM"]
        return {"series": full, "abbr": abbr, "full_name": full,
                "vol": int(num), "vol_label": "C" + num}

    # ② M3-2026秋 / C102 之类 "字母-年份季节"
    m = _RE_LETTER_YEAR.match(base)
    if m:
        letters = m.group(1).upper()
        year = int(m.group(2))
        season = (m.group(3) or "").strip()
        abbr, full = KNOWN_SERIES.get(letters, (letters, letters))
        vol = M3_VOL_BY_YEAR_SEASON.get((year, season), 0)
        vol_label = (f"{abbr}-{vol}" if vol else
                     (f"{abbr}-{year}{season}" if season else f"{abbr}-{year}"))
        # series 是**系列 key**(稳定, 用作外键); full_name 是**系列全称**;
        # 会次的展示名由 data 层用会次自己的名字拼, 不在这里。
        return {"series": full, "abbr": abbr, "full_name": full,
                "vol": vol, "vol_label": vol_label}

    # ③ 例大祭21 / 秋例13 / 博丽神社例大祭 21
    m = _RE_CJK_NUM.match(base)
    if m:
        prefix = m.group(1).strip()
        num = int(m.group(2))
        # 前缀里含"秋例" -> 秋例系列
        if "秋例" in prefix:
            abbr, full = KNOWN_SERIES["秋例"]
            label = f"秋例{num}"
        elif "例大祭" in prefix:
            abbr, full = KNOWN_SERIES["例大祭"]
            label = f"例大祭{num}" if prefix.strip() == "例大祭" else f"{prefix}{num}"
        else:
            abbr, full = prefix, prefix
            label = f"{prefix}{num}"
        return {"series": full, "abbr": abbr, "full_name": full,
                "vol": num, "vol_label": label}

    # ④ 认不出来: 整个当系列名
    abbr, full = KNOWN_SERIES.get(base, ("", base))
    return {"series": base, "abbr": abbr, "full_name": full,
            "vol": 0, "vol_label": ""}


def display_name(abbr: Any, full_name: Any, vol_label: Any,
                 fallback: Any = "") -> str:
    """组合成 "[M3] M3-2026秋 (M3-58)" 这种显示名.

    缺哪一段就省略哪一段, 不会留下空括号。
    """
    a = str(abbr or "").strip()
    f = str(full_name or "").strip() or str(fallback or "").strip()
    v = str(vol_label or "").strip()
    if not f:
        f = str(fallback or "").strip()
    if not f:
        return ""
    text = f
    if a and a != f:
        text = f"[{a}] {f}"
    elif a and a == f:
        text = f"[{a}] {f}"
    if v:
        text = f"{text} ({v})"
    return text
