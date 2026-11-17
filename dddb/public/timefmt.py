"""时间戳按**语言对应的时区**格式化(不依赖服务器时区).

为什么需要这个模块:
    云端服务器是 UTC, 而 ``time.localtime()`` 取的是**服务器本地时区** ——
    中文页面上的时间会比北京时间慢 8 小时。这里改成按语言取固定偏移,
    与服务器时区无关, 也不会因为部署机器不同而变。

时区选择依据(都是**无夏令时**的固定偏移, 所以用 timedelta 就够):
    zh -> UTC+8  中国标准时间
    ja -> UTC+9  日本标准时间
    en -> UTC+0  英文界面用 UTC(国际通用)

用法::

    from .timefmt import format_stamp
    format_stamp(last_login, ctx.lang)      # -> "2026-11-16 21:26"
    format_stamp(value, "zh", with_seconds=True)
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

#: 语言 -> UTC 偏移(小时)。都是固定偏移, 没有夏令时问题。
LANGUAGE_TZ_OFFSET: dict[str, int] = {
    "zh": 8,
    "ja": 9,
    "en": 0,
}

#: 默认(未知语言)按中文处理 —— 站点主要是中文用户
DEFAULT_OFFSET = 8

#: 时区的展示名(调试/测试用)
LANGUAGE_TZ_LABEL: dict[str, str] = {
    "zh": "UTC+8",
    "ja": "UTC+9",
    "en": "UTC",
}


def zone_offset(lang: Any = "zh") -> int:
    """取语言对应的 UTC 偏移小时数(未知语言回退到中文 +8)."""
    code = str(lang or "").strip().lower()[:2]
    return LANGUAGE_TZ_OFFSET.get(code, DEFAULT_OFFSET)


def zone_of(lang: Any = "zh") -> timezone:
    """取 ``datetime.timezone`` 对象."""
    return timezone(timedelta(hours=zone_offset(lang)))


def zone_label(lang: Any = "zh") -> str:
    """``zh`` -> ``"UTC+8"``(用于界面上标注时间基准)."""
    code = str(lang or "").strip().lower()[:2]
    return LANGUAGE_TZ_LABEL.get(code, "UTC+{}".format(DEFAULT_OFFSET))


def _to_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def format_stamp(value: Any, lang: Any = "zh", *,
                 with_seconds: bool = False, empty: str = "") -> str:
    """时间戳 -> 按语言时区格式化.

    ``value`` 是 Unix 时间戳(秒)。空值/非法值返回 ``empty``。
    """
    stamp = _to_float(value)
    if stamp <= 0:
        return empty
    try:
        moment = datetime.fromtimestamp(stamp, tz=zone_of(lang))
    except (OverflowError, OSError, ValueError):     # 极端值兜底
        return empty
    if with_seconds:
        return moment.strftime("%Y-%m-%d %H:%M:%S")
    return moment.strftime("%Y-%m-%d %H:%M")


def format_stamp_iso(value: Any, lang: Any = "zh") -> str:
    """ISO 8601 带时区偏移: ``2026-11-16T21:26:00+08:00``."""
    stamp = _to_float(value)
    if stamp <= 0:
        return ""
    try:
        moment = datetime.fromtimestamp(stamp, tz=zone_of(lang))
    except (OverflowError, OSError, ValueError):
        return ""
    return moment.isoformat(timespec="seconds")


def now_stamp_text(lang: Any = "zh") -> str:
    """当前时间(按语言时区)的文本, 给"草稿已保存"这类提示用。"""
    return format_stamp(time.time(), lang)
